# Production Readiness and Evidence Integrity

How this bot measures itself, what each number is allowed to mean, and where
the measurements stop.

The controlling principle: **a counterfactual number may never appear in a
line labelled P&L.** Everything below follows from that.

---

## 1. The mode taxonomy

Every ledger row is exactly one of three modes. They are never summed, never
averaged together, and never rendered in a shared total.

| Mode | Meaning | Is the P&L money? |
|---|---|---|
| `live` | Really traded on the exchange | **Yes.** From settlement rows only. |
| `paper` | Approved but simulated, or filled nothing | No. Decision-time quote. |
| `refused` | A Checker or risk gate declined it | No. The trade never existed. |

The mapping from `edges.action_taken` is written once, in
`core.reasons.mode_for_action`, and mirrors the SQL `CASE` in
`EdgeStore.calibration_by_category`:

- `executed` → `live`
- `dry_run`, `no_fill` → `paper`
- everything else, including `NULL` and unrecognised values → `refused`

The fallthrough direction is deliberate. An action the code does not
recognise is quarantined into `refused` rather than allowed to inflate a
live total. A new action state added later is therefore conservative by
default, not silently promoted.

### Why this is enforced rather than documented

On 2026-08-27 the production calibration summary read:

```
Crypto/quant: n=9,675  brier=0.124  said 44% actual 45%  pnl -$39.07
Weather/llm:  n=2,729  brier=0.205  said 29% actual 27%  pnl -$0.62
Finance/llm:  n=1,581  brier=0.085  said 25% actual  5%  pnl +$87.79
```

Every one of those rows is `refused`. The Kalshi balance was $0.00 and the
bot had filled nothing, ever. Summed naively that is "+$48.10 profit" — a
number describing no event in the real world, in a format that looks exactly
like a result. The `+$87.79` is the most quotable figure in the ledger and
the least real.

That is why the aggregate `refused` section is **opt-in behind
`--include-refused`**: nobody should be able to generate it by accident and
paste the total somewhere it will be read as performance.

---

## 2. Absence is reported, never defaulted

`reporting.evidence.Unavailable` is a value that does not exist, carrying the
precise reason why. It renders as `N/A (reason)` and **deliberately does not
support arithmetic** — any attempt to sum or average it raises `TypeError`,
so a manufactured figure is caught where it is manufactured rather than in
the report.

`0.0` is a claim about the world. `None` silently becomes `0` in most
arithmetic. A zero fee is the most attractive lie a trading report can tell,
so the rule is absolute:

- missing `fees_cents` on any settlement → net P&L is `N/A`, not gross
- missing `realized_pnl` on any settlement → gross and net are both `N/A`
- no deployed capital → return on capital is `N/A`, because 0/0 is not 0%
- a category with nothing settled → its P&L is `N/A`, not "broke even"
- a bot that has never traded → every live figure is `N/A`, and a note says
  this is because nothing was executed, not because a measurement failed

---

## 3. Reason taxonomy: no reason collapse

`core.reasons.Reason` gives every stopping condition exactly one spelling.
The test for whether a condition deserves its own constant is simple: **does
it have a different operator remedy?**

The failure this prevents is real. A production pass read
`llm_called=0 llm_disabled=109`, and those 109 covered four conditions with
four different fixes:

| Observed | Actual condition | Remedy |
|---|---|---|
| Moonshot 429 "insufficient balance" | `provider_billing` | Money, on Moonshot |
| Gemini 429 "retry in 11.1s" | `provider_rate_limited` | Wait |
| Anthropic 400 "credit balance too low" | `provider_billing` | Money, on Anthropic |
| No provider configured | `provider_unconfigured` | Configuration |

All four rendered as one number. Three of the four remedies are wrong for
any given case, and money spent on the wrong vendor is the cheapest bad
outcome available.

Reason counts are read from **both** `stage_events` and
`provider_calls.outcome`. A provider billing failure normally appears only
in the second: the call failed, so the candidate never reached a stage worth
recording. Reading one table reported zero billing failures on a database
that contained several.

### Pairs that must never pool

| This | Not this | Why it matters |
|---|---|---|
| `zero_balance` | `risk_refused` | An unfunded account refusing everything looks like a working gate. The gate was never consulted. |
| `scan_cap_reached` | `no_candidates` | A cap means markets were never examined. No-candidates means they were examined and rejected. |
| `no_contract_spec` | `market_quality` | A missing spec is a capability gap. Market quality is a per-instance rejection of a family we *can* price. |
| `provider_billing` | `provider_rate_limited` | Money versus waiting. |
| `truncated_reject_recovered` | `truncated_approval_abstained` | One refused a trade the model wanted to refuse. The other refused one it wanted to take. |
| `dry_run` | any execution failure | Simulation is a configuration consequence, not a fault. |

