<!--
Verbatim text of the implementation brief, extracted from

    "Claude Implementation Brief: DÆMON-KALSHI Safety and Reliability
     Improvements.docx"

in the ladre777/d-mony-Kalshi777 repository. Committed here because the brief
is referred to as docs/SAFETY_BRIEF.md but only ever existed as a .docx in a
separate repo, so nothing working from this checkout could read it.

Content is unedited apart from markdown headings for readability. See
docs/SAFETY.md for what has been implemented against it.
-->

# Claude Implementation Brief: DÆMON-KALSHI Safety and Reliability Improvements
You are improving the supplied DÆMON-KALSHI Python trading bot. Treat this as a safety-critical trading-system refactor. Do not enable production trading by default. Preserve DRY_RUN=true, and make the application fail closed whenever account state, market data, order state, or settlement data cannot be verified.

## Primary objective
Make the bot safe to test against the Kalshi demo environment before any production key can be enabled. Do not merely patch symptoms. Add tests and durable state so the bot can reconstruct its actual exposure after restart and accurately distinguish signals, orders, fills, and settlements.

## P0 requirements: complete these first
### 1. Implement account and order reconciliation
Replace the in-memory open_positions counter in main.py with an account-state component that loads, normalizes, and persists:

Available balance and account limits.
Current positions by ticker and side.
Open orders by exchange order ID and client order ID.
Requested, filled, remaining, cancelled, and expired quantities.
Average fill price and fees where available.
Last successful reconciliation timestamp.

On startup, reconcile with Kalshi before allowing any new order. If reconciliation fails or is stale, do not trade. On restart, the reconstructed exposure must match the exchange state.

### 2. Make order submission idempotent and fill-aware
Add an order lifecycle model with explicit states such as intent, submitted, open, partially_filled, filled, cancelled, expired, rejected, and unknown.

Use a deterministic client order ID for the same signal/order intent, rather than generating a new UUID on every retry. Persist the ID before submission and safely recover from an HTTP timeout where the exchange may have accepted the order.

After submission, reconcile the exchange order. Do not mark an order as executed merely because the request returned successfully. Only confirmed fills should increase exposure or enter executed-trade analytics.

Handle IOC zero-fill and partial-fill responses correctly. Handle GTC orders with explicit time-to-live, cancellation, and requote policies.

### 3. Disable unmanaged maker mode
Until open-order tracking, cancellation, TTL, and duplicate prevention are implemented and tested, reject ORDER_STRATEGY=maker at startup with a clear error. Never allow the 30-second scan loop to submit duplicate passive orders for the same ticker and signal.

For maker mode, implement:

Existing-order lookup before submission.
One active order per defined strategy/ticker/side/price intent.
TTL expiration.
Cancellation when the signal becomes stale or the market closes.
Repricing only after cancelling or safely replacing the prior order.
Exposure reservation for all outstanding orders.

### 4. Replace settlement heuristics with exchange-confirmed results
Remove the logic that infers the outcome from resting_orders_count or the sign of realized PnL. Obtain the explicit market settlement result from the exchange or the authoritative market endpoint.

Match settlement PnL to actual fills and internal order IDs. Do not apply one aggregate ticker PnL to every recent database row. Reconciliation must be idempotent: running it repeatedly must not duplicate PnL or change already-settled records.

Store at least:

Ticker and exchange market ID.
Order ID and fill ID.
Side and action.
Fill quantity.
Fill price.
Fees.
Settlement result.
Realized PnL.
Settlement timestamp.

### 5. Rebuild risk around worst-case dollar exposure
Do not use only a count of open positions. Risk must account for:

Existing positions.
Pending and resting orders.
Per-ticker exposure.
Per-event exposure.
Category or correlated exposure.
Total available balance.
Worst-case loss and liability.
Fees and conservative slippage.
Actual exchange balance rather than only the CLI --bankroll value.

Enforce limits transactionally immediately before submission. If state is stale, missing, or inconsistent, reject the order. Persist the kill-switch state so a process restart cannot silently reset it. Include realized PnL, unrealized PnL, pending-order risk, and fees in loss controls where supported by available data.

## P1 requirements: validation and strategy correctness
### 6. Add strict schemas for all external and model data
Validate Scout market data before creating a candidate:

Required ticker and title.
Prices finite and within 0–100 cents.
Bid no greater than ask.
Positive time to close.
Valid strike and strike type where applicable.
Nonnegative volume.
Fresh quote timestamp.

