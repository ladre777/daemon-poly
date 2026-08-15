"""
Telegram alerting — the operator's view of an otherwise silent bot.

Same shape as the other clients in this package (espn_client, weather_client):
a small class wrapping one HTTP API, config-driven, no global state. It
differs in one deliberate way, and the difference is the whole point of the
file:

    ESPN and NOAA failures are *loud*. They raise, because feeding Maker
    stale or empty grounding data silently is worse than a visible error.

    Telegram failures are *quiet*. They log a warning and are dropped,
    because an alerting channel must never be able to stop the thing it is
    alerting about. A Telegram outage taking down a live trading bot — or
    worse, halting it mid-order — would be the alerting system causing the
    incident it exists to report.

Non-blocking by construction
----------------------------
Sends go through a bounded queue drained by a background daemon thread. The
trading loop's call is a `put_nowait` and returns immediately, so a slow or
hanging Telegram API cannot stall a scan pass. This matters concretely: the
loop runs on a 30-second cycle, and a synchronous send with an 8-second
timeout on every executed trade would eat a quarter of that budget on a bad
day, and all of it if the API hangs.

If the queue is full (Telegram down, backlog building), the oldest messages
are dropped and a counter is logged. Dropping alerts is the correct failure
mode — unbounded buffering in a long-running trading process is a memory
leak, and stale alerts are not worth memory anyway.

Rate limits
-----------
Telegram allows roughly one message per second to a given chat and bursts
are answered with 429 plus a `retry_after`. The worker spaces sends by
``TELEGRAM_MIN_INTERVAL_SECONDS`` and honours `retry_after` when it sees one.
Per-key throttling on top of that stops a repeating condition (reconciliation
failing every pass) from sending an alert every 30 seconds forever.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from config import CONFIG

log = logging.getLogger("daemon_kalshi.telegram")

API_BASE = "https://api.telegram.org"

#: Telegram rejects messages over 4096 characters outright.
MAX_MESSAGE_CHARS = 4000


@dataclass
class _Outbound:
    text: str
    queued_at: float


class TelegramClient:
    """Fire-and-forget Telegram notifier.

    Disabled unless both a bot token and a chat ID are configured, so a
    checkout with no Telegram setup runs normally and simply says nothing.
    Every public method is safe to call when disabled.
    """

    def __init__(
        self,
        bot_token: str = None,
        chat_id: str = None,
        http: httpx.Client = None,
        start_worker: bool = True,
    ):
        cfg = CONFIG.telegram
        self.bot_token = bot_token if bot_token is not None else cfg.bot_token
        self.chat_id = chat_id if chat_id is not None else cfg.chat_id
        self._http = http or httpx.Client(timeout=cfg.timeout_seconds)
        self._queue: queue.Queue[_Outbound] = queue.Queue(maxsize=cfg.queue_maxsize)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_send_at = 0.0
        #: key -> last time an alert with that key was accepted, for throttling.
        self._last_alert: dict[str, float] = {}
        self._lock = threading.Lock()

        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.throttled = 0

        if self.enabled and start_worker:
            self._start_worker()

    # -- lifecycle ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _start_worker(self) -> None:
        # daemon=True so a wedged send can never keep the process alive; the
        # explicit flush() on shutdown is what gets messages out cleanly.
        self._worker = threading.Thread(
            target=self._drain, name="telegram-notifier", daemon=True
        )
        self._worker.start()

    def flush(self, timeout: float = None) -> bool:
        """Wait for queued messages to drain. Returns True if the queue emptied.

        Called on graceful shutdown so the "shutting down" alert actually
        leaves the process before it exits. Bounded, because a shutdown that
        hangs waiting on Telegram is its own failure.
        """
        if not self.enabled:
            return True
        deadline = time.time() + (
            timeout if timeout is not None else CONFIG.telegram.flush_timeout_seconds
        )
        while time.time() < deadline:
            if self._queue.empty():
                # The worker may still be mid-send on the last item.
                time.sleep(0.1)
                return self._queue.empty()
            time.sleep(0.05)
        remaining = self._queue.qsize()
        if remaining:
            log.warning("Shutting down with %d Telegram message(s) undelivered", remaining)
        return False

    def close(self, flush: bool = True) -> None:
        if flush:
            self.flush()
        self._stop.set()
        try:
            self._http.close()
        except Exception:  # pragma: no cover - close must never raise
            pass

    # -- sending -----------------------------------------------------------

    def send(self, text: str, key: str = None, throttle_seconds: float = None) -> bool:
        """Queue a message. Returns True if it was accepted for delivery.

        Never blocks, never raises. A False return means dropped, throttled,
        or disabled — none of which is an error the caller should handle.

        `key` groups related alerts for throttling: pass a stable string
        (e.g. "reconciliation_failed") and repeats inside the throttle window
        are suppressed. Without a key every call is queued.
        """
        if not self.enabled:
            return False
        try:
            if key is not None and not self._allow(key, throttle_seconds):
                self.throttled += 1
                log.debug("Telegram alert %r throttled", key)
                return False

            text = self._truncate(text)
            item = _Outbound(text=text, queued_at=time.time())
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                # Drop the oldest rather than the newest: during an incident
                # the most recent state is the useful one.
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(item)
                except queue.Empty:  # pragma: no cover - race, harmless
                    pass
                self.dropped += 1
                if self.dropped % 10 == 1:
                    log.warning(
                        "Telegram queue full — dropped %d message(s) so far",
                        self.dropped,
                    )
            return True
        except Exception:
            # Belt and braces. Nothing about notification may propagate into
            # the trading loop.
            log.exception("Telegram send failed unexpectedly — continuing")
            return False

    def _allow(self, key: str, throttle_seconds: float = None) -> bool:
        window = (
            throttle_seconds
            if throttle_seconds is not None
            else CONFIG.telegram.throttle_seconds
        )
        if window <= 0:
            return True
        now = time.time()
        with self._lock:
            last = self._last_alert.get(key, 0.0)
            if now - last < window:
                return False
            self._last_alert[key] = now
            return True

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) <= MAX_MESSAGE_CHARS:
            return text
        return text[:MAX_MESSAGE_CHARS] + f"\n… [truncated from {len(text)} chars]"

    # -- worker ------------------------------------------------------------

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._deliver(item)
            except Exception:
                # The worker thread must not die; if it did, every later
                # alert would queue forever and the operator would go blind
                # without any signal that it had happened.
                self.failed += 1
                log.exception("Telegram delivery raised — dropping this message")
            finally:
                self._queue.task_done()

    def _deliver(self, item: _Outbound) -> None:
        gap = CONFIG.telegram.min_interval_seconds - (time.time() - self._last_send_at)
        if gap > 0:
            time.sleep(gap)

        try:
            resp = self._http.post(
                f"{API_BASE}/bot{self.bot_token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": item.text,
                    "disable_web_page_preview": True,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            self.failed += 1
            log.warning("Telegram unreachable (%r) — alert dropped, trading continues", e)
            return
        finally:
            self._last_send_at = time.time()

        if resp.status_code == 429:
            retry_after = 1.0
            try:
                retry_after = float(
                    resp.json().get("parameters", {}).get("retry_after", 1)
                )
            except (ValueError, AttributeError):
                pass
            log.warning("Telegram rate limited — pausing %.1fs", retry_after)
            time.sleep(min(retry_after, CONFIG.telegram.max_backoff_seconds))
            self.failed += 1
            return

        if resp.status_code != 200:
            self.failed += 1
            # 401 = bad token, 400 "chat not found" = wrong chat ID, 403 =
            # the user has never messaged the bot. All are setup problems the
            # operator has to fix, and all are logged rather than raised.
            log.warning(
                "Telegram API returned %s: %.200s", resp.status_code, resp.text
            )
            return

        self.sent += 1

    # -- semantic alerts ---------------------------------------------------
    #
    # Kept here rather than scattered through the workers so message wording
    # and throttling policy live in one place.

    def notify_startup(self, env: str, dry_run: bool, strategy: str,
                       balance_usd: float, positions: int, exposure_usd: float,
                       bankroll_usd: float) -> None:
        mode = "PAPER (DRY_RUN)" if dry_run else "LIVE — REAL ORDERS"
        self.send(
            f"🟢 DÆMON-KALSHI started\n"
            f"Mode: {mode}\n"
            f"Environment: {env}\n"
            f"Strategy: {strategy}\n"
            f"Balance: ${balance_usd:,.2f}\n"
            f"Effective bankroll: ${bankroll_usd:,.2f}\n"
            f"Open positions: {positions}\n"
            f"Worst-case exposure: ${exposure_usd:,.2f}"
        )

    def notify_shutdown(self, reason: str, filled_today: int = 0,
                        realized_pnl: float = 0.0) -> None:
        self.send(
            f"🔴 DÆMON-KALSHI shutting down\n"
            f"Reason: {reason}\n"
            f"Fills this session: {filled_today}\n"
            f"Realized PnL today: ${realized_pnl:+,.2f}"
        )

    def notify_trade(self, record, decision=None, reason: str = "") -> None:
        """One alert per order that reached the exchange.

        Deliberately reports *filled* quantity and average fill price, not
        what was requested — a zero-fill IOC is a different event from a fill
        and the operator needs to see which happened.
        """
        if record.dry_run:
            header = "🧪 PAPER TRADE (no real order sent)"
        elif record.filled_count <= 0:
            header = "⚪ NO FILL"
        elif record.filled_count < record.requested_count:
            header = "🟡 PARTIAL FILL"
        else:
            header = "🔵 FILLED"

        price = record.avg_fill_price_cents or record.limit_price_cents
        lines = [
            header,
            f"{record.ticker} — BUY {record.side.upper()}",
            f"Filled: {record.filled_count}/{record.requested_count} @ {price:.0f}c",
        ]
        if record.filled_count > 0:
            lines.append(f"Cost: ${record.filled_cost_cents / 100:,.2f} "
                         f"(fees ${record.fees_cents / 100:,.2f})")
        if decision is not None and decision.detail:
            edge = decision.detail.get("net_edge")
            if edge is not None:
                lines.append(f"Net edge: {edge:.2%}")
        lines.append(f"State: {record.state.value}")
        # Why this alert fired now. On a standing signal that has been quiet,
        # "edge moved 6.2pp" is the whole point of breaking the silence.
        if reason:
            lines.append(f"Alerting because: {reason}")
        self.send("\n".join(lines))

    def notify_duplicate_blocked(self, ticker: str, detail: str) -> None:
        """An approved decision that was deliberately not re-submitted.

        Risk approved this trade and execution declined to place it because
        an order for the same intent already exists. That is correct — it is
        what stops a persistent signal from stacking orders every pass — but
        without an alert the operator sees "approved" in the ledger and
        nothing on their phone, which is indistinguishable from alerting
        being broken. Keyed on the ticker so a signal that persists for hours
        costs one message rather than one per pass.
        """
        self.send(
            "\n".join([
                "\u26aa ALREADY HOLDING (no new order)",
                ticker,
                detail,
                "Approved again, but an order for this intent already exists.",
            ]),
            key=f"duplicate:{ticker}",
        )

    def notify_locked_arb(self, arb) -> None:
        """A dual-side opportunity whose profit does not depend on the outcome.

        Detection only — the bot does not place these. A two-legged trade
        needs both legs or neither, and a half-filled arb is an unhedged
        directional position taken for no reason. So this asks the operator to
        act rather than acting, and says so plainly rather than implying a
        trade was made.
        """
        self.send(
            "\n".join([
                "\U0001f7e2 LOCKED ARB DETECTED (not traded)",
                arb.describe(),
                f"Guaranteed after fees: ${arb.total_profit_cents / 100:,.2f} "
                f"across {arb.max_pairs} pair(s).",
                "The bot does not execute these — both legs must fill or "
                "neither, and it has no order-lifecycle management yet.",
            ]),
            key=f"arb:{arb.ticker}",
        )

    def notify_kill_switch(self, reason: str, realized_pnl_today: float,
                           bankroll_usd: float) -> None:
        limit = -abs(CONFIG.risk.max_daily_loss_pct * bankroll_usd)
        self.send(
            f"🛑 KILL SWITCH TRIPPED — trading halted\n"
            f"Reason: {reason}\n"
            f"Realized PnL today: ${realized_pnl_today:+,.2f}\n"
            f"Daily limit: ${limit:,.2f}\n"
            f"This is persisted and survives restart. It requires a manual "
            f"reset before the bot will trade again.",
            key="kill_switch",
            # Long throttle: this condition persists by design, and one alert
            # is the signal. Repeats would be noise on top of a halt.
            throttle_seconds=CONFIG.telegram.kill_switch_throttle_seconds,
        )

    def notify_systemic_error(self, kind: str, detail: str) -> None:
        """Auth, config, database, invariant and reconciliation failures —
        the classes that stop trading rather than being retried."""
        self.send(
            f"⛔ SYSTEMIC ERROR — trading stopped\n"
            f"Type: {kind}\n"
            f"{detail}",
            key=f"systemic:{kind}",
        )

    def notify_stalled(self, what: str, seconds: float) -> None:
        """The brief's watchdog: alert when no successful scan or
        reconciliation has happened within a defined interval.

        A bot that quietly stops trading looks identical to a bot that finds
        no edges. This is what tells them apart.
        """
        self.send(
            f"⚠️ NO SUCCESSFUL {what.upper()} IN {seconds / 60:.0f} MINUTES\n"
            f"The bot is running but not making progress. Check the logs for "
            f"data-feed or API failures.",
            key=f"stalled:{what}",
            throttle_seconds=CONFIG.telegram.stall_throttle_seconds,
        )

    def notify_daily_summary(self, trades: int, fills: int, realized_pnl: float,
                             kill_switch_tripped: bool, exposure_usd: float,
                             open_positions: int) -> None:
        state = "TRIPPED — halted" if kill_switch_tripped else "OK"
        self.send(
            f"📊 DÆMON-KALSHI daily summary\n"
            f"Orders placed: {trades}\n"
            f"Fills: {fills}\n"
            f"Realized PnL: ${realized_pnl:+,.2f}\n"
            f"Open positions: {open_positions}\n"
            f"Worst-case exposure: ${exposure_usd:,.2f}\n"
            f"Kill switch: {state}"
        )

    def notify_test(self) -> bool:
        """Used by `python -m core.telegram_client` to verify setup."""
        return self.send("DÆMON-KALSHI Telegram alerting connected.")


def _main() -> int:
    """Verify TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the shell.

        python -m core.telegram_client

    Exits non-zero if the message could not be delivered, so it is usable as
    a deployment smoke check.
    """
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    client = TelegramClient()
    if not client.enabled:
        print("Telegram is not configured: set TELEGRAM_BOT_TOKEN and "
              "TELEGRAM_CHAT_ID (see README).")
        return 2
    client.notify_test()
    client.flush(timeout=15)
    client.close(flush=False)
    print(f"sent={client.sent} failed={client.failed} dropped={client.dropped}")
    if client.sent:
        print("OK — check the chat for 'DÆMON-KALSHI Telegram alerting connected.'")
        return 0
    print("FAILED — see the warning above. Common causes: wrong token (401), "
          "wrong chat id (400 'chat not found'), or you have not sent the bot "
          "a message yet (403).")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
