"""Tests for the 2026-10-08 surgical fixes (Fix 1, Fix 2, minors a-d).

Covers: tau-grid nearest-wins, single-fetch diagnostics, module-level diag
cache (never persisted), scored_windows.jsonl dedupe-by-T, monthly rotation,
and SIG/RISK invariance.
"""
import json
import math
import os
import time

import pytest

import paper as pm


@pytest.fixture(autouse=True)
def _clear_diag_cache():
    pm._DIAG_CACHE.clear()
    yield
    pm._DIAG_CACHE.clear()


# ---------- Fix 1: tau-grid nearest-wins ----------

def _entry(p=0.6, m=0.55):
    return {"p_fair": p, "mid_up": m, "S": 100.0, "sigma": 1e-5}


def test_tau_grid_640_misses_then_605_wins():
    g = pm._fill_tau_grid({}, 640, _entry())
    assert g == {}, "tau=640 is 40s from 600, outside tol=30"
    g = pm._fill_tau_grid(g, 605, _entry(p=0.7))
    assert 600 in g and g[600]["tau_actual"] == 605
    assert g[600]["p_fair"] == 0.7


def test_tau_grid_worse_tick_does_not_displace():
    g = pm._fill_tau_grid({}, 605, _entry(p=0.7))
    g2 = pm._fill_tau_grid(g, 630, _entry(p=0.2))
    # 630 is 30s away (>= tol): no fill, and 605 entry survives anyway.
    assert g2[600]["tau_actual"] == 605 and g2[600]["p_fair"] == 0.7
    g3 = pm._fill_tau_grid(g, 598, _entry(p=0.9))
    # 598 is 2s away < 5s: displaces the 605 entry (nearest wins).
    assert g3[600]["tau_actual"] == 598 and g3[600]["p_fair"] == 0.9


def test_tau_grid_midpoint_fills_neither():
    # 150 is exactly 30s from both 120 and 180; with strict < tol,
    # one tick can never fill two points.
    g = pm._fill_tau_grid({}, 150, _entry())
    assert g == {}
    g = pm._fill_tau_grid({}, 149, _entry())
    assert list(g) == [120] and g[120]["tau_actual"] == 149
    g = pm._fill_tau_grid({}, 151, _entry())
    assert list(g) == [180] and g[180]["tau_actual"] == 151


def test_tau_grid_string_keys_survive_json_roundtrip():
    # pending_brier round-trips through JSON: int keys come back as str.
    g = pm._fill_tau_grid({}, 605, _entry(p=0.7))
    rt = json.loads(json.dumps(g))
    assert list(rt) == ["600"]
    g2 = pm._fill_tau_grid(rt, 640, _entry(p=0.1))
    # 600 slot already held (by the 605 tick); 640 is out of tol anyway.
    assert g2["600" if "600" in g2 else 600]["p_fair"] == 0.7
    assert len(g2) == 1


def test_tau_grid_tol_is_30():
    assert pm.TAU_GRID_TOL == 30


# ---------- Fix 2: single-fetch diagnostics ----------

def _synth_rows(t_now, n=7201, seed=11):
    import random
    rng = random.Random(seed)
    rows = []
    px = 100000.0
    for i in range(n):
        ts = t_now - (n - 1) + i
        px *= math.exp(rng.gauss(0, 1e-5))
        tc = 0 if rng.random() < 0.02 else 5
        rows.append((ts, px, tc))
    return rows


def test_compute_diagnostics_single_fetch(monkeypatch, tmp_path):
    import brti
    import diag_buffer
    t_now = 1791435000
    rows = _synth_rows(t_now)
    calls = []

    def fake_fetch(a, b, **kw):
        calls.append((a, b))
        return [r for r in rows if a <= r[0] <= b]

    monkeypatch.setattr(brti, "fetch_1s_with_tickcount", fake_fetch)
    # Buffer path isolated per-test: the sidecar must never touch the
    # real diag_buffer.json during tests.
    bufp = str(tmp_path / "diag_buffer.json")
    monkeypatch.setattr(diag_buffer, "buffer_path", lambda sp: bufp)
    d = pm._compute_diagnostics(t_now)
    assert len(calls) == 1, f"expected 1 venue fetch, got {len(calls)}"
    a, b = calls[0]
    # (2026-10-08, per Claude): the buffer backfills 7260s (2h + 60s
    # margin), not exactly 7200s.
    assert (a, b) == (t_now - diag_buffer.BUFFER_SECS, t_now)

    # rv_long must match fairvalue.realized_sigma's exact math.
    closes = [(ts, c) for ts, c, _ in rows]
    exp_long, _ = pm._sd_of_logrets(closes)
    assert d["rv_long"] == pytest.approx(exp_long)
    short = [(ts, c) for ts, c in closes if ts >= t_now - 900]
    exp_short, rets_short = pm._sd_of_logrets(short)
    assert d["rv_short"] == pytest.approx(exp_short)
    exp_zero = sum(1 for r in rets_short if r == 0.0) / len(rets_short)
    assert d["zero_ret_share_15m"] == pytest.approx(exp_zero)
    tcs = [tc for ts, _, tc in rows if ts >= t_now - 900]
    exp_gap = sum(1 for tc in tcs if tc == 0) / len(tcs)
    assert d["gapfill_share_15m"] == pytest.approx(exp_gap)
    # consistency with the old formula: sigma_used = max(rvs, floor)
    from fairvalue import SIGMA_FLOOR
    assert d["sigma_used"] == pytest.approx(max(exp_long, exp_short,
                                               SIGMA_FLOOR))


