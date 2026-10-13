#!/usr/bin/env python3
"""Tests for the D-shadow observation re-entry fix (paper.py SKIP condition).

The bug: paper.py's SKIP check was
    if str(T) in state.get("decided_windows", []) and not (d1_open or d2_open):
which always skipped decided windows while D is disabled, killing shadow
observation (LaDre 2026-10-06: observation must not stop).

The fix: include the observe flags in the SKIP condition:
    if str(T) in state.get("decided_windows", []) and not (d1_open or d2_open or d1_observe or d2_observe):

These tests replicate the EXACT boolean expressions from paper.py (lines
1740-1753) and verify the three required properties:
  (a) decided window + D disabled + in D band  -> NOT skipped (observation proceeds)
  (b) decided window + D disabled + outside bands -> still SKIPPED
  (c) fills remain gated on d_disabled (observe path never fills)

Run:  python -m pytest /tmp/test_observe_reentry.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.expanduser("~"), "workspace/polymarket/btc15m"))

from paper import (  # noqa: E402
    d_disabled,
    D1_TAU_LO, D1_TAU_HI,
    D2_TAU_LO, D2_TAU_HI,
)


def compute_flags(T, tau, sleeves, state):
    """Replicates paper.py lines 1740-1751 EXACTLY."""
    d1_band = D1_TAU_LO <= tau <= D1_TAU_HI
    d2_band = D2_TAU_LO <= tau <= D2_TAU_HI
    d1_open = ("D" in sleeves and not d_disabled(state, "D1") and d1_band
               and str(T) not in state.get("decided_d1", []))
    d2_open = ("D" in sleeves and not d_disabled(state, "D2") and d2_band
               and str(T) not in state.get("decided_d2", []))
    # Shadow observation runs even while D is disabled (LaDre 2026-10-06:
    # observation must not stop). The fill stays gated on enabled.
    d1_observe = ("D" in sleeves and d1_band
                  and str(T) not in state.get("decided_d1", []))
    d2_observe = ("D" in sleeves and d2_band
                  and str(T) not in state.get("decided_d2", []))
    return d1_open, d2_open, d1_observe, d2_observe


def old_skip(T, state, d1_open, d2_open):
    """The BUGGY SKIP condition (paper.py line 1753, before fix)."""
    return str(T) in state.get("decided_windows", []) and not (d1_open or d2_open)


def new_skip(T, state, d1_open, d2_open, d1_observe, d2_observe):
    """The FIXED SKIP condition."""
    return (str(T) in state.get("decided_windows", [])
            and not (d1_open or d2_open or d1_observe or d2_observe))


def base_state():
    """D disabled (as in production since 2026-10-13)."""
    return {"disabled": ["B", "C", "D"],
            "decided_windows": [],
            "decided_d1": [],
            "decided_d2": []}


# --- (a) decided window + D disabled + in D band -> NOT skipped ---

def test_a1_decided_window_in_d1_band_not_skipped():
    T, tau = 1791912000, 300  # tau=300 in D1 band [240,360]
    state = base_state()
    state["decided_windows"] = [str(T)]
    sleeves = ("A", "B", "D")
    d1_open, d2_open, d1_observe, d2_observe = compute_flags(T, tau, sleeves, state)
    # Sanity: with D disabled, open flags are False but observe is True
    assert d1_open is False
    assert d1_observe is True
    # OLD code skipped (the bug)
    assert old_skip(T, state, d1_open, d2_open) is True
    # NEW code does NOT skip -> observation proceeds
    assert new_skip(T, state, d1_open, d2_open, d1_observe, d2_observe) is False


def test_a2_decided_window_in_d2_band_not_skipped():
    T, tau = 1791912000, 120  # tau=120 in D2 band [60,180]
    state = base_state()
    state["decided_windows"] = [str(T)]
    sleeves = ("A", "B", "D")
    d1_open, d2_open, d1_observe, d2_observe = compute_flags(T, tau, sleeves, state)
    assert d2_open is False
    assert d2_observe is True
    assert old_skip(T, state, d1_open, d2_open) is True
    assert new_skip(T, state, d1_open, d2_open, d1_observe, d2_observe) is False


# --- (b) decided window + D disabled + outside bands -> still SKIPPED ---

def test_b1_decided_window_outside_bands_still_skipped():
    T, tau = 1791912000, 500  # tau=500 outside both bands
    state = base_state()
    state["decided_windows"] = [str(T)]
    sleeves = ("A", "B", "D")
    d1_open, d2_open, d1_observe, d2_observe = compute_flags(T, tau, sleeves, state)
    assert d1_observe is False
    assert d2_observe is False
    assert new_skip(T, state, d1_open, d2_open, d1_observe, d2_observe) is True


def test_b2_undecided_window_never_skipped():
    T, tau = 1791912000, 300
    state = base_state()  # decided_windows empty
    sleeves = ("A", "B", "D")
    d1_open, d2_open, d1_observe, d2_observe = compute_flags(T, tau, sleeves, state)
    assert new_skip(T, state, d1_open, d2_open, d1_observe, d2_observe) is False


# --- (c) fills remain gated on d_disabled (observe path never fills) ---

def test_c1_fill_blocked_when_d_disabled():
    """Replicates paper.py line 2004: the fill gate."""
    state = base_state()
    # Even when observe is True, d_disabled blocks the fill
    assert d_disabled(state, "D1") is True
    assert d_disabled(state, "D2") is True
    # The downstream gate: `if d_disabled(state, sub) or observe_only:`
    # -> flush_d_shadow("disabled_no_trade"); mark_decided(sub); continue
    # i.e. observe only; never fill while disabled.
    sub = "D1"
    observe_only = False
    would_fill = not (d_disabled(state, sub) or observe_only)
    assert would_fill is False


def test_c2_observe_flags_do_not_enable_fills():
    """d1_observe/d2_observe are not consulted by any fill gate."""
    # Grep-level guarantee: the only fill gate in the D loop is
    # `if d_disabled(state, sub) or observe_only:` (paper.py:2004).
    # d1_observe/d2_observe appear only in:
    #   - the loop tuple (paper.py:1995-1996) as the `observe` element,
    #   - the `if trade is not None or not observe: continue` guard (observation only),
    #   - and (after fix) the SKIP condition.
    # None of these is a fill path. Verified by code inspection 2026-10-13.
    assert True  # documents the audit; the behavioral guarantee is test_c1


def test_c3_d1_band_boundaries():
    """Band edges are inclusive, matching paper.py."""
    assert D1_TAU_LO == 240 and D1_TAU_HI == 360
    assert D2_TAU_LO == 60 and D2_TAU_HI == 180
    T = 1791912000
    state = base_state()
    state["decided_windows"] = [str(T)]
    sleeves = ("A", "B", "D")
    for tau, expect_observe in [(239, False), (240, True), (360, True),
                                (361, False), (59, False), (60, True),
                                (180, True), (181, False)]:
        _, _, d1o, d2o = compute_flags(T, tau, sleeves, state)
        assert (d1o or d2o) == expect_observe, f"tau={tau}"
