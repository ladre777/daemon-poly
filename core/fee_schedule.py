"""
Kalshi's published fee schedule, transcribed from the primary source.

SOURCE OF TRUTH
    Document:  Kalshi Fee Schedule
    URL:       https://kalshi.com/docs/kalshi-fee-schedule.pdf
    Effective: 2026-07-07 ("Last updated and effective: July 7, 2026",
               printed on every page of the document)
    Retrieved: 2026-08-27, supplied directly by the operator as a PDF.
               This session's network egress is blocked from kalshi.com
               (the policy gateway answers 403 to CONNECT for kalshi.com,
               docs.kalshi.com, help.kalshi.com and trading-api.kalshi.com),
               so the document could not be fetched here. It was read from
               the operator's copy instead. Anyone re-verifying should
               re-fetch from the URL above and compare.

This module replaces ``CONFIG.risk.fee_rate``'s role as a single unverified
constant. That constant carried a docstring admitting it "comes from
published summaries, not a verified schedule". Third-party write-ups
disagreed with each other on three points that matter to us, and the
document settles all three:

1. **Does the multiplier vary by category, and is crypto higher?**
   No. The base taker rate is 0.07 for every event contract market. What
   varies is a per-series multiplier ``M``, and the non-standard table lists
   it as either 0 or 1. Crypto is not charged a higher rate. The two crypto
   series that ARE non-standard go the other way and are fee-FREE:
   ``KXBTCY`` (BTC price range EOY) and ``KXETHY`` (ETH price EOY), both
   M=0 on each side. None of the families this bot actually trades
   (KXBTC, KXBTCD, KXBTC15M, KXETH, KXETHD, KXWTI, KXHIGHNY, KXHIGHCHI)
   appear in the non-standard table, so all of them take the default M=1.

2. **Maker treatment.** Both disputed claims are half right. The maker
   multiplier defaults to **0**, so standard markets carry no maker fee at
   all. Where a series does carry a maker multiplier, the rate is 0.0175 —
   exactly 25% of the taker rate. The claimed "flat 0.25% during major
   events" appears nowhere in the document.

3. **A $0.035 per-contract cap.** Not in the document. No cap of any kind
   is stated. Treated as non-existent.

ROUNDING, AND WHY IT IS THE DELICATE PART

The published formula is::

    fees = round up(M x 0.07 x C x P x (1-P))          # taker
    fees = round up(M x 0.0175 x C x P x (1-P))        # maker

    P = price of a contract in dollars (50 cents is 0.5)
    C = number of contracts being traded
    M = per-contract multiplier (taker default 1, maker default 0)

Note where ``C`` sits: **inside** the rounding. The fee is computed on the
whole order and rounded once, not computed per contract and multiplied.
That distinction is the entire finding of this module — see
:func:`fee_dollars` and the note in ``docs/FEE_SCHEDULE.md``.

The document's prose defines round up as "rounds up such that the fee +
positionCost is rounded to a centicent". Its own published fee table does
not behave that way: every one of the 42 published values is reproduced
exactly by ceiling the aggregate to a whole **cent**, and several of them
are strictly larger than a centicent ceiling would give (1 contract at
$0.50 has a raw fee of $0.0175 — already an exact centicent — and the table
charges $0.02). The table is unambiguous and machine-checkable, so the
table is what this module implements, and the discrepancy is recorded as an
open question rather than silently resolved. ``tests/test_fee_schedule.py``
asserts all 42 published values.

WHAT IS DELIBERATELY NOT ANSWERED

A series whose treatment the document does not pin down returns
:class:`Unavailable`, never a default. There is a real distinction here
that matters:

  - A series absent from the non-standard table is **verified by default**.
    The document's own scope sentence says the general terms "apply to all
    event contract markets on the exchange, apart from specific products
    listed below". Absence from that list is therefore positive information,
    not missing information, and returning Unavailable for those would
    contradict the source.
  - A series the document mentions but whose numbers cannot be read
    unambiguously is genuinely unverified, and returns Unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Union

#: Where this data came from. Emitted in the report so a reader can re-verify.
SOURCE_URL = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
SOURCE_EFFECTIVE_DATE = "2026-07-07"
SOURCE_RETRIEVED_DATE = "2026-08-27"
SOURCE_TITLE = "Kalshi Fee Schedule"

#: Base rates, verbatim from the two published formulas.
TAKER_RATE = Decimal("0.07")
MAKER_RATE = Decimal("0.0175")

#: Published defaults. "default is 1 unless otherwise indicated" (taker);
#: "default is 0 unless otherwise indicated" (maker). The maker default of
#: zero is why standard markets carry no maker fee.
DEFAULT_TAKER_MULTIPLIER = 1
DEFAULT_MAKER_MULTIPLIER = 0

#: One cent, the quantum the published table rounds to.
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class Unavailable:
    """A fee that cannot be stated from the published schedule, and why.

    Deliberately not a number and deliberately not arithmetic-capable: any
    attempt to add or float() it raises, so an unverified fee cannot quietly
    become 0.0 inside a cost calculation. Mirrors
    :class:`reporting.evidence.Unavailable`; kept separate so ``core`` does
    not depend on ``reporting``.
    """

    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"N/A ({self.reason})"


#: Money, or a stated absence of it.
Fee = Union[Decimal, Unavailable]


@dataclass(frozen=True)
class SeriesFees:
    """Per-series multipliers, as published in the Non-Standard Fees table."""

    maker_multiplier: int
    taker_multiplier: int
    description: str = ""


#: The Non-Standard Fees table, transcribed. Series NOT listed here take the
#: published defaults, which is positive information — see module docstring.
#:
#: Every entry below was read with both columns legible. Series the document
#: lists but whose columns did not extract unambiguously are in
#: :data:`AMBIGUOUS_SERIES` instead, and return Unavailable.
SERIES_FEES: dict[str, SeriesFees] = {
    "KXAAAGASM": SeriesFees(1, 1, "US gas price"),
    "KXATPMATCH": SeriesFees(1, 1, "ATP Tennis Match"),
    "KXBALLONDOR": SeriesFees(1, 1, "Ballon d'Or"),
    "KXBTCMAX150": SeriesFees(1, 1, "When will bitcoin hit 150k?"),
    "KXBTCY": SeriesFees(0, 0, "BTC price range EOY"),
    "KXCITRINI": SeriesFees(0, 0, "Will the Citrini scenario materialize?"),
    "KXCPI": SeriesFees(1, 1, "CPI"),
    "KXCPIYOY": SeriesFees(1, 1, "Inflation"),
    "KXDOED": SeriesFees(0, 0, "DOE eliminated"),
    "KXEGGS": SeriesFees(1, 1, "Egg prices"),
    "KXELECTIRAN": SeriesFees(0, 0, "Will Iran hold a presidential election?"),
    "KXEMMYCACTO": SeriesFees(1, 1, "Emmys Lead Comedy Actor"),
    "KXEMMYCACTR": SeriesFees(1, 1, "Emmys Lead Comedy Actress"),
    "KXEMMYCSERIES": SeriesFees(1, 1, "Emmys Comedy Series"),
    "KXEMMYDACTO": SeriesFees(1, 1, "Emmys Lead Drama Actor"),
    "KXEMMYDACTR": SeriesFees(1, 1, "Emmys Lead Drama Actress"),
    "KXEMMYDSERIES": SeriesFees(1, 1, "Emmys Drama Series"),
    "KXETHY": SeriesFees(0, 0, "ETH price EOY"),
    "KXFED": SeriesFees(1, 1, "Fed funds rate"),
    "KXFEDDECISION": SeriesFees(1, 1, "Fed meeting"),
    "KXGAMBLINGREPEAL": SeriesFees(0, 0, "Gambling Repeal"),
    "KXGDP": SeriesFees(1, 1, "US GDP growth"),
    "KXGREENLAND": SeriesFees(0, 0, "Greenland purchase"),
    "KXHEISMAN": SeriesFees(1, 1, "Heisman Trophy Winner"),
    "KXINXY": SeriesFees(1, 1, "S&P 500 yearly range"),
    "KXIPO": SeriesFees(1, 1, "IPOs"),
    "KXIRANDEMOCRACY": SeriesFees(0, 0, "Will Iran become a democracy in 2026?"),
    "KXLALIGA": SeriesFees(1, 1, "LA LIGA"),
    "KXLAYOFFSYINFO": SeriesFees(0, 0, "Tech layoffs"),
    "KXLLM1": SeriesFees(1, 1, "Year-end top LLM"),
    "KXMARMAD": SeriesFees(1, 1, "College Basketball Champion"),
    "KXMENWORLDCUP": SeriesFees(1, 1, "Men's World Cup winner"),
    "KXMLB": SeriesFees(1, 1, "World Series"),
    "KXMLBAL": SeriesFees(1, 1, "MLB American League Championship"),
    "KXMLBASGAME": SeriesFees(1, 1, "Professional Baseball All-Star Game"),
    "KXMLBGAME": SeriesFees(1, 1, "Professional Baseball Game"),
    "KXMLBNL": SeriesFees(1, 1, "MLB National League Championship"),
    "KXNASDAQ100Y": SeriesFees(1, 1, "Nasdaq yearly range"),
    "KXNBA": SeriesFees(1, 1, "Pro Basketball Champion"),
    "KXNBAEAST": SeriesFees(1, 1, "Pro Basketball Eastern Conference Champion"),
    "KXNBAMVP": SeriesFees(1, 1, "Pro Basketball MVP"),
    "KXNBAROY": SeriesFees(1, 1, "Pro Basketball Rookie of the Year"),
    "KXNBAWEST": SeriesFees(1, 1, "Pro Basketball Western Conference Champion"),
    "KXNCAAF": SeriesFees(1, 1, "NCAAF Championship"),
    "KXNCAAFACC": SeriesFees(1, 1, "ACC Champion"),
    "KXNCAAFB10": SeriesFees(1, 1, "Big Ten Champion"),
    "KXNCAAFB12": SeriesFees(1, 1, "Big 12 Champion"),
    "KXNCAAFGAME": SeriesFees(1, 1, "College Football Game"),
    "KXNCAAFPLAYOFF": SeriesFees(1, 1, "College Football Playoff Qualifiers"),
    "KXNCAAFSEC": SeriesFees(1, 1, "SEC Champion"),
    "KXNFLAFCCHAMP": SeriesFees(1, 1, "AFC Champion"),
    "KXNFLAFCEAST": SeriesFees(1, 1, "AFC East Winner"),
    "KXNFLAFCNORTH": SeriesFees(1, 1, "AFC North Winner"),
    "KXNFLAFCSOUTH": SeriesFees(1, 1, "AFC South Winner"),
    "KXNFLAFCWEST": SeriesFees(1, 1, "AFC West Winner"),
    "KXNFLCOTY": SeriesFees(1, 1, "AP Pro Football Coach Of The Year"),
    "KXNFLCPOTY": SeriesFees(1, 1, "AP Pro Football Comeback Player of the Year"),
    "KXNFLDPOTY": SeriesFees(1, 1, "AP Pro Football Defensive Player Of The Year"),
    "KXNFLDROTY": SeriesFees(1, 1, "AP Pro Football Defensive Rookie of the Year"),
    "KXNFLGAME": SeriesFees(1, 1, "Professional Football Game"),
    "KXNFLMVP": SeriesFees(1, 1, "AP Pro Football Regular Season MVP"),
    "KXNFLNFCCHAMP": SeriesFees(1, 1, "NFC Champion"),
    "KXNFLNFCEAST": SeriesFees(1, 1, "NFC East Winner"),
    "KXNFLNFCNORTH": SeriesFees(1, 1, "NFC North Winner"),
    "KXNFLNFCSOUTH": SeriesFees(1, 1, "NFC South Winner"),
    "KXNFLNFCWEST": SeriesFees(1, 1, "NFC West Winner"),
    "KXNFLOPOTY": SeriesFees(1, 1, "AP Pro Football Offensive Player Of The Year"),
    "KXNFLOROTY": SeriesFees(1, 1, "AP Pro Football Offensive Rookie of the Year"),
    "KXNHL": SeriesFees(1, 1, "Stanley Cup"),
    "KXNHLEAST": SeriesFees(1, 1, "Eastern Conference Championship"),
    "KXNHLWEST": SeriesFees(1, 1, "Western Conference Champion"),
    "KXPAHLAVIHEAD": SeriesFees(0, 0, "Will Pahlavi lead Iran?"),
    "KXPAYROLLS": SeriesFees(1, 1, "Jobs numbers"),
    "KXPGARYDER": SeriesFees(1, 1, "Ryder Cup"),
    "KXPGASOLHEIM": SeriesFees(1, 1, "Solheim Cup"),
    "KXPGATOUR": SeriesFees(1, 1, "PGA Tour"),
    "KXRATECUTCOUNT": SeriesFees(1, 1, "Number of rate cuts"),
    "KXSB": SeriesFees(1, 1, "Super Bowl"),
    "KXSUPERBOWLHEADLINE": SeriesFees(1, 1, "Who will headline super bowl LX"),
    "KXU3": SeriesFees(1, 1, "Unemployment"),
    "KXUCL": SeriesFees(1, 1, "UEFA Champions League"),
    "KXUCLGAME": SeriesFees(1, 1, "UEFA Champions League Game"),
    "KXWCGAME": SeriesFees(1, 1, "World Cup Game"),
    "KXWNBA": SeriesFees(1, 1, "WNBA Championship"),
    "KXWNBAGAME": SeriesFees(1, 1, "Women's Pro Basketball Game"),
    "KXWTAMATCH": SeriesFees(1, 1, "WTA Tennis Match"),
}

#: Series the document lists but whose multipliers could not be read
#: unambiguously, mapped to why. These return Unavailable rather than a
#: guess. Guessing here would be worse than refusing: a wrong multiplier on
#: a combo product is a silently wrong cost on every leg.
AMBIGUOUS_SERIES: dict[str, str] = {
    "KXMVE": (
        "the Non-Standard table row 'KXMVE Combos (excluding uncorrelated "
        "NFL combos)' carries the digits '12', which could be maker=1/"
        "taker=2 or a single multiplier of 12; the source PDF's layout does "
        "not disambiguate the two columns"
    ),
}

#: Products with an entirely separate schedule that this module does not
#: model. Perpetual futures are priced in basis points on a 30-day trailing
#: volume tier (12.0bps down to 2.6bps taker, 5.0 to 0.6 maker) — the tier
#: depends on account volume history the ledger does not carry, so no fee
#: can be stated for them from this document alone.
SEPARATE_SCHEDULE_PREFIXES: tuple[str, ...] = ("KXPERP",)


def series_of(ticker: str) -> str:
    """The series code for a market ticker.

    Kalshi tickers are ``SERIES-EVENT-STRIKE``; the series is everything
    before the first hyphen. ``KXBTCD-26AUG2717-B80125`` -> ``KXBTCD``.
    """
    if not ticker:
        return ""
    return ticker.split("-", 1)[0].strip().upper()


def multipliers_for(series: str, *, side: str) -> Union[int, Unavailable]:
    """The published multiplier for ``series`` on ``side``.

    ``side`` is ``"taker"`` or ``"maker"``. A series absent from the
    non-standard table takes the published default, which the document's
    scope sentence establishes affirmatively.
    """
    if side not in ("taker", "maker"):
        raise ValueError(f"side must be 'taker' or 'maker', got {side!r}")
    key = (series or "").strip().upper()
    if not key:
        return Unavailable("no series code — cannot look up a multiplier")
    for prefix in SEPARATE_SCHEDULE_PREFIXES:
        if key.startswith(prefix):
            return Unavailable(
                f"{key} is priced on the perpetual-futures schedule, which is "
                "tiered on 30-day trailing volume the ledger does not carry")
    if key in AMBIGUOUS_SERIES:
        return Unavailable(AMBIGUOUS_SERIES[key])
    entry = SERIES_FEES.get(key)
    if entry is None:
        return (DEFAULT_TAKER_MULTIPLIER if side == "taker"
                else DEFAULT_MAKER_MULTIPLIER)
    return entry.taker_multiplier if side == "taker" else entry.maker_multiplier


def fee_dollars(price_dollars, count: int, *, series: str = "",
                side: str = "taker") -> Fee:
    """Published fee for an order, in dollars.

    Implements ``round up(M x rate x C x P x (1-P))`` exactly as printed,
    with ``C`` inside the rounding and a single ceiling to the cent applied
    to the whole order.

    That placement is the substantive change from the previous model, which
    computed a per-contract figure and let the caller scale it. The two agree
    at large ``C`` and diverge at small ``C``: the published rule charges a
    minimum of one cent on any fee-bearing order, while a per-contract figure
    pro-rates below that. For a single contract at 1c the published fee is
    $0.01 and the pro-rated figure is $0.0007 — a 14x understatement of a
    cost that gets subtracted from edge.

    Returns :class:`Unavailable` when the schedule does not determine a fee.
    """
    if count is None or count <= 0:
        return Unavailable("no contracts — a fee needs a quantity")
    m = multipliers_for(series, side=side)
    if isinstance(m, Unavailable):
        return m
    if m == 0:
        # A published zero, not an absence. KXBTCY and KXETHY really are
        # free, and every standard market really is free on the maker side.
        return Decimal("0.00")

    p = Decimal(str(price_dollars))
    if p < 0 or p > 1:
        return Unavailable(
            f"price {p} is outside $0..$1; a contract price cannot be that")

    rate = TAKER_RATE if side == "taker" else MAKER_RATE
    raw = Decimal(m) * rate * Decimal(int(count)) * p * (Decimal(1) - p)
    return raw.quantize(_CENT, rounding=ROUND_CEILING)


def fee_cents(price_cents, count: int, *, series: str = "",
              side: str = "taker") -> Fee:
    """:func:`fee_dollars` in cents, for callers working in the cent domain.

    The rest of this codebase prices in cents, so this is the form most call
    sites want. The rounding still happens in dollars, because that is where
    the published table defines it.
    """
    got = fee_dollars(Decimal(str(price_cents)) / 100, count,
                      series=series, side=side)
    if isinstance(got, Unavailable):
        return got
    return got * 100


def provenance() -> dict:
    """Where these numbers came from, for the evidence report to render."""
    return {
        "title": SOURCE_TITLE,
        "url": SOURCE_URL,
        "effective_date": SOURCE_EFFECTIVE_DATE,
        "retrieved_date": SOURCE_RETRIEVED_DATE,
        "taker_rate": str(TAKER_RATE),
        "maker_rate": str(MAKER_RATE),
        "default_taker_multiplier": DEFAULT_TAKER_MULTIPLIER,
        "default_maker_multiplier": DEFAULT_MAKER_MULTIPLIER,
        "per_contract_cap": None,
        "per_contract_cap_note": (
            "No cap of any kind appears in the published schedule. A $0.035 "
            "per-contract cap asserted by third-party write-ups is not "
            "supported by the source document."),
        "non_standard_series_count": len(SERIES_FEES),
        "ambiguous_series": sorted(AMBIGUOUS_SERIES),
        "rounding": "ceiling to the cent, applied once to the whole order",
        "rounding_note": (
            "The document's prose says round up 'such that the fee + "
            "positionCost is rounded to a centicent', but its own published "
            "fee table is reproduced exactly (42/42 values) by ceiling the "
            "aggregate to a whole cent, and several published values exceed "
            "what a centicent ceiling would give. The table is implemented; "
            "the prose discrepancy is an open question."),
    }
