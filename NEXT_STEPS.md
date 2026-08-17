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

### Where to look

`PriceHistory.realized_vol` (`core/spot_price_client.py:171`) is correct as
written — it normalises each log return by its own elapsed time, so uneven
spacing is handled properly. The defect is upstream of it, in what lands in
the buffer. In rough order of likelihood:

1. **Repeated ticks.** `record_tick` downsamples to
   `RTI_TICK_SAMPLE_SECONDS=5`. If BRTI republishes an unchanged value between
   updates, the stored series carries runs of identical prices, every one of
   which contributes a zero return and drags sigma down. Check first: pull the
   `price_history` table and count how many consecutive pairs are equal.
   ```sql
   SELECT symbol, COUNT(*) FROM price_history GROUP BY symbol;
   ```
   then diff successive prices and see what fraction are exactly zero.
2. **BRTI is itself a smoothed index**, not a raw print. Its 5-second returns
   are damped relative to the underlying, so the level may be genuinely low
   even with clean data.
3. **Horizon extrapolation.** sqrt(t)-scaling a 5-second sigma to four days is
   a ~70,000x extrapolation and misses drift and jumps entirely. Note the
   understatement is ~2.7x at 45 minutes and ~3.5x at four days — it worsens
   with horizon, but the base level is already wrong, so this is a second
   effect and not the main one.

Whatever the cause, the fix must keep the fail-closed property: a sigma that
cannot be trusted means no trade, not a fudge factor. A defensible interim
step is to refuse to price when the estimate implies an annualized vol outside
a sanity band for the asset — that is a new refusal, not a loosened one.

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
