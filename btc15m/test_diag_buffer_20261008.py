"""Incremental diag_buffer sidecar tests (2026-10-08, per Claude's second
re-review).

Covers: incremental == one-shot fetch; sigma math == _compute_diagnostics
on the same rows; gap handling; backfill cooldown; the breaker; staleness
alignment in _shadow_fill; _grid_zs complete-window enforcement.
All offline -- fetch is always a fake.
"""
import copy
import math
import os
import random

import pytest

import diag_buffer as db
import paper as pm
import shadow_sigma as sh


# ---------- synthetic rows ----------

def _synth_rows(t_end, n=7300, seed=11, vol=2e-5):
    rng = random.Random(seed)
    px = 100.0
    rows = []
    for i in range(n):
        px *= math.exp(rng.gauss(0, vol))
        rows.append((t_end - n + 1 + i, round(px, 4), 3))
    return rows


def _buf_path(tmp_path):
    return str(tmp_path / "diag_buffer.json")


# ---------- 1. incremental == one-shot ----------

def test_incremental_equals_oneshot(tmp_path):
    t0 = 1791480000
    full = _synth_rows(t0)          # the "venue truth"
    path = _buf_path(tmp_path)

    # Seed the buffer as if a backfill ran 120s ago.
    seed_rows = [r for r in full if r[0] <= t0 - 120]
    db.save_buffer(path, {"rows": seed_rows, "updated_at": seed_rows[-1][0],
                           "last_backfill": t0 - 120, "breaker_until": 0})

    calls = []

    def fake_fetch(a, b):
        calls.append((a, b))
        return [r for r in full if a <= r[0] <= b]

    rows, ok = db.refresh_rows(path, t0, fake_fetch)
    assert ok
    # Only the missing tail was fetched (small range), with a 30s
    # overlap so late-arriving candles land.
    assert len(calls) == 1
    a, b = calls[0]
    assert a == seed_rows[-1][0] - db.OVERLAP and b == t0
    assert b - a <= 130 + db.OVERLAP
    # Merged rows equal the one-shot fetch exactly.
    assert rows == [r for r in full if t0 - db.BUFFER_SECS <= r[0] <= t0]


def test_first_run_backfills_once(tmp_path):
    t0 = 1791480000
    full = _synth_rows(t0)
    path = _buf_path(tmp_path)
    calls = []

    def fake_fetch(a, b):
        calls.append((a, b))
        return [r for r in full if a <= r[0] <= b]

    rows, ok = db.refresh_rows(path, t0, fake_fetch)
    assert ok and len(calls) == 1
    a, b = calls[0]
    assert (a, b) == (t0 - db.BUFFER_SECS, t0)
    assert rows == [r for r in full if a <= r[0] <= b]


# ---------- 2. sigma math matches _compute_diagnostics ----------

def test_diagnostics_from_rows_matches_compute(monkeypatch):
    import brti
    t_now = 1791480000
    full = _synth_rows(t_now)

    def fake_fetch(a, b, **kw):
        return [r for r in full if a <= r[0] <= b]

    monkeypatch.setattr(brti, "fetch_1s_with_tickcount", fake_fetch)
    # _compute_diagnostics now sources rows from a temp buffer dir.
    monkeypatch.setattr(db, "buffer_path", lambda sp: "/tmp/_tbuf_x.json")
    try:
        os.unlink("/tmp/_tbuf_x.json")
    except OSError:
        pass
    d_new = pm._compute_diagnostics(t_now)

    rows_2h = [r for r in full if r[0] >= t_now - 7200]
    d_rows = pm._diagnostics_from_rows(rows_2h, t_now)
    for k in ("rv_long", "rv_short", "sigma_used",
              "zero_ret_share_15m", "gapfill_share_15m"):
        assert d_new[k] == pytest.approx(d_rows[k]), k
    assert d_new["floor_bound"] == d_rows["floor_bound"]
    try:
        os.unlink("/tmp/_tbuf_x.json")
    except OSError:
        pass


def test_diagnostics_from_rows_pure_math():
    t_now = 1791480000
    rows = _synth_rows(t_now)
    d = pm._diagnostics_from_rows(rows, t_now)
    closes = [(ts, c) for ts, c, _ in rows if ts >= t_now - 7200]
    exp_long, _ = pm._sd_of_logrets(closes)
    assert d["rv_long"] == pytest.approx(exp_long)
    # margin rows beyond 2h are ignored
    extra = [(t_now - 7260 + i, 100.0, 3) for i in range(50)]
    d2 = pm._diagnostics_from_rows(extra + rows, t_now)
    assert d2["rv_long"] == pytest.approx(d["rv_long"])


