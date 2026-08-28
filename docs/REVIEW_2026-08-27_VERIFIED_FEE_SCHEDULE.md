# Review — Verified Fee Schedule

**Date:** 2026-08-27
**Branch:** `feat/verified-fee-schedule`
**Base:** `de9f467fe6878cbab0de48fb45a3725595630e94` — current `main`, **not**
`feat/production-evidence-readiness`, which remains unmerged and awaiting review.

**Nothing was merged. Nothing was deployed. No Railway variable, `DRY_RUN`,
`ORDER_STRATEGY`, `CHECKER_MAX_TOKENS`, threshold, cap, gate or risk
parameter was changed. No order was submitted or cancelled. Kalshi's trading
API was not contacted.**

---

## Source

| | |
|---|---|
| Document | Kalshi Fee Schedule |
| URL | https://kalshi.com/docs/kalshi-fee-schedule.pdf |
| Effective | 2026-07-07 |
| Retrieved | 2026-08-27, supplied by the operator |

**Retrieval note.** This session could not fetch the document. The egress
policy gateway answers `403` to `CONNECT` for every Kalshi host
(`kalshi.com`, `docs.kalshi.com`, `help.kalshi.com`,
`trading-api.kalshi.com`). Per `/root/.ccr/README.md`, policy denials are
reported rather than retried. The operator supplied their own copy of the
PDF and the transcription was made from that. No fee number in this branch
comes from a blog post, a search snippet, or an inference — every one is
either read from the document or refused.

---

## Diff summary

| File | Reason | Safety impact | Test coverage | Config/deploy action later? |
|---|---|---|---|---|
| `core/fee_schedule.py` *(new, 330 lines)* | Transcribed schedule: rates, per-series multipliers, rounding, provenance, `Unavailable` sentinel | **None.** Nothing imports it yet — it is not wired into `core/pricing.py` or any decision path. Pure data plus a pure function. | 53 tests, incl. all 42 published table values | **Yes.** Wiring it into the pricing path is a separate, reviewed change — see "Not done deliberately". |
| `tests/test_fee_schedule.py` *(new, 250 lines)* | Boundary, rounding, symmetry, multiplier, refusal and provenance coverage | None | — | No |
| `docs/FEE_SCHEDULE.md` *(new)* | Data definitions, the three settled disputes, the before/after divergence table, the open rounding question | None | — | No |

**Net: 3 new files. No file modified. No file deleted. No existing test
changed.**

---

## Test results

```
$ python -m pytest -q
15 failed, 1049 passed in 20.71s

$ python -m ruff check .
All checks passed!
```

| | Baseline (`de9f467`) | This branch | Delta |
|---|---|---|---|
| Passed | 996 | 1049 | **+53** |
| Failed | 15 | 15 | **0** |

These counts are post-verification; see "Independent verification" below for
what changed after the first pass.

The 15 are the documented pre-existing set, unchanged in count and identity:
8 × `test_checker_budget`, 4 × `test_funnel_accounting`,
2 × `test_signal_alert_dedupe`, 1 × `test_vol_smoothing_bias`.
Not investigated — out of scope per the brief.

---

## The three disputed points, settled

### 1. Category-varying multiplier / crypto premium — **claim is false**

The base rate is `0.07` for every event contract market. What varies is a
per-series multiplier `M`: taker is only ever `0` or `1`, maker is `0`, `1`,
or `2` in the single case of `KXMVE`. Crypto is not charged
more; the two non-standard crypto series go the other way and are
**fee-free**: `KXBTCY` (BTC price range EOY) and `KXETHY` (ETH price EOY),
both `0/0`.

**None of `KXBTC`, `KXBTCD`, `KXBTC15M`, `KXETH`, `KXETHD`, `KXWTI`,
`KXHIGHNY`, `KXHIGHCHI` appear in the non-standard table**, so every family
this bot trades takes the default `M = 1`. The concern that "a flat 0.07 may
be materially wrong on the exact families we trade most" is resolved: for
the taker side it is exactly right.

`KXPGATOUR` is the one traded family that *is* listed — maker 1 / taker 1,
so standard on taker and **not** maker-exempt.

### 2. Maker treatment — **both claims half right**

Maker multiplier defaults to **0**, so standard markets carry no maker fee.
Where a maker multiplier applies the rate is `0.0175`, exactly 25% of taker —
except `KXMVE` at maker `M = 2`, i.e. half of taker. The "flat 0.25% during
major events" claim appears nowhere in the document.

### 3. $0.035 per-contract cap — **absent**

