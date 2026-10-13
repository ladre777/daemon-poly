# P2 pre-registration — "retire the directional signal" test (2026-10-13)

**Written BEFORE running the stacking test.** Paper-only, read-only on the
engine. This test RETIRES the Sleeve B directional fair-value signal to
shadow-logging only on failure; it never disables kills or sleeves —
B/C/D stay halted regardless (re-enabling needs LaDre's word).

## Hypotheses / outcomes
- **H1 (Brier):** Brier(model) − Brier(market) on CLEAN venue labels, 95%
  paired CI over venue-labeled windows. If the sign flips vs the old
  s_avg-label result (+0.0092, n=813; CI LB +0.0037 at n=882), STOP
  EVERYTHING and report immediately (expected: it won't).
- **H2 (stacking):** stacking coefficient b > 0 with 95% CI excluding 0 AND
  out-of-sample log-loss beating market-alone → KEEP; otherwise RETIRE
  the signal permanently (log it; delete nothing).

## Decision set
- Windows with `venue_outcome` ∈ {up, down} in `research/venue_truth.jsonl`
  (pt1 labels, per the P1 fingerprint: settlementPrice = CF Benchmarks BRTI
  60s average, exact match 491/491).
- Joined to `scored_windows.jsonl` rows on window start `T`.
- Rows where the model prob is unavailable are EXCLUDED, not proxied:
  63 scored rows carry `p_fair` exactly 0.0/1.0 with S=null, O=null,
  sigma_used=null (degenerate gapfill rows, no fair-value inputs) — 62 of
  them fall in the venue overlap. `mid_up` is present on all rows (0 None).
- Expected decision-set size: ~423 windows (485 overlap − 62 degenerate).
  Exact n reported in results.

## Decision tick
- One prediction per window at the ENGINE'S OWN decision tick: the scored
  row's `p_fair`/`mid_up` were recorded by the engine when it scored the
  window into `pending_brier` (written to disk at settle time; `scored_at`
  ≈ Te+52s is the WRITE time, not the decision time).
- Empirical offset: `tau_at_score` quartiles 132 / 578 / 723 s to expiry →
  decision tick t = window_start + 768 / 322 / 177 s (median T+322s,
  i.e. ~5.4 min into the window). Exact distribution printed by the script.
- Model prob = `p_fair` from the scored row (fairvalue fv1 model).
- Market prob = `mid_up` from the SAME scored row (book mid from the US
  product's own book, recorded at the same tick).
- `research/book_snapshots/` covers only 6 windows on 2026-10-13 — coverage
  insufficient → declared gap; the in-engine `mid_up` at the decision tick
  is the closest available market prob. No market prob is fabricated.

## Walk-forward design
- Decision set sorted chronologically, split into 5 equal blocks (B1..B5;
  n≈85 per block).
- Expanding window: for k=1..4, fit (a,b) on blocks 1..k, predict OOS on
  block k+1.
- Stacking model: p = σ(a·logit(market) + b·(logit(model) − logit(market))).
- Probs clipped to [1e-3, 1−1e-3] before logit (mid_up ∈ [0.015, 0.985]
  observed; p_fair extremes excluded above).
- Fit = MLE logistic regression on features x1=logit(mkt),
  x2=logit(mod)−logit(mkt) (Newton, pure numpy-free stdlib).

## Estimands
- b coefficient for the keep-if rule: from the POOLED fit on train blocks
  1–4 (the largest training set), 95% CI by window-level bootstrap
  (B=10,000 resamples of the train windows, refit per resample, 2.5/97.5
  percentiles). RNG seeded 20261013 (stdlib `random.Random`).
- OOS log-loss: pooled predictions on test blocks B2..B5 (k=1..4 fits),
  n≈340; stacked-model OOS log-loss vs market-alone OOS log-loss on the
  same windows.

## Keep-if rule (Claude's v3 spec, verbatim semantics)
KEEP iff ALL hold: (1) b > 0; (2) 95% CI of b excludes 0;
(3) OOS log-loss (stacked) < OOS log-loss (market-alone).
Otherwise RETIRE the signal permanently to shadow-logging only.

## Sensitivity run (reported, NOT decision-bearing)
- All `scored_windows.jsonl` rows with valid p_fair (excl. 0/1 degenerate)
  and mid_up, using the s_avg `outcome` proxy labels — the closest
  reconstruction of the old n=882 set (that file rotated; unavailable).
  ~425 proxy-labeled windows expected. Same 5-block procedure.

## Analysis code
- `research/p2_brier_clean.py` — H1 Brier re-run on clean labels.
- `research/p2_stacking.py` — 5-block walk-forward stacking + sensitivity.
- Both read-only on engine state; new files only under `research/`.

## Consequences table
| Outcome | Action |
|---|---|
| Brier sign flips vs +0.0092 | STOP EVERYTHING, report immediately |
| b ≤ 0 or CI includes 0 or stacked LL ≥ market LL | RETIRE directional signal (shadow-logging only); kills/sleeves untouched |
| All three keep conditions hold | KEEP signal in the evaluation pipeline |

Pre-registered 2026-10-13 ~08:40 UTC (04:40 EDT) by the P2 subagent, before
any stacking computation.
