# Settlement-Source Packet — REVISION 4 for Claude's signature
**Date:** 2026-10-13 (~15:50 EDT) · **Status: REVISION 4 — all 4 of Claude's Revision-3 edits applied verbatim**
**Scope:** Satoshi BTC 15m paper stack. Paper-only. No changes to `paper.py`, no kill/risk/sleeve changes, no live path.
**Supersedes:** settlement_source_packet_rev3_20261013.md (Revision 3).

---

## 1. The certified outcome rule (stated exactly)

Polymarket US 15m BTC Up/Down settles Up when the venue-published `settlementPrice` for window T is greater than or equal to the venue-published `priceToBeat` for window T; tie goes to Up:

```
outcome = "up"  if settlementPrice(T) >= priceToBeat(T)
          "down" otherwise
```

- `settlementPrice(T)` = the venue's `assetPriceTerms.settlementPrice` for window T.
- `priceToBeat(T)` = the venue's `assetPriceTerms.priceToBeat` for window T; when a live fetch misses it, our observed `O` is used and flagged `our_O_proxy` (the stored start price equals the captured price-to-beat(T) on 544/544 windows, verified; see §6).
- `settle_rule_version = "pt1"` (tie → up).

Both `settlementPrice` and `priceToBeat` are the venue's own 60-second BRTI simple averages, rounded to 2 decimals, as stated in the venue's market rules. Our own averaging (`s_avg`) is never used for settlement.

The certification below rests on the verified evidence in §6: 543/543 point-rule consistency across the store, 79/79 settlement prices equal to the next window's price-to-beat to the cent, and start prices equal to the captured price-to-beat(T) on 544/544 windows. Certification covers **this rule only** — not the resolution index (§5).

**What this packet changes and what it doesn't:**
- Trade settlement already uses the next window's `priceToBeat` as the outcome label (Part 1, adopted 2026-10-09). This packet does not change that.
- If this packet moves Brier or research labels to `venue_truth` labels, the shadow verdict's **primary** analysis stays on `s_avg` labels as pre-registered. Venue labels remain its registered **secondary** analysis.
- The shadow experiment has not latched: it stands at 172/300 complete windows and has been stalled since Oct 10 (paper.py:1753 skips decided windows while D is disabled). This packet does not amend the shadow pre-registration.

---

## 2. Primary and fallback settlement sources

**Primary source — Tier B: `venue_settlementPrice`.**
The venue's own published end-point price, captured live within minutes of window close. 79 windows in the venue-truth store (`research/venue_truth.jsonl`) carry tier-B rows as of 2026-10-13 14:35 EDT. These are direct venue readings, not estimates.

- **Endpoint:** `https://gateway.polymarket.us/v1/search?query=btc-updown-15m-YYYY-MM-DD-HHMMz&limit=10` (US search API, deterministic event slug).
- **Field:** `markets[0].assetPriceTerms.settlementPrice.value`.
- **Capture:** the `satoshi-venue-truth-capture` cron (every 5 min, installed 02:32 EDT) fetches just-closed windows within minutes of close. The US search index drops closed windows within ~25 min, so capture-within-minutes is mandatory.
- **Schema note:** 70 of the 79 tier-B rows are missing the `end_price_src` field (only `outcome_src` is set). This is a tagging gap in the capture script. A schema fix (tag `end_price_src` on all settlement-price rows) is queued as a capture-script change with diff, test, and Claude review. It does not affect any outcome in the store: 1,016 raw lines across 545 windows, zero conflicting outcomes.

**Fallback source — Tier C: `next_pb` proxy.**
The venue's own published boundary price, captured live as the *next* window's `priceToBeat`. Flagged per row (`end_price_src = "next_pb_proxy"`, `outcome_src = "next_pb_proxy"`). 464 windows in the store carry tier-C rows as of 2026-10-13 14:35 EDT.

**Validation of the fallback (measured, not assumed):**
- `venue_settlementPrice == next_pb` to the cent on 79/79 tier-B windows (§6 row 2, verified).
- Every stored end price equals the captured next-window price-to-beat to the cent: 543/543 (§6 row 3, verified on the file as pushed).
- Earlier snapshot figures (496/496 at 06:26Z, the 57/57 probe) are context only; see the note under §6.
- Tier C is therefore a venue-published price with measured cent-level agreement — a flagged provenance, not a model estimate. No tier-C row is ever presented as tier-B.

**Coverage (verified on the file as pushed, 2026-10-13 ~14:30 EDT):**
- 543/545 distinct windows in the store carry an outcome (99.63%); missing T=1791433800 and T=1791452700 (§6 row 4).
- 0 new settlements from the averaging path since deploy, as reported by p0_acceptance.py. UNVERIFIED: not part of the certification.

