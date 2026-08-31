# NEXT_STEPS

Handoff for the next session. **Read this and `docs/PRODUCTION.md`.**

Last updated: **2026-08-30** (ledger-answers session).

---

## The headline that supersedes everything below

On 2026-08-30 the container read the ledger directly for the first time
(`scripts/ledger_answers.py`, PR #45) and every PnL figure was recomputed at the
**event** level rather than the row level. Rows are not independent: the same strike is
re-priced every pass and every strike on one contract resolves off a single settlement
price. Collapsed to the 975 events that actually resolve:

| Bucket | Rows | Events | Mean PnL | t (naive, rows) | **t (cluster-robust)** |
|---|---:|---:|---:|---:|---:|
| Crypto/quant | 13,708 | 951 | -$0.0426 | -13.42 | **-0.95** |
| Weather/llm | 4,663 | 18 | -$0.0368 | -9.49 | **-2.11** |
| Finance/llm | 1,616 | 6 | +$0.0536 | +7.79 | **-0.05** |

**Nothing in this bot has a demonstrated positive edge.** Across 19,988 settled rows the
only result clearing 2 sigma in either direction is Weather/llm **losing money**.

- **Weather/llm demonstrated loss t=-2.11; path frozen.**
- **Finance/llm frozen too, for a different reason: it cannot be measured.** Six settled
  events in the whole ledger, cluster t=-0.05. A promotion rule needs 15-30 events; at six
  in ten days on multi-day contracts that is most of a year away.
- Finance's apparent +$86.67 was **one contract**: `KXWTI-26AUG2514`, 136 rows (8.4% of
  Finance) supplying 63% of the PnL. Four of the six Finance events lost money and the
  event-weighted mean is **negative**.
- Finance splits hard by direction: buy-NO 473 rows **+$188.23**; buy-YES 1,143 rows
  **-$101.56 with zero wins** (upper 95% bound on its win rate 0.335%).
- `CHECKER_MIN_CONFIDENCE=0.65` is **not** a bottleneck — 11 under-confident approvals in
  4,905 KXWTI rows; confident rejects outnumber them 293:1. Do not touch it.

### Rules that follow from this

1. **A naive row-level t is not evidence and must never be a go-live criterion.** Any
   claim of edge is event-level (cluster-robust) or it is not a claim.
2. A sqrt-of-cluster-size adjustment is **not** an approximation to a cluster-robust
   standard error — it was tried, and it got Crypto and Weather backwards in opposite
   directions. Compute the real statistic from rows grouped by event.
3. No gate may be loosened to manufacture approvals.

### Frozen categories

`FROZEN_CATEGORIES` (default `weather`) blocks a category from ever reaching the risk
layer or execution. It is **sampled, not deleted**: `FROZEN_CATEGORY_SAMPLE_RATE`
(default 20) still prices it on one pass in twenty and writes the proposal as
`action_taken='skipped_frozen'`, so the counterfactual grading continues and a future
weather model can be seen flipping the sign. Removing a category from the set restores it
with no code change.

### The 42-cell search: nothing survives

Every (category, direction, price band) cell was scored cluster-robustly. **Not one cell
clears 2 sigma with a credible event count**, let alone the Bonferroni threshold of
|t|>=3.16 that 42 cells demand:

| Cell | Rows | Events | Mean PnL | t |
|---|---:|---:|---:|---:|
| Crypto NO 20-34c | 1,012 | 283 | +$0.0140 | **+1.59** |
| Crypto YES 35-49c | 215 | 109 | +$0.0703 | +1.17 |
| Crypto NO 10-19c | 1,375 | 228 | +$0.0302 | -0.66 |

Everything with a spectacular t has 2-4 events. Finance NO 50-64c reads **t=+22.69 on
three events** with 211/211 wins — three settlements that all went one way leave almost no
between-event variance, so the denominator collapses. The report now suppresses any t
computed on fewer than 10 clusters and prints `3ev<10` instead, because anyone scanning
for the largest t would otherwise land on the least evidence in the table.

