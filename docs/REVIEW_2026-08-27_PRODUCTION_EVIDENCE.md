# Review — Production Evidence and Readiness

**Date:** 2026-08-27
**Branch:** `feat/production-evidence-readiness`
**Base:** `de9f467fe6878cbab0de48fb45a3725595630e94` (verified against the checkout)

**Nothing was merged. Nothing was deployed. No Railway variable, `DRY_RUN`
setting, risk threshold, exposure cap, order strategy or provider credential
was changed. No order was submitted or cancelled. No Kalshi account state was
touched, and no live test was run.**

---

## Diff summary

| File | Reason | Safety impact | Test coverage | Config/deploy action later? |
|---|---|---|---|---|
| `core/reasons.py` *(new, 300 lines)* | Stable `Stage`/`Reason`/`Mode`/`Readiness` enums, plus the `stats` → taxonomy translation table | **None** — pure constants and a mapping function. No decision reads it. | `test_reason_values_are_unique`, `test_stage_order_covers_every_stage`, `test_billing_throttling_and_faults_are_three_different_things`, `test_there_is_no_live_ready_state`, `test_worst_is_conservative` | No |
| `memory/telemetry_store.py` *(new, 380 lines)* | Persists pass/stage/provider/decision telemetry the funnel log line discarded | **None by construction** — every method swallows its own exceptions; the constructor disables the store rather than raising; callers tolerate `None` ids | `test_telemetry_never_raises_into_the_trading_path`, `test_an_unopenable_database_disables_telemetry_instead_of_crashing`, `test_quote_ages_are_derived_at_each_stage`, `test_provider_calls_record_outcome_classes_separately`, `test_unknown_update_fields_are_refused_not_written` | No — new tables are created additively on first run |
| `reporting/evidence.py` *(new, 470 lines)* | Mode-separated aggregation; the `Unavailable` sentinel | **None** — read-only, no client | 14 tests incl. `test_dry_run_orders_never_enter_live_totals`, `test_missing_fees_block_the_net_rather_than_defaulting_to_zero`, `test_unavailable_refuses_to_be_arithmetic` | No |
| `reporting/readiness.py` *(new, 400 lines)* | Eight-requirement readiness assessment and blocker disambiguation | **None** — reporting only; observes the kill switch, never resets it | 15 tests incl. `test_a_tripped_kill_switch_blocks_and_is_not_reset`, `test_brier_is_never_used_as_an_approval_criterion` | No |
| `reporting/render.py` *(new, 250 lines)* | Markdown + JSON from one payload | **None** | `test_unavailable_renders_with_its_reason_never_as_zero`, `test_markdown_never_emits_model_prose` | No |
| `scripts/production_readiness_report.py` *(new, 215 lines)* | Read-only CLI | **None** — `file:...?mode=ro`; a test poisons the socket layer | `test_generating_a_report_leaves_the_database_byte_identical`, `test_the_report_never_constructs_a_network_client`, `test_cli_exits_two_on_a_missing_database` | No |
| `scripts/make_evidence_fixture.py` *(new, 200 lines)* | Deterministic synthetic ledger — no production data, no credentials | **None** | Used by 30+ tests | No |
| `main.py` *(modified, +30/−4)* | Optional `telemetry=` parameter; persists the same funnel numbers already logged | **Low.** Additive and guarded. No decision, threshold, gate or ordering changed. Telemetry is constructed once in `main()` and passed through; omitting it is a no-op. | Full suite regression; `run_once` signature test | No |
| `docs/PRODUCTION_READINESS_EVIDENCE.md` *(new)* | Metric definitions, mode taxonomy, limitations, maker prerequisites | None | — | No |
| `docs/EXAMPLE_EVIDENCE_REPORT.md` *(new, generated)* | Worked example from the synthetic fixture | None | Regenerable and diffable | No |

**Net: 8 new files, 1 modified. No file was deleted. No existing test was
changed.**

---

## Test results

```
$ python -m pytest -q
15 failed, 1058 passed in 23.40s

$ python -m ruff check .
All checks passed!
```

| | Baseline (`de9f467`) | This branch | Delta |
|---|---|---|---|
| Passed | 996 | 1058 | **+62** |
| Failed | 15 | 15 | **0** |

The 15 failures are the documented pre-existing set, unchanged in both count
and identity:

- 8 × `test_checker_budget` (deferred by the operator)
- 4 × `test_funnel_accounting`
- 2 × `test_signal_alert_dedupe`
- 1 × `test_vol_smoothing_bias`

**Zero new failures. No existing test was modified**, so the regression
requirement is satisfied without any "objectively incorrect test" exception
being invoked.

---

## Two defects found and fixed during development

Both were in the new code, and both were the exact class of error this work
exists to prevent — which is a useful signal about how easy that error is to
make.

