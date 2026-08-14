"""
Ledger: records every decision to edge memory and reconciles settlements
against exchange-confirmed results, fill by fill.

What this replaces
------------------
The original version inferred a market's outcome from ``resting_orders_count``
or the sign of ``realized_pnl``. PnL sign depends on which side you held, not
which side won — a NO position that resolves NO also shows positive PnL — so
that inference silently mislabelled outcomes and corrupted every Brier score
the calibration system depends on. It then applied one aggregate ticker-level
PnL to whichever recent database row it happened to match, and gave up
entirely when more than one row matched.

Settlement is now computed per fill from the exchange's own result:

    held side wins:  pnl = count * (100c - fill_price) - fees
    held side loses: pnl = -(count * fill_price) - fees

Each fill carries its own price, so two entries on one ticker at different
prices settle to different PnL instead of sharing an average. ``fill_id`` is
the idempotency key, so re-running reconciliation is a no-op rather than
double-counted PnL.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.kalshi_client import KalshiAPIError, KalshiClient, KalshiTimeoutError
from core.order_state import OrderRecord, OrderState
from memory.edge_store import EdgeStore, EdgeRecord
from memory.order_store import OrderStore
from workers.checker import Verdict
from workers.risk_guardrail import RiskDecision

log = logging.getLogger("daemon_kalshi.ledger")

#: A settled binary contract pays out this much per contract.
CONTRACT_PAYOUT_CENTS = 100.0


class Ledger:
    def __init__(
        self,
        client: KalshiClient = None,
        store: EdgeStore = None,
        order_store: OrderStore = None,
    ):
        self.client = client or KalshiClient()
        self.store = store or EdgeStore()
        self.order_store = order_store or OrderStore()

    # -- decision logging ---------------------------------------------------

    def log_decision(self, verdict: Verdict, decision: RiskDecision) -> int:
        """Record the decision *before* execution.

        Written first so the edge row exists to attach an order to, and so a
        crash between decision and submission still leaves the reasoning on
        disk. Approved rows start as ``pending``; :meth:`record_execution`
        replaces that with what actually happened.
        """
        p = verdict.proposal
        c = p.candidate
        action = "pending" if decision.approved else "skipped_risk"
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
                entry_price=None,
                size_contracts=0,
            )
        )
        log.info("Logged edge #%d for %s (%s: %s)", edge_id, c.ticker, action, decision.reason)
        return edge_id

    def record_execution(self, edge_id: int, record: OrderRecord) -> None:
        """Attach the reconciled order outcome to its decision row.

        Only confirmed fills are recorded as ``executed``. A zero-fill IOC
        becomes ``no_fill`` and stays out of the calibration queries, which
        count executed rows only — counting an unfilled order as a trade would
        pollute both the Brier score and the PnL history with a trade that
        never happened.
        """
        if record.dry_run:
            action = "dry_run"
        elif record.state is OrderState.REJECTED:
            action = "rejected"
        elif record.filled_count > 0:
            action = "executed"
        else:
            action = "no_fill"

        entry_price = (
            record.avg_fill_price_cents / 100.0
            if record.filled_count > 0 and record.avg_fill_price_cents
            else None
        )
        self.store.update_execution(
            edge_id,
            action_taken=action,
            entry_price=entry_price,
            size_contracts=record.filled_count,
            client_order_id=record.client_order_id,
        )
        log.info(
            "Edge #%d -> %s (%d/%d filled, order %s)",
            edge_id, action, record.filled_count, record.requested_count,
            record.client_order_id,
        )

    # -- settlement reconciliation -----------------------------------------

    def reconcile_settlements(self) -> int:
        """Settle every stored fill whose market the exchange has resolved.

        Returns the number of newly written settlement rows. Safe to call on
        every pass: already-settled fills are skipped by the ``settled`` flag
        and re-blocked by the UNIQUE ``settlement_key`` if they somehow slip
        through.
        """
        unsettled = self.order_store.unsettled_fills()
        if not unsettled:
            return 0

        by_ticker: dict[str, list[dict]] = {}
        for fill in unsettled:
            by_ticker.setdefault(fill["ticker"], []).append(fill)

        results = self._settlement_results(list(by_ticker))
        written = 0
        for ticker, fills in by_ticker.items():
            result = results.get(ticker)
            if result is None:
                log.debug("No settlement result yet for %s — leaving %d fill(s) open",
                          ticker, len(fills))
                continue
            outcome = result.get("result")
            if outcome not in ("yes", "no"):
                log.warning(
                    "Non-definitive result %r for %s — leaving fills unsettled",
                    outcome, ticker,
                )
                continue
            for fill in fills:
                if self._settle_fill(ticker, fill, result):
                    written += 1

        if written:
            self._writeback_edges()
        return written

    def _settle_fill(self, ticker: str, fill: dict, result: dict) -> bool:
        side = (fill["side"] or "").lower()
        if side not in ("yes", "no"):
            log.warning(
                "Fill %s on %s has no usable side (%r) — cannot compute PnL, skipping",
                fill["fill_id"], ticker, fill["side"],
            )
            return False

        count = int(fill["count"])
        price = float(fill["price_cents"])
        fees = float(fill["fees_cents"] or 0.0)
        won = side == result["result"]
        gross_cents = (
            count * (CONTRACT_PAYOUT_CENTS - price) if won else -(count * price)
        )
        realized_pnl = (gross_cents - fees) / 100.0

        wrote = self.order_store.record_settlement(
            # One row per fill per market resolution: the fill can only settle
            # once, so this key makes repeated reconciliation a no-op.
            settlement_key=f"{ticker}:{fill['fill_id']}",
            ticker=ticker,
            market_id=result.get("market_id"),
            client_order_id=fill["client_order_id"],
            exchange_order_id=fill["exchange_order_id"],
            fill_id=fill["fill_id"],
            side=side,
            action=fill["action"],
            fill_count=count,
            fill_price_cents=price,
            fees_cents=fees,
            settlement_result=result["result"],
            realized_pnl=realized_pnl,
            settled_at=result.get("settled_at"),
        )
        if wrote:
            log.info(
                "Settled fill %s on %s: held %s, result %s, %d @ %.0fc -> PnL $%.2f",
                fill["fill_id"], ticker, side.upper(), result["result"].upper(),
                count, price, realized_pnl,
            )
        return wrote

    def _settlement_results(self, tickers: list[str]) -> dict[str, dict]:
        """Authoritative outcomes, preferring the settlements endpoint.

        Falls back to each market's own ``result`` field when the settlements
        endpoint is unavailable — some demo accounts do not expose it. Both
        paths read an explicit result from the exchange; neither infers one.
        """
        results: dict[str, dict] = {}
        wanted = set(tickers)
        try:
            cursor = None
            while wanted:
                page = self.client.get_settlements(limit=200, cursor=cursor) or {}
                for row in page.get("settlements", []):
                    ticker = row.get("ticker")
                    if ticker not in wanted:
                        continue
                    outcome = (row.get("market_result") or row.get("result") or "").lower()
                    results[ticker] = {
                        "result": outcome,
                        "market_id": row.get("market_id"),
                        "settled_at": _ts(row.get("settled_time")),
                    }
                    wanted.discard(ticker)
                cursor = page.get("cursor")
                if not cursor:
                    break
        except (KalshiAPIError, KalshiTimeoutError) as e:
            log.warning("Settlements endpoint unavailable (%s) — falling back to market results", e)

        for ticker in wanted:
            fallback = self._market_result(ticker)
            if fallback:
                results[ticker] = fallback
        return results

    def _market_result(self, ticker: str) -> Optional[dict]:
        try:
            payload = self.client.get_market(ticker) or {}
        except (KalshiAPIError, KalshiTimeoutError) as e:
            log.warning("Couldn't fetch market result for %s: %s", ticker, e)
            return None
        market = payload.get("market", payload)
        outcome = (market.get("result") or "").lower()
        if outcome not in ("yes", "no"):
            return None
        return {
            "result": outcome,
            "market_id": market.get("market_id") or market.get("ticker"),
            "settled_at": _ts(market.get("close_time")),
        }

    def _writeback_edges(self) -> None:
        """Roll fill-level settlements up into the edge rows that produced
        them, so PF-09 calibration sees real outcomes.

        Each edge is matched by its own ``client_order_id`` — the link
        established when the order was placed — rather than by guessing which
        recent row on this ticker a settlement belongs to.
        """
        for edge in self.store.unsettled_executed_edges():
            client_order_id = edge.get("client_order_id")
            if not client_order_id:
                continue
            rows = [
                s for s in self.order_store.settlements_for_ticker(edge["ticker"])
                if s["client_order_id"] == client_order_id
            ]
            if not rows:
                continue
            pnl = sum(float(r["realized_pnl"] or 0.0) for r in rows)
            outcome = rows[0]["settlement_result"]
            self.store.settle(edge["id"], outcome, pnl)
            log.info(
                "Edge #%d settled from %d fill-level settlement(s): outcome=%s pnl=$%.2f",
                edge["id"], len(rows), outcome, pnl,
            )


def _ts(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
