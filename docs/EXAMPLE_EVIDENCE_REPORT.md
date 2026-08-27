<!--
Generated, do not hand-edit. Reproduce with:

    python -m scripts.make_evidence_fixture --out ./evidence-fixture.db
    python -m scripts.production_readiness_report --db ./evidence-fixture.db \
        --as-of 2026-08-23T00:05:00Z --include-refused \
        --out docs/EXAMPLE_EVIDENCE_REPORT.md
    rm ./evidence-fixture.db

Every number below comes from synthetic data built by the fixture script.
No production ledger rows and no credentials are involved. The fixture is
time-anchored, so regenerating produces an identical file.
-->

# DÆMON-KALSHI — Production Readiness and Evidence Report

- **As of:** 2026-08-23T00:05:00+00:00
- **Data cutoff:** 2026-08-23T00:05:00+00:00
- **Database:** `./evidence-fixture.db` (opened read-only)
- **Window:** unbounded → unbounded
- **DRY_RUN:** yes
- **Order strategy:** taker
- **Provider routing observed:** maker:moonshot/kimi-k2.6×3, checker:gemini/gemini-3.5-flash-lite×2, checker:anthropic/claude-haiku-4-5×1, maker:gemini/gemini-3.5-flash-lite×1

> This report never contacts Kalshi and never writes to the database. It distinguishes **measurement activity** from **trade activity**; the three modes below are never summed.

## Readiness: `BLOCKED`

There is deliberately no `LIVE_READY` state. The best available outcome is `MEASUREMENT_READY`, which means the evidence is sound enough to support a separately reviewed decision — not that the decision has been made.

| Requirement | State | Why |
|---|---|---|
| DRY_RUN state detected | `NOT_APPLICABLE` | DRY_RUN is true. No live P&L claim is possible from this data: orders were simulated and no capital was at risk. Every economic figure in this report is counterfactual. |
| Reconciliation health | `MEASUREMENT_READY` | Snapshot is 300s old with no unknown order states. |
| Ledger data integrity | `MEASUREMENT_READY` | Modes are cleanly separable; no duplicate fill or settlement identities found. |
| Fill-verified sample | `INSUFFICIENT_EVIDENCE` | 18 fill-verified contracts against an explicit minimum of 30. No fee-net performance conclusion may rest on this. The minimum is a report parameter, stated so a reviewer can disagree with it rather than have it applied invisibly. |
| Actual costs measured | `MEASUREMENT_READY` | Actual fees and realized slippage are both available for live rows. |
| Per-strategy stability | `MEASUREMENT_READY` | No material divergence between category/source cells. |
| Provider and coverage reliability | `BLOCKED` | 2 provider billing failure(s) recorded. A checker that abstains because a vendor is unpaid is non-functional, not conservative, and its abstentions are not judgements. |
| Risk safety state | `MEASUREMENT_READY` | Kill switch not tripped; no unknown order states. |

## Live execution (fill- and settlement-confirmed only)

Every figure in this section comes from `orders`, `fills` and `settlements` with `dry_run = 0`. Nothing counterfactual can reach it. If the bot has never traded, every line reads N/A — which is the correct description, not a measurement failure.

| Metric | Value |
|---|---|
| Orders submitted | 4 |
| Orders fully filled | 1 |
| Orders partially filled | 1 |
| Orders cancelled | 1 |
| Orders rejected | 1 |
| Orders expired | 0 |
| Orders in unknown state | 0 |
| Contracts requested | 50 |
| Contracts filled | 18 |
| Fill rate | 36.0% |
| Partial-fill rate | 25.0% |
| Gross realized P&L | $1.26 |
| Actual fees | $0.54 |
| **Net realized P&L** | **$0.72** |
| Turnover | $8.74 |
| Avg slippage vs decision price | 0.75¢ |
| Return on deployed capital | 8.2% |
| Max drawdown | $-4.44 |
| Avg holding time | 11s |
| Max holding time | 11s |
| Open marked exposure | N/A (open exposure is exchange state, not ledger state; this report is read-only and never contacts Kalshi) |
| Open unmarked exposure | N/A (open exposure is exchange state, not ledger state; this report is read-only and never contacts Kalshi) |

## Paper (counterfactual)

**These are not results.** The economics below describe trades that did not happen.

| Metric | Value |
|---|---|
| Rows | 6 |
| Settled rows | 6 |
| Modelled P&L (counterfactual) | $0.57 |
| Avg decision-time price | 44.00¢ |
| Sizing assumption | decision-time requested size; no partial-fill or queue model applied |

Why this is not performance:
- Approved but not executed on the exchange, or executed with zero fill.
- PnL is computed from the decision-time quote, not from a fill.
- Modelled fees are an estimate; FEE_RATE is documented as unverified against a published Kalshi schedule (see docs/SAFETY.md).
- No slippage, queue position or adverse selection is represented.

| Category | Rows | Settled | Counterfactual P&L |
|---|---|---|---|
| Crypto | 6 | 6 | $0.57 |

## Refused (counterfactual)

**These are not results.** The economics below describe trades that did not happen.

| Metric | Value |
|---|---|
| Rows | 12 |
| Settled rows | 8 |
| Modelled P&L (counterfactual) | $0.44 |
| Avg decision-time price | 30.00¢ |
| Sizing assumption | decision-time requested size; no partial-fill or queue model applied |

