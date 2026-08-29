"""Minimal Telegram Bot API client — just the calls this app needs."""

import os
from typing import Optional

import requests

from . import config


class TelegramError(Exception):
    pass


def _url(method: str) -> str:
    return f"{config.TELEGRAM_API_BASE}/bot{config.TELEGRAM_TOKEN}/{method}"


def _call(method: str, timeout: int = 30, **payload) -> dict:
    resp = requests.post(_url(method), json=payload, timeout=timeout)
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(data.get("description", f"{method} failed"))
    return data.get("result", {})


def get_updates(offset: int, timeout: int = 30) -> list[dict]:
    resp = requests.get(
        _url("getUpdates"),
        params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
        timeout=timeout + 15,
    )
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(data.get("description", "getUpdates failed"))
    return data.get("result", [])


def get_me() -> dict:
    return _call("getMe", timeout=15)


def send_message(chat_id, text: str, reply_to: Optional[int] = None) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    return _call("sendMessage", **payload)


def edit_message(chat_id, message_id: int, text: str) -> None:
    try:
        _call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        # "message is not modified" is routine when progress has not moved.
        if "not modified" not in str(exc):
            raise


def send_chat_action(chat_id, action: str) -> None:
    try:
        _call("sendChatAction", timeout=15, chat_id=chat_id, action=action)
    except Exception:
        pass


def _upload(method: str, chat_id, path: str, field: str, caption: str, extra: dict):
    data = {"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML"}
    data.update({k: str(v) for k, v in extra.items() if v is not None})
    with open(path, "rb") as fh:
        files = {field: (os.path.basename(path), fh)}
        resp = requests.post(
            _url(method), data=data, files=files, timeout=(30, 900)
        )
    payload = resp.json()
    if not payload.get("ok"):
        raise TelegramError(payload.get("description", f"{method} failed"))
    return payload["result"]


def send_video(chat_id, path: str, caption: str, **extra) -> dict:
    extra.setdefault("supports_streaming", "true")
    return _upload("sendVideo", chat_id, path, "video", caption, extra)


def send_audio(chat_id, path: str, caption: str, **extra) -> dict:
    return _upload("sendAudio", chat_id, path, "audio", caption, extra)


def send_document(chat_id, path: str, caption: str, **extra) -> dict:
    return _upload("sendDocument", chat_id, path, "document", caption, extra)
