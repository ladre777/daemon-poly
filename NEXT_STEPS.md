# NEXT_STEPS

Handoff for the next session. **Read this, not chat history** — chat contains
stale PR numbers and at least two claims I had to correct later.

Last updated: 2026-08-17 17:35 UTC, end of the prod-cutover session.

---

## Ground truth as of this file

Verify these rather than trusting them; they were true when written.

| | |
|---|---|
| `main` | `1b431a5`, `#34` merged plus four follow-ups. Check `git log --oneline -8`. |
| Tests | 880 passing locally, `ruff` clean. **CI was NOT read** — see below. |
| Railway | `env=prod`, `DRY_RUN=false` — **LIVE, REAL MONEY**, confirmed from the boot banner at 16:50. |
| Live commit | `1b431a5`, deployment `0c8eae18`, booted 17:31 UTC. Read `meta.commitHash`, never a green SUCCESS. |
| Account | funded ~$49.98 |
| RTI feed | live on prod, subscribed to BRTI + ETHUSD_RTI |
| Volatility clock | **starts warm now** (`Restored volatility history: btc 430 point(s), eth 431`). Sigma is measured across a sampling ladder and logged both at boot and per family per pass — the two agree, 52% vs 52.2% at 17:33. |
| ESPN | **blocked for bots. Do not touch the ESPN client.** |
| Web access from the agent sandbox | direct `curl` is blocked, but the proxy-backed WebSearch/WebFetch tools DO work — usable for research, not for testing whether an endpoint works from Railway |
| NOAA | wired, never yet executed against a live weather market |

**Fetch before you read `origin/main`.** A stale remote ref in this session
made `#31` look unmerged when it was already on main, and produced `#32` — an
empty squash commit. Harmless, but `git fetch origin main` first.

Effective bankroll is `min(--bankroll, exchange balance)`, so sizing is capped
by the real $49.98, not the $1000 CLI default. At `MAX_POSITION_PCT=0.05`
that is ~$2.50/position — roughly 6 contracts at 40c.

### Deploy state

Live cutover completed at 14:13; production sat on `#24` until 16:50, when
deployment `65e6e4ea` finally shipped the head of main. Confirmed from the
banner and from a log line only the new code emits, rather than assumed:

```
Restored volatility history: btc 612 point(s), eth 614 point(s)
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

**The dashboard "Redeploy" button does NOT ship latest `main`.** It rebuilds
the commit that deployment carried. Verified: a dashboard redeploy at 14:38
came back `reason: "redeploy"`, `commitHash: 71bc67e7` — i.e. `#24`, leaving
`#28` and `#30` behind while looking like a healthy successful deploy.

To ship latest `main`, push a commit to `main` and let auto-deploy run. To
confirm which commit is actually live, read `meta.commitHash` from
list-deployments, or check for a log line only the newest code emits — do not
infer it from a green SUCCESS.

**`get-status` hides failed attempts.** It reports the latest *active*
deployment, not the latest *attempt*, so a newer failure is invisible there
and the service looks fine. This led to a wrong diagnosis once
("auto-deploy has stopped responding") when auto-deploy was working and the
build was simply failing. Always use `list-deployments` to see attempts.

**Auto-deploy on push works.** Builds are just flaky — roughly 3 successes in
9 attempts on 2026-08-17, all failures stopping at
`scheduling build on Metal builder` with no Docker steps. The remedy is to
retry, not to hunt for a cause in the repo. Pushing a commit to `main` is a
reliable trigger.

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

**Item 3 — latency-aware crypto.** Substantially done, not finished.

- `#30` persists the volatility buffer, so a redeploy no longer resets the
  clock. Confirmed in production: the 16:50 boot restored 612 btc and 614 eth
  points instead of starting at zero. This was the reason the 15-minute and
  hourly crypto families had never priced once in any run.
- `#31` verified `KXBTC` from its own rules text (60-second BRTI average,
  fixed strike, hourly window), which took it off the "unverified spec"
  refusal path. Census now reports 27 candidates on that family alone.

- `#34` fixed the volatility estimate. It read 6.6% annualized for bitcoin
  against a market implying 18.4%, because BRTI is a smoothed aggregate and we
  sampled it at 5 seconds — inside its smoothing window. Sigma is now measured
  across a ladder of sampling intervals, taking the largest, and the quant path
  refuses outright when the estimate implies an implausible annualized vol.

