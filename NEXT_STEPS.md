# NEXT_STEPS

Handoff for the next session. **Read this, not chat history** — chat contains
stale PR numbers and at least two claims I had to correct later.

Last updated: 2026-08-17 17:00 UTC, end of the prod-cutover session.

---

## Ground truth as of this file

Verify these rather than trusting them; they were true when written.

| | |
|---|---|
| `main` | `#33` merged. Check `git log --oneline -8`. |
| Tests | 859 passing locally, `ruff` clean. **CI was NOT read** — see below. |
| Railway | `env=prod`, `DRY_RUN=false` — **LIVE, REAL MONEY**, confirmed from the boot banner at 16:50. |
| Live commit | `ddadc43`, deployment `65e6e4ea`, booted 16:50 UTC. Read `meta.commitHash`, never a green SUCCESS. |
| Account | funded ~$49.98 |
| RTI feed | live on prod, subscribed to BRTI + ETHUSD_RTI |
| Volatility clock | **starts warm now.** Boot logged `Restored volatility history: btc 612 point(s), eth 614 point(s)` — well past the 600s the quant path needs |
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

What is left on item 3 is genuinely latency-aware behaviour — reacting inside
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

**`approved 0` on every pass observed so far**, and the reason is now known
rather than suspected. On the LLM path the Checker rejects; on the quant path
the coherence gate refuses every crypto proposal because the volatility
estimate is ~3.5x too small. See "The single next action". Both are upstream
of execution, so nothing has reached the exchange.

---

## The single next action

**The quant model's volatility estimate is roughly 3.5x too small. Fix that.**

This supersedes the previous "read the calibration table" instruction, which
was written before the quant path had ever run. It has now run, and the answer
it gave is much sharper than anything the calibration table would have said.

### What the first warm pass showed

`ddadc43`, 16:51 UTC, the first pass in the bot's history with a warm
volatility clock:

```
Pass funnel: 2962 candidate(s) -> quant 99 (no proposal 0, below edge 69),
             llm 10 | proposed 35 -> checked 16 (rejected 16) -> approved 0
```

`no proposal 0` is the headline. Every prior pass read `no proposal 68` or
`no proposal 73` — the quant path declining every candidate for want of
history. It now prices all 99. Items `#30` and `#31` did what they were for.

And every single crypto proposal was then refused by the coherence gate:

```
Refusing KXBTCD-26AUG2117-T65499.99: model says 0% against a market at 19.0%
  — 5.46 in log-odds, over the 3.00 limit
Refusing KXBTCD-26AUG2117-T62499.99: model says 100% against a market at 84.5%
  — 5.21 in log-odds, over the 3.00 limit
```

Not one or two. All of them, on both assets, at horizons from 45 minutes to
four days, always in the same direction: the model collapses to 0% or 100%
where the market prices 7-20% or 76-94%.

### The arithmetic

`KXBTCD-26AUG2117` expires four days out, and its strike ladder back-solves to
a strikingly consistent market view (spot ~$64,100):

| strike | market | implied sigma (4d) | per-second |
|---|---|---|---|
| 65,500 | 19.0% | 2.46% | 4.19e-5 |
| 66,000 | 11.5% | 2.43% | 4.14e-5 |
| 66,500 |  7.5% | 2.55% | 4.34e-5 |
| 63,000 | 76.5% | 2.40% | 4.08e-5 |
| 62,500 | 84.5% | 2.49% | 4.24e-5 |

Five strikes agreeing to within 6% on one number — that is a coherent implied
surface, not noise. Our model, clipped at 0.1%/99.9% on the same strikes,
implies a per-second sigma of at most 1.19e-5.

Annualized, that is the whole story:

```
market-implied BTC vol :  23.5%   <- plausible for a calm bitcoin regime
our estimator          :   6.7%   <- not a credible number for bitcoin, ever
```

6.7% annualized is roughly the volatility of a G10 currency pair. This needs
no market to refute it. **Do not treat this as "the market disagrees with us"
— treat it as an estimator bug**, and note the corollary: the 69 candidates
counted `below edge` in that funnel were scored with the same broken sigma, so
that number means nothing yet either.

(The 23.5% figure above comes from the four-day ladder, which assumed spot
~$64,100. The spot-free derivation in the next section puts the market at
18.4% on a ten-minute market. Both are in the same place; the ten-minute one
is the stronger evidence because it assumes nothing.)

### It is a level error, not a scaling error

The sharpest measurement comes from `KXBTCD-26AUG1713`, expiring 17:00Z and
logged at 16:52Z — about **ten minutes** to expiry — on two *adjacent* strikes
$100 apart:

```
T63999.99   market 84.5%   model 100%
T64099.99   market 17.5%   model   1%
```

