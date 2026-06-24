import os
import requests
from datetime import datetime

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID   = "8486909237"
BASE_URL  = f"https://api.telegram.org/bot{BOT_TOKEN}"

EDGE_EMOJI = {
    "CASCADE":  "🌊",
    "BRACKET":  "🗂",
    "IN_PLAY":  "⚡",
    "LADDER":   "📊",
    "PROP":     "🎯",
}

CONFIDENCE_EMOJI = {
    "HIGH":        "🔴",
    "MEDIUM":      "🟡",
    "SPECULATIVE": "⚪",
}


def send_message(text: str, parse_mode: str = "HTML") -> dict:
    try:
        resp = requests.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def send_trade_signal(signal: dict) -> dict:
    edge        = signal.get("edge", "UNKNOWN")
    edge_emoji  = EDGE_EMOJI.get(edge, "🔵")
    conf        = signal.get("confidence", "MEDIUM")
    conf_emoji  = CONFIDENCE_EMOJI.get(conf, "⚪")
    gate        = signal.get("gate_check", "UNKNOWN")
    gate_icon   = "✅" if gate == "PASS" else "⚠️"

    text = (
        f"{conf_emoji} <b>DÆMON-POLY ⚽ SIGNAL</b>\n"
        f"──────────────────────\n"
        f"<b>TYPE:</b> {edge_emoji} {edge}\n"
        f"<b>MARKET:</b> {signal.get('market', 'N/A')}\n"
        f"<b>DIRECTION:</b> {signal.get('direction', 'N/A')} — {signal.get('outcome', '')}\n"
        f"<b>ENTRY:</b> {signal.get('entry_price_pct', '?')}¢\n"
        f"<b>TARGET EXIT:</b> {signal.get('target_exit_pct', '?')}¢\n"
        f"<b>EDGE:</b> {signal.get('rationale', 'N/A')}\n"
        f"<b>GATE CHECK:</b> {gate_icon} {gate}\n"
        f"<b>GATE NOTES:</b> {signal.get('gate_notes', '')}\n"
        f"<b>SIZE:</b> {signal.get('size_pct_bankroll', '?')}% bankroll\n"
        f"<b>CONFIDENCE:</b> {conf}\n"
        f"<b>EXPIRES:</b> {signal.get('expires', 'N/A')}\n"
        f"──────────────────────\n"
        f"⚡ <i>ACTION REQUIRED: APPROVE / SKIP</i>\n"
        f"<code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</code>"
    )
    return send_message(text)


def send_monitor_signal(signal: dict) -> dict:
    text = (
        f"🟡 <b>DÆMON-POLY ⚽ MONITOR</b>\n"
        f"──────────────────────\n"
        f"<b>WATCHING:</b> {signal.get('watch', 'N/A')}\n"
        f"<b>TRIGGER:</b> {signal.get('trigger', 'N/A')}\n"
        f"<b>NEXT CHECK:</b> {signal.get('next_check', 'N/A')}\n"
        f"──────────────────────\n"
        f"<code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</code>"
    )
    return send_message(text)


def send_status(message: str) -> dict:
    return send_message(f"🤖 DÆMON-POLY ⚽\n{message}")


def send_error(error: str) -> dict:
    return send_message(f"🔴 DÆMON-POLY ⚽ ERROR\n{error}")


def get_updates(offset: int = 0) -> list:
    try:
        resp = requests.get(
            f"{BASE_URL}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10,
        )
        data = resp.json()
        return data.get("result", [])
    except Exception:
        return []
