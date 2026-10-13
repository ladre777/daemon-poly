#!/usr/bin/env python3
"""Incremental diagnostics buffer sidecar (2026-10-08, per Claude's second
re-review; replaces the 10-minute scalar cache idea, which is NOT built).

Problem: _compute_diagnostics fetched 2h of 1s candles (7,200 rows) on
every tick. When the venue 503s on large ranges, the failing call adds
~4s between decision and re-poll on trade ticks.

Design:
  - Sidecar buffer of the last 7,260 seconds of (ts, close, tickCount),
    written atomically next to STATE (~100-200KB JSON).
  - Per-tick fetch: only the seconds since the last buffered ts
    (normally ~60 candles). Small ranges are proven to work.
  - Backfill: if the buffer is empty or has a gap over 10 minutes, one
    big backfill, at most once per 10 minutes.
  - Breaker: after a failed backfill, skip diagnostics for 10 minutes.
    No retries inside the decision tick.
  - Compute (rv_long, rv_short, sigma_h60 inputs, zero-return and
    gap-fill shares) is done locally from the buffer rows.

All public functions are pure except load/save/refresh, which take
explicit paths and a fetch callable so unit tests never touch the
network or the real state dir. Nothing here raises into the caller:
refresh_rows returns ([], False) on any failure and the caller treats
that as "diagnostics unavailable this tick".
"""
import json
import os
import tempfile

BUFFER_SECS = 7260          # 2h lookback + 60s margin
BACKFILL_COOLDOWN = 600     # at most one big backfill per 10 min
GAP_THRESHOLD = 600         # gap over 10 min triggers a backfill
BREAKER_COOLDOWN = 600      # after a failed backfill, skip diag 10 min
# (2026-10-08, per Claude's third review): silent-failure guards.
INCR_BREAKER = 90   # after a failed incremental fetch, skip diag one tick
STALE_MAX = 90      # newest row older than this => diagnostics unavailable
OVERLAP = 30        # re-fetch the tail so late-arriving candles land


def buffer_path(state_path):
    """Sidecar lives next to STATE."""
    return os.path.join(os.path.dirname(os.path.abspath(state_path)),
                        "diag_buffer.json")


def _empty_buf():
    return {"rows": [], "updated_at": 0, "last_backfill": 0,
            "breaker_until": 0, "incr_failures": {},
            "backfill_failures": {}}


def load_buffer(path):
    """Read the buffer file. Returns a normalized dict; never raises."""
    try:
        with open(path, "r") as f:
            d = json.load(f)
        rows = []
        for r in d.get("rows") or []:
            try:
                ts, close = int(r[0]), float(r[1])
                tc = r[2] if len(r) > 2 else None
                tc = int(tc) if tc is not None else None
                rows.append((ts, close, tc))
            except (TypeError, ValueError, IndexError):
                continue
        rows.sort(key=lambda r: r[0])
        out = _empty_buf()
        out["rows"] = rows
        for k in ("updated_at", "last_backfill", "breaker_until"):
            try:
                out[k] = int(d.get(k) or 0)
            except (TypeError, ValueError):
                out[k] = 0
        try:
            _ic = d.get("incr_failures") or {}
            out["incr_failures"] = {str(k): int(v) for k, v in
                                    _ic.items()} if isinstance(_ic, dict) \
                else {}
        except (TypeError, ValueError, AttributeError):
            out["incr_failures"] = {}
        try:
            _bf = d.get("backfill_failures") or {}
            out["backfill_failures"] = {str(k): int(v) for k, v in
                                       _bf.items()} if isinstance(_bf, dict) \
                else {}
        except (TypeError, ValueError, AttributeError):
            out["backfill_failures"] = {}
        return out
    except (OSError, ValueError):
        return _empty_buf()


