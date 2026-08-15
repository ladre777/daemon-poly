"""Shared fixtures. Every test gets its own SQLite file and a CONFIG snapshot
that is restored afterwards, so config mutation in one test cannot leak into
another and quietly change what a safety assertion means."""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CONFIG  # noqa: E402
from core.account_state import AccountState  # noqa: E402
from memory.edge_store import EdgeStore  # noqa: E402
from memory.order_store import OrderStore  # noqa: E402
from workers.execution import Execution  # noqa: E402
from workers.ledger import Ledger  # noqa: E402
from workers.maker import Proposal  # noqa: E402
from workers.checker import Verdict  # noqa: E402
from workers.risk_guardrail import RiskGuardrail  # noqa: E402
from workers.scout import Candidate  # noqa: E402

from tests.fakes import FakeKalshiClient  # noqa: E402


#: App-level CONFIG attributes tests mutate. Restored alongside CONFIG.risk.
_APP_FIELDS = (
    "telegram",
    "scout_categories",
    "llm_reasoning_categories",
    "priority_keywords",
    "max_llm_calls_per_pass",
    "scout_poll_seconds",
    "scout_max_pages",
)


@pytest.fixture(autouse=True)
def restore_config():
    """Snapshot and restore mutable CONFIG state around every test.

    DRY_RUN in particular is global; a test that flips it and forgets to
    restore would make later tests silently paper-trade. The app-level lists
    matter for the same reason — a leaked SCOUT_CATEGORIES makes a later
    test's Scout return nothing for reasons that have nothing to do with it.
    """
    saved_risk = copy.deepcopy(CONFIG.risk)
    saved_app = {name: copy.deepcopy(getattr(CONFIG, name)) for name in _APP_FIELDS}
    yield CONFIG
    for field, value in vars(saved_risk).items():
        setattr(CONFIG.risk, field, value)
    for name, value in saved_app.items():
        setattr(CONFIG, name, value)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "daemon_kalshi.db")


@pytest.fixture
def order_store(db_path):
    return OrderStore(db_path)


@pytest.fixture
def edge_store(db_path):
    return EdgeStore(db_path)


@pytest.fixture
def client():
    return FakeKalshiClient()


@pytest.fixture
def account(client, order_store):
    return AccountState(client, order_store)


@pytest.fixture
def execution(client, order_store, account):
    # Live mode by default: the point of most of these tests is what happens
    # when real orders are sent. Dry-run behaviour is tested explicitly.
    CONFIG.risk.dry_run = False
    # Reconciled up front, mirroring main.py — execution refuses to submit
    # against unverified account state, so an unreconciled fixture would
    # exercise the refusal path rather than the behaviour under test.
    account.reconcile()
    return Execution(client, order_store, account)


@pytest.fixture
def risk(edge_store, order_store):
    return RiskGuardrail(bankroll_usd=1000.0, store=edge_store, order_store=order_store)


@pytest.fixture
def ledger(client, edge_store, order_store):
    return Ledger(client, edge_store, order_store)


def make_candidate(
    ticker="KXTEST-25AUG14-A",
    title="Test market",
    category="Sports",
    yes_bid=48.0,
    yes_ask=52.0,
    volume=10_000.0,
    event_ticker="KXTEST-25AUG14",
) -> Candidate:
    return Candidate(
        ticker=ticker,
        title=title,
        category=category,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        volume=volume,
        close_time="2026-12-31T00:00:00Z",
        event_ticker=event_ticker,
    )


def make_verdict(
    candidate: Candidate = None,
    maker_probability=0.70,
    verdict="approve",
    confidence=0.90,
    source="llm",
) -> Verdict:
    candidate = candidate or make_candidate()
    proposal = Proposal(
        candidate=candidate,
        maker_probability=maker_probability,
        maker_confidence=0.8,
        reasoning="test reasoning",
        source=source,
    )
    return Verdict(
        proposal=proposal,
        verdict=verdict,
        confidence=confidence,
        reasoning="test check",
    )
