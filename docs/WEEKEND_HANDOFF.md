# Weekend demo collection — handoff

Preflight performed 2026-08-16 against the live run. This records what the
service was doing, what to capture on Monday, and five findings that shape
how the resulting data should be read.

**Nothing in this document authorises live trading.** The run described here
is `KALSHI_ENV=demo`, `DRY_RUN=true`, and produced no orders.

## What was running

| | |
|---|---|
| Repo / branch | `ladre777/d-mony-Kalshi777` @ `main` |
| Commit | `63e698134019988ad453d0487484608d370cddf1` |
| Railway project | `zoological-cooperation` |
| Service | `daemon-kalshi-v2` (region `sfo`, 1 replica) |
| Deployment | `d3a6756b-5ad7-4667-b8b4-65e4c0f59330`, `SUCCESS` |
| Ledger | `/data/daemon_kalshi.db` on Volume `2f7a618c`, mounted at `/data` |
| Verified healthy | scan `2026-08-16T01:47:26Z`, reconcile `2026-08-16T01:46:48Z`, `0 unknown \| 0 inconsistency(ies)` |
| Runtime safety state | **verified from the boot banner**, not inferred |

The boot line at `2026-08-15T21:40:21Z` confirms the safety state directly:

```
DÆMON-KALSHI starting | env=demo dry_run=True strategy=taker
categories=['Sports', 'Crypto', 'Politics', 'Finance', 'Weather']
```

Maker LLM at boot: `primary=moonshot:kimi-k2.6 fallback=anthropic:claude-haiku-4-5-20251001`.
Telegram alerting enabled. Volume mount confirmed in the platform log
immediately before `Starting Container`.

Storage **is** durable — the Volume is attached, so the ledger survives
redeploys. (An earlier note in the project history claimed no Volume was
attached. That was wrong; the service config above is the evidence.)

## Findings that change how to read the data

### F1 — Auto-deploy from `main` is ON

`source: {repo: ladre777/d-mony-Kalshi777, branch: main}`. Merging to `main`
redeploys and restarts the collector. One merge on 2026-08-15 produced **five
deployments in 28 minutes** (21:11, 21:16, 21:19, 21:31, 21:39 — four
`REMOVED`, one `SUCCESS`).

**Do not merge to `main` while a collection run matters.** Restart gaps in
the data are self-inflicted otherwise.

### F2 — Every log line arrives as severity `error`

Python's default handler writes to stderr; Railway tags stderr as `error`.
Filtering the log stream by severity therefore returns everything, and a real
failure looks like a routine pass.

Fixed on this branch (`stream=sys.stdout`), **but the fix only takes effect
after a restart**, so it does nothing for the weekend already in progress.

### F3 — Checker parse failures, cause still open

Two in roughly an hour: `KXRAINSHARD2-26AUG15-NYC` at `00:38:43Z`,
`-PHIL` at `00:58:17Z`.

This is **not** the token-cap truncation fixed earlier — that path logs from
`workers.checker`, names `CHECKER_MAX_TOKENS`, and abstains before parsing.
These came from `daemon_kalshi.validation`, so `stop_reason` was not
`max_tokens`.

The cause could not be determined because the log clipped the payload at 200
characters, leaving no way to tell whether the *model* or the *logger* ended
the text. Both are now recorded (see below). **Expect the weekend's data to
still contain undiagnosable instances**; instances logged after the next
restart will carry the evidence.

### F4 — Roughly 99.6% of each scan is never evaluated

`Maker primary provider moonshot failed (transient): The read operation timed
out — falling back` fires on **every** pass. On the Anthropic fallback,
`MAX_FALLBACK_LLM_CALLS_PER_PASS=10` applies:

```
Pass funnel: 2889 candidate(s) -> quant 0 (no proposal 0), llm 10
(below edge 3, failed 0, capped 2814), no grounding source 65 |
proposed 7 -> checked 7 (rejected 5, failed 0) -> approved 0 -> filled 0
```

So the corpus is **a large scan sample with ~10 model opinions per pass**,
concentrated on the same handful of weather markets. Do not read the verdict
distribution as representative of the candidate universe. Whether to fix the
Moonshot timeout or raise the fallback cap is a strategy decision and was
deliberately left alone.

### F5 — The scan does not cover the whole catalog

From the first pass:

```
Scan stopped at the 400-page cap (~80000 markets) with more catalog
remaining. Raise SCOUT_MAX_PAGES if markets you expect to trade are
being missed.
Scout found 1713 candidates ... (skipped by group: Other 27244,
Entertainment 4557, Science/Tech 934, Media 870, Esports 96,
World Events 81, invalid: 385, below the $500 liquidity floor: 44120)
```

So the corpus is bounded three ways before any model sees it: the page cap,
the five configured categories, and the $500 liquidity floor (which removes
~44k markets on its own). None of these were changed — they are strategy
scope — but the weekend's data describes *that slice*, not Kalshi.

### F6 — Expect zero orders, fills, and settlements

`approved 0 -> filled 0` on every pass sampled from 21:42 to 01:37. The
weekend yields scan, verdict, and reconciliation data only. **If any order,
fill, or settlement row appears, treat it as a finding and investigate before
drawing conclusions** — the export calls out live orders explicitly.

Also outstanding, unrelated to this run: the Telegram bot token was printed
into Railway logs by builds predating the logging fix, and those logs are
retained. **Rotate it.** Do not export raw unfiltered logs until it is rotated.

## Monday checklist

### 1. Copy the ledger before reading it

Never point tooling at the file the daemon is writing. From a Railway shell
or a volume snapshot:

```
cp /data/daemon_kalshi.db /tmp/ledger-copy.db
```

### 2. Export the sanitized summary

```
python scripts/weekend_export.py --db /tmp/ledger-copy.db
python scripts/weekend_export.py --db /tmp/ledger-copy.db --json > summary.json
```

The script opens the database `mode=ro` (SQLite refuses writes), reads no
environment variables, and **never emits model prose** — `maker_reasoning`,
`checker_reasoning`, `kill_switch_reason` and `last_error` are counted, not
printed. Its output is safe to paste into a review document.

### 3. Capture the logs

Railway → `daemon-kalshi-v2` → deploy stream. Useful filters:

- `Pass funnel` — the decision funnel, one line per pass
- `Reconciled:` — account health, `unknown` and inconsistency counts
- `unparseable JSON` — F3 instances
- `falling back` — F4 frequency

Filter before exporting; do not dump the raw stream (see the token note).

### 4. Metrics for the strategy review

| Metric | Where |
|---|---|
| Candidates per pass | `Pass funnel` |
| `llm_called` vs `capped` (quantifies F4) | `Pass funnel` |
| proposed → checked → rejected → approved → filled | `Pass funnel` |
| Checker verdict mix and mean confidence | export, `edges.by_verdict` |
| Maker edge distribution | export, `edges.edge_size` |
| Category coverage | export, `edges.top_categories` |
| Orders by state, dry-run vs live | export, `orders` |
| Unresolved UNKNOWN orders | export, `orders.unresolved_unknown` |
| Kill-switch state | export, `kill_switch` |
| Last reconciliation | export, `account_snapshot` |
| Parse-failure count | log filter `unparseable JSON` |

### 5. Before changing anything

The commit under review is `63e6981`. CI on it: `ruff` clean, **494 tests
passing**. Re-run both before and after any change:

```
ruff check core workers memory backtest scripts tests main.py config.py
python -m pytest
```