def test_compute_diagnostics_never_raises(monkeypatch, tmp_path):
    import brti
    import diag_buffer
    monkeypatch.setattr(brti, "fetch_1s_with_tickcount",
                        lambda a, b, **kw: (_ for _ in ()).throw(
                            RuntimeError("venue down")))
    # Isolate the sidecar: a failed backfill writes breaker state.
    bufp = str(tmp_path / "diag_buffer.json")
    monkeypatch.setattr(diag_buffer, "buffer_path", lambda sp: bufp)
    d = pm._compute_diagnostics(1791435000)
    assert d["rv_long"] is None and d["gapfill_share_15m"] is None


# ---------- minor (a): diag cache never persisted ----------

def test_tick_diagnostics_not_written_to_state(monkeypatch):
    sentinel = {"rv_long": 1e-5}
    monkeypatch.setattr(pm, "_compute_diagnostics", lambda now: sentinel)
    state = {"open": [], "decided_windows": []}
    out = pm._tick_diagnostics(state, 12345, 999)
    assert out is sentinel
    assert "diag_cache" not in state
    # second call hits the module cache: no recompute.
    calls = []
    monkeypatch.setattr(pm, "_compute_diagnostics",
                        lambda now: calls.append(now) or sentinel)
    out2 = pm._tick_diagnostics(state, 12345, 1000)
    assert out2 is sentinel and calls == []


# ---------- minor (b): dedupe by T, keep last ----------

def test_scored_windows_dedupe_keeps_last(tmp_path):
    p = pm._scored_windows_path()
    pm.append_jsonl(p, {"T": 1, "p_fair": 0.5, "tau_grid_scores": {}})
    pm.append_jsonl(p, {"T": 2, "p_fair": 0.6, "tau_grid_scores": {}})
    pm.append_jsonl(p, {"T": 1, "p_fair": 0.9, "tau_grid_scores": {}})
    recs = pm._read_scored_windows()
    assert [r["T"] for r in recs] == [1, 2]
    assert recs[0]["p_fair"] == 0.9, "last line per T wins"


def test_scored_windows_reader_includes_archives(tmp_path):
    stem = pm._scored_windows_path()[:-len(".jsonl")]
    with open(stem + ".2026-09.jsonl", "w") as f:
        f.write(json.dumps({"T": 7, "p_fair": 0.4,
                            "tau_grid_scores": {}}) + "\n")
    pm.append_jsonl(pm._scored_windows_path(),
                    {"T": 8, "p_fair": 0.5, "tau_grid_scores": {}})
    recs = pm._read_scored_windows()
    assert [r["T"] for r in recs] == [7, 8]


# ---------- minor (c): monthly rotation ----------

def test_monthly_rotation_archives_old_file(tmp_path):
    p = pm._scored_windows_path()
    pm.append_jsonl(p, {"T": 1})
    # fake an mtime from two months ago
    old = time.time() - 62 * 86400
    os.utime(p, (old, old))
    import datetime
    mt = datetime.datetime.fromtimestamp(old)
    expect = f"{p[:-len('.jsonl')]}.{mt.year:04d}-{mt.month:02d}.jsonl"
    pm._append_scored_windows({"T": 2})
    assert os.path.exists(expect), "old month archived"
    with open(p) as f:
        lines = f.readlines()
    assert len(lines) == 1 and json.loads(lines[0])["T"] == 2


def test_monthly_rotation_noop_when_current(tmp_path):
    p = pm._model_only_path()
    pm._append_model_only({"ts": 1})
    pm._append_model_only({"ts": 2})
    assert len(open(p).readlines()) == 2
    assert not [f for f in os.listdir(tmp_path) if f.startswith(
        "model_only.") and f != "model_only.jsonl"]


# ---------- hash invariance ----------

def test_sig_risk_hashes_still_pinned():
    assert pm.SIG == {'A': '4acf18e6bd198af9', 'B': '9a20c2fd5000cac5',
                      'C': '3a633e3ce6ea21b6', 'D': '3c549728a8166b88'}
    assert pm.RISK == 'e2af68a6407c920d'
