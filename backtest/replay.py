"""
Replay/backtest harness for the quant path.

P1 item 8 ends with: "Add replay/backtest tests that report calibration,
Brier score, net PnL after fees, drawdown, fill assumptions, and sensitivity
to stale or delayed quotes. Do not claim profitability without this
evidence."

The last sentence is the point of this module, and it cuts against the
harness itself. A backtest over *synthetic* price paths cannot establish that
this strategy makes money on Kalshi; it can only establish that the pricing
code is internally consistent — that a model given the true data-generating
process is calibrated, that fees and spread subtract what they should, and
that degrading the inputs degrades the results in the expected direction and
magnitude. Those are worth knowing, because if the model is not calibrated
against a process it is exactly right about, it will not be calibrated
against a real one either.

So: this is a *falsifier*, not evidence of profitability. Nothing here
licenses a claim about live returns. What it does license is the negative —
if the quant path cannot clear these bars, it should not trade at all.

Fill assumptions, stated explicitly because they drive the PnL number:

- Every approved order fills in full, immediately, at the executable price
  (ask for YES, 100-bid for NO) plus the configured slippage allowance.
  Real IOC orders partially fill and miss; this is optimistic.
- Fees are charged per contract at the modelled rate on the limit price.
- Positions are held to settlement. No exits, no mid-life re-marking.
- No market impact. Sizes here are small enough that this is plausible, but
  it is an assumption, not a finding.

Since these are optimistic, a strategy that loses money *here* would lose
more in reality. The converse does not hold.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import CONFIG
from core.pricing import cost_per_contract_cents
from core.validation import Quote
from workers.scout import Candidate


@dataclass
class ReplayTrade:
    ticker: str
    direction: str
    model_probability: float
    executable_price_cents: float
    limit_price_cents: float
    fee_cents: float
    contracts: int
    outcome: str            # "yes" | "no"
    realized_pnl_dollars: float

    @property
    def won(self) -> bool:
        return self.outcome == self.direction


@dataclass
class ReplayReport:
    """Everything the brief asks a backtest to report."""

    trades: list[ReplayTrade] = field(default_factory=list)
    considered: int = 0
    skipped_no_edge: int = 0
    skipped_stale: int = 0

    # -- headline numbers --------------------------------------------------

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def net_pnl(self) -> float:
        """Net of fees, in dollars. Fees are already inside each trade's PnL."""
        return sum(t.realized_pnl_dollars for t in self.trades)

    @property
    def total_fees(self) -> float:
        return sum(t.fee_cents * t.contracts / 100.0 for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return self.net_pnl + self.total_fees

    @property
    def hit_rate(self) -> float:
        return sum(1 for t in self.trades if t.won) / self.n if self.n else 0.0

    @property
    def brier_score(self) -> Optional[float]:
        """Mean squared error between stated probability and the 0/1 outcome.

        Scored on the YES probability regardless of which side was traded, so
        it measures the model's calibration rather than the strategy's luck.
        A model that always says 50% scores 0.25; anything worse than that is
        worse than admitting ignorance.
        """
        if not self.trades:
            return None
        total = 0.0
        for t in self.trades:
            actual = 1.0 if t.outcome == "yes" else 0.0
            # model_probability is always P(YES).
            total += (t.model_probability - actual) ** 2
        return total / len(self.trades)

    def calibration_buckets(self, buckets: int = 10) -> list[dict]:
        """Predicted vs actual frequency, bucketed by stated probability.

        The check that matters for a probability model: of the markets it
        called 70%, did roughly 70% happen?
        """
        out = []
        for i in range(buckets):
            lo, hi = i / buckets, (i + 1) / buckets
            in_bucket = [
                t for t in self.trades
                if lo <= t.model_probability < hi or (i == buckets - 1 and t.model_probability == 1.0)
            ]
            if not in_bucket:
                continue
            actual = sum(1 for t in in_bucket if t.outcome == "yes") / len(in_bucket)
            predicted = sum(t.model_probability for t in in_bucket) / len(in_bucket)
            out.append({
                "bucket": f"{lo:.0%}-{hi:.0%}",
                "n": len(in_bucket),
                "predicted": predicted,
                "actual": actual,
                "error": actual - predicted,
            })
        return out

    @property
    def max_drawdown(self) -> float:
        """Largest peak-to-trough decline of the cumulative PnL curve."""
        peak = 0.0
        equity = 0.0
        worst = 0.0
        for t in self.trades:
            equity += t.realized_pnl_dollars
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return worst

    def summary(self) -> str:
        cal = self.calibration_buckets()
        lines = [
            f"trades={self.n} of {self.considered} considered "
            f"(no edge: {self.skipped_no_edge}, stale: {self.skipped_stale})",
            f"net PnL after fees: ${self.net_pnl:+.2f} "
            f"(gross ${self.gross_pnl:+.2f}, fees ${self.total_fees:.2f})",
            f"hit rate: {self.hit_rate:.1%}",
            f"Brier score: {self.brier_score:.4f}" if self.brier_score is not None
            else "Brier score: n/a",
            f"max drawdown: ${self.max_drawdown:.2f}",
        ]
        for row in cal:
            lines.append(
                f"  {row['bucket']:>9}  n={row['n']:<4} predicted={row['predicted']:.1%} "
                f"actual={row['actual']:.1%} err={row['error']:+.1%}"
            )
        return "\n".join(lines)


# -- synthetic world -------------------------------------------------------


def simulate_gbm_path(spot: float, vol_per_second: float, seconds: float,
                      steps: int, rng: random.Random) -> list[float]:
    """Geometric Brownian motion path. The data-generating process the quant
    model assumes, so a model fed its true parameters should be calibrated
    against it — which is exactly what makes a calibration failure here a
    real finding about the code rather than about the market."""
    dt = seconds / steps
    path = [spot]
    for _ in range(steps):
        shock = rng.gauss(0.0, 1.0) * vol_per_second * math.sqrt(dt)
        # Drift-free in log space, matching the model's own assumption.
        path.append(path[-1] * math.exp(-0.5 * vol_per_second ** 2 * dt + shock))
    return path


def true_probability_above(spot: float, strike: float, vol_per_second: float,
                           seconds: float) -> float:
    """Closed-form P(S_T > K) for the same process."""
    sigma_t = vol_per_second * math.sqrt(seconds)
    if sigma_t <= 0:
        return 1.0 if spot > strike else 0.0
    z = math.log(strike / spot) / sigma_t
    return 1.0 - 0.5 * (1 + math.erf(z / math.sqrt(2)))


def make_candidate(ticker: str, yes_bid: float, yes_ask: float,
                   strike: float, seconds_to_close: float,
                   quote_age: float = 0.0) -> Candidate:
    import time
    from datetime import datetime, timedelta, timezone

    close = datetime.now(timezone.utc) + timedelta(seconds=seconds_to_close)
    return Candidate(
        ticker=ticker,
        title=ticker,
        category="Crypto",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        volume=100_000,
        close_time=close.isoformat().replace("+00:00", "Z"),
        strike_type="greater",
        floor_strike=strike,
        quote=Quote(yes_bid=yes_bid, yes_ask=yes_ask,
                    captured_at=time.time() - quote_age),
    )


# -- the replay ------------------------------------------------------------


def run_replay(
    n_markets: int = 400,
    seed: int = 7,
    spot: float = 100_000.0,
    vol_per_second: float = 2e-4,
    seconds_to_expiry: float = 3600.0,
    spread_cents: float = 2.0,
    #: Systematic bias of the book away from the true probability, in cents.
    #: Zero means a fairly-priced market, where a correct model has no edge
    #: and should trade almost never — the null hypothesis. Positive means the
    #: market overprices YES, giving a correct model a real, tradeable edge.
    mispricing_cents: float = 0.0,
    #: Delay between the quote the model prices against and the quote that
    #: actually fills. This is the stale-quote sensitivity knob.
    quote_delay_seconds: float = 0.0,
    #: Multiplier applied to the model's volatility input, for testing what a
    #: mis-estimated vol does to calibration and PnL.
    vol_error: float = 1.0,
    model: Callable[[float, float, float, float], float] = None,
    contracts: int = 10,
) -> ReplayReport:
    """Replay `n_markets` synthetic contracts through the pricing and sizing
    logic, and report what the brief asks for.

    The market is priced *fairly* — the book is centred on the true
    probability with a spread around it. So a correct model has no edge net of
    costs, and should trade rarely and roughly break even. That is the null
    hypothesis this harness is built to detect violations of: if a model with
    no informational advantage shows a profit here, the accounting is wrong.
    """
    rng = random.Random(seed)
    model = model or true_probability_above
    report = ReplayReport()

    for i in range(n_markets):
        # Strike scattered around spot so probabilities span the range.
        strike = spot * math.exp(rng.gauss(0.0, 0.02))
        true_p = true_probability_above(spot, strike, vol_per_second,
                                        seconds_to_expiry)

        # Book centred on the true probability, plus any systematic bias.
        mid_cents = max(min(true_p * 100.0 + mispricing_cents, 99.0), 1.0)
        yes_bid = max(mid_cents - spread_cents / 2, 0.0)
        yes_ask = min(mid_cents + spread_cents / 2, 100.0)

        candidate = make_candidate(
            f"KXBTCD-{i}", yes_bid, yes_ask, strike, seconds_to_expiry,
            quote_age=quote_delay_seconds,
        )
        report.considered += 1

        if candidate.quote.is_stale():
            report.skipped_stale += 1
            continue

        model_p = model(spot, strike, vol_per_second * vol_error,
                        seconds_to_expiry)
        model_p = min(max(model_p, 0.001), 0.999)

        direction = "yes" if model_p > candidate.implied_yes_probability else "no"
        price = candidate.executable_price_cents(direction)
        limit_price, fee, _ = cost_per_contract_cents(price)
        implied = candidate.executable_probability(direction)
        edge = (model_p - implied) if direction == "yes" else (implied - model_p)
        net = edge - (fee + CONFIG.risk.slippage_cents) / 100.0

        if net < CONFIG.risk.min_edge_threshold:
            report.skipped_no_edge += 1
            continue

        # Resolve against a real path drawn from the true process.
        path = simulate_gbm_path(spot, vol_per_second, seconds_to_expiry,
                                 steps=50, rng=rng)
        outcome = "yes" if path[-1] > strike else "no"

        # Fill assumption: full fill at the limit price (executable +
        # slippage), fees charged per contract. Optimistic, as documented.
        cost_cents = limit_price + fee
        payout_cents = 100.0 if outcome == direction else 0.0
        pnl = contracts * (payout_cents - cost_cents) / 100.0

        report.trades.append(ReplayTrade(
            ticker=candidate.ticker,
            direction=direction,
            # Always stored as P(YES) so Brier scoring is direction-agnostic.
            model_probability=model_p,
            executable_price_cents=price,
            limit_price_cents=limit_price,
            fee_cents=fee,
            contracts=contracts,
            outcome=outcome,
            realized_pnl_dollars=pnl,
        ))

    return report
