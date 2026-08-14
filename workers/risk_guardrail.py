"""
Risk Guardrail: the last gate before Execution. Even a Checker-approved
proposal has to clear position sizing, exposure caps, and the drawdown kill
switch here.

IMPORTANT — PF-04 / PF-09 / PF-10 are placeholders, not ported. I only had
the rule *names* to go on, not their actual logic, so I've labeled each stub
with what a rule of that name would plausibly gate in this kind of system.
Replace the bodies with your real DÆMON-POLY logic, or send it to me and
I'll port it exactly instead of guessing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from config import CONFIG
from memory.edge_store import EdgeStore
from workers.checker import Verdict

log = logging.getLogger("daemon_kalshi.risk")


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    size_contracts: int = 0


class KillSwitchTripped(Exception):
    pass


class RiskGuardrail:
    def __init__(self, bankroll_usd: float, store: EdgeStore = None):
        self.bankroll_usd = bankroll_usd
        self.store = store or EdgeStore()
        # Loaded from persisted state, not a bare in-memory flag — Railway
        # restarts on every redeploy, and an in-memory kill switch would
        # silently un-trip itself on the next push even if the underlying
        # daily-loss condition is still true. See load_kill_switch/
        # set_kill_switch in edge_store.py.
        self._killed = self.store.load_kill_switch()["tripped"]

    # -- placeholder PF rules — replace with your real logic ----------------

    def _pf04_market_quality(self, verdict: Verdict) -> RiskDecision:
        """PF-04 (placeholder): reject markets that are too thin or too wide
        to trade at the size Maker's edge would justify. Replace with your
        actual PF-04 definition."""
        c = verdict.proposal.candidate
        if c.spread > 8:  # cents
            return RiskDecision(False, f"PF-04 stub: spread {c.spread}c too wide")
        if c.volume < CONFIG.risk.min_liquidity_usd:
            return RiskDecision(False, "PF-04 stub: volume below floor")
        return RiskDecision(True, "PF-04 stub: pass")

    def _pf09_category_calibration(self, verdict: Verdict) -> RiskDecision:
        """PF-09 (placeholder): down-weight or block (category, strategy)
        pairs where edge memory shows either bad PnL or poor Brier-score
        calibration. Replace with your actual PF-09 definition."""
        category = verdict.proposal.candidate.category
        source = verdict.proposal.source
        calibration = {
            (row["category"], row["source"]): row for row in self.store.calibration_by_category()
        }
        row = calibration.get((category, source))
        if row and row["n"] >= 10:
            if row["avg_pnl"] is not None and row["avg_pnl"] < 0:
                return RiskDecision(
                    False,
                    f"PF-09 stub: ({category}/{source}) has negative avg PnL over {row['n']} trades",
                )
            if row["brier_score"] is not None and row["brier_score"] > 0.28:
                # 0.25 is what a coin flip stating 50% always scores — above
                # that, Maker is actively worse than admitting it doesn't know.
                return RiskDecision(
                    False,
                    f"PF-09 stub: ({category}/{source}) Brier score {row['brier_score']:.3f} "
                    f"indicates poor calibration over {row['n']} trades",
                )
        return RiskDecision(True, "PF-09 stub: pass")

    def _pf10_exposure_cap(self, verdict: Verdict, open_positions: int) -> RiskDecision:
        """PF-10 (placeholder): cap total concurrent open positions and
        per-position bankroll exposure. Replace with your actual PF-10
        definition."""
        if open_positions >= CONFIG.risk.max_open_positions:
            return RiskDecision(False, "PF-10 stub: max open positions reached")
        return RiskDecision(True, "PF-10 stub: pass")

    def _longshot_bias_guard(self, verdict: Verdict) -> RiskDecision:
        """Not one of your original PF rules — added from Jonathan Becker's
        analysis of 72.1M Kalshi trades (jbecker.dev/research/prediction-
        market-microstructure), which found contracts priced under ~20c
        systematically underperform their implied odds: a documented
        favorite-longshot bias in Kalshi's own retail order flow, not a
        golf-specific pattern. This doesn't block longshot YES trades — your
        golf edge should still be able to override a base rate — it just
        raises the bar: Maker's stated edge has to clear a higher multiple
        of the normal threshold to pass, since the crowd is usually wrong
        for a *reason* (optimism bias) at this end of the price range, not
        randomly wrong."""
        c = verdict.proposal.candidate
        direction = verdict.proposal.direction
        yes_price_cents = c.yes_ask if direction == "yes" else (100 - c.yes_bid)
        if direction == "yes" and yes_price_cents < CONFIG.risk.longshot_price_threshold_cents:
            required_edge = CONFIG.risk.min_edge_threshold * CONFIG.risk.longshot_edge_multiplier
            if verdict.proposal.edge_size < required_edge:
                return RiskDecision(
                    False,
                    f"Longshot bias guard: YES at {yes_price_cents:.0f}c needs edge >= "
                    f"{required_edge:.2%} (has {verdict.proposal.edge_size:.2%}) — documented "
                    f"Kalshi-wide bias means longshot YES needs to clear a higher bar, not just "
                    f"the standard threshold",
                )
        return RiskDecision(True, "longshot bias guard: pass")

    # -- drawdown kill switch -------------------------------------------------

    def check_kill_switch(self) -> bool:
        """Returns True if trading should halt. Sums today's settled PnL
        against bankroll; trips if losses exceed max_daily_loss_pct."""
        if self._killed:
            return True
        day_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        pnl_today = self.store.daily_pnl(day_start)
        loss_limit = -abs(CONFIG.risk.max_daily_loss_pct * self.bankroll_usd)
        if pnl_today <= loss_limit:
            self._killed = True
            reason = f"daily PnL {pnl_today:.2f} breached limit {loss_limit:.2f}"
            self.store.set_kill_switch(True, reason)
            log.error("KILL SWITCH TRIPPED (persisted): %s", reason)
            return True
        return False

    def reset_kill_switch(self):
        """Manual reset only — never call this automatically from inside the
        trading loop. Requires a human decision to resume. Clears the
        persisted flag too, not just the in-memory one."""
        self._killed = False
        self.store.set_kill_switch(False)

    # -- main gate --------------------------------------------------------

    def evaluate(self, verdict: Verdict, open_positions: int) -> RiskDecision:
        if self.check_kill_switch():
            raise KillSwitchTripped("Daily drawdown limit hit — trading halted")

        if not verdict.approved:
            return RiskDecision(False, f"Checker did not approve: {verdict.verdict}")

        for rule in (
            self._pf04_market_quality(verdict),
            self._pf09_category_calibration(verdict),
            self._pf10_exposure_cap(verdict, open_positions),
            self._longshot_bias_guard(verdict),
        ):
            if not rule.approved:
                return rule

        max_position_usd = self.bankroll_usd * CONFIG.risk.max_position_pct
        price_cents = (
            verdict.proposal.candidate.yes_ask
            if verdict.proposal.direction == "yes"
            else 100 - verdict.proposal.candidate.yes_bid
        )
        price_usd = max(price_cents / 100.0, 0.01)
        size_contracts = int(max_position_usd // price_usd)
        if size_contracts < 1:
            return RiskDecision(False, "Position sizing rounds to zero contracts")

        return RiskDecision(True, "approved", size_contracts=size_contracts)
