"""The reporting script is an observer. These tests pin that it stays one.

Its only job is to read the ledger and print what it finds. The properties
that matter are therefore not about the numbers it produces but about what it
is incapable of doing: writing to the database, and stopping the bot.
"""
import hashlib
import re
import sqlite3

import pytest

from memory.edge_store import SCHEMA
from scripts.ledger_answers import build_report, emit, event_of


def _db(tmp_path, rows=()):
    p = tmp_path / "ledger.db"
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    c.executemany(
        """INSERT INTO edges (ticker, category, source, maker_probability,
             counterfactual_direction, counterfactual_price_cents, outcome, pnl,
             settled, checker_verdict, checker_confidence, action_taken, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""", rows)
    c.commit()
    c.close()
    return str(p)


_ROW = ("KXWTI-26AUG3114-T86.99", "Finance", "llm", 0.35,
        "yes", 4.0, "no", -0.05, 1, "reject", 0.82, "skipped_risk")


def test_the_connection_is_read_only(tmp_path):
    """mode=ro, so SQLite refuses the write — not merely 'we never issue one'."""
    path = _db(tmp_path, [_ROW])
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE edges SET pnl = 999")
    conn.close()


def test_building_the_report_does_not_modify_the_database(tmp_path):
    path = _db(tmp_path, [_ROW] * 5)
    before = hashlib.sha256(open(path, "rb").read()).hexdigest()
    build_report(path)
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before


@pytest.mark.parametrize("broken", ["missing", "wrong_schema", "corrupt"])
def test_emit_never_raises(tmp_path, broken, monkeypatch):
    """Every failure mode is logged and swallowed.

    This is the property that lets it sit in the startup path at all: the bot
    must boot and trade whether or not the report can be produced.
    """
    monkeypatch.setattr("scripts.ledger_answers.REPORT_PATH",
                        str(tmp_path / "reports" / "out.txt"))
    if broken == "missing":
        target = str(tmp_path / "absent.db")
    elif broken == "wrong_schema":
        target = str(tmp_path / "other.db")
        c = sqlite3.connect(target)
        c.execute("CREATE TABLE unrelated (a INT)")
        c.commit()
        c.close()
    else:
        target = str(tmp_path / "corrupt.db")
        open(target, "wb").write(b"not a sqlite database")
    emit(target)          # must return, not raise


def test_emit_writes_the_report_when_it_can(tmp_path, monkeypatch):
    out = tmp_path / "reports" / "out.txt"
    monkeypatch.setattr("scripts.ledger_answers.REPORT_PATH", str(out))
    emit(_db(tmp_path, [_ROW] * 3))
    assert "LEDGER ANSWERS" in out.read_text()


def test_an_unwritable_report_path_still_does_not_raise(tmp_path, monkeypatch):
    """The log is the primary channel; the file is a convenience."""
    monkeypatch.setattr("scripts.ledger_answers.REPORT_PATH", "/proc/nope/out.txt")
    emit(_db(tmp_path, [_ROW]))


def test_events_collapse_on_the_last_hyphen():
    """The strike is the last segment; the event is what actually resolves."""
    assert event_of("KXWTI-26AUG3114-T86.99") == "KXWTI-26AUG3114"
    assert event_of("KXHIGHNY-26AUG28-T80") == "KXHIGHNY-26AUG28"
    assert event_of("NOHYPHEN") == "NOHYPHEN"


def test_report_separates_the_two_checker_arms(tmp_path):
    """A confident reject and an under-confident approve are different problems.

    They are indistinguishable in every log line the bot emits, which is the
    whole reason this script exists, so the split is asserted directly.
    """
    rows = [
        ("KXWTI-1-T1", "Finance", "llm", 0.3, "yes", 4.0, "no", -0.05, 1, "approve", 0.55, "skipped_risk"),
        ("KXWTI-1-T2", "Finance", "llm", 0.3, "yes", 4.0, "no", -0.05, 1, "approve", 0.80, "skipped_risk"),
        ("KXWTI-1-T3", "Finance", "llm", 0.3, "yes", 4.0, "no", -0.05, 1, "reject", 0.90, "skipped_risk"),
    ]
    report = build_report(_db(tmp_path, rows))
    assert re.search(r"approve_underconfident\s+1\b", report)
    assert re.search(r"approve_confident\s+1\b", report)
    assert re.search(r"^\s+reject\s+1$", report, re.M)


def test_clustered_and_naive_statistics_are_both_reported(tmp_path):
    """Two events, so a between-cluster standard error is defined.

    Reporting both is the point: the inflation factor between them is the
    finding, and a single number would hide it.
    """
    rows = []
    for ev, pnl in (("KXWTI-A", -0.05), ("KXWTI-B", 0.05)):
        for i in range(10):
            rows.append((f"{ev}-T{i}", "Finance", "llm", 0.3, "yes", 4.0,
                         "no", pnl, 1, "reject", 0.8, "skipped_risk"))
    report = build_report(_db(tmp_path, rows))
    assert "t_naive" in report and "t_clustered" in report
    assert re.search(r"Finance/llm\s+20\s+2\s", report)   # 20 rows, 2 events


def test_the_runtime_image_ships_every_package_main_imports():
    """The Dockerfile copies named directories, not the whole tree.

    A package main.py imports but the image does not copy fails at boot, not
    at build — and under restartPolicyType=ON_FAILURE that is a crash loop,
    which is how a reporting script could take down a trading bot. Adding
    scripts/ was exactly this bug, caught before deploy; this keeps it caught.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile.kalshi").read_text()
    # Parse the DESTINATION, not the source: a package may be copied as a
    # directory ("COPY core/ ./core/") or as named files into one
    # ("COPY scripts/a.py scripts/b.py ./scripts/"), and both ship it.
    copied = set()
    for line in dockerfile.splitlines():
        if not line.startswith("COPY "):
            continue
        dest = line.split()[-1]
        if dest.endswith("/"):
            copied.add(dest.rstrip("/").lstrip("./") or ".")

    tree = ast.parse((root / "main.py").read_text())
    top = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            top.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for a in node.names:
                top.add(a.name.split(".")[0])

    local = {p for p in top if (root / p).is_dir() and (root / p / "__init__.py").exists()}
    missing = local - copied
    assert not missing, f"main.py imports {sorted(missing)}, not copied by Dockerfile.kalshi"
