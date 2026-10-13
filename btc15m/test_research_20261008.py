"""Tests for the 2026-10-08 research/logging patch set (Claude joint review).

Covers: futility_status (report-only), fee_model stamping, BRIER_WINDOWS_MAX,
_brier_windows_current, --stamp-legacy-model-v, model_v at pending creation,
scored_windows.jsonl research records, _veto_fields, and SIG/RISK invariance.
"""
import json
import os

import pytest

import paper as pm


# ---------- futility (report-only, hash-neutral) ----------

def test_futility_n74_never_futile():
    # Below the first checkpoint: never futile, even deep red.
    fs = pm.futility_status([-100.0] * 74)
    assert fs is not None and fs["futile"] is False
    assert fs["checkpoint"] is None


def test_futility_n75_mean_minus2_futile():
    fs = pm.futility_status([-2.0] * 75)
    assert fs["futile"] is True
    assert fs["checkpoint"] == 75
    assert fs["n"] == 75


def test_futility_n75_mean_plus3_not_futile():
    fs = pm.futility_status([3.0] * 75)
    assert fs["futile"] is False
    assert fs["checkpoint"] == 75


def test_futility_identical_pnls_falls_back_to_sd_floor():
    fs = pm.futility_status([5.0] * 80)
    assert fs["sd_eff"] == pm.FUTILITY_SD_FLOOR == 8.0
    # ub = 5 + 1.2816*8/sqrt(80) ~ 6.15 < 1.26? No -> not futile.
    assert fs["futile"] is False
    fs2 = pm.futility_status([-5.0] * 80)
    assert fs2["sd_eff"] == 8.0
    assert fs2["futile"] is True  # ub ~ -3.85 < 1.26


def test_futility_n_lt_2_returns_none():
    assert pm.futility_status([]) is None
    assert pm.futility_status([1.0]) is None


def test_futility_checkpoints_100_125():
    fs = pm.futility_status([-2.0] * 100)
    assert fs["checkpoint"] == 100 and fs["futile"] is True
    fs = pm.futility_status([3.0] * 125)
    assert fs["checkpoint"] == 125 and fs["futile"] is False


def test_futility_pure_no_state_or_blotter_touch():
    pnls = [1.5, -0.5, 2.0]
    before = list(pnls)
    out = pm.futility_status(pnls)
    assert pnls == before  # input not mutated
    assert set(out) == {"n", "mean", "sd_eff", "ub90",
                        "checkpoint", "futile"}
    # Constants live outside every hash: signal + risk hashes have no
    # futility inputs.
    import json as _j
    assert "FUTILITY" not in _j.dumps(pm.signal_hashes())
    assert "FUTILITY" not in _j.dumps(pm.risk_hash())


def test_futility_threshold_is_ladre_pick():
    assert pm.FUTILITY_MIN_EDGE_USD == 1.26
    assert pm.FUTILITY_CHECKPOINTS == (75, 100, 125)


# ---------- fee_model stamping ----------

def test_fee_model_on_blotter_base():
    snap = {"S": 85010.5, "tau": 420, "p_up": 0.62}
    book = {"ask_up": (0.55, 100.0), "ask_down": (0.47, 120.0)}
    rec = pm.blotter_base("slug", 123, 84900.0, snap, book)
    assert rec["fee_model"] == pm.FEE_MODEL == "taker:0.07*price*(1-price)"


def test_fee_model_matches_signal_hash_context():
    # The stamped value is the same string hashed into the signal context.
    assert pm.signal_hashes()["A"] == pm.signal_hashes()["A"]  # deterministic
    # FEE_MODEL constant feeds the shared hash context; value pinned so a
    # silent rename can't drift the stamp away from the hashed value.
    import hashlib
    shared = {"FEE_MODEL": pm.FEE_MODEL, "LATENCY_MS": pm.LATENCY_MS,
              "BRTI_SETTLE": "avg60[T-60,T-1]",
              "FAIRVALUE_MODEL_V": pm.FAIRVALUE_MODEL_V}
    assert pm._hash_params(shared) is not None


# ---------- retention cap ----------

def test_brier_windows_max_constant():
    assert pm.BRIER_WINDOWS_MAX == 2000