# ---------- 3. gap handling ----------

def test_gap_over_10min_triggers_backfill(tmp_path):
    t0 = 1791480000
    full = _synth_rows(t0)
    path = _buf_path(tmp_path)
    stale = [r for r in full if r[0] <= t0 - 3600]  # 1h gap
    db.save_buffer(path, {"rows": stale, "updated_at": stale[-1][0],
                           "last_backfill": t0 - 3600, "breaker_until": 0})
    calls = []

    def fake_fetch(a, b):
        calls.append((a, b))
        return [r for r in full if a <= r[0] <= b]

    rows, ok = db.refresh_rows(path, t0, fake_fetch)
    assert ok and len(calls) == 1
    assert calls[0] == (t0 - db.BUFFER_SECS, t0)
    assert rows[-1][0] == t0


def test_backfill_cooldown_uses_stale_rows(tmp_path):
    t0 = 1791480000
    full = _synth_rows(t0)
    path = _buf_path(tmp_path)
    stale = [r for r in full if r[0] <= t0 - 3600]
    # backfill attempted 60s ago (inside the 10-min cooldown)
    db.save_buffer(path, {"rows": stale, "updated_at": stale[-1][0],
                           "last_backfill": t0 - 60, "breaker_until": 0})
    calls = []

    def fake_fetch(a, b):
        calls.append((a, b))
        return [r for r in full if a <= r[0] <= b]

    rows, ok = db.refresh_rows(path, t0, fake_fetch)
    # (2026-10-08, per Claude's third review): no fetch inside the
    # cooldown, but 1h-stale rows are NOT served as ok -- the stale-tail
    # guard makes diagnostics unavailable instead.
    assert not ok and rows == [] and calls == []


# ---------- 4. breaker ----------

def test_breaker_after_failed_backfill(tmp_path):
    t0 = 1791480000
    path = _buf_path(tmp_path)

    def boom(a, b):
        raise RuntimeError("venue 503")

    rows, ok = db.refresh_rows(path, t0, boom)
    assert not ok and rows == []
    buf = db.load_buffer(path)
    assert buf["breaker_until"] == t0 + db.BREAKER_COOLDOWN

    # Inside the breaker window: no fetch attempted, still unavailable.
    calls = []

    def fake_fetch(a, b):
        calls.append((a, b))
        return []

    rows2, ok2 = db.refresh_rows(path, t0 + 60, fake_fetch)
    assert not ok2 and calls == []

    # After the breaker expires: backfill is retried.
    full = _synth_rows(t0 + 700)

    def good_fetch(a, b):
        calls.append((a, b))
        return [r for r in full if a <= r[0] <= b]

    rows3, ok3 = db.refresh_rows(path, t0 + 700, good_fetch)
    assert ok3 and len(calls) == 1


def test_incremental_failure_trips_short_breaker(tmp_path):
    # (2026-10-08, per Claude's third review): a failed incremental
    # fetch trips a 90s breaker and returns unavailable -- no silent
    # stale rows.
    t0 = 1791480000
    full = _synth_rows(t0)
    path = _buf_path(tmp_path)
    seed_rows = [r for r in full if r[0] <= t0 - 120]
    db.save_buffer(path, {"rows": seed_rows, "updated_at": seed_rows[-1][0],
                           "last_backfill": t0 - 120, "breaker_until": 0})

    def boom(a, b):
        raise RuntimeError("blip")

    rows, ok = db.refresh_rows(path, t0, boom)
    assert not ok and rows == []
    buf = db.load_buffer(path)
    assert buf["breaker_until"] == t0 + db.INCR_BREAKER
    # The failure is logged persistently for the progress report.
    import datetime
    day = datetime.datetime.fromtimestamp(t0, datetime.timezone.utc
                                          ).strftime("%Y-%m-%d")
    assert buf["incr_failures"].get(day) == 1

    # Next tick inside the 90s breaker: no fetch attempted.
    calls = []

    def fake_fetch(a, b):
        calls.append((a, b))
        return [r for r in full if a <= r[0] <= b]

    rows2, ok2 = db.refresh_rows(path, t0 + 30, fake_fetch)
    assert not ok2 and calls == []

    # After the short breaker expires, the incremental fetch is retried.
    # (The fake venue must have rows up to the new t_now, else the
    # stale-tail guard -- correctly -- reports unavailable.)
    full2 = _synth_rows(t0 + 120)

    def fake_fetch2(a, b):
        calls.append((a, b))
        return [r for r in full2 if a <= r[0] <= b]

    rows3, ok3 = db.refresh_rows(path, t0 + 120, fake_fetch2)
    assert ok3 and len(calls) == 1