**Independent verifier — Tier K: Kalshi `KXBTC15M` `expiration_value`.** [STATUS: UNSIGNED MONITOR — see §5 and edit 6 below.]
Kalshi's KXBTC15M market settles on the same BRTI 60-second simple average. Across **491/491** compared windows (2026-10-02 10:30Z to 2026-10-13 07:15Z), the Kalshi expiration value agrees with the venue's settlement reference at **$0.000000** maximum difference. File also contains 8 `no_markets` and 1 JSON error, stated here for completeness.
**Regime alarm:** any future nonzero diff between tier-K `expiration_value` and the venue's settlement reference for the same window fires an **automatic regime alarm** to Claude and LaDre immediately — same urgency as the §4 void trigger.

---

## 3. Tier-A spot-check documentation

Tier A = the venue's own **published outcome** (`venue_resolution`) — the highest-trust tier, preferred over any price comparison.

**What was attempted.** Two independent capture paths scan every closed window's live market record for a published resolution:
- `research/settle_forensics.py::resolve_windows` (15m cron, running since 2026-10-09): for every closed window still within retention, `markets.get_event(T)` is called and the record is scanned for `winningOutcome`, `winner`, `resolvedOutcome`, `resolution`, `result`, `outcome`, `settledOutcome`, `marketResult`, plus `outcomes[*].winner/isWinner`.
- `research/venue_truth.py --capture` (deploy run 2026-10-13 06:25Z): `_extract_resolution` scans `resolution`, `resolvedOutcome`, `winner`, `result`, plus `outcomes[*].winner/isWinner`, on every just-closed window fetched.

**Result.** 1301 live fetch attempts across **481 distinct closed windows** (forensics) + 2 live fetches (venue-truth capture) = **483 distinct windows attempted. Tier-A `venue_resolution` observed: 0/483.**

Not part of the certified evidence (see the context note under §6).

Where no resolution field exists, the row falls to tier B (`venue_settlementPrice` decides the outcome) or the attempt is recorded as a miss.

**Honest fact about the US API.** The US search index (`/v1/search` on gateway.polymarket.us) drops closed windows within minutes of close (verified live: a window closed ~25 min prior was already unfindable), and direct `/v1/markets/{slug}` lookup of a closed window returns an error. Tier-A observation must happen inside the capture window — and inside that window, the US API has never exposed a published resolution field on any of the 483 windows attempted. Capture-within-minutes is mandatory; that is why the P0 capture cron exists.

---

## 4. Standing condition (automatic void)

If **any** future observation captures a tier-A `venue_resolution` that **contradicts** the tier-B/C-derived outcome for the same window, this certification's latch **voids automatically** — no vote, no grace period. The contradiction, the window, both records, and the capture timestamps come to **Claude and LaDre** immediately, and the outcome rule returns to *unconfirmed* until the contradiction is resolved and re-signed. Tier-A agreement, when it ever occurs, reinforces the latch; tier-A silence does not weaken it.

**Cent-level void trigger:** any disagreement **over $0.01** between a tier-B `settlementPrice` and the corresponding tier-C `next_pb`, or any Kalshi tier-K difference **above $0.00**, voids this certification under the same automatic procedure. The $0.50 cross-check remains as a monitor only.

---

## 5. What this packet does NOT certify

This packet certifies the **outcome rule only**: venue-published settlementPrice(T) ≥ venue-published priceToBeat(T) → Up; tie → Up.

It does **not** certify which data stream the venue's settlement prices track. The US product's `assetPriceTerms` carries `indexSymbol: "BRTI"`, while venue documentation names the **Chainlink BTC/USD data stream** as the resolution source. **P1 is answered: the US product settles on the BRTI 60-second simple average** — Kalshi's KXBTC15M `expiration_value` agrees with the venue's settlement reference 491/491 at $0.00 (see Tier K, §2).

