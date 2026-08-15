# DÆMON-KALSHI safety model (P0)

This documents the P0 requirements from the implementation brief: what was
built, the exact formulas risk enforces, and — importantly — what has *not*
been verified. Read the "Not verified" section before pointing this at a
production key.

**DRY_RUN defaults to `true` and is unchanged.** A fresh checkout places no
real orders. Paper mode is a terminal `dry_run` order state that never
contributes exposure and never counts as an executed trade.

---

## State machine: signal → order → fill → settlement

```
  Scout candidate
        │
        ▼
  Maker proposal ──(edge < MIN_EDGE_THRESHOLD)──▶ dropped
        │
        ▼
  Checker verdict ──(reject / low confidence)──▶ logged, no order
        │
        ▼
  RiskGuardrail.evaluate(verdict, reconciled snapshot)
        │                       │
        │                       └──(refused)──▶ edge row: skipped_risk
        ▼
  OrderIntent  ── deterministic client_order_id ──┐
        │                                          │
        │  persisted BEFORE the network call       │
        ▼                                          │
    [intent] ──(duplicate intent key)──▶ blocked, no submission
        │
        ├── DRY_RUN ────────────────────▶ [dry_run]  (terminal, no exposure)
        │
        ▼  POST /portfolio/orders
        │
        ├── 4xx ────────────────────────▶ [rejected] (terminal, no exposure)
        ├── timeout / 5xx ──────────────▶ [unknown]  ── lookup by client_order_id
        │                                     │              │
        │                                     │        found │ not found
        │                                     │              ▼
        │                                     │         [rejected]
        │                                     ▼
        └── 2xx ──▶ [submitted] ──▶ reconcile fills ──▶ [open]
                                          │              [partially_filled]
                                          │              [filled]
                                          │              [cancelled] / [expired]
                                          ▼
                              fills recorded (unique fill_id)
                                          │
                                          ▼
                          exposure = f(confirmed fills only)
                                          │
                                          ▼
                     Ledger.reconcile_settlements()
                       exchange result per market
                                          │
                                          ▼
                     settlement row per fill (unique settlement_key)
                                          │
                                          ▼
                     edge row settled via client_order_id → calibration
```

Terminal states: `filled`, `cancelled`, `expired`, `rejected`, `dry_run`, and
`partially_filled` when the order was IOC (the exchange cancels the
remainder). `unknown` is never terminal — while any order sits there, the
account is not tradeable.

### Why `unknown` exists

A timeout on order submission is the one case where our view and the
exchange's can silently diverge. It is modelled explicitly rather than folded
into a generic error, and it is never retried: the client order ID is
deterministic and was persisted before the request, so recovery is a lookup,
not a resubmission.

---

## Exposure definitions

This bot only *buys* binary contracts. A contract bought at price `p` settles
at 0 or 100, so worst-case loss equals cash committed. All figures in cents.

```
position worst case      = quantity × avg_fill_price + fees
pending worst case       = outstanding_quantity × limit_price
  where outstanding      = requested − filled           (normal live order)
                         = requested                    (unknown state)
worst-case exposure      = Σ positions + Σ pending
```

An `unknown` order reserves its **full** requested quantity: the exchange may
hold all of it. Assuming the optimistic case is how an outage becomes an
unbudgeted position.

Exposure is grouped three ways, because concentration is a distinct risk from
total size:

| Grouping | Key | Rationale |
|---|---|---|
| Ticker | market ticker | single-market concentration |
| Event | `event_ticker`, else ticker up to the last `-` | markets in one event are usually mutually exclusive outcomes of the same question, so several positions there are one correlated bet |
| Category | Kalshi's category | correlated across an entire vertical |

## Risk formulas

```
effective_bankroll   = min(--bankroll, exchange_balance)
fee_per_contract     = ceil(FEE_RATE × p × (1 − p) × 100) / 100   , p = price/100
limit_price          = min(executable_price + SLIPPAGE_CENTS, 99)
cost_per_contract    = limit_price + fee_per_contract
executable_price     = yes_ask            (buying YES)
                     = 100 − yes_bid      (buying NO)
```

Sizing takes the **minimum headroom** across every cap, then re-checks each
cap against the final size:

```
headroom = min(
    MAX_POSITION_PCT        × bankroll,
    MAX_TOTAL_EXPOSURE_PCT  × bankroll − current_total_exposure,
    MAX_TICKER_EXPOSURE_PCT × bankroll − ticker_exposure,
    MAX_EVENT_EXPOSURE_PCT  × bankroll − event_exposure,
    MAX_CATEGORY_EXPOSURE_PCT × bankroll − category_exposure,
    available_balance − pending_exposure,
    MAX_DAILY_LOSS_PCT × bankroll − realized_loss_today,
)
size = floor(headroom / cost_per_contract)
```

