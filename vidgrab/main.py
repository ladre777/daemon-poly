"""Entry point: start the job worker, the web UI, and the Telegram bot."""

import sys
import threading

from . import config, downloader, web
from . import bot as tg_bot
from .jobs import manager


def main() -> int:
    print(f"[vidgrab] download dir : {config.DOWNLOAD_DIR}")
    print(f"[vidgrab] upload limit : {config.MAX_UPLOAD_MB}MB")
    print(f"[vidgrab] ladder       : {config.QUALITY_LADDER}")
    if not downloader.HAS_FFMPEG:
        print(
            "[vidgrab] WARNING: ffmpeg not found — limited to progressive "
            "formats, which caps quality on many sites"
        )

    manager.start()

    if config.WEB_ENABLED:
        threading.Thread(target=web.serve, name="vidgrab-web", daemon=True).start()

    if not config.TELEGRAM_TOKEN:
        print("[vidgrab] no TELEGRAM_TOKEN set — running web UI only")
        if not config.WEB_ENABLED:
            print("[vidgrab] nothing to do: no token and web UI disabled")
            return 1
        threading.Event().wait()
        return 0

    if not config.ALLOWED_CHAT_IDS:
        print(
            "[vidgrab] refusing to start: VIDGRAB_ALLOWED_CHAT_IDS (or "
            "TELEGRAM_CHAT_ID) is empty, so every message would be ignored"
        )
        return 1

    tg_bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
