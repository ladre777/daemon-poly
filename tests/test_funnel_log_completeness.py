"""
The funnel line has to print the numbers it already holds.

``stats["checker_rejected"]`` and ``stats["risk_refused"]`` were incremented
and then never emitted, which made the gap between ``checked`` and
``approved`` unreadable from the log stream. While the exchange balance is
zero that gap is the whole question: risk.evaluate refuses on bankroll
before it reaches the verdict branch, so the ledger logs an approval and a
rejection identically, and the only durable record of what the Checker
decided is ``edges.checker_verdict`` — which needs the database.

These tests pin the rendering, and one of them pins the control flow that
must NOT change.
"""
from __future__ import annotations

import inspect
import logging

import main


def _fake_stats(**kw):
    from collections import defaultdict
    s = defaultdict(int)
    s.update(kw)
    return s


# ---------------------------------------------------------------------------
# the funnel line
# ---------------------------------------------------------------------------

def test_the_funnel_line_carries_both_previously_hidden_counters():
    src = inspect.getsource(main.run_once)
    fmt = src.split('"Pass funnel: ')[1].split('",')[0]
    assert "checker_rejected=%d" in fmt
    assert "risk_refused=%d" in fmt


def test_the_new_fields_sit_in_pipeline_order():
    """checked -> checker_rejected -> risk_refused -> approved. A funnel read
    out of order invites subtraction between stages that do not adjoin."""
    src = inspect.getsource(main.run_once)
    fmt = src.split('"Pass funnel: ')[1].split('",')[0]
    for earlier, later in (("checked=%d", "checker_rejected=%d"),
                           ("checker_rejected=%d", "risk_refused=%d"),
                           ("risk_refused=%d", "approved=%d")):
        assert fmt.index(earlier) < fmt.index(later), f"{earlier} must precede {later}"


def test_every_pre_existing_field_survives():
    """This is an addition, not a redesign. Anything an existing dashboard or
    grep already keys on must still be there."""
    src = inspect.getsource(main.run_once)
    fmt = src.split('"Pass funnel: ')[1].split('",')[0]
    for field in ("candidates=%d", "quant=%d", "llm_called=%d",
                  "llm_disabled=%d", "proposed=%d", "ladder_deduped=%d",
                  "checked=%d", "approved=%d", "filled=%d"):
        assert field in fmt, f"{field} disappeared from the funnel line"


# ---------------------------------------------------------------------------
# the verdict summary
# ---------------------------------------------------------------------------

def test_a_mixed_pass_splits_approved_from_rejected(caplog):
    with caplog.at_level(logging.INFO, logger="daemon_kalshi.main"):
        main._log_checker_verdicts(
            _fake_stats(checked=10, checker_rejected=7),
            {"approve": [0.9, 0.8, 0.7], "reject": [0.6] * 7},
        )
    msg = caplog.text
    assert "approved=3" in msg
    assert "rejected=7" in msg
    assert "of 10 checked" in msg
    assert "30% approved" in msg


def test_mean_confidence_is_reported_per_disposition(caplog):
    with caplog.at_level(logging.INFO, logger="daemon_kalshi.main"):
        main._log_checker_verdicts(
            _fake_stats(checked=4, checker_rejected=2),
            {"approve": [0.90, 0.80], "reject": [0.50, 0.70]},
        )
    assert "approve=0.85" in caplog.text
    assert "reject=0.60" in caplog.text


def test_a_pass_where_nothing_was_approved_says_zero(caplog):
    """The live case. It must render as an explicit zero, not vanish."""
    with caplog.at_level(logging.INFO, logger="daemon_kalshi.main"):
        main._log_checker_verdicts(
            _fake_stats(checked=18, checker_rejected=18),
            {"approve": [], "reject": [0.7] * 18},
        )
    assert "approved=0" in caplog.text
    assert "0% approved" in caplog.text
    assert "approve=n/a" in caplog.text


def test_absent_confidence_is_not_averaged_as_zero(caplog):
    """No confidences recorded is not a confidence of zero. Averaging an
    absence reports a number nobody produced."""
    with caplog.at_level(logging.INFO, logger="daemon_kalshi.main"):
        main._log_checker_verdicts(
            _fake_stats(checked=2, checker_rejected=2),
            {"approve": [], "reject": []},
        )
    assert "approve=n/a" in caplog.text
    assert "reject=n/a" in caplog.text
    assert "0.00" not in caplog.text


def test_a_pass_that_checked_nothing_emits_no_line(caplog):
    """Zero checked would make the approval-rate divisor zero. Silence is the
    honest output, not 0%."""
    with caplog.at_level(logging.INFO, logger="daemon_kalshi.main"):
        main._log_checker_verdicts(_fake_stats(checked=0), {"approve": [], "reject": []})
    assert "Checker verdicts" not in caplog.text


# ---------------------------------------------------------------------------
# the trap
# ---------------------------------------------------------------------------

