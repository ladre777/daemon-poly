"""
Asking for the priority families by name instead of hoping pagination arrives.

VERIFIED AGAINST PRODUCTION, 2026-08-17, on the first scan against the real
exchange::

    Scan stopped at the 400-page cap (~80000 markets) with more catalog remaining.
    Family census KXBTC15M: 0 seen (family absent from the scanned catalog)
    Family census KXETH:    0 seen (family absent from the scanned catalog)
    Tradeable families in Politics: KXMVECROSSCATEGORY x1425
    Tradeable families in Sports:   KXMVESPORTSMULTIGAMEEXTENDED x859
    Pass funnel: 2295 candidate(s) -> quant 0 ... -> filled 0

Every priority family reported "0 seen", and the quant path attempted nothing
at all — not because those markets are thin, and not because they are absent,
but because the sweep never reached them. Production's catalog is dominated by
multi-value event shards, the sweep is ordered by Kalshi rather than by us,
and 80,000 markets ran out first.

Raising the page cap does not fix that, it moves it: the catalog is larger
still, every extra page is a rate-limited request, and the ordering stays
outside our control.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from workers.scout import Scout

from tests.fakes import FakeKalshiClient
from tests.test_categories import _market


class ShardedClient(FakeKalshiClient):
    """A catalog whose sweep is full of shards, like production's.

    The sweep pages return only shard markets. The priority family exists but
    is reachable *only* by asking for the series — exactly the situation that
    made every census line read "0 seen".
    """

    def __init__(self, sweep_pages=3, series_markets=None):
        super().__init__()
        self.sweep_pages = sweep_pages
        self.pages = 0
        self.series_calls: list[str] = []
        self._series_markets = series_markets if series_markets is not None else [
            _market("KXETH-26AUG1702-T2594.99"),
            _market("KXETH-26AUG1702-T2600.99"),
        ]

    def list_markets(self, series_ticker=None, status="open", limit=200, cursor=None):
        if series_ticker:
            self.series_calls.append(series_ticker)
            if series_ticker.upper() == "KXETH":
                return {"markets": self._series_markets, "cursor": None}
            return {"markets": [], "cursor": None}
        self.pages += 1
        return {
            "markets": [_market(f"KXMVECROSSCATEGORY-SHARD{self.pages}")],
            "cursor": "next" if self.pages < self.sweep_pages else None,
        }


@pytest.fixture(autouse=True)
def watch_eth():
    CONFIG.scout_categories = []          # take every group
    CONFIG.scout_census_families = ["KXETH"]
    CONFIG.scout_max_pages = 3


# -- the markets the sweep never reached -----------------------------------


def test_a_priority_family_missed_by_the_sweep_is_still_found():
    """The production failure, as a test."""
    client = ShardedClient()

    tickers = [c.ticker for c in Scout(client).scan()]

    assert "KXETH-26AUG1702-T2594.99" in tickers
    assert "KXETH-26AUG1702-T2600.99" in tickers


def test_the_series_is_requested_by_name():
    client = ShardedClient()

    Scout(client).scan()

    assert "KXETH" in client.series_calls


def test_it_still_runs_when_the_sweep_hit_its_cap():
    """The cap is exactly when this matters — the sweep stopping early is the
    condition that hid these markets in the first place."""
    CONFIG.scout_max_pages = 2
    client = ShardedClient(sweep_pages=50)

    tickers = [c.ticker for c in Scout(client).scan()]

    assert client.pages == 2, "the sweep still stops at its cap"
    assert any(t.startswith("KXETH") for t in tickers)


def test_the_census_reports_the_family_as_found():
    client = ShardedClient()

    Scout(client).scan()

    # 2 seen, 2 accepted — sourced entirely from the targeted fetch.
    assert client.series_calls == ["KXETH"]


# -- it is not a way around the filters ------------------------------------


def test_the_liquidity_floor_still_applies_to_targeted_markets():
    """The targeted fetch shares `_consider` with the sweep precisely so this
    cannot drift. It finds markets the sweep missed; it does not exempt them."""
    client = ShardedClient(series_markets=[
        _market("KXETH-26AUG1702-T2594.99", volume=1.0),
    ])

    assert [c.ticker for c in Scout(client).scan()
            if c.ticker.startswith("KXETH")] == []


def test_the_category_filter_still_applies_to_targeted_markets():
    CONFIG.scout_categories = ["Sports"]
    client = ShardedClient()

    assert [c.ticker for c in Scout(client).scan()
            if c.ticker.startswith("KXETH")] == []


def test_validation_still_applies_to_targeted_markets():
    client = ShardedClient(series_markets=[
        {**_market("KXETH-26AUG1702-T2594.99"),
         "close_time": "1999-01-01T00:00:00Z"},
    ])

    assert [c.ticker for c in Scout(client).scan()
            if c.ticker.startswith("KXETH")] == []


# -- overlap between the two passes ----------------------------------------


def test_a_market_found_by_both_passes_appears_once():
    class OverlappingClient(ShardedClient):
        def list_markets(self, series_ticker=None, status="open", limit=200, cursor=None):
            if series_ticker:
                return {"markets": [_market("KXETH-26AUG1702-T2594.99")], "cursor": None}
            self.pages += 1
            return {"markets": [_market("KXETH-26AUG1702-T2594.99")], "cursor": None}

    tickers = [c.ticker for c in Scout(OverlappingClient()).scan()]

    assert tickers.count("KXETH-26AUG1702-T2594.99") == 1


def test_a_market_seen_twice_is_counted_once_in_the_census(caplog):
    """Dedupe covers every market considered, not only accepted ones —
    otherwise a thin market found by both passes is counted twice and the
    family totals stop matching the catalog."""
    class OverlappingClient(ShardedClient):
        def list_markets(self, series_ticker=None, status="open", limit=200, cursor=None):
            thin = _market("KXETH-26AUG1702-T2594.99", volume=1.0)
            if series_ticker:
                return {"markets": [thin], "cursor": None}
            self.pages += 1
            return {"markets": [thin], "cursor": None}

    with caplog.at_level("INFO", logger="daemon_kalshi.scout"):
        Scout(OverlappingClient()).scan()

    assert "Family census KXETH: 1 seen" in caplog.text


# -- failure containment ---------------------------------------------------


def test_an_unreachable_series_does_not_cost_the_scan(caplog):
    from core.kalshi_client import KalshiAPIError

    class BrokenSeriesClient(ShardedClient):
        def list_markets(self, series_ticker=None, status="open", limit=200, cursor=None):
            if series_ticker:
                raise KalshiAPIError(503, "series unavailable")
            return super().list_markets(None, status, limit, cursor)

    with caplog.at_level("WARNING", logger="daemon_kalshi.scout"):
        candidates = Scout(BrokenSeriesClient()).scan()

    assert candidates, "the sweep's candidates survive"
    assert "Targeted fetch for series KXETH failed" in caplog.text


def test_an_empty_watch_list_makes_no_targeted_calls():
    CONFIG.scout_census_families = []
    client = ShardedClient()

    Scout(client).scan()

    assert client.series_calls == []


def test_a_runaway_series_is_bounded():
    """Present only so a pathological response cannot spin forever."""
    from workers.scout import _MAX_SERIES_PAGES

    class EndlessSeriesClient(ShardedClient):
        def __init__(self):
            super().__init__()
            self.series_pages = 0

        def list_markets(self, series_ticker=None, status="open", limit=200, cursor=None):
            if series_ticker:
                self.series_pages += 1
                return {"markets": [_market(f"KXETH-{self.series_pages}")],
                        "cursor": "always-more"}
            return super().list_markets(None, status, limit, cursor)

    client = EndlessSeriesClient()
    Scout(client).scan()

    assert client.series_pages == _MAX_SERIES_PAGES