def save_buffer(path, buf):
    """Atomic write (tmp + os.replace). Never raises."""
    try:
        rows = [[int(ts), float(close),
                 (int(tc) if tc is not None else None)]
                for ts, close, tc in buf.get("rows") or []]
        try:
            _ic = {str(k): int(v) for k, v in
                   (buf.get("incr_failures") or {}).items()}
        except (TypeError, ValueError, AttributeError):
            _ic = {}
        try:
            _bf = {str(k): int(v) for k, v in
                   (buf.get("backfill_failures") or {}).items()}
        except (TypeError, ValueError, AttributeError):
            _bf = {}
        payload = {"rows": rows,
                   "updated_at": int(buf.get("updated_at") or 0),
                   "last_backfill": int(buf.get("last_backfill") or 0),
                   "breaker_until": int(buf.get("breaker_until") or 0),
                   "incr_failures": _ic,
                   "backfill_failures": _bf}
        d = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(prefix=".diag_buffer.",
                                   suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        pass


def merge_rows(old, new):
    """Merge two ascending (ts, close, tc) lists; new wins on dup ts."""
    by_ts = {}
    for r in old:
        by_ts[int(r[0])] = r
    for r in new:
        by_ts[int(r[0])] = r
    return [by_ts[ts] for ts in sorted(by_ts)]


def trim_rows(rows, t_now, keep_secs=BUFFER_SECS):
    """Keep only rows in [t_now - keep_secs, t_now], ascending."""
    lo = int(t_now) - keep_secs
    hi = int(t_now)
    return [r for r in rows if lo <= int(r[0]) <= hi]


def _note_daily_failure(buf, t_now, key):
    """Persistent daily count of fetch failures under `key`.

    (2026-10-08, per Claude's third review): kept in the sidecar so the
    daily progress report can read it in a few days without digging
    through paper.log. Pruned to the last 7 UTC days. Never raises.
    """
    try:
        import datetime
        day = datetime.datetime.fromtimestamp(int(t_now),
                                              datetime.timezone.utc
                                              ).strftime("%Y-%m-%d")
        counts = buf.get(key) or {}
        if not isinstance(counts, dict):
            counts = {}
        counts = {d: int(c) for d, c in counts.items()
                  if isinstance(d, str) and len(d) == 10}
        counts[day] = int(counts.get(day) or 0) + 1
        # prune to the 7 most recent days
        for d in sorted(counts)[:-7]:
            del counts[d]
        buf[key] = counts
    except Exception:
        pass


def _note_incr_failure(buf, t_now):
    """Persistent daily count of incremental-fetch failures.

    (2026-10-08, per Claude's third review): kept in the sidecar so the
    daily progress report can read it in a few days without digging
    through paper.log. Pruned to the last 7 UTC days. Never raises.
    """
    _note_daily_failure(buf, t_now, "incr_failures")


def _note_backfill_failure(buf, t_now):
    """Persistent daily count of big-backfill failures.

    (2026-10-08, per Claude's fourth review): counted separately from
    incremental failures -- a 503 on the big 7,260-row range is exactly
    what the next few days of data should show. Never raises.
    """
    _note_daily_failure(buf, t_now, "backfill_failures")


def refresh_rows(path, t_now, fetch_fn):
    """Incremental refresh. Returns (rows, ok).

    fetch_fn(a, b) -> [(ts, close, tc)] for the inclusive range [a, b];
    may raise. ok=False means "diagnostics unavailable this tick"
    (breaker active or backfill failed/cooldown); rows is [] then.
    Never raises.
    """
    t_now = int(t_now)
    try:
        buf = load_buffer(path)
        # Breaker: a failed backfill skips diagnostics for 10 minutes.
        if buf["breaker_until"] > t_now:
            return [], False
        rows = buf["rows"]
        if rows:
            last_ts = int(rows[-1][0])
            gap = t_now - last_ts
            if gap <= 0:
                rows = [r for r in rows if int(r[0]) <= t_now]
            elif gap <= GAP_THRESHOLD:
                # Normal tick: fetch only the new seconds, with a 30s
                # overlap so late-arriving candles land. Small ranges are
                # proven to work against the venue.
                try:
                    new_rows = fetch_fn(last_ts - OVERLAP, t_now) or []
                    rows = merge_rows(rows, new_rows)
                except Exception:
                    # (2026-10-08, per Claude's third review): a failed
                    # incremental fetch trips a short breaker -- skip
                    # diagnostics one tick rather than serving rows whose
                    # freshness we can no longer vouch for.
                    buf["breaker_until"] = t_now + INCR_BREAKER
                    _note_incr_failure(buf, t_now)
                    save_buffer(path, buf)
                    return [], False
            else:
                # Gap over 10 min: one big backfill, at most once/10min.
                if t_now - buf["last_backfill"] >= BACKFILL_COOLDOWN:
                    try:
                        rows = fetch_fn(t_now - BUFFER_SECS, t_now) or []
                        buf["last_backfill"] = t_now
                    except Exception:
                        buf["last_backfill"] = t_now
                        buf["breaker_until"] = t_now + BREAKER_COOLDOWN
                        _note_backfill_failure(buf, t_now)
                        save_buffer(path, buf)
                        return [], False
                # else: inside cooldown -- fall through with stale rows.
        else:
            # Empty buffer: big backfill, at most once per 10 min.
            if t_now - buf["last_backfill"] >= BACKFILL_COOLDOWN:
                try:
                    rows = fetch_fn(t_now - BUFFER_SECS, t_now) or []
                    buf["last_backfill"] = t_now
                except Exception:
                    buf["last_backfill"] = t_now
                    buf["breaker_until"] = t_now + BREAKER_COOLDOWN
                    _note_backfill_failure(buf, t_now)
                    save_buffer(path, buf)
                    return [], False
            else:
                return [], False
        rows = trim_rows(rows, t_now)
        buf["rows"] = rows
        buf["updated_at"] = int(rows[-1][0]) if rows else 0
        save_buffer(path, buf)
        # (2026-10-08, per Claude's third review): stale-tail guard.
        # If the newest row is older than STALE_MAX, the diagnostics
        # would silently mismatch live's data vintage. Treat as
        # unavailable -- the shadow no-ops for this tick and the window
        # drops from the complete set for both arms.
        if not rows or t_now - int(rows[-1][0]) > STALE_MAX:
            return [], False
        return rows, True
    except Exception:
        return [], False