def test_a_checker_rejection_still_falls_through_to_risk():
    """THE TRAP IN THIS TASK.

    Seeing `checker_rejected` increment without a `continue` looks like an
    oversight worth tidying. It is not. The fall-through to risk.evaluate is
    what writes edges.checker_verdict, and that column is the only durable
    record of what the Checker decided. A `continue` here would look like
    cleanup and would silently destroy the data.
    """
    src = inspect.getsource(main.run_once)
    block = src.split('stats["checker_rejected"] += 1')[1]
    # Everything up to the risk call — the span a short-circuit would live in.
    head = block.split("risk.evaluate")[0]
    # Strip comments: the code deliberately *mentions* `continue` in a warning
    # comment explaining why there isn't one. Only executable lines count.
    code = "\n".join(line.split("#", 1)[0] for line in head.splitlines())
    assert "continue" not in code, (
        "a `continue` was added after checker_rejected — this destroys "
        "edges.checker_verdict for rejected proposals"
    )


def test_the_counters_are_only_printed_not_recomputed():
    """Out of scope for this change: any alteration to how the counters are
    incremented. They must still be raw reads from `stats`."""
    src = inspect.getsource(main.run_once)
    assert 'stats["checker_rejected"] += 1' in src
    assert 'stats["risk_refused"] += 1' in src
    # exactly one increment site each
    assert src.count('stats["checker_rejected"] += 1') == 1
    assert src.count('stats["risk_refused"] += 1') == 1


# ---------------------------------------------------------------------------
# the per-family breakdown
# ---------------------------------------------------------------------------

def test_families_are_broken_out_worst_first(caplog):
    """Ordering is the point, not decoration.

    An operator reading this line wants the family costing the most Checker
    calls for nothing, and reads left to right. Alphabetical order would put
    that answer in an arbitrary position.
    """
    with caplog.at_level(logging.INFO, logger="daemon_kalshi.main"):
        main._log_checker_verdicts(
            _fake_stats(checked=12, checker_rejected=9),
            {"approve": [0.8] * 3, "reject": [0.8] * 9},
            {"KXBTCD": [2, 1], "KXHIGHNY": [0, 6], "KXWTI": [1, 2]},
        )
    line = [ln for ln in caplog.text.splitlines() if "by family" in ln][0]
    assert line.index("KXHIGHNY") < line.index("KXWTI") < line.index("KXBTCD")


def test_a_family_the_checker_never_approves_is_visible_as_such(caplog):
    with caplog.at_level(logging.INFO, logger="daemon_kalshi.main"):
        main._log_checker_verdicts(
            _fake_stats(checked=6, checker_rejected=6),
            {"approve": [], "reject": [0.8] * 6},
            {"KXHIGHCHI": [0, 6]},
        )
    assert "KXHIGHCHI 0/6" in caplog.text


def test_family_counts_total_to_checked(caplog):
    """The breakdown must account for every verdict.

    Confidence is optional on a verdict and the family is not, so these are
    accumulated at different points. If the family line were fed from the
    confidence bucket instead, a verdict with no confidence would go missing
    here and the shortfall would have no visible explanation.
    """
    families = {"KXBTCD": [2, 1], "KXETHD": [1, 3], "KXWTI": [0, 2]}
    with caplog.at_level(logging.INFO, logger="daemon_kalshi.main"):
        main._log_checker_verdicts(
            _fake_stats(checked=9, checker_rejected=6),
            {"approve": [0.8], "reject": [0.8]},  # deliberately sparse
            families,
        )
    assert sum(a + r for a, r in families.values()) == 9
    line = [ln for ln in caplog.text.splitlines() if "by family" in ln][0]
    for fam, (a, r) in families.items():
        assert f"{fam} {a}/{a + r}" in line


def test_the_family_line_is_omitted_rather_than_printed_empty(caplog):
    with caplog.at_level(logging.INFO, logger="daemon_kalshi.main"):
        main._log_checker_verdicts(
            _fake_stats(checked=3, checker_rejected=3),
            {"approve": [], "reject": [0.8] * 3},
            {},
        )
    assert "by family" not in caplog.text
    assert "approved=0" in caplog.text, "the aggregate line must still appear"


def test_every_verdict_records_a_family_including_ones_without_confidence():
    """Guards the accumulation site, not the rendering.

    The family append must sit outside the `if verdict.confidence is not
    None` branch. Nesting it there would silently undercount, and the
    rendering tests above cannot see that because they are handed a dict.
    """
    src = inspect.getsource(main.run_once)
    block = src.split("verdict_families.setdefault")[0]
    tail = block.rsplit("verdict_confidence[bucket].append", 1)[1]
    # Between the confidence append and the family append there must be a
    # dedent back out of the `if`, not a deeper indent.
    code = [ln for ln in tail.splitlines()[1:]
            if ln.strip() and not ln.strip().startswith("#")]
    assert code, "expected the family append to follow the confidence block"
    assert code[0].startswith("        seen ="), (
        "the family append is indented into the confidence branch — verdicts "
        "without a confidence would not be counted"
    )
