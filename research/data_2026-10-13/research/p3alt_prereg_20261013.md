# P3-alt final-minute-lock — preregistration
**Date:** 2026-10-13 (Tue) · **Author:** Sofiya (subagent, Wave 3 strategy B)
**Status:** preregistered BEFORE any run. Paper-only. Shadow-only. No engine/kill/risk
logic touched. `live_ok` stays false. No live order path anywhere.

## 0. Dependency gate (Claude v3 build order #6)
P3-alt exists only because P1 returned **Q1 = BRTI 60s average** (exact $0.000000
match, 491/491 boundaries, `p1_fingerprint_report_20261013.md`). Settlement rule
(P0): **Up iff final_avg ≥ strike**, where
`final_avg = mean of 60 per-second BRTI prints in [Te−60, Te)` and strike = the
venue's published window start value. **Do NOT use the venue 1s series as the
index** — P1 proved it is a different, nearby stream (median $4.20 off).

## 1. Lock-in math (pre-registered, frozen)
At second k of the final 60 (k prints observed: p₀…pₖ₋₁ at Te−60…Te−60+k−1):
- `locked_sum = Σᵢ₌₀..ₖ₋₁ pᵢ`, `R = 60 − k` prints remaining.
- Bound source: each remaining print lies within **±b dollars per second** of the
  previous print. b is NOT a guarantee; it is a calibrated parameter (see §2).
- Remaining print at position i ∈ 1..R is within ±i·b of the last observed
  print pₖ₋₁, so the sum of the R remaining prints is within `R·pₖ₋₁ ± b·R(R+1)/2`.
- **Lock interval:**
  - `lo = (locked_sum + R·pₖ₋₁ − b·R(R+1)/2) / 60`
  - `hi = (locked_sum + R·pₖ₋₁ + b·R(R+1)/2) / 60`
- **Lock-Up:** `lo ≥ strike` (strictly determined Up, since outcome = final_avg ≥ strike).
- **Lock-Down:** `hi < strike` (strictly determined Down).
- Otherwise (interval straddles strike): **no lock** — locked is NOT locked.
- A lock can never "flip" under the bound; if a later print violates the ±b
  expansion, that is a **bound violation**, handled per §5 (not a lock flip).

## 2. Bound b (pre-registered candidates + source)
Bound source: empirical per-second absolute moves of the **venue 1s series**
(`p1_brti_1s.jsonl`, n=605,843 seconds, 2026-10-06→10-13). That series is NOT
the settlement index — it is used ONLY to calibrate move scale. Measured:
median $0.22, p90 $0.83, p99 $2.00, p999 $4.02, max $13.99.
**Sensitivity set (all three reported): b ∈ {$2.00, $4.00, $8.00}.**
Rationale for over-estimation: BRTI is itself an average across constituent
exchange feeds, so its per-second prints are smoother than any single venue
stream; calibrating b on a single-stream proxy is conservative (wider interval
→ later/fewer triggers → fails toward silence, never toward false locks).
Interval half-width at R remaining = b·R(R+1)/120. For b=$4: R=10 → ±$3.67,
R=5 → ±$1.00, R=3 → ±$0.40.

## 3. Mispricing trigger (pre-registered)
Given a lock at second k with R ≥ 5 remaining (no entries in the last 5s):
- Lock-Up → buy the **Up** token at best ask `a_up`. Enter **iff**
  `(1 − a_up) − fee(a_up) − SLIP − MARGIN > 0`,
  where `fee(p) = theta · p · (1−p)` per share, theta read **live** from
  `research/fee_cache.json` at shadow-run start (currently 0.0695; if the
  cache is missing or >36h old, NO shadow entries — fail closed).
  `SLIP = 0.005` ($/share, pre-registered), `MARGIN = 0.03` ($/share, pre-registered).
- Lock-Down → buy the **Down** token at best ask `a_dn`. Enter **iff**
  `(1 − a_dn) − fee(a_dn) − SLIP − MARGIN > 0`.
- No lock ⇒ no trade. Lock + market already converged (condition fails) ⇒ no trade.
  **This is the expected failure mode** — taker fees exist to tax exactly this.

## 4. Sizing (pre-registered)
- One position per window, max **10 contracts**, fixed size, no adds, no averaging.
- Entry requires top-of-book ask size ≥ 10 contracts (no sweep assumption).
- Max one entry attempt per window. No exits before settlement except §5.

## 5. Quote-pull / stop conditions (pre-registered)
- **Quote-pull (no entry):** ask size < 10 at entry; R < 5; book stale (>10s since
  last book update); fee cache stale/missing; market price already converged.
- **Stop (post-entry):** if a later print violates the ±b bound such that the
  recomputed interval re-straddles strike → **bound-violation exit**: attempt to
  sell the position at the best bid immediately; record realized loss + reason.
- **Mark stop:** if the position's mid-value falls below $0.50/share → exit at bid.
- Everything else: hold to settlement (the lock thesis is binary).

## 6. Fill model for the shadow backtest (pre-registered)
- Entry modeled at best ask + SLIP ($0.005/share) haircut, ask size ≥ 10 required.
- Per Q6: in the live shadow, a fill counts only if a subsequent trade print
  crosses at or below our price within 5s; otherwise the signal is marked
  "unfilled" and contributes 0 (not a paper gain).

## 7. Acceptance criterion (EXACT, frozen)
**EV after live taker fee and realistic fill > 0 with 95% CI excluding 0,
over ≥300 windows.** Window-level bootstrap CI. EV computed per window as
(realized PnL incl. fees and slippage) − and the trigger must fire on a
reportable share of windows (§8b).

## 8. Falsification conditions (pre-registered)
- **F1 (no opportunity):** trigger fires on <5% of eligible windows over ≥300
  windows → strategy has no venue opportunity; verdict FAIL.
- **F2 (no edge):** post-fee, post-fill EV ≤ 0, or 95% CI includes 0, over ≥300
  windows → edge falsified; verdict FAIL.
- **F3 (bound miscalibrated):** bound violations on >1% of entered windows →
  b calibration falsified; widen b and re-run, do not trade the finding.
- **F4 (contested away):** lock fires but the mispricing condition never holds
  (market converges within the lock) → edge is real but unharvestable; verdict FAIL.

## 9. What "UNPROVEN" means here
A real backtest requires the per-second BRTI index path for historical windows.
That path does not exist in any stored data (see `p3alt_build_log_20261013.md`
§stored-data). Until the live feed exists, the verdict is UNPROVEN — not "promising",
not "likely works". The dependency chain is: `brti_ws_logger.py` stream-id →
live shadow loop → 300 windows → acceptance §7.

## 10. Synthetic validation acceptance (pre-registered, before the sim runs)
The engine `p3alt_locksim.py` must, on clearly-labeled synthetic paths:
- (a) fire a Lock-Up trigger on a constructed lock-Up path, at the expected R;
- (b) fire a Lock-Down trigger on a constructed lock-Down path;
- (c) stay silent on a constructed near-strike non-lock path (interval straddles);
- (d) flag a bound-violation loss correctly when a spike breaks the ±b expansion
      after a lock fired;
- (e) not trade when the market price has already converged (lock + no mispricing).
Any deviation = engine FAIL; fix the engine, not the prereg.
