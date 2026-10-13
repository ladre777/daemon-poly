import fcntl
import hashlib
import json
import os

import pytest

import paper as pm
import research.shadow_decision as sd

# The REAL live files. No test may touch these; the session guard asserts it.
_LIVE_DIR = "/home/hatch/workspace/polymarket/btc15m"
_LIVE = [os.path.join(_LIVE_DIR, f) for f in (
    "paper_state.json",
    "paper_state.json.last_good",
    "paper_state.json.last_good.prev",
    "paper_state.json.kc_error.json",
    "paper_state.json.kc_audit.log",
    "live_config.json",
    "blotter.jsonl",
    os.path.join("research", "shadow_verdict.json"),
)]

# Same lock tick.sh takes (flock(2), so fcntl.flock is compatible).
# Held for the whole test session; ticks take the designed skip path
# while it is held.
_LOCK_PATH = os.environ.get("BTC_LOCK", "/tmp/btc-paper-run.lock")


def _content_sig(p):
    """sha256 of raw file bytes; None if missing. Never raises."""
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


@pytest.fixture(autouse=True)
def _isolate_paper(tmp_path, monkeypatch):
    # Every test gets a fresh STATE; flag/snapshot paths derive from STATE
    # at call time, so they follow the isolation automatically.
    # BLOTTER is bound once at import, so it needs its own patch --
    # otherwise a test that records a trade would append to the REAL
    # blotter.jsonl (found by Code mode 2026-10-08).
    monkeypatch.setattr(pm, "HERE", str(tmp_path))
    monkeypatch.setattr(pm, "STATE", str(tmp_path / "paper_state.json"))
    monkeypatch.setattr(pm, "BLOTTER", str(tmp_path / "blotter.jsonl"))
    # The verdict latch file: research/shadow_verdict.json is the one
    # file the experiment cannot recover from if a test latches it.
    # Redirect every test's verdict reads/writes to tmp. (2026-10-08,
    # per Claude's review -- do before Sunday.)
    monkeypatch.setattr(sd, "VERDICT_PATH",
                        str(tmp_path / "shadow_verdict.json"))


@pytest.fixture(autouse=True, scope="session")
def _live_untouched():
    # Hold the tick lock for the whole session: the live loop/cron then
    # takes its designed skip path and cannot save state mid-run. With
    # no writer active, a byte-exact before/after comparison is both
    # race-free and blind-spot-free (it catches even a ledger clobber
    # that re-stamps identical hashes -- the case a stable-field hash
    # misses, found by Code mode 2026-10-08). The lock releases on
    # process exit even if the suite crashes. Bounded wait: a stuck tick
    # fails loudly instead of hanging the suite forever.
    import time
    fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + 150
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # Lock held by a live tick; retry until the deadline.
                # Any other OSError (bad fd, etc.) propagates immediately.
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"timed out waiting for the tick lock "
                        f"({_LOCK_PATH}); is a tick stuck?")
                time.sleep(1)
        before = [_content_sig(p) for p in _LIVE]
        yield
        after = [_content_sig(p) for p in _LIVE]
        assert after == before, f"a test touched live state files: {_LIVE}"
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
