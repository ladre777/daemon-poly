"""
Error classification and a circuit breaker.

Safety brief item 9: failures must be *classified*, and the classification
must decide the behaviour. The distinction that matters in a trading loop is
not which library raised, it is:

  transient  — this attempt failed, a later one plausibly won't. Skip the
               work item, keep the pass going.
  systemic   — something is wrong with the world, not with this item. Every
               subsequent attempt will fail the same way. Stop.
  fatal      — misconfiguration or a broken invariant. Stop and stay stopped.

The bug that motivated this file: a single 404 from the Maker's LLM provider
raised out of `maker.propose()`, unwound the entire `run_once` pass, and was
caught by the top-level "continuing" handler in main. 2,914 candidates were
discarded because of one bad HTTP response, every 30 seconds, forever. The
loop looked healthy in the logs. Nothing traded.

The rule this file encodes: a per-item failure is contained at the item, and
repeated per-item failures escalate to a systemic stop rather than spinning
silently. Fail-closed means *stopping*, which is not the same thing as
catching everything and carrying on.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger("daemon_kalshi.errors")


class Severity(str, Enum):
    TRANSIENT = "transient"
    SYSTEMIC = "systemic"
    FATAL = "fatal"


class DaemonError(Exception):
    """Base for errors this system raises deliberately."""

    severity: Severity = Severity.SYSTEMIC


class TransientError(DaemonError):
    """Retry later; skip this item now."""

    severity = Severity.TRANSIENT


class SystemicError(DaemonError):
    """Every attempt will fail the same way until something changes."""

    severity = Severity.SYSTEMIC


class FatalError(DaemonError):
    """Configuration or invariant failure. Do not continue."""

    severity = Severity.FATAL


def classify(exc: BaseException) -> Severity:
    """Best-effort severity for an arbitrary exception.

    Deliberately conservative: anything unrecognised is SYSTEMIC, because the
    failure mode we are guarding against is treating a real problem as noise
    and continuing to trade through it. An unknown exception in a money loop
    is not evidence that things are fine.
    """
    if isinstance(exc, DaemonError):
        return exc.severity

    # Network-level failures are transient by nature: the request never got a
    # verdict from the server, so trying again later is meaningful.
    name = type(exc).__name__
    if name in {
        "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
        "PoolTimeout", "ReadError", "WriteError", "RemoteProtocolError",
        "TimeoutException", "APIConnectionError", "APITimeoutError",
        "KalshiTimeoutError",
    }:
        return Severity.TRANSIENT

    status = _status_code(exc)
    if status is not None:
        if status == 429 or status >= 500:
            # Rate limited or the provider is down: retryable.
            return Severity.TRANSIENT
        if status in (401, 403):
            # Credentials are wrong. Retrying cannot fix that, and a loop that
            # keeps trying just burns quota against a key that will never work.
            return Severity.FATAL
        if status == 404:
            # A 404 on an endpoint we hardcoded means the endpoint or the
            # model name is wrong — configuration, not weather.
            return Severity.FATAL
        return Severity.SYSTEMIC

    return Severity.SYSTEMIC


def _status_code(exc: BaseException) -> Optional[int]:
    """Pull an HTTP status off httpx / anthropic / requests style exceptions."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


@dataclass
class CircuitBreaker:
    """Trips after `threshold` consecutive failures; resets on any success.

    Consecutive, not cumulative: a provider that fails one call in fifty is
    noisy, and a provider that fails fifty in a row is down. Only the second
    should stop the system.

    `cooldown_seconds` lets a tripped breaker heal on its own, so a provider
    outage doesn't require a redeploy to recover from. Set it to 0 for a
    breaker that stays open until something calls `reset()`.
    """

    name: str
    threshold: int = 5
    cooldown_seconds: float = 300.0
    consecutive_failures: int = 0
    opened_at: Optional[float] = None
    last_error: str = ""
    _now: object = field(default=time.monotonic, repr=False)

    @property
    def is_open(self) -> bool:
        """True while the breaker is refusing work."""
        if self.opened_at is None:
            return False
        if self.cooldown_seconds <= 0:
            return True
        if self._now() - self.opened_at >= self.cooldown_seconds:
            # Cooldown elapsed: allow one probe through. If it fails, the
            # next record_failure re-opens immediately (the failure count is
            # still at threshold).
            self.opened_at = None
            log.info("Circuit breaker %s: cooldown elapsed, allowing a probe", self.name)
            return False
        return True

    def record_success(self) -> None:
        if self.consecutive_failures or self.opened_at is not None:
            log.info("Circuit breaker %s: recovered after %d failure(s)",
                     self.name, self.consecutive_failures)
        self.consecutive_failures = 0
        self.opened_at = None
        self.last_error = ""

    def record_failure(self, error: str = "") -> bool:
        """Record a failure. Returns True if this failure tripped the breaker."""
        self.consecutive_failures += 1
        self.last_error = error
        if self.consecutive_failures >= self.threshold and self.opened_at is None:
            self.opened_at = self._now()
            log.error("Circuit breaker %s TRIPPED after %d consecutive failures: %s",
                      self.name, self.consecutive_failures, error)
            return True
        return False

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self.last_error = ""
