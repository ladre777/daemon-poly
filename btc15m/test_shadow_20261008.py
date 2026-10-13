"""72h sigma shadow tests (2026-10-08, LaDre's word).

Covers: shadow_sigma.py cross-checked against research/sigma_walkforward.py
logic; shadow values in pending_brier + tau grid + scored_windows.jsonl;
shadow never affecting decisions; hash invariance; candle archive dry-run.
"""
import copy
import gzip
import math
import random

import pytest

import paper as pm
import shadow_sigma as sh


# ---------- synthetic series ----------

def _synth_series(n=9000, seed=7, vol=2e-5):
    rng = random.Random(seed)
    t0 = 1791400000
    px = 100.0
    rows = []
    for i in range(n):
        px *= math.exp(rng.gauss(0, vol))
        rows.append((t0 + i, round(px, 4), 3))  # (ts, close, tickCount)
    return rows


def _wf_sigma_h60(rows):
    """Replicate research/sigma_walkforward.py's horizon60 estimator."""
    t0 = rows[0][0]
    px = [c for _, c, _ in rows]
    N = len(px)
    q = [0.0] * N
    for i in range(60, N):
        q[i] = math.log(px[i] / px[i - 60])
    i = N - 1  # last tick index
    lo, hi = i - 7200, i
    seg = q[lo:hi + 1]
    m = sum(seg) / len(seg)
    sd = math.sqrt(sum((x - m) ** 2 for x in seg) / (len(seg) - 1))
    return max(sd / math.sqrt(60), 1e-5)


def _wf_p(S, O, sigma, tau):
    return min(max(0.5 * (1 + math.erf(
        math.log(S / O) / (sigma * math.sqrt(tau)) / math.sqrt(2))),
        1e-6), 1 - 1e-6)


# ---------- 1. cross-check vs walk-forward ----------

def test_sigma_horizon60_matches_walkforward():
    rows = _synth_series()
    got = sh.sigma_horizon60(rows)
    exp = _wf_sigma_h60(rows)
    assert got == pytest.approx(exp, rel=1e-9)


def test_sigma_horizon60_matches_walkforward_highvol():
    # High-vol regime: exercises the non-floor path differently.
    rows = _synth_series(seed=99, vol=8e-5)
    assert sh.sigma_horizon60(rows) == pytest.approx(
        _wf_sigma_h60(rows), rel=1e-9)


def test_sigma_floor_3e5():
    assert sh.sigma_floor_3e5(1e-5, 2e-5) == pytest.approx(3e-5)
    assert sh.sigma_floor_3e5(4e-5, 2e-5) == pytest.approx(4e-5)
    assert sh.sigma_floor_3e5(None, None) is None
    assert sh.sigma_floor_3e5(None, 1e-5) == pytest.approx(3e-5)


def test_p_up_matches_walkforward():
    for S, O, sig, tau in [(100.5, 100.0, 2e-5, 300),
                           (99.0, 100.0, 5e-5, 120),
                           (100.0, 100.0, 1e-5, 600)]:
        assert sh.p_up(S, O, sig, tau) == pytest.approx(
            _wf_p(S, O, sig, tau), rel=1e-12)


def test_p_up_clamped_and_guarded():
    # Extreme move + tiny sigma would overflow phi -> clamped.
    assert sh.p_up(200, 100, 1e-9, 600) == 1 - 1e-6
    assert sh.p_up(50, 100, 1e-9, 600) == 1e-6
    assert sh.p_up(0, 100, 2e-5, 300) is None
    assert sh.p_up(100, 100, 2e-5, 0) is None
    assert sh.p_up(100, 100, None, 300) is None


def test_sigma_horizon60_needs_data():
    assert sh.sigma_horizon60([]) is None
    assert sh.sigma_horizon60([(1, 100.0)]) is None


# ---------- 2. pending_brier -> tau grid -> scored jsonl ----------

def _prime_diag_cache(T, rows):
    pm._DIAG_CACHE.clear()
    pm._DIAG_CACHE.update({
        "T": T, "ts": rows[-1][0],
        "diag": {"rv_long": 2e-5, "rv_short": 3e-5},
        "rows": rows,
    })


