#!/usr/bin/env python3
"""Design-validation checks for Sleeve D (lead persistence).

Uses cached Phase-3 data (cells_candles.json, cells_markets.json,
captures.jsonl). No network.

Checks:
 1. D2 (10-15bp lead @ 2min) split by vol regime -- does the published
    cell's no-regime-split hold, or does regime matter?
 2. D1 band robustness: 5-10bp hold rate when the lead is measured at
    Te-240 / Te-300 / Te-360 (design proposes trigger band tau 240-360).
"""
import json
import math
import sys

sys.path.insert(0, '.')
from brti import avg60

candles = {int(k): v for k, v in json.load(open('cells_candles.json')).items()}
mkts = json.load(open('cells_markets.json'))
clist = sorted(candles.items())
caps = {}
for line in open('captures.jsonl'):
    line = line.strip()
    if line:
        c = json.loads(line)
        caps[c['slug']] = c['T']


def vol_bp(Te):
    closes = [candles.get(t) for t in range(Te - 3660, Te - 59, 60)]
    if any(x is None for x in closes) or len(closes) < 61:
        return None
    rets = [math.log(closes[j + 1] / closes[j]) for j in range(60)]
    mu = sum(rets) / 60
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / 60) * 1e4


def hold(lead, winner):
    return (lead > 0 and winner == 'Up') or (lead < 0 and winner == 'Down')


rows = []
for slug, T in caps.items():
    v = mkts.get(slug)
    if not v or not v.get('resolved') or v['open'] is None \
            or v['winner'] not in ('Up', 'Down'):
        continue
    Te = T + 900
    o = v['open']
    leads = {}
    ok = True
    for tag, t in (('m6', Te - 360), ('m5', Te - 300), ('m4', Te - 240),
                   ('m2', Te - 120)):
        tw = avg60(clist, t)
        if tw is None:
            ok = False
            break
        leads[tag] = (tw - o) / o * 1e4
    vb = vol_bp(Te)
    if not ok or vb is None:
        continue
    rows.append({'lead': leads, 'vol': vb, 'winner': v['winner']})

vols = sorted(r['vol'] for r in rows)
med = vols[len(vols) // 2]
print(f"windows: {len(rows)}, vol median {med:.2f} bp")

print("\n--- check 1: D2 (10-15bp @2min) by regime ---")
for name, sub in (('calm', [r for r in rows if r['vol'] < med]),
                  ('volatile', [r for r in rows if r['vol'] >= med])):
    b = [r for r in sub if 10 <= abs(r['lead']['m2']) <= 15]
    k = sum(1 for r in b if hold(r['lead']['m2'], r['winner']))
    print(f"  {name}: {k}/{len(b)} = {k/len(b):.1%}" if b else f"  {name}: n=0")

print("\n--- check 2: D1 band robustness (5-10bp lead, calm) ---")
for tag in ('m6', 'm5', 'm4'):
    b = [r for r in rows if 5 <= abs(r['lead'][tag]) <= 10 and r['vol'] < med]
    k = sum(1 for r in b if hold(r['lead'][tag], r['winner']))
    print(f"  lead @{tag}: {k}/{len(b)} = {k/len(b):.1%}" if b else f"  @{tag}: n=0")

print("\n--- check 3: D1 volatile band (should stay a fade) ---")
for tag in ('m6', 'm5', 'm4'):
    b = [r for r in rows if 5 <= abs(r['lead'][tag]) <= 10 and r['vol'] >= med]
    k = sum(1 for r in b if hold(r['lead'][tag], r['winner']))
    print(f"  lead @{tag}: {k}/{len(b)} = {k/len(b):.1%}" if b else f"  @{tag}: n=0")
