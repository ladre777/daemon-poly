# NEXT_STEPS

Handoff for the next session. **Read this, not chat history** — chat contains
stale PR numbers and at least two claims I had to correct later.

Last updated: 2026-08-17, end of the prod-cutover session.

---

## Ground truth as of this file

Verify these rather than trusting them; they were true when written.

| | |
|---|---|
| `main` | `#24` merged (paper calibration). Check `git log --oneline -5`. |
| Tests | 813 passing locally, `ruff` clean. **CI was NOT read** — see below. |
| Railway | `env=prod`, `DRY_RUN=false` — **LIVE, REAL MONEY**, confirmed from the boot banner at 14:13. |
| Account | funded ~$49.98 |
| RTI feed | live on prod, ~200 frames/2min across BRTI + ETHUSD_RTI |
| ESPN | **blocked for bots. Do not touch the ESPN client.** |
| NOAA | wired, never yet executed against a live weather market |

Effective bankroll is `min(--bankroll, exchange balance)`, so sizing is capped
by the real $49.98, not the $1000 CLI default. At `MAX_POSITION_PCT=0.05`
that is ~$2.50/position — roughly 6 contracts at 40c.

### Deploy state

Live cutover completed at 14:13 on deployment `803656a8`, confirmed from the
banner rather than assumed:

```
DÆMON-KALSHI starting | env=prod dry_run=False strategy=taker
Startup state: $49.98 balance, 0 open position(s), 0 live order(s)
```

Getting there took four failed builds. They were Railway builder failures, not
code: a successful build logs every Docker step (`[4/8] RUN pip install`, ...,
`image push`), the failed ones log only `scheduling build on Metal builder`
and stop. `#24` was briefly suspected and cleared — the Dockerfile copies only
`main.py config.py core/ workers/ memory/`, which is exactly what it touched.

