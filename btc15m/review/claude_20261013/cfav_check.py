#!/usr/bin/env python3
"""C-fav independent checker (Claude, 2026-10-13).

Written from research/cfav_prereg_20261013.md ONLY, without reading
cfav_shadow.py, so the two scripts can be compared trade by trade (§11).
Read-only: never writes engine files. Stdlib only.

Usage:
  python research/cfav_check.py --R <registration epoch> \
      --books 'research/p3/book_snapshots_*.jsonl*' \
      --captures captures.jsonl [--strikes 'research/p3/book_snapshots_*'] \
      [--out research/cfav_check_trades.jsonl] [--verdict research/cfav_verdict_check.json]
"""
import argparse, glob, gzip, json, math, random, sys
from decimal import Decimal, ROUND_HALF_EVEN

Q = 10                     # §6
A_LO, A_HI = 0.85, 0.97    # §5
TAU_LO, TAU_HI = 60, 300   # §5
FAV_MIN = 0.85             # §4
GAP_MAX = 30               # §8
N_EVAL = 300               # §7
FUTILITY_FROM = 50         # §9
DD_MAX = 50.0              # §7.4
BLOCK, NBOOT, SEED = 8, 10000, 20261013


def _open(p):
    return gzip.open(p, "rt") if p.endswith(".gz") else open(p)


def _iter(paths):
    for p in sorted(paths):
        with _open(p) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def _num(x):
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def fee(theta, p, c):
    """§4: round_half_even(θ × C × p × (1−p), 2) in Decimal."""
    t, pp, cc = Decimal(str(theta)), Decimal(str(p)), Decimal(str(c))
    return float((t * cc * pp * (1 - pp)).quantize(Decimal("0.01"),
                                                   rounding=ROUND_HALF_EVEN))


def top(levels):
    """Best level (price, size) or None."""
    if not levels:
        return None
    p, s = _num(levels[0][0]), _num(levels[0][1])
    return (p, s) if p is not None and s is not None else None


def favourite(rec):
    """§4: the side whose best ask >= 0.85; None if neither or both."""
    up, dn = top(rec.get("offers_up")), top(rec.get("offers_down"))
    cand = [(side, lv) for side, lv in (("up", up), ("down", dn))
            if lv and lv[0] >= FAV_MIN]
    return cand[0] if len(cand) == 1 else None


def is_open(rec):
    return (rec.get("state") == "MARKET_STATE_OPEN"
            and rec.get("empty_book") is False)


def coverage_ok(T, recs):
    """§8: >=1 book record in tau [60,300]; no gap > 30 s between band
    start (ts=T+600), consecutive records, and band end (ts=T+840)."""
    lo, hi = T + 900 - TAU_HI, T + 900 - TAU_LO
    ts = sorted(r["ts"] for r in recs if lo <= r["ts"] <= hi)
    if not ts:
        return False
    pts = [lo] + ts + [hi]
    return all(b - a <= GAP_MAX for a, b in zip(pts, pts[1:]))


def label(T, pb):
    """§3: Up iff round(pb(T+900),2) >= round(pb(T),2). None if missing."""
    a, b = pb.get(T), pb.get(T + 900)
    if a is None or b is None:
        return None
    return round(b, 2) >= round(a, 2)


def window_trade(T, recs, pb):
    """Apply §5 to one window's book records (sorted by ts).
    Returns (status, trade_or_None)."""
    if not coverage_ok(T, recs):
        return "coverage_fail", None
    for i, r in enumerate(recs):
        tau = T + 900 - r["ts"]
        if not (TAU_LO <= tau <= TAU_HI) or not is_open(r):
            continue
        fav = favourite(r)
        if not fav:
            continue
        side, (a, depth) = fav
        if not (A_LO <= a <= A_HI) or depth < Q:
            continue
        # trigger found -> exactly one latency check on the NEXT record
        nxt = recs[i + 1] if i + 1 < len(recs) else None
        if nxt is None:
            return "faded", None
        tau2 = T + 900 - nxt["ts"]
        fav2 = favourite(nxt) if is_open(nxt) else None
        if (fav2 is None or fav2[0] != side or tau2 < TAU_LO
                or not (A_LO <= fav2[1][0] <= A_HI) or fav2[1][1] < Q):
            return "faded", None
        a2 = fav2[1][0]
        theta = (nxt.get("fee") or {}).get("taker_theta")
        if _num(theta) is None:
            return "no_fee", None
        up = label(T, pb)
        if up is None:
            return "no_label", None
        win = (side == "up") == up
        f = fee(theta, a2, Q)
        net = round(Q * (1.0 if win else 0.0) - Q * a2 - f, 2)
        return "entered", {"T": T, "side": side, "a_trigger": a,
                           "a2": a2, "tau_trigger": tau, "tau_entry": tau2,
                           "fee": f, "win": win, "net": net,
                           "net_per_share": round(net / Q, 6)}
    return "no_trigger", None


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    ph = k / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def ell_star(trades):
    return sum(1 - t["a2"] - t["fee"] / Q for t in trades) / len(trades)


