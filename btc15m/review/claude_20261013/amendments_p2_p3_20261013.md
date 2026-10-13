# Amendments — P2 and P3 preregistrations (Claude review, 2026-10-13)

Both amendments are written BEFORE any decision-grade result is read. If
either test has already produced a decision-grade result, say so first; the
amendment then applies to a new version, and the old result is reported as
it was.

## P2 ("retire the directional signal") — amendment A1
**Problem:** the decision tick τ varies widely (quartiles 132 / 578 / 723 s),
and the mix shifted on 2026-10-10 when D was disabled (windows scored at
first tick, τ > 600). Chronological blocks therefore put mostly τ > 600
windows in the last test blocks. A RETIRE outcome would confound "the model
adds nothing" with "the test set is a different τ".
**Change:**
1. Primary analysis restricted to windows whose decision tick has
   τ ∈ [60, 600]. Same 5-block walk-forward, same keep-if rule.
2. The stacking model adds the interaction term: features
   `x1 = logit(mkt)`, `x2 = logit(mod) − logit(mkt)`,
   `x3 = x2 × 1[τ > 300]`. KEEP requires b (on x2) > 0 with CI excluding 0
   AND the OOS log-loss condition; x3 is reported, not decision-bearing.
3. Reported, not decision-bearing: the original all-τ analysis, and the
   τ > 600 stratum alone.
4. Labels: the venue settlement reference (next window's price-to-beat).

## P3 (maker-first) — amendment A1
1. **OutcomeProvider primary:** the next window's published price-to-beat
   vs the window's own (tie → Up), from `captures.jsonl` / recorder
   strike records. That is the settlement reference by the venue's rules.
   Kalshi `expiration_value` and venue `settlementPrice` become
   cross-checks only (report disagreements; never decision-bearing).
2. Record in the prereg (not a parameter change): the feed `I_t` is the
   venue 1 s series, measured ≈ 15 s behind spot and smoothed (lag check
   2026-10-09). v1 is frozen as is; a fresh-price v2 is a separate
   preregistration.
3. The pending amendment (Θ_maker = 0 for all windows; ≥ 100 post-Oct-19
   windows in the 300; 120 s pull; strict trade-through fills) must be
   committed before any replay result is read.
