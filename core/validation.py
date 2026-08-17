"""
Strict validation for everything that enters the system from outside: Kalshi
market data, and the JSON the Maker and Checker models return.

The rule this file enforces is that bad input becomes a *refusal*, never an
exception that propagates toward execution and never a silently coerced
value. Two concrete failures in the previous code:

- ``Checker.check`` did ``float(parsed["confidence"])`` and
  ``parsed["verdict"]`` straight off the model's JSON. A response of
  ``{"verdict": "approve", "confidence": "very high"}`` raised ValueError
  mid-pass; ``{"verdict": "APPROVE!", "confidence": 1e9}`` sailed through
  with a confidence that clears any threshold. ``float("nan")`` is worse
  still: every comparison against it is False, so a NaN confidence silently
  fails the ``>=`` check while a NaN probability makes every edge NaN.
- ``Scout.scan`` did ``float(m.get("yes_bid", 0))`` with no bounds. A market
  quoting bid 60 / ask 40 (crossed, which happens around halts) produced a
  negative spread that passed the "spread too wide" check, and a midpoint
  outside anything meaningful.

Model output is validated to *abstain*, market data to *skip the market*.
Neither is allowed to raise.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from config import CONFIG

log = logging.getLogger("daemon_kalshi.validation")

#: Kalshi contract prices are integer cents in [1, 99]; 0 and 100 are the
#: settled values, not tradeable quotes. Bounds are inclusive of 0/100 here
#: because an untraded market legitimately quotes bid 0 / ask 100.
MIN_PRICE_CENTS = 0.0
MAX_PRICE_CENTS = 100.0

VALID_VERDICTS = ("approve", "reject", "abstain")
VALID_STRIKE_TYPES = ("greater", "less", "between", "greater_or_equal",
                      "less_or_equal", "custom")


class MarketDataInvalid(Exception):
    """A market failed validation and must not become a Candidate."""


@dataclass
class Quote:
    """The exact quote a decision was made against.

    Recorded so a proposal can be rejected if the market moves between the
    Maker's estimate and submission — P1 item 7 requires the decision to name
    the quote it used rather than re-reading a fresh one at execution time and
    pretending that was what was evaluated.
    """

    yes_bid: float
    yes_ask: float
    captured_at: float
    #: Where captured_at came from: "exchange" if the market payload carried a
    #: quote timestamp, "scan" if we fell back to our own read time. A "scan"
    #: timestamp bounds how stale the quote can be by our clock, but says
    #: nothing about how long it had already been sitting on Kalshi's side.
    source: str = "scan"

    @property
    def age_seconds(self) -> float:
        import time

        return max(time.time() - self.captured_at, 0.0)

    def is_stale(self, max_age: float = None) -> bool:
        limit = max_age if max_age is not None else CONFIG.risk.max_quote_age_seconds
        return self.age_seconds > limit

    @property
    def midpoint_cents(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2.0

    def executable_price_cents(self, direction: str) -> float:
        """What we would actually pay per contract, buying `direction`.

        Buying YES lifts the YES ask. Buying NO lifts the NO ask, which is
        ``100 - yes_bid``. This is the number that matters for both sizing and
        edge; the midpoint is not a price anyone can trade at.
        """
        return self.yes_ask if direction == "yes" else (MAX_PRICE_CENTS - self.yes_bid)


# -- numeric helpers -------------------------------------------------------


def finite(value: Any) -> Optional[float]:
    """Coerce to float, rejecting None, non-numerics, NaN and infinity.

    ``float("nan")`` and ``float("inf")`` both parse happily from JSON, and
    NaN is the dangerous one: every comparison against it is False, so it
    fails threshold checks silently rather than loudly.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def in_unit_interval(value: Any) -> Optional[float]:
    out = finite(value)
    if out is None or not (0.0 <= out <= 1.0):
        return None
    return out


def clamp_text(value: Any, limit: int, fallback: str = "") -> str:
    """Bound free text from a model or an external feed.

    Unbounded model text ends up in the database, in the next prompt as
    playbook context, and in log lines. A model that loops can emit megabytes.
    """
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[:limit] + f"… [truncated from {len(value)} chars]"