def test_silent_empty_fetch_with_stale_tail_is_unavailable(tmp_path):
    # Venue returns [] without raising, but the buffered tail is older
    # than STALE_MAX: must not serve stale rows as ok.
    t0 = 1791480000
    full = _synth_rows(t0)
    path = _buf_path(tmp_path)
    stale = [r for r in full if r[0] <= t0 - 200]  # tail 200s old
    db.save_buffer(path, {"rows": stale, "updated_at": stale[-1][0],
                           "last_backfill": t0 - 200, "breaker_until": 0})
    rows, ok = db.refresh_rows(path, t0, lambda a, b: [])
    assert not ok and rows == []


def test_lagging_tail_is_unavailable(tmp_path):
    # Fetch succeeds but the venue's newest row still lags > STALE_MAX:
    # stale-row mismatches must be zero.
    t0 = 1791480000
    full = _synth_rows(t0)
    path = _buf_path(tmp_path)
    seed_rows = [r for r in full if r[0] <= t0 - 120]
    db.save_buffer(path, {"rows": seed_rows, "updated_at": seed_rows[-1][0],
                           "last_backfill": t0 - 120, "breaker_until": 0})

    def lagging_fetch(a, b):
        # venue only has rows up to t0 - 200 (lagging tail)
        return [r for r in full if a <= r[0] <= t0 - 200]

    rows, ok = db.refresh_rows(path, t0, lagging_fetch)
    assert not ok and rows == []
    # and the rows that WOULD have been served are indeed stale
    buf = db.load_buffer(path)
    assert t0 - buf["rows"][-1][0] > db.STALE_MAX


def test_backfill_always_failing_limits_big_calls(tmp_path):
    # Breaker still limits big backfill calls to 1 per 10 min even when
    # every backfill fails.
    t0 = 1791480000
    path = _buf_path(tmp_path)
    calls = []

    def boom(a, b):
        calls.append((a, b))
        raise RuntimeError("venue 503")

    for dt in (0, 60, 300, 590):
        rows, ok = db.refresh_rows(path, t0 + dt, boom)
        assert not ok
    # Only the first attempt made a big call; the rest hit the breaker.
    assert len(calls) == 1
    assert calls[0] == (t0 - db.BUFFER_SECS, t0)


def test_atomic_write_roundtrip(tmp_path):
    path = _buf_path(tmp_path)
    rows = _synth_rows(1791480000, n=100)
    db.save_buffer(path, {"rows": rows, "updated_at": rows[-1][0],
                           "last_backfill": 1, "breaker_until": 0})
    buf = db.load_buffer(path)
    assert buf["rows"] == rows
    assert buf["updated_at"] == rows[-1][0]
    # corrupt file -> normalized empty, never raises
    with open(path, "w") as f:
        f.write("not json{{{")
    assert db.load_buffer(path)["rows"] == []


# ---------- 5. staleness alignment in _shadow_fill ----------

def _prime(T, rows, sigma_ts=None):
    pm._DIAG_CACHE.clear()
    pm._DIAG_CACHE.update({
        "T": T, "ts": rows[-1][0],
        "diag": {"rv_long": 2e-5, "rv_short": 3e-5},
        "rows": rows,
    })


def _pending(T):
    return {"p_fair": 0.6, "mid_up": 0.55, "tau": 300,
            "model_v": pm.FAIRVALUE_MODEL_V, "event_id": "ev",
            "O": 100.0, "S": 100.5, "S_source": "last_close",
            "tau_at_score": 300, "tau_grid": {}}