The invariant, enforced immediately before submission:

> never submit an order when reconstructed worst-case exposure plus the
> proposed order exceeds the configured limit, and never count an order as
> executed until confirmed fills are reconciled.

### Two distinct loss controls

- **Kill switch** — trips on *realized* daily losses breaching
  `MAX_DAILY_LOSS_PCT`, persists to SQLite, and requires a human to clear.
  An in-memory flag would un-trip itself on the next Railway redeploy.
- **Daily loss budget** — a sizing gate: losses already booked today shrink
  how much *new* risk may be opened, so a bad morning tightens the afternoon.

These are deliberately separate. An earlier draft counted all open exposure
as a same-day loss, which made `MAX_TOTAL_EXPOSURE_PCT` (50%) unreachable
under a 10% daily limit — the larger cap became dead code and every order was
refused once exposure passed 10%. Open positions are not a realized loss.

## Settlement

PnL is computed per fill from the exchange's own result:

```
held side wins:   pnl = count × (100 − fill_price) − fees
held side loses:  pnl = −(count × fill_price) − fees
```

Each fill carries its own price, so two entries on one ticker at different
prices settle to different PnL. `settlement_key = "{ticker}:{fill_id}"` is
UNIQUE, so re-running reconciliation is a no-op rather than doubled PnL.

The previous implementation inferred the outcome from `resting_orders_count`
or the sign of realized PnL. PnL sign depends on which side you held, not
which side won — a NO position resolving NO also shows positive PnL — so that
inference mislabelled outcomes and corrupted every Brier score the
calibration system depends on.

## Fail-closed conditions

No order is placed when any of these hold:

- account state has never been reconciled;
- the snapshot is older than `MAX_RECONCILIATION_AGE_SECONDS` (90s default);
- any order is in `unknown` state;
- locally reconstructed positions disagree with the exchange's
  (override with `ALLOW_POSITION_DRIFT=true` for a first run against an
  account with pre-existing manual positions — it logs loudly instead);
- the kill switch is set;
- `ORDER_STRATEGY` is anything other than `taker`.

Startup refuses to run at all if reconciliation fails, rather than trading
against an unknown account.

## Maker mode

`ORDER_STRATEGY=maker` is refused at startup **and** at the execution
chokepoint (config is mutable at runtime, and a future caller could construct
`Execution` directly). Enabling it requires, per the brief: existing-order
lookup before submission, duplicate prevention across scan passes, one active
order per strategy/ticker/side/price intent, TTL expiration, cancellation on
stale signal or market close, repricing only after a confirmed cancel, and
exposure reservation for outstanding orders.

`Execution.expire_stale_orders()` and `ORDER_TTL_SECONDS` exist as the start
of that lifecycle, but the rest is not built and maker mode stays refused.

## Duplicate prevention

The intent key hashes ticker, action, side, count, price, time-in-force,
source and an hourly bucket. One attempt per intent per
`DEDUPE_WINDOW_SECONDS`, **whatever became of the previous one**.

Blocking only live-or-filled orders would leave a zero-fill IOC
re-submittable on the very next pass, so a signal that persists while the
price sits just out of reach becomes an order every 30 seconds. A re-quote at
a different price is a different intent, so genuine re-attempts still work.
This trades fill rate for bounded submission volume, which is the right side
to err on for an unattended bot.

---

## Not verified

**No part of this has been run against a live or demo Kalshi endpoint.** The
92 tests pass against an in-memory fake built from the response shapes this
repo's client *believes* Kalshi returns. They prove the bot's own logic;
they do not prove the wire format.

Confirm against `demo-api.kalshi.co` before trusting any of it:

- [ ] Order status vocabulary. `_state_from_status` maps `resting`, `open`,
      `pending`, `executed`, `filled`, `canceled`, `expired`, `rejected`.
      Unrecognised statuses fall back to inferring from quantities, which is
      safe but blunt.
- [ ] `time_in_force` accepted values. The client sends `IOC` and `GTC`,
      inferred from Kalshi's order-panel docs rather than a request schema.
- [ ] `client_order_id` really is an idempotency key. The whole
      timeout-recovery design rests on this. If Kalshi does not dedupe on it,
      a recovered timeout could double a position.
- [ ] `GET /portfolio/orders?client_order_id=` filters server-side. If it is
      ignored, recovery still works (we filter client-side) but pages more.
