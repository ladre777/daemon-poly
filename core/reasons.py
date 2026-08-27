"""
Stable identifiers for what happened to a candidate, and why.

Every stage a candidate passes through, every reason it stopped, and every
way a provider can fail has exactly one spelling here. That matters more
than it looks.

The failure this exists to prevent is *reason collapse*. In production on
2026-08-27 a pass funnel read ``llm_called=0 llm_disabled=109``, and those
109 rows covered at least four genuinely different conditions:

    - Moonshot returned 429 with "insufficient balance" — a billing failure,
      fixed with money
    - Gemini returned 429 with "retry in 11.1s" — per-minute throttling,
      fixed by waiting
    - Anthropic returned 400 "credit balance is too low" — a billing failure
      on a different vendor
    - no provider was configured at all — fixed with configuration

All four rendered as one number. An operator reading it cannot tell which
remedy applies, and three of the four remedies are wrong for any given case.
Money spent on the wrong vendor is the cheapest bad outcome available there.

So: no generic ``failed``. No generic ``no_trade``. If two conditions have
different remedies, they get different constants, and the reporting layer
counts them apart.

These are strings rather than ints because they are written to a database
that outlives any particular build, and read back by a report that may be
older or newer than the writer. A string that no longer maps to a known
constant is still legible to a human reading the table; an integer is not.
"""
from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    """Where in a pass a candidate got to.

    Ordered by progression, and the order is load-bearing: the funnel report
    asserts that counts are non-increasing along it, which is how a
    miscounted stage shows up as an impossible funnel rather than as a
    plausible-looking wrong number.
    """

    SCOUTED = "scouted"
    PRICED = "priced"
    PROPOSED = "proposed"
    CHECKER_SELECTED = "checker_selected"
    CHECKER_VERDICT = "checker_verdict"
    RISK_DECISION = "risk_decision"
    INTENT = "intent"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    FILLED = "filled"
    SETTLED = "settled"


#: Progression order, for the monotonicity check described above.
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.SCOUTED,
    Stage.PRICED,
    Stage.PROPOSED,
    Stage.CHECKER_SELECTED,
    Stage.CHECKER_VERDICT,
    Stage.RISK_DECISION,
    Stage.INTENT,
    Stage.SUBMITTED,
    Stage.ACKNOWLEDGED,
    Stage.FILLED,
    Stage.SETTLED,
)