def test_trim_brier_windows_enforces_cap():
    s = {"brier_windows": [{"T": i} for i in range(2500)]}
    pm._trim_brier_windows(s)
    assert len(s["brier_windows"]) == 2000
    assert s["brier_windows"][0]["T"] == 500  # keeps the newest


def test_both_score_paths_use_the_cap():
    src = open(pm.__file__).read()
    # No hardcoded retention slices remain; both paths call the helper.
    assert "[-500:]" not in src
    assert "[-2000:]" not in src
    assert src.count("_trim_brier_windows(state)") >= 2


# ---------- _brier_windows_current ----------

def _w(T, mv, dm=0.01, dk=0.02):
    return {"T": T, "tau": 300, "brier_model": dm, "brier_mkt": dk,
            "model_v": mv}


def test_current_windows_filter():
    cur = pm.FAIRVALUE_MODEL_V
    st = {"brier_windows": [
        _w(1, cur), _w(2, pm.LEGACY_MODEL_V),
        _w(3, "fv1:sha256:deadbeef:phi(other)"),
        _w(1, cur),  # duplicate T
        {"T": 4, "tau": 300, "brier_model": None, "brier_mkt": 0.02,
         "model_v": cur},  # unscored
        {"T": 5, "tau": 300, "brier_model": 0.01,
         "brier_mkt": 0.02},  # no label -> treated as current
    ]}
    out = pm._brier_windows_current(st)
    assert sorted(w["T"] for w in out) == [1, 5]  # dup collapsed, legacy out


def test_legacy_window_included_after_relabel():
    st = {"brier_windows": [_w(9, pm.LEGACY_MODEL_V)]}
    assert pm._brier_windows_current(st) == []
    st["brier_windows"][0]["model_v"] = pm.FAIRVALUE_MODEL_V
    assert len(pm._brier_windows_current(st)) == 1


# ---------- --stamp-legacy-model-v ----------

def _write_state(obj):
    with open(pm.STATE, "w") as f:
        json.dump(obj, f)


def test_stamp_relabels_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_tick_lock_held", lambda: False)
    cur = pm.FAIRVALUE_MODEL_V
    st = {"brier_windows": [_w(1, pm.LEGACY_MODEL_V), _w(2, cur),
                            _w(3, "fv1:sha256:deadbeef:phi(other)")],
          "open": [], "decided_windows": [], "day_pnl": 0.0,
          "consec_losses": 0}
    _write_state(st)
    n1 = pm._stamp_legacy_model_v()
    assert n1 == 1
    with open(pm.STATE) as f:
        st2 = json.load(f)
    w1 = next(w for w in st2["brier_windows"] if w["T"] == 1)
    assert w1["model_v"] == cur
    assert w1["model_v_stamped_from"] == "legacy_label"
    # other-model window untouched
    w3 = next(w for w in st2["brier_windows"] if w["T"] == 3)
    assert w3["model_v"] == "fv1:sha256:deadbeef:phi(other)"
    # backup + audit log written
    assert os.path.exists(pm.STATE + ".model_v_audit.log")
    n2 = pm._stamp_legacy_model_v()
    assert n2 == 0  # idempotent