def block_ci(x, L=BLOCK, n=NBOOT, seed=SEED):
    rng, m = random.Random(seed), len(x)
    L = max(1, min(L, m))
    means = []
    for _ in range(n):
        s = []
        while len(s) < m:
            st = rng.randrange(0, m - L + 1)
            s.extend(x[st:st + L])
        means.append(sum(s[:m]) / m)
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def max_drawdown(nets):
    peak = cum = dd = 0.0
    for v in nets:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return round(dd, 2)


def evaluate(trades):
    """§7 on exactly the first 300 trades by T."""
    t = sorted(trades, key=lambda r: r["T"])[:N_EVAL]
    x = [r["net_per_share"] for r in t]
    lo, hi = block_ci(x)
    losses = sum(1 for r in t if not r["win"])
    w_lo, w_hi = wilson(losses, len(t))
    es = ell_star(t)
    h1, h2 = x[:N_EVAL // 2], x[N_EVAL // 2:]
    dd = max_drawdown([r["net"] for r in t])
    c = {"edge": sum(x) / len(x) > 0 and lo > 0,
         "tail": w_hi < es,
         "stability": sum(h1) / len(h1) > 0 and sum(h2) / len(h2) > 0,
         "drawdown": dd <= DD_MAX}
    return {"verdict": "PASS" if all(c.values()) else "FAIL",
            "criteria": c, "n": len(t), "mean_per_share": sum(x) / len(x),
            "ci95": [lo, hi], "losses": losses, "wilson95": [w_lo, w_hi],
            "ell_star": es, "max_drawdown": dd}


def futility(trades):
    """§9: from trade 50 on, after each trade."""
    t = sorted(trades, key=lambda r: r["T"])
    for k in range(FUTILITY_FROM, min(len(t), N_EVAL) + 1):
        sub = t[:k]
        losses = sum(1 for r in sub if not r["win"])
        if wilson(losses, k)[0] > ell_star(sub):
            return {"verdict": "FAIL (futility)", "at_trade": k,
                    "losses": losses, "ell_star": ell_star(sub)}
    return None


def run(R, book_paths, cap_path, strike_paths=()):
    pb = {}
    if cap_path:
        for r in _iter([cap_path]):
            v, T = _num(r.get("published_priceToBeat")), r.get("T")
            if v is not None and T is not None:
                pb[int(T)] = v
    for r in _iter(strike_paths):
        if r.get("type") == "strike_retry_ok":
            v = _num(r.get("strike_priceToBeat"))
            if v is not None:
                pb.setdefault(int(r["T"]), v)
    by_T = {}
    for r in _iter(book_paths):
        if r.get("type") != "book" or r.get("T") is None or r.get("ts") is None:
            continue
        T = int(r["T"])
        if T < R + 900:
            continue
        by_T.setdefault(T, []).append(r)
    counts, trades = {}, []
    for T in sorted(by_T):
        recs = sorted(by_T[T], key=lambda r: r["ts"])
        st, tr = window_trade(T, recs, pb)
        counts[st] = counts.get(st, 0) + 1
        if tr:
            trades.append(tr)
    return counts, trades


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--R", type=int, required=True)
    ap.add_argument("--books", required=True)
    ap.add_argument("--captures")
    ap.add_argument("--strikes", default="")
    ap.add_argument("--out", default="research/cfav_check_trades.jsonl")
    a = ap.parse_args(argv)
    counts, trades = run(a.R, glob.glob(a.books), a.captures,
                         glob.glob(a.strikes) if a.strikes else ())
    with open(a.out, "w") as f:
        for t in trades:
            f.write(json.dumps(t, sort_keys=True) + "\n")
    print("counts:", json.dumps(counts, sort_keys=True), "entered:", len(trades))
    fut = futility(trades)
    if fut:
        print("FUTILITY:", json.dumps(fut))
    elif len(trades) >= N_EVAL:
        print("EVALUATION:", json.dumps(evaluate(trades)))
    else:
        print(f"counts only until {N_EVAL} entered trades (§7)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