- [ ] `GET /portfolio/settlements` exists and returns `market_result`. There
      is a fallback to each market's `result` field, also unverified.
- [ ] Fill field names: `trade_id`, `yes_price`/`no_price`, `fee_paid`.
- [ ] Position sign convention — positive `position` is read as long YES,
      negative as long NO, with `market_exposure` as total cost in cents.
- [ ] `available_balance` on the balance response; falls back to `balance`.
- [ ] Fee formula. `FEE_RATE=0.07` is from published summaries, not a
      verified schedule.

## Known limitations

- **Unrealized PnL is not in the loss controls.** It needs a live mark per
  open position; the loss controls use realized PnL and worst-case exposure
  instead. The brief asks for unrealized "where supported by available data",
  and it is not yet.
- **Edge is still measured against the midpoint**, while sizing uses the
  executable price. That mismatch is P1 item 7; risk now enforces a minimum
  edge as a backstop but does not yet recompute edge net of fees and
  slippage.
- **Sells and short positions are not modelled.** Every order is a buy, and
  the exposure formula assumes it. A sell path needs its own liability model.
- **Position metadata is best-effort.** Event and category for positions
  opened by a previous process are recovered from local order history, or
  parsed from the ticker. A position with neither gets an empty category and
  is not counted against the category cap.
- **No schema migrations beyond additive columns.** `schema_version` is
  written but only an additive `ALTER TABLE` path exists (P1 item 11).
- **`LEDGER_DB_PATH` defaults to `/data/`**, which assumes a mounted Railway
  volume. Without it the database is ephemeral and every restart loses order
  history — the startup check for this is P1 item 11.

## Manual checks before enabling production

1. Run against demo with `DRY_RUN=true` and confirm reconciliation logs
   sensible balance and position numbers.
2. Work through the "Not verified" list above against demo responses.
3. Run with `DRY_RUN=false` on demo and confirm one order's full lifecycle:
   intent → submitted → fills → settlement row → edge writeback.
4. Kill the process mid-pass and confirm startup reconciliation rebuilds the
   same exposure.
5. Confirm `LEDGER_DB_PATH` points at a mounted volume.
6. Only then consider a production key, still starting at `DRY_RUN=true`.

---

# P1: validation and strategy correctness

## Item 6 — strict schemas

`core/validation.py` validates everything entering the system from outside.
The rule: bad input becomes a **refusal**, never an exception that propagates
toward execution and never a silently coerced value.

Market data → skip the market. Model output → abstain. Neither raises.

| Input | Checks |
|---|---|
| Scout market | ticker and title present; prices finite and in 0–100c; bid ≤ ask (a crossed book is rejected, not turned into a negative spread); volume finite and nonnegative; positive time to close; `between` strikes have both bounds in order; quote timestamp present and not in the future |
| Maker JSON | `probability_yes` and `confidence` finite and in [0,1]; reasoning present and length-bounded; anything else → no proposal |
| Checker JSON | verdict is **exactly** `approve`/`reject`/`abstain` (no prefix matching, no `approved`→`approve`); confidence finite and in [0,1]; anything else → abstain |

NaN is the case worth naming. `float("nan")` survives `json.loads` and
`float()`, and every comparison against it is False — so a NaN confidence
fails a `>=` threshold silently, and a NaN probability makes every downstream
edge NaN without anything raising. `finite()` rejects it explicitly, and
booleans are rejected too since `True == 1` would otherwise become a valid
probability of 1.0.

## Item 7 — edge from executable prices

```
executable price   = yes_ask                 (buying YES)
                   = 100 − yes_bid           (buying NO)
executable edge    = p − ask/100             (YES)
                   = bid/100 − p             (NO)
net edge           = executable edge − (fee + slippage)/100
```

Approval now uses **net edge**. The midpoint flatters every trade by half the
spread and omits the fee entirely: on a 48/52 market a "4pp edge" measured
against the midpoint is 2pp before costs and can be negative after them.

`Proposal.edge_size` is now the executable figure, so everything downstream
(risk thresholds, the longshot guard, edge memory) reads the tradeable
number. `midpoint_edge` is kept for reporting and appears in refusal messages
so the difference is visible.

Maker screens on the same number risk enforces — one definition in
`core/pricing.py`, imported by both. If they diverged, Maker would spend LLM
budget generating proposals risk always rejects.

Each decision records the exact quote it used (`quote_yes_bid`,
`quote_yes_ask`, `quote_captured_at`, `quote_source`) in `RiskDecision.detail`,
and a quote older than `MAX_QUOTE_AGE_SECONDS` is refused immediately before
submission — a proposal that sat in the LLM queue is not traded at a price
that has since moved.

