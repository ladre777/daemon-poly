"""Configuration for vidgrab, loaded from the environment.

Every value has a usable default so the app boots with nothing but a
Telegram token set.
"""

import os
import tempfile


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _csv(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, "").strip() or default
    return [p.strip() for p in raw.split(",") if p.strip()]


# --- Telegram ---------------------------------------------------------------
# A dedicated token is preferred so this does not share an update stream with
# the trading daemon, but TELEGRAM_TOKEN is accepted as a fallback.
TELEGRAM_TOKEN = (
    os.environ.get("VIDGRAB_TELEGRAM_TOKEN", "").strip()
    or os.environ.get("TELEGRAM_TOKEN", "").strip()
)
TELEGRAM_API_BASE = os.environ.get(
    "TELEGRAM_API_BASE", "https://api.telegram.org"
).rstrip("/")

# Only these chat IDs may drive the bot. Empty means "nobody" (fail closed).
ALLOWED_CHAT_IDS = set(
    _csv("VIDGRAB_ALLOWED_CHAT_IDS", os.environ.get("TELEGRAM_CHAT_ID", ""))
)

# --- Download behaviour -----------------------------------------------------
# The quality ladder: try each height in turn until the file fits the limit.
QUALITY_LADDER = [int(h) for h in _csv("VIDGRAB_QUALITY_LADDER", "1080,720,480,360,240")]

# Cloud Bot API caps bot uploads at 50 MB. A local Bot API server raises that
# to 2000 MB — set TELEGRAM_API_BASE and VIDGRAB_MAX_UPLOAD_MB together.
MAX_UPLOAD_MB = _int("VIDGRAB_MAX_UPLOAD_MB", 48)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Ceiling for the web UI, where there is no Telegram upload limit to respect.
MAX_WEB_MB = _int("VIDGRAB_MAX_WEB_MB", 4096)
MAX_WEB_BYTES = MAX_WEB_MB * 1024 * 1024

# Refuse absurdly long media before spending bandwidth on it. 0 disables.
MAX_DURATION_MIN = _int("VIDGRAB_MAX_DURATION_MIN", 240)

# Fall back to audio-only when every video rung is still too large.
AUDIO_FALLBACK = _bool("VIDGRAB_AUDIO_FALLBACK", True)

DOWNLOAD_DIR = os.environ.get("VIDGRAB_DOWNLOAD_DIR", "").strip() or os.path.join(
    tempfile.gettempdir(), "vidgrab"
)
# Finished files are kept this long so the web UI can fetch them, then deleted.
FILE_TTL_MIN = _int("VIDGRAB_FILE_TTL_MIN", 60)

# Optional yt-dlp extras for sites that need them.
COOKIES_FILE = os.environ.get("VIDGRAB_COOKIES_FILE", "").strip()
COOKIES_FROM_BROWSER = os.environ.get("VIDGRAB_COOKIES_FROM_BROWSER", "").strip()
PROXY = os.environ.get("VIDGRAB_PROXY", "").strip()
SOCKET_TIMEOUT = _int("VIDGRAB_SOCKET_TIMEOUT", 30)

# --- Web UI -----------------------------------------------------------------
WEB_ENABLED = _bool("VIDGRAB_WEB_ENABLED", True)
PORT = _int("PORT", _int("VIDGRAB_PORT", 8080))
# When set, the web UI requires this token. Leave empty only on a private host.
WEB_TOKEN = os.environ.get("VIDGRAB_WEB_TOKEN", "").strip()