class Reason(str, Enum):
    """Why a candidate stopped advancing.

    Grouped by the layer that decided. Every member names a condition with a
    distinct operator remedy — that is the test for whether something
    deserves its own constant.
    """

    # -- coverage: the candidate never became priceable ------------------
    #: Scan hit its page cap with catalog remaining. Distinct from
    #: NO_CANDIDATES: markets may exist that were never looked at.
    SCAN_CAP_REACHED = "scan_cap_reached"
    #: The scan completed and simply found nothing suitable. The universe was
    #: fully examined. Opposite remedy to SCAN_CAP_REACHED — nothing to raise.
    NO_CANDIDATES = "no_candidates"
    #: No verified contract specification for this ticker family, so the
    #: quant path cannot price it at all. Fixed by adding a spec, never by
    #: loosening a threshold.
    NO_CONTRACT_SPEC = "no_contract_spec"
    #: Priced fine, but the market itself failed a quality rule (liquidity
    #: floor, stale quote, absent book). Distinct from NO_CONTRACT_SPEC: the
    #: bot knows how to price this family, this instance was unusable.
    MARKET_QUALITY = "market_quality"
    CATEGORY_OUT_OF_SCOPE = "category_out_of_scope"

    # -- provider: an LLM was needed and could not be reached -------------
    #: No provider configured or importable. Configuration, not money.
    PROVIDER_UNCONFIGURED = "provider_unconfigured"
    #: HTTP 429 with a retry hint — throughput throttling. Waiting fixes it.
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    #: HTTP 429/400 naming an exhausted balance or credit. Money fixes it,
    #: and only on the vendor that reported it.
    PROVIDER_BILLING = "provider_billing"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_ERROR = "provider_error"
    #: Circuit breaker open, so the call was never attempted.
    PROVIDER_BREAKER_OPEN = "provider_breaker_open"

    # -- model output: a provider answered, the answer was unusable -------
    SCHEMA_PARSE_FAILURE = "schema_parse_failure"
    #: Response ran out of tokens. Fixed by raising the token cap, which
    #: loosens no gate — not by changing the prompt or the thresholds.
    TRUNCATED = "truncated"
    #: A truncated response that recovered a usable ``reject``. Acted on,
    #: because a reject can only ever refuse a trade. Counted separately so
    #: the truncation rate stays visible even while it is being handled.
    TRUNCATED_REJECT_RECOVERED = "truncated_reject_recovered"
    #: A truncated response whose recovered verdict was ``approve``, and was
    #: therefore abstained. This is the fail-closed rule doing its job, and
    #: it must never be pooled with TRUNCATED_REJECT_RECOVERED: one refused a
    #: trade the model wanted to refuse, the other refused a trade the model
    #: wanted to take.
    TRUNCATED_APPROVAL_ABSTAINED = "truncated_approval_abstained"
    UNKNOWN_VERDICT = "unknown_verdict"
    INVALID_CONFIDENCE = "invalid_confidence"

    # -- rationing: the bot chose not to spend a call ---------------------
    LADDER_DEDUPED = "ladder_deduped"
    CAPPED_PER_EVENT = "capped_per_event"
    # noqa justification: S105 matches the substring "pass" and reads this as
    # a credential. It is a trading pass, not a password.
    CAPPED_PER_PASS = "capped_per_pass"  # noqa: S105
    SKIPPED_INCOHERENT = "skipped_incoherent"

    # -- judgement and risk: the bot decided against the trade ------------
    BELOW_EDGE_THRESHOLD = "below_edge_threshold"
    CHECKER_REJECTED = "checker_rejected"
    CHECKER_ABSTAINED = "checker_abstained"
    #: Risk declined for a populated account — a real risk judgement.
    RISK_REFUSED = "risk_refused"
    #: Risk declined because the exchange balance is zero, so there was no
    #: judgement to make. Kept apart from RISK_REFUSED on purpose: a run of
    #: "risk refused everything" reads as a working gate, when in fact the
    #: gate was never consulted. The 2026-08 paper window is entirely this.
    ZERO_BALANCE = "zero_balance"
    KILL_SWITCH = "kill_switch"
    STALE_RECONCILIATION = "stale_reconciliation"
    UNKNOWN_ORDER_STATE = "unknown_order_state"

    # -- execution --------------------------------------------------------
    #: Suppressed because DRY_RUN is true. Not an execution failure, and must
    #: never be counted as one.
    DRY_RUN = "dry_run"
    DUPLICATE_BLOCKED = "duplicate_blocked"
    QUOTE_REFRESH_FAILED = "quote_refresh_failed"
    SUBMIT_REJECTED = "submit_rejected"
    NO_FILL = "no_fill"
    PARTIAL_FILL = "partial_fill"


#: Reasons that mean "a vendor could not serve us", split by remedy. The
#: readiness report counts these three groups separately and never sums them.
BILLING_REASONS = frozenset({Reason.PROVIDER_BILLING})
THROTTLE_REASONS = frozenset({Reason.PROVIDER_RATE_LIMITED, Reason.PROVIDER_BREAKER_OPEN})
PROVIDER_FAULT_REASONS = frozenset({
    Reason.PROVIDER_TIMEOUT, Reason.PROVIDER_ERROR, Reason.PROVIDER_UNCONFIGURED,
})

