"""Fixed-tau Brier for kill(c) and gate [3] (Claude 2026-10-13, LaDre-approved).

Kill(c) and gate [3] must read paired model-minus-market Brier at the grid
point KILL_BRIER_TAU only -- never the mixed last-tick tau.
"""
import json
import math
import os

import pytest

import paper as pm


def _bw(T, wl_model, wl_mkt, g=None, model_v=None):
    w = {"T": T, "tau": 700, "brier_model": wl_model, "brier_mkt": wl_mkt,
         "model_v": model_v or pm.FAIRVALUE_MODEL_V}
    if g is not None:
        w.update(g_tau=pm.KILL_BRIER_TAU, g_brier_model=g[0],
                 g_brier_mkt=g[1], g_tau_actual=301)
    return w


def _state(bw):
    return {"open": [], "decided_windows": [], "day_pnls": {},
            "day": pm.today_key(), "consec_losses": 0, "disabled": [],
            "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0,
            "brier_windows": bw}


def test_constant_and_hashes():
    assert pm.KILL_BRIER_TAU == 300
    assert pm.signal_hashes() == pm.SIG          # no signal era reset
    assert pm.RISK == "6951f00a2acaaebe"         # intended risk-domain change


def test_grid_diffs_filters_and_dedupes():
    bw = [_bw(1, .1, .2, g=(.30, .10)),
          _bw(1, .1, .2, g=(.90, .10)),              # duplicate T: first kept
          _bw(2, .1, .2),                            # no grid point: skipped
          _bw(3, .1, .2, g=(.20, .10), model_v="old"),   # other model: skipped
          _bw(4, .1, .2, g=(True, .10)),             # bool: skipped
          _bw(5, .1, .2, g=(float("nan"), .10)),     # NaN: skipped
          _bw(6, .1, .2, g=("0.3", .10)),            # string: skipped
          _bw(7, .1, .2, g=(.10, .25))]
    d = pm._brier_grid_diffs(_state(bw))
    assert [round(x, 6) for x in d] == [0.20, -0.15]


def test_kill_fires_on_grid_not_window_level():
    # window level says model much BETTER, grid says model WORSE -> fires
    bw = [_bw(i, 0.0, 0.5, g=(0.30 + (i % 3) * 0.01, 0.10))
          for i in range(120)]
    st = _state(bw)
    halt, reasons = pm.kill_check(st, arm=True)
    assert any("tau=300s" in r for r in reasons), reasons
    assert {"B", "C", "D"} <= set(st["disabled"])


def test_kill_quiet_when_grid_fine_even_if_window_level_bad():
    bw = [_bw(i, 0.5, 0.0, g=(0.10 - (i % 3) * 0.01, 0.10))
          for i in range(120)]
    st = _state(bw)
    halt, reasons = pm.kill_check(st, arm=True)
    assert not any("Brier" in r for r in reasons), reasons
    assert st["disabled"] == []


def test_kill_needs_min_n_grid_windows():
    bw = [_bw(i, 0.0, 0.0, g=(0.9, 0.0)) for i in range(99)]
    bw += [_bw(1000 + i, 0.9, 0.0) for i in range(500)]   # no grid point
    halt, reasons = pm.kill_check(_state(bw), arm=True)
    assert not any("Brier" in r for r in reasons)


def test_score_record_stamps_grid_point():
    pb = {"p_fair": 0.8, "mid_up": 0.7, "tau": 700, "model_v": pm.FAIRVALUE_MODEL_V,
          "tau_grid": {"300": {"p_fair": 0.9, "mid_up": 0.6, "tau_actual": 297},
                       "120": {"p_fair": 0.99, "mid_up": 0.95, "tau_actual": 118}}}
    bw, _ = pm._score_window_record(100, pb, 101.0, 100.0, 0)
    assert bw["g_tau"] == 300 and bw["g_tau_actual"] == 297
    assert math.isclose(bw["g_brier_model"], (0.9 - 1) ** 2)
    assert math.isclose(bw["g_brier_mkt"], (0.6 - 1) ** 2)
    pb["tau_grid"] = {"120": pb["tau_grid"]["120"]}
    bw2, _ = pm._score_window_record(100, pb, 101.0, 100.0, 0)
    assert "g_tau" not in bw2


def test_stamp_grid_brier_backfills_idempotently(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_tick_lock_held", lambda: False)
    monkeypatch.setattr(pm, "_kc_flag_read", lambda: None)
    st = _state([_bw(10, .1, .2), _bw(20, .1, .2), _bw(30, .1, .2, g=(.5, .5))])
    with open(pm.STATE, "w") as f:
        json.dump(st, f)
    with open(pm._scored_windows_path(), "w") as f:
        f.write(json.dumps({"T": 10, "tau_grid_scores": {"300": {
            "brier_model": .4, "brier_mkt": .1, "tau_actual": 305}}}) + "\n")
        f.write(json.dumps({"T": 20, "tau_grid_scores": {"120": {
            "brier_model": .4, "brier_mkt": .1}}}) + "\n")
        f.write(json.dumps({"T": 30, "tau_grid_scores": {"300": {
            "brier_model": .9, "brier_mkt": .0}}}) + "\n")
    assert pm._stamp_grid_brier() == 1
    raw = json.load(open(pm.STATE))
    w = {x["T"]: x for x in raw["brier_windows"]}
    assert w[10]["g_brier_model"] == .4 and w[10]["g_stamped_from"] == "scored_windows"
    assert "g_tau" not in w[20]                      # no grid point: not estimated
    assert w[30]["g_brier_model"] == .5              # already stamped: untouched
    assert pm._stamp_grid_brier() == 0               # idempotent
    assert any(p.startswith("paper_state.json.pre_grid_stamp.")
               for p in os.listdir(tmp_path))


def test_stamp_refuses_when_lock_held(monkeypatch):
    monkeypatch.setattr(pm, "_tick_lock_held", lambda: True)
    monkeypatch.setattr(pm, "_kc_flag_read", lambda: None)
    with pytest.raises(SystemExit):
        pm._stamp_grid_brier()
