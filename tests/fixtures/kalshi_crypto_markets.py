"""
Real `GET /markets` payloads, captured 2026-08-17 from the live Kalshi API.

Not hand-written. These are the exact objects the exchange returned for one
open market in each crypto family, trimmed only of the `price_ranges` array
(pure display metadata, several hundred bytes, nothing reads it).

They exist because every previous schema assumption in this repo was wrong in
a way that failed silently: `yes_bid` when the field was `yes_bid_dollars`,
`volume` when it was `volume_fp`, `last_price_time` read as quote freshness
when it is last-trade time. Guessing was the recurring cause; fixtures
captured from the wire are the fix.

The `rules_primary` text on each is the primary evidence for
core/contract_specs.py — it is the exchange stating, in its own words, what
the market settles on.
"""
from __future__ import annotations

#: 15-minute BTC up/down market. Strike is the opening 60s BRTI average.
#: Heavily traded: volume_fp 1,575,916.
KXBTC15M = {
    "can_close_early": True,
    "close_time": "2026-08-17T05:00:00Z",
    "created_time": "2026-08-17T00:00:45.397234Z",
    "custom_strike": {"round_digits": "2"},
    "event_ticker": "KXBTC15M-26AUG170100",
    "exchange_index": 0,
    "expected_expiration_time": "2026-08-17T05:05:00Z",
    "expiration_time": "2026-08-24T05:00:00Z",
    "expiration_value": "",
    "floor_strike": 63441.03,
    "last_price_dollars": "0.9530",
    "latest_expiration_time": "2026-08-24T05:00:00Z",
    "liquidity_dollars": "0.0000",
    "market_type": "binary",
    "no_ask_dollars": "0.0480",
    "no_bid_dollars": "0.0470",
    "no_sub_title": "Target price: TBD",
    "notional_value_dollars": "1.0000",
    "occurrence_datetime": "2026-08-17T05:05:00Z",
    "open_interest_fp": "522100.53",
    "open_time": "2026-08-17T04:45:00Z",
    "previous_price_dollars": "0.0000",
    "previous_yes_ask_dollars": "0.0000",
    "previous_yes_bid_dollars": "0.0000",
    "price_level_structure": "tapered_deci_cent",
    "result": "",
    "rules_primary": (
        "If the simple average of the sixty seconds of CF Benchmarks' BRTI "
        "before 1:00 AM EDT on Aug 17, 2026 is at least the simple average "
        "of the sixty seconds of CF Benchmarks' BRTI before 12:45 AM EDT on "
        "August 17, 2026, then the market resolves to Yes."
    ),
    "rules_secondary": (
        "Not all cryptocurrency price data is the same. While checking a "
        "source like Google or Coinbase may help guide your decision, the "
        "price used to determine this market is based on CF Benchmarks' "
        "corresponding Real Time Index (RTI). At the last minute before "
        "expiration, 60 RTI prices are collected. The official and final "
        "value is the average of these prices, rounded to the nearest 2 "
        "decimal places."
    ),
    "settlement_timer_seconds": 1,
    "status": "active",
    "strike_type": "greater_or_equal",
    "ticker": "KXBTC15M-26AUG170100-00",
    "title": "BTC price up in next 15 mins?",
    "updated_time": "2026-08-17T04:45:00.707862Z",
    "volume_24h_fp": "737032.57",
    "volume_fp": "1575916.05",
    "yes_ask_dollars": "0.9530",
    "yes_ask_size_fp": "813.44",
    "yes_bid_dollars": "0.9520",
    "yes_bid_size_fp": "8.00",
    "yes_sub_title": "Target Price: $63,441.03",
}