Why this is not performance:
- The Checker or a risk gate declined this trade. It never existed.
- PnL is what the trade would have returned had every gate passed and the decision-time quote been available in full size.
- Useful only for the question 'are the gates refusing winners'. It is not performance, and must never be summed with live results.
- Modelled fees are an estimate; FEE_RATE is documented as unverified.
- During the 2026-08 window every refusal was upstream zero-balance rejection, so these rows measure the model, not the gates.

| Category | Rows | Settled | Counterfactual P&L |
|---|---|---|---|
| Weather | 8 | 8 | $0.44 |
| Finance | 4 | 0 | N/A (no settled rows in this category) |

## Calibration (reported separately from P&L)

A Brier score measures whether stated probabilities track observed frequencies. It says nothing about whether acting on them earns money after fees, and it is **not** used as an approval criterion anywhere in this report.

| Category | Source | Mode | n | Brier | Said | Observed | P&L |
|---|---|---|---|---|---|---|---|
| Weather | llm | `refused` | 8 | 0.258 | 39.0% | 37.5% | $0.44 |
| Crypto | quant | `paper` | 6 | 0.233 | 52.5% | 50.0% | $0.57 |
| Crypto | quant | `live` | 2 | 0.187 | 55.0% | 50.0% | $1.26 |

**Weather/llm/refused**
- n=8 is below the 100-row minimum for this report; treat the Brier score as indicative only
- mode=refused: PnL here is counterfactual, not realized

**Crypto/quant/paper**
- n=6 is below the 100-row minimum for this report; treat the Brier score as indicative only
- mode=paper: PnL here is counterfactual, not realized

**Crypto/quant/live**
- n=2 is below the 100-row minimum for this report; treat the Brier score as indicative only

### Reliability tables

**Weather/llm/refused** (n=8)

| Stated bucket | n | Avg stated | Observed |
|---|---|---|---|
| 20%-30% | 2 | 27.0% | 50.0% |
| 30%-40% | 2 | 35.0% | 50.0% |
| 40%-50% | 3 | 45.0% | 33.3% |
| 50%-60% | 1 | 53.0% | 0.0% |

**Crypto/quant/paper** (n=6)

| Stated bucket | n | Avg stated | Observed |
|---|---|---|---|
| 40%-50% | 2 | 42.5% | 50.0% |
| 50%-60% | 2 | 52.5% | 50.0% |
| 60%-70% | 2 | 62.5% | 50.0% |

**Crypto/quant/live** (n=2)

| Stated bucket | n | Avg stated | Observed |
|---|---|---|---|
| 40%-50% | 1 | 48.0% | 0.0% |
| 60%-70% | 1 | 62.0% | 100.0% |

## Observed blockers

Each entry names a condition and the lookalike it must not be confused with. Two conditions that produce identical inaction and have different remedies are the most expensive kind of ambiguity in this system.

- **`zero_exchange_balance`** — Reconciled balance is 0.00 USD. Risk refuses upstream of every gate, so no gate judgement is being exercised at all.
  - *Not to be confused with:* risk-gate rejection on a populated account, which would be a real judgement
- **`dry_run_simulation`** — Orders were simulated. Absence of fills is a configuration consequence, not an execution failure.
  - *Not to be confused with:* a live execution failure, which would appear as rejected/unknown order states
- **`provider_degradation`** — billing failures=2, rate-limited=1, truncated rejects recovered=2, truncated approvals abstained=1.
  - *Not to be confused with:* a single generic 'LLM unavailable' count — these four have four different remedies (money, waiting, token cap, token cap)
- **`coverage_limit`** — scan-cap reached on 1 pass(es); 0 pass(es) genuinely found no candidates.
  - *Not to be confused with:* each other — a cap means markets were never examined, no-candidates means they were examined and rejected
- **`pricing_coverage`** — 4 declined for missing contract spec; 12 declined on market quality.
  - *Not to be confused with:* each other — a missing spec is a capability gap fixed by adding a spec; market quality is a per-instance rejection of a priceable family
- **`ladder_dedup_activity`** — Zero dedup actions. The cap is deployed and instrumented but has never bound: no (event_ticker, direction) group exceeded it. The mechanism is unexercised in production, so its projected saving remains a projection.
  - *Not to be confused with:* the dedup being disabled or absent

## Prerequisites for a future maker branch

Listed for planning only. Maker mode is **not** implemented, not enabled, and not approved. Each item below would need to be true, and separately reviewed, before it could be considered.

1. Verified order lifecycle: TTL expiry, market-close cancellation and reprice-after-confirmed-cancel, running in production and observed.
2. Exchange-state reconciliation for resting orders, including recovery of unknown states without operator intervention.
3. A quote generator. The quant path currently produces a fair value, not a two-sided quote, so there is nothing to rest.
4. A GTC path in build_intent. Only IOC taker intents are built today and expires_at is never set, so the TTL sweep has nothing to act on.
5. Inventory-aware sizing. Risk sizes for a taker fill; a resting quote must skew as the position builds.
6. Fill and adverse-selection measurement: what fraction of resting fills occur immediately before an unfavourable move.
7. Cancel/replace latency measurement, with a timeout policy for cancels that do not confirm.
8. Per-order exposure reservation, already present, re-verified under resting orders rather than IOC.
9. A verified Kalshi fee schedule. FEE_RATE=0.07 is documented as unverified; a maker earns the spread, so fee uncertainty is a far larger share of maker margin than of a wide taker edge.

