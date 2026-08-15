"""
Risk Guardrail: the last gate before Execution, enforced transactionally on
the reconciled account snapshot immediately before submission.

The invariant this file exists to hold:

    The bot must never submit an order when locally reconstructed worst-case
    exposure plus the proposed order exceeds the configured limit, and it must
    never count an order as executed until confirmed fills are reconciled.

What changed and why
--------------------
The previous version gated on ``open_positions >= max_open_positions`` — a
count. A count cannot distinguish 15 positions at $2 from 15 at $200, so the
account-level cap it appeared to enforce was not a dollar limit at all. It
also took that count from a process-local integer that reset to zero on every
redeploy, and sized against a ``--bankroll`` CLI value that had no connection
to the money actually in the account.

Risk is now computed in worst-case dollars against exchange-reconciled state:
existing positions, pending and resting orders, per-ticker, per-event and
per-category concentration, real available balance, plus fees and conservative
slippage. The position count survives as one cap among several, not as the
definition of exposure.

Fee model
---------
Kalshi's taker fee is approximately ``fee_rate * P * (1 - P)`` per contract
with ``P`` the price in dollars, peaking near 50c. It is charged into sizing
and into the executable-edge check up front, so a trade whose edge is thinner
than its fees is rejected rather than discovered afterwards.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import CONFIG
from core.account_state import AccountSnapshot
from core.pricing import (
    executable_price_cents as _executable_price_cents,
    fee_cents_per_contract as _fee_cents_per_contract,
    net_edge,
)
from memory.edge_store import EdgeStore
from memory.order_store import OrderStore
from workers.checker import Verdict

log = logging.getLogger("daemon_kalshi.risk")


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    size_contracts: int = 0
    #: Price we would actually pay per contract, before slippage/fees.
    executable_price_cents: float = 0.0
    #: Price sent to the exchange (executable price plus slippage allowance).
    limit_price_cents: float = 0.0
    #: Worst-case cash this order can cost us, fees and slippage included.
    projected_cost_cents: float = 0.0
    estimated_fees_cents: float = 0.0
    #: Worst-case exposure before and after this order, for the audit trail.
    current_exposure_cents: float = 0.0
    projected_exposure_cents: float = 0.0
    edge_id: Optional[int] = None
    detail: dict = field(default_factory=dict)


class KillSwitchTripped(Exception):
    pass


# Re-exported so existing callers and tests keep importing these from here,
# while Maker and risk share one definition (see core/pricing.py).
fee_cents_per_contract = _fee_cents_per_contract
executable_price_cents = _executable_price_cents


class RiskGuardrail:
    def __init__(
        self,
        bankroll_usd: float,
        store: EdgeStore = None,
        order_store: OrderStore = None,
        notifier=None,
    ):
        #: Optional TelegramClient. Alerting is best-effort and must never
        #: affect a risk decision, so every call is guarded and the absence
        #: of a notifier changes nothing.
        self.notifier = notifier
        #: Operator-declared ceiling. The effective bankroll is the lesser of
        #: this and the real exchange balance — the CLI value can only ever
        #: reduce risk, never authorise more than the account holds.
        self.declared_bankroll_usd = bankroll_usd
        self.store = store or EdgeStore()
        self.order_store = order_store or OrderStore()
        # Loaded from persisted state, not a bare in-memory flag — Railway
        # restarts on every redeploy, and an in-memory kill switch would
        # silently un-trip itself on the next push even if the underlying
        # daily-loss condition is still true.
        self._killed = self.store.load_kill_switch()["tripped"]

    # -- bankroll ----------------------------------------------------------

    def effective_bankroll_usd(self, account: AccountSnapshot) -> float:
        exchange_usd = account.balance_cents / 100.0
        return min(self.declared_bankroll_usd, exchange_usd)

    # -- market-quality and calibration rules ------------------------------

    def _pf04_market_quality(self, verdict: Verdict) -> RiskDecision:
        """PF-04 (placeholder): reject markets too thin or too wide to trade
        at the size Maker's edge would justify. Replace with the real PF-04
        definition from DÆMON-POLY."""
        c = verdict.proposal.candidate
        if c.spread > 8:  # cents
            return RiskDecision(False, f"PF-04 stub: spread {c.spread}c too wide")
        if c.volume < CONFIG.risk.min_liquidity_usd:
            return RiskDecision(False, "PF-04 stub: volume below floor")
        return RiskDecision(True, "PF-04 stub: pass")

    def _pf09_category_calibration(self, verdict: Verdict) -> RiskDecision:
        """PF-09 (placeholder): block (category, strategy) pairs where edge
        memory shows bad PnL or poor Brier-score calibration."""
        category = verdict.proposal.candidate.category
        source = verdict.proposal.source
        calibration = {
            (row["category"], row["source"]): row
            for row in self.store.calibration_by_category()
        }
        row = calibration.get((category, source))
        if row and row["n"] >= 10:
            if row["avg_pnl"] is not None and row["avg_pnl"] < 0:
                return RiskDecision(
                    False,
                    f"PF-09 stub: ({category}/{source}) has negative avg PnL over "
                    f"{row['n']} trades",
                )
            if row["brier_score"] is not None and row["brier_score"] > 0.28:
                # 0.25 is what a coin flip stating 50% always scores — above
                # that, Maker is actively worse than admitting it doesn't know.
                return RiskDecision(
                    False,
                    f"PF-09 stub: ({category}/{source}) Brier score "
                    f"{row['brier_score']:.3f} indicates poor calibration over "
                    f"{row['n']} trades",
                )
        return RiskDecision(True, "PF-09 stub: pass")

    def _longshot_bias_guard(self, verdict: Verdict) -> RiskDecision:
        """From Jonathan Becker's analysis of 72.1M Kalshi trades
        (jbecker.dev/research/prediction-market-microstructure): contracts
        priced under ~20c systematically underperform their implied odds — a
        documented favorite-longshot bias in Kalshi's own retail order flow.
        This doesn't block longshot trades, it raises the bar, since the crowd
        is usually wrong for a *reason* (optimism bias) at this end of the
        price range rather than randomly wrong.

        Applied to whichever side we are actually buying: a NO contract at 15c
        is exactly as much a longshot as a YES contract at 15c, and the old
        version only checked the YES case.
        """
        c = verdict.proposal.candidate
        direction = verdict.proposal.direction
        price = executable_price_cents(c, direction)
        if price < CONFIG.risk.longshot_price_threshold_cents:
            required_edge = (
                CONFIG.risk.min_edge_threshold * CONFIG.risk.longshot_edge_multiplier
            )
            if verdict.proposal.edge_size < required_edge:
                return RiskDecision(
                    False,
                    f"Longshot bias guard: {direction.upper()} at {price:.0f}c needs "
                    f"edge >= {required_edge:.2%} (has {verdict.proposal.edge_size:.2%}) "
                    f"— documented Kalshi-wide bias means a longshot needs to clear a "
                    f"higher bar, not just the standard threshold",
                )
        return RiskDecision(True, "longshot bias guard: pass")

    # -- drawdown kill switch ----------------------------------------------

    def realized_pnl_today(self) -> float:
        day_start = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        return self.order_store.realized_pnl_since(day_start)

    def check_kill_switch(self, bankroll_usd: float) -> bool:
        """True if trading must halt. Trips on *realized* losses breaching the
        daily limit, and persists the flag so a restart cannot clear it."""
        if self._killed:
            return True
        pnl_today = self.realized_pnl_today()
        loss_limit = -abs(CONFIG.risk.max_daily_loss_pct * bankroll_usd)
        if pnl_today <= loss_limit:
            self._killed = True
            reason = f"realized daily PnL {pnl_today:.2f} breached limit {loss_limit:.2f}"
            self.store.set_kill_switch(True, reason)
            log.error("KILL SWITCH TRIPPED (persisted): %s", reason)
            # Alerted at the moment of tripping rather than from the caller,
            # so it fires on every path that trips the switch. Wrapped
            # because an alerting failure must not stop the halt from taking
            # effect — the switch is already set and persisted above.
            if self.notifier is not None:
                try:
                    self.notifier.notify_kill_switch(reason, pnl_today, bankroll_usd)
                except Exception:
                    log.exception("Kill-switch alert failed — the halt still stands")
            return True
        return False

    def reset_kill_switch(self):
        """Manual reset only — never call this automatically from inside the
        trading loop. Requires a human decision to resume."""
        self._killed = False
        self.store.set_kill_switch(False)

    # -- exposure ----------------------------------------------------------

    def _exposure_gates(
        self, account: AccountSnapshot, candidate, bankroll_usd: float
    ) -> list[tuple[str, float, float]]:
        """(label, already_used_cents, cap_cents) for every concentration cap.

        Headroom on each of these caps bounds the order size, and every one is
        re-checked against the final size before approval.
        """
        bankroll_cents = bankroll_usd * 100.0
        ticker = candidate.ticker
        event = getattr(candidate, "event_ticker", "") or (
            ticker.rsplit("-", 1)[0] if "-" in ticker else ticker
        )
        category = candidate.category

        by_ticker = account.exposure_by_ticker_cents()
        by_event = account.exposure_by_event_cents()
        by_category = account.exposure_by_category_cents()

        gates = [
            (
                "total exposure",
                account.worst_case_exposure_cents(),
                bankroll_cents * CONFIG.risk.max_total_exposure_pct,
            ),
            (
                f"ticker {ticker}",
                by_ticker.get(ticker, 0.0),
                bankroll_cents * CONFIG.risk.max_ticker_exposure_pct,
            ),
            (
                f"event {event}",
                by_event.get(event, 0.0),
                bankroll_cents * CONFIG.risk.max_event_exposure_pct,
            ),
            (
                f"category {category}",
                by_category.get(category, 0.0) if category else 0.0,
                bankroll_cents * CONFIG.risk.max_category_exposure_pct,
            ),
            # Never commit more than the account can actually pay for. Kalshi
            # would reject it anyway; failing here keeps the refusal in our own
            # audit trail instead of as an exchange error.
            (
                "available balance",
                account.pending_exposure_cents(),
                account.available_balance_cents,
            ),
            # Losses already booked today shrink how much *new* risk may be
            # added, so a bad morning tightens the afternoon automatically.
            #
            # Deliberately not "realized PnL minus all open exposure against
            # the daily limit": open positions are not a realized loss, and
            # counting them as one would make MAX_TOTAL_EXPOSURE_PCT (50%)
            # unreachable under a 10% daily loss limit — the larger cap would
            # be dead code. This bounds new risk instead, which is the part a
            # pre-trade check can actually control.
            (
                "daily loss budget",
                max(-self.realized_pnl_today(), 0.0) * 100.0,
                abs(CONFIG.risk.max_daily_loss_pct * bankroll_cents),
            ),
        ]
        return gates

    # -- main gate ---------------------------------------------------------

    def evaluate(self, verdict: Verdict, account: AccountSnapshot) -> RiskDecision:
        """Approve or refuse one proposal against reconciled account state.

        Every refusal path returns a RiskDecision rather than raising, except
        the kill switch, which halts the whole pass.
        """
        # 1. Account state must be verified and fresh. Everything downstream
        #    is arithmetic on these numbers, so stale or self-contradictory
        #    input is a refusal, not a warning.
        if not account.is_tradeable:
            return RiskDecision(False, f"account state unusable: {account.blocking_reason()}")

        bankroll_usd = self.effective_bankroll_usd(account)
        if bankroll_usd <= 0:
            return RiskDecision(
                False,
                f"effective bankroll is ${bankroll_usd:.2f} (declared "
                f"${self.declared_bankroll_usd:.2f}, exchange "
                f"${account.balance_cents / 100:.2f})",
            )

        if self.check_kill_switch(bankroll_usd):
            raise KillSwitchTripped("Daily drawdown limit hit — trading halted")

        if not verdict.approved:
            return RiskDecision(False, f"Checker did not approve: {verdict.verdict}")

        for rule in (
            self._pf04_market_quality(verdict),
            self._pf09_category_calibration(verdict),
            self._longshot_bias_guard(verdict),
        ):
            if not rule.approved:
                return rule

        c = verdict.proposal.candidate
        direction = verdict.proposal.direction

        # The quote this decision was made against must still be current. A
        # proposal can sit in the LLM queue for seconds; approving it against
        # a price that has since moved is trading on a number that no longer
        # exists. Checked here, immediately before submission, rather than
        # only at scan time.
        quote = c.quote
        if quote is None:
            return RiskDecision(False, "candidate carries no quote — cannot verify price")
        if quote.is_stale():
            return RiskDecision(
                False,
                f"quote is {quote.age_seconds:.0f}s old (limit "
                f"{CONFIG.risk.max_quote_age_seconds:.0f}s) — refusing to trade on "
                f"a price that may have moved",
            )

        price = executable_price_cents(c, direction)
        if not (0 < price < 100):
            return RiskDecision(
                False, f"executable price {price:.1f}c is outside 0-100c — refusing"
            )

        # Edge is measured against the price we would actually pay, net of
        # fees and slippage — not the midpoint. The midpoint flatters every
        # trade by half the spread, so a 4pp "edge" on a 48/52 market is
        # roughly 2pp before costs and can be negative after them. Maker
        # screens on the same number; this is the enforcing check.
        net = net_edge(verdict.proposal.maker_probability, c, direction)
        if net < CONFIG.risk.min_edge_threshold:
            return RiskDecision(
                False,
                f"net edge {net:.2%} below the {CONFIG.risk.min_edge_threshold:.2%} "
                f"minimum (executable {verdict.proposal.executable_edge:.2%}, "
                f"midpoint would have shown {verdict.proposal.midpoint_edge:.2%})",
                executable_price_cents=price,
            )
        # 2. Budget the worst case per contract: what we pay, plus a slippage
        #    allowance, plus fees. Sizing against the raw price would let the
        #    real cost of a filled order exceed the cap it was approved under.
        limit_price = min(price + CONFIG.risk.slippage_cents, 99.0)
        fees_per_contract = fee_cents_per_contract(limit_price)
        cost_per_contract = limit_price + fees_per_contract

        # 3. Position cap and every concentration cap bound the size together.
        bankroll_cents = bankroll_usd * 100.0
        max_position_cents = bankroll_cents * CONFIG.risk.max_position_pct
        gates = self._exposure_gates(account, c, bankroll_usd)

        headroom_cents = max_position_cents
        binding = "max position size"
        for label, used, cap in gates:
            available = cap - used
            if available < headroom_cents:
                headroom_cents = available
                binding = label

        if headroom_cents <= 0:
            return RiskDecision(
                False,
                f"no exposure headroom: {binding} is already at its limit "
                f"(${headroom_cents / 100:.2f} available)",
                current_exposure_cents=account.worst_case_exposure_cents(),
            )

        size = int(headroom_cents // cost_per_contract)
        if size < 1:
            return RiskDecision(
                False,
                f"position sizing rounds to zero contracts: {binding} leaves "
                f"${headroom_cents / 100:.2f}, one contract costs "
                f"${cost_per_contract / 100:.2f} (price {limit_price:.0f}c + fees "
                f"{fees_per_contract:.1f}c)",
                current_exposure_cents=account.worst_case_exposure_cents(),
            )

        # The position-count cap survives as one gate among many. It stops
        # attention being spread across more markets than the operator wants
        # to supervise, which is a different concern from dollar exposure.
        if account.open_position_count() >= CONFIG.risk.max_open_positions:
            return RiskDecision(
                False,
                f"max open positions reached ({account.open_position_count()}/"
                f"{CONFIG.risk.max_open_positions})",
            )

        projected_cost = size * cost_per_contract
        current_exposure = account.worst_case_exposure_cents()
        projected_exposure = current_exposure + projected_cost

        # 4. Re-check every cap against the *final* size. The size was derived
        #    from the binding constraint, so this should always pass — which is
        #    exactly why it is worth asserting. This is the invariant check,
        #    and it runs immediately before submission.
        for label, used, cap in gates:
            if used + projected_cost > cap + 1e-6:
                return RiskDecision(
                    False,
                    f"invariant check failed: {label} would reach "
                    f"${(used + projected_cost) / 100:.2f} against a cap of "
                    f"${cap / 100:.2f}",
                    current_exposure_cents=current_exposure,
                    projected_exposure_cents=projected_exposure,
                )

        realized_today = self.realized_pnl_today()
        return RiskDecision(
            True,
            f"approved: {size} contracts @ {limit_price:.0f}c, binding constraint "
            f"'{binding}'",
            size_contracts=size,
            executable_price_cents=price,
            limit_price_cents=limit_price,
            projected_cost_cents=projected_cost,
            estimated_fees_cents=size * fees_per_contract,
            current_exposure_cents=current_exposure,
            projected_exposure_cents=projected_exposure,
            detail={
                "bankroll_usd": bankroll_usd,
                "binding_constraint": binding,
                "cost_per_contract_cents": cost_per_contract,
                "realized_pnl_today": realized_today,
                "reconciled_age_seconds": account.age_seconds,
                "evaluated_at": time.time(),
                # The exact quote this decision was made against, so the
                # audit trail says what price was used rather than leaving it
                # to be re-derived from a later, different quote.
                "quote_yes_bid": quote.yes_bid,
                "quote_yes_ask": quote.yes_ask,
                "quote_captured_at": quote.captured_at,
                "quote_age_seconds": quote.age_seconds,
                "quote_source": quote.source,
                "executable_edge": verdict.proposal.executable_edge,
                "midpoint_edge": verdict.proposal.midpoint_edge,
                "net_edge": net,
            },
        )
