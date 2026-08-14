"""
DÆMON-KALSHI orchestrator. One pass = Scout -> Maker -> Checker -> Risk
Guardrail -> Execution -> Ledger, looped on SCOUT_POLL_SECONDS. Run with
--dry-run to force paper mode regardless of the DRY_RUN env var (useful for
a first live test against demo-api.kalshi.co without editing env vars).

Startup order is a safety property, not a convenience: the bot reconciles
with Kalshi before the first scan and refuses to run if that fails. Every
pass re-reconciles before evaluating any candidate, and risk is evaluated
against that reconciled snapshot rather than a process-local counter. If
account state cannot be verified, the pass places no orders.
"""
from __future__ import annotations

import argparse
import logging
import time

from config import CONFIG
from core.account_state import AccountState, ReconciliationError
from core.kalshi_client import KalshiClient
from memory.edge_store import EdgeStore
from memory.order_store import OrderStore
from workers.scout import Scout
from workers.maker import Maker
from workers.checker import Checker
from workers.risk_guardrail import RiskGuardrail, KillSwitchTripped
from workers.execution import (
    DuplicateOrderBlocked,
    Execution,
    UnmanagedMakerMode,
    assert_order_strategy_supported,
)
from workers.ledger import Ledger
from workers.reflect import Reflector
from workers.context import ContextEnricher
from core.espn_client import ESPNClient
from core.weather_client import NOAAClient
from core.fred_client import FredClient
from core.spot_price_client import SpotPriceClient
from workers.quant_maker import QuantMaker