#: Reasons that indicate truncation, in either disposition.
TRUNCATION_REASONS = frozenset({
    Reason.TRUNCATED,
    Reason.TRUNCATED_REJECT_RECOVERED,
    Reason.TRUNCATED_APPROVAL_ABSTAINED,
})


class Mode(str, Enum):
    """How much a row's economics are worth believing.

    These three are never summed. The mapping from ``edges.action_taken``
    matches :meth:`memory.edge_store.EdgeStore.calibration_by_category`
    exactly, and :func:`mode_for_action` is the single place it is written.
    """

    #: Really traded, on the exchange, with real money. PnL is money.
    LIVE = "live"
    #: Approved but simulated, or filled nothing. PnL is what the quote at
    #: decision time would have returned net of modelled fees.
    PAPER = "paper"
    #: The Checker or risk declined it. PnL is what the trade *would* have
    #: made — useful for finding a gate refusing winners, and not a result.
    REFUSED = "refused"


#: ``edges.action_taken`` values that mean the row really traded.
_LIVE_ACTIONS = frozenset({"executed"})
#: ``action_taken`` values that mean it was approved but not really filled.
_PAPER_ACTIONS = frozenset({"dry_run", "no_fill"})


def mode_for_action(action_taken: str | None) -> Mode:
    """Classify a ledger row. Mirrors the SQL CASE in ``EdgeStore``.

    Anything unrecognised — including ``None`` — falls to ``REFUSED``, which
    is the conservative direction: an unclassifiable row is quarantined out
    of live and paper totals rather than inflating them.
    """
    if action_taken in _LIVE_ACTIONS:
        return Mode.LIVE
    if action_taken in _PAPER_ACTIONS:
        return Mode.PAPER
    return Mode.REFUSED


class Readiness(str, Enum):
    """How much evidence exists, never whether to trade.

    There is deliberately no ``LIVE_READY``. This assessment is one input to
    a human decision about deploying capital; naming a state that sounds like
    the decision itself invites it being read as the decision. The best
    available outcome is MEASUREMENT_READY — "the instrumentation is sound
    enough that a separately reviewed paper-to-live decision could now be
    based on it".
    """

    #: The requirement does not apply in this configuration.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: Nothing disqualifying found, but not enough observations to conclude.
    #: The default. A report that cannot prove otherwise says this.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    #: Something actively disqualifying: kill switch, unknown orders, stale
    #: reconciliation, mode contamination.
    BLOCKED = "BLOCKED"
    #: Enough fill-verified, mode-separated evidence to support a review.
    MEASUREMENT_READY = "MEASUREMENT_READY"


#: Worst-to-best, for combining per-requirement states into an overall one.
#: BLOCKED outranks INSUFFICIENT_EVIDENCE because a disqualifying finding is
#: more urgent than a thin sample, and NOT_APPLICABLE never lifts a result.
_SEVERITY: dict[Readiness, int] = {
    Readiness.BLOCKED: 0,
    Readiness.INSUFFICIENT_EVIDENCE: 1,
    Readiness.MEASUREMENT_READY: 2,
    Readiness.NOT_APPLICABLE: 3,
}


def worst(states) -> Readiness:
    """Combine requirement states conservatively.

    An empty set is INSUFFICIENT_EVIDENCE, not MEASUREMENT_READY: having
    checked nothing is not the same as having checked everything and found
    nothing wrong. All-NOT_APPLICABLE collapses to INSUFFICIENT_EVIDENCE for
    the same reason — no requirement actually demonstrated anything.
    """
    states = list(states)
    if not states:
        return Readiness.INSUFFICIENT_EVIDENCE
    ranked = min(states, key=lambda s: _SEVERITY[Readiness(s)])
    if Readiness(ranked) is Readiness.NOT_APPLICABLE:
        return Readiness.INSUFFICIENT_EVIDENCE
    return Readiness(ranked)