def _fake_pending(T, S=100.5, O=100.0, tau=300):
    return {
        "p_fair": 0.6, "mid_up": 0.55, "tau": tau,
        "model_v": pm.FAIRVALUE_MODEL_V,
        "event_id": "ev", "O": O, "S": S, "S_source": "last_close",
        "tau_at_score": tau,
        "tau_grid": {
            300: {"p_fair": 0.6, "mid_up": 0.55, "tau_actual": 300,
                  "S": S, "sigma": 2e-5},
            120: {"p_fair": 0.58, "mid_up": 0.54, "tau_actual": 125,
                  "S": 100.4, "sigma": 2e-5},
        },
    }


def test_shadow_fill_writes_pending_and_grid():
    rows = _synth_series()
    T = 1791408000
    _prime_diag_cache(T, rows)
    state = {"pending_brier": {str(T): _fake_pending(T)}}
    snap = {"S": 100.5, "O": 100.0}
    pm._shadow_fill(T, 300, snap, state)

    pb = state["pending_brier"][str(T)]
    assert pb["shadow"]["p_h60"] is not None
    assert pb["shadow"]["p_f3"] is not None
    assert pb["shadow"]["sigma_h60"] == pytest.approx(
        sh.sigma_horizon60(rows))
    assert pb["shadow"]["sigma_f3"] == pytest.approx(3e-5)
    # grid points carry shadow p values beside live p_fair --
    # but ONLY the entry written this tick (Fix B, no lookahead).
    # tau_actual=300 matches this tick's tau=300, so it gets shadow values.
    assert pb["tau_grid"][300]["p_h60"] is not None
    assert pb["tau_grid"][300]["p_f3"] is not None
    assert pb["tau_grid"][300]["sigma_h60"] is not None
    # tau_actual=125 != 300: an older entry, must NOT be re-priced with
    # this tick's sigma (that was the lookahead bug).
    assert "p_h60" not in pb["tau_grid"][120]
    assert "p_f3" not in pb["tau_grid"][120]
    # live fields untouched
    assert pb["p_fair"] == 0.6 and pb["mid_up"] == 0.55


def test_shadow_fill_noop_without_rows():
    pm._DIAG_CACHE.clear()
    T = 1791408000
    pb = _fake_pending(T)
    state = {"pending_brier": {str(T): pb}}
    before = copy.deepcopy(pb)
    pm._shadow_fill(T, 300, {"S": 100.5, "O": 100.0}, state)
    assert state["pending_brier"][str(T)] == before


def test_shadow_fill_noop_wrong_window():
    rows = _synth_series()
    _prime_diag_cache(111, rows)  # cache is for a different T
    T = 1791408000
    pb = _fake_pending(T)
    state = {"pending_brier": {str(T): pb}}
    before = copy.deepcopy(pb)
    pm._shadow_fill(T, 300, {"S": 100.5, "O": 100.0}, state)
    assert state["pending_brier"][str(T)] == before


def test_shadow_scored_into_research_record():
    rows = _synth_series()
    T = 1791408000
    _prime_diag_cache(T, rows)
    pb = _fake_pending(T)
    state = {"pending_brier": {str(T): pb}}
    pm._shadow_fill(T, 300, {"S": 100.5, "O": 100.0}, state)
    pb = state["pending_brier"][str(T)]

    bw, research = pm._score_window_record(T, pb, 100.6, 100.0, 1791409000)
    # outcome: round(100.6,2) >= round(100.0,2) -> up -> oc=1.0
    assert research["outcome"] == 1.0
    sh_rec = research["shadow"]
    assert sh_rec["p_h60"] == pytest.approx(pb["shadow"]["p_h60"])
    assert sh_rec["brier_h60"] == pytest.approx(
        (pb["shadow"]["p_h60"] - 1.0) ** 2)
    assert sh_rec["brier_f3"] == pytest.approx(
        (pb["shadow"]["p_f3"] - 1.0) ** 2)
    # grid shadow briers present per grid point written this tick
    # (Fix B: the 120 entry with tau_actual=125 was NOT re-priced,
    # so its shadow briers are None -- no lookahead)
    assert research["tau_grid_scores"]["300"]["brier_h60"] is not None
    assert research["tau_grid_scores"]["120"]["brier_f3"] is None
    assert research["tau_grid_scores"]["120"]["brier_h60"] is None
    # gate entry stays minimal (no shadow in brier_windows)
    assert "shadow" not in bw