---

## 4. Truncation: the asymmetric rule

When a Checker response is cut off mid-write, `core/validation.py` recovers
whatever complete fields were written. What happens next is **asymmetric on
purpose**, and this work does not change it — only counts it:

- A recovered **`reject` is acted on.** It can only ever refuse a trade, so
  acting on it is safe, and it keeps a real judgement out of the calibration
  data as a parse error.
- A recovered **`approve` abstains**, with reason `truncated_approval`.
  *A cut-off approval is not an approval.* The caveat that would have changed
  the verdict is exactly the part most likely to be missing.
- Recovered rows are prefixed `(recovered from a truncated response)` in
  stored reasoning, so a ledger row cannot later be misread as complete.

The report counts the two dispositions separately. Pooling them would hide
the second, which is the one that costs opportunities rather than preventing
losses.

**Known live condition:** `CHECKER_MAX_TOKENS=1200` was tuned for
`claude-sonnet-5`. The Checker now runs a different model, and truncation was
observed in production on 2026-08-27 within minutes of the LLM path coming
back up. The cap is a budget lever, not a gate — raising it loosens nothing —
but while it is too low, some genuine approvals become abstentions for a
mechanical rather than a judgement reason, which biases the approval rate
downward. **Not changed here. Flagged for an operator decision.**

---

## 5. Readiness states

| State | Meaning |
|---|---|
| `NOT_APPLICABLE` | The requirement does not apply in this configuration. |
| `INSUFFICIENT_EVIDENCE` | Nothing disqualifying, but not enough to conclude. **The default.** |
| `BLOCKED` | Something actively disqualifying. |
| `MEASUREMENT_READY` | Enough sound evidence to support a separately reviewed decision. |

**There is deliberately no `LIVE_READY`.** The best available outcome is
`MEASUREMENT_READY` — "the instrumentation is sound enough that a human
decision could now rest on it". Naming a state that sounds like the decision
invites the report being mistaken for the decision. A test asserts the state
does not exist, so any future edit adding one has to argue for it.

Combination is conservative (`core.reasons.worst`): one `BLOCKED`
requirement blocks the overall result. An empty set of requirements is
`INSUFFICIENT_EVIDENCE`, not ready — having checked nothing is not the same
as having checked everything and found nothing wrong. All-`NOT_APPLICABLE`
collapses to `INSUFFICIENT_EVIDENCE` for the same reason.

### The eight requirements

1. **DRY_RUN detected.** If true → `NOT_APPLICABLE`, and the report states
   that no live P&L claim is possible.
2. **Reconciliation health.** Fresh snapshot, no unknown order states. A
   snapshot timestamped *after* the report's as-of time is `BLOCKED` as a
   broken measurement — a negative age would render as "very fresh".
3. **Data integrity.** Mode contamination and duplicate accounting
   identities. Checked from outside even though the schema constrains them,
   because a constraint added after data existed does not clean it.
4. **Execution viability.** Fill-verified observations against an **explicit,
   stated** minimum (default 30). The number appears in the output so a
   reviewer can disagree with it rather than have it applied invisibly.
5. **Cost and latency.** Actual fees *and* realized slippage must both exist
   for live rows. Modelled costs alone fail — `FEE_RATE` is documented as
   unverified, so a modelled fee is the assumption being tested, not evidence.
6. **Strategy stability.** Category/source cells diverging by ≥15% in
   observed event rate are flagged rather than pooled.
7. **Operational reliability.** Provider availability, throttle rate, billing
   failures, truncation counts, scan-cap incidence, spec exclusions — each
   counted apart. Any billing failure is `BLOCKED`: a Checker abstaining
   because a vendor is unpaid is non-functional, not conservative.
8. **Risk safety.** Kill switch, unknown orders, stale reconciliation.
   **Observed, never modified** — a tripped switch is reported tripped, not
   reset.

Calibration deliberately does **not** feed the verdict. A test asserts the
overall result is identical with and without calibration cells supplied.

---

## 6. Telemetry

`memory/telemetry_store.py` persists what the funnel log line previously
discarded. Two rules govern it:

