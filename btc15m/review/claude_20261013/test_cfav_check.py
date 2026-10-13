"""Synthetic tests for cfav_check.py (no real post-registration data exists)."""
import cfav_check as c

R = 1_800_000_000
T = R + 900 * 4   # an eligible window


def book(ts, up=None, dn=None, state="MARKET_STATE_OPEN", theta=0.0695, Tw=T):
    return {"type": "book", "ts": ts, "T": Tw, "state": state,
            "empty_book": False, "fee": {"taker_theta": theta},
            "offers_up": up or [[0.10, 500]], "offers_down": dn or [[0.91, 500]]}


def dense(Tw, **kw):
    # records every 3 s across the band tau 300..60
    return [book(Tw + 600 + i, Tw=Tw, **kw) for i in range(0, 241, 3)]


def test_fee_banker_rounding():
    assert c.fee(0.0695, 0.91, 10) == 0.06   # 0.0569... -> 0.06
    assert c.fee(0.5, 0.5, 1) == 0.12        # 0.125 -> 0.12 (half-even)
    assert c.fee(0.5, 0.5, 3) == 0.38        # 0.375 -> 0.38


def test_entry_win_and_values():
    recs = dense(T)
    pb = {T: 100.0, T + 900: 99.0}           # Down wins
    st, tr = c.window_trade(T, recs, pb)
    assert st == "entered" and tr["side"] == "down" and tr["win"]
    assert tr["a2"] == 0.91 and tr["tau_trigger"] == 300 and tr["tau_entry"] == 297
    assert tr["net"] == round(10 - 9.1 - c.fee(0.0695, 0.91, 10), 2)


def test_tie_settles_up_so_down_loses():
    st, tr = c.window_trade(T, dense(T), {T: 100.0, T + 900: 100.0})
    assert st == "entered" and not tr["win"]


def test_fade_when_next_ask_leaves_band():
    recs = dense(T)
    recs[1] = book(recs[1]["ts"], dn=[[0.98, 500]])
    st, tr = c.window_trade(T, recs, {T: 1.0, T + 900: 0.5})
    assert st == "faded" and tr is None       # no second attempt


def test_depth_and_band():
    st, _ = c.window_trade(T, dense(T, dn=[[0.91, 5]]), {T: 1.0, T + 900: 0.5})
    assert st == "no_trigger"
    st, _ = c.window_trade(T, dense(T, dn=[[0.80, 500]]), {T: 1.0, T + 900: 0.5})
    assert st == "no_trigger"


def test_coverage_gap_and_edges():
    recs = [r for r in dense(T) if not (T + 700 <= r["ts"] <= T + 740)]
    assert c.window_trade(T, recs, {})[0] == "coverage_fail"
    late = [r for r in dense(T) if r["ts"] >= T + 640]   # band start gap 40 s
    assert c.window_trade(T, late, {})[0] == "coverage_fail"


def test_no_label():
    assert c.window_trade(T, dense(T), {T: 1.0})[0] == "no_label"


def test_wilson_and_futility():
    lo, hi = c.wilson(0, 151)
    assert lo == 0.0 and 0.024 < hi < 0.026
    losing = [{"T": i, "win": i % 4 != 0, "a2": 0.92, "fee": 0.05,
               "net": 0.0, "net_per_share": 0.0} for i in range(60)]
    f = c.futility(losing)
    assert f and f["at_trade"] == 50


def test_run_excludes_pre_registration_windows(tmp_path):
    early, late = R, R + 900
    p = tmp_path / "b.jsonl"
    with open(p, "w") as f:
        import json
        for r in dense(early) + dense(late):
            f.write(json.dumps(r) + "\n")
    counts, trades = c.run(R, [str(p)], None)
    assert sum(counts.values()) == 1          # only T >= R + 900
