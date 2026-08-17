"""
Weekend data-collection safeguards.

Two defects found during a read-only preflight of the live demo run
(deployment d3a6756b, commit 63e6981):

1. Two Checker parse failures — KXRAINSHARD2-26AUG15-NYC at 00:38:43Z and
   -PHIL at 00:58:17Z — were undiagnosable, because the log clipped the
   payload at 200 chars with `%.200s`. The logged text ended mid-sentence,
   and nothing distinguished "the model stopped there" from "the logger
   stopped there". Those two readings have opposite fixes, so a log that
   conflates them sends the next investigation in a random direction. It
   already sent one.

2. There was no way to summarise a collection run without hand-querying
   SQLite, which for a run whose whole purpose is producing reviewable data
   is the difference between an audit trail and a pile of rows.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from core.validation import (
    MAX_UNPARSEABLE_LOG_CHARS,
    _describe_unparseable,
    validate_checker_output,
    validate_maker_output,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORT = REPO_ROOT / "scripts" / "weekend_export.py"


# --------------------------------------------------------------------------
# P1: the parse-failure log must preserve the evidence
# --------------------------------------------------------------------------

def test_a_short_payload_is_logged_whole_and_marked_complete():
    """The exact production shape, at its real length.

    The two live failures were ~200 chars *as logged*. At this length the log
    must say the payload is complete, so nobody spends another cycle
    wondering whether the model was cut off.
    """
    payload = (
        '{"verdict": "reject", "confidence": 0.8, "reasoning": "The maker\'s '
        'own reasoning undermines their estimate: a forecast from ~2 years '
        'out has essentially zero predictive skill, and NYC August '
        'climatology gives roughly a 30% chance of measurable rain."}'
    )
    described = _describe_unparseable(payload)

    assert "complete" in described
    assert str(len(payload)) in described, "must state the true length"
    assert payload[-30:] in described, "the tail must survive — it was clipped before"


def test_a_clipped_payload_says_the_logger_did_the_clipping():
    """The distinction the old log destroyed."""
    described = _describe_unparseable("x" * (MAX_UNPARSEABLE_LOG_CHARS + 500))

    assert "clipped by the logger, NOT by the model" in described
    assert str(MAX_UNPARSEABLE_LOG_CHARS + 500) in described


def test_the_budget_is_large_enough_to_hold_a_whole_verdict():
    from config import CONFIG

    assert MAX_UNPARSEABLE_LOG_CHARS >= CONFIG.models.checker_max_tokens, (
        "a clip must now mean genuinely anomalous output, not a normal verdict"
    )


def test_the_checker_parse_failure_is_logged_in_full(caplog):
    """Genuinely unrecoverable output: no field was ever finished, so there is
    nothing to salvage and the whole payload has to reach the log.

    The payload deliberately has no complete key/value pair. A response cut
    off *after* one — `{"verdict": "approve", "reasoning": "yyy...` — is
    recoverable and takes the salvage path instead; see
    tests/test_checker_truncation.py.
    """
    unrecoverable = '{"verdict": "appr' + "y" * 800
    with caplog.at_level("WARNING"):
        result = validate_checker_output(unrecoverable, ticker="KXA-1")

    assert result.verdict == "abstain", "unparseable output must never trade"
    assert result.reasoning == "parse_error"
    assert "y" * 800 in caplog.text, "the payload must not be truncated at 200"
    assert "complete" in caplog.text


def test_a_recoverable_approval_still_never_trades(caplog):
    """The two mechanisms meeting: the payload below IS recoverable, so it
    does not take the parse-error path — but a truncated approval is refused
    on its own grounds, and the verdict is the same either way."""
    recoverable = '{"verdict": "approve", "reasoning": "' + "y" * 800
    with caplog.at_level("WARNING"):
        result = validate_checker_output(recoverable, ticker="KXA-1")

    assert result.verdict == "abstain"
    assert result.reasoning == "truncated_approval"


def test_the_maker_parse_failure_is_logged_in_full(caplog):
    payload = "not json " + "z" * 600
    with caplog.at_level("WARNING"):
        assert validate_maker_output(payload, ticker="KXA-1") is None
    assert "z" * 600 in caplog.text


def test_a_non_string_payload_does_not_crash_the_logger():
    """A model client returning None or an object must not break the path
    whose whole job is reporting that something was wrong."""
    assert "None" in _describe_unparseable(None)
    assert _describe_unparseable(12345)


def test_checker_records_stop_reason_on_a_parse_failure(caplog):
    """Separates 'stopped early for another reason' from 'finished, emitted
    non-JSON' — the open question from the two production failures."""
    from workers.checker import Checker
    from tests.test_checker_budget import _Block, _Resp, _RecordingClient, _proposal

    checker = Checker.__new__(Checker)
    checker._client = _RecordingClient(_Resp([_Block("plainly not json")],
                                             stop_reason="end_turn"))
    with caplog.at_level("WARNING"):
        verdict = checker.check(_proposal())

    assert verdict.verdict == "abstain"
    assert "stop_reason=end_turn" in caplog.text
    assert "NOT a token-cap truncation" in caplog.text