No cap of any kind is stated. Recorded as `per_contract_cap: None` with an
explicit note, and a test asserts nothing clamps at high volume.

---

## Defect found: the previous model understates fees at small order sizes

This is the finding the branch exists to surface, and it needs care because
**an earlier scratch comparison I ran was wrong** and pointed the opposite
way. Correcting it: `core/pricing.py::fee_cents_per_contract` ceilings to a
**centicent**, not a whole cent, which makes it far more accurate than a
naive per-contract model. At C = 100 it reproduces the published table
exactly at every price tested.

The divergence is at small `C`, and it runs in the unsafe direction:

| Price | C | Previous model | Published | Direction |
|---|---|---|---|---|
| $0.01 | 1 | $0.0007 | **$0.01** | understates 14x |
| $0.10 | 1 | $0.0063 | **$0.01** | understates |
| $0.25 | 1 | $0.0132 | **$0.02** | understates |
| $0.50 | 1 | $0.0175 | **$0.02** | understates |
| $0.50 | 10 | $0.175 | **$0.18** | understates |
| $0.50 | 100 | $1.75 | $1.75 | exact |

Cause: `C` sits **inside** the published round-up, so the schedule charges a
minimum of one cent on any fee-bearing order. A per-contract figure
pro-rates below that floor.

The existing docstring states its own intent — "rounded up because
underestimating a cost that is subtracted from edge is the direction that
puts on trades which do not clear their own fees." It rounds up per
contract to a centicent, which does not reach the published per-order
minimum, so at small sizes it understates in precisely the direction it set
out to avoid.

Absolute magnitude is sub-cent per order, so this is not a large money
error. It matters because it sits inside the edge calculation and is
largest at the wings — the longshot prices where `MIN_EDGE_THRESHOLD` and
`LONGSHOT_EDGE_MULTIPLIER` already do delicate work.

### Effect on historical modelled P&L

Per the brief: **reported, not silently restated.**

Direction: modelled costs have been slightly too low, so counterfactual
P&L in the ledger is slightly too **optimistic** — every `paper` and
`refused` row's economics included a fee marginally smaller than Kalshi
would have charged.

Magnitude: bounded by roughly $0.01 per order, and zero for orders of ~100
contracts or more. I have **not** recomputed the affected rows. The
production ledger lives on the Railway volume and is not reachable from this
session, and restating past numbers is explicitly out of scope. **Flagged
for the Sep 4 arbitrage review**, where it matters most: an arbitrage margin
thinner than a one-cent-per-order fee floor is not an edge, and the floor is
proportionally largest exactly where arb candidates cluster.

---

## Data migrations and rollback

**Migration:** none. No schema change, no column, no stored data.

**Rollback:** delete the branch. Nothing imports `core/fee_schedule.py`, so
removal is inert.

**Forward:** wiring the module into `core/pricing.py` is the follow-up. It
would raise modelled costs slightly, shrinking computed edges and making the
bot marginally *more* conservative — the safe direction, but still a
behavioural change to every pricing decision, and therefore its own branch
and its own review.

---

## Not done deliberately

- **Not wired into `core/pricing.py`.** The brief forbids changing
  thresholds and gates; swapping the live fee model changes every edge
  computation. Built and verified so the switch is small when reviewed.
- **`CONFIG.risk.fee_rate` left in place.** Still `0.07`, still used by the
  live path. Removing it is part of the wiring change.
- **Historical P&L not restated**, per the brief.
- **The `llm_disabled` counter split** (ambiguity #2 from the previous
  review) is not touched. It is a behavioural change to the funnel and gets
  its own branch, as instructed.
- **The 15 pre-existing failures** not investigated.

---

## New unresolved ambiguities

1. **Prose vs table on rounding granularity.** The document defines round up
   as "rounds up such that the fee + positionCost is rounded to a
   **centicent**", but its own table is reproduced exactly (42/42) by
   ceiling the aggregate to a whole **cent**, and several published values
   exceed a centicent ceiling — one contract at $0.50 has a raw fee of
   $0.0175, already an exact centicent, and the table charges $0.02. The
   table is implemented. If the prose is the operative rule and the table is
   illustrative rounding, fees at small sizes would be lower than this
   module states. Worth a direct question to Kalshi support.

2. **`KXMVE` multiplier unreadable.** The row extracts as
   `Combos (excluding uncorrelated NFL combos) 12` — maker=1/taker=2, or a
   single multiplier of 12? Returns `Unavailable`. Relevant because
   `KXMVECROSSCATEGORY` shards appear in production scout logs, though the
   category filter currently excludes them.