`#34` is confirmed against the live feed — the signature climbs and the
coherence gate now passes crypto proposals. What is left is that the estimate
is a *trailing realized* volatility used as a *forward* one, which overshoots
after a move (52% measured against a market implying ~28%). See "The single
next action". After that, genuinely latency-aware behaviour — reacting inside
the settlement window rather than merely being warm enough to price.

**Order pricing.** `#33` fixed a silent defect worth knowing about even though
it is closed. Kalshi quotes some markets in tenths of a cent
(`price_level_structure: "tapered_deci_cent"` — `KXBTC15M` rests 0.9590 /
0.9600), and the wire price was built with `f"{cents / 100:.2f}"`, which
rounds to the *nearest* cent. A 5.55c limit went out as a 6c order: above the
price risk sized against, above the number stored on the order, above what
counted as exposure. The limit is now floored onto the grid inside
`cost_per_contract_cents`, and the formatter raises rather than rounds.

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

**`approved 0` on every pass observed so far.** On the LLM path the Checker
rejects. On the quant path the coherence gate refused every crypto proposal
because the volatility estimate was ~3x too small — diagnosed and fixed in
`#34`, but **not yet observed working against the live feed**. Both are
upstream of execution, so nothing has reached the exchange.

---

## The single next action — READ THIS FIRST

**Split the calibration table by time, then re-read it. Do not touch a
threshold before you do.**

The table has rows for the first time in the bot's history, and the first row
says something important:

```
Calibration [refused] Crypto/quant: n=64  brier=0.175  said 46% actual 45%  pnl $12.75
```

Read carefully, that is the gates refusing trades that would have won:

| | |
|---|---|
| Brier | 0.175 against a climatology baseline of 0.2475 — a **+29% skill score** |
| Calibration | said 46%, actual 45% — one point off in aggregate |
| Counterfactual PnL | **+$12.75** over 64 forecasts, $0.199/contract |

At a plausible per-row spread of $0.3-0.7 that PnL carries t = 2.3 to 5.3. The
model has genuine skill and the refused trades were profitable.

**And it is still not enough to act on.** Four reasons, and the third is
disqualifying on its own:

1. n=64, one category, one source.
2. Counterfactual PnL assumes a fill at the quote available when the decision
   was made. Real fills slip.
3. **These rows span two different models.** Grading only started tonight, but
   the rows themselves are older — many were written before 17:00 on
   2026-08-17, when sigma was 6.6% annualized and every crypto probability was
   pinned at 0% or 100%. This number may be measuring a model that no longer
   exists.
4. Aggregate calibration hides offsetting errors: a mix of over- and
   under-confident forecasts averages to "well calibrated".

So the next action is to make (3) answerable. `calibration_by_category` groups
by category, source and mode; it needs a time dimension too — either a
`since` parameter or a bucket by day — so the pre- and post-fix regimes can be
read apart. Then re-read, and only then consider whether any gate is too
tight.

The logging from `#40` fires on every settlement, so the table keeps filling
on its own. By morning n should be substantially larger.

**Do not lower `MIN_EDGE_THRESHOLD`, raise `CHECKER_MIN_CONFIDENCE` or widen
`COHERENCE_MAX_LOG_ODDS` on the strength of one aggregate row that mixes two
model regimes.** Two wrong calls were made today by reasoning from a single
observation; this is the same shape.

---

## What the overnight review found (2026-08-18, ~02:50 UTC)

**Forecast grading runs now.** `#38` fixed head-of-line blocking and produced
nine grading runs and ~28 rows overnight, after producing exactly zero in the
bot's entire prior history.

**The calibration table is visible.** `#40` logs it on every settlement, plus
each graded row individually. It had been readable only by opening SQLite on
the production volume — the same blindness as the volatility estimate, one
level up.

**The Checker date fix worked, and immediately found a real defect in the
Maker.** Its weather reasoning went from *"NWS forecasts don't extend 2+ years
out, so the Maker's claimed forecast is almost certainly a hallucination"* to:

```
same-day forecast errors are much smaller (often <2°F) than the 3-4°F the
Maker assumes
A forecast of 86°F is only 1°F above threshold, well within typical forecast
error margins
```

**That is the same bug as the crypto volatility problem, in the weather
path**: overstated uncertainty inflating the chance of a threshold miss, which
manufactures edge that is not there. Different path, identical shape. Worth
fixing next after the calibration split — the Maker should use realistic
same-day NWS error (~1-2°F), not 3-4°F.

