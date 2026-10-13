#!/usr/bin/env python3
"""72-hour sigma-estimator shadow (2026-10-08, LaDre's word: "whatever Claude
suggests goes").

Pure functions, no state, no network, no fairvalue.py import. The shadow
estimators are scored alongside the live model for 72h so the adoption
decision is made on paired market data, not on the 2.4-day model-only
sample alone.

Estimators (match research/sigma_walkforward.py exactly):
  - sigma_horizon60: stdev of 60s log returns over the trailing 2h,
    divided by sqrt(60) -> per-second units, floored at 1e-5.
  - sigma_floor_3e5: max(sd_2h, sd_15m, 3e-5) -- the live estimator's
    shape with a 3e-5 floor, as the control.

p_up uses the same phi formula as the live model, clamped to
[1e-6, 1-1e-6] exactly like the walk-forward script.

This module is deliberately outside fairvalue.py: importing or editing
it must never change FAIRVALUE_MODEL_V, SIG, or RISK.
"""
import math

SIGMA_FLOOR = 1e-5
LOOKBACK_LONG = 2 * 3600  # 7200s, matches the walk-forward's 2h window
SIXTY = 60


def _sample_sd(xs):
    """Sample standard deviation (ddof=1). None if fewer than 2 points."""
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(max(var, 0.0))


def sigma_horizon60(rows_1s):
    """Horizon-matched sigma from 60s log returns.

    rows_1s: iterable of (ts, close) or (ts, close, tickCount), sorted
    by ts ascending (venue gap-fills, so seconds are dense). For each
    second in the trailing 2h window [t_end-7200, t_end], the 60s log
    return log(px[t]/px[t-60]) is formed; sigma = sd(returns)/sqrt(60),
    floored at 1e-5. Matches the walk-forward's
    max(sd(Q,Q2,i-7200,i)/sqrt(60), 1e-5).
    """
    closes = {}
    for r in rows_1s:
        ts, c = int(r[0]), float(r[1])
        if c > 0:
            closes[ts] = c
    if not closes:
        return None
    t_end = max(closes)
    t_start = t_end - LOOKBACK_LONG
    rets = []
    for ts in range(t_start, t_end + 1):
        a = closes.get(ts - SIXTY)
        b = closes.get(ts)
        if a is not None and b is not None:
            rets.append(math.log(b / a))
    sd = _sample_sd(rets)
    if sd is None:
        return None
    return max(sd / math.sqrt(SIXTY), SIGMA_FLOOR)


def sigma_floor_3e5(sd_2h, sd_15m):
    """Live estimator shape with a 3e-5 floor (the walk-forward control)."""
    cands = [x for x in (sd_2h, sd_15m) if x]
    if not cands:
        return None
    return max(max(cands), 3e-5)


def p_up(S, O, sigma, tau):
    """Fair P(Up), same phi formula as the live model, clamped to
    [1e-6, 1-1e-6] exactly like research/sigma_walkforward.py."""
    if not S or not O or S <= 0 or O <= 0:
        return None
    if not sigma or sigma <= 0 or tau is None or tau <= 0:
        return None
    p = 0.5 * (1.0 + math.erf(
        math.log(S / O) / (sigma * math.sqrt(tau)) / math.sqrt(2.0)))
    return min(max(p, 1e-6), 1 - 1e-6)
