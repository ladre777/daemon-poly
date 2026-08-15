"""
Production-safe persistence (brief item 11's startup check).

Every P0 durability guarantee depends on the SQLite file outliving the
process. On Railway it only does that if a Volume is mounted, and redeploys
happen on every push — so "does this path survive a restart?" has to be
answered at startup rather than discovered after the first redeploy loses a
day of order history and silently un-trips the kill switch.
"""
from __future__ import annotations

import pytest

from memory.db import FALLBACK_DB_PATH, storage_status


@pytest.fixture(autouse=True)
def clean_railway_env(monkeypatch):
    for var in ("RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_ID",
                "RAILWAY_VOLUME_MOUNT_PATH"):
        monkeypatch.delenv(var, raising=False)


def test_a_local_path_is_durable(tmp_path):
    status = storage_status(str(tmp_path / "ledger.db"))
    assert status.usable and status.durable


def test_railway_without_a_volume_is_not_durable(tmp_path, monkeypatch):
    """The default LEDGER_DB_PATH=/data only persists if a Volume is mounted
    there. Without one the container filesystem is discarded on redeploy."""
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

    status = storage_status(str(tmp_path / "ledger.db"))

    assert status.usable, "still writable — the bot can run, it just forgets"
    assert not status.durable
    assert "no Railway Volume" in status.reason


def test_railway_with_a_matching_volume_is_durable(tmp_path, monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))

    status = storage_status(str(tmp_path / "ledger.db"))

    assert status.durable
    assert "volume mounted at" in status.reason


def test_a_path_outside_the_volume_is_not_durable(tmp_path, monkeypatch):
    """Attaching a volume at /data does nothing if LEDGER_DB_PATH points
    somewhere else — an easy and completely silent misconfiguration."""
    volume = tmp_path / "volume"
    volume.mkdir()
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(volume))

    status = storage_status(str(tmp_path / "elsewhere" / "ledger.db"))

    assert not status.durable
    assert "outside the mounted volume" in status.reason


def test_an_unwritable_path_falls_back_rather_than_crashing(monkeypatch):
    """A read-only or forbidden location must not stop the process from
    booting — it degrades to a temporary file and says so."""
    status = storage_status("/proc/definitely-not-writable/ledger.db")

    assert not status.durable
    assert status.effective_path == FALLBACK_DB_PATH
    assert "not writable" in status.reason or "cannot create" in status.reason


def test_the_status_reason_is_always_actionable():
    """Whatever the outcome, the operator gets a sentence explaining it."""
    for path in ("/tmp/ledger.db", "/proc/nope/ledger.db"):
        assert storage_status(path).reason
