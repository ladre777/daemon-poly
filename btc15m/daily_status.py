#!/usr/bin/env python3
"""Daily Satoshi status: B trades n/150, Brier n+CI (current model_v + all),
kills, skipped ticks (last 24h). REPORT-ONLY: reads state files, writes
nothing, takes no engine actions. Gate decisions use `paper.py --report` only.
"""
import json, math, re, sys
from datetime import datetime, timezone, timedelta

BASE = '/home/hatch/workspace/polymarket/btc15m'
sys.path.insert(0, BASE)
from paper import FAIRVALUE_MODEL_V  # read-only import; no engine action

st = json.load(open(BASE + '/paper_state.json'))

# B trades under CURRENT sig_B (read from state, never hardcoded).
# Join on settle.sig_B == current sig_B, exactly like paper.py --report.
# (Post-freeze settle records inherit hash_stamp() from their open record;
# pre-freeze trades carry no sig and land in history.)
SIG_B = st.get('sig_B')
# Gate join (LaDre 2026-10-06 review fix, STAGED): count settle records
# stamped with the CURRENT sig_B, exactly like paper.py --report.
# Decision records always carry sleeve=None, so the old join
# (type=="decision" and sleeve=="B") could never match -- the headline
# would sit at 0/150 forever and real trades would land in history.
b_trades = 0
try:
    with open(BASE + '/blotter.jsonl') as f:
        recs = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn blotter line: skip, don't crash the status read
    for r in recs:
        if (r.get('type') == 'settle' and r.get('sleeve') == 'B'
                and SIG_B and r.get('sig_B') == SIG_B):
            b_trades += 1
except FileNotFoundError:
    pass

# Brier CI, read-only: current model_v AND all records, side by side.
def brier_str(ws):
    diffs = [w['brier_model'] - w['brier_mkt'] for w in ws
             if w.get('brier_model') is not None and w.get('brier_mkt') is not None]
    n = len(diffs)
    if n >= 2:
        mean = sum(diffs) / n
        var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
        se = math.sqrt(var / n)
        return (f"n={n}, point {mean:+.4f}, "
                f"95% CI [{mean - 1.96 * se:+.4f}, {mean + 1.96 * se:+.4f}]")
    if n == 1:
        return f"n={1}, point {diffs[0]:+.4f} (CI n/a)"
    return "n=0"

bw = st.get('brier_windows', [])
bw_cur = [w for w in bw if w.get('model_v') == FAIRVALUE_MODEL_V]

# Kills (read-only)
disabled = st.get('disabled', [])
kills_str = ', '.join(disabled) if disabled else 'none active'

# Skipped ticks in the last 24h only (timestamped log lines).
now = datetime.now(timezone.utc)
skips_24h = 0
skip_pat = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z SKIP \(run lock held')
try:
    with open(BASE + '/paper.log', errors='replace') as f:
        for line in f:
            m = skip_pat.match(line)
            if m:
                ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
                if now - ts <= timedelta(hours=24):
                    skips_24h += 1
except FileNotFoundError:
    skips_24h = -1

print("SATOSHI DAILY STATUS (paper, report-only)")
print(f"  Sleeve B trades (current sig_B {SIG_B}): {b_trades}/150 toward gate v2")
print(f"  Brier current model_v (model-market): {brier_str(bw_cur)}")
print(f"  Brier all records (model-market):    {brier_str(bw)}")
print(f"  Kills: {kills_str}")
print(f"  Skipped ticks, last 24h (lock): {skips_24h}")
print(f"  Open positions: {len(st.get('open', []))}")
print(f"  live_ok: false (paper only)")

# Tick durations + coverage from HEARTBEAT lines (last 24h), both phases.
# Settle gets its own parse: a slow settle (drains up to 24 closed windows with
# sequential API calls) is the leading suspect for lock contention and is
# invisible otherwise. Seen-vs-expected makes missing ticks show up.
def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(p * (len(sorted_vals) - 1)))
    return sorted_vals[i]

hb = {"run-once": [], "settle": []}
hb_pat = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z '
                    r'HEARTBEAT phase=([a-z-]+) status=(ok|error) secs=([0-9.]+)')
skip_pat = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z '
                      r'SKIP \(run lock held')
skips_24h_hb = 0
window_start = now - timedelta(hours=24)
try:
    with open(BASE + '/paper.log', errors='replace') as f:
        for line in f:
            m = hb_pat.match(line)
            if m:
                ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
                if ts >= window_start and m.group(2) in hb:
                    hb[m.group(2)].append((ts, float(m.group(4)), m.group(3)))
                continue
            m2 = skip_pat.match(line)
            if m2:
                ts2 = datetime.fromisoformat(m2.group(1)).replace(tzinfo=timezone.utc)
                if ts2 >= window_start:
                    skips_24h_hb += 1
except FileNotFoundError:
    pass
for phase in ("run-once", "settle"):
    recs = hb[phase]
    secs = sorted(s for _, s, _ in recs)
    errs = sum(1 for _, _, st in recs if st == "error")
    span_start = min((ts for ts, _, _ in recs), default=window_start)
    if span_start < window_start:
        span_start = window_start
    expected = max(1, int((now - span_start).total_seconds() // 60))
    seen = len(secs)
    # Claude 2026-10-09: when seen > expected these are EXTRA runs (duplicate
    # schedulers), not missing ones -- label the direction honestly.
    _delta = seen - expected
    _dlab = f"extra +{_delta}" if _delta > 0 else f"missing {-_delta}"
    if secs:
        med, p95 = _pct(secs, 0.50), _pct(secs, 0.95)
        print(f"  Heartbeats phase={phase}: seen {seen}/{expected} "
              f"({_dlab}, errors {errs}), median {med:.1f}s, "
              f"p95 {p95:.1f}s, max {secs[-1]:.1f}s")
    else:
        print(f"  Heartbeats phase={phase}: seen {seen}/{expected} "
              f"({_dlab}), no data yet")
print(f"  Lock skips (last 24h, heartbeat window): {skips_24h_hb}")
print(f"  [Gate decisions use `paper.py --report` only.]")
