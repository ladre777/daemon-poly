# C-fav: buy the late favorite (shadow-only) — preregistration v1

**Status:** DRAFT for commit. The registration time `R` is the UTC epoch of
the git commit that adds this file to the research repo. Nothing in §5–§9
may change after that commit; any change is a new version (v2) with a new `R`.

**Scope:** paper/shadow research only. No engine change, no sleeve change,
`live_ok=false`, no orders, no purchases. Sleeve C stays disabled; this test
does not use or touch it. Runs as a separate read-only script over recorder
files.

## 1. Why this test exists (discovery, NOT evidence)
On 524 already-scored windows (2026-10-08 → 2026-10-13), buying the market
favourite at its ask with τ ≤ 300 s and ask ≥ 0.85 won 151/151, with mean
net EV ≈ +$0.031–0.036 per $1 share after the taker-fee formula. That
pattern was found by slicing the data ~15 ways, from one snapshot per
window. **It is a hypothesis only.** None of those windows count below.

## 2. Hypothesis
H1: Buying the favourite side at its best ask, late in the window and only
when that ask is high, has positive expected net P&L after taker fees,
latency and depth limits, on windows never seen during discovery.

## 3. Data (forward only)
- **Windows:** every US-venue BTC 15m Up/Down window with window start
  `T ≥ R + 900` (the first full window after registration).
- **Book:** P3 recorder `book` records (≈3 s poll, 5 levels per side):
  `ts, T, state, offers_up, offers_down, bids_up, bids_down, empty_book,
  fee.taker_theta`. `poll_error`, `heartbeat`, `window_boundary` records
  are ignored except for the coverage rule in §8.
- **Outcome (settlement reference, by venue rule):** `Up` iff
  `round(pb(T+900), 2) ≥ round(pb(T), 2)`, where `pb(x)` is the published
  price-to-beat of the window starting at `x` from `captures.jsonl`
  (or the recorder's `strike_retry_ok`). Tie → Up. A window lacking either
  value is excluded from P&L and counted as `no_label`.
- τ = `T + 900 − ts` (seconds to window close).

## 4. Definitions
- `ask_up = offers_up[0][0]`, `ask_dn = offers_down[0][0]`; depth at the
  best ask = `offers_side[0][1]` (contracts).
- **Favourite** = the side whose best ask is ≥ 0.85. (At most one side can
  be; if neither is, there is no favourite.)
- `fee(p, C) = round_half_even(θ_taker × C × p × (1 − p), 2)` with θ_taker
  from the ENTRY record's `fee` block (the §5 step-2 record), computed in
  decimal arithmetic (Python `Decimal`, `ROUND_HALF_EVEN`), never binary
  floats. Never hardcoded.

## 5. Entry rule (frozen)
For each window, scan `book` records in time order:
1. **Trigger:** the first record with `state == MARKET_STATE_OPEN`,
   `empty_book == false`, τ ∈ [60, 300], a favourite exists with best ask
   `a ∈ [0.85, 0.97]`, and depth at that ask ≥ `q`.
2. **Latency check:** take the NEXT `book` record of the same window
   (≈3 s later; more conservative than the engine's 500 ms re-poll). Entry
   happens only if, in that record, the same side is still the favourite,
   its best ask `a2 ∈ [0.85, 0.97]`, depth at `a2` ≥ `q`, τ ≥ 60, and state
   OPEN. Otherwise log `faded` and **do not enter this window** (no second
   attempt).
3. **Fill:** `q` contracts at `a2` (top level only; no book walking).
4. At most **one entry per window.** No exits; hold to settlement.

## 6. Size and P&L (frozen)
- `q = 10` contracts per entry. Shadow bankroll `B0 = $500`, separate from
  Satoshi's paper bankroll; no shared state.
- Net P&L per trade = `q × 1[win] − q × a2 − fee(a2, q)`.
- Reported per trade and per share (`net / q`).

## 7. Acceptance (ALL must hold at the evaluation point)
Evaluation point: the first **300 entered trades** in chronological order
(by window start T), evaluated exactly once. The verdict is written once to
`research/cfav_verdict.json` and never recomputed; later runs print the
latched verdict only. Until then, the script prints **counts only**
(windows seen, triggers, entries, faded, no_label) — plus the futility
check in §9.
1. **Edge:** mean net P&L per share > 0, and the 95% block-bootstrap CI
   (chronological order, block length 8, 10,000 resamples, seed 20261013)
   has lower bound > 0.
2. **Tail margin:** the Wilson 95% upper bound of the loss rate is below the
   breakeven loss rate `ℓ* = mean over trades of (1 − a2 − fee(a2,q)/q)`.
3. **Stability:** mean net per share > 0 in both chronological halves
   (trades 1–150 and 151–300).
4. **Drawdown:** maximum peak-to-trough of cumulative net P&L ≤ $50
   (10% of B0).

**PASS** = all four. Anything else = **FAIL**. A pass makes C-fav a
candidate for a signed engine packet; it does not enable anything.

## 8. Coverage and exclusions (frozen)
- A window counts as **observed** only if it has ≥ 1 `book` record with
  τ ∈ [60, 300] and no recorder gap > 30 s inside τ ∈ [60, 300]. A gap is
  measured between consecutive `book` records of that window, AND from the
  band start (τ = 300, i.e. ts = T + 600) to the first record in the band,
  AND from the last record in the band to the band end (τ = 60). Any of
  these > 30 s fails coverage. Only `book` records count; `poll_error`
  records do not fill gaps.
- Windows failing coverage are excluded and counted (`coverage_fail`).
- Recorder `test_mode` must be false for production records; if the
  recorder still emits `test_mode: true`, that is reported, and the window
  still counts only if the book fields above are present.

## 9. Futility stop (pre-registered, checked after every trade from trade 50)
Stop early and report **FAIL (futility)** if the Wilson 95% **lower** bound
of the loss rate exceeds `ℓ*` (as in §7.2), both computed over the trades
so far. The futility verdict latches like §7. This is the only early look; it
can only stop the test, never pass it.

## 10. Reported, NOT decision-bearing
Splits by τ sub-band (60–150 / 151–300), by entry-price bucket
(0.85–0.90 / 0.90–0.95 / 0.95–0.97), by vol regime, by UTC hour; fade
rate; mean slippage `a2 − a`. Also a counterfactual at the engine's 500 ms
re-poll is NOT computed (no data at that resolution).

## 11. Code and verification
- `research/cfav_shadow.py`: read-only over recorder files and
  `captures.jsonl`; writes `research/cfav_trades.jsonl` and a summary.
- `research/cfav_check.py`: an independently written second script that
  recomputes entries, outcomes and P&L. The evaluation is valid only if
  both agree on every trade to the cent.
- Both scripts and this file are committed before `R`'s first eligible
  window closes.

## 12. Known limitations (accepted)
- 3 s snapshots: the true fill could be better or worse than `a2`.
- Top-of-book only; real size above `q` is untested.
- Fat-tailed loss risk: one loss at a2 ≈ 0.95 costs ≈ 30 wins.
- The P3 recorder's 404 burst at each window boundary (listing lag) does
  not affect τ ∈ [60, 300], but coverage is checked per §8.