Using the gap between two adjacent strikes cancels spot entirely: only the
log-spacing and the two prices are needed, so this derivation assumes nothing.

```
market per-second sigma  3.27e-5   (18.4% annualized)
model  per-second sigma  1.18e-5   ( 6.6% annualized)
understatement           2.78x
```

Now put that beside the four-day ladder:

| horizon | model per-second sigma | understatement |
|---|---|---|
| 10 minutes | 1.18e-5 | 2.78x |
| 4 days | <=1.19e-5 | 3.5x |

**The model's per-second sigma is the same number at both horizons.** The
sqrt(t) scaling is working correctly and the model is internally consistent —
what is wrong is the level of the per-second estimate itself, by a roughly
constant ~3x (about 8x in variance).

That rules out the explanation to reach for first. sqrt(t)-scaling a
5-second sigma out to four days *is* a ~70,000x extrapolation and would
normally be the prime suspect, but the error is already 2.78x at ten minutes,
where there is no extrapolation at all — the estimator's own window is longer
than the horizon it is pricing. Horizon effects explain the 2.78 -> 3.5 drift
and nothing more.

### Where to look

`PriceHistory.realized_vol` (`core/spot_price_client.py:171`) is correct as
written — it normalises each log return by its own elapsed time, so uneven
spacing is handled properly. So is the sqrt(t) scaling, per the table above.

**Leading explanation: BRTI is a smoothed index, and we sample it at 5
seconds.** CF Benchmarks' Real-Time Index is a deliberately smoothed
aggregation across venues, built to resist manipulation rather than to
reproduce tick-level variance. Smoothing suppresses high-frequency variance
while leaving low-frequency moves intact, so realized vol measured at a
sampling interval near or below the smoothing window is damped — by a
roughly constant factor, at every horizon. That is exactly the signature
observed.

**The diagnostic that settles it**, from the buffer already on the production
volume — no new data collection needed. Compute realized vol from the same
stored series at several sampling intervals:

```python
from memory.price_store import PriceStore
from core.spot_price_client import PriceHistory

points = PriceStore().load("btc", max_age_seconds=3600)
for step in (5, 15, 30, 60, 120, 300):
    h = PriceHistory()
    for at, p in points[::max(1, step // 5)]:
        h.add(p, at)
    v = h.realized_vol(lookback_seconds=3600)
    print(step, v, v and f"{v * (365*24*3600)**0.5:.1%} annualized")
```

This is a volatility-signature plot, the standard test for microstructure
damping. Read it as:

- **sigma rises with sampling interval, then plateaus** -> smoothing confirmed.
  The plateau is the honest sigma; estimate at or beyond that interval. Expect
  the plateau near 18-23% annualized if this diagnosis is right.
- **sigma flat across all intervals** -> smoothing is not the cause. Then check
  whether the stored series carries runs of identical prices (nothing in
  `record_tick` or `PriceHistory.add` rejects a repeated value). Note that
  repeated ticks alone are *not* obviously biasing: a zero return followed by
  one large return contributes the same sum of squares as the moves spread
  evenly, so this inflates the estimator's variance rather than shifting its
  level. It would have to be combined with something else to produce a
  constant 3x.

Sampling less often costs span — at a 300-second interval the same 500-point
buffer covers many hours rather than 40 minutes — so `MIN_VOL_SPAN_SECONDS`
and the retention window have to move together with any change here. That is
a real design trade, not a one-line edit.

Whatever the cause, the fix must keep the fail-closed property: a sigma that
cannot be trusted means no trade, not a fudge factor. **Do not apply a
calibration multiplier to make the numbers agree with the market** — that
fits one observation and hides the mechanism. A defensible interim step is
the opposite direction: refuse to price when the estimate implies an
annualized vol outside a sanity band for the asset. That is a new refusal,
not a loosened one, and it would have caught this on the first pass.

### Meanwhile, nothing is at risk

The coherence gate is refusing 100% of these, which is exactly what it exists
for, and `approved 0` means no money has moved. Live trading can be left
running: an uncalibrated model behind a working gate trades nothing. Do not
widen `COHERENCE_MAX_LOG_ODDS` to "unblock" the funnel — that gate is the only
thing standing between this estimator and the account.

### Still worth doing, after the above

Read the calibration table, which now has real rows to read:

```python
from memory.edge_store import EdgeStore
for row in EdgeStore().calibration_by_category():
    print(row["mode"], row["category"], row["source"], row["n"],
          row["brier_score"], row["total_pnl"])
```

`refused` is the interesting mode: it answers whether the gates are turning
down trades that would have won. Expect it to be dominated by
`skipped_incoherent` crypto rows until the sigma above is fixed — which is
itself the confirmation that the diagnosis was right.

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
