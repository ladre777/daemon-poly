#!/usr/bin/env python3
"""Tests for Sleeve D (lead persistence) — pure decide_sleeve_d, no network.

Run:  python -m pytest test_sleeve_d.py -q   (from ~/workspace/polymarket/btc15m)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from paper import (decide_sleeve_d, d_disabled, migrate_decided,
                   D1_BREAKEVEN, D2_BREAKEVEN, D_EDGE_MIN, D_VETO_EDGE,
                   D_VOL_MEDIAN)


def book(ask_up, size_up, ask_down, size_down):
    return {"ask_up": (ask_up, size_up), "ask_down": (ask_down, size_down),
            "bid_up": (ask_up - 0.02, size_up), "bid_down": (ask_down - 0.02, size_down)}


def test_d1_fires_calm_up():
    b = book(0.78, 10.0, 0.22, 10.0)
    t = decide_sleeve_d(b, 7.0, 2.5, 300, "D1")
    assert t and t["sleeve"] == "D1"
    assert t["legs"][0]["side"] == "up"
    assert t["legs"][0]["price"] == 0.78
    assert t["edge"] >= D_EDGE_MIN


def test_d1_fires_down():
    b = book(0.30, 10.0, 0.78, 10.0)
    t = decide_sleeve_d(b, -7.0, 2.5, 300, "D1")
    assert t and t["legs"][0]["side"] == "down"
    assert t["legs"][0]["price"] == 0.78


def test_d1_fade_volatile():
    b = book(0.78, 10.0, 0.22, 10.0)
    t = decide_sleeve_d(b, 7.0, 5.0, 300, "D1")
    assert t and t.get("fade") is True


def test_d1_boundary_vol_calms():
    b = book(0.78, 10.0, 0.22, 10.0)
    # vol exactly at median counts as volatile (needs_calm is strict <)
    assert decide_sleeve_d(b, 7.0, D_VOL_MEDIAN, 300, "D1").get("fade") is True
    # just under: fires
    assert decide_sleeve_d(b, 7.0, D_VOL_MEDIAN - 0.01, 300, "D1").get("fade") is None


def test_d1_rejects_out_of_band():
    b = book(0.78, 10.0, 0.22, 10.0)
    assert decide_sleeve_d(b, 7.0, 2.5, 239, "D1") is None
    assert decide_sleeve_d(b, 7.0, 2.5, 361, "D1") is None
    assert decide_sleeve_d(b, 4.9, 2.5, 300, "D1") is None   # lead too small
    assert decide_sleeve_d(b, 10.1, 2.5, 300, "D1") is None  # lead too big
    assert decide_sleeve_d(b, 7.0, None, 300, "D1") is None  # missing vol
    assert decide_sleeve_d(b, None, 2.5, 300, "D1") is None  # missing lead


def test_d1_ask_cap():
    b = book(0.81, 10.0, 0.22, 10.0)  # over the 80c cap
    assert decide_sleeve_d(b, 7.0, 2.5, 300, "D1") is None


def test_d1_winners_curse_veto():
    b = book(0.70, 10.0, 0.30, 10.0)  # edge ~= 20c
    t = decide_sleeve_d(b, 7.0, 2.5, 300, "D1")
    assert t and t.get("veto_candidate") is True
    assert t["edge"] >= D_VETO_EDGE


def test_d2_fires_volatile_regime():
    # D2 ignores regime: fires even at high vol
    b = book(0.85, 10.0, 0.15, 10.0)
    t = decide_sleeve_d(b, 12.0, 8.0, 120, "D2")
    assert t and t["sleeve"] == "D2"
    assert t["legs"][0]["side"] == "up"


def test_d2_rejects():
    b = book(0.85, 10.0, 0.15, 10.0)
    assert decide_sleeve_d(b, 9.9, 8.0, 120, "D2") is None   # lead too small
    assert decide_sleeve_d(b, 15.1, 8.0, 120, "D2") is None  # lead too big
    assert decide_sleeve_d(b, 12.0, 8.0, 59, "D2") is None
    assert decide_sleeve_d(b, 12.0, 8.0, 181, "D2") is None
    # D2 ignores regime entirely: missing vol is fine, fires anyway
    assert decide_sleeve_d(b, 12.0, None, 120, "D2")["sleeve"] == "D2"
    b2 = book(0.91, 10.0, 0.15, 10.0)  # over the 90c cap
    assert decide_sleeve_d(b2, 12.0, 8.0, 120, "D2") is None


def test_d_sleeves_dont_overlap():
    # D1 lead band at D2's tau -> None, and vice versa
    b = book(0.78, 10.0, 0.22, 10.0)
    assert decide_sleeve_d(b, 7.0, 2.5, 120, "D1") is None
    assert decide_sleeve_d(b, 12.0, 8.0, 300, "D2") is None


def test_d_disabled_granularity():
    st = {"disabled": []}
    assert d_disabled(st, "D1") is False
    st["disabled"].append("D")
    assert d_disabled(st, "D1") is True
    assert d_disabled(st, "D2") is True
    st2 = {"disabled": ["D1"]}
    assert d_disabled(st2, "D1") is True
    assert d_disabled(st2, "D2") is False


def test_migrate_decided_idempotent():
    st = {"decided_windows": ["123"]}
    migrate_decided(st)
    assert st["decided_windows"] == ["123"]
    assert st["decided_d1"] == [] and st["decided_d2"] == []
    migrate_decided(st)
    assert st["decided_d1"] == [] and st["decided_d2"] == []


def test_d_breakevens_sane():
    # breakeven = ask cap + taker fee at cap; edge gate must exceed it
    from common import taker_fee_per_share
    assert abs(D1_BREAKEVEN - (0.80 + taker_fee_per_share(0.80))) < 1e-9
    assert abs(D2_BREAKEVEN - (0.90 + taker_fee_per_share(0.90))) < 1e-9


def _run_once_isolated(tmp_path, **patches):
    """Run paper.run_once with isolated STATE/BLOTTER and mocked I/O."""
    import json
    from unittest import mock
    import paper
    state_p = tmp_path / "state.json"
    blot_p = tmp_path / "blotter.jsonl"
    mods = [mock.patch.object(paper, "STATE", str(state_p)),
            mock.patch.object(paper, "BLOTTER", str(blot_p))]
    mods += [mock.patch.object(paper, k, v) for k, v in patches.items()]
    for m in mods:
        m.start()
    try:
        ret = paper.run_once(sleeves=("A", "B", "D"))
    finally:
        for m in reversed(mods):
            m.stop()
    s = json.load(open(state_p))
    blot = blot_p.read_text().strip() if blot_p.exists() else ""
    return ret, s, blot, paper.window_start(paper.now_s())


def test_soft_fail_event_fetch_does_not_burn_window(tmp_path):
    ret, s, blot, T = _run_once_isolated(
        tmp_path, get_event=_boom)
    assert ret is None
    assert str(T) not in s.get("decided_windows", [])
    assert str(T) not in s.get("decided_d1", [])
    assert blot == ""  # no blotter spam on transient failure


def _boom(*a, **k):
    raise Exception("boom")


def test_soft_fail_window_not_listed_does_not_burn_window(tmp_path):
    ret, s, blot, T = _run_once_isolated(
        tmp_path, get_event=lambda T: {})
    assert ret is None
    assert str(T) not in s.get("decided_windows", [])
    assert blot == ""
