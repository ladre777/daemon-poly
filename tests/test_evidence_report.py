"""
Tests for the evidence report, readiness assessment and telemetry store.

The property under test throughout is *honesty*, not correctness of
arithmetic. Every one of these guards a way the report could produce a
number that looks like a result and is not:

- a counterfactual leaking into a live total
- a missing value rendering as zero
- an unfilled intent counted as execution
- two different provider failures collapsing into one count
- a readiness verdict better than the evidence supports

Fakes only. Nothing here opens a socket or touches Kalshi.
"""
from __future__ import annotations

import sqlite3

import pytest

from core.reasons import (
    Mode, Readiness, Reason, Stage, STAGE_ORDER, mode_for_action, worst,
)
from memory.telemetry_store import TelemetryStore, _ms
from reporting import evidence as ev
from reporting import readiness as rd
from reporting import render
from scripts.make_evidence_fixture import T0, DAY, build


@pytest.fixture
def fixture_db(tmp_path):
    """A synthetic ledger covering every branch. Never production data."""
    p = str(tmp_path / "fixture.db")
    build(p)
    return p


@pytest.fixture
def conn(fixture_db):
    c = sqlite3.connect(f"file:{fixture_db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture
def empty_conn(tmp_path):
    """A database with the schemas and no rows at all."""
    from memory.edge_store import SCHEMA as ES
    from memory.order_store import SCHEMA as OS
    from memory.telemetry_store import SCHEMA as TS
    p = str(tmp_path / "empty.db")
    w = sqlite3.connect(p)
    for s in (OS, ES, TS):
        w.executescript(s)
    w.commit()
    w.close()
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


# ---------------------------------------------------------------------------
# mode separation
# ---------------------------------------------------------------------------

def test_a_refused_row_is_never_a_live_row():
    assert mode_for_action("skipped_risk") is Mode.REFUSED
    assert mode_for_action("skipped_checker") is Mode.REFUSED
    assert mode_for_action("rejected") is Mode.REFUSED


def test_an_unrecognised_action_is_quarantined_as_refused():
    """The conservative direction. An action the code does not recognise must
    not be allowed to inflate live or paper totals just because it is new."""
    assert mode_for_action("some_future_state") is Mode.REFUSED
    assert mode_for_action(None) is Mode.REFUSED


def test_only_executed_counts_as_live():
    assert mode_for_action("executed") is Mode.LIVE
    for other in ("dry_run", "no_fill"):
        assert mode_for_action(other) is Mode.PAPER


def test_refused_rows_do_not_reach_the_paper_total(conn):
    paper = ev.counterfactual_evidence(conn, Mode.PAPER)
    refused = ev.counterfactual_evidence(conn, Mode.REFUSED)
    # The fixture has 6 paper rows and 12 refused. If the filter leaked, the
    # paper count would pick up refused rows.
    assert paper.n == 6
    assert refused.n == 12
    assert {c["category"] for c in paper.by_category} == {"Crypto"}
    assert {c["category"] for c in refused.by_category} == {"Weather", "Finance"}


def test_dry_run_orders_never_enter_live_totals(conn):
    """The fixture contains a 50-contract dry-run order with 99c of fees.
    If dry_run filtering were dropped, contracts_filled would jump by 50."""
    live = ev.live_evidence(conn)
    assert live.orders_submitted == 4          # 5 orders, one is dry_run
    assert live.contracts_filled == 18         # 10 + 8, not 68
    assert live.contracts_requested == 50      # 10+20+15+5, not 100


def test_live_totals_exclude_unfilled_intent(conn):
    """A cancelled and a rejected order contribute requested size but no
    fills. Counting intent as execution is the classic way a fill rate gets
    reported as 100%."""
    live = ev.live_evidence(conn)
    assert live.orders_cancelled == 1
    assert live.orders_rejected == 1
    assert live.fill_rate == pytest.approx(18 / 50)
    assert live.fill_rate < 1.0


# ---------------------------------------------------------------------------
# accounting honesty
# ---------------------------------------------------------------------------

def test_realized_pnl_comes_from_settlements(conn):
    live = ev.live_evidence(conn)
    assert live.gross_pnl == pytest.approx(570.0 - 444.0)
    assert live.fees == pytest.approx(54.0)
    assert live.net_realized_pnl == pytest.approx(126.0 - 54.0)


def test_missing_fees_block_the_net_rather_than_defaulting_to_zero(tmp_path):
    """The single most attractive lie a trading report can tell is a zero
    fee. A NULL must produce N/A, never a net computed as if fees were 0."""
    p = str(tmp_path / "nofee.db")
    build(p)
    w = sqlite3.connect(p)
    w.execute("UPDATE settlements SET fees_cents = NULL WHERE settlement_key='s-1'")
    w.commit()
    w.close()
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    live = ev.live_evidence(c)
    c.close()

    assert isinstance(live.fees, ev.Unavailable)
    assert isinstance(live.net_realized_pnl, ev.Unavailable)
    assert "fees" in live.net_realized_pnl.reason.lower()
    # And gross survives, because gross does not depend on fees.
    assert live.gross_pnl == pytest.approx(126.0)


def test_missing_realized_pnl_blocks_the_gross_too(tmp_path):
    p = str(tmp_path / "nopnl.db")
    build(p)
    w = sqlite3.connect(p)
    w.execute("UPDATE settlements SET realized_pnl = NULL WHERE settlement_key='s-2'")
    w.commit()
    w.close()
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    live = ev.live_evidence(c)
    c.close()
    assert isinstance(live.gross_pnl, ev.Unavailable)
    assert isinstance(live.net_realized_pnl, ev.Unavailable)


def test_a_bot_that_never_traded_reports_na_not_zero(empty_conn):
    live = ev.live_evidence(empty_conn)
    assert live.orders_submitted == 0
    for f in ("fill_rate", "gross_pnl", "fees", "net_realized_pnl",
              "return_on_deployed_capital", "max_drawdown"):
        v = getattr(live, f)
        assert isinstance(v, ev.Unavailable), f"{f} should be N/A, got {v!r}"
    assert any("nothing was executed" in n for n in live.notes)


def test_a_category_with_nothing_settled_has_no_pnl(conn):
    """Finance has 4 refused rows and none settled. A 0.0 there would read
    as 'broke even' rather than 'has not resolved'."""
    refused = ev.counterfactual_evidence(conn, Mode.REFUSED)
    finance = next(c for c in refused.by_category if c["category"] == "Finance")
    assert finance["settled"] == 0
    assert isinstance(finance["pnl"], ev.Unavailable)


def test_return_on_capital_needs_a_real_denominator(empty_conn):
    live = ev.live_evidence(empty_conn)
    assert isinstance(live.return_on_deployed_capital, ev.Unavailable)


def test_unavailable_refuses_to_be_arithmetic():
    """The sentinel must not silently participate in a sum. If it did, a
    missing value would become 0 at the first ``+`` and the whole guarantee
    would be gone."""
    u = ev.Unavailable("nope")
    with pytest.raises(TypeError):
        _ = u + 1          # type: ignore[operator]
    with pytest.raises(TypeError):
        _ = float(u)       # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# read-only
# ---------------------------------------------------------------------------

def test_the_report_cannot_write_to_the_database(fixture_db):
    """SQLite itself refuses, so this is enforced below the application."""
    c = sqlite3.connect(f"file:{fixture_db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    with pytest.raises(sqlite3.OperationalError):
        c.execute("INSERT INTO edges (ticker, created_at) VALUES ('X', 1)")
    c.close()


def test_generating_a_report_leaves_the_database_byte_identical(fixture_db):
    import hashlib
    from scripts.production_readiness_report import build_payload, open_readonly

    def digest():
        with open(fixture_db, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    before = digest()
    c = open_readonly(fixture_db)
    payload = build_payload(c, as_of=T0 + 3 * DAY + 300, include_refused=True)
    c.close()
    assert payload["live"]["orders_submitted"] == 4
    assert digest() == before


def test_the_report_never_constructs_a_network_client(monkeypatch, fixture_db):
    """A report that quietly reaches the exchange would both be slow and,
    far worse, be capable of acting. Poison the socket layer and generate a
    full report over it."""
    import socket
    from scripts.production_readiness_report import build_payload, open_readonly

    def explode(*a, **k):
        raise AssertionError("the report attempted a network connection")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)

    c = open_readonly(fixture_db)
    payload = build_payload(c, as_of=T0 + 3 * DAY + 300, include_refused=True)
    c.close()
    assert payload["meta"]["contacts_network"] is False


def test_refused_section_is_opt_in(fixture_db):
    from scripts.production_readiness_report import build_payload, open_readonly
    c = open_readonly(fixture_db)
    without = build_payload(c, as_of=T0 + 3 * DAY, include_refused=False)
    with_it = build_payload(c, as_of=T0 + 3 * DAY, include_refused=True)
    c.close()
    assert "refused" not in without
    assert "refused_omitted" in without
    assert with_it["refused"]["n"] == 12


def test_cli_exits_two_on_a_missing_database(tmp_path, capsys):
    from scripts.production_readiness_report import main
    rc = main(["--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "no such database" in capsys.readouterr().err


def test_cli_renders_markdown_and_json(fixture_db, capsys):
    from scripts.production_readiness_report import main
    assert main(["--db", fixture_db]) == 0
    assert "# DÆMON-KALSHI" in capsys.readouterr().out
    assert main(["--db", fixture_db, "--json"]) == 0
    import json
    json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def test_unavailable_renders_with_its_reason_never_as_zero():
    u = ev.Unavailable("settlements table absent")
    assert render.usd(u) == "N/A (settlements table absent)"
    assert render.pct(u) == "N/A (settlements table absent)"
    assert render.fmt(u) == "N/A (settlements table absent)"
    for r in (render.usd(u), render.pct(u), render.fmt(u)):
        assert "0" not in r.replace("N/A (", "").replace(")", "") or "absent" in r


def test_none_renders_as_not_recorded_not_zero():
    assert render.usd(None) == "N/A (not recorded)"
    assert render.pct(None) == "N/A (not recorded)"


def test_markdown_labels_every_counterfactual_block(fixture_db):
    from scripts.production_readiness_report import build_payload, open_readonly
    c = open_readonly(fixture_db)
    md = render.to_markdown(build_payload(c, as_of=T0 + 3 * DAY + 300,
                                          include_refused=True))
    c.close()
    assert md.count("**These are not results.**") == 2      # paper and refused
    assert "never summed" in md
    assert "no `LIVE_READY` state" in md


def test_markdown_never_emits_model_prose(fixture_db):
    """Reasoning columns exist in the ledger and must not reach the report."""
    from scripts.production_readiness_report import build_payload, open_readonly
    c = open_readonly(fixture_db)
    payload = build_payload(c, as_of=T0 + 3 * DAY, include_refused=True)
    c.close()
    blob = render.to_markdown(payload) + render.to_json(payload)
    for col in render.REDACTED_COLUMNS:
        assert col not in blob


# ---------------------------------------------------------------------------
# provider classification
# ---------------------------------------------------------------------------

def test_billing_throttling_and_faults_are_three_different_things():
    """The 2026-08-27 incident in one assertion: these must never pool."""
    assert Reason.PROVIDER_BILLING not in rd.THROTTLE_REASONS
    assert Reason.PROVIDER_RATE_LIMITED not in rd.BILLING_REASONS
    assert Reason.PROVIDER_TIMEOUT not in rd.BILLING_REASONS
    assert Reason.PROVIDER_TIMEOUT not in rd.THROTTLE_REASONS


def test_the_two_truncation_dispositions_are_counted_apart(conn):
    """A recovered reject refused a trade the model wanted to refuse. An
    abstained approval refused one the model wanted to take. Pooling them
    hides the second, which is the one that costs opportunities."""
    live = ev.live_evidence(conn)
    report = rd.assess(conn, live_ev=live, calibration_cells=[],
                       now=T0 + 3 * DAY + 300)
    op = next(r for r in report.requirements
              if r.key == "operational_reliability")
    assert op.evidence["truncated_reject_recovered"] == 2
    assert op.evidence["truncated_approval_abstained"] == 1
    assert op.evidence["truncations_total"] == 3


def test_a_billing_failure_blocks_operational_readiness(conn):
    """An abstention caused by an unpaid vendor is not a judgement, and a
    report that treats it as conservative behaviour is misleading."""
    live = ev.live_evidence(conn)
    report = rd.assess(conn, live_ev=live, calibration_cells=[],
                       now=T0 + 3 * DAY + 300)
    op = next(r for r in report.requirements
              if r.key == "operational_reliability")
    assert op.state is Readiness.BLOCKED
    assert "non-functional" in op.detail
    assert report.overall is Readiness.BLOCKED


def test_no_telemetry_is_reported_as_unmeasured_not_as_zero(empty_conn):
    live = ev.live_evidence(empty_conn)
    report = rd.assess(empty_conn, live_ev=live, calibration_cells=[])
    op = next(r for r in report.requirements
              if r.key == "operational_reliability")
    assert op.state is Readiness.INSUFFICIENT_EVIDENCE
    assert "not the same as being zero" in op.detail


# ---------------------------------------------------------------------------
# coverage reasons
# ---------------------------------------------------------------------------

def test_scan_cap_and_no_candidates_are_distinct_reasons():
    assert Reason.SCAN_CAP_REACHED != Reason.NO_CANDIDATES


def test_missing_spec_and_market_quality_are_distinct_reasons():
    assert Reason.NO_CONTRACT_SPEC != Reason.MARKET_QUALITY


def test_blockers_separate_each_pair_of_lookalikes(conn):
    live = ev.live_evidence(conn)
    report = rd.assess(conn, live_ev=live, calibration_cells=[],
                       now=T0 + 3 * DAY + 300)
    keys = {b["blocker"] for b in report.blockers}
    assert "zero_exchange_balance" in keys
    assert "coverage_limit" in keys
    assert "pricing_coverage" in keys
    assert "provider_degradation" in keys
    # Every blocker must name what it is not.
    for b in report.blockers:
        assert b["not_to_be_confused_with"].strip()


def test_zero_balance_is_not_reported_as_a_risk_judgement(conn):
    live = ev.live_evidence(conn)
    report = rd.assess(conn, live_ev=live, calibration_cells=[],
                       now=T0 + 3 * DAY + 300)
    zb = next(b for b in report.blockers if b["blocker"] == "zero_exchange_balance")
    assert "upstream of every gate" in zb["detail"]
    assert "risk-gate rejection" in zb["not_to_be_confused_with"]


def test_dedup_inactivity_is_distinguished_from_dedup_absence(conn):
    live = ev.live_evidence(conn)
    report = rd.assess(conn, live_ev=live, calibration_cells=[],
                       now=T0 + 3 * DAY + 300)
    d = next(b for b in report.blockers if b["blocker"] == "ladder_dedup_activity")
    assert "never bound" in d["detail"]
    assert "disabled or absent" in d["not_to_be_confused_with"]


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------

def test_dry_run_forbids_any_live_performance_claim(conn):
    live = ev.live_evidence(conn)
    report = rd.assess(conn, live_ev=live, calibration_cells=[], dry_run=True,
                       now=T0 + 3 * DAY + 300)
    r = next(x for x in report.requirements if x.key == "dry_run")
    assert r.state is Readiness.NOT_APPLICABLE
    assert "No live P&L claim is possible" in r.detail


def test_an_empty_database_is_insufficient_never_ready(empty_conn):
    live = ev.live_evidence(empty_conn)
    report = rd.assess(empty_conn, live_ev=live, calibration_cells=[])
    assert report.overall is Readiness.INSUFFICIENT_EVIDENCE


def test_there_is_no_live_ready_state():
    """Guards the naming decision itself. A future edit that adds one should
    fail here and have to argue for it."""
    assert not hasattr(Readiness, "LIVE_READY")
    assert "LIVE_READY" not in {s.value for s in Readiness}


def test_a_thin_fill_sample_cannot_reach_measurement_ready(conn):
    live = ev.live_evidence(conn)
    report = rd.assess(conn, live_ev=live, calibration_cells=[],
                       min_fill_sample=1000, now=T0 + 3 * DAY + 300)
    r = next(x for x in report.requirements if x.key == "execution_viability")
    assert r.state is Readiness.INSUFFICIENT_EVIDENCE
    assert r.evidence["minimum"] == 1000


def test_the_minimum_sample_is_stated_not_hidden(conn):
    live = ev.live_evidence(conn)
    report = rd.assess(conn, live_ev=live, calibration_cells=[],
                       now=T0 + 3 * DAY + 300)
    r = next(x for x in report.requirements if x.key == "execution_viability")
    assert str(r.evidence["minimum"]) in r.detail


def test_unknown_order_states_block(tmp_path):
    p = str(tmp_path / "unknown.db")
    build(p)
    w = sqlite3.connect(p)
    w.execute("UPDATE orders SET state='unknown' WHERE client_order_id='coid-3'")
    w.commit()
    w.close()
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    live = ev.live_evidence(c)
    report = rd.assess(c, live_ev=live, calibration_cells=[],
                       now=T0 + 3 * DAY + 300)
    c.close()
    assert next(r for r in report.requirements
                if r.key == "risk_safety").state is Readiness.BLOCKED
    assert report.overall is Readiness.BLOCKED


def test_a_tripped_kill_switch_blocks_and_is_not_reset(tmp_path):
    p = str(tmp_path / "kill.db")
    build(p)
    w = sqlite3.connect(p)
    w.execute("UPDATE bot_state SET kill_switch_tripped=1 WHERE id=1")
    w.commit()
    w.close()
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    report = rd.assess(c, live_ev=ev.live_evidence(c), calibration_cells=[],
                       now=T0 + 3 * DAY + 300)
    c.close()
    assert report.overall is Readiness.BLOCKED
    # Still tripped afterwards — the report observes, it does not clear.
    v = sqlite3.connect(p)
    assert v.execute(
        "SELECT kill_switch_tripped FROM bot_state WHERE id=1").fetchone()[0] == 1
    v.close()


def test_a_future_dated_snapshot_is_flagged_not_reported_as_fresh(conn):
    """A negative age would render as a very small number and read as
    'reconciled moments ago', which is the opposite of the truth."""
    live = ev.live_evidence(conn)
    report = rd.assess(conn, live_ev=live, calibration_cells=[],
                       now=T0)  # before the fixture's snapshot
    r = next(x for x in report.requirements if x.key == "reconciliation")
    assert r.state is Readiness.BLOCKED
    assert r.evidence["snapshot_age_seconds"] is None
    assert "AFTER" in r.detail


def test_stale_reconciliation_blocks(conn):
    live = ev.live_evidence(conn)
    report = rd.assess(conn, live_ev=live, calibration_cells=[],
                       stale_after=1, now=T0 + 4 * DAY)
    assert next(r for r in report.requirements
                if r.key == "reconciliation").state is Readiness.BLOCKED


def test_worst_is_conservative():
    assert worst([]) is Readiness.INSUFFICIENT_EVIDENCE
    assert worst([Readiness.NOT_APPLICABLE]) is Readiness.INSUFFICIENT_EVIDENCE
    assert worst([Readiness.MEASUREMENT_READY,
                  Readiness.BLOCKED]) is Readiness.BLOCKED
    assert worst([Readiness.MEASUREMENT_READY,
                  Readiness.INSUFFICIENT_EVIDENCE]) is Readiness.INSUFFICIENT_EVIDENCE
    assert worst([Readiness.MEASUREMENT_READY,
                  Readiness.NOT_APPLICABLE]) is Readiness.MEASUREMENT_READY


def test_maker_prerequisites_are_listed_and_nothing_is_enabled(conn):
    report = rd.assess(conn, live_ev=ev.live_evidence(conn),
                       calibration_cells=[], now=T0 + 3 * DAY + 300)
    joined = " ".join(report.maker_prerequisites).lower()
    for must in ("quote generator", "gtc", "inventory", "adverse-selection",
                 "cancel/replace", "fee schedule"):
        assert must in joined


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------

def test_calibration_is_split_by_mode(conn):
    cells = ev.calibration(conn)
    modes = {c.mode for c in cells}
    assert Mode.LIVE in modes and Mode.PAPER in modes and Mode.REFUSED in modes
    # and no cell mixes two modes
    for c in cells:
        assert isinstance(c.mode, Mode)


def test_every_non_live_cell_is_labelled_counterfactual(conn):
    for c in ev.calibration(conn):
        if c.mode is not Mode.LIVE:
            assert any("counterfactual" in w for w in c.warnings)


def test_small_cells_carry_a_minimum_sample_warning(conn):
    for c in ev.calibration(conn, min_sample=1000):
        assert any("below the 1000-row minimum" in w for w in c.warnings)


def test_reliability_buckets_sum_to_the_cell(conn):
    for c in ev.calibration(conn):
        assert sum(b["n"] for b in c.reliability) == c.n


def test_brier_is_never_used_as_an_approval_criterion(conn):
    """Calibration must not feed the readiness verdict. Rerun the assessment
    with the calibration cells replaced by perfect ones and confirm nothing
    about the verdict improves."""
    live = ev.live_evidence(conn)
    real = rd.assess(conn, live_ev=live, calibration_cells=ev.calibration(conn),
                     now=T0 + 3 * DAY + 300)
    none = rd.assess(conn, live_ev=live, calibration_cells=[],
                     now=T0 + 3 * DAY + 300)
    assert real.overall is none.overall


# ---------------------------------------------------------------------------
# integrity checks
# ---------------------------------------------------------------------------

def test_a_live_row_carrying_counterfactual_pricing_is_contamination(tmp_path):
    p = str(tmp_path / "contam.db")
    build(p)
    w = sqlite3.connect(p)
    w.execute("UPDATE edges SET counterfactual_price_cents = 50.0 "
              "WHERE action_taken='executed'")
    w.commit()
    w.close()
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    findings = ev.mode_contamination(c)
    report = rd.assess(c, live_ev=ev.live_evidence(c), calibration_cells=[],
                       now=T0 + 3 * DAY + 300)
    c.close()
    assert any(f["issue"] == "executed_row_has_counterfactual_price"
               for f in findings)
    assert next(r for r in report.requirements
                if r.key == "data_integrity").state is Readiness.BLOCKED


def test_a_live_row_with_no_order_id_cannot_verify_its_own_claim(tmp_path):
    p = str(tmp_path / "orphan.db")
    build(p)
    w = sqlite3.connect(p)
    w.execute("UPDATE edges SET client_order_id = NULL WHERE action_taken='executed'")
    w.commit()
    w.close()
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    findings = ev.mode_contamination(c)
    c.close()
    assert any(f["issue"] == "executed_row_not_joinable_to_fill" for f in findings)


def test_a_clean_fixture_has_no_contamination_or_duplicates(conn):
    assert ev.mode_contamination(conn) == []
    assert ev.duplicate_accounting(conn) == []


# ---------------------------------------------------------------------------
# telemetry store
# ---------------------------------------------------------------------------

def test_durations_are_computed_from_known_timestamps():
    assert _ms(10.5, 10.0) == pytest.approx(500.0)
    assert _ms(10.0, 10.0) == 0.0


def test_a_missing_timestamp_yields_none_not_zero():
    assert _ms(None, 1.0) is None
    assert _ms(1.0, None) is None
    assert _ms(None, None) is None


def test_a_backwards_clock_is_flagged_not_reported_as_a_duration():
    """A negative duration is a broken measurement, not a fast one. Returning
    it would quietly drag every average down."""
    assert _ms(1.0, 5.0) is None


def test_telemetry_records_a_pass_and_its_stages(tmp_path):
    t = TelemetryStore(str(tmp_path / "t.db"))
    pid = t.begin_pass(dry_run=True, kalshi_env="prod", order_strategy="taker",
                       balance_cents=0.0)
    assert pid is not None
    t.record_stages(pid, {
        (Stage.SCOUTED.value, None): 297,
        (Stage.PRICED.value, Reason.NO_CONTRACT_SPEC.value): 4,
        (Stage.PROPOSED.value, Reason.LADDER_DEDUPED.value): 0,   # skipped
    })
    t.finish_pass(pid, candidates=297, scan_cap_reached=True, scout_ms=118000)

    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT stage, reason, count FROM stage_events").fetchall()
    p = c.execute("SELECT * FROM pass_telemetry WHERE pass_id=?", (pid,)).fetchone()
    c.close()
    assert len(rows) == 2                    # the zero was not written
    assert p["candidates"] == 297
    assert p["scan_cap_reached"] == 1
    assert p["finished_at"] is not None


def test_quote_ages_are_derived_at_each_stage(tmp_path):
    t = TelemetryStore(str(tmp_path / "t.db"))
    pid = t.begin_pass()
    did = t.record_decision(pid, ticker="KXBTCD-A", quote_captured_at=100.0,
                            proposed_at=100.25, decision_price_cents=42.0,
                            requested_count=10)
    t.mark_quote_age(did, "age_at_checker_ms", 101.0, 100.0)
    t.mark_quote_age(did, "age_at_risk_ms", 101.5, 100.0)
    t.mark_quote_age(did, "age_at_submit_ms", 102.0, 100.0)

    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM decision_telemetry WHERE id=?", (did,)).fetchone()
    c.close()
    assert r["age_at_proposal_ms"] == pytest.approx(250.0)
    assert r["age_at_checker_ms"] == pytest.approx(1000.0)
    assert r["age_at_risk_ms"] == pytest.approx(1500.0)
    assert r["age_at_submit_ms"] == pytest.approx(2000.0)


def test_telemetry_never_raises_into_the_trading_path(tmp_path):
    """Every method must tolerate a broken store. An instrument that can
    abort the thing it measures is a liability."""
    t = TelemetryStore(str(tmp_path / "t.db"))
    t._enabled = False
    assert t.begin_pass() is None
    assert t.record_decision(None, ticker="X") is None
    # None ids and unknown fields are absorbed, not raised.
    t.record_stage(None, "scouted")
    t.finish_pass(None, candidates=1)
    t.update_decision(None, filled_count=1)
    t.mark_quote_age(None, "age_at_checker_ms", 1.0, 0.0)


def test_an_unopenable_database_disables_telemetry_instead_of_crashing(tmp_path):
    """A missing parent directory is NOT this case — memory.db.connect creates
    parents deliberately. Point at a directory instead, which SQLite cannot
    open as a database however hard it tries."""
    d = tmp_path / "iam_a_directory"
    d.mkdir()
    t = TelemetryStore(str(d))
    assert t._enabled is False
    assert t.begin_pass() is None


def test_unknown_update_fields_are_refused_not_written(tmp_path):
    t = TelemetryStore(str(tmp_path / "t.db"))
    pid = t.begin_pass()
    did = t.record_decision(pid, ticker="KXBTCD-A")
    t.update_decision(did, definitely_not_a_column="x", filled_count=7)
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT filled_count FROM decision_telemetry WHERE id=?",
                  (did,)).fetchone()
    c.close()
    assert r["filled_count"] == 7


def test_provider_calls_record_outcome_classes_separately(tmp_path):
    t = TelemetryStore(str(tmp_path / "t.db"))
    pid = t.begin_pass()
    t.record_provider_call(pid, role="maker", provider="moonshot",
                           model="kimi-k2.6", started_at=1.0, finished_at=1.5,
                           ok=True)
    t.record_provider_call(pid, role="maker", provider="moonshot",
                           model="kimi-k2.6", ok=False,
                           outcome=Reason.PROVIDER_BILLING.value, http_status=429)
    t.record_provider_call(pid, role="checker", provider="gemini",
                           model="g", ok=False,
                           outcome=Reason.PROVIDER_RATE_LIMITED.value,
                           http_status=429)
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT outcome, COUNT(*) n FROM provider_calls GROUP BY outcome").fetchall()
    got = {r["outcome"]: r["n"] for r in rows}
    ok = c.execute("SELECT elapsed_ms FROM provider_calls WHERE ok=1").fetchone()
    c.close()
    # Both are HTTP 429 and they must still be two different rows.
    assert got[Reason.PROVIDER_BILLING.value] == 1
    assert got[Reason.PROVIDER_RATE_LIMITED.value] == 1
    assert ok["elapsed_ms"] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# taxonomy
# ---------------------------------------------------------------------------

def test_stage_order_covers_every_stage():
    assert set(STAGE_ORDER) == set(Stage)
    assert len(STAGE_ORDER) == len(Stage)


def test_reason_values_are_unique():
    values = [r.value for r in Reason]
    assert len(values) == len(set(values))
