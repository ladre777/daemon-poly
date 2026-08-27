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
| `core/fee_schedule.py` *(new, 330 lines)* | Transcribed schedule: rates, per-series multipliers, rounding, provenance, `Unavailable` sentinel | **None.** Nothing imports it yet — it is not wired into `core/pricing.py` or any decision path. Pure data plus a pure function. | 50 tests, incl. all 42 published table values | **Yes.** Wiring it into the pricing path is a separate, reviewed change — see "Not done deliberately". |
| `tests/test_fee_schedule.py` *(new, 250 lines)* | Boundary, rounding, symmetry, multiplier, refusal and provenance coverage | None | — | No |
| `docs/FEE_SCHEDULE.md` *(new)* | Data definitions, the three settled disputes, the before/after divergence table, the open rounding question | None | — | No |

**Net: 3 new files. No file modified. No file deleted. No existing test
changed.**

---

## Test results

```
$ python -m pytest -q
15 failed, 1046 passed in 20.79s

$ python -m ruff check .
All checks passed!
```

| | Baseline (`de9f467`) | This branch | Delta |
|---|---|---|---|
| Passed | 996 | 1046 | **+50** |
| Failed | 15 | 15 | **0** |

The 15 are the documented pre-existing set, unchanged in count and identity:
8 × `test_checker_budget`, 4 × `test_funnel_accounting`,
2 × `test_signal_alert_dedupe`, 1 × `test_vol_smoothing_bias`.
Not investigated — out of scope per the brief.

---

## The three disputed points, settled

### 1. Category-varying multiplier / crypto premium — **claim is false**

The base rate is `0.07` for every event contract market. What varies is a
per-series multiplier `M`, published as `0` or `1`. Crypto is not charged
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
Where a maker multiplier applies the rate is `0.0175`, exactly 25% of taker.
The "flat 0.25% during major events" claim appears nowhere in the document.

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

**Awaiting human review.**
