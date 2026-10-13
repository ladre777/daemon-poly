# Claude review files — 2026-10-13

Verify first: `cd btc15m/review/claude_20261013 && sha256sum -c SHA256SUMS`
(every line must say OK; README.md is not in the list).

| File | What it is |
|---|---|
| `paper_reference_20261013.py` | sync-2026-10-13 `btc15m/paper.py` + `observe_reentry.diff` + `fixed_tau_brier.diff`, applied with `patch`. Reference for `diff -u paper_reference_20261013.py ../../paper.py`. |
| `observe_reentry.diff`, `fixed_tau_brier.diff` | The two approved engine diffs (apply in that order with `patch` / `git apply`; never by hand). |
| `risk_pin_update.diff` | Updates the 3 tests that pin RISK `e2af68a6…` → `6951f00a2acaaebe`. |
| `test_observe_reentry_20261013.py` | 9 tests (observation re-entry). |
| `test_fixed_tau_brier_20261013.py` | 8 tests (fixed-τ kill/gate + stamp). Expected full suite: 263 + 8 = 271. |
| `cfav_prereg_20261013.md` | C-fav preregistration, final. Its commit to the research tree sets R. |
| `cfav_check.py`, `test_cfav_check.py` | Independent C-fav checker + 9 synthetic tests. Do not read before writing `cfav_shadow.py`. |
| `amendments_p2_p3_20261013.md` | P2 and P3 amendments. |
| `AUDIT_PROTOCOL.md` | Five-element audit protocol. |
