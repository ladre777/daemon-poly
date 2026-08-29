"""Telegram front end: paste a link, get the video back."""

import html
import re
import threading
import time
import traceback
from typing import Optional

from . import config, downloader, telegram
from .jobs import FAILED, RUNNING, Job, manager

URL_RE = re.compile(r"https?://\S+")

HELP = (
    "<b>vidgrab</b> — paste a link, get the video.\n\n"
    "Send any link and I download it at the best quality that still fits "
    f"Telegram's {config.MAX_UPLOAD_MB}MB limit, stepping down "
    f"{' → '.join(str(h) + 'p' for h in config.QUALITY_LADDER)} until it does.\n\n"
    "<b>Commands</b>\n"
    "<code>&lt;link&gt;</code> — download it\n"
    "<code>/audio &lt;link&gt;</code> — audio only\n"
    "<code>/q 480 &lt;link&gt;</code> — start no higher than 480p\n"
    "<code>/status</code> — what I'm working on\n"
    "<code>/help</code> — this message"
)


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _authorised(chat_id: str) -> bool:
    return str(chat_id) in config.ALLOWED_CHAT_IDS


def _caption(result) -> str:
    bits = [f"<b>{_esc(result.title)}</b>"]
    meta = [
        result.label,
        downloader.human_size(result.size),
        downloader.human_duration(result.duration),
    ]
    bits.append(" · ".join(m for m in meta if m and m != "?"))
    if result.stepped_down:
        stepped = ", ".join(
            str(a) for a in result.attempts if a.outcome != "ok"
        )
        bits.append(f"<i>stepped down — {_esc(stepped)}</i>")
    return "\n".join(bits)


def _progress_text(job: Job) -> str:
    line = f"⏳ <b>{_esc(job.stage)}</b>"
    if job.status == RUNNING and job.percent:
        filled = int(job.percent // 10)
        bar = "█" * filled + "░" * (10 - filled)
        line += f"\n<code>{bar}</code> {job.percent:.0f}%"
    return f"{line}\n<i>{_esc(job.url[:80])}</i>"


def _upload(chat_id, result) -> None:
    caption = _caption(result)
    if result.audio_only:
        telegram.send_chat_action(chat_id, "upload_voice")
        telegram.send_audio(
            chat_id,
            result.path,
            caption,
            title=result.title[:60],
            duration=int(result.duration) if result.duration else None,
        )
        return
    telegram.send_chat_action(chat_id, "upload_video")
    try:
        telegram.send_video(
            chat_id,
            result.path,
            caption,
            width=result.width,
            height=result.height,
            duration=int(result.duration) if result.duration else None,
        )
    except telegram.TelegramError:
        # Some containers are rejected as video but accepted as a file.
        telegram.send_document(chat_id, result.path, caption)


def _handle_job(chat_id, job: Job, notice_id: int) -> None:
    last_text = ""
    while not job.done.wait(3):
        text = _progress_text(job)
        if text != last_text:
            last_text = text
            try:
                telegram.edit_message(chat_id, notice_id, text)
            except Exception:
                pass

    if job.status == FAILED or not job.result:
        telegram.edit_message(
            chat_id, notice_id, f"❌ <b>Failed</b>\n{_esc(job.error or 'unknown error')}"
        )
        manager.discard(job)
        return

    try:
        telegram.edit_message(
            chat_id,
            notice_id,
            f"📤 <b>Uploading</b> {downloader.human_size(job.result.size)} "
            f"({job.result.label})",
        )
        _upload(chat_id, job.result)
        telegram.edit_message(
            chat_id, notice_id, f"✅ <b>{_esc(job.result.title)}</b> — sent"
        )
    except Exception as exc:
        telegram.edit_message(
            chat_id, notice_id, f"❌ <b>Upload failed</b>\n{_esc(str(exc)[:300])}"
        )
    finally:
        manager.discard(job)


def _start_job(
    chat_id,
    url: str,
    reply_to: Optional[int],
    *,
    audio_only: bool = False,
    max_height: Optional[int] = None,
) -> None:
    job = manager.submit(
        Job(
            url=url,
            source="telegram",
            max_bytes=config.MAX_UPLOAD_BYTES,
            max_height=max_height,
            audio_only=audio_only,
        )
    )
    notice = telegram.send_message(chat_id, _progress_text(job), reply_to=reply_to)
    threading.Thread(
        target=_handle_job,
        args=(chat_id, job, notice["message_id"]),
        daemon=True,
    ).start()


def handle_message(msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not chat_id or not text:
        return
    if not _authorised(chat_id):
        print(f"[vidgrab] ignoring message from unauthorised chat {chat_id}")
        return

    message_id = msg.get("message_id")
    command, _, rest = text.partition(" ")
    command = command.lower().split("@")[0]

    if command in ("/start", "/help"):
        telegram.send_message(chat_id, HELP)
        return

    if command == "/status":
        telegram.send_message(chat_id, _status_text())
        return

    if command == "/audio":
        urls = URL_RE.findall(rest)
        if not urls:
            telegram.send_message(chat_id, "Usage: <code>/audio &lt;link&gt;</code>")
            return
        for url in urls:
            _start_job(chat_id, url, message_id, audio_only=True)
        return

    if command == "/q":
        parts = rest.split(None, 1)
        urls = URL_RE.findall(rest)
        if not parts or not parts[0].rstrip("p").isdigit() or not urls:
            telegram.send_message(
                chat_id, "Usage: <code>/q 480 &lt;link&gt;</code>"
            )
            return
        height = int(parts[0].rstrip("p"))
        for url in urls:
            _start_job(chat_id, url, message_id, max_height=height)
        return

    urls = URL_RE.findall(text)
    if not urls:
        telegram.send_message(
            chat_id, "Send me a link, or /help for what I can do."
        )
        return
    for url in urls:
        _start_job(chat_id, url, message_id)


def _status_text() -> str:
    active = manager.active()
    if not active:
        return "💤 Idle — nothing in the queue."
    lines = ["<b>In progress</b>"]
    for job in active[:10]:
        pct = f" {job.percent:.0f}%" if job.percent else ""
        lines.append(f"• {_esc(job.stage)}{pct} — <i>{_esc(job.url[:60])}</i>")
    return "\n".join(lines)


def run() -> None:
    """Long-poll Telegram forever, dispatching each message."""
    me = telegram.get_me()
    print(f"[vidgrab] telegram bot @{me.get('username')} listening")
    print(f"[vidgrab] authorised chats: {sorted(config.ALLOWED_CHAT_IDS) or 'NONE'}")
    offset = 0
    while True:
        try:
            for update in telegram.get_updates(offset):
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    try:
                        handle_message(message)
                    except Exception:
                        traceback.print_exc()
        except Exception as exc:
            print(f"[vidgrab] poll error: {exc}")
            time.sleep(5)