**A band chosen from that table has already spent its evidence.** It needs new events, not
a re-read of the same ones.

### Open, and blocking any Finance work

The report gives direction and event separately but never crossed, so **it is not yet
known whether the +$188.23 NO book is a repeatable effect or one contract.** Finance has
six events total, so it may well be one. The direction x event x price-band decomposition
now ships in `ledger_answers.py`; read it before building anything on the short side.

Also unresolved: the Maker in production may be **kimi-k2.6**, while every probability
study in the four review documents was measured on **Gemini** output. If the model
changed, those samples describe a model that is no longer running and need a fresh window.

---

## Ground truth

| | |
|---|---|
| Fills | Still **none, ever**. `DRY_RUN=true`, balance $0.00. Treat the first fill as a systems test |
| Risk posture | Fail-closed. Do **not** lower Checker/coherence/edge gates to force trades |
| Durable storage | Requires Railway Volume at `/data` or memory resets every redeploy |
| ESPN | Blocked from Railway. Do not touch ESPN client |
| Golf | Slash Golf **wired** in `main.py` — needs `SLASH_GOLF_API_KEY` |
| Checker | Default **Moonshot/Kimi** (`CHECKER_LLM_PROVIDER=moonshot`) — stops Claude burn |
| Crypto quant | Verified families: KXBTC15M, KXBTC, KXBTCD, KXETH. Vol spike refusal on |
| Weather | **FROZEN** — demonstrated loss, cluster t=-2.11 over 18 events. NOAA ingestion still live; path sampled for grading only, never traded |

---

## Done on 2026-08-21

1. **Checker → Moonshot/Kimi** — configurable provider; Claude optional fallback
2. **Vol spike refusal** — quant path declines when short-window vol spikes vs baseline
3. **Weather error guidance** — context tells Maker realistic same-day NWS error bands
4. **Slash Golf wired** — `SlashGolfClient` passed into `ContextEnricher`
5. **`docs/PRODUCTION.md`** — competitor comparison + go-live checklist

---

## Operator checklist (now)

1. Confirm Railway Volume mounted at `/data`
2. Env:
   ```bash
   CHECKER_LLM_PROVIDER=moonshot
   CHECKER_MODEL=kimi-k2-turbo-preview
   SLASH_GOLF_API_KEY=...
   RTI_FEED_ENABLED=true
   PERSIST_VOL_HISTORY=true
   LEDGER_DB_PATH=/data/daemon_kalshi.db
   ```
3. Prefer paper until funnel shows approvals:
   ```bash
   DRY_RUN=true
   # KALSHI_ENV=demo recommended until machinery proven
   ```
4. After redeploy, confirm logs:
   - `Checker LLM: primary=moonshot:...`
   - `Slash Golf grounding enabled` (if key set)
   - Vol history restore if volume present

---

## Single next engineering priorities (profit-oriented)

Ordered by structural edge from Becker / open-source bot research:

1. **Order lifecycle → enable maker mode** — makers earn; takers lose on average
2. **Atomic YES+NO arb execution** — detection exists; both legs or neither
3. **Faster 15m crypto lag path** — spot/RTI move vs lagged Kalshi book
4. **Weather normal-CDF path** — NWS high + horizon-dependent σ (same shape as quant crypto)
5. **Calibration time-split views** — already partially supported via `CALIBRATION_REGIME_SPLIT_AT`

Do **not** start by lowering `CHECKER_MIN_CONFIDENCE` or `MIN_EDGE_THRESHOLD`.

---

## What not to do

- Do not widen coherence gates because nothing approved
- Do not enable maker mode without resting-order cancel + reconcile
- Do not trade gold/silver until unit mismatch is resolved
- Do not run live money without durable `/data`
- Do not trust X equity curves without settled PnL

---

## Standing constraints

- Golf is the only sports priority
- Fail-closed; small reviewable changes; no silent risk weakening
- Progress = real edge + real fills, not activity volume

See **`docs/PRODUCTION.md`** for full competitor comparison and go-live gates.
