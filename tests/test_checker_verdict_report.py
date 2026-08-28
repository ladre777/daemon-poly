"""
Tests for the Checker verdict report.

The properties under test are the same ones the rest of this codebase
insists on: the report cannot alter what it reports, it cannot reach the
network, and it never renders an absence as a number.

The last one carries the most weight here. "The Checker approved nothing"
and "we could not look" are opposite findings, and a report that prints them
identically is worse than no report.

Fakes only. Nothing here opens a socket or touches Kalshi.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from scripts.checker_verdict_report import (
    collect, family_of, main, open_readonly, parse_iso, render,
)

#: 2026-08-26T00:00:00Z and 2026-08-27T00:00:00Z
DAY26 = parse_iso("2026-08-26T00:00:00Z")
DAY27 = parse_iso("2026-08-27T00:00:00Z")


def _build(path, rows):
    """rows: (ticker, category, source, created_at, verdict, conf, action)"""
    from memory.edge_store import SCHEMA
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for tk, cat, src, ts, v, conf, act in rows:
        c.execute(
            """INSERT INTO edges (ticker, category, source, created_at,
               maker_probability, checker_verdict, checker_confidence,
               action_taken)
               VALUES (?,?,?,?,0.5,?,?,?)""",
            (tk, cat, src, ts, v, conf, act))
    c.commit()
    c.close()


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "ledger.db")
    _build(p, [
        # KXBTCD — mixed, on both days
        ("KXBTCD-26AUG26-T80", "Crypto", "quant", DAY26 + 100, "approve", 0.80, "skipped_risk"),
        ("KXBTCD-26AUG26-T81", "Crypto", "quant", DAY26 + 200, "reject", 0.70, "skipped_risk"),
        ("KXBTCD-26AUG27-T82", "Crypto", "quant", DAY27 + 100, "reject", 0.90, "skipped_risk"),
        # KXHIGHNY — never approved, must show as 0 not vanish
        ("KXHIGHNY-26AUG26-T80", "Weather", "llm", DAY26 + 300, "reject", 0.60, "skipped_risk"),
        ("KXHIGHNY-26AUG27-T80", "Weather", "llm", DAY27 + 300, "reject", 0.65, "skipped_risk"),
        # KXWTI — one abstention with no confidence recorded
        ("KXWTI-26AUG27-T86", "Finance", "llm", DAY27 + 400, "abstain", None, "skipped_checker"),
        # a row with no verdict at all — stopped before the Checker
        ("KXETHD-26AUG27-T99", "Crypto", "quant", DAY27 + 500, None, None, "skipped_incoherent"),
    ])
    return p


@pytest.fixture
def conn(db):
    c = open_readonly(db)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# read-only posture
# ---------------------------------------------------------------------------

def test_the_report_cannot_write_to_the_database(db):
    c = open_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("INSERT INTO edges (ticker, created_at) VALUES ('X', 1)")
    c.close()


def test_generating_a_report_leaves_the_database_byte_identical(db):
    def digest():
        with open(db, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    before = digest()
    c = open_readonly(db)
    data = collect(c)
    c.close()
    render(data)
    assert data["total_checked"] == 6
    assert digest() == before


def test_the_report_never_constructs_a_network_client(monkeypatch, db):
    """Poison the socket layer and generate a full report over the top."""
    import socket

    def explode(*a, **k):
        raise AssertionError("the report attempted a network connection")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    c = open_readonly(db)
    render(collect(c))
    c.close()


def test_a_missing_database_exits_non_zero_not_empty(tmp_path, capsys):
    """An empty report reads like a finding. A missing file must not
    produce one."""
    rc = main(["--db", str(tmp_path / "nope.db")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no such database" in err
    assert "will not guess" in err


# ---------------------------------------------------------------------------
# the question it exists to answer
# ---------------------------------------------------------------------------

def test_it_reports_the_verdict_distribution(conn):
    d = collect(conn)
    assert d["overall"] == {"approve": 1, "reject": 4, "abstain": 1}
    assert d["total_checked"] == 6


def test_rows_that_never_reached_the_checker_are_excluded(conn):
    """The KXETHD row was stopped by the coherence gate and has no verdict.
    Counting it would understate the approval rate against a denominator
    that never saw the Checker."""
    d = collect(conn)
    assert "KXETHD" not in d["by_family"]
    assert d["total_checked"] == 6          # not 7


def test_confidence_is_split_by_verdict(conn):
    d = collect(conn)
    assert d["confidence"]["approve"]["mean"] == pytest.approx(0.80)
    assert d["confidence"]["reject"]["mean"] == pytest.approx(0.7125)
    assert d["confidence"]["reject"]["median"] == pytest.approx(0.675)


def test_a_missing_confidence_is_absent_not_zero(conn):
    """The abstain row has NULL confidence. Averaging it as 0.0 would drag
    the mean toward a number nobody recorded."""
    c = collect(conn)["confidence"]["abstain"]
    assert c["n_with_confidence"] == 0
    assert c["mean"] is None
    assert c["median"] is None


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------

def test_a_family_with_zero_approvals_renders_as_zero_not_absent(conn):
    """The requirement that motivated the by-family split: a family that is
    never approved must be visible as such, not averaged away."""
    d = collect(conn)
    assert "KXHIGHNY" in d["by_family"]
    assert d["by_family"]["KXHIGHNY"].get("approve", 0) == 0
    assert d["by_family"]["KXHIGHNY"]["reject"] == 2
    out = render(d)
    line = next(row for row in out.splitlines() if row.startswith("KXHIGHNY"))
    assert "0.0%" in line          # approval share printed, not omitted


def test_days_are_separable(conn):
    d = collect(conn)
    assert set(d["by_day"]) == {"2026-08-26", "2026-08-27"}
    assert d["by_day"]["2026-08-26"]["approve"] == 1
    assert d["by_day"]["2026-08-27"].get("approve", 0) == 0


def test_families_are_parsed_from_the_ticker(conn):
    assert family_of("KXBTCD-26AUG2817-T80499.99") == "KXBTCD"
    assert family_of("KXPGATOUR-TOC26-XSCH") == "KXPGATOUR"
    assert family_of("KXBTCD") == "KXBTCD"
    assert family_of("") == "(no ticker)"


def test_the_window_bounds_are_honoured(conn):
    d = collect(conn, since=DAY27)
    assert set(d["by_day"]) == {"2026-08-27"}
    assert d["total_checked"] == 3
    assert d["overall"].get("approve", 0) == 0


# ---------------------------------------------------------------------------
# rendering and honesty
# ---------------------------------------------------------------------------

def test_an_empty_window_says_so_rather_than_implying_zero_approvals(conn):
    d = collect(conn, since=parse_iso("2030-01-01T00:00:00Z"))
    out = render(d)
    assert d["total_checked"] == 0
    assert "NO ROWS WITH A CHECKER VERDICT" in out
    assert "not the same as" in out
    assert "APPROVAL RATE" not in out


def test_every_report_carries_the_interpretation_warning(conn):
    out = render(collect(conn))
    assert "do NOT present approval rate and counterfactual P&L" in out
    assert "never became a trade" in out


def test_the_report_never_emits_model_prose(conn):
    from scripts.checker_verdict_report import REDACTED_COLUMNS
    d = collect(conn)
    blob = render(d) + json.dumps(d)
    for col in REDACTED_COLUMNS:
        assert col not in blob


def test_cli_renders_text_and_json(db, capsys):
    assert main(["--db", db]) == 0
    assert "CHECKER VERDICT REPORT" in capsys.readouterr().out
    assert main(["--db", db, "--json"]) == 0
    json.loads(capsys.readouterr().out)


def test_a_database_without_an_edges_table_is_an_error_not_a_zero(tmp_path):
    p = str(tmp_path / "wrong.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE something_else (x INTEGER)")
    c.commit()
    c.close()
    ro = open_readonly(p)
    with pytest.raises(sqlite3.DatabaseError, match="edges"):
        collect(ro)
    ro.close()