Fee policy is symmetric across YES and NO. This needed a fix: `p*(1-p)` is
mathematically symmetric, but 0.7 has no exact binary representation, so
`ceil()` charged a 70c contract a full cent more than its 30c mirror.

## Item 8 — contract-specific, data-quality-aware quant path

`core/contract_specs.py` records, per ticker family, what the contract
settles on: strike units, feed units, settlement definition, timezone,
observation window — **and whether any of it has been verified**. None has,
so every spec ships `verified=False` and the quant path declines by default.
`QUANT_ALLOW_UNVERIFIED=true` overrides for demo work.

The gold and silver specs carry an explicit unit mismatch (per-troy-ounce
strike vs per-share ETF feed). That is a ~10x error which would price every
contract at ~0 or ~1 without raising anything, so no override permits it.

Price client fixes, each tied to a specific failure:

| Problem | Fix |
|---|---|
| Ten markets on one symbol → ten API calls and ten duplicate history points, driving realized vol toward zero (vol is in the *denominator* of the probability, so understated vol = overconfident pricing) | One fetch per symbol per pass, via `begin_pass()` |
| A 429 returned `None`, read as "no price" rather than "back off" | Bounded exponential backoff with jitter, per source |
| Unknown sampling interval — vol scaled by an assumed poll count | `realized_vol` returns a **per-second** figure computed from real elapsed time between observations |
| Feed glitch (0.0, a 10x tick) used like a good quote | Outlier filter against the recent median; non-finite and non-positive rejected before entering state |
| A cached quote from an hour ago used like a fresh one | `MAX_SPOT_AGE_SECONDS` staleness check |
| Thin history priced off anyway | `MIN_VOL_OBSERVATIONS` and `MIN_VOL_SPAN_SECONDS` — declines rather than pricing off noise |

## Replay/backtest evidence

`backtest/replay.py` reports calibration buckets, Brier score, net PnL after
fees, gross PnL, total fees, hit rate and max drawdown.

**This is a falsifier, not evidence of profitability.** The world is
synthetic, so a profit in it is a statement about the arithmetic, not about
Kalshi. Nothing here licenses a claim about live returns.

Fill assumptions, which drive the PnL number and are all optimistic:

- every approved order fills in full, immediately, at the executable price
  plus the slippage allowance;
- fees charged per contract on the limit price;
- positions held to settlement, no exits or re-marking;
- no market impact.

Because they are optimistic, a strategy that loses money here would lose more
in reality. The converse does not hold.

The load-bearing test is the **null hypothesis**: against a fairly priced
book, a model that knows the true probability must find *zero* trades once it
crosses the spread and pays fees. If that ever starts printing profits, the
edge or cost accounting is broken. Against a book biased 8c from fair, the
same model trades ~330 of 400 markets, returns a profit, and scores a Brier
of ~0.13 against the 0.25 a coin-flip model scores.

Sensitivity is checked in both directions the brief asks for: stale quotes
are skipped entirely at the configured threshold, and mis-estimating
volatility measurably degrades tail calibration (predicted 94.5% vs actual
80.6% in the confident buckets at 0.4x vol).

## P1 additions to "Not verified"

- [ ] Which field, if any, carries a quote timestamp on a Kalshi market
      payload. `core/validation.py` tries `last_price_time`, `quote_time`,
      `updated_time`, `last_updated_time`, `ts` and falls back to our own read
      time, which bounds staleness by our clock only.
- [ ] Every contract spec in `core/contract_specs.py`: which index settles
      each family, at what observation instant, in what units, in what
      timezone. **All ship unverified.**
- [ ] Whether Kalshi gold/silver strikes are quoted per troy ounce or per ETF
      share. Until resolved those families cannot trade.
- [ ] `VALID_STRIKE_TYPES` — the accepted `strike_type` vocabulary is
      inferred, not confirmed.

## P1 known limitations

- The backtest's synthetic world assumes GBM, which is the quant model's own
  assumption. It cannot detect that the assumption is wrong about real
  markets — only that the code is consistent with it.
- Volatility is still estimated from a short in-process rolling buffer that
  starts empty on every restart. It is not persisted, so a redeploy costs the
  entire vol history and the quant path goes quiet until it rebuilds.
- `SpotQuote.observed_at` is our read time. The free feeds do not reliably
  report their own print time, so a quote can be older than we think.
- Direction is still chosen against the midpoint (which side the model
  disagrees with); only the tradeability test uses the executable price. On a
  very wide book those can disagree, and the trade is then rejected by the
  net-edge check rather than re-routed to the other side.