**If it fails again:** the API cannot fix it. `redeploy` refuses ("that
deployment has no build to copy"), and re-setting a variable to a value it
already holds triggers nothing. Redeploy from the Railway dashboard.

---

## What is DONE

**Item 1 — visibility and calibration.** Complete.

- Scout per-family census reports every disposition per ticker family, every
  pass, including zero counts (`#11`, `#15`).
- `#23` fixed the reason every priority family read "0 seen" in prod: the
  400-page sweep exhausts on MVE shards before reaching them. Families are now
  fetched by series name. Census went from all-zeros to KXBTC 318 seen / 21
  candidates, KXHIGHNY 6/6, KXETHD 390/22, etc.
- `#24` made the paper period measurable. Three queries filtered on
  `action_taken='executed'`, so dry-run rows were never settled and never
  scored — zero calibration data by construction. Forecasts are now graded
  whether they traded, papered, or were refused, in three modes that are never
  averaged (`live` / `paper` / `refused`).

---

## What is IN PROGRESS / UNVERIFIED

**CI was never read for `#24`.** GitHub's check-runs API began returning
`403 Resource not accessible by integration` mid-session and the commit-status
API reported no registered checks. It was merged on local evidence only — 813
tests passing and `ruff` clean on that exact commit. Defensible, but not the
same as green CI. Re-run the suite before building on it.

**`#24` has not yet produced a single graded row.** The code is tested but the
mechanism has never run against a real resolution — and cannot until the
deploy above succeeds. First thing to check once it does:

```
railway logs | grep "Graded .* forecast row"
```

If that line never appears after a few hours, look at
`Ledger.reconcile_forecasts` — most likely cause is `_market_result` returning
nothing because markets have not resolved yet, which is benign, but confirm
rather than assume.

**No fill has ever completed, in the entire history of this bot.** Not one
order has ever been executed, in demo or prod. The first live fill will
exercise a code path that has never run end to end — reconciliation, fill
recording, settlement and calibration writeback all included. Treat the first
live trade as a test of the machinery, not as a trade.

**`approved 0` on every pass observed so far.** The Checker rejects everything.
Live mode does not change that — if it still reads `approved 0`, nothing is
trading and the reason is upstream of execution.

---

## The single next action

**Read the calibration table and decide whether the Checker is miscalibrated.**

```python
from memory.edge_store import EdgeStore
for row in EdgeStore().calibration_by_category():
    print(row["mode"], row["category"], row["source"], row["n"],
          row["brier_score"], row["total_pnl"])
```

The `refused` mode is the one that matters right now. It answers: *is the gate
turning down trades that would have won?* With `approved 0` every pass, that is
the highest-value question in the system. If `refused` shows good Brier and
positive counterfactual PnL, the gate is too tight and that is where the edge
is being lost — not in the model.

Do not tune thresholds before that table has rows in it. Guessing at gate
settings without it is exactly the failure mode this session spent its time
eliminating.

---

## Then, in order (do not skip ahead)

### Item 2 — golf grounding without ESPN

Golf is the only sports priority. ESPN is blocked at the IP level from
Railway — **not a header problem, do not revisit the ESPN client.**

Groundwork already done: `#20` made the golf context player-aware.
`yes_sub_title` (the player name) now survives validation onto `Candidate`,
the named player's line is always included even outside the top ten, and
absence from the field is stated explicitly. That work is source-agnostic —
only the *fetch* needs replacing, not the context shaping.

Note `PGATOUR: 0 seen` in the census is currently a true answer (no active PGA
event), not a bug. Confirm a tournament is live before concluding the fetch is
broken.

Candidate sources not yet investigated: the PGA Tour's own public endpoints,
DataGolf, or a scraped leaderboard. Whatever is chosen must work from a
datacenter IP and fail closed.

### Item 3 — latency-aware crypto quant path

RTI settlement is correct and the feed is live. Volatility history is built
from the tick stream (`#17`) at `RTI_TICK_SAMPLE_SECONDS=5`, needing
`MIN_VOL_SPAN_SECONDS=600` — so ~10 minutes of uptime before crypto can price
at all, and **every redeploy resets it** (in-memory buffer). That is the
obvious first target: persist the history, or shorten the warmup safely.

Keep fail-closed: no feed → no trade. Do not weaken
`MAX_SPOT_AGE_SECONDS`, the outlier filter, or the settlement blackout.

### Item 4 — market-making / inventory-skew layer

Not started. Design + scaffolding + tests only. Requirements: two-sided
quoting with inventory skew, per-market and global inventory limits, ability
to pull or widen under risk, no weakening of existing controls, no dilution of
the directional and quant paths.

---

## Traps already paid for — do not re-derive

- **Verification does not spread by prefix.** `KXETHY` (yearly) once inherited
  `KXETH`'s hourly settlement confirmation. Fixed in `#19`; the invariant is
  pinned by test. Anchoring ticker matching to the string start was tried and
  **rejected** — it breaks `KXFRENCHPRES`, `KXVPRESNOMR`, `KXNEXTPRESSEC` and
  hands `KXECONSTATCPIYOY` to the two-letter Electoral College pattern.
- **Taxonomy matching is a substring test.** `KXDRAINTHESWAMP` matched `RAIN`
  and was filed as Weather; a TV market matched `ETH` and was filed as Crypto.
  Corrected individually via `_OVERRIDES` (`#16`), from observed tickers only.
- **Railway tags stderr as `error`.** Every INFO line read as an error until
  `#18` sent logging to stdout. Old log excerpts in chat are misleading.
- **Demo and prod Kalshi keys are separate.** A demo key against prod returns
  `401 authentication_error / NOT_FOUND`, which reads like a routing bug.
  `#21` makes that message say so.
- **The credential currently in Railway was pasted into a chat transcript.**
  It should be rotated. Operator was told and chose to proceed.

---

## Standing constraints

- Golf is the only sports priority; other sports are out of scope.
- Do not touch the ESPN client.
- Small, reviewable PRs. Comprehensive tests. `ruff` clean. Fail-closed.
- No silent behaviour changes; no weakening of existing risk controls.
- Progress means real edge and real fills, not activity.