3. **Perpetual futures are unmodelled.** Tiered on 30-day trailing volume,
   which the ledger does not carry. Returns `Unavailable`. Not currently
   traded.

4. **Whether `positionCost` interacts with the fee at all.** The prose
   phrase "the fee + positionCost is rounded" hints the rounding may be
   applied to a combined quantity rather than the fee alone. The table gives
   no way to distinguish, since it publishes fees in isolation.

---

## Independent verification — 2026-08-27, after the fact

A separate session transcribed the same PDF without access to
`core/fee_schedule.py`, `tests/test_fee_schedule.py`, `docs/FEE_SCHEDULE.md`
or this review. Its work is on `claude/fixture-findings-docs-0tk1or`
(`f90878c` fixture and findings, committed *before* it looked at this
branch; `3bb522c` the diff afterwards). It was commissioned because the
transcription pass had produced a wrong scratch comparison and corrected it
within the same pass — self-checked work underpinning the Sep 4 review.

**Result: one confirmation, one defect found, two rows resolved.**

### Confirmed — the 42 published values

Exact match. No discrepancy.

### Confirmed — cent-not-centicent rounding

The independent session reached the same conflict from the prose alone,
having never seen `provenance()["rounding_note"]`. This is the finding the
Sep 4 arbitrage review rests on, and it now has two derivations. No code
change. On its suggestion, `docs/FEE_SCHEDULE.md` now shows the arithmetic
that disproves the prose, rather than only asserting the conflict — a reader
who sees "centicent" has no reason to doubt it until they watch it fail.

### Defect — `KXMVE` was wrongly marked `Unavailable`

**The independent read was right and this branch was wrong.**

It reported the row as legible in both its extraction passes:
`KXMVE | Combos (excluding uncorrelated NFL combos) | 2 | 1`. This branch
had asserted the row "did not extract with two legible columns".

Settled by rendering page 8 at 170dpi and reading it. The row prints
`2   1`, plainly, and the positioned glyphs confirm it: `2` at x=341
(Maker), `1` at x=394 (Taker). Of the three hypotheses the handoff offered,
the second was correct — the `Unavailable` came from a mangled extraction
that was never checked against the page.

Fixed: `KXMVE` is now `SeriesFees(maker=2, taker=1)`. It is the only row in
the table where maker and taker differ, and the only multiplier above 1. A
maker multiplier of 2 makes its maker rate `2 x 0.0175 = 0.035`, half the
taker rate rather than the usual quarter.

Noted in passing: `0.035` is also the figure third-party write-ups assert as
a "per-contract cap". The folklore may be a garbled reading of this
multiplier. That is a guess about provenance, not a finding, and no cap
exists in the document either way.

### Resolved — `KXMLBNL` and `KXNASDAQ100Y`

Both sit either side of `KXMVE` on page 8. The independent session's two
extraction passes disagreed with *each other* about which was missing a
taker value — and so, on re-inspection, did this branch's. That is a genuine
artifact of how the page's text layer flattens rows with wrapped
descriptions, reproduced independently.

The rendered page shows **both as `1 | 1`**, which is what this branch had.
But the independent session was right that the value could not be trusted:
it was pattern-matched from surrounding rows, not read. Both are now pinned
individually in tests so neither can drift back to an unverified default.

### What this changes about the branch's own claims

The review above states that a series is marked `Unavailable` only when "the
document is genuinely ambiguous". For `KXMVE` that was not true — the
document was clear and the tooling was not. `AMBIGUOUS_SERIES` is now empty;
perpetual futures remain the only `Unavailable` case, on the sound grounds
that their tier depends on data the ledger does not carry.

The general lesson is recorded in both the module and
`docs/FEE_SCHEDULE.md`: refusing to guess is only a virtue when the refusal
is itself checked. An extraction artifact was promoted to a finding without
anyone looking at the page, and it took a reader who had not seen the answer
to catch it.

### Tests after the correction

```
$ python -m pytest -q
15 failed, 1049 passed

$ python -m ruff check .
All checks passed!
```

Fee-schedule tests: 50 -> 53. Three added (`KXMVE` asymmetry, the empty
`AMBIGUOUS_SERIES`, and the two visually-verified rows); one replaced (the
test asserting `KXMVE` was illegible); one repointed (the arithmetic-refusal
test now sources its `Unavailable` from perpetual futures, since `KXMVE` no
longer produces one). Same 15 pre-existing failures, unchanged in identity.
No existing test outside this file was modified.

**Awaiting human review.**
