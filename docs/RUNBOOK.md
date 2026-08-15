# Runbook

What to check, in what order, when the bot is not doing what you expect.

Every entry here is a failure this bot actually produced in production, with
the log line it produced and what it meant. They are ordered by how often
they came up.

---

## The bot is running and never trades

This is the default appearance of almost every bug in this system, because
every stage fails by producing nothing rather than by crashing. Start with
the funnel line, printed once per pass:

```
Pass funnel: 2917 candidate(s) -> quant 412 (no proposal 400), llm 10
(below edge 10, failed 0, capped 2495), no grounding source 0 | proposed 0
-> checked 0 (rejected 0, failed 0) -> approved 0 -> filled 0
```

Read left to right and stop at the first number that surprises you.

| Where it stops | What it means | What to do |
|---|---|---|
| `0 candidate(s)` | Scout found nothing. Either the catalog read is broken or every market is being filtered. | Check the `Scout found N candidates` line for the rejection breakdown, and the field-names line below. |
| `llm N, failed N` | The model provider is rejecting calls. | Look for `Maker primary provider ... failed`. See "Maker/Checker errors". |
| `below edge N` for everything | Working as designed — no market cleared the net edge threshold. | Nothing to fix. `MIN_EDGE_THRESHOLD` is 4pp after fees and slippage. |
| `capped N` | The per-pass LLM call cap. | Expected with thousands of candidates. Raise `MAX_LLM_CALLS_PER_PASS` if you mean to. |
| `checked N, rejected N` | The Checker is vetoing. | Working as designed. Read the verdicts. |
| `approved 0` with proposals | Risk is refusing. | Read the `Risk refused` lines — they always give a reason. |
| `approved N -> filled 0` | Orders placed, nothing filled. | IOC orders that cross no resting size. Normal on thin markets. |

---

## Scout finds 0 candidates

Scout logs Kalshi's actual field names on the first market of every scan:

```
Kalshi /markets fields present on KX...: ..., liquidity_dollars, ...,
volume_fp, yes_ask_dollars, yes_bid_dollars, ...
```

Compare that list against what `core/validation.py` reads. Kalshi changed to
dollar-denominated fields in March 2026 — there is no `yes_bid` and no
`volume` — and reading absent fields as `0` filtered all 79,947 open markets
while every log line looked healthy. That line exists so this takes seconds
rather than two deploys.

The rejection breakdown on the `Scout found` line tells you which gate ate
them: `invalid`, `below the $N liquidity floor`, or `skipped by group`.

---

## Kalshi returns 401

```
Kalshi API error 401: {"error":{"code":"authentication_error",
"message":"authentication_error","details":"NOT_FOUND"}}
```

**Check which host the request went to before assuming the key is bad.**
Demo and production keys are not interchangeable, and a production key
against `demo-api.kalshi.co` returns exactly the same 401 as a revoked one.
The startup line names the environment:

```
DÆMON-KALSHI starting | env=prod dry_run=True ...
```

- `env=prod` → `api.elections.kalshi.com`, needs a production key.
- `env=demo` → `demo-api.kalshi.co`, needs a demo key, balance is play money.

If the host is right and the key still 401s, check that the key has not been
revoked in Kalshi's dashboard, and that this container is not being rate
limited — see below.

The bot refuses to start on a failed startup reconciliation. That is
deliberate: it cannot know its own exposure. It now holds
`STARTUP_FAILURE_HOLD_SECONDS` (60) before exiting so the supervisor's
restart loop does not become a burst of failing auth attempts.

---

## Rate limiting

A full Scout pass paginates the entire ~80,000-market catalog: 400 calls.
Two settings keep that inside Kalshi's limits, and both matter:

- `KALSHI_MIN_REQUEST_INTERVAL` (0.15s) — a floor on spacing between every
  outbound request, applied in the client. Backing off after a 429 does not
  help when the steady-state rate is the problem.
- `SCOUT_POLL_SECONDS` (180) — gap between passes. At the original 30s this
  re-read all of Kalshi twice a minute to act on a few dozen candidates.

If you lower either, do the arithmetic first: pages ÷ interval, against how
often the loop runs.

---

## Maker/Checker errors

```
Maker primary provider moonshot failed (systemic): Client error '400 ...'
Moonshot rejected model 'kimi-k3' with HTTP 400: {"error": ...}
Moonshot: switched to model 'kimi-k2.6' for the rest of this process.
```

The Maker resolves its own model: on a 404 or 400 it asks the account which
models the key has, walks them in preference order, and retries once without
`temperature` (some checkpoints reject it). Pin the winner with
`MOONSHOT_MODEL` to skip the discovery on the next boot.

If Moonshot is unusable it fails over to Anthropic on
`MAKER_FALLBACK_MODEL`, under the tighter `MAX_FALLBACK_LLM_CALLS_PER_PASS`
cap — the fallback is dearer per call and exists to keep the bot alive, not
to run full volume indefinitely. `MAKER_LLM_PROVIDER=moonshot|anthropic`
pins one provider and disables failover.

A single failure skips one candidate. `MODEL_FAILURE_THRESHOLD` (5)
consecutive failures end the pass with a Telegram alert.

---

## "account state is stale"

```
Risk refused KX...: account state unusable: account state is stale
(109s old, limit 90s)
```

The snapshot is refreshed automatically once it passes half its freshness
budget, so this should not recur. If it does, the pass is taking far longer
than expected — check for a hung provider (`MAKER_TIMEOUT_SECONDS`) or a
scan that is being paced harder than intended.

---

## Storage

```
Ledger storage is NOT durable: no Railway Volume is attached to this
service, so the container filesystem is discarded on every redeploy.
```

**Attach a Railway Volume at `/data` before trading real money.** Without
it, every redeploy loses order, fill and settlement history — and a tripped
kill switch silently clears itself. The bot refuses to start when
`env=prod` and `DRY_RUN=false` without durable storage, and only warns in
paper mode.

Railway moved volumes out of Settings: open the service, use the **Data** or
**Volumes** tab, or the `+ Create` menu → **Volume**, and mount at `/data`.
`LEDGER_DB_PATH` already defaults to `/data/daemon_kalshi.db`.

---

## Secrets in logs

httpx logs full request URLs at INFO, and Telegram puts the bot token in the
URL path. `HTTPX_LOG_LEVEL` is `WARNING` for that reason. If you raise it to
debug a request, lower it again before leaving a real token configured — and
rotate any token that was logged, since Railway retains log history.

---

## Kill switch

Tripping is persisted and requires a human to clear. The process exits
rather than looping, because a kill switch that a restart can clear is not a
kill switch. Clear it in the ledger database only after you understand why
it tripped.