def test_shadow_uses_rows_up_to_sigma_cache_ts():
    rows = _synth_rows(1791480000, n=7300)
    T = 1791408000
    _prime(T, rows)
    sigma_ts = rows[-1][0] - 300  # live sigma lags 5 min
    state = {"pending_brier": {str(T): _pending(T)},
             "sigma_cache": {"T": T, "ts": sigma_ts, "sigma": 2e-5}}
    pm._shadow_fill(T, 300, {"S": 100.5, "O": 100.0}, state)
    pb = state["pending_brier"][str(T)]
    truncated = [r for r in rows if r[0] <= sigma_ts]
    assert pb["shadow"]["sigma_h60"] == pytest.approx(
        sh.sigma_horizon60(truncated))
    # and NOT the fresh-tick value
    assert pb["shadow"]["sigma_h60"] != pytest.approx(
        sh.sigma_horizon60(rows))


def test_shadow_falls_back_without_sigma_cache():
    rows = _synth_rows(1791480000, n=7300)
    T = 1791408000
    _prime(T, rows)
    state = {"pending_brier": {str(T): _pending(T)}}  # no sigma_cache
    pm._shadow_fill(T, 300, {"S": 100.5, "O": 100.0}, state)
    pb = state["pending_brier"][str(T)]
    assert pb["shadow"]["sigma_h60"] == pytest.approx(
        sh.sigma_horizon60(rows))


# ---------- 6. _grid_zs complete-window enforcement ----------

def _rec(complete=True, drop=None):
    gs = {}
    for g in ("300", "450", "600"):
        gs[g] = {"brier_model": 0.2, "brier_h60": 0.19, "brier_f3": 0.21,
                 "sigma_h60": 2e-5, "sigma_f3": 3e-5,
                 "S": 100.0, "tau_actual": int(g)}
    if not complete:
        del gs[drop or "600"]
    return {"settle_price": 101.0, "tau_grid_scores": gs}


def test_grid_zs_skips_incomplete_windows():
    from research.shadow_decision import _grid_zs
    full = _rec(complete=True)
    partial = _rec(complete=False, drop="300")
    zs_full = _grid_zs([full], "h60")
    # grid 300 only since the 2026-10-09 tau=300 amendment (was 3)
    assert len(zs_full) == 1
    # incomplete window contributes zero z's
    assert _grid_zs([partial], "h60") == []
    assert len(_grid_zs([full, partial], "h60")) == 1


# ---------- 7. third-review: fetch bounds, brti defaults, heartbeat ----------

def test_brti_defaults_untouched():
    # (2026-10-08, per Claude's third review): brti.py must keep
    # timeout=45, retries=2 -- the diagnostics path bounds itself via
    # kwargs instead of moving the shared defaults.
    import inspect
    import brti
    sig = inspect.signature(brti.fetch_1s_with_tickcount)
    assert sig.parameters["timeout"].default == 45
    assert sig.parameters["retries"].default == 2


def test_diag_fetch_small_range_bounded(monkeypatch, tmp_path):
    # Incremental path: small range -> timeout=8, retries=0.
    import brti
    t0 = 1791480000
    full = _synth_rows(t0)
    path = _buf_path(tmp_path)
    seed_rows = [r for r in full if r[0] <= t0 - 60]
    db.save_buffer(path, {"rows": seed_rows, "updated_at": seed_rows[-1][0],
                           "last_backfill": t0 - 60, "breaker_until": 0})
    seen = {}

    def fake_fetch(a, b, timeout=None, retries=None):
        seen["timeout"] = timeout
        seen["retries"] = retries
        seen["span"] = b - a
        return [r for r in full if a <= r[0] <= b]

    monkeypatch.setattr(brti, "fetch_1s_with_tickcount", fake_fetch)
    monkeypatch.setattr(db, "buffer_path", lambda sp: path)
    d = pm._compute_diagnostics(t0)
    assert seen["span"] <= 1000
    assert seen["timeout"] == 8 and seen["retries"] == 0
    assert d["rv_long"] is not None


def test_diag_fetch_big_range_bounded(monkeypatch, tmp_path):
    # Backfill path: big range -> timeout=30, retries=0.
    import brti
    t0 = 1791480000
    full = _synth_rows(t0)
    path = _buf_path(tmp_path)
    seen = {}

    def fake_fetch(a, b, timeout=None, retries=None):
        seen["timeout"] = timeout
        seen["retries"] = retries
        seen["span"] = b - a
        return [r for r in full if a <= r[0] <= b]

    monkeypatch.setattr(brti, "fetch_1s_with_tickcount", fake_fetch)
    monkeypatch.setattr(db, "buffer_path", lambda sp: path)
    d = pm._compute_diagnostics(t0)
    assert seen["span"] > 1000
    assert seen["timeout"] == 30 and seen["retries"] == 0
    assert d["rv_long"] is not None