**It cannot break a trading pass.** Every method swallows its own exceptions
and logs at warning. The constructor disables the store rather than raising
if the schema cannot be prepared. Callers must tolerate a `None` pass id —
that is the contract. An observer that can abort what it observes is a
liability, not an instrument.

**It cannot leak.** There is no column for prompts, model reasoning, checker
prose, credentials or environment values. Provider *identifiers* and outcome
*classifications* are stored; the text that flowed through them is not. This
is narrower than the existing ledger — `edges.maker_reasoning` already stores
prose and is untouched — but nothing here widens that surface.

Tables: `pass_telemetry` (timings, coverage, config in force),
`stage_events` (the funnel, decomposed by reason), `provider_calls` (latency
and outcome class per attempt), `decision_telemetry` (quote ages at each
decision point, requested-vs-filled, slippage).

Durations use `_ms()`, which returns `None` rather than a negative number
when the clock runs backwards. A negative duration is a broken measurement,
not a fast one, and returning it would quietly drag every average down.

---

## 7. Running the report

Take a copy of the database first. Never point this at the file a running
daemon is writing.

```bash
# operator-readable
python -m scripts.production_readiness_report --db ./ledger-copy.db

# machine-readable
python -m scripts.production_readiness_report --db ./ledger-copy.db --json

# bounded window, including the opt-in refused section
python -m scripts.production_readiness_report --db ./ledger-copy.db \
    --as-of 2026-08-27T21:00:00Z --since 2026-08-21T19:01:00Z --include-refused
```

Guarantees, each covered by a test:

- **Read-only.** Opened via `file:...?mode=ro`; SQLite itself refuses writes.
  A test hashes the database before and after generating a full report.
- **No network.** A test poisons `socket.socket` and `socket.create_connection`
  and generates a complete report over the top.
- **No prose.** A test asserts none of the redacted column names appear in
  either the Markdown or the JSON.

Exit codes: `0` success, `2` database unreadable.

A synthetic fixture — no production data, no credentials — is available for
exercising every branch:

```bash
python -m scripts.make_evidence_fixture --out /tmp/fixture.db
python -m scripts.production_readiness_report --db /tmp/fixture.db --include-refused
```

---

## 8. Known limitations

- **Open exchange exposure is not reported.** It is exchange state, not
  ledger state, and this report never contacts Kalshi. Rendered as `N/A` with
  that reason rather than estimated from local records.
- **`FEE_RATE = 0.07` is unverified** against a published Kalshi schedule
  (see `docs/SAFETY.md`). Every modelled cost inherits that uncertainty. An
  independent implementation uses the identical formula, which is
  corroboration but not verification — both may trace to the same summary.
- **The ladder dedup has never bound in production.** `ladder_deduped = 0`
  in every observed pass. The mechanism is deployed and instrumented, but its
  projected saving remains a projection from historical rows, not an observed
  result. The report says so explicitly rather than reporting a zero that
  could be read as "disabled".
- **Counterfactual sizing ignores queue position, partial fills and adverse
  selection.** It assumes the decision-time quote was available in full size.
- **Calibration cells below the stated minimum carry a warning**, and the
  minimum is a report parameter, not a gate.

---

## 9. Prerequisites for a future maker branch

Listed for planning only. **Maker mode is not implemented, not enabled, and
not approved.** `assert_order_strategy_supported` in `workers/execution.py`
remains the gate, and this work does not touch it.

1. Verified order lifecycle — TTL expiry, market-close cancellation,
   reprice-after-confirmed-cancel — running in production and observed.
2. Exchange-state reconciliation for resting orders, including recovery of
   unknown states without operator intervention.
3. A quote generator. The quant path produces a fair value, not a two-sided
   quote, so there is currently nothing to rest.
4. A GTC path in `build_intent`. Only IOC taker intents are built today and
   `expires_at` is never set, so the TTL sweep has nothing to act on.
5. Inventory-aware sizing. Risk sizes for a taker fill; a resting quote must
   skew as the position builds.
6. Fill and adverse-selection measurement.
7. Cancel/replace latency measurement, with a timeout policy for cancels
   that do not confirm.
8. Per-order exposure reservation — already present — re-verified under
   resting orders rather than IOC.
9. **A verified fee schedule.** A maker earns the spread, so fee uncertainty
   is a far larger share of maker margin than of a wide taker edge.

The honest prior question, which the numbers above do not yet answer: taker
mode has not been shown profitable on paper. Maker mode may be premature
regardless of whether the lifecycle work is complete.