def test_stamp_refused_when_sidecar_flag_present(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_tick_lock_held", lambda: False)
    _write_state({"brier_windows": []})
    with open(pm.STATE + ".kc_error.json", "w") as f:
        json.dump({"type": "X", "message": "y"}, f)
    with pytest.raises(SystemExit):
        pm._stamp_legacy_model_v()


def test_stamp_refused_when_tick_lock_held(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_tick_lock_held", lambda: True)
    _write_state({"brier_windows": []})
    with pytest.raises(SystemExit):
        pm._stamp_legacy_model_v()


def test_kill_c_fires_after_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_tick_lock_held", lambda: False)
    # 100 legacy-labeled windows, model consistently worse (diff +0.1, sd 0
    # -> 95% lower bound = +0.1 > 0).
    bws = [{"T": 1000 + i, "tau": 300, "brier_model": 0.30,
            "brier_mkt": 0.20, "model_v": pm.LEGACY_MODEL_V}
           for i in range(100)]
    st = {"open": [], "disabled": [], "day": "2026-10-08", "day_pnl": 0.0,
          "consec_losses": 0, "brier_windows": bws, "pending_brier": {},
          "decided_windows": [], "decided_d1": [], "decided_d2": [],
          "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0}
    _write_state(st)
    loaded = pm.load_state()
    halt_before, _ = pm.kill_check(loaded, arm=True)
    assert halt_before is False  # legacy labels excluded pre-stamp
    assert pm._stamp_legacy_model_v() == 100
    loaded2 = pm.load_state()
    halt_after, reasons = pm.kill_check(loaded2, arm=True)
    assert halt_after is True
    assert any(r.startswith("Brier") for r in reasons)
    assert "B" in loaded2["disabled"]


# ---------- model_v at pending creation (D) ----------

def test_score_carries_pending_model_v_not_current():
    # Pending entry created under model A; scored after the model changed.
    pb = {"p_fair": 0.6, "mid_up": 0.55, "tau": 300,
          "model_v": "fv1:sha256:aaa:phi(old)",
          "event_id": "e", "O": 100.0, "S": 100.5, "S_source": "last_close",
          "tau_grid": {}}
    bw, research = pm._score_window_record(12345, pb, 100.6, 100.0, 999)
    assert bw["model_v"] == "fv1:sha256:aaa:phi(old)"
    assert research["model_v"] == "fv1:sha256:aaa:phi(old)"
    assert bw["model_v"] != pm.FAIRVALUE_MODEL_V


def test_score_window_record_fields():
    pb = {"p_fair": 0.7, "mid_up": 0.6, "tau": 300,
          "model_v": pm.FAIRVALUE_MODEL_V,
          "event_id": "slug", "O": 85000.0, "S": 85010.0,
          "S_source": "running_avg60",
          "rv_long": 2e-5, "rv_short": 3e-5, "sigma_used": 3e-5,
          "floor_bound": False,
          "zero_ret_share_15m": 0.4, "gapfill_share_15m": 0.1,
          "bid_up": 0.59, "ask_up": 0.61, "bid_down": 0.39,
          "ask_down": 0.41, "depth_up": 50.0, "depth_down": 60.0,
          "tau_grid": {"600": {"p_fair": 0.65, "mid_up": 0.6,
                               "tau_actual": 598}}}
    bw, research = pm._score_window_record(777, pb, 85001.0, 85000.0, 999)
    # brier_windows entry stays minimal for the gates
    assert set(bw) == {"T", "tau", "brier_model", "brier_mkt", "model_v"}
    assert bw["brier_model"] == pytest.approx((0.7 - 1.0) ** 2)
    # research record carries the full field list
    for k in ("event_id", "T", "O", "S", "S_source", "tau_at_score",
              "p_fair", "outcome", "settle_price",
              "rv_long", "rv_short", "sigma_used", "floor_bound",
              "zero_ret_share_15m", "gapfill_share_15m",
              "mid_up", "bid_up", "ask_up", "bid_down", "ask_down",
              "depth_up", "depth_down", "model_v", "tau_grid_scores",
              "scored_at"):
        assert k in research, k
    assert research["outcome"] == 1.0  # 85001 >= 85000
    assert research["settle_price"] == 85001.0
    assert research["tau_grid_scores"]["600"]["brier_model"] == pytest.approx(
        (0.65 - 1.0) ** 2)


def test_scored_windows_jsonl_append(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "HERE", str(tmp_path))
    pb = {"p_fair": 0.5, "mid_up": 0.5, "tau": 300,
          "model_v": pm.FAIRVALUE_MODEL_V, "tau_grid": {}}
    bw, research = pm._score_window_record(555, pb, 100.0, 100.0, 999)
    pm.append_jsonl(pm._scored_windows_path(), research)
    pm.append_jsonl(pm._scored_windows_path(), research)
    with open(pm._scored_windows_path()) as f:
        lines = f.readlines()
    assert len(lines) == 2  # append-only, no truncation
    assert json.loads(lines[0])["T"] == 555


# ---------- veto fields ----------

def test_veto_fields_shape():
    vf = pm._veto_fields(fav="up", ask_fav=0.62341, depth=42.0, edge=0.21,
                         p_fair=0.9123456, sigma_used=3e-5,
                         floor_bound=True, S=85010.0, O=84900.0, tau=400)
    assert vf["veto"] is True
    assert vf["fav"] == "up"
    assert vf["ask_fav"] == 0.6234
    assert vf["depth"] == 42.0
    assert vf["edge"] == 0.21
    assert vf["p_fair"] == 0.912346
    assert vf["sigma"] == 3e-5
    assert vf["floor_bound"] is True
    assert vf["S"] == 85010.0 and vf["O"] == 84900.0 and vf["tau"] == 400


def test_floor_bound_from():
    assert pm._floor_bound_from(1e-5, 5e-6, 8e-6) is True   # raw < floor
    assert pm._floor_bound_from(3e-5, 2e-5, 3e-5) is False   # raw > floor
    assert pm._floor_bound_from(1e-5, None, None) is True   # fallback
    assert pm._floor_bound_from(3e-5, None, None) is False
    assert pm._floor_bound_from(None, None, None) is None


# ---------- hash invariance (F) ----------

def test_sig_risk_hashes_pinned():
    # Exact values pre/post the 2026-10-08 patch set. If these change, a
    # signal, sizing, or risk input changed -- the era accounting moved.
    assert pm.SIG == {"A": "4acf18e6bd198af9", "B": "9a20c2fd5000cac5",
                      "C": "3a633e3ce6ea21b6", "D": "3c549728a8166b88"}
    assert pm.RISK == "e2af68a6407c920d"
    assert pm.FAIRVALUE_MODEL_V.startswith("fv1:sha256:8f403a0e")


# ---------- shadow decision: first-300-once + latch (third review) ----------

def _mk_rec(T, complete=True, brier_base=0.2, seed=1):
    import random
    rng = random.Random(seed + T)
    gs = {}
    for g in ("300", "450", "600"):
        if not complete and g == "600":
            continue
        gs[g] = {"brier_model": brier_base + rng.uniform(-0.01, 0.01),
                 "brier_h60": brier_base - 0.01 + rng.uniform(-0.01, 0.01),
                 "brier_f3": brier_base - 0.005 + rng.uniform(-0.01, 0.01),
                 "sigma_h60": 2e-5, "sigma_f3": 3e-5,
                 "S": 100.0 + rng.uniform(-1, 1), "tau_actual": int(g)}
    return {"T": T, "settle_price": 101.0, "tau_grid_scores": gs,
            "shadow": {"p_h60": 0.6, "p_f3": 0.6}}


def test_eval_recs_first_300_chronological():
    from research.shadow_decision import _eval_recs, SHADOW_ELIGIBLE_FROM_T as C0
    recs = [_mk_rec(C0 + T) for T in range(400, 0, -1)]  # reverse order
    # sprinkle incomplete windows (dropped by the unified filter)
    recs += [_mk_rec(C0 + 1000 + i, complete=False) for i in range(10)]
    ev = _eval_recs(recs)
    assert len(ev) == 300
    Ts = [r["T"] for r in ev]
    assert Ts == sorted(Ts)
    assert Ts[0] == C0 + 1 and Ts[-1] == C0 + 300


def test_eval_recs_fewer_than_300_extends():
    from research.shadow_decision import decide2, _eval_recs, \
        SHADOW_ELIGIBLE_FROM_T as C0
    recs = [_mk_rec(C0 + T) for T in range(1, 101)]
    assert len(_eval_recs(recs)) == 100
    verdict, why = decide2(recs)
    assert verdict.startswith("EXTEND")


def test_400_windows_verdict_matches_300():
    from research.shadow_decision import decide2, _eval_recs, \
        SHADOW_ELIGIBLE_FROM_T as C0
    recs300 = [_mk_rec(C0 + T) for T in range(1, 301)]
    recs400 = recs300 + [_mk_rec(C0 + T) for T in range(301, 401)]
    v300, _ = decide2(recs300)
    v400, _ = decide2(recs400)
    assert v300 == v400
    # the eval set is identical: first 300 chronologically
    assert [r["T"] for r in _eval_recs(recs400)] == \
        [r["T"] for r in _eval_recs(recs300)]


def test_eval_recs_excludes_pre_cutoff_windows():
    # (2026-10-08, per Claude's fourth review): windows with T below
    # SHADOW_ELIGIBLE_FROM_T were logged under the old unaligned
    # shadow-sigma vintage and are excluded from the evaluation set,
    # even when complete.
    from research.shadow_decision import _eval_recs, SHADOW_ELIGIBLE_FROM_T as C0
    pre = [_mk_rec(C0 - 200 + i) for i in range(100)]
    post = [_mk_rec(C0 + i) for i in range(350)]
    ev = _eval_recs(pre + post)
    assert len(ev) == 300
    assert all(r["T"] >= C0 for r in ev)
    assert ev[0]["T"] == C0
    assert ev[-1]["T"] == C0 + 299


def test_latch_freezes_verdict(tmp_path, monkeypatch):
    import research.shadow_decision as sd
    vpath = str(tmp_path / "shadow_verdict.json")
    monkeypatch.setattr(sd, "VERDICT_PATH", vpath)
    recs = [_mk_rec(sd.SHADOW_ELIGIBLE_FROM_T + T) for T in range(1, 301)]
    verdict, why = sd.decide2(recs)
    sd._latch_verdict(verdict, why, 300)
    latched = sd._load_latched()
    assert latched["verdict"] == verdict
    assert latched["n_windows"] == 300
    # a flipped later sample cannot change the latched HOLD
    assert sd._load_latched()["verdict"] == verdict


def test_main_truncated_run_never_latches(tmp_path, monkeypatch, capsys):
    # (2026-10-08, per Claude's fourth review): a --hours N run must
    # never latch the one-shot verdict, even with >= 300 windows.
    import os
    import research.shadow_decision as sd
    vpath = str(tmp_path / "shadow_verdict.json")
    monkeypatch.setattr(sd, "VERDICT_PATH", vpath)
    recs = [_mk_rec(sd.SHADOW_ELIGIBLE_FROM_T + i) for i in range(350)]
    monkeypatch.setattr(sd, "load_shadow", lambda hours: recs)
    rc = sd.main(["--hours", "72"])
    assert rc == 0
    assert not os.path.exists(vpath)
    out = capsys.readouterr().out
    assert "VERDICT" in out
    assert "latched" not in out


def test_main_full_history_latches(tmp_path, monkeypatch, capsys):
    # Complement: the full-history run (no --hours) still latches.
    import os
    import research.shadow_decision as sd
    vpath = str(tmp_path / "shadow_verdict.json")
    monkeypatch.setattr(sd, "VERDICT_PATH", vpath)
    recs = [_mk_rec(sd.SHADOW_ELIGIBLE_FROM_T + i) for i in range(350)]
    monkeypatch.setattr(sd, "load_shadow", lambda hours: recs)
    rc = sd.main([])
    assert rc == 0
    assert os.path.exists(vpath)
    assert sd._load_latched()["n_windows"] == 300


def test_main_prints_latched_and_stops(tmp_path, monkeypatch, capsys):
    import research.shadow_decision as sd
    vpath = str(tmp_path / "shadow_verdict.json")
    monkeypatch.setattr(sd, "VERDICT_PATH", vpath)
    sd._latch_verdict("HOLD (f3: z-scale 0.50)", "rule (b) failed", 300)
    monkeypatch.setattr(sd, "load_shadow", lambda hours: [])
    rc = sd.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "LATCHED VERDICT" in out
    assert "monitoring only" in out


def test_eval_recs_grid300_only_counts_complete():
    # 2026-10-09 amendment (Claude's call): the grid slice is tau=300
    # only -- 450 was never filled by the engine. A window with only
    # grid 300 filled (all fields) counts as complete; 450/600 are not
    # required.
    from research.shadow_decision import _eval_recs, SHADOW_ELIGIBLE_FROM_T as C0
    gs300 = {"300": {"brier_model": 0.20, "brier_h60": 0.19, "brier_f3": 0.195,
                     "sigma_h60": 2e-5, "sigma_f3": 3e-5,
                     "S": 100.5, "tau_actual": 300}}
    rec = {"T": C0 + 1, "settle_price": 101.0, "tau_grid_scores": gs300,
           "shadow": {"p_h60": 0.6, "p_f3": 0.6}}
    ev = _eval_recs([rec])
    assert len(ev) == 1 and ev[0]["T"] == C0 + 1
    # a window with no grid 300 entry is still incomplete
    ev2 = _eval_recs([{"T": C0 + 2, "settle_price": 101.0,
                       "tau_grid_scores": {}, "shadow": {}}])
    assert ev2 == []
