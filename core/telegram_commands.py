"""
Inbound Telegram commands: an operator halt that does not need a redeploy.

The bot could already talk; it could not listen. Every way of stopping it
went through Railway — set a variable, redeploy, wait for the container — and
a kill switch you reach by redeploying is not a kill switch, it is a
deployment. The persisted flag and the risk guardrail that reads it already
existed. This is the missing half: a way for a human to set that flag from a
phone, in seconds.

Deliberately small:

- **Two commands.** ``/halt`` trips the persisted kill switch. ``/status``
  reports what the bot thinks is true. Nothing else is recognised.

- **No ``/resume``.** Halting is safe to do by accident; resuming is not. A
  mistyped message must never be able to put capital back at risk, so
  clearing the switch stays a deliberate act outside this channel.

- **Authenticated on chat_id, strictly.** Anyone who finds a bot can message
  it, and this path reaches the trading loop. Every update from any other
  chat is dropped, and dropped silently — replying to an unknown sender
  confirms the bot exists and is listening.

- **It cannot break the trading loop.** Runs on its own daemon thread, and
  every failure inside it is caught. A Telegram outage means no remote halt,
  which is where we already were; it must never mean no trading, and must
  never mean a crash.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import httpx

from config import CONFIG

log = logging.getLogger("daemon_kalshi.telegram_cmd")

API_BASE = "https://api.telegram.org"

#: Long-poll seconds. Telegram holds the request open until an update arrives
#: or this elapses, so the loop costs one idle request per interval rather
#: than spinning.
POLL_TIMEOUT_SECONDS = 25

#: After a transport error, wait before retrying so a sustained outage does
#: not become a hot loop against Telegram.
ERROR_BACKOFF_SECONDS = 15.0

HELP = (
    "Commands:\n"
    "/halt — stop trading now (persisted, survives restart)\n"
    "/status — balance, kill switch, dry-run state\n\n"
    "There is no /resume. Clearing the halt is deliberate and happens "
    "outside this chat."
)


class TelegramCommandListener:
    """Polls getUpdates and turns two authorised commands into callbacks.

    `on_halt` is called with the reason string when an authorised /halt
    arrives. `status_provider` returns the text /status replies with. Both are
    supplied by the caller so this module owns no trading state.
    """

    def __init__(
        self,
        on_halt: Callable[[str], None],
        status_provider: Callable[[], str],
        send: Callable[[str], object] = None,
        bot_token: str = None,
        chat_id: str = None,
        http: httpx.Client = None,
    ):
        cfg = CONFIG.telegram
        self.bot_token = bot_token if bot_token is not None else cfg.bot_token
        self.chat_id = str(chat_id if chat_id is not None else cfg.chat_id)
        self._on_halt = on_halt
        self._status_provider = status_provider
        self._send = send or (lambda text: None)
        self._http = http or httpx.Client(timeout=POLL_TIMEOUT_SECONDS + 10)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: Telegram's update cursor. Acknowledging past this offset is what
        #: stops one /halt being processed again on the next poll — and after
        #: a restart, what stops a backlog of old commands replaying.
        self._offset: Optional[int] = None

        self.handled = 0
        self.rejected = 0

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        if not self.enabled:
            log.info(
                "Telegram command listener disabled: no bot token or chat id. "
                "Remote /halt is unavailable this run."
            )
            return False
        self._drop_backlog()
        self._thread = threading.Thread(
            target=self._run, name="telegram-commands", daemon=True
        )
        self._thread.start()
        log.info(
            "Telegram command listener started — /halt and /status accepted "
            "from chat %s only", self.chat_id,
        )
        return True

    def stop(self) -> None:
        self._stop.set()

    def _drop_backlog(self) -> None:
        """Skip past anything sent while the bot was down.

        Without this, a restart replays every queued update. A ``/status`` from
        yesterday answering itself is merely confusing; the general case of
        acting on stale commands at startup is not something a halt path
        should do at all.
        """
        try:
            updates = self._get_updates(timeout=0)
        except Exception:
            # Best effort. If this fails the loop below still advances the
            # offset normally; the cost is at most one replayed command.
            log.debug("Could not clear the Telegram update backlog", exc_info=True)
            return
        if updates:
            self._offset = updates[-1]["update_id"] + 1
            log.info("Skipped %d Telegram update(s) queued while offline", len(updates))

    # -- polling -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for update in self._get_updates(timeout=POLL_TIMEOUT_SECONDS):
                    self._offset = update["update_id"] + 1
                    self._handle(update)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                log.warning(
                    "Telegram command polling unreachable (%r) — remote halt "
                    "is unavailable until it recovers; trading continues", e,
                )
                self._stop.wait(ERROR_BACKOFF_SECONDS)
            except Exception:
                # Never let this thread die: it would remove the halt path
                # silently, and the logs would show nothing after the fact.
                log.exception("Telegram command listener error — continuing")
                self._stop.wait(ERROR_BACKOFF_SECONDS)

    def _get_updates(self, timeout: int) -> list:
        params = {"timeout": timeout, "allowed_updates": '["message"]'}
        if self._offset is not None:
            params["offset"] = self._offset
        resp = self._http.get(
            f"{API_BASE}/bot{self.bot_token}/getUpdates", params=params
        )
        if resp.status_code != 200:
            log.warning(
                "Telegram getUpdates returned %s — remote halt unavailable "
                "this cycle", resp.status_code,
            )
            return []
        payload = resp.json()
        if not payload.get("ok"):
            log.warning("Telegram getUpdates not ok: %s", payload.get("description"))
            return []
        return payload.get("result", []) or []

    # -- dispatch ----------------------------------------------------------

    def _handle(self, update: dict) -> None:
        message = update.get("message") or {}
        sender_chat = str((message.get("chat") or {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not text:
            return

        if sender_chat != self.chat_id:
            # Silent by design. A reply would confirm to an unknown sender
            # that this bot is live and processing commands.
            self.rejected += 1
            log.warning(
                "Ignoring Telegram command from unauthorised chat %s: %.40r",
                sender_chat or "<none>", text,
            )
            return

        # "/halt@some_bot extra words" -> "/halt"
        command = text.split()[0].split("@")[0].lower()

        if command == "/halt":
            self._do_halt(text)
        elif command == "/status":
            self.handled += 1
            self._reply(self._safe_status())
        elif command in ("/start", "/help"):
            self.handled += 1
            self._reply(HELP)
        else:
            self._reply(f"Unrecognised command {command}.\n\n{HELP}")

    def _do_halt(self, raw_text: str) -> None:
        reason = f"manual halt via Telegram at {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}"
        try:
            self._on_halt(reason)
        except Exception as e:
            log.exception("Manual halt failed to persist")
            # Tell the operator it did NOT take. A halt that silently failed
            # is worse than no halt path at all, because they will stop
            # watching.
            self._reply(f"HALT FAILED — the switch was NOT set: {e}")
            return
        self.handled += 1
        log.error("KILL SWITCH TRIPPED by operator via Telegram")
        self._reply(
            "🛑 Halted. The kill switch is set and persisted — it survives "
            "restart.\nNo further orders will be placed.\nClearing it is "
            "deliberate and happens outside this chat."
        )

    def _safe_status(self) -> str:
        try:
            return self._status_provider()
        except Exception:
            log.exception("Status provider failed")
            return "Status unavailable — the bot could not read its own state."

    def _reply(self, text: str) -> None:
        try:
            self._send(text)
        except Exception:
            log.exception("Could not reply to a Telegram command")