def test_diag_last_tracks_unavailable(monkeypatch, tmp_path):
    # _DIAG_LAST drives the three-state heartbeat: ok=False + age when
    # the buffer reports unavailable.
    import brti
    t0 = 1791480000
    path = _buf_path(tmp_path)
    monkeypatch.setattr(brti, "fetch_1s_with_tickcount",
                        lambda a, b, **kw: (_ for _ in ()).throw(
                            RuntimeError("venue down")))
    monkeypatch.setattr(db, "buffer_path", lambda sp: path)
    pm._compute_diagnostics(t0)
    assert pm._DIAG_LAST["ok"] is False
    assert pm._DIAG_LAST["age"] == -1
    # and a healthy tick flips it back
    full = _synth_rows(t0 + 700)

    def good_fetch(a, b, **kw):
        return [r for r in full if a <= r[0] <= b]

    monkeypatch.setattr(brti, "fetch_1s_with_tickcount", good_fetch)
    pm._compute_diagnostics(t0 + 700)
    assert pm._DIAG_LAST["ok"] is True
    assert pm._DIAG_LAST["age"] >= 0


# ---------- 8. third-review: _shadow_fill alignment guards ----------

def test_shadow_ignores_other_window_sigma_cache():
    # sigma_cache belonging to a DIFFERENT window must not truncate rows.
    rows = _synth_rows(1791480000, n=7300)
    T = 1791408000
    _prime(T, rows)
    state = {"pending_brier": {str(T): _pending(T)},
             "sigma_cache": {"T": T - 900, "ts": rows[-1][0] - 300,
                             "sigma": 2e-5}}
    pm._shadow_fill(T, 300, {"S": 100.5, "O": 100.0}, state)
    pb = state["pending_brier"][str(T)]
    assert pb["shadow"]["sigma_h60"] == pytest.approx(
        sh.sigma_horizon60(rows))


def test_shadow_skips_when_rows_stale_vs_sigma_ts():
    # Rows don't reach within 120s of the cached sigma ts: no shadow
    # this tick (window drops from the complete set for both arms).
    rows = _synth_rows(1791480000, n=7300)
    T = 1791408000
    _prime(T, rows)
    sigma_ts = rows[-1][0] + 500  # cache ahead of available rows
    state = {"pending_brier": {str(T): _pending(T)},
             "sigma_cache": {"T": T, "ts": sigma_ts, "sigma": 2e-5}}
    pm._shadow_fill(T, 300, {"S": 100.5, "O": 100.0}, state)
    pb = state["pending_brier"][str(T)]
    assert "shadow" not in pb


# ---------- 9. fourth-review: backfill failures counted separately ----------

def test_backfill_failure_counted_separately(tmp_path):
    # (2026-10-08, per Claude's fourth review): a failed big backfill
    # increments backfill_failures (not incr_failures) and trips the
    # 10-minute breaker.
    import datetime
    t0 = 1791480000
    path = _buf_path(tmp_path)
    db.save_buffer(path, {"rows": [], "updated_at": 0,
                          "last_backfill": 0, "breaker_until": 0})

    def boom(a, b):
        raise RuntimeError("503")

    rows, ok = db.refresh_rows(path, t0, boom)
    assert not ok and rows == []
    buf = db.load_buffer(path)
    assert buf["breaker_until"] == t0 + db.BREAKER_COOLDOWN
    day = datetime.datetime.fromtimestamp(t0, datetime.timezone.utc
                                          ).strftime("%Y-%m-%d")
    assert buf["backfill_failures"].get(day) == 1
    assert buf["incr_failures"].get(day) is None


def test_backfill_failure_on_gap_also_counted(tmp_path):
    # Same for the gap-triggered (non-empty buffer) backfill path.
    import datetime
    t0 = 1791480000
    full = _synth_rows(t0 - 3600)  # stale: gap > 10 min
    path = _buf_path(tmp_path)
    db.save_buffer(path, {"rows": full, "updated_at": full[-1][0],
                          "last_backfill": 0, "breaker_until": 0})

    def boom(a, b):
        raise RuntimeError("503")

    rows, ok = db.refresh_rows(path, t0, boom)
    assert not ok and rows == []
    buf = db.load_buffer(path)
    day = datetime.datetime.fromtimestamp(t0, datetime.timezone.utc
                                          ).strftime("%Y-%m-%d")
    assert buf["backfill_failures"].get(day) == 1