Validate Maker and Checker JSON strictly:

Probabilities and confidence must be finite and in [0, 1].
Checker verdict must be exactly approve, reject, or abstain.
Required fields must exist and have correct types.
Invalid output must result in abstention, never an exception that continues toward execution.
Limit reasoning/playbook/context length.

Use a schema library or explicit validation functions and add unit tests for malformed JSON, missing values, NaN, infinity, out-of-range values, and unknown verdicts.

### 7. Compute edge from executable prices
The current proposal compares model probability with midpoint-implied probability but sizes at executable bid/ask prices. Refactor the calculation so approval and expected value use the actual executable price, fees, slippage, and direction.

Record the exact quote and timestamp used for the decision. Reject the proposal if the quote becomes stale before submission or if the executable edge falls below the threshold.

Apply longshot and fee policies symmetrically to YES and NO directions.

### 8. Make the quant path contract-specific and data-quality-aware
For every ticker family, verify that the external spot instrument matches the Kalshi settlement definition, strike units, timezone, observation window, and contract semantics.

Improve the price client by:

Caching one quote per symbol per scan pass.
Recording source timestamps and quote age.
Rejecting stale quotes.
Handling rate limits and provider failures with bounded backoff.
Filtering invalid or obvious outlier prices.
Using a known sampling interval for volatility estimation.
Avoiding duplicated observations caused by multiple candidates.

Add replay/backtest tests that report calibration, Brier score, net PnL after fees, drawdown, fill assumptions, and sensitivity to stale or delayed quotes. Do not claim profitability without this evidence.

## P1 operational requirements
### 9. Fail closed on systemic errors
Replace the broad except Exception in the main loop with classified handling:

Stop trading on authentication, configuration, database, invariant, and reconciliation failures.
Retry only transient network and rate-limit failures.
Use bounded exponential backoff with jitter.
Add a circuit breaker after repeated failures.
Alert when no successful scan or reconciliation has occurred within a defined interval.

Add structured logs and metrics for scan age, reconciliation age, open orders, filled quantity, rejected orders, API errors, duplicate-prevention events, current exposure, and kill-switch state. Redact keys, account identifiers, and sensitive API response fields.

### 10. Add graceful shutdown
Handle SIGTERM and SIGINT. Stop creating new orders, persist state, reconcile outstanding orders according to policy, flush the ledger, and close HTTP/WebSocket clients cleanly. Ensure deployment restarts cannot lose order or fill state.

### 11. Make persistence production-safe
Require durable storage when running outside local development. Add:

Schema versioning and migrations.
SQLite busy timeout and clear transaction boundaries.
Uniqueness constraints for exchange order IDs and fill IDs.
Idempotency keys for reconciliation writes.
Backup/restore documentation.
A startup check that refuses production mode when the configured database path is ephemeral or unavailable.

### 12. Fix dependency and build reproducibility
Add every direct import to the dependency manifest, including httpx. Pin tested versions or provide a lock file. Test installation in a clean environment. Add linting, type checking, and a CI test command.

## Test suite to add
Add automated tests for:

Duplicate scans of the same ticker and signal.
Restart with open positions and open orders.
IOC full fill, zero fill, and partial fill.
GTC fill followed by cancellation.
HTTP timeout after exchange acceptance.
Rate-limit retry with idempotent order identity.
Malformed market quotes and stale quotes.
NaN, infinity, missing, and out-of-range LLM values.
YES and NO settlement outcomes.
Multiple fills and multiple trades on one ticker.
Fees and average fill price in PnL.
Daily-loss breach using realized and unrealized risk.
Database lock/retry and repeated reconciliation.
SIGTERM during scan, order submission, and reconciliation.
Production startup safety checks.

The key invariant is:

The bot must never submit an order when locally reconstructed worst-case exposure plus the proposed order exceeds the configured limit, and it must never count an order as executed until confirmed fills are reconciled.

## Delivery requirements
Implement the changes in small, reviewable commits or clearly separated patches. For every change, explain the affected files and the safety invariant it protects. Show the test output. Keep production trading disabled unless all P0 requirements and the demo integration tests pass. Do not remove warnings or weaken validation merely to make tests pass.

Before claiming completion, provide:

A changed-file summary.
A state-machine diagram or textual description for signal → order → fill → settlement.
The exact risk formulas and exposure definitions.
Demo-environment integration-test results.
Known limitations and remaining manual checks.
Confirmation that DRY_RUN=true remains the default.
