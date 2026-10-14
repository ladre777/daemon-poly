"""Coverage-gap and restart finder for the P3 recorders (read-only).
Usage: python3 -I p3_gaps.py BOOK.jsonl[.gz] TRADES.jsonl[.gz] CHAINLINK.jsonl[.gz]
Prints each file's span, startup records and every gap above its threshold, in EDT.
Then prints JOINT gaps: times where all three files have a gap that overlaps."""
import gzip, json, sys, datetime as dt
EDT = dt.timezone(dt.timedelta(hours=-4))
def f(t): return dt.datetime.fromtimestamp(t, EDT).strftime('%m-%d %H:%M:%S')
def opn(p): return gzip.open(p, 'rt') if p.endswith('.gz') else open(p)
THR = [30, 120, 30]  # book polls 3s, trades 2s but prints are sparse, chainlink streams ~1s
allgaps = []
for p, thr in zip(sys.argv[1:4], THR):
    ts, starts = [], []
    for l in opn(p):
        try: r = json.loads(l)
        except Exception: continue
        t = r.get('recv_ts') or r.get('ts')
        if t is None: continue
        if t > 1e12: t /= 1000
        if r.get('type') == 'startup': starts.append(t)
        ts.append(t)
    ts.sort()
    g = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > thr]
    allgaps.append(g)
    print(f'== {p} n={len(ts)} {f(ts[0])} -> {f(ts[-1])}')
    print('   startups:', [f(s) for s in starts])
    for a, b in g: print(f'   gap {f(a)} -> {f(b)}  {round(b-a)}s')
print('== JOINT gaps (all three overlap)')
for a, b in allgaps[0]:
    if all(any(a2 < b and b2 > a for a2, b2 in G) for G in allgaps[1:]):
        print(f'   {f(a)} -> {f(b)}  {round(b-a)}s')