#: Translation from ``main.run_once``'s internal ``stats`` keys to the
#: (stage, reason) taxonomy above.
#:
#: This table exists because the funnel counters grew organically as a log
#: line and their names encode the code path rather than the condition —
#: ``llm_disabled`` and ``llm_rate_limited`` are both "no LLM answer", but
#: one is fixed with configuration and the other by waiting. Mapping them
#: here is what lets the report count them apart without renaming counters
#: that other code and several tests already depend on.
#:
#: Anything absent from this table is still recorded, under its own raw key
#: with stage ``pass_stat`` — see :func:`stats_to_events`. Silently dropping
#: an unmapped counter would reintroduce exactly the blindness this module
#: was written to remove.
STATS_TO_STAGE_REASON: dict[str, tuple[str, str | None]] = {
    "quant_attempted": (Stage.PRICED.value, None),
    "quant_below_edge_threshold": (Stage.PRICED.value, Reason.BELOW_EDGE_THRESHOLD.value),
    "quant_no_proposal": (Stage.PRICED.value, Reason.MARKET_QUALITY.value),

    "llm_disabled": (Stage.PRICED.value, Reason.PROVIDER_UNCONFIGURED.value),
    "llm_rate_limited": (Stage.PRICED.value, Reason.PROVIDER_RATE_LIMITED.value),
    "maker_failed": (Stage.PRICED.value, Reason.PROVIDER_ERROR.value),
    "llm_skipped_tainted": (Stage.PRICED.value, Reason.SKIPPED_INCOHERENT.value),
    "llm_capped_per_event": (Stage.PRICED.value, Reason.CAPPED_PER_EVENT.value),
    "llm_capped": (Stage.PRICED.value, Reason.CAPPED_PER_PASS.value),
    "llm_below_edge_threshold": (Stage.PRICED.value, Reason.BELOW_EDGE_THRESHOLD.value),
    "no_grounding_source": (Stage.PRICED.value, Reason.MARKET_QUALITY.value),

    "proposed": (Stage.PROPOSED.value, None),
    "incoherent": (Stage.PROPOSED.value, Reason.SKIPPED_INCOHERENT.value),
    "ladder_deduped": (Stage.CHECKER_SELECTED.value, Reason.LADDER_DEDUPED.value),

    "checked": (Stage.CHECKER_VERDICT.value, None),
    "checker_rejected": (Stage.CHECKER_VERDICT.value, Reason.CHECKER_REJECTED.value),
    "checker_failed": (Stage.CHECKER_VERDICT.value, Reason.PROVIDER_ERROR.value),

    "quote_refresh_failed": (Stage.RISK_DECISION.value, Reason.QUOTE_REFRESH_FAILED.value),
    "risk_refused": (Stage.RISK_DECISION.value, Reason.RISK_REFUSED.value),
    "approved": (Stage.RISK_DECISION.value, None),
    "duplicate_blocked": (Stage.INTENT.value, Reason.DUPLICATE_BLOCKED.value),
}


def stats_to_events(stats, *, zero_balance: bool = False) -> dict:
    """Translate a pass ``stats`` mapping into ``{(stage, reason): count}``.

    ``zero_balance`` reclassifies risk refusals. While the exchange balance
    is zero, risk refuses upstream of every gate, so those refusals are not
    judgements about the trade — recording them as ``RISK_REFUSED`` would
    make an unfunded account look like a working, discriminating gate. That
    misreading is precisely what the 2026-08 paper window invited.

    Never raises: a malformed value is skipped rather than allowed to
    propagate into the trading path that called this.
    """
    out: dict = {}
    for key, count in (stats or {}).items():
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        if not n:
            continue
        if key == "risk_refused" and zero_balance:
            out[(Stage.RISK_DECISION.value, Reason.ZERO_BALANCE.value)] = n
            continue
        stage, reason = STATS_TO_STAGE_REASON.get(key, ("pass_stat", key))
        out[(stage, reason)] = out.get((stage, reason), 0) + n
    return out
