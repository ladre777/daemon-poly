"""
Sub-cent markets, and the order price that quietly rounded up on them.

Kalshi does not quote every market in whole cents. The live payload for
`KXBTC15M` — the fifteen-minute bitcoin family, and one of the most liquid
things on the exchange at ~1.03M contracts of volume — carries:

    "price_level_structure": "tapered_deci_cent"
    "yes_bid_dollars": "0.9590",  "yes_ask_dollars": "0.9600"

A tenth-of-a-cent spread, on a grid that steps by $0.001 below $0.10 and
above $0.90. The read path already handles that correctly: prices are floats
all the way from validation through the edge calculation.

The *write* path did not. The wire price was built as::

    price_dollars = f"{record.limit_price_cents / 100:.2f}"

which rounds to the nearest cent. So a limit of 5.55c left the process as a
6c order — above the price the risk decision sized against, budgeted fees on,
and ran its invariant check on, and above the number persisted as the order's
limit and counted as its exposure. Under budget by 0.45c per contract is not
the interesting part; that the submitted price silently stopped matching the
approved price is.

The fix floors the limit onto the grid inside `cost_per_contract_cents`, so
risk, the stored order and the wire all carry one number, and the formatter
raises rather than rounds if it is ever handed something it cannot express.

A whole cent is a member of every grid Kalshi publishes, so flooring to the
cent is valid on `linear_cent` and `tapered_deci_cent` alike without reading
the structure off the market. Quoting *inside* a 0.1c spread is a
market-making problem and belongs with that layer.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from core.pricing import (
    cost_per_contract_cents,
    grid_floor_cents,
    price_dollars_string,
)
from workers.risk_guardrail import RiskDecision

from tests.conftest import make_candidate, make_verdict
from tests.test_risk import snapshot


# -- the formatter ---------------------------------------------------------


def test_whole_cent_prices_are_unchanged():
    """The overwhelmingly common case must format exactly as before."""
    assert price_dollars_string(52.0) == "0.52"
    assert price_dollars_string(1.0) == "0.01"
    assert price_dollars_string(99.0) == "0.99"


def test_a_sub_cent_price_is_refused_rather_than_rounded():
    """The whole point: no silent rounding at the wire boundary."""
    with pytest.raises(ValueError, match="not on the .* order grid"):
        price_dollars_string(5.55)


def test_the_refusal_names_the_price():
    with pytest.raises(ValueError, match="5.55"):
        price_dollars_string(5.55)


# -- the grid --------------------------------------------------------------


def test_flooring_never_rounds_up():
    """The old `.2f` sent 6c for a 5.55c limit. Paying more than approved is
    the one direction that must be impossible."""
    assert grid_floor_cents(5.55) == 5.0
    assert grid_floor_cents(5.99) == 5.0
    assert grid_floor_cents(96.4) == 96.0


def test_float_dust_does_not_cost_a_cent():
    """4.10c arrives as 4.0999999999999996 often enough to matter — a limit
    of 5.1c must floor to 5, not to 4."""
    assert grid_floor_cents(5.0 + 1e-12) == 5.0
    assert grid_floor_cents(4.1 + 1.0) == 5.0


def test_exact_cents_are_left_alone():
    for cents in (0.0, 1.0, 42.0, 99.0):
        assert grid_floor_cents(cents) == cents


# -- the shared cost model -------------------------------------------------


def test_a_sub_cent_quote_yields_a_gridded_limit():
    """KXBTC15M, NO side: 100 - 95.90 = 4.10c executable, +1c slippage."""
    limit, _, _ = cost_per_contract_cents(4.10)

    assert limit == 5.0
    assert price_dollars_string(limit) == "0.05"


def test_the_limit_still_crosses_the_quote():
    """Flooring must not cost the fill. With a 1c slippage allowance,
    floor(price + 1) is strictly above price for any price."""
    for price in (0.7, 4.10, 4.55, 9.95, 50.0, 95.9, 96.0):
        limit, _, _ = cost_per_contract_cents(price)
        assert limit > price, f"limit {limit} does not cross {price}"


def test_a_whole_cent_quote_is_priced_exactly_as_before():
    """No silent behaviour change on `linear_cent` markets, which is every
    market this bot has priced to date."""
    assert cost_per_contract_cents(52.0)[0] == 53.0
    assert cost_per_contract_cents(4.0)[0] == 5.0


def test_the_ninety_nine_cent_cap_survives():
    assert cost_per_contract_cents(99.0)[0] == 99.0
    assert cost_per_contract_cents(99.5)[0] == 99.0


def test_fees_are_charged_on_the_price_actually_sent():
    """The fee was computed on the pre-floor limit, so the estimate no longer
    describes the order. Recomputing on the gridded limit keeps the budget
    and the order the same trade."""
    limit, fee, total = cost_per_contract_cents(4.55)

    assert limit == 5.0
    assert total == pytest.approx(limit + fee)


# -- the risk gate ---------------------------------------------------------


def test_the_only_non_crossing_limit_is_at_the_top_tick():
    """Where the crossing guard in `evaluate` can actually bite.

    For any price at or below 98c, `floor(price + slippage)` is strictly
    above `price`, so flooring never costs the fill. The single exception is
    the 99c cap: a 99.5c ask — a legal price on the tapered grid — yields a
    99c limit that no longer reaches it.
    """
    non_crossing = [p / 10 for p in range(1, 1000)
                    if cost_per_contract_cents(p / 10)[0] < p / 10]

    assert non_crossing and min(non_crossing) > 98.0


def test_the_edge_gate_already_refuses_the_top_tick(risk):
    """So the crossing guard is defence in depth, not a live path.

    At 99.5c the whole upside is half a cent, which the 1c slippage
    allowance alone exceeds — net edge is negative before the crossing check
    is ever reached. The guard stays because "an order that cannot fill must
    not be sent" should not depend on a risk threshold keeping its current
    value.
    """
    candidate = make_candidate(yes_bid=99.4, yes_ask=99.5)
    verdict = make_verdict(candidate=candidate, maker_probability=1.0)

    decision = risk.evaluate(verdict, snapshot())

    assert not decision.approved
    assert "below the" in decision.reason


def test_an_ordinary_sub_cent_quote_is_still_approved(risk):
    """Refusing the top tick must not turn into refusing sub-cent markets —
    they are the priority families."""
    candidate = make_candidate(yes_bid=3.9, yes_ask=4.10)
    verdict = make_verdict(candidate=candidate, maker_probability=0.60)

    decision = risk.evaluate(verdict, snapshot())

    assert decision.approved, decision.reason
    assert decision.limit_price_cents == 5.0


# -- end to end ------------------------------------------------------------


def test_the_price_on_the_wire_is_the_price_that_was_approved(
    execution, client, account
):
    """The property the change exists for. Previously the submitted price
    could sit half a cent above `limit_price_cents`, so the stored order, the
    exposure it contributed and the order Kalshi held were three different
    numbers."""
    CONFIG.risk.dry_run = False
    candidate = make_candidate(yes_bid=3.9, yes_ask=4.10, category="Crypto")
    verdict = make_verdict(candidate=candidate)
    decision = RiskDecision(
        approved=True, reason="test", size_contracts=2,
        executable_price_cents=4.10, limit_price_cents=5.0,
    )

    record = execution.execute(verdict, decision)

    sent = client.place_order_calls[-1]
    assert sent["yes_price_dollars"] == "0.05"
    assert float(sent["yes_price_dollars"]) * 100 == record.limit_price_cents


def test_an_off_grid_limit_never_reaches_the_exchange(
    execution, client, account
):
    """Fail closed. If some future path hands execution a sub-cent limit, the
    order must not go out rounded — no order should go out at all."""
    CONFIG.risk.dry_run = False
    verdict = make_verdict(candidate=make_candidate(yes_bid=3.9, yes_ask=4.10))
    decision = RiskDecision(
        approved=True, reason="test", size_contracts=2,
        executable_price_cents=4.10, limit_price_cents=5.55,
    )

    with pytest.raises(ValueError, match="order grid"):
        execution.execute(verdict, decision)

    assert client.place_order_calls == []