1. **A category with nothing settled reported `$0.00` counterfactual P&L.**
   The per-category accumulator started at `0.0` and was simply never added
   to, so "has not resolved yet" rendered as "broke even". Now
   `N/A (no settled rows in this category)`. Guarded by
   `test_a_category_with_nothing_settled_has_no_pnl`.

2. **Provider billing failures were counted as zero.** The readiness
   assessment read reasons only from `stage_events`, but a billing failure
   normally appears only in `provider_calls.outcome` — the call failed, so
   the candidate never reached a recordable stage. The report therefore said
   "0 billing failures" on a database containing several, reproducing the
   original reason-collapse bug *inside the tool built to detect it*. Now
   merged from both tables. Guarded by
   `test_a_billing_failure_blocks_operational_readiness`.

A third issue was a bad test premise, not a code defect: a test asserted that
a missing parent directory would disable telemetry, but `memory.db.connect`
creates parent directories deliberately. The test was corrected to use a path
SQLite genuinely cannot open.

---

## Data migrations and rollback

**Migration:** `TelemetryStore.__init__` runs `CREATE TABLE IF NOT EXISTS`
for four new tables (`pass_telemetry`, `stage_events`, `provider_calls`,
`decision_telemetry`). No existing table is altered, no column is added to an
existing table, and no row is rewritten.

**Rollback:** revert the branch. The four tables become orphaned but harmless
— nothing else reads them, and no existing query joins them. They can be
dropped at leisure or left in place. No data loss either way.

**Forward compatibility:** stage and reason values are stored as strings, so
a report built from an older or newer revision than the writer still renders
a human-legible value for a constant it does not recognise.

---

## Current observed blockers

From production logs on 2026-08-27, and reflected in the report's blocker
section:

1. **Kalshi bankroll is $0.00.** Risk refuses upstream of every gate, so no
   gate judgement is being exercised. This is *not* the same as a risk-gate
   rejection on a funded account, and the report now says so explicitly.
   Operator has indicated funding at 17:00 EDT on 2026-08-28.
2. **`DRY_RUN=true`.** No live P&L claim is possible from any data in this
   window. Readiness reports `NOT_APPLICABLE` for that requirement.
3. **Checker truncation is live.** Observed at 21:02:31 UTC on
   `KXHIGHNY-26AUG27-T80`, minutes after the LLM path recovered.
   `CHECKER_MAX_TOKENS=1200` was tuned for a different model. The recovery
   path is correctly fail-closed, but while the cap is too low some genuine
   approvals become abstentions for a mechanical reason. **Not changed —
   this is an operator decision, and the lever loosens no gate.**
4. **Zero fill-verified observations.** `INSUFFICIENT_EVIDENCE` on execution
   viability, and no fee-net conclusion is possible. This will not change
   while `DRY_RUN` is true.
5. **Ladder dedup has never bound.** `ladder_deduped = 0` in every observed
   pass, including one with `proposed=11`. Deployed and instrumented but
   unexercised; the projected saving remains a projection.
6. **Scan cap reached** at 400 pages (~82,000 markets) with catalog
   remaining — distinct from "no suitable candidates found".
7. **Four families lack verified contract specs** (`KXETHD`, `KXHIGHCHI`,
   `KXHIGHNY`, `KXWTI`) and are correctly declined.

---

## Unresolved ambiguities

1. **`FEE_RATE = 0.07` remains unverified** against a published Kalshi
   schedule. Every modelled cost inherits that uncertainty. Obtaining the
   real schedule is the single highest-value thing available to the Sep 4
   arbitrage review, and it matters more for any future maker work than for
   taker.
2. **`llm_disabled` maps to `provider_unconfigured`** in the stats
   translation table, but the counter is also set after a rate-limit trips
   the breaker mid-pass. The dedicated `llm_rate_limited` counter captures
   the latter separately, so the two are distinguishable in aggregate — but
   the mapping is approximate at the margin. Resolving it properly means
   splitting the counter in `main.py`, which is a behavioural change to the
   funnel and out of scope here.
3. **Open exchange exposure is not reported.** It is exchange state, and this
   report never contacts Kalshi. Rendered `N/A` with that reason. A separate
   reconciliation-backed path could supply it if wanted.
4. **The 15 pre-existing failures were not investigated.** Out of scope, and
   CI status was deliberately not made a release gate in this task.

---

## What this branch does not do

- It does not merge `feat/order-lifecycle-resting-orders`, which remains
  unmerged and is the Sep 1 review item.
- It does not implement quoting, inventory-aware sizing, GTC submission,
  cancellation, repricing, or maker mode in any form.
- It does not open `assert_order_strategy_supported` or change
  `ORDER_STRATEGY`.
- It does not change any threshold, cap, or gate.
- It does not make a capital-deployment decision, and the readiness states
  deliberately exclude any value that could be read as one.

**Awaiting human review.**
