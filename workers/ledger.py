"""
Ledger: records every decision (executed, skipped, dry-run) to edge memory,
and periodically reconciles open positions against Kalshi's settlement data
to write back outcomes + PnL — this is the writeback half of the memory loop
that lets Risk Guardrail's PF-09 calibration check mean something over time.
"""
from __future__ import annotations

import logging

from core.kalshi_client import KalshiClient
from memory.edge_store import EdgeStore, EdgeRecord
from workers.checker import Verdict
from workers.risk_guardrail import RiskDecision

log = logging.getLogger("daemon_kalshi.ledger")


class Ledger:
    def __init__(self, client: KalshiClient = None, store: EdgeStore = None):
        self.client = client or KalshiClient()
        self.store = store or EdgeStore()

    def log_decision(self, verdict: Verdict, decision: RiskDecision, order: dict = None) -> int:
        p = verdict.proposal
        c = p.candidate
        action = "skipped_risk" if not decision.approved else ("dry_run" if order and order.get("dry_run") else "executed")
        edge_id = self.store.record_edge(
            EdgeRecord(
                ticker=c.ticker,
                category=c.category,
                source=p.source,
                maker_probability=p.maker_probability,
                maker_reasoning=p.reasoning,
                market_implied_probability=c.implied_yes_probability,
                edge_size=p.edge_size,
                checker_verdict=verdict.verdict,
                checker_confidence=verdict.confidence,
                checker_reasoning=verdict.reasoning,
                action_taken=action,
                entry_price=float(order["price_dollars"]) if order else None,
                size_contracts=decision.size_contracts if decision.approved else 0,
            )
        )
        log.info("Logged edge #%d for %s (%s)", edge_id, c.ticker, action)
        return edge_id

    def reconcile_settlements(self):
        """Pull settled positions from Kalshi and write outcomes back into
        edge memory for every open (unsettled-in-our-DB) edge that matches.

        FIXED (was a real correctness bug, not just an incompleteness): the
        previous version guessed the outcome from `resting_orders_count` or
        the sign of `realized_pnl`. PnL sign depends on which side YOU held,
        not which side won — a NO position that resolves NO also shows
        positive PnL. That would have silently mislabeled outcomes and
        corrupted every Brier-score number the calibration system depends
        on. Now pulls the market's actual `result` field instead of
        inferring anything.

        STILL PARTIAL: if there's more than one unsettled 'executed' edge
        for the same ticker, this can't yet tell which fill each one
        corresponds to — proper fix needs fill-level reconciliation (order
        ID / fill ID tracking), which is a bigger lift than this method
        alone. Until that exists, ambiguous cases (>1 match) are logged and
        left unsettled rather than guessing which row gets which PnL — an
        unsettled row is a visible gap; a wrongly-settled one is a silent
        one, and silent is worse here.
        """
        positions = self.client.get_positions(settlement_status="settled")
        for pos in positions.get("market_positions", []):
            ticker = pos["ticker"]
            realized_pnl = float(pos.get("realized_pnl", 0)) / 100.0  # cents -> dollars

            try:
                market = self.client.get_market(ticker)
                # Assumes the response wraps the object as {"market": {...}}
                # matching this client's other endpoints (list_markets ->
                # {"markets": [...]}, etc.) — not yet verified against a live
                # response, confirm this shape against demo before trusting it.
                outcome = market.get("market", {}).get("result")
            except Exception:
                log.exception("Couldn't fetch market result for %s — skipping settlement this pass", ticker)
                continue
            if outcome not in ("yes", "no"):
                log.warning("No definitive result yet for %s (got %r) — skipping", ticker, outcome)
                continue

            matches = [
                e for e in self.store.recent_edges(ticker=ticker, limit=5)
                if not e["settled"] and e["action_taken"] == "executed"
            ]
            if len(matches) > 1:
                log.warning(
                    "%d ambiguous unsettled edges for %s — can't attribute PnL to a specific "
                    "one without fill-level tracking, leaving all unsettled rather than guessing",
                    len(matches), ticker,
                )
                continue
            for edge in matches:
                self.store.settle(edge["id"], outcome, realized_pnl)
                log.info("Settled edge #%d (%s): outcome=%s pnl=%.2f", edge["id"], ticker, outcome, realized_pnl)
