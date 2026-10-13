# Audit protocol — every number that can drive a decision (2026-10-13)

Applies to every brief, report, gate, kill readout and research result.

## The five elements (all required)
| # | Element | Example |
|---|---|---|
| 1 | Definition + sign convention | "model Brier − market Brier at τ=300 s; positive = model worse" |
| 2 | Source | file(s) + function/query, e.g. `paper_state.json` → `_brier_grid_diffs()` |
| 3 | n and time range | "n=227 windows, T 2026-10-08 04:30 → 2026-10-10 16:00 UTC" |
| 4 | Independent recompute | a second, separately written script; must match to the stated precision |
| 5 | Status | `VERIFIED` (both paths match) or `UNVERIFIED: <reason>` |

## Rules
- `UNVERIFIED` numbers are shown but never drive a decision (gates, kills,
  adoption, sizing, spend).
- A figure whose sign convention is ambiguous is `UNVERIFIED` by definition.
- A count that has not changed since the previous brief is flagged
  `STALLED` (this would have caught the shadow freeze on 2026-10-10).
- Every code change: exact diff, a test that fails before and passes after,
  a two-path recompute when numbers are involved, and Claude's review
  before it touches the engine. Nothing goes in on "probably fine".
- One source of truth: the repo that runs is the repo that is reviewed,
  `research/` included.
