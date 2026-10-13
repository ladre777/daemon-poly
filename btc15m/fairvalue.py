#!/usr/bin/env python3
"""Fair value P(Up) = Phi(ln(S/O) / (sigma * sqrt(tau))). BRTI ONLY.

S selection (brief §5.3, adjusted for the verified [T-60,T-1] venue rule):
  - tau > 59s : S = latest complete 1s BRTI close.
  - tau <= 59s: S = running 60-print average over [now-60, now-1], which
    converges to the exact statistic the venue settles on.

sigma: realized vol per sqrt-second from 1s log returns. Computed over a
2h lookback AND a rolling 15m; we take the max (conservative: never
overconfident). Floor at 1e-5/sqrt(s) to avoid degenerate 0/1 probabilities.

Binance/Coinbase/Chainlink are NEVER inputs here. A fixture test (#11)
pins that a Binance-up / BRTI-flat scenario emits no Up signal.
"""
import math
import time

from brti import fetch_1s

SIGMA_FLOOR = 1e-5
LOOKBACK_LONG = 2 * 3600
LOOKBACK_SHORT = 900


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realized_sigma(t_end, lookback_sec):
    """Per-sqrt-second vol from 1s log returns in [t_end-lookback, t_end]."""
    rows = fetch_1s(t_end - lookback_sec, t_end)
    rets = []
    for i in range(1, len(rows)):
        a, b = rows[i - 1][1], rows[i][1]
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 60:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def estimate_sigma(t_now):
    """Conservative sigma: max(2h, 15m, floor). None if data insufficient."""
    s_long = realized_sigma(t_now, LOOKBACK_LONG)
    s_short = realized_sigma(t_now, LOOKBACK_SHORT)
    cands = [s for s in (s_long, s_short) if s]
    if not cands:
        return None
    return max(max(cands), SIGMA_FLOOR)


def select_S(T_end, t_now):
    """Return (S, s_source). Inside final 59s use the running 60-print avg."""
    tau = T_end - t_now
    if tau <= 59:
        rows = fetch_1s(t_now - 60, t_now - 1)
        by_ts = {ts: c for ts, c in rows}
        vals = [by_ts[t] for t in range(t_now - 60, t_now) if t in by_ts]
        if len(vals) == 60:
            return sum(vals) / 60.0, "running_avg60"
    rows = fetch_1s(t_now - 3, t_now)
    if rows:
        return rows[-1][1], "last_close"
    return None, "unavailable"


def p_up(S, O, sigma, tau):
    """Fair P(Up). Returns None on bad inputs. Tie convention: close>=open
    settles Up, so P(Up)=0.5 exactly when S==O (phi(0)=0.5)."""
    if not S or not O or S <= 0 or O <= 0:
        return None
    if not sigma or sigma <= 0 or tau is None or tau <= 0:
        return None
    return phi(math.log(S / O) / (sigma * math.sqrt(tau)))


def fair_snapshot(T_start, O, t_now=None, sigma=None):
    """Full fair-value snapshot for window [T_start, T_start+900)."""
    t_now = int(t_now or time.time())
    T_end = T_start + 900
    tau = T_end - t_now
    S, src = select_S(T_end, t_now)
    sig = sigma if sigma else estimate_sigma(t_now)
    p = p_up(S, O, sig, tau) if tau > 0 else None
    return {"T": T_start, "T_end": T_end, "t": t_now, "tau": tau,
            "O": O, "S": S, "S_source": src, "sigma": sig, "p_up": p}


def brier(p, outcome):
    """Brier score component. outcome in {0,1}."""
    return (p - outcome) ** 2