#: Daily BTC market, fixed strike. Essentially untraded: volume_fp 2.00.
KXBTCD = {
    "can_close_early": True,
    "close_time": "2026-08-17T21:00:00Z",
    "created_time": "2026-08-16T09:00:38.03486Z",
    "event_ticker": "KXBTCD-26AUG1717",
    "exchange_index": 0,
    "expected_expiration_time": "2026-08-17T21:05:00Z",
    "expiration_time": "2026-08-24T21:00:00Z",
    "expiration_value": "",
    "floor_strike": 72749.99,
    "last_price_dollars": "0.0100",
    "latest_expiration_time": "2026-08-24T21:00:00Z",
    "liquidity_dollars": "0.0000",
    "market_type": "binary",
    "no_ask_dollars": "1.0000",
    "no_bid_dollars": "0.9900",
    "no_sub_title": "$72,750 or above",
    "notional_value_dollars": "1.0000",
    "occurrence_datetime": "2026-08-17T21:05:00Z",
    "open_interest_fp": "2.00",
    "open_time": "2026-08-16T20:00:00Z",
    "previous_price_dollars": "0.0000",
    "previous_yes_ask_dollars": "0.0000",
    "previous_yes_bid_dollars": "0.0000",
    "price_level_structure": "linear_cent",
    "result": "",
    "rules_primary": (
        "If the simple average of the sixty seconds of CF Benchmarks' "
        "Bitcoin Real-Time Index (BRTI) before 5 PM EDT is above 72749.99 at "
        "5 PM EDT on Aug 17, 2026, then the market resolves to Yes."
    ),
    "rules_secondary": (
        "Not all cryptocurrency price data is the same. While checking a "
        "source like Google or Coinbase may help guide your decision, the "
        "price used to determine this market is based on CF Benchmarks' "
        "corresponding Real Time Index (RTI). At the last minute before "
        "expiration, 60 RTI prices are collected. The official and final "
        "value is the average of these prices."
    ),
    "settlement_timer_seconds": 60,
    "status": "active",
    "strike_type": "greater",
    "subtitle": "$72,750 or above",
    "ticker": "KXBTCD-26AUG1717-T72749.99",
    "title": "Bitcoin price on Aug 17, 2026?",
    "updated_time": "2026-08-16T20:00:00.404905Z",
    "volume_24h_fp": "2.00",
    "volume_fp": "2.00",
    "yes_ask_dollars": "0.0100",
    "yes_ask_size_fp": "14862.00",
    "yes_bid_dollars": "0.0000",
    "yes_bid_size_fp": "0.00",
    "yes_sub_title": "$72,750 or above",
}

#: Hourly ETH market, fixed strike, settles on ETHUSD_RTI (written "ERTI" in
#: the rules prose). Completely untraded: every volume field is 0.00.
KXETH = {
    "can_close_early": True,
    "close_time": "2026-08-17T06:00:00Z",
    "created_time": "2026-08-16T09:03:25.245564Z",
    "event_ticker": "KXETH-26AUG1702",
    "exchange_index": 0,
    "expected_expiration_time": "2026-08-17T06:05:00Z",
    "expiration_time": "2026-08-24T06:00:00Z",
    "expiration_value": "",
    "floor_strike": 2594.99,
    "last_price_dollars": "0.0000",
    "latest_expiration_time": "2026-08-24T06:00:00Z",
    "liquidity_dollars": "0.0000",
    "market_type": "binary",
    "no_ask_dollars": "1.0000",
    "no_bid_dollars": "0.9900",
    "no_sub_title": "$2,595 or above",
    "notional_value_dollars": "1.0000",
    "occurrence_datetime": "2026-08-17T06:05:00Z",
    "open_interest_fp": "0.00",
    "open_time": "2026-08-17T05:00:00Z",
    "previous_price_dollars": "0.0000",
    "previous_yes_ask_dollars": "0.0000",
    "previous_yes_bid_dollars": "0.0000",
    "price_level_structure": "linear_cent",
    "result": "",
    "rules_primary": (
        "If the simple average of the sixty seconds of CF Benchmarks' "
        "Ethereum Real-Time Index (ERTI) before 2 AM EDT is above 2594.99 at "
        "2 AM EDT on Aug 17, 2026, then the market resolves to Yes."
    ),
    "rules_secondary": (
        "Not all cryptocurrency price data is the same. While checking a "
        "source like Google or Coinbase may help guide your decision, the "
        "price used to determine this market is based on CF Benchmarks' "
        "corresponding Real Time Index (RTI). At the last minute before "
        "expiration, 60 RTI prices are collected. The official and final "
        "value is the average of these prices."
    ),
    "settlement_timer_seconds": 60,
    "status": "active",
    "strike_type": "greater",
    "subtitle": "$2,595 or above",
    "ticker": "KXETH-26AUG1702-T2594.99",
    "title": "Ethereum price at Aug 17, 2026 at 2am EDT?",
    "updated_time": "2026-08-17T05:00:00.861494Z",
    "volume_24h_fp": "0.00",
    "volume_fp": "0.00",
    "yes_ask_dollars": "0.0100",
    "yes_ask_size_fp": "33300.00",
    "yes_bid_dollars": "0.0000",
    "yes_bid_size_fp": "0.00",
    "yes_sub_title": "$2,595 or above",
}

ALL_CRYPTO_MARKETS = (KXBTC15M, KXBTCD, KXETH)