# ---------- 3. shadow never affects decisions ----------

def test_shadow_adds_only_shadow_keys():
    rows = _synth_series()
    T = 1791408000
    _prime_diag_cache(T, rows)
    pb = _fake_pending(T)
    state = {"pending_brier": {str(T): pb}}
    before = copy.deepcopy(pb)
    pm._shadow_fill(T, 300, {"S": 100.5, "O": 100.0}, state)
    after = state["pending_brier"][str(T)]

    assert set(after) - set(before) == {"shadow"}
    for k in before:
        if k == "tau_grid":
            for g in before["tau_grid"]:
                new_keys = (set(after["tau_grid"][g])
                            - set(before["tau_grid"][g]))
                if g == 300:
                    # this-tick entry: shadow p + sigmas (Fix B stores
                    # sigmas so z can be computed at grid points)
                    assert new_keys == {"p_h60", "p_f3",
                                        "sigma_h60", "sigma_f3"}, new_keys
                else:
                    # older entry: untouched (no lookahead re-pricing)
                    assert new_keys == set(), new_keys
                for kk in before["tau_grid"][g]:
                    assert after["tau_grid"][g][kk] == before["tau_grid"][g][kk]
        else:
            assert after[k] == before[k]


def test_shadow_never_raises_on_garbage():
    # None snap, empty rows, missing entry: all must be silent no-ops.
    pm._shadow_fill(1, 300, None, {})
    pm._DIAG_CACHE.clear()
    pm._DIAG_CACHE.update({"T": 1, "ts": 2, "diag": {}, "rows": []})
    pm._shadow_fill(1, 300, {"S": 1, "O": 1},
                    {"pending_brier": {"1": {}}})


def test_decide_functions_do_not_use_shadow():
    # Architectural guarantee: the pure decision functions never touch
    # the shadow module or shadow state.
    import inspect
    for fn in ("decide_sleeve_a", "decide_sleeve_b", "decide_sleeve_c",
               "decide_sleeve_d"):
        body = inspect.getsource(getattr(pm, fn))
        assert "shadow" not in body.lower(), fn


# ---------- 4. hash invariance ----------

def test_sig_risk_hashes_still_pinned():
    # The shadow must not move signal, sizing, or risk inputs.
    assert pm.SIG == {"A": "4acf18e6bd198af9", "B": "9a20c2fd5000cac5",
                      "C": "3a633e3ce6ea21b6", "D": "3c549728a8166b88"}
    assert pm.RISK == "e2af68a6407c920d"
    assert pm.FAIRVALUE_MODEL_V.startswith("fv1:sha256:8f403a0e")
    # shadow_sigma is not part of any hash input
    import hashlib, json
    assert "shadow" not in json.dumps(pm.signal_hashes(), sort_keys=True)


# ---------- 5. candle archive dry-run ----------

def test_archive_day_dry_run(tmp_path, monkeypatch):
    import brti
    from research import archive_candles as ac

    rows = _synth_series(n=9000)
    t0 = rows[0][0]

    def fake_fetch(a, b):
        return [r for r in rows if a <= r[0] <= b]

    monkeypatch.setattr(brti, "fetch_1s_with_tickcount", fake_fetch)
    # day_bounds_utc is real; pick the ET day containing t0.
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    day = datetime.fromtimestamp(t0, tz=timezone.utc).astimezone(
        ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    # monkeypatch day bounds to our synthetic span for determinism
    monkeypatch.setattr(ac, "day_bounds_utc",
                        lambda d: (t0, t0 + 9000))
    path, n = ac.archive_day(day, outdir=str(tmp_path))
    assert path.endswith(f"{day}.csv.gz")
    with gzip.open(path, "rt") as f:
        lines = f.read().splitlines()
    assert lines[0] == "ts,close,tickCount"
    assert len(lines) - 1 == n == 9000
    # idempotent without --force
    with pytest.raises(FileExistsError):
        ac.archive_day(day, outdir=str(tmp_path))
    # --force overwrites
    path2, _ = ac.archive_day(day, outdir=str(tmp_path), force=True)
    assert path2 == path
