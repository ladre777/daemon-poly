"""
DÆMON-KALSHI orchestrator. One pass = Scout -> Maker -> Checker -> Risk
Guardrail -> Execution -> Ledger, looped on SCOUT_POLL_SECONDS. Run with
--dry-run to force paper mode regardless of the DRY_RUN env var (useful for
a first live test against demo-api.kalshi.co without editing env vars).
"""
from __future__ import annotations

import argparse
import logging
import time

from config import CONFIG
from core.kalshi_client import KalshiClient
from memory.edge_store import EdgeStore
from workers.scout import Scout
from workers.maker import Maker
from workers.checker import Checker
from workers.risk_guardrail import RiskGuardrail, KillSwitchTripped
from workers.execution import Execution
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


def run_once(scout, maker, quant_maker, checker, risk, execution, ledger, open_positions_count):
    candidates = scout.scan()
    log.info("Scout returned %d candidates", len(candidates))

    def _is_priority(c):
        title = c.title.lower()
        return any(kw in title for kw in CONFIG.priority_keywords)

    # Priority candidates (golf, by default) always go first and are never
    # subject to the LLM call cap below — everything else fills remaining
    # budget in whatever order Scout returned it.
    candidates.sort(key=lambda c: not _is_priority(c))

    executed_this_pass = 0
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

        try:
            decision = risk.evaluate(verdict, open_positions_count + executed_this_pass)
        except KillSwitchTripped as e:
            log.error("%s — halting this pass", e)
            break

        order = None
        if decision.approved:
            order = execution.execute(verdict, decision)
            executed_this_pass += 1

        ledger.log_decision(verdict, decision, order)

    return executed_this_pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="force paper mode for this run")
    parser.add_argument("--once", action="store_true", help="run a single pass instead of looping")
    parser.add_argument("--bankroll", type=float, default=1000.0, help="starting bankroll in USD for sizing")
    parser.add_argument("--reflect-every", type=int, default=200, help="run reflection every N passes (0 disables)")
    args = parser.parse_args()

    if args.dry_run:
        CONFIG.risk.dry_run = True

    if CONFIG.risk.order_strategy == "maker":
        # Manus's review of this repo caught something I'd only half-flagged
        # myself: "maker" mode posts resting GTC orders with no existing-
        # order lookup, no TTL, no cancellation, no duplicate-prevention
        # across scan passes. Every 30-second pass that still finds the same
        # candidate would happily post another resting order on top of the
        # last one. That's not a smaller edge, it's an unbounded-exposure
        # bug waiting to happen. Refuse to start until that lifecycle
        # tracking actually exists rather than let it run "mostly fine."
        raise SystemExit(
            "ORDER_STRATEGY=maker is not safe to run yet: no open-order "
            "tracking, TTL, cancellation, or duplicate-order prevention "
            "exists in execution.py. Set ORDER_STRATEGY=taker, or implement "
            "that lifecycle management before enabling maker mode."
        )

    client = KalshiClient()
    store = EdgeStore()

    scout = Scout(client)
    enricher = ContextEnricher(
        espn=ESPNClient(),
        weather=NOAAClient(),
        fred=FredClient() if CONFIG.models.fred_api_key else None,
    )
    maker = Maker(enricher=enricher)
    quant_maker = QuantMaker(SpotPriceClient())
    checker = Checker()
    risk = RiskGuardrail(bankroll_usd=args.bankroll, store=store)
    execution = Execution(client)
    ledger = Ledger(client, store)
    reflector = Reflector(store)

    log.info(
        "DÆMON-KALSHI starting | env=%s dry_run=%s categories=%s",
        CONFIG.kalshi.env, CONFIG.risk.dry_run, CONFIG.scout_categories,
    )

    open_positions = 0  # TODO: seed from client.get_positions() on startup
    pass_count = 0
    while True:
        try:
            executed = run_once(scout, maker, quant_maker, checker, risk, execution, ledger, open_positions)
            open_positions += executed
            ledger.reconcile_settlements()
            pass_count += 1
            if args.reflect_every and pass_count % args.reflect_every == 0:
                reflector.reflect()
        except Exception:
            log.exception("Unhandled error in main loop pass — continuing")

        if args.once:
            break
        time.sleep(CONFIG.scout_poll_seconds)


if __name__ == "__main__":
    main()