def parse_timestamp(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
        if not math.isfinite(out) or out <= 0:
            return None
        # Kalshi mixes seconds and milliseconds across endpoints; anything
        # past year ~2286 in seconds is milliseconds.
        return out / 1000.0 if out > 1e11 else out
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


# -- Scout market data -----------------------------------------------------

#: Fields that would carry a genuine *quote* timestamp, if Kalshi returned
#: one. It appears not to.
#:
#: VERIFIED AGAINST PRODUCTION, 2026-08-15: this list used to include
#: ``last_price_time``, and that was a misreading with a large blast radius.
#: ``last_price_time`` is the time of the market's last *trade*, not the age
#: of its order book. Treating it as quote freshness rejected every market
#: that had not traded within MAX_QUOTE_AGE_SECONDS — 47,000 of 80,000 open
#: markets, with ages like "1577s old, limit 60s". A market that last traded
#: 26 minutes ago is perfectly tradeable; its bid and ask are current, it is
#: simply quiet.
#:
#: So the honest position is that we have no exchange-side quote timestamp.
#: Freshness is bounded by our own read time, which is what
#: MAX_QUOTE_AGE_SECONDS now measures: how long ago *we* fetched the price.
#: That is a real and useful bound — it stops a proposal being acted on
#: minutes after the price was read — it just cannot tell us how long the
#: book had been sitting unchanged before we looked.
_QUOTE_TIME_FIELDS: tuple[str, ...] = ()


#: Price fields, as (field name, multiplier to convert to cents).
#:
#: VERIFIED AGAINST PRODUCTION, 2026-08-15, by logging the actual keys Kalshi
#: returns. The live schema is:
#:
#:   yes_bid_dollars, yes_ask_dollars, no_bid_dollars, no_ask_dollars,
#:   last_price_dollars, volume_fp, volume_24h_fp, open_interest_fp,
#:   liquidity_dollars, updated_time, ...
#:
#: There is no `yes_bid` and no `volume`. Kalshi removed the integer-cent
#: fields in March 2026 — this repo's own client docstring says so, and the
#: order-placement path was updated for it, but the read path never was. So
#: every price and liquidity read silently found nothing.
#:
#: The `_dollars` fields are dollar-denominated (0.52, not 52), hence the
#: x100. Legacy cent names are still accepted first so an older or
#: differently-versioned endpoint keeps working.
PRICE_FIELDS = {
    "yes_bid": (("yes_bid", 1.0), ("yes_bid_dollars", 100.0)),
    "yes_ask": (("yes_ask", 1.0), ("yes_ask_dollars", 100.0)),
    # Read only by mark_price_cents below. Kalshi quotes the NO book
    # directly, so a NO position is marked against Kalshi's own number
    # rather than one derived from the YES side.
    "no_bid": (("no_bid", 1.0), ("no_bid_dollars", 100.0)),
    "no_ask": (("no_ask", 1.0), ("no_ask_dollars", 100.0)),
}

#: Liquidity signals, best first. `liquidity_dollars` is the USD figure that
#: MIN_LIQUIDITY_USD is named for; the `_fp` fields are contract counts and
#: serve as fallbacks. Read all of them and take the largest rather than
#: trusting one name — guessing a single name is what caused three separate
#: bugs in this file.
LIQUIDITY_FIELDS = (
    "liquidity_dollars", "notional_value_dollars",
    "volume_fp", "volume_24h_fp", "open_interest_fp",
    "volume", "volume_24h", "open_interest", "liquidity",
)


def _price_cents(raw: dict, key: str):
    """(value_in_cents, was_present). Handles both cent and dollar schemas."""
    for name, multiplier in PRICE_FIELDS[key]:
        if name not in raw or raw.get(name) is None:
            continue
        value = finite(raw.get(name))
        if value is None:
            return None, True          # present but unusable -> bad data
        return value * multiplier, True
    return None, False                 # absent -> "no resting order"


def mark_price_cents(raw: dict, side: str) -> Optional[float]:
    """What one contract of ``side`` could actually be sold for right now.

    Marked to the *bid* on the side held, not the midpoint. This number feeds
    a loss control, and the midpoint values a position at a price nobody is
    currently offering — it reports the book as friendlier than it is, in the
    one place where being flattered is most expensive. Marking to the bid is
    the same conservatism the entry path already applies in
    ``executable_price_cents``, pointed the other way.

    Returns ``None`` when the market quotes nothing usable. That is not zero:
    a market with no bid has not become worthless, we simply cannot say what
    it is worth, and the caller has to handle not knowing. Treating it as
    zero would book a total loss on every illiquid position.
    """
    if side not in ("yes", "no"):
        return None
    value, _ = _price_cents(raw, "no_bid" if side == "no" else "yes_bid")
    if value is None:
        # No direct quote for that side. The opposite ask is the same number
        # by construction (a bid of 40 on NO is an ask of 60 on YES), so it
        # is a derivation rather than a guess — but only ever a fallback,
        # because Kalshi's own field is authoritative when it is present.
        opposite, _ = _price_cents(raw, "no_ask" if side == "yes" else "yes_ask")
        if opposite is None:
            return None
        value = 100.0 - opposite
    if not 0.0 <= value <= 100.0:
        # Crossed or nonsensical book. Same rule as everywhere else in this
        # file: unusable input is refused, never coerced into range.
        return None
    return value


def _best_liquidity(raw: dict):
    """Largest usable liquidity number on the payload, or None if absent.

    None is meaningfully different from 0.0: it means Kalshi told us nothing
    about this market's liquidity, not that the market is untraded. Callers
    must not filter a None as though it were zero.
    """
    seen = []
    for name in LIQUIDITY_FIELDS:
        if name not in raw:
            continue
        value = finite(raw.get(name))
        if value is not None:
            seen.append(value)
    return max(seen) if seen else None


@dataclass
class ValidatedMarket:
    ticker: str
    title: str
    quote: Quote
    volume: Optional[float]
    close_time: str
    seconds_to_close: float
    strike_type: str = ""
    floor_strike: Optional[float] = None
    cap_strike: Optional[float] = None
    warnings: list = field(default_factory=list)


def validate_market(raw: dict, event: dict = None, now: float = None) -> ValidatedMarket:
    """Validate one Kalshi market payload before it becomes a Candidate.

    Raises MarketDataInvalid with a specific reason. Scout catches it, counts
    it and moves on — one malformed market must not abort a whole scan.
    """
    import time

    event = event or {}
    now = now if now is not None else time.time()

    ticker = raw.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise MarketDataInvalid("missing ticker")
    ticker = ticker.strip()

    title = raw.get("title") or event.get("title")
    if not isinstance(title, str) or not title.strip():
        raise MarketDataInvalid(f"{ticker}: missing title")
    title = clamp_text(title, CONFIG.risk.max_title_chars)

    # A market with nothing resting on one side returns null for that price.
    # VERIFIED AGAINST PRODUCTION: this is the common case, not an anomaly —
    # roughly 3,000 of every 5,000 open markets have a null yes_bid. Treating
    # null as malformed data was wrong twice over: it rejected the entire
    # catalog, and it logged a data-quality alarm for what is really just an
    # untraded market.
    #
    # No bid means nobody will buy from us at any price, i.e. an effective
    # bid of 0. No ask means nobody will sell to us, i.e. an effective ask of
    # 100. Encoding it that way lets the ordinary liquidity and spread gates
    # decide — a 0/100 market has a 100c spread and almost always zero
    # volume, so it is filtered downstream as untradeable rather than
    # reported as broken.
    #
    # A price that is *present* but not a number is still bad data and still
    # raises: null and "banana" are different problems.
    bid_value, bid_present = _price_cents(raw, "yes_bid")
    ask_value, ask_present = _price_cents(raw, "yes_ask")
    missing_bid = not bid_present
    missing_ask = not ask_present
    if bid_present and bid_value is None:
        raise MarketDataInvalid(f"{ticker}: yes_bid is not a finite number")
    if ask_present and ask_value is None:
        raise MarketDataInvalid(f"{ticker}: yes_ask is not a finite number")
    yes_bid = 0.0 if missing_bid else bid_value
    yes_ask = MAX_PRICE_CENTS if missing_ask else ask_value
    for name, price in (("yes_bid", yes_bid), ("yes_ask", yes_ask)):
        if not (MIN_PRICE_CENTS <= price <= MAX_PRICE_CENTS):
            raise MarketDataInvalid(
                f"{ticker}: {name} {price} outside {MIN_PRICE_CENTS}-{MAX_PRICE_CENTS}c"
            )
    if yes_bid > yes_ask:
        # Crossed book. Happens around halts and bad ticks; the old code let
        # it through as a negative spread, which passed the max-spread check.
        raise MarketDataInvalid(
            f"{ticker}: crossed book, bid {yes_bid}c > ask {yes_ask}c"
        )

    # Liquidity, from whichever field Kalshi actually populates.
    #
    # VERIFIED AGAINST PRODUCTION, 2026-08-15: reading only `volume` and
    # defaulting a missing key to 0 reported all 79,947 open markets as having
    # zero volume, so every one of them fell under MIN_LIQUIDITY_USD and the
    # bot concluded there was nothing to trade. Same failure shape as the
    # yes_bid bug: an absent field silently becomes a value that filters
    # everything out, with no error anywhere.
    #
    # `volume` and `volume_24h` are contract counts; `open_interest` is
    # contracts outstanding. Any of them is a usable liquidity proxy, and the
    # largest is the least likely to be a spuriously quiet window.
    #
    # None (rather than 0) means "no liquidity field was present at all",
    # which Scout must not treat as "illiquid" — see its handling.
    volume = _best_liquidity(raw)
    if volume is not None and volume < 0:
        raise MarketDataInvalid(f"{ticker}: volume {volume!r} is negative")

    close_time = raw.get("close_time") or event.get("close_time") or ""
    close_ts = parse_timestamp(close_time)
    if close_ts is None:
        raise MarketDataInvalid(f"{ticker}: unparseable close_time {close_time!r}")
    seconds_to_close = close_ts - now
    if seconds_to_close <= 0:
        raise MarketDataInvalid(
            f"{ticker}: closes in {seconds_to_close:.0f}s (already closed)"
        )

    strike_type = raw.get("strike_type") or ""
    if not isinstance(strike_type, str):
        raise MarketDataInvalid(f"{ticker}: strike_type is not a string")
    strike_type = strike_type.strip().lower()
    if strike_type and strike_type not in VALID_STRIKE_TYPES:
        # Not fatal: the quant path checks strike_type itself and an unknown
        # one just means the LLM path handles this market instead.
        log.debug("%s: unrecognised strike_type %r", ticker, strike_type)

    floor_strike = finite(raw.get("floor_strike"))
    cap_strike = finite(raw.get("cap_strike"))
    if raw.get("floor_strike") is not None and floor_strike is None:
        raise MarketDataInvalid(
            f"{ticker}: floor_strike {raw.get('floor_strike')!r} is not finite"
        )
    if raw.get("cap_strike") is not None and cap_strike is None:
        raise MarketDataInvalid(
            f"{ticker}: cap_strike {raw.get('cap_strike')!r} is not finite"
        )
    if strike_type == "between":
        if floor_strike is None or cap_strike is None:
            raise MarketDataInvalid(
                f"{ticker}: strike_type 'between' needs both floor and cap strikes"
            )
        if floor_strike >= cap_strike:
            raise MarketDataInvalid(
                f"{ticker}: floor strike {floor_strike} >= cap strike {cap_strike}"
            )

    warnings: list[str] = []
    if missing_bid:
        warnings.append("no resting bid — treated as 0c (nothing to sell into)")
    if missing_ask:
        warnings.append("no resting ask — treated as 100c (nothing to buy from)")
    quote_ts, quote_source = None, "scan"
    for candidate_field in _QUOTE_TIME_FIELDS:
        if candidate_field in raw:
            quote_ts = parse_timestamp(raw[candidate_field])
            if quote_ts is not None:
                quote_source = "exchange"
            break
    if quote_ts is None:
        quote_ts = now
        warnings.append(
            "no quote timestamp on the market payload — freshness is bounded "
            "by our own read time only"
        )
    elif quote_ts > now + 60:
        raise MarketDataInvalid(
            f"{ticker}: quote timestamp is {quote_ts - now:.0f}s in the future"
        )

    quote = Quote(yes_bid=yes_bid, yes_ask=yes_ask, captured_at=quote_ts,
                  source=quote_source)
    if quote.is_stale():
        raise MarketDataInvalid(
            f"{ticker}: quote is {quote.age_seconds:.0f}s old, limit "
            f"{CONFIG.risk.max_quote_age_seconds:.0f}s"
        )

    return ValidatedMarket(
        ticker=ticker,
        title=title,
        quote=quote,
        volume=volume,
        close_time=str(close_time),
        seconds_to_close=seconds_to_close,
        strike_type=strike_type,
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        warnings=warnings,
    )


# -- model output ----------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(raw: Any) -> Optional[dict]:
    """Parse a model response into a dict, tolerating fenced code blocks.

    Returns None rather than raising. Models wrap JSON in ```json fences
    often enough that failing on it would throw away usable answers, but
    anything beyond "find the outermost object" is guessing.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def repair_truncated_json(raw: Any) -> Optional[dict]:
    """Recover the complete leading fields of a JSON object cut off mid-write.

    Returns None unless a strict prefix of ``raw`` parses as an object.

    This repairs nothing and infers nothing. It finds a point where the model
    had *finished* writing a field, closes the object there, and parses what
    was actually written — so every returned value is one the model emitted in
    full. A field still being written is dropped, never completed.

    Production kept producing this, several times an hour::

        {"verdict": "reject", "confidence": 0.65, "reasoning": "The maker's
         reasoning is generic and lacks specific evidence about actual Senate
         scheduling for CLARITY Act, which has had significant bipartisa

    A real verdict at a real confidence, discarded as "unparseable" because
    the prose ran out of room. :func:`validate_checker_output` governs when a
    recovered verdict may be acted on — and it is not "always".
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    start = text.find("{")
    if start < 0:
        return None
    text = text[start:]

    # Every position where a field ended at the top level of the object: a
    # comma at depth 1, outside a string. Closing there yields exactly the
    # fields written before it.
    cuts: list[int] = []
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = in_string
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 1:
            cuts.append(i)

    # Latest cut first, to recover as many complete fields as possible.
    for cut in reversed(cuts):
        try:
            parsed = json.loads(text[:cut] + "}")
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


#: How much of an unparseable model response to keep in the log.
#:
#: Was 200, which is why two production Checker parse failures
#: (KXRAINSHARD2-26AUG15-NYC at 00:38:43Z, -PHIL at 00:58:17Z) could not be
#: diagnosed: the logged payload ended mid-sentence, and the clip meant there
#: was no way to tell whether the *model* stopped there or the *logger* did.
#: Those two readings have opposite fixes — a token budget versus a prompt or
#: parser problem — so a log that cannot distinguish them sends the next
#: investigation in a random direction. It already sent one.
#:
#: Bounded rather than unbounded: a looping model can emit megabytes, and this
#: text goes to the log stream. 4000 comfortably exceeds a complete verdict
#: (the Checker's whole budget is CHECKER_MAX_TOKENS=4000 for thinking *and*
#: answer together), so a clip here now means genuinely anomalous output.
MAX_UNPARSEABLE_LOG_CHARS = 4000


def _describe_unparseable(raw: Any) -> str:
    """Render an unparseable payload for the log, stating what was withheld.

    Reports the true length alongside the excerpt so a clipped log line can
    never again be mistaken for a short response.
    """
    text = raw if isinstance(raw, str) else repr(raw)
    total = len(text)
    if total <= MAX_UNPARSEABLE_LOG_CHARS:
        return f"[{total} chars, complete] {text}"
    return (
        f"[{total} chars, showing first {MAX_UNPARSEABLE_LOG_CHARS} — clipped "
        f"by the logger, NOT by the model] "
        f"{text[:MAX_UNPARSEABLE_LOG_CHARS]}"
    )


@dataclass
class MakerOutput:
    probability_yes: float
    confidence: float
    reasoning: str


def validate_maker_output(raw: Any, ticker: str = "") -> Optional[MakerOutput]:
    """Validate the Maker model's JSON. Returns None to mean "no proposal".

    None is a refusal, not an error: the market is skipped this pass. Every
    rejection is logged with the reason, because a Maker that has quietly
    started emitting prose instead of JSON otherwise looks identical to a
    Maker that simply finds no edges.
    """
    parsed = extract_json(raw)
    if parsed is None:
        log.warning("Maker returned unparseable JSON for %s: %s",
                    ticker, _describe_unparseable(raw))
        return None

    probability = in_unit_interval(parsed.get("probability_yes"))
    if probability is None:
        log.warning(
            "Maker probability_yes invalid for %s: %r (need a finite number "
            "in [0,1])", ticker, parsed.get("probability_yes"),
        )
        return None

    confidence = in_unit_interval(parsed.get("confidence"))
    if confidence is None:
        log.warning(
            "Maker confidence invalid for %s: %r", ticker, parsed.get("confidence")
        )
        return None

    reasoning = clamp_text(parsed.get("reasoning"), CONFIG.risk.max_reasoning_chars)
    if not reasoning:
        log.warning("Maker gave no reasoning for %s", ticker)
        return None

    return MakerOutput(probability_yes=probability, confidence=confidence,
                       reasoning=reasoning)


@dataclass
class CheckerOutput:
    verdict: str
    confidence: float
    reasoning: str


#: Returned whenever Checker output cannot be trusted. Abstain is the safe
#: default: it is not an approval, and it is distinguishable from a real
#: reject in the calibration data.
def abstention(reason: str) -> CheckerOutput:
    return CheckerOutput(verdict="abstain", confidence=0.0, reasoning=reason)


def validate_checker_output(raw: Any, ticker: str = "") -> CheckerOutput:
    """Validate the Checker model's JSON. Always returns a CheckerOutput.

    Never raises and never returns an approval it is unsure about — the brief
    is explicit that invalid output must abstain rather than throw an
    exception that continues toward execution.
    """
    truncated = False
    parsed = extract_json(raw)
    if parsed is None:
        # Recovery first: a response cut off mid-prose usually still carries a
        # complete verdict, and discarding it records a real judgement as a
        # parse error.
        parsed = repair_truncated_json(raw)
        truncated = parsed is not None
        if parsed is None:
            log.warning("Checker returned unparseable JSON for %s: %s",
                        ticker, _describe_unparseable(raw))
            return abstention("parse_error")

    verdict = parsed.get("verdict")
    if not isinstance(verdict, str):
        log.warning("Checker verdict is not a string for %s: %r", ticker, verdict)
        return abstention("invalid_verdict_type")
    verdict = verdict.strip().lower()
    if verdict not in VALID_VERDICTS:
        # Deliberately exact: no prefix matching, no "approved" -> "approve".
        # A model that has drifted off the contract should abstain, not have
        # its output guessed at.
        log.warning(
            "Checker verdict %r for %s is not one of %s — abstaining",
            parsed.get("verdict"), ticker, VALID_VERDICTS,
        )
        return abstention("unknown_verdict")

    if truncated and verdict == "approve":
        # Asymmetric on purpose. A recovered "reject" can only ever refuse a
        # trade, so acting on it is safe and keeps a real judgement out of the
        # calibration data as a parse error. A recovered "approve" would
        # authorise real money on a response whose reasoning was cut off
        # before it finished — and the caveat that would have changed the
        # verdict is exactly the part most likely to be missing.
        log.warning(
            "Checker approval for %s was recovered from a truncated response "
            "— abstaining. A cut-off approval is not an approval.", ticker,
        )
        return abstention("truncated_approval")

    confidence = in_unit_interval(parsed.get("confidence"))
    if confidence is None:
        log.warning(
            "Checker confidence invalid for %s: %r — abstaining",
            ticker, parsed.get("confidence"),
        )
        return abstention("invalid_confidence")

    reasoning = clamp_text(parsed.get("reasoning"), CONFIG.risk.max_reasoning_chars,
                           fallback="(no reasoning given)")
    if truncated:
        # Marked in the stored reasoning, so a ledger row cannot later be read
        # as a complete judgement when it was a recovered fragment.
        log.warning(
            "Checker response for %s was cut off; recovered verdict=%s "
            "confidence=%.2f from the complete fields", ticker, verdict, confidence,
        )
        reasoning = f"(recovered from a truncated response) {reasoning}"
    return CheckerOutput(verdict=verdict, confidence=confidence, reasoning=reasoning)
