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

from config import CONFIG
from core.kalshi_client import KalshiAPIError, KalshiClient, KalshiTimeoutError
from core.order_state import OrderRecord, OrderState
from core.validation import parse_timestamp
from memory.edge_store import EdgeStore, EdgeRecord
from memory.order_store import OrderStore
from workers.checker import Verdict
from workers.risk_guardrail import RiskDecision

log = logging.getLogger("daemon_kalshi.ledger")


def _close_epoch(candidate) -> Optional[float]:
    """Market close time as epoch seconds, or None if unparseable.

    Stored on every edge row so reconcile_forecasts can ask only about markets
    that could have resolved. Candidates carry close_time as an ISO string;
    the ledger needs a number it can compare in SQL.
    """
    return parse_timestamp(getattr(candidate, "close_time", None))

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
        price, side = _counterfactual_entry(p)
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
                counterfactual_price_cents=price,
                counterfactual_direction=side,
                close_time=_close_epoch(c),
            )
        )
        log.info("Logged edge #%d for %s (%s: %s)", edge_id, c.ticker, action, decision.reason)
        return edge_id

    def log_refused_proposal(self, proposal, action: str, reason: str) -> int:
        """Record a proposal killed before it reached the Checker.

        The coherence gates run between Maker and Checker, so there is no
        verdict to attach — but the row still belongs on disk. A proposal
        refused for being arithmetically impossible is the most diagnostic
        thing the model produces, and dropping it would leave the ledger
        showing a quiet pass rather than a model that needs fixing.

        ``checker_*`` stay null, which is what distinguishes these rows: the
        Checker was never asked.
        """
        c = proposal.candidate
        price, side = _counterfactual_entry(proposal)
        edge_id = self.store.record_edge(
            EdgeRecord(
                ticker=c.ticker,
                category=c.category,
                source=proposal.source,
                maker_probability=proposal.maker_probability,
                maker_reasoning=proposal.reasoning,
                market_implied_probability=c.implied_yes_probability,
                edge_size=proposal.edge_size,
                checker_verdict=None,
                checker_confidence=None,
                checker_reasoning=None,
                action_taken=action,
                entry_price=None,
                size_contracts=0,
                counterfactual_price_cents=price,
                counterfactual_direction=side,
                close_time=_close_epoch(c),
            )
        )
        log.info("Logged edge #%d for %s (%s: %s)", edge_id, c.ticker, action, reason)
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

    def reconcile_forecasts(self, max_tickers: int = None) -> int:
        """Grade predictions that never became positions.

        Returns the number of edge rows newly settled.

        Bounded on purpose. Rows accumulate at roughly a hundred an hour and
        most resolve days later, so an unbounded sweep would spend the whole
        rate-limit budget re-asking about markets that are still open. Work is
        deduplicated by ticker — many rows share one market — and capped per
        call, oldest first, so every row is reached eventually without any
        single pass being expensive.

        PnL written here is COUNTERFACTUAL: what one contract at the quote
        available when the decision was made would have returned, net of the
        modelled fee. It is stored on rows whose action_taken is not
        'executed', which is what keeps it out of the live figures.
        """
        cap = (max_tickers if max_tickers is not None
               else CONFIG.risk.forecast_reconcile_max_tickers)
        if cap <= 0:
            return 0
        pending = self.store.unsettled_forecast_edges()
        if not pending:
            return 0

        by_ticker: dict[str, list[dict]] = {}
        for row in pending:
            by_ticker.setdefault(row["ticker"], []).append(row)
            if len(by_ticker) >= cap:
                break

        settled = 0
        for ticker in by_ticker:
            result = self._market_result(ticker)
            if not result:
                continue
            for row in by_ticker[ticker]:
                pnl = _counterfactual_pnl(row, result["result"])
                self.store.settle(row["id"], result["result"], pnl)
                settled += 1
                # Each graded row, individually.
                #
                # The aggregate count alone cannot answer the question these
                # rows exist for: "the Checker rejected a 45-point edge on
                # KXHIGHCHI-T78 — was it right?" That needs the ticker, what
                # the model said, what happened, and what the refusal cost or
                # saved. Cheap: this fires only when a market actually
                # resolves, a few times an hour.
                log.info(
                    "Settled forecast %s (%s/%s): model said %.0f%%, market "
                    "%.0f%%, outcome %s, counterfactual PnL $%.2f",
                    row["ticker"], row.get("category") or "?",
                    row.get("action_taken") or "?",
                    (row.get("maker_probability") or 0.0) * 100,
                    (row.get("market_implied_probability") or 0.0) * 100,
                    result["result"].upper(), pnl or 0.0,
                )
        if settled:
            log.info("Graded %d forecast row(s) across %d resolved market(s)",
                     settled, len(by_ticker))
            self._log_calibration()
        return settled

    def _log_calibration(self) -> None:
        """Print the calibration table whenever new rows land in it.

        Nothing surfaced this before, so the one measurement built to say
        whether the gates are refusing correctly could only be read by opening
        the database on the production volume — which is not somewhere an
        operator, or a future session, is going to look. Every other number
        that mattered today was wrong in a way the logs did not show, which is
        precisely the failure this closes.

        Emitted on settlement rather than per pass, so it appears exactly when
        it has changed.
        """
        try:
            rows = self.store.calibration_by_category()
        except Exception:
            log.exception("Could not read the calibration table — continuing")
            return
        if not rows:
            return
        for row in sorted(rows, key=lambda r: (r["mode"], -(r["n"] or 0))):
            log.info(
                "Calibration [%s] %s/%s: n=%d  brier=%.3f  said %.0f%% "
                "actual %.0f%%  pnl $%.2f",
                row["mode"], row["category"] or "?", row["source"] or "?",
                row["n"] or 0,
                row["brier_score"] if row["brier_score"] is not None else -1.0,
                (row["avg_maker_probability"] or 0.0) * 100,
                (row["actual_yes_rate"] or 0.0) * 100,
                row["total_pnl"] or 0.0,
            )

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


def _counterfactual_entry(proposal) -> tuple[Optional[float], Optional[str]]:
    """The price and side this proposal would have traded at, right now.

    Captured at decision time because it cannot be recovered afterwards: the
    book moves, and by settlement the quote that was actually available is
    gone. Without it a forecast row can be graded for accuracy but not for
    profitability, and "the model was well calibrated but the price was never
    there" is exactly the failure worth catching.

    Returns (None, None) rather than guessing when the quote is unusable —
    a fabricated entry price would make paper PnL look real.
    """
    try:
        side = proposal.direction
        price = proposal.candidate.executable_price_cents(side)
    except Exception:                        # noqa: BLE001 - never break logging
        return None, None
    if price is None or not (0.0 < float(price) < CONTRACT_PAYOUT_CENTS):
        return None, None
    return float(price), side


def _counterfactual_pnl(row: dict, outcome: str) -> Optional[float]:
    """Per-contract PnL the recorded quote would have produced.

    Returns None when no usable entry price was captured — a row can still be
    graded for calibration on its probability alone, and inventing a price to
    fill the column would be worse than leaving it empty.

    Uses the same fee function the live path subtracts, so a paper result is
    not flattered by pretending trading is free.
    """
    from core.pricing import fee_cents_per_contract

    price = row.get("counterfactual_price_cents")
    side = (row.get("counterfactual_direction") or "").lower()
    if price is None or side not in ("yes", "no"):
        return None
    price = float(price)
    won = side == outcome
    gross = (CONTRACT_PAYOUT_CENTS - price) if won else -price
    return (gross - fee_cents_per_contract(price)) / 100.0


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
