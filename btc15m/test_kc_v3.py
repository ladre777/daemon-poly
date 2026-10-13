"""02 v3 tests: sidecar flag + validated snapshot (Claude 2026-10-07 review).

Scenarios A-E adapted from v2 for the sidecar design; F-K are the new
regression scenarios from the review.
"""
import json
import os
import sys

import pytest

import paper as pm


# ---------- helpers ----------

def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _valid_state(**over):
    s = {"open": [], "disabled": [], "day": None, "day_pnl": 0.0,
         "day_pnls": {},
         "consec_losses": 0, "brier_windows": [], "pending_brier": {},
         "decided_windows": [], "decided_d1": [], "decided_d2": [],
         "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0}
    s.update(over)
    return s


def _no_network(monkeypatch):
    # run_once fetches event/book after the kill_check block; never let a
    # unit test touch the network.
    monkeypatch.setattr(pm, "get_event", lambda T: None)
    monkeypatch.setattr(pm, "get_book", lambda T: None)


def _crash_kill_check(monkeypatch, exc=RuntimeError("boom")):
    def _raise(state, arm=True):
        raise exc
    monkeypatch.setattr(pm, "kill_check", _raise)


def _clean_kill_check(monkeypatch):
    monkeypatch.setattr(pm, "kill_check", lambda state, arm=True: (False, []))


# ---------- A: crash with snapshot ----------

def test_a_crash_with_snapshot(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    _crash_kill_check(monkeypatch)
    corrupt = {"open": [], "day_pnl": 0.0}  # missing decided_windows, consec_losses
    _write(pm.STATE, corrupt)
    before = _read_bytes(pm.STATE)
    _write(pm.STATE + ".last_good",
           _valid_state(day_pnl=1.5, consec_losses=5))
    pm.run_once()
    out = capsys.readouterr().out
    assert out.count("KILL_CHECK_ERROR:") == 1
    assert "(sticky)" not in out
    assert "KILL TRANSITION" not in out
    assert "restored" in out
    assert "ledger keys missing/invalid" in out  # blocker 1: ledger validated first
    # sidecar flag exists ...
    with open(pm.STATE + ".kc_error.json") as f:
        flag = json.load(f)
    assert flag["type"] == "ValueError"
    assert "consec_losses" in flag["restored"]
    # ... but STATE was never written.
    assert _read_bytes(pm.STATE) == before


def test_a_kill_check_raise_still_flagged(tmp_path, monkeypatch, capsys):
    # ledger valid, but kill_check itself raises -> RuntimeError flag
    _no_network(monkeypatch)
    _crash_kill_check(monkeypatch)
    _write(pm.STATE, _valid_state())
    before = _read_bytes(pm.STATE)
    pm.run_once()
    out = capsys.readouterr().out
    assert out.count("KILL_CHECK_ERROR:") == 1
    with open(pm.STATE + ".kc_error.json") as f:
        flag = json.load(f)
    assert flag["type"] == "RuntimeError"
    assert _read_bytes(pm.STATE) == before


def test_a_fill_restores_in_memory(monkeypatch):
    # unit-level: _kc_fill restores consec_losses=5 from a valid snapshot
    _write(pm.STATE + ".last_good",
           _valid_state(day_pnl=1.5, consec_losses=5))
    state = {"open": [], "day_pnl": 0.0}  # missing ledger keys
    restored, scaffolded, age = pm._kc_fill(state)
    assert state["consec_losses"] == 5
    assert "consec_losses" in restored
    assert isinstance(age, int)


# ---------- B: sticky second tick ----------

def test_b_sticky_second_tick(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    _crash_kill_check(monkeypatch)
    _write(pm.STATE, {"open": [], "day_pnl": 0.0})
    _write(pm.STATE + ".last_good",
           _valid_state(day_pnl=1.5, consec_losses=5))
    before = _read_bytes(pm.STATE)
    pm.run_once()  # tick 1: crash
    capsys.readouterr()
    pm.run_once()  # tick 2: fresh load, flag read from sidecar
    out = capsys.readouterr().out
    assert "KILL_CHECK_ERROR (sticky):" in out
    assert out.count("KILL_CHECK_ERROR") == 1
    assert _read_bytes(pm.STATE) == before  # still never written


# ---------- C: clear flag ----------

def test_c_clear_flag(monkeypatch, capsys):
    _write(pm.STATE, _valid_state())
    _write(pm.STATE + ".kc_error.json",
           {"type": "RuntimeError", "message": "boom", "ts": 1,
            "restored": [], "scaffolded": []})
    monkeypatch.setattr(sys, "argv", ["paper.py", "--clear-kc-error"])
    pm.main()
    out = capsys.readouterr().out
    assert "cleared kill_check_error" in out
    assert not os.path.exists(pm.STATE + ".kc_error.json")


def test_c_clear_flag_absent(monkeypatch, capsys):
    _write(pm.STATE, _valid_state())
    monkeypatch.setattr(sys, "argv", ["paper.py", "--clear-kc-error"])
    pm.main()
    assert "no kill_check_error flag set" in capsys.readouterr().out


# ---------- D: crash with no snapshot ----------

def test_d_crash_no_snapshot(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    _crash_kill_check(monkeypatch)
    _write(pm.STATE, {"open": [], "day_pnl": 0.0})
    before = _read_bytes(pm.STATE)
    assert not os.path.exists(pm.STATE + ".last_good")
    pm.run_once()
    out = capsys.readouterr().out
    assert out.count("KILL_CHECK_ERROR:") == 1
    assert "no usable last-good snapshot" in out
    assert os.path.exists(pm.STATE + ".kc_error.json")
    assert _read_bytes(pm.STATE) == before


# ---------- E: clean tick writes snapshot ----------

def test_e_clean_tick_writes_snapshot(monkeypatch):
    _no_network(monkeypatch)
    _clean_kill_check(monkeypatch)
    _write(pm.STATE, _valid_state(day_pnl=2.5, consec_losses=3))
    pm.run_once()
    with open(pm.STATE + ".last_good") as f:
        snap = json.load(f)
    assert snap["day_pnl"] == 2.5
    assert snap["consec_losses"] == 3


# ---------- F: blocker-1 regression (flag survives, no snapshot) ----------

def test_f_flag_survives_without_snapshot(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    _crash_kill_check(monkeypatch)
    _write(pm.STATE, {"open": [], "day_pnl": 0.0})
    before = _read_bytes(pm.STATE)
    pm.run_once()  # tick 1: crash, no snapshot
    capsys.readouterr()
    assert os.path.exists(pm.STATE + ".kc_error.json")
    # tick 2 from a FRESH load_state: the sidecar must make it sticky.
    pm.run_once()
    out = capsys.readouterr().out
    assert "KILL_CHECK_ERROR (sticky):" in out
    assert _read_bytes(pm.STATE) == before


# ---------- G: live files untouched ----------

def test_g_live_files_untouched(monkeypatch):
    import conftest as cf
    _no_network(monkeypatch)
    _clean_kill_check(monkeypatch)
    _write(pm.STATE, _valid_state())
    before = [cf._content_sig(p) for p in cf._LIVE]
    pm.run_once()
    assert [cf._content_sig(p) for p in cf._LIVE] == before


def test_g_content_sig_catches_same_hash_clobber(tmp_path):
    """A ledger overwrite that re-stamps identical hashes must still be
    caught (the blind spot in the stable-field hash, found by Code mode
    2026-10-08)."""
    import conftest as cf
    p = tmp_path / "paper_state.json"
    base = {"config_hash": "a", "sig_A": "b", "risk_hash": "c",
            "open": [], "cum_pnl": 0.0}
    p.write_text(json.dumps(base))
    before = cf._content_sig(str(p))
    clobber = dict(base, open=[{"id": 1}], cum_pnl=40.0,
                   consec_losses=2)
    p.write_text(json.dumps(clobber))
    assert cf._content_sig(str(p)) != before


def test_g_tick_lock_held_during_session():
    """The session fixture must hold the tick lock: a second exclusive
    non-blocking acquire has to fail while the suite runs."""
    import conftest as cf
    import fcntl
    fd = os.open(cf._LOCK_PATH, os.O_RDWR)
    try:
        with pytest.raises(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_g_blotter_isolated(tmp_path, monkeypatch):
    """BLOTTER must follow the tmp isolation like STATE does -- a test
    recording a trade must not append to the real blotter.jsonl
    (found by Code mode 2026-10-08)."""
    import conftest as cf
    real_blotter = os.path.join(cf._LIVE_DIR, "blotter.jsonl")
    assert pm.BLOTTER == str(tmp_path / "blotter.jsonl")
    before = cf._content_sig(real_blotter)
    pm.append_jsonl(pm.BLOTTER, {"test": "row"})
    assert os.path.exists(pm.BLOTTER)
    assert cf._content_sig(real_blotter) == before


def test_g_verdict_path_isolated(tmp_path):
    """sd.VERDICT_PATH must point at tmp, never the real research dir --
    a test latching a verdict must not touch the experiment's
    shadow_verdict.json (2026-10-08, per Claude's review)."""
    import conftest as cf
    import research.shadow_decision as sd
    real = os.path.join(cf._LIVE_DIR, "research", "shadow_verdict.json")
    assert sd.VERDICT_PATH == str(tmp_path / "shadow_verdict.json")
    before = cf._content_sig(real)
    sd._latch_verdict("HOLD", "test", 300)
    assert sd._load_latched()["verdict"] == "HOLD"
    assert cf._content_sig(real) == before


# ---------- H: clear refuses without --restore-from-snapshot ----------

def test_h_clear_refuses_without_restore(monkeypatch, capsys):
    _write(pm.STATE, {"open": [], "day_pnl": 0.0})  # broken ledger
    _write(pm.STATE + ".kc_error.json",
           {"type": "RuntimeError", "message": "boom", "ts": 1,
            "restored": [], "scaffolded": []})
    _write(pm.STATE + ".last_good",
           _valid_state(day_pnl=1.5, consec_losses=5))
    monkeypatch.setattr(sys, "argv", ["paper.py", "--clear-kc-error"])
    with pytest.raises(SystemExit) as ei:
        pm.main()
    assert ei.value.code == 1
    assert "REFUSED" in capsys.readouterr().out
    # flag kept: the human has not fixed anything yet
    assert os.path.exists(pm.STATE + ".kc_error.json")


def test_h_clear_with_restore(monkeypatch, capsys):
    _write(pm.STATE, {"open": [], "day_pnl": 0.0})  # broken ledger
    _write(pm.STATE + ".kc_error.json",
           {"type": "RuntimeError", "message": "boom", "ts": 1,
            "restored": [], "scaffolded": []})
    _write(pm.STATE + ".last_good",
           _valid_state(day_pnl=1.5, consec_losses=5))
    monkeypatch.setattr(sys, "argv",
                        ["paper.py", "--clear-kc-error",
                         "--restore-from-snapshot"])
    pm.main()
    out = capsys.readouterr().out
    assert "restoring consec_losses" in out
    assert "restoring decided_windows" in out
    assert "cleared kill_check_error" in out
    assert not os.path.exists(pm.STATE + ".kc_error.json")
    with open(pm.STATE) as f:
        st = json.load(f)
    assert st["consec_losses"] == 5
    assert st["decided_windows"] == []
    # day_pnl was present and valid in the broken file: left untouched
    assert st["day_pnl"] == 0.0
    # broken file kept as evidence
    corrupt = [p for p in os.listdir(os.path.dirname(pm.STATE))
               if os.path.basename(p).startswith("paper_state.json.corrupt.")]
    assert len(corrupt) == 1


# ---------- I: deepcopy (no aliasing of module defaults) ----------

def test_i_scaffold_deepcopy():
    s1 = {}
    r1, sc1, _ = pm._kc_fill(s1)
    assert "open" in sc1
    s1["open"].append(1)
    s2 = {}
    _, sc2, _ = pm._kc_fill(s2)
    assert "open" in sc2
    assert s2["open"] == []
    assert pm._KC_STRUCTURAL_DEFAULTS["open"] == []


# ---------- J: wrong-typed values count as bad ----------

def test_j_none_counts_as_bad():
    st = _valid_state(consec_losses=None)
    assert "consec_losses" in pm._kc_bad_keys(st)


def test_j_snapshot_with_bad_ledger_rejected():
    _write(pm.STATE + ".last_good",
           _valid_state(consec_losses="five"))  # wrong type
    assert pm._kc_snapshot_load() is None


def test_j_bool_is_not_int():
    # bool is a subclass of int; True must not pass as consec_losses
    st = _valid_state(consec_losses=True)
    assert "consec_losses" in pm._kc_bad_keys(st)


# ---------- K: odd flag fails closed ----------

def test_k_flag_true_fails_closed(monkeypatch, capsys):
    _no_network(monkeypatch)
    _write(pm.STATE, _valid_state())
    with open(pm.STATE + ".kc_error.json", "w") as f:
        f.write("true")
    pm.run_once()  # must not raise
    out = capsys.readouterr().out
    assert "KILL_CHECK_ERROR (sticky):" in out
    assert "UnreadableFlag" in out


def test_k_flag_invalid_json_fails_closed(monkeypatch, capsys):
    _no_network(monkeypatch)
    _write(pm.STATE, _valid_state())
    with open(pm.STATE + ".kc_error.json", "w") as f:
        f.write("{not json")
    pm.run_once()  # must not raise
    out = capsys.readouterr().out
    assert "KILL_CHECK_ERROR (sticky):" in out


# ---------- save_state guard ----------

def test_save_state_skips_flagged():
    s = _valid_state()
    s["kill_check_error"] = {"type": "X", "message": "y"}
    _write(pm.STATE, _valid_state(day_pnl=9.9))
    before = _read_bytes(pm.STATE)
    pm.save_state(s)  # must not write, must not raise
    assert _read_bytes(pm.STATE) == before


def test_save_state_still_guards_unflagged():
    with pytest.raises(ValueError):
        pm.save_state({"open": []})  # missing ledger keys, no flag


# ---------- settle respects the flag ----------

def test_settle_skipped_when_flagged(monkeypatch, capsys):
    _write(pm.STATE, _valid_state())
    _write(pm.STATE + ".kc_error.json",
           {"type": "RuntimeError", "message": "boom", "ts": 1,
            "restored": [], "scaffolded": []})
    before = _read_bytes(pm.STATE)
    pm.settle()
    assert "settle skipped" in capsys.readouterr().out
    assert _read_bytes(pm.STATE) == before


# ---------- L: corrupt-but-tolerated state is flagged (blocker 1) ----------

def test_l_missing_ledger_flagged_even_if_kill_check_passes(tmp_path,
                                                             monkeypatch,
                                                             capsys):
    _no_network(monkeypatch)
    _clean_kill_check(monkeypatch)  # kill_check would pass; ledger check fires first
    st = _valid_state()
    del st["decided_windows"]
    _write(pm.STATE, st)
    before = _read_bytes(pm.STATE)
    _write(pm.STATE + ".last_good", _valid_state())
    pm.run_once()
    out = capsys.readouterr().out
    assert "KILL_CHECK_ERROR:" in out
    assert "ledger keys missing/invalid" in out
    assert os.path.exists(pm.STATE + ".kc_error.json")
    assert _read_bytes(pm.STATE) == before


# ---------- M: clear checks _KC_CLEAR_KEYS (blocker 2) ----------

def test_m_clear_refuses_missing_disabled(monkeypatch, capsys):
    st = _valid_state()
    del st["disabled"]  # in _KC_CLEAR_KEYS (kill_check reads it directly)
    _write(pm.STATE, st)
    _write(pm.STATE + ".kc_error.json",
           {"type": "RuntimeError", "message": "boom", "ts": 1,
            "restored": [], "scaffolded": []})
    _write(pm.STATE + ".last_good", _valid_state())  # snapshot HAS disabled
    monkeypatch.setattr(sys, "argv", ["paper.py", "--clear-kc-error"])
    with pytest.raises(SystemExit) as ei:
        pm.main()
    assert ei.value.code == 1
    assert "REFUSED" in capsys.readouterr().out
    assert os.path.exists(pm.STATE + ".kc_error.json")


def test_m_clear_restores_disabled_with_flag(monkeypatch, capsys):
    st = _valid_state()
    del st["disabled"]
    _write(pm.STATE, st)
    _write(pm.STATE + ".kc_error.json",
           {"type": "RuntimeError", "message": "boom", "ts": 1,
            "restored": [], "scaffolded": []})
    _write(pm.STATE + ".last_good", _valid_state(disabled=["B"]))
    monkeypatch.setattr(sys, "argv",
                        ["paper.py", "--clear-kc-error",
                         "--restore-from-snapshot"])
    pm.main()
    out = capsys.readouterr().out
    assert "restoring disabled" in out
    assert not os.path.exists(pm.STATE + ".kc_error.json")
    with open(pm.STATE) as f:
        assert json.load(f)["disabled"] == ["B"]
    # audit trail written to file, never blocking the clear
    with open(pm.STATE + ".kc_audit.log") as f:
        audit = f.read()
    assert "cleared by" in audit and "restore=True" in audit


def test_m_clear_refuses_unrestorable(monkeypatch, capsys):
    st = _valid_state()
    del st["disabled"]
    _write(pm.STATE, st)
    _write(pm.STATE + ".kc_error.json",
           {"type": "RuntimeError", "message": "boom", "ts": 1,
            "restored": [], "scaffolded": []})
    snap = _valid_state()
    del snap["disabled"]  # snapshot lacks it too
    _write(pm.STATE + ".last_good", snap)
    monkeypatch.setattr(sys, "argv",
                        ["paper.py", "--clear-kc-error",
                         "--restore-from-snapshot"])
    with pytest.raises(SystemExit) as ei:
        pm.main()
    assert ei.value.code == 1
    assert "not restorable from snapshot" in capsys.readouterr().out
    assert os.path.exists(pm.STATE + ".kc_error.json")


def test_m_legacy_missing_cum_pnl_clears_fine(monkeypatch, capsys):
    # cum_pnl/peak_cum are .get-read only: a legacy state missing them is
    # NOT refused by --clear-kc-error.
    st = _valid_state()
    del st["cum_pnl"]
    del st["peak_cum"]
    _write(pm.STATE, st)
    _write(pm.STATE + ".kc_error.json",
           {"type": "RuntimeError", "message": "boom", "ts": 1,
            "restored": [], "scaffolded": []})
    monkeypatch.setattr(sys, "argv", ["paper.py", "--clear-kc-error"])
    pm.main()
    assert "cleared kill_check_error" in capsys.readouterr().out
    assert not os.path.exists(pm.STATE + ".kc_error.json")


# ---------- N: flag write failure doesn't traceback ----------

def test_n_flag_write_failure_no_traceback(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    _crash_kill_check(monkeypatch)
    _write(pm.STATE, {"open": [], "day_pnl": 0.0})  # corrupt
    real_atomic = pm._atomic_json_write

    def _fail_sidecar(path, obj):
        if path.endswith(".kc_error.json"):
            raise OSError("disk full (simulated)")
        return real_atomic(path, obj)

    monkeypatch.setattr(pm, "_atomic_json_write", _fail_sidecar)
    monkeypatch.setattr(sys, "argv", ["paper.py", "--run-once"])
    pm.main()  # must not raise
    out = capsys.readouterr().out
    assert "WARNING flag file NOT written" in out
    assert "STATE NOT SAVED (evidence-only tick)" in out


# ---------- O: .prev rotation ----------

def test_o_prev_rotation():
    snap = pm.STATE + ".last_good"
    prev = snap + ".prev"
    a = _valid_state(day_pnl=1.0)
    b = _valid_state(day_pnl=2.0)
    c = _valid_state(day_pnl=3.0)
    pm._kc_snapshot_write(a)
    assert not os.path.exists(prev)  # first write: no .prev yet
    pm._kc_snapshot_write(b)  # .prev should now hold A
    assert os.path.exists(prev)
    with open(prev) as f:
        assert json.load(f)["day_pnl"] == 1.0
    pm._kc_snapshot_write(c)  # .prev still A (recent)
    with open(prev) as f:
        assert json.load(f)["day_pnl"] == 1.0
    with open(snap) as f:
        assert json.load(f)["day_pnl"] == 3.0
    # backdate .prev beyond the hour: next write rotates it to C
    os.utime(prev, (0, 0))
    d = _valid_state(day_pnl=4.0)
    pm._kc_snapshot_write(d)
    with open(prev) as f:
        assert json.load(f)["day_pnl"] == 3.0


# ---------- settle ledger guard (recommended 1) ----------

def test_settle_skipped_on_invalid_ledger_no_flag(monkeypatch, capsys):
    st = _valid_state()
    del st["open"]  # invalid ledger, no flag file
    with open(pm.STATE, "w") as f:
        json.dump(st, f)
    before = open(pm.STATE, "rb").read()
    pm.settle()  # must not raise KeyError
    out = capsys.readouterr().out
    assert "SETTLE_SKIPPED (ledger keys missing/invalid)" in out
    assert open(pm.STATE, "rb").read() == before


# ---------- settlement integrity check (review 2026-10-08, Claude) ----------

def _caps_with_next(t, next_pb):
    return {str(t + 900): next_pb}


def _drain(state, caps, now):
    pm._settle_check_drain(state, caps, now)
    return state.get("settle_check_stats", {})


def test_i_enqueue_drain_flags_drift(capsys):
    """Enqueue at settle, drain when next_pb arrives -> drift flagged."""
    state, t, now = {}, 1791491400, 1791492000
    pm._settle_check_enqueue(state, t, 83294.38, 83300.00, now)
    assert str(t) in state["settle_check_queue"]
    # next_pb not yet published: drain is a no-op, entry stays queued
    st = _drain(state, {}, now + 60)
    assert st.get("checked", 0) == 0
    assert str(t) in state["settle_check_queue"]
    # next_pb arrives on a later run: checked + drift logged
    st = _drain(state, _caps_with_next(t, 83308.98), now + 120)
    assert st["checked"] == 1 and st["drift"] == 1
    assert str(t) not in state["settle_check_queue"]
    out = capsys.readouterr().out
    assert "SETTLE_MISMATCH" in out and f"T={t}" in out
    mm = state["settle_mismatches"]
    assert len(mm) == 1 and mm[0]["T"] == t and mm[0]["flip"] is True


def test_i_quiet_on_small_gap(capsys):
    """Gap within tolerance and no flip -> counted, not flagged."""
    state, t, now = {}, 1791491400, 1791492000
    pm._settle_check_enqueue(state, t, 83308.99, 83300.00, now)
    st = _drain(state, _caps_with_next(t, 83308.98), now + 60)
    assert st["checked"] == 1 and st["drift"] == 0 and st["flips"] == 0
    assert "SETTLE_MISMATCH" not in capsys.readouterr().out
    assert "settle_mismatches" not in state


def test_i_flip_flagged_under_tolerance(capsys):
    """A $0.30 gap crossing the price-to-beat is a FLIP even under $0.50."""
    state, t, now = {}, 1791491400, 1791492000
    # s_avg below cap (down), next_pb above cap (up): flip on $0.30 gap
    pm._settle_check_enqueue(state, t, 83299.85, 83300.00, now)
    st = _drain(state, _caps_with_next(t, 83300.15), now + 60)
    assert st["checked"] == 1 and st["flips"] == 1 and st["drift"] == 0
    out = capsys.readouterr().out
    assert "SETTLE_MISMATCH" in out and "FLIP" in out
    assert state["settle_mismatches"][0]["flip"] is True


def test_i_expires_after_ttl():
    """Entry still waiting past SETTLE_CHECK_TTL expires and is counted."""
    state, t, now = {}, 1791491400, 1791492000
    pm._settle_check_enqueue(state, t, 83300.0, 83300.00, now)
    st = _drain(state, {}, now + pm.SETTLE_CHECK_TTL + 1)
    assert st["expired"] == 1 and st["checked"] == 0
    assert state["settle_check_queue"] == {}


def test_i_exactly_once_across_paths():
    """A window enqueued by both Brier drain and trade settle is checked once."""
    state, t, now = {}, 1791491400, 1791492000
    pm._settle_check_enqueue(state, t, 83300.0, 83300.00, now)
    pm._settle_check_enqueue(state, t, 83300.0, 83300.00, now + 5)
    assert len(state["settle_check_queue"]) == 1
    st = _drain(state, _caps_with_next(t, 83300.00), now + 60)
    assert st["checked"] == 1


def test_i_stats_track_distribution():
    """checked/gap_gt_1c/max_gap reflect the observed gap distribution."""
    state, now = {}, 1791492000
    pm._settle_check_enqueue(state, 1000, 83300.00, 83300.00, now)
    pm._settle_check_enqueue(state, 2000, 83300.05, 83300.00, now)
    caps = {str(1900): 83300.005, str(2900): 83300.10}
    st = _drain(state, caps, now + 60)
    assert st["checked"] == 2
    assert st["gap_gt_1c"] == 1  # only the $0.05 gap
    assert st["max_gap"] == round(0.05, 2)
    assert st["drift"] == 0 and st["flips"] == 0


def test_i_never_raises_on_garbage(capsys):
    """Garbage inputs -> silent no-ops, no exception escapes."""
    pm._settle_check_enqueue(None, None, None, None, None)
    pm._settle_check_enqueue({}, "x", "junk", "junk", 1)
    pm._settle_check_drain(None, None, None)
    pm._settle_check_drain({}, {"a": []}, 1)
    assert "SETTLE_MISMATCH" not in capsys.readouterr().out


def test_i_mismatch_log_bounded():
    """The state record keeps only the last 20 mismatches."""
    state, t0, now = {}, 1791491400, 1791492000
    for i in range(25):
        t = t0 + i * 900  # distinct windows so keys don't collide
        pm._settle_check_enqueue(state, t, 200.0, 100.00, now)
        _drain(state, _caps_with_next(t, 100.0), now + 60)
    mm = state["settle_mismatches"]
    assert len(mm) == 20
    assert mm[0]["T"] == t0 + 5 * 900 and mm[-1]["T"] == t0 + 24 * 900


def test_i_bad_entry_dropped_not_blocking(capsys):
    """A malformed next_pb is dropped+counted; entries behind it still run."""
    state, now = {}, 1791492000
    pm._settle_check_enqueue(state, 1000, 83300.0, 83300.00, now)
    pm._settle_check_enqueue(state, 2000, 83300.0, 83300.00, now)
    caps = {str(1900): "not-a-number", str(2900): 83300.00}
    st = _drain(state, caps, now + 60)
    assert st["errors"] == 1
    assert st["checked"] == 1  # the valid entry behind it still ran
    assert state["settle_check_queue"] == {}
    assert "SETTLE_MISMATCH" not in capsys.readouterr().out


def test_i_boundary_gap_exact_tol(capsys):
    """Gap of exactly $0.50 is not drift (strict >), but still counted."""
    state, t, now = {}, 1791491400, 1791492000
    pm._settle_check_enqueue(state, t, 83300.00, 83300.00, now)
    st = _drain(state, _caps_with_next(t, 83300.50), now + 60)
    assert st["checked"] == 1 and st["drift"] == 0
    assert st["gap_gt_1c"] == 1 and st["max_gap"] == 0.50
    assert "SETTLE_MISMATCH" not in capsys.readouterr().out


# ---------- N: day_pnls in the kill-check ledger (Claude review fix C) ----------

def test_n_day_pnls_corrupted_scaffolded(monkeypatch):
    """day_pnls present but wrong-typed counts as bad and is scaffolded."""
    state = _valid_state(day_pnls="oops")  # corrupt: string, not dict
    bad = pm._kc_bad_keys(state)
    assert "day_pnls" in bad
    restored, scaffolded, _ = pm._kc_fill(state)
    assert "day_pnls" in scaffolded
    assert state["day_pnls"] == {}


def test_n_day_pnls_restored_with_ledger(monkeypatch):
    """A restore from last_good brings day_pnls with the ledger -- a
    consistent pair, never one without the other."""
    _write(pm.STATE + ".last_good",
           _valid_state(day_pnls={"2026-10-09": 1.5}, consec_losses=5))
    state = {"open": [], "day_pnl": 0.0}  # missing ledger keys incl day_pnls
    restored, _, _ = pm._kc_fill(state)
    assert "day_pnls" in restored
    assert state["day_pnls"] == {"2026-10-09": 1.5}
    assert state["consec_losses"] == 5


def test_n_day_pnls_bool_rejected(monkeypatch):
    """A bool day_pnls is not a dict -- rejected like other type errors."""
    assert "day_pnls" in pm._kc_bad_keys(_valid_state(day_pnls=True))


def test_n_kill_reads_derived_day_pnl(monkeypatch):
    """The day kill reads _day_pnl() (the bucket), not the legacy key: a
    stale state['day_pnl'] cannot drift the kill input."""
    s = _valid_state(day="2026-10-09",
                     day_pnls={"2026-10-09": -99.0}, day_pnl=999.0)
    monkeypatch.setattr(pm, "today_key", lambda ts=None: "2026-10-09")
    halt, reasons = pm.kill_check(s, arm=True)
    assert halt and any(r.startswith("day_pnl") for r in reasons)
    assert "-99.00" in reasons[0]  # the bucket value, not 999.0


def test_n_pre_part1_snapshot_backfilled_valid(monkeypatch):
    """Fix 5: a pre-Part-1 snapshot (no day_pnls) loads as VALID with the
    backfill applied in memory -- deploy doesn't lose the last-good."""
    snap = _valid_state(day_pnl=2.5, consec_losses=3)
    del snap["day_pnls"]  # pre-Part-1 shape
    snap["day"] = "2026-10-09"
    _write(pm.STATE + ".last_good", snap)
    good = pm._kc_snapshot_load()
    assert good is not None
    assert good["day_pnls"] == {"2026-10-09": 2.5}
    assert good["consec_losses"] == 3
