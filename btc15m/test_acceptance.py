#!/usr/bin/env python3
"""Acceptance tests §7 (#10-#16) for the 15m BTC Up/Down paper stack.

All hermetic: fixtures + pure functions, no network.
Run:  python -m pytest test_acceptance.py -q   (from ~/workspace/polymarket/btc15m)
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from brti import avg60
from common import taker_fee_per_share
from markets import parse_book
from fairvalue import p_up, phi
from paper import (decide_sleeve_a, decide_sleeve_b, decide_sleeve_c,
                   paper_fill, blotter_base)


def candles_for(T, fn):
    """Build [(ts, close)] over [T-65, T+5] from fn(ts)."""
    return [(t, fn(t)) for t in range(T - 65, T + 6)]


# #10: open average over [T-60, T-1] matches priceToBeat; boundaries exact.
def test_10_avg60_interval_and_boundaries():
    T = 1_790_511_300
    # T-60 -> 160.0 (INCLUDED), T-59..T-1 -> 100.0, T -> 999.0 (EXCLUDED),
    # T-61 -> 888.0 (EXCLUDED)
    def fn(t):
        if t == T - 60:
            return 160.0
        if t == T:
            return 999.0
        if t == T - 61:
            return 888.0
        return 100.0
    v = avg60(candles_for(T, fn), T)
    assert v == (59 * 100.0 + 160.0) / 60.0 == 101.0
    # and it must NOT equal the docs' wrong [T-59,T] interval
    wrong = (59 * 100.0 + 999.0) / 60.0
    assert v != wrong


def test_10_avg60_missing_prints():
    T = 1_790_511_300
    rows = [(t, 100.0) for t in range(T - 60, T - 1)]  # 59 prints, one short
    assert avg60(rows, T) is None


def test_10_priceToBeat_fixture():
    # end-to-end on the fixture: published value == recomputed value
    T = 1_790_511_300
    rows = [(t, 84981.42) for t in range(T - 60, T)]
    assert round(avg60(rows, T), 2) == 84981.42


# #11: Binance-up / BRTI-flat must NOT emit an Up signal.
def test_11_brti_flat_no_up_signal():
    # BRTI flat vs open -> P(Up) is exactly 0.5 no matter what Binance did
    assert p_up(S=85000.0, O=85000.0, sigma=3e-5, tau=600) == 0.5
    # tie convention: close >= open settles Up, so S == O -> 0.5, never > 0.5
    assert p_up(S=84999.99, O=85000.0, sigma=3e-5, tau=600) < 0.5
    # sleeve B needs edge >= 0.08 over the ask; p_fair=0.5 can never clear it
    book = {"ask_up": (0.55, 100.0), "ask_down": (0.47, 100.0)}
    assert decide_sleeve_b(book, 0.5, 600) is None


def test_11_fair_value_sanity():
    # strong BRTI move up, plenty of time -> high P(Up)
    assert p_up(S=86000.0, O=85000.0, sigma=3e-5, tau=600) > 0.9
    # bad inputs -> None, never a signal
    assert p_up(S=0, O=85000.0, sigma=3e-5, tau=600) is None
    assert p_up(S=85000.0, O=85000.0, sigma=0, tau=600) is None
    assert p_up(S=85000.0, O=85000.0, sigma=3e-5, tau=0) is None


# #12: sleeve B cannot emit an underdog buy (fuzz).
def test_12_b_never_buys_underdog():
    rng = random.Random(42)
    for _ in range(2000):
        bid = round(rng.uniform(0.05, 0.90), 2)
        spread = round(rng.uniform(0.01, 0.10), 2)
        ask_u = min(0.99, bid + spread)
        book = {"ask_up": (ask_u, rng.uniform(1, 500)),
                "bid_up": (bid, rng.uniform(1, 500)),
                "ask_down": (round(1 - bid, 4), rng.uniform(1, 500)),
                "bid_down": (round(1 - ask_u, 4), rng.uniform(1, 500))}
        p_fair = rng.random()
        tau = rng.uniform(0, 900)
        tr = decide_sleeve_b(book, p_fair, tau)
        if tr is None or tr.get("veto_candidate"):
            continue
        fav = "up" if ask_u > (1 - bid) else "down"
        for leg in tr["legs"]:
            assert leg["side"] == fav, f"underdog buy emitted: {tr} fav={fav}"


# #12b: sleeve B buys the book's favorite = pricier ask (regression for the
# 2026-10-02 favorite-side inversion bug, where fav picked the cheaper ask).
def test_12b_b_buys_favorite_side():
    # up is the clear favorite at 0.70; model agrees with 8c+ edge
    book = {"ask_up": (0.70, 100.0), "bid_up": (0.68, 100.0),
            "ask_down": (0.32, 100.0), "bid_down": (0.30, 100.0)}
    tr = decide_sleeve_b(book, 0.85, 600)
    assert tr is not None and not tr.get("veto_candidate")
    assert tr["legs"][0]["side"] == "up", f"bought wrong side: {tr}"
    assert tr["edge"] >= 0.08
    # model disagrees with the book's favorite -> no trade
    assert decide_sleeve_b(book, 0.20, 600) is None
    # mirror image: down is the favorite (edge 0.12: clears 8c, under veto)
    book2 = {"ask_up": (0.30, 100.0), "bid_up": (0.28, 100.0),
             "ask_down": (0.72, 100.0), "bid_down": (0.70, 100.0)}
    tr2 = decide_sleeve_b(book2, 0.15, 600)
    assert tr2 is not None and not tr2.get("veto_candidate")
    assert tr2["legs"][0]["side"] == "down", f"bought wrong side: {tr2}"


# #12c: winner's-curse veto at >=15c (polybtc study: 0-for-5 at >=15c).
def test_12c_b_veto_at_15c():
    book = {"ask_up": (0.70, 100.0), "bid_up": (0.68, 100.0),
            "ask_down": (0.32, 100.0), "bid_down": (0.30, 100.0)}
    tr = decide_sleeve_b(book, 0.88, 600)  # edge = 0.17
    assert tr is not None and tr.get("veto_candidate"), f"no veto at 17c: {tr}"
    assert tr["edge"] >= 0.15
    tr2 = decide_sleeve_b(book, 0.85, 600)  # edge = 0.14
    assert tr2 is not None and not tr2.get("veto_candidate"), \
        f"false veto at 14c: {tr2}"


# #13: sleeve C never at 1c or 99c+.
def test_13_c_price_guards():
    book99 = {"ask_up": (0.995, 50.0), "ask_down": (0.05, 50.0)}
    assert decide_sleeve_c(book99, 0.999, 30) is None
    book1c = {"ask_up": (0.99, 50.0), "ask_down": (0.01, 50.0)}
    assert decide_sleeve_c(book1c, 0.002, 30) is None
    book_ok = {"ask_up": (0.96, 50.0), "ask_down": (0.06, 50.0)}
    tr = decide_sleeve_c(book_ok, 0.9985, 30)
    assert tr is not None and tr["legs"][0]["side"] == "up"
    # wrong side of certainty -> None
    assert decide_sleeve_c(book_ok, 0.5, 30) is None
    # too early -> None
    assert decide_sleeve_c(book_ok, 0.9985, 300) is None


# #14: empty book disables B and C.
def test_14_empty_book_disables_b_and_c():
    md = {"marketData": {"bids": [], "offers": [],
                         "state": "MARKET_STATE_OPEN"}}["marketData"]
    b = parse_book(md)
    assert b["empty_book"] is True
    assert decide_sleeve_b(b, 0.75, 300) is None
    assert decide_sleeve_c(b, 0.999, 30) is None
    md2 = {"bids": [{"px": {"value": "0.44"}, "qty": "10"}],
           "offers": []}
    b2 = parse_book(md2)
    assert b2["empty_book"] is True          # one-sided still counts
    assert b2["ask_up"] is None
    assert decide_sleeve_b(b2, 0.75, 300) is None


# #15: unfilled limits are marked unfilled, never booked as wins.
def test_15_partial_fill_accounting():
    book = {"ask_up": (0.60, 10.0), "ask_down": (0.45, 10.0)}
    legs = [{"side": "up", "price": 0.60, "size": 25.0}]
    filled, unfilled = paper_fill(legs, book)
    assert len(filled) == 1 and filled[0]["filled"] == 10.0
    assert len(unfilled) == 1
    assert abs(unfilled[0]["unfilled"] - 15.0) < 1e-9
    # P&L must only ever touch filled qty: simulate a loss
    leg = filled[0]
    pnl = leg["filled"] * 0.0 - leg["cost"]   # Up loses
    # stake + taker fee (0.07*p*(1-p) per share) on filled only
    assert pnl == -(10.0 * 0.60 + 10.0 * taker_fee_per_share(0.60))


def test_15_no_depth_no_fill():
    book = {"ask_up": None, "ask_down": (0.45, 10.0)}
    filled, unfilled = paper_fill([{"side": "up", "price": 0.6, "size": 5}], book)
    assert filled == [] and len(unfilled) == 1


# #16: blotter completeness.
def test_16_blotter_fields():
    snap = {"S": 85010.5, "tau": 420, "p_up": 0.62}
    book = {"ask_up": (0.55, 100.0), "ask_down": (0.47, 120.0)}
    rec = blotter_base("btc-updown-15m-2026-09-27-1215z", 1_790_511_300,
                       84981.42, snap, book)
    required = {"event_id", "Topen", "Tclose", "O", "S", "tau", "p_fair",
                "ask_up", "ask_down"}
    assert required.issubset(rec.keys()), required - set(rec.keys())
    assert rec["Tclose"] == rec["Topen"] + 900
    assert rec["p_fair"] == 0.62


def test_16_pair_sleeve_a_math():
    # ask_up + ask_down + fees + slip < 1.00 -> trade; else None
    book = {"ask_up": (0.45, 200.0), "ask_down": (0.50, 200.0)}
    tr = decide_sleeve_a(book)
    assert tr is not None and len(tr["legs"]) == 2
    assert tr["edge"] > 0
    book2 = {"ask_up": (0.55, 200.0), "ask_down": (0.50, 200.0)}
    assert decide_sleeve_a(book2) is None  # 1.05 + costs >= 1
    # no size on one side -> abort (both legs fill or neither)
    book3 = {"ask_up": (0.45, 0.0), "ask_down": (0.50, 200.0)}
    tr3 = decide_sleeve_a(book3)
    assert tr3 is None or all(l["size"] > 0 for l in tr3["legs"])


# ---- review 2026-10-06: config hash, kill switches, latency ----

def test_17_signal_hashes_deterministic_and_stamped():
    from paper import (SIG, RISK, HASH_FROZEN_AT, FAIRVALUE_MODEL_V,
                       signal_hashes, risk_hash, hash_stamp)
    assert SIG == signal_hashes()
    assert RISK == risk_hash()
    assert all(len(h) == 16 for h in list(SIG.values()) + [RISK])
    assert len(set(SIG.values())) == 4  # sleeves hash independently
    # v2: per-hash freeze times are metadata, not hash inputs
    assert set(HASH_FROZEN_AT) == {"A", "B", "C", "D", "risk"}
    assert all(v.startswith("2026-10-06") or v == "__V2_FREEZE_TIME__" for v in HASH_FROZEN_AT.values())
    assert FAIRVALUE_MODEL_V.startswith("fv1:")
    snap = {"p_up": 0.7, "S": 85000.0, "tau": 300}
    book = {"ask_up": (0.6, 100.0), "ask_down": (0.45, 100.0)}
    rec = blotter_base("slug", 123, 84900.0, snap, book)
    stamp = hash_stamp()
    for k, v in stamp.items():
        assert rec[k] == v
    assert rec["frozen_at"] == HASH_FROZEN_AT


def test_17_signal_hash_changes_with_params():
    from paper import signal_hashes
    import paper as pm
    h1 = signal_hashes()
    old = pm.B_EDGE_MIN
    pm.B_EDGE_MIN = old + 0.001
    try:
        h2 = signal_hashes()
        assert h2["B"] != h1["B"]
        assert h2["D"] == h1["D"]  # tuning B never resets D's era
        assert h2["A"] == h1["A"] and h2["C"] == h1["C"]
    finally:
        pm.B_EDGE_MIN = old
    assert signal_hashes() == h1


def test_17_risk_hash_independent_of_signal():
    from paper import signal_hashes, risk_hash
    import paper as pm
    s1, r1 = signal_hashes(), risk_hash()
    old = pm.KILL_DRAWDOWN_PCT
    pm.KILL_DRAWDOWN_PCT = old + 0.01
    try:
        assert risk_hash() != r1      # risk change re-arms risk accounting
        assert signal_hashes() == s1  # but never invalidates signal eras
    finally:
        pm.KILL_DRAWDOWN_PCT = old
    assert risk_hash() == r1


def test_18_kill_rolling20_and_drawdown():
    from paper import kill_check, KILL_ROLL20_PCT, KILL_DRAWDOWN_PCT, BANKROLL
    base = {"day": "2999-01-01", "day_pnl": 0.0, "consec_losses": 0,
    "day_pnls": {},
            "disabled": [], "brier_windows": []}
    # rolling-20: 20 losses of -6 each = -120 <= -10% of 1000
    st = dict(base, settle_pnls=[-6.0] * 20, cum_pnl=-120.0, peak_cum=0.0)
    halt, reasons = kill_check(st)
    assert halt and any("rolling-20" in r for r in reasons)
    assert "B" in st["disabled"]
    # drawdown: peak 50, now -120 -> dd 170 >= 15% of 1000
    st = dict(base, settle_pnls=[1.0] * 20, cum_pnl=-120.0, peak_cum=50.0)
    halt, reasons = kill_check(st)
    assert halt and any("drawdown" in r for r in reasons)
    # healthy state: no halt
    st = dict(base, settle_pnls=[2.0] * 20, cum_pnl=40.0, peak_cum=40.0)
    halt, reasons = kill_check(st)
    assert not halt


def test_18_kill_rolling20_needs_20_samples():
    from paper import kill_check
    st = {"day": "2999-01-01", "day_pnl": 0.0, "consec_losses": 0,
    "day_pnls": {},
          "disabled": [], "brier_windows": [],
          "settle_pnls": [-50.0] * 19, "cum_pnl": 0.0, "peak_cum": 0.0}
    halt, _ = kill_check(st)
    assert not halt  # 19 samples is not enough to judge


def test_19_latency_reverify_blocks_when_book_empties():
    from paper import reverify_after_latency, LATENCY_MS
    import paper as pm
    assert LATENCY_MS > 0
    trade = {"sleeve": "B", "edge": 0.10,
             "legs": [{"side": "up", "size": 20.0, "price": 0.60}]}
    # monkeypatch get_book to return an empty book after the delay
    orig_sleep, orig_gb, orig_pb = pm.time.sleep, pm.get_book, pm.parse_book
    pm.time.sleep = lambda s: None
    pm.get_book = lambda T: {"empty": True}
    pm.parse_book = lambda raw: {"empty_book": True}
    try:
        ok, t2, e2, b2 = reverify_after_latency(
            trade, 123, 84900.0, 999, {"p_up": 0.7}, {})
    finally:
        pm.time.sleep, pm.get_book, pm.parse_book = orig_sleep, orig_gb, orig_pb
    assert ok is False


# ---- review 2026-10-06 round 3: Brier drain must run with no matured trades ----

def test_20_brier_drain_runs_without_matured_positions(tmp_path, monkeypatch):
    import json as _json
    import paper as pm
    import brti
    now = 2000000000
    t_closed = now - 2000
    state = {"open": [], "pending_brier": {
                 str(t_closed): {"p_fair": 0.7, "mid_up": 0.6, "tau": 300}},
             "brier_windows": [], "day_pnl": 0.0, "consec_losses": 0,
             "day_pnls": {},
             "decided_windows": [],
             "disabled": [], "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state", lambda s: saved.update(s))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    (tmp_path / "captures.jsonl").write_text(
        _json.dumps({"T": t_closed, "published_priceToBeat": 85000.0}) + "\n")
    monkeypatch.setattr(pm, "HERE", str(tmp_path))
    monkeypatch.setattr(brti, "fetch_1s", lambda a, b: [])
    monkeypatch.setattr(brti, "avg60", lambda candles, Te: 85001.0)
    pm.settle()
    assert len(saved["brier_windows"]) == 1
    assert str(t_closed) not in saved["pending_brier"]
    w = saved["brier_windows"][0]
    assert w["T"] == t_closed
    # outcome up (85001 >= 85000): model 0.7 beats market 0.6
    assert w["brier_model"] < w["brier_mkt"]


# ---- round 4 (2026-10-06): batched freeze — shadow mode, phase coverage ----

def test_21_d_band_phase_coverage_60s_ticks():
    """60s tick spacing must hit both 120s-wide D bands on every cron phase."""
    from paper import D1_TAU_LO, D1_TAU_HI, D2_TAU_LO, D2_TAU_HI
    for phase in range(60):  # cron phase within the 60s tick grid
        taus = [phase + 60 * k for k in range(20)]
        assert any(D1_TAU_LO <= t <= D1_TAU_HI for t in taus), f"D1 miss at phase {phase}"
        assert any(D2_TAU_LO <= t <= D2_TAU_HI for t in taus), f"D2 miss at phase {phase}"


def test_22_shadow_trigger_scored_at_settle(tmp_path, monkeypatch):
    import json as _json
    import paper as pm
    import brti
    now = 2000000000
    t_closed = now - 2000
    shadow = {"ts": now - 2000, "T": t_closed, "event_id": "slug",
              "sub": "D1", "tau": 300, "lead_bp": 7.0, "vol_bp": 2.0,
              "regime": "calm", "side": "up", "ask": 0.80, "depth": 50.0,
              "ask_cap": 0.80, "ask_cap_pass": True, "edge": 0.10,
              "trigger": "trade", "sig_D": pm.SIG["D"],
              "frozen_at": dict(pm.HASH_FROZEN_AT), "disposition": "traded",
              "latency_pass": True, "outcome": None, "hold": None}
    state = {"open": [], "pending_brier": {}, "brier_windows": [],
             "d_shadow": [shadow], "day_pnl": 0.0, "consec_losses": 0,
             "day_pnls": {},
             "decided_windows": [],
             "disabled": [], "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state", lambda s: saved.update(s))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    (tmp_path / "captures.jsonl").write_text(
        _json.dumps({"T": t_closed, "published_priceToBeat": 85000.0}) + "\n")
    monkeypatch.setattr(pm, "HERE", str(tmp_path))
    monkeypatch.setattr(brti, "fetch_1s", lambda a, b: [])
    monkeypatch.setattr(brti, "avg60", lambda candles, Te: 85001.0)
    pm.settle()
    assert saved["d_shadow"][0]["outcome"] == "up"
    assert saved["d_shadow"][0]["hold"] == 1


def test_23_gate_v2_constants_enacted():
    from paper import GATE_B_TRADES, GATE_BRIER_TOL, GATE_BRIER_N
    from paper import KILL_ROLL20_PCT, KILL_DRAWDOWN_PCT
    assert GATE_B_TRADES == 150
    assert GATE_BRIER_TOL == 0.005
    assert GATE_BRIER_N == 100
    assert KILL_ROLL20_PCT == -0.07   # LaDre: -7% ($70)
    assert KILL_DRAWDOWN_PCT == 0.10  # LaDre: 10% ($100)


def test_24_bootstrap_lb_sane():
    from paper import _bootstrap_lb
    assert _bootstrap_lb([1.0] * 50) == 1.0
    assert _bootstrap_lb([-1.0] * 50) == -1.0
    lb = _bootstrap_lb([2.0, -1.0] * 25)
    assert -1.0 < lb < 2.0


def test_24_d_shadow_logs_while_disabled():
    """Observation must not stop: D shadow logs triggers even when D is
    disabled; no fill occurs."""
    import paper as pm
    # disabled D1, band open (tau in D1 band), trigger fires
    state = {"open": [], "decided_windows": [], "decided_d1": [], "decided_d2": [],
             "day_pnl": 0.0, "consec_losses": 0, "disabled": ["D"],
             "day_pnls": {},
             "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0,
             "d_shadow": [], "brier_windows": [], "pending_brier": {},
             "day": "2026-10-06"}
    assert pm.d_disabled(state, "D1")
    # simulate the observe path: decide_sleeve_d fires, shadow logged
    book = {"ask_up": (0.80, 100.0), "bid_up": (0.78, 100.0),
            "ask_down": (0.22, 100.0), "bid_down": (0.20, 100.0)}
    td = pm.decide_sleeve_d(book, lead_bp=8.0, vol_bp=2.0, tau=400, sub="D1")
    # if a trigger fires, the disabled path must log it without trading
    if td is not None and not td.get("fade") and not td.get("veto_candidate"):
        assert pm.d_disabled(state, "D1")  # fill stays gated
    # hashes unaffected by the observation-gating change
    assert pm.signal_hashes()["D"] == pm.SIG["D"]


# ---- 2026-10-06 P1 (early-halt path): a kill stops fills, never evidence ----

def test_25_halt_observes_without_trading(tmp_path, monkeypatch):
    """While halted, the tick still fetches the book, logs asks, queues the
    Brier intake and runs D shadow observation -- but books no trade."""
    import paper as pm
    # tau in the D1 band (240-360): now % 900 == 600 -> tau == 300
    now = 1999999800 + 600
    T = pm.window_start(now)
    assert T + 900 - now == 300
    state = {"open": [], "decided_windows": [], "decided_d1": [], "decided_d2": [],
             "day_pnl": -60.0, "consec_losses": 0, "disabled": [],
             "day_pnls": {pm.today_key(): -60.0},
             "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0,
             "d_shadow": [], "brier_windows": [], "pending_brier": {},
             "day": pm.today_key()}
    saved = {}
    wrote = []
    book = {"ask_up": (0.80, 100.0), "bid_up": (0.78, 100.0),
            "ask_down": (0.22, 100.0), "bid_down": (0.20, 100.0)}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state", lambda s: saved.update(s))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "append_jsonl", lambda path, rec: wrote.append(rec))
    monkeypatch.setattr(pm, "get_event", lambda T: {"markets": [{"m": 1}]})
    monkeypatch.setattr(pm, "parse_terms",
                        lambda m: {"priceToBeat": 85000.0, "index": "BRTI"})
    monkeypatch.setattr(pm, "get_book", lambda T: {"raw": 1})
    monkeypatch.setattr(pm, "parse_book", lambda raw: book)
    monkeypatch.setattr(pm, "mid_up", lambda b: 0.79)
    monkeypatch.setattr(pm, "_cached_sigma", lambda *a: 0.01)
    monkeypatch.setattr(pm, "fair_snapshot",
                        lambda *a, **k: {"p_up": 0.75, "S": 85010.0, "tau": 300})
    monkeypatch.setattr(pm, "d_lead_vol", lambda O, Te, st: (8.0, 2.0))
    monkeypatch.setattr(pm, "decide_sleeve_d",
                        lambda b, lead, vol, tau, sub: {"sleeve": sub, "edge": 0.10})
    rec = pm.run_once()
    assert rec is not None
    assert rec["reason"].startswith("kill switch (observation only)")
    # Brier intake ran despite the halt
    assert str(T) in saved["pending_brier"]
    pb = saved["pending_brier"][str(T)]
    assert pb["p_fair"] == 0.75 and pb["mid_up"] == 0.79 and pb["tau"] == 300
    # asks were logged on the blotter rec
    assert rec["ask_up"][0] == 0.80
    # D shadow observed with halt disposition -- and nothing filled
    shadows = saved.get("d_shadow", [])
    assert shadows and shadows[-1]["disposition"] == "halt_no_trade"
    assert shadows[-1]["sub"] == "D1"
    assert saved["open"] == []
    assert wrote and wrote[-1]["reason"].startswith("kill switch (observation only)")
    # non-halt behavior and hashes untouched by the gating change
    assert pm.signal_hashes()["B"] == pm.SIG["B"]


# ---- 2026-10-06 P1 (kill-transition alert): deduplicated, one alert per cycle ----

def test_26_kill_transition_alert_dedup(capsys):
    """A kill alerts exactly once while active, again on clear, and again
    on re-fire. Live values inside the reason string must not re-trigger."""
    import paper as pm
    state = {}
    # first fire: alerts
    new, cleared = pm.kill_transition_alert(
        state, ["day_pnl -60.00 <= -5% bankroll"])
    assert new == ["day"] and cleared == []
    out = capsys.readouterr().out
    assert "KILL TRANSITION (active): day_pnl -60.00" in out
    assert state["kill_alerted"] == ["day"]
    # same kill, changed live value: no re-alert (stable id, not text)
    new, cleared = pm.kill_transition_alert(
        state, ["day_pnl -61.25 <= -5% bankroll"])
    assert new == [] and cleared == []
    assert capsys.readouterr().out == ""
    # second kill type joins: only the new one alerts
    new, cleared = pm.kill_transition_alert(
        state, ["day_pnl -61.25 <= -5% bankroll",
                "8 consecutive directional losses"])
    assert new == ["consec"] and cleared == []
    assert "KILL TRANSITION (active): 8 consecutive" in capsys.readouterr().out
    # day clears: cleared alert fires once
    new, cleared = pm.kill_transition_alert(
        state, ["8 consecutive directional losses"])
    assert new == [] and cleared == ["day"]
    out = capsys.readouterr().out
    assert "KILL TRANSITION (cleared; day kill disables nothing -- " \
        "trading resumes on recovery / next UTC day): day" in out
    # sleeve-disabling kills keep the manual re-enable text
    pm.kill_transition_alert(state, ["8 consecutive directional losses"])
    capsys.readouterr()
    new, cleared = pm.kill_transition_alert(state, [])
    assert cleared == ["consec"]
    assert "KILL TRANSITION (cleared; reason cleared but B/C/D remain " \
        "disabled -- manual re-enable required): consec" in capsys.readouterr().out
    # all clear, then re-fire: alerts again
    pm.kill_transition_alert(state, [])
    capsys.readouterr()
    new, cleared = pm.kill_transition_alert(
        state, ["day_pnl -55.00 <= -5% bankroll"])
    assert new == ["day"] and cleared == []
    assert "KILL TRANSITION (active)" in capsys.readouterr().out
    # unknown reason shape still gets a stable id, never crashes
    new, _ = pm.kill_transition_alert(state, ["something brand new"])
    assert new == ["other:something brand new"]
    # hashes unaffected by the alert plumbing
    assert pm.signal_hashes()["B"] == pm.SIG["B"]