**Nothing has traded.** `approved 0 -> filled 0` on every pass, before and
after the date fix. No fill has ever occurred in this bot's history.

**Off-mandate scanning.** MLB player props (`KXMLBKS-...`) are being scanned
and consuming Checker calls. They are not parlay shards so `#36` did not
exclude them, and golf is the only sports priority. Costs money, not
correctness.

---

## What the 4-hour review found (2026-08-17, ~22:30 UTC)

**The volatility ladder plateaus now.** `#37` was correct that 120s was
truncating it:

```
btc  tick 16%  15s 26%  30s 35%  60s 48%  120s 58%  300s 62%
eth  tick 11%  15s 18%  30s 24%  60s 33%  120s 41%  300s 42%
```

btc flattens 58→62, eth 41→42. The knee is at 300s, not 120s.

**But the sigma in use has drifted high for medium-dated contracts.** Live
readings at 22:04-22:27:

```
btc 17.7%   lookback 12046s (~10-min contract)
btc 31.1%   lookback 59381s (~49-min contract, uses the whole 5.4h buffer)
eth 23.1%
```

The short-lookback reading (17.7%) matches market-implied almost exactly —
18.4% back-solved this afternoon from two adjacent strikes. The long-lookback
one is nearly double, because `lookback = seconds_to_expiry * 20` pulls a
49-minute contract's estimate across five hours that include an earlier
volatile stretch.

Back-solving one live market — 38 minutes out, spot 0.23% above strike, market
94.5% — puts market-implied vol at **~17%** against our **30%**. The Checker
flagged the same thing independently and in the opposite direction from this
afternoon: it now says *"the model's per-second vol seems too high or
misapplied"*, where at 17:53 it said 0.008 was too low.

**So the 20x lookback multiplier is the next suspect** — but do not change it
on this evidence alone. That is exactly the reasoning-from-one-observation
that produced two wrong calls today. Wait for the calibration table.

**The Checker is mostly right, and once was wrong for a fixable reason.** Its
objections are specific and on-the-merits: it caught a bucket-contract
mispricing in our quant model and the ETH sigma understatement. But on
`KXHIGHNY-26AUG17-T84` it rejected an official NWS forecast as *"almost
certainly a hallucination"* because it thought Aug 2026 was two years away.
Nothing told it the date. `#39` fixes that. The false rejections cluster on
weather — the markets with the best grounding.

**Funnel is healthy on volume, still zero on approvals:**

```
110 candidate(s) -> quant 71 (no proposal 57, below edge 8),
llm 10 (capped 0) | proposed 13 -> checked 12 (rejected 12) -> approved 0
```

2863 → 110 candidates and `capped 0` after `#36`. `no proposal 57` is the
horizon guard refusing daily contracts, which at ~19000s of history are just
over the 4x limit.

**Nothing has traded. No fill has ever occurred in this bot's history.**

---

## Previously the single next action (now done)

**Confirm the volatility fix in production, then read the calibration table.**

The previous next action — "the quant model's sigma is ~3.5x too small" — was
diagnosed and fixed in `#34`. What remains is verifying it against the live
feed rather than against a simulation.

### What was wrong, and what fixed it

The first pass with a warm clock (`ddadc43`, 16:51 UTC) priced every crypto
candidate for the first time in the bot's history — `quant 99 (no proposal 0)`
where every previous pass read `no proposal 68` — and the coherence gate then
refused all of it:

```
model says 0% against a market at 19.0%  — 5.46 in log-odds
model says 100% against a market at 84.5% — 5.21 in log-odds
```

Back-solving sigma from two *adjacent* strikes on a ten-minute contract (a
derivation that cancels spot, so it assumes nothing) put us at 6.6% annualized
against the market's 18.4%. The same 1.18e-5 per-second figure appeared at ten
minutes and at four days, so the sqrt(t) scaling was correct and the error was
in the level.

The cause was the feed, not the arithmetic: BRTI is a deliberately smoothed
cross-venue aggregate and `record_tick` sampled it every 5 seconds, inside its
smoothing window. `#34` measures across a ladder of sampling intervals and
takes the largest estimate, since averaging can destroy variance but never
create it. On the production-equivalent input:

```
tick 5.3%  15s 9.0%  30s 12.0%  60s 14.8%  120s 16.1%  300s 15.6%
old 5.3%  ->  new 16.1%     (market implied 18.4%)
```

and the market that logged `model says 100% against a market at 84.5%` prices
at 86.6% with the recovered sigma.

### Verified live — the diagnosis was right

Startup logs the signature, and production printed it at 17:14 and again at
17:15:

```
btc   tick 6%   15s 10%   30s 13%   60s 15%   120s 16%
eth   tick 5%   15s  9%   30s 12%   60s 14%   120s 16%
```

against what a 60-second trailing average of an 18.4% path predicted before
any of this shipped:

```
      tick 5.3%  15s 9.0%  30s 12.0%  60s 14.8%  120s 16.1%
```

A near-exact match on real data. The `tick` rung reads 6% — the same number
back-solved from market prices — and the robust estimate now takes 16%.
Smoothing was the mechanism.

The 300s rung was dropped on this evidence: it read 12% on one container and
19-21% on the next 90 seconds later, while every other rung agreed to the
point. Nine to twelve observations is not enough, and because the estimate is
a *maximum* a noisy rung can only hurt.

### What the fix bought, and what it did not

The gate went from refusing every crypto proposal to refusing one far-tail
strike, and the probability curve became coherent for the first time. Same
event, `KXBTCD-26AUG2117`, before and after:

| strike | before | after | market |
|---|---|---|---|
| T66499.99 | 0% | 24.65% | 9.00% |
| T65999.99 | 0% | 29.68% | 13.50% |
| T65499.99 | 0% | 35.17% | 23.00% |
| T64999.99 | — | 41.04% | 32.50% |
| T63499.99 | — | 59.61% | 70.00% |
| T62999.99 | 99% | 65.62% | 78.50% |
| T62499.99 | 100% | 71.31% | 85.50% |
| T61999.99 | 100% | 76.55% | 90.50% |

Monotone decreasing in strike, as an "above strike" ladder must be. Those are
now refused by the *Checker*, a much later stage, not by coherence.

**But it overshot, and this is not finished.** Back-solving that ladder pair by
pair — spot-free, so it assumes nothing — gives a flat **47.5% annualized for
the model against ~28.3% for the market**. Every deviation has the same sign:
too much probability in both tails, too little in the middle.

That direction is not the harmless one. It manufactures apparent edge on
out-of-the-money strikes — `Maker edge: T66499.99 -> 24.65% (market 9.00%,
edge 14.65%)` is the model inventing 15 points of edge on a tail it is
overpricing. Buying those is how a too-wide sigma loses money. Nothing traded,
because the Checker rejected all of it, but do not read `approved 0` as safety
here.

**Why 47.5% when startup logged 17% — and the answer is not what I first
wrote.** My first explanation was that the quant path uses a horizon-dependent
lookback (`max(3600, min(seconds_to_expiry * 20, 86400))`) while the startup
signature used the default, so a four-day contract measured over 24 hours
against one. That is **wrong**, and the logging added to test it falsified it
immediately:

```
Volatility used btc by lookback (annualized): 3600s -> 51%  86400s -> 51%
Volatility used eth by lookback (annualized): 3600s -> 39%  86400s -> 39%
```

Identical. The lookback makes no difference, because the buffer never holds
more than the retention hour anyway.

The real answer is **staleness**. The 17% was measured at boot at 17:19; the
ladder was priced at 17:22; and by the 17:29 boot the same measurement read
51%. Realized volatility genuinely tripled inside ten minutes:

```
17:19  btc  tick  6%  15s 10%  30s 13%  60s 16%  120s 17%
17:29  btc  tick 14%  15s 23%  30s 30%  60s 43%  120s 51%
```

The tick-to-120s ratio held (2.8x, then 3.6x), so the smoothing correction is
still doing its job — the whole level moved with the market. The model was
never inconsistent with its own diagnostic; the diagnostic was three minutes
old.

### So the remaining problem is a different one

The estimator is no longer broken. What it now is, is a **trailing realized**
volatility being used as a **forward** volatility. Right after a move, trailing
realized spikes and forward implied does not, so the model reads 47.5% while
the market reads 28.3% — and every out-of-the-money strike acquires apparent
edge that is really just a recent move being extrapolated.

That is a genuinely harder problem than the smoothing bug, and it is a
modelling decision rather than a defect to patch. Two honest directions:

1. **Damp the response.** Blend trailing realized toward a longer-run anchor,
   or cap how far one pass can move sigma. Standard, but every parameter is a
   free choice that wants justifying.
2. **Refuse instead.** When trailing realized has moved sharply against its own
   recent history, decline to price rather than trade a spike. Fits the
   fail-closed posture already in this repo, and costs nothing but volume.

There is also a real argument that `max()` across rungs is too aggressive in
trending conditions. At 17:19 the signature flattened (16% -> 17%, plateau
reached); at 17:29 it was still climbing at the top rung (43% -> 51%). A
signature that keeps rising is the signature of a *trend*, not of smoothing,
and taking the maximum picks up the trend along with the undamped vol. A
plateau detector — take the value the signature flattens to, and refuse when
it has not flattened — would handle both cases and is probably the right shape
for this. Do **not** just revert to the tick rung: that is the 6.6% bug.

**Ruled out — do not re-derive.** Gaps in the stored history from container
restarts do *not* inflate the estimate. An hour of 5-second ticks with five
outages returns max-across-rungs 22% against a true 20%, identical to no
outages, because `realized_vol` normalises each return by its own elapsed
time.

Until this is resolved the crypto quant path produces edge estimates that are
too generous on tails after a move. The Checker is currently the only thing
between them and an order, and `approved 0` is not evidence that the numbers
are safe.

Read these next:

- **If the quant path logs `outside the plausible band`**, the estimate is
  broken again. That refusal is doing its job; the answer is upstream of it.
- **If the signature goes flat and low at every rung**, something changed in
  the feed and smoothing is no longer the whole story.

Then check what the coherence gate does with the corrected numbers:

```
railway logs | grep -E "Refusing|Maker edge" | head -40
```

Disagreements should now be single-digit to low-double-digit percentage
points rather than 0%-vs-19%. Log-odds distances under 3.00 will start
reaching the Checker.

### Do not

- **Do not widen `COHERENCE_MAX_LOG_ODDS`.** It caught this bug. If it starts
  refusing again, that is information, not an obstacle.
- **Do not apply a calibration multiplier** to make the model agree with the
  market. That fits one observation and hides the mechanism.
- **Do not lower `MIN_PLAUSIBLE_ANNUAL_VOL`** to get past a refusal.

### Then, the calibration table

It now has real rows to read:

```python
from memory.edge_store import EdgeStore
for row in EdgeStore().calibration_by_category():
    print(row["mode"], row["category"], row["source"], row["n"],
          row["brier_score"], row["total_pnl"])
```

`refused` is the interesting mode: it answers whether the gates are turning
down trades that would have won. Rows written before `#34` were scored with
the broken sigma, so `skipped_incoherent` crypto entries from before 17:00 on
2026-08-17 describe the old estimator and should not be read as evidence about
the gate. Judge the gate on rows written after that.

---

## Then, in order (do not skip ahead)

### Item 2 — golf grounding without ESPN

Golf is the only sports priority. ESPN is blocked at the IP level from
Railway — **not a header problem, do not revisit the ESPN client.**

**Source research is done — do not redo it.** Every viable golf feed needs an
API key:

| source | key | notes |
|---|---|---|
| SportsDataIO | yes, free trial | real-time PGA leaderboards, JSON |
| Sportradar | yes | hole-by-hole, most thorough |
| Slash Golf (RapidAPI) | yes, free tier | PGA + LIV leaderboards |
| DataGolf | yes, paid | strong predictive models, not just scores |
| GolfProjectAPI (GitHub) | no | **scrapes ESPN — inherits the same IP block. Useless here.** |

So item 2 needs the operator to obtain a key before any code is worth
writing. Recommend Slash Golf or SportsDataIO for the free tier. Set it as
`GOLF_API_KEY` and build behind an interface with schema logging on first
contact, the way `_log_golf_schema` already does.

**Do not build this before there are golf markets to validate against.**
`PGATOUR: 0 seen` is a true reading — Kalshi currently lists no golf markets
at all, so a new integration could not be verified end to end even with a key.
Weather is live and liquid today; golf is not.

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

The warmup half is done (`#30` persists the buffer, `#31` verified `KXBTC`),
and the path now prices 99/99 candidates. What replaced it as the blocker is
the sigma calibration described in "The single next action" — do that first,
because latency-aware behaviour built on a 6.7%-annualized bitcoin is
pointless.

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