def test_a_token_cap_truncation_still_takes_the_earlier_path(caplog):
    """The budget check must keep firing first — this must not regress it."""
    from workers.checker import Checker
    from tests.test_checker_budget import _Block, _Resp, _RecordingClient, _proposal

    checker = Checker.__new__(Checker)
    checker._client = _RecordingClient(_Resp([_Block('{"verdict": "appr')],
                                             stop_reason="max_tokens"))
    with caplog.at_level("ERROR"):
        verdict = checker.check(_proposal())

    assert "truncated" in verdict.reasoning
    assert "CHECKER_MAX_TOKENS" in caplog.text
    assert "NOT a token-cap truncation" not in caplog.text, (
        "the two diagnoses must never both fire — that is the confusion "
        "this whole change exists to remove"
    )


# --------------------------------------------------------------------------
# P2: the export utility
# --------------------------------------------------------------------------

@pytest.fixture
def ledger(tmp_path):
    """A ledger with one dry-run order and reasoning text that must not leak."""
    from memory.edge_store import EdgeRecord, EdgeStore
    from memory.order_store import OrderStore
    from core.order_state import OrderIntent, OrderState

    path = str(tmp_path / "weekend.db")
    edges = EdgeStore(path)
    orders = OrderStore(path)

    edges.record_edge(EdgeRecord(
        ticker="KXRAINSHARD2-26AUG15-NYC", category="Weather",
        maker_probability=0.25, maker_reasoning="SECRET-MAKER-PROSE",
        market_implied_probability=0.50, edge_size=0.23,
        checker_verdict="reject", checker_confidence=0.8,
        checker_reasoning="SECRET-CHECKER-PROSE",
        action_taken="skipped_risk", source="llm",
    ))
    intent = OrderIntent(
        ticker="KXRAINSHARD2-26AUG15-SFO", action="buy", side="no", count=91,
        limit_price_cents=53.0, time_in_force="IOC", source="llm",
        dedupe_bucket=1000,
    )
    record = orders.record_intent(intent)
    record.state = OrderState.DRY_RUN
    record.dry_run = True
    orders.update_order(record)
    return path


def _run_export(db, *extra):
    return subprocess.run(
        [sys.executable, str(EXPORT), "--db", str(db), *extra],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def test_the_export_runs_and_reports_the_run(ledger):
    result = _run_export(ledger)
    assert result.returncode == 0, result.stderr
    assert "DECISIONS" in result.stdout
    assert "ORDERS" in result.stdout
    assert "Weather" in result.stdout


def test_model_prose_never_reaches_the_output(ledger):
    """The property that makes this safe to paste into a review doc."""
    for args in ((), ("--json",)):
        result = _run_export(ledger, *args)
        assert result.returncode == 0
        assert "SECRET-MAKER-PROSE" not in result.stdout
        assert "SECRET-CHECKER-PROSE" not in result.stdout
        assert "SECRET" not in result.stdout
    # ...but their presence is still counted, so nobody assumes the run had
    # no reasoning attached.
    assert "1 maker" in _run_export(ledger).stdout


def test_the_export_cannot_write_to_the_database(ledger):
    """Opened mode=ro: SQLite itself refuses, so a bug here cannot corrupt
    the very data the weekend exists to collect."""
    from scripts.weekend_export import open_readonly

    conn = open_readonly(ledger)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE should_not_exist (x INTEGER)")
    conn.close()


def test_the_dry_run_order_is_reported_as_dry_run(ledger):
    result = _run_export(ledger, "--json")
    import json

    data = json.loads(result.stdout)
    assert data["orders"]["total"] == 1
    assert data["orders"]["dry_run"] == 1
    assert data["orders"]["live"] == 0, (
        "a demo weekend must produce no live orders; this is the line that "
        "would show it if one appeared"
    )
    assert data["orders"]["unresolved_unknown"] == 0


def test_a_live_order_is_called_out(tmp_path):
    """If a live order ever appears in a demo ledger, the summary must say so
    rather than burying it in a state histogram."""
    from memory.order_store import OrderStore
    from core.order_state import OrderIntent

    path = str(tmp_path / "live.db")
    orders = OrderStore(path)
    intent = OrderIntent(ticker="KXA-1", action="buy", side="yes", count=1,
                         limit_price_cents=50.0, time_in_force="IOC",
                         source="llm", dedupe_bucket=1)
    orders.record_intent(intent)          # dry_run defaults to 0 = live

    out = _run_export(path).stdout
    assert "live (non-dry-run) orders exist" in out


def test_a_missing_database_exits_two_rather_than_traceback(tmp_path):
    result = _run_export(tmp_path / "nope.db")
    assert result.returncode == 2
    assert "No such database" in result.stderr


def test_an_empty_ledger_reports_zero_rather_than_nothing(tmp_path):
    """'No data' and 'no trades' must not look identical."""
    from memory.order_store import OrderStore

    path = str(tmp_path / "empty.db")
    OrderStore(path)
    result = _run_export(path)
    assert result.returncode == 0
    assert "total               : 0" in result.stdout


def test_the_export_reads_no_environment_or_credentials():
    """Static guarantee: nothing in this script can pick up a key."""
    source = EXPORT.read_text()
    assert "os.environ" not in source
    assert "getenv" not in source
    assert "CONFIG" not in source, (
        "importing config would pull the whole credentialed environment into "
        "a script whose output is meant to be shareable"
    )