logging.basicConfig(
    level=CONFIG.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("daemon_kalshi.main")


def run_once(scout, maker, quant_maker, checker, risk, execution, ledger, account):
    """One scan pass. Returns the number of orders that actually filled.

    Reconciliation happens first: if it fails, the pass places no orders at
    all rather than trading on a stale picture.
    """
    try:
        snapshot = account.reconcile()
    except ReconciliationError as e:
        log.error("Reconciliation failed — placing no orders this pass: %s", e)
        return 0
    if not snapshot.is_tradeable:
        log.error("Not safe to trade this pass: %s", snapshot.blocking_reason())
        return 0

    candidates = scout.scan()
    log.info("Scout returned %d candidates", len(candidates))

    def _is_priority(c):
        title = c.title.lower()
        return any(kw in title for kw in CONFIG.priority_keywords)

    # Priority candidates (golf, by default) always go first and are never
    # subject to the LLM call cap below — everything else fills remaining
    # budget in whatever order Scout returned it.
    candidates.sort(key=lambda c: not _is_priority(c))

    filled_this_pass = 0
    llm_calls_this_pass = 0
    for candidate in candidates:
        # Route: quant path for markets with a live spot feed + numeric
        # strike (fast, no LLM); LLM path only for categories where Maker
        # actually has something to reason from. Everything else gets
        # skipped outright — better to pass on a market than have Maker
        # guess with no real information behind it (e.g. GPU rental pricing,
        # gas prices with no live feed wired). Trading a market you have no
        # real edge in isn't a smaller edge, it's fee-paying speculation.
        proposal = None
        if quant_maker.can_handle(candidate):
            quant_result = quant_maker.propose(candidate)
            if quant_result:
                proposal = quant_result.to_maker_proposal()
        elif candidate.category.lower() in CONFIG.llm_reasoning_categories:
            is_priority = _is_priority(candidate)
            if not is_priority and CONFIG.max_llm_calls_per_pass and llm_calls_this_pass >= CONFIG.max_llm_calls_per_pass:
                log.debug("LLM call cap (%d) reached this pass — skipping non-priority %s",
                          CONFIG.max_llm_calls_per_pass, candidate.ticker)
                continue
            proposal = maker.propose(candidate)
            llm_calls_this_pass += 1
        else:
            log.debug(
                "Skipping %s [%s] — no quant path and category isn't in "
                "LLM_REASONING_CATEGORIES (no grounding data source)",
                candidate.ticker, candidate.category,
            )
            continue

        if not proposal:
            continue
        log.info(
            "Maker edge: %s -> %.2f%% (market %.2f%%, edge %.2f%%)",
            candidate.ticker, proposal.maker_probability * 100,
            candidate.implied_yes_probability * 100, proposal.edge_size * 100,
        )

        verdict = checker.check(proposal)
        log.info("Checker verdict on %s: %s (conf %.2f)", candidate.ticker, verdict.verdict, verdict.confidence)

        # Risk runs against the snapshot as it stands right now, including
        # every order already placed earlier in this same pass — that is why
        # the snapshot is refreshed after each fill rather than reused.
        try:
            decision = risk.evaluate(verdict, account.snapshot)
        except KillSwitchTripped as e:
            log.error("%s — halting this pass", e)
            break

        edge_id = ledger.log_decision(verdict, decision)
        if not decision.approved:
            log.info("Risk refused %s: %s", candidate.ticker, decision.reason)
            continue

        decision.edge_id = edge_id
        try:
            record = execution.execute(verdict, decision)
        except DuplicateOrderBlocked as e:
            # The 30-second loop re-derives the same candidate while a signal
            # persists; this is the guard that keeps that from stacking orders.
            log.info("Skipping duplicate order for %s: %s", candidate.ticker, e)
            continue
        except ReconciliationError as e:
            log.error("Execution refused for %s: %s — ending pass", candidate.ticker, e)
            break
        except Exception:
            log.exception("Order submission failed for %s — ending pass", candidate.ticker)
            break

        ledger.record_execution(edge_id, record)
        if record.filled_count > 0:
            filled_this_pass += 1

        # Re-reconcile so the next candidate is evaluated against exposure
        # that includes what just filled. Without this, N candidates in one
        # pass could each be approved against the same pre-trade exposure.
        try:
            account.reconcile()
        except ReconciliationError as e:
            log.error("Post-trade reconciliation failed — ending pass: %s", e)
            break

    return filled_this_pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="force paper mode for this run")
    parser.add_argument("--once", action="store_true", help="run a single pass instead of looping")
    parser.add_argument("--bankroll", type=float, default=1000.0,
                        help="operator ceiling in USD; effective bankroll is the lesser "
                             "of this and the real exchange balance")
    parser.add_argument("--reflect-every", type=int, default=200, help="run reflection every N passes (0 disables)")
    args = parser.parse_args()

    if args.dry_run:
        CONFIG.risk.dry_run = True

    # Refuse maker mode before anything connects. Execution re-checks this at
    # the point of submission, since config is mutable at runtime.
    try:
        assert_order_strategy_supported()
    except UnmanagedMakerMode as e:
        raise SystemExit(str(e))

    client = KalshiClient()
    store = EdgeStore()
    order_store = OrderStore()
    account = AccountState(client, order_store)

    scout = Scout(client)
    enricher = ContextEnricher(
        espn=ESPNClient(),
        weather=NOAAClient(),
        fred=FredClient() if CONFIG.models.fred_api_key else None,
    )
    maker = Maker(enricher=enricher)
    quant_maker = QuantMaker(SpotPriceClient())
    checker = Checker()
    risk = RiskGuardrail(bankroll_usd=args.bankroll, store=store, order_store=order_store)
    execution = Execution(client, order_store, account)
    ledger = Ledger(client, store, order_store)
    reflector = Reflector(store)

    log.info(
        "DÆMON-KALSHI starting | env=%s dry_run=%s strategy=%s categories=%s",
        CONFIG.kalshi.env, CONFIG.risk.dry_run, CONFIG.risk.order_strategy,
        CONFIG.scout_categories,
    )

    # Startup reconciliation: refuse to start rather than trade against an
    # unknown account. This is also what rebuilds exposure from positions and
    # orders opened by a previous process — the state a restart used to lose.
    try:
        snapshot = account.reconcile()
    except ReconciliationError as e:
        raise SystemExit(
            f"Startup reconciliation with Kalshi failed: {e}\n"
            f"Refusing to start — the bot cannot know its own exposure."
        )
    if not snapshot.is_tradeable:
        raise SystemExit(
            f"Startup reconciliation succeeded but the account is not safe to "
            f"trade: {snapshot.blocking_reason()}\n"
            f"Resolve this before starting (see docs/SAFETY.md)."
        )
    log.info(
        "Startup state: $%.2f balance, %d open position(s), %d live order(s), "
        "$%.2f worst-case exposure",
        snapshot.balance_cents / 100, len(snapshot.positions),
        len(snapshot.open_orders), snapshot.worst_case_exposure_cents() / 100,
    )

    pass_count = 0
    while True:
        try:
            run_once(scout, maker, quant_maker, checker, risk, execution, ledger, account)
            ledger.reconcile_settlements()
            pass_count += 1
            if args.reflect_every and pass_count % args.reflect_every == 0:
                reflector.reflect()
        except KillSwitchTripped as e:
            # Persisted and requires a human to clear, so retrying the loop
            # would just spin. Exit loudly instead.
            raise SystemExit(f"Kill switch tripped: {e}")
        except Exception:
            log.exception("Unhandled error in main loop pass — continuing")

        if args.once:
            break
        time.sleep(CONFIG.scout_poll_seconds)


if __name__ == "__main__":
    main()
