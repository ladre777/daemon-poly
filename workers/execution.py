"""
Execution: takes an approved RiskDecision and actually places the order.
Respects CONFIG.risk.dry_run — defaults to True so a fresh checkout never
fires real orders until you deliberately flip it off in the environment.

ORDER_STRATEGY matters more than it might look: Becker's 72.1M-trade Kalshi
analysis found liquidity TAKERS lose ~1.12% on average while MAKERS gain
~1.12% — the documented edge sits on the passive side of the spread, not the
aggressive side. "taker" mode (IOC, priced at the current ask) matches what
was here before — fills immediately, no execution risk, but gives up the
maker-side edge and pays the taker fee. "maker" mode (GTC, priced at the
current bid instead of ask) doesn't cross the spread — cheaper, potentially
captures the documented edge, but the order might never fill if price moves
away, and this file doesn't yet track/cancel/requote unfilled resting
orders. That's real order-lifecycle management, not a config toggle — treat
"maker" mode here as a starting point, not a complete solution, until
that's built.
"""
from __future__ import annotations

import logging
import uuid

from core.kalshi_client import KalshiClient, KalshiAPIError
from config import CONFIG
from workers.checker import Verdict
from workers.risk_guardrail import RiskDecision

log = logging.getLogger("daemon_kalshi.execution")


class Execution:
    def __init__(self, client: KalshiClient = None):
        self.client = client or KalshiClient()

    def execute(self, verdict: Verdict, decision: RiskDecision) -> dict:
        c = verdict.proposal.candidate
        direction = verdict.proposal.direction
        client_order_id = str(uuid.uuid4())

        price_field = "yes_price_dollars" if direction == "yes" else "no_price_dollars"
        strategy = CONFIG.risk.order_strategy
        if strategy == "maker":
            # Price at the passive side (the bid, not the ask) so the order
            # rests instead of crossing the spread — this is what makes it a
            # maker order rather than a relabeled taker order.
            price_cents = c.yes_bid if direction == "yes" else (100 - c.yes_ask)
            time_in_force = "GTC"
        else:
            price_cents = c.yes_ask if direction == "yes" else (100 - c.yes_bid)
            time_in_force = "IOC"
        price_dollars = f"{price_cents / 100:.2f}"

        if CONFIG.risk.dry_run:
            log.info(
                "[DRY RUN] [%s] Would BUY %d %s contracts on %s @ $%s (client_order_id=%s)",
                strategy, decision.size_contracts, direction.upper(), c.ticker, price_dollars, client_order_id,
            )
            return {
                "dry_run": True,
                "ticker": c.ticker,
                "direction": direction,
                "count": decision.size_contracts,
                "price_dollars": price_dollars,
                "strategy": strategy,
                "client_order_id": client_order_id,
            }

        try:
            order = self.client.place_order(
                ticker=c.ticker,
                action="buy",
                side=direction,
                count=decision.size_contracts,
                order_type="limit",
                time_in_force=time_in_force,
                client_order_id=client_order_id,
                **{price_field: price_dollars},
            )
            log.info("Order placed [%s]: %s", strategy, order)
            return order
        except KalshiAPIError as e:
            log.error("Order failed for %s: %s", c.ticker, e)
            raise