**Tier K status — UNSIGNED MONITOR.** D7 parked the Kalshi route on terms (Kalshi's developer terms do not permit the use pattern). Whether reading Kalshi's *public* `expiration_value` purely as a settlement cross-check is permitted under those terms is **not confirmed**. Until LaDre confirms, Tier K is an unsigned monitor: its 491/491 agreement is reported as observed fact, but it is **not part of this certification**. Nothing else in the packet depends on it. The tier-K regime alarm above is active as a monitor regardless.

---

## 6. Evidence table — every number with the five audit elements

*As-of timestamps are stated per row because the store grows every 5 minutes.*

| # | Figure | Definition | Source (file:function) | n | Time range | Status |
|---|---|---|---|---|---|---|
| 1 | 79 tier-B rows | `outcome_src="venue_settlementPrice"` in venue_truth.jsonl | `settle_forensics.py::venue_terms` → `markets.py::get_event`/`parse_terms`; endpoint `gateway.polymarket.us/v1/search`, field `assetPriceTerms.settlementPrice.value` | 79 | window-start T 2026-10-10 00:45Z – 2026-10-13 16:45Z | VERIFIED (Claude independent recompute) |
| 2 | 79/79 cent agreement | `next_pb` equals `settlementPrice` to the cent; all gaps 0.0 | `p0_acceptance.py:84-91` | 79 | same as above | VERIFIED (Claude independent recompute) |
| 3 | 543/543 end-price agreement | every stored end price equals the captured next-window price-to-beat to the cent | Claude independent recompute on venue_truth.jsonl as pushed | 543 | store state at push (2026-10-13 ~14:30 EDT) | VERIFIED (Claude independent recompute) |
| 4 | 543/545 windows with outcome (99.63%) | distinct windows in the store with an outcome; missing T=1791433800 and T=1791452700 | Claude independent recompute on venue_truth.jsonl as pushed | 545 | store state at push | VERIFIED (Claude independent recompute) |
| 5 | 491/491 Kalshi at $0.00 | Kalshi `expiration_value` vs venue settlement ref; max diff $0.000000; plus 8 no_markets, 1 JSON error | `p1_kalshi_expirations.json` + `p1_fingerprint_report_20261013.md`; pulled by `p1_kalshi_pull.py` | 491 of 500 | 2026-10-02 10:30Z – 2026-10-13 07:15Z | VERIFIED as computed (Claude independent recompute). Certification status: UNSIGNED MONITOR per §5. |
| 6 | −$30.78 point P&L | Σ over 37 Sleeve B trades of round(per-trade point P&L, 2); sig 9a20c2fd5000cac5 | `p0_point_rule_ledger.py:82`; `pnl_restate.py::recompute_pnl`; inputs `blotter.jsonl` + `venue_truth.jsonl` | 37 trades | 2026-10-02 – 2026-10-13 | VERIFIED (Claude independent recompute). Unrounded Σ = −$30.8169 → −$30.82. The $0.04 is per-trade cent rounding, not an error. Convention stated here. |
| 7 | −$64.72 recorded | Σ `pnl_after_fees` as booked | `blotter.jsonl` | 37 trades | same | VERIFIED (unchanged) |

**Superseded figure:** the earlier +$20.28 was the restated P&L for a 23-trade subset (the ledger was stale with only 23 of 37 B trades at that time). It is **superseded** by the 37-trade −$30.78 figure above — the two cover different trade sets and must not be compared directly.

**Store structure note:** the file holds 1,016 raw lines across 545 distinct windows — many windows were captured more than once. No window has conflicting outcomes across its lines; readers deduplicate by T with last-line-wins. T=1791433800 and T=1791452700 are two separate windows with no outcome recorded (the two missing from the 543/545 count), not duplicate rows.

**Context (not certified):** the 57/57 P0 probe (`settle_probe_20261013.md`, point rule vs venue on next_pb-recoverable truth windows) and the 0/483 tier-A spot-check are retained here as background. Their inputs were not in the data drop, so they are not part of the certified evidence above.

---

## 7. Change log (Revision 3 → Revision 4)

1. §1 priceToBeat bullet: "(probe-verified 70/70 faithful strike proxy)" replaced with "(the stored start price equals the captured price-to-beat(T) on 544/544 windows, verified; see §6)".
2. §2 validation bullets: replaced with the verified figures (79/79, 543/543); earlier snapshots noted as context only.
3. §2 coverage block: replaced with the verified 543/545; p0 averaging-path figure marked UNVERIFIED, not part of certification.
4. §3: tier-A result marked "Not part of the certified evidence (see the context note under §6)."

## 8. Change log (Revision 2 → Revision 3)

1. Row 3: replaced 496/496 (06:26Z snapshot, not independently recomputed) with Claude's verified 543/543 on the pushed file.
2. Row 4: replaced 477/479 (unverified denominator) with Claude's verified 543/545.
3. Removed the 57/57 probe and 0/483 tier-A rows from the certified table; kept as context. §1 now rests the certification on the verified evidence (543/543, 79/79, 544/544).
4. Fixed the duplicates footnote: T=1791433800 and T=1791452700 are separate windows with no outcome; 1,016 raw lines for 545 windows, dedupe by T last-wins, no conflicts.
5. Row 1: time range stated as window-start T, 2026-10-10 00:45Z to 2026-10-13 16:45Z.

## 8. Change log (Revision 1 → Revision 2)

1. §1 reworded: outcome defined via venue-published prices; both identified as the venue's 60-second BRTI averages; "not on an average" and "every resolved FLIP collapses" removed.
2. Added §1 "what this packet changes and what it doesn't": shadow primary stays on s_avg; shadow stalled at 172/300 noted.
3. Void trigger tightened to cent level ($0.01 B-vs-C; $0.00 Kalshi); $0.50 retained as monitor only.
4. All counts corrected and reconciled: 79 tier-B (was 71), 464 tier-C (was 426), 496/496 (was 497/497), each with the five audit elements.
5. Scoreboard: $0.04 identified as per-trade rounding; convention stated; "+$20.28" corrected to "superseded" with its n=23.
6. Tier K moved to unsigned monitor pending LaDre's terms confirmation.
7. Removed "Claude's 3 v4 edits" attribution (source session not available to the signer).

---

**Claude's decision:**

- [ ] **SIGNED** — I accept the outcome rule as certified above, with the §4 void condition and the §5 scope limit.
- [ ] **RETURNED WITH EDITS** — edits attached; the draft stands unsigned.

Signed: ___________________________ Date: ___________

Edits (if returned): ____________________________________________________
