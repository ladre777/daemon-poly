"""yt-dlp wrapper implementing the descending-quality ladder.

The core idea: ask for the best video at a given height cap, and if the
result is bigger than the caller can accept, throw it away and try the next
rung down. yt-dlp's ``max_filesize`` aborts most oversized downloads before
any bytes move; the post-download size check catches the rest (HLS and other
streams that do not declare a size up front).
"""

import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import yt_dlp

from . import config

ProgressCB = Callable[[str, float], None]

# Merging separate video+audio streams needs ffmpeg; without it we can only
# use progressive formats that already carry both.
HAS_FFMPEG = shutil.which("ffmpeg") is not None


class DownloadFailed(Exception):
    pass


@dataclass
class Attempt:
    label: str
    outcome: str  # "too_large" | "unavailable" | "ok"
    size: Optional[int] = None
    detail: str = ""

    def __str__(self) -> str:
        if self.outcome == "too_large" and self.size:
            return f"{self.label}: {human_size(self.size)} — too large"
        if self.outcome == "too_large":
            return f"{self.label}: over limit"
        if self.outcome == "unavailable":
            return f"{self.label}: not available"
        return f"{self.label}: ok"


@dataclass
class Result:
    path: str
    title: str
    ext: str
    size: int
    height: Optional[int]
    duration: Optional[float]
    width: Optional[int] = None
    audio_only: bool = False
    label: str = ""
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def stepped_down(self) -> bool:
        return any(a.outcome != "ok" for a in self.attempts)


def human_size(num: Optional[int]) -> str:
    if not num:
        return "?"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def human_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _base_opts(workdir: str) -> dict:
    opts = {
        "outtmpl": os.path.join(workdir, "%(title).120B.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": config.SOCKET_TIMEOUT,
        "retries": 3,
        "fragment_retries": 3,
        "consoletitle": False,
        "ignoreerrors": False,
    }
    if config.COOKIES_FILE:
        opts["cookiefile"] = config.COOKIES_FILE
    if config.COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (config.COOKIES_FROM_BROWSER,)
    if config.PROXY:
        opts["proxy"] = config.PROXY
    return opts


def probe(url: str) -> dict:
    """Resolve metadata without downloading. Raises DownloadFailed on error."""
    opts = _base_opts(config.DOWNLOAD_DIR)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadFailed(_clean_error(str(exc))) from exc
    if info is None:
        raise DownloadFailed("nothing found at that link")
    # A playlist/channel URL resolves to entries; take the first item.
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadFailed("that link is an empty playlist")
        info = entries[0]
    return info


def _format_for(height: Optional[int]) -> str:
    """Format selector for one rung of the ladder."""
    if height is None:
        return "bv*+ba/b" if HAS_FFMPEG else "b"
    if HAS_FFMPEG:
        return f"bv*[height<={height}]+ba/b[height<={height}]/b"
    return f"b[height<={height}]/b"


def _clean_error(msg: str) -> str:
    msg = msg.replace("ERROR: ", "").strip()
    for prefix in ("[generic] ", "[youtube] "):
        if msg.startswith(prefix):
            msg = msg.split(": ", 1)[-1]
    return msg.splitlines()[0][:300] if msg else "download failed"


def _downloaded_path(info: dict) -> Optional[str]:
    for entry in info.get("requested_downloads") or []:
        path = entry.get("filepath") or entry.get("_filename")
        if path and os.path.exists(path):
            return path
    path = info.get("filepath") or info.get("_filename")
    return path if path and os.path.exists(path) else None


def _progress_hook(label: str, cb: Optional[ProgressCB]):
    state = {"last": 0.0}

    def hook(d: dict) -> None:
        if cb is None or d.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - state["last"] < 2.0:
            return
        state["last"] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        pct = (done / total * 100) if total else 0.0
        cb(label, pct)

    return hook


def _try_rung(
    url: str,
    workdir: str,
    label: str,
    fmt: str,
    max_bytes: Optional[int],
    progress: Optional[ProgressCB],
) -> tuple[Optional[str], Optional[dict], Attempt]:
    opts = _base_opts(workdir)
    opts["format"] = fmt
    opts["progress_hooks"] = [_progress_hook(label, progress)]
    if max_bytes:
        # Aborts before transfer whenever the size is known in advance.
        opts["max_filesize"] = max_bytes
    if HAS_FFMPEG:
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        return None, None, Attempt(label, "unavailable", detail=_clean_error(str(exc)))

    path = _downloaded_path(info or {})
    if not path:
        # yt-dlp skipped it — nearly always the max_filesize guard firing.
        return None, None, Attempt(label, "too_large")

    size = os.path.getsize(path)
    if max_bytes and size > max_bytes:
        os.remove(path)
        return None, None, Attempt(label, "too_large", size=size)

    return path, info, Attempt(label, "ok", size=size)


def download(
    url: str,
    *,
    max_bytes: Optional[int],
    max_height: Optional[int] = None,
    audio_only: bool = False,
    ladder: Optional[list[int]] = None,
    audio_fallback: Optional[bool] = None,
    progress: Optional[ProgressCB] = None,
    workdir: Optional[str] = None,
) -> Result:
    """Download `url`, stepping down the quality ladder until it fits.

    `max_bytes` of None means "no size limit" — the top rung is taken as-is.
    """
    workdir = workdir or os.path.join(config.DOWNLOAD_DIR, uuid.uuid4().hex[:12])
    os.makedirs(workdir, exist_ok=True)

    info = probe(url)
    title = info.get("title") or "video"
    duration = info.get("duration")
    if (
        config.MAX_DURATION_MIN
        and duration
        and duration > config.MAX_DURATION_MIN * 60
    ):
        raise DownloadFailed(
            f"{human_duration(duration)} is longer than the "
            f"{config.MAX_DURATION_MIN} min limit"
        )

    attempts: list[Attempt] = []

    if audio_only:
        path, got, attempt = _try_rung(
            url, workdir, "audio", "ba/b", max_bytes, progress
        )
        attempts.append(attempt)
        if not path:
            raise DownloadFailed(
                attempt.detail or "audio is larger than the upload limit"
            )
        return Result(
            path=path,
            title=title,
            ext=os.path.splitext(path)[1].lstrip("."),
            size=os.path.getsize(path),
            height=None,
            duration=duration,
            audio_only=True,
            label="audio",
            attempts=attempts,
        )

    rungs = list(ladder if ladder is not None else config.QUALITY_LADDER)
    if max_height is not None:
        rungs = [h for h in rungs if h <= max_height] or [max_height]
    if not rungs:
        rungs = [max_height or 720]
    # Never start above the source's own resolution.
    source_height = info.get("height")
    if source_height:
        rungs = [h for h in rungs if h <= source_height] or [source_height]

    for height in rungs:
        label = f"{height}p"
        path, got, attempt = _try_rung(
            url, workdir, label, _format_for(height), max_bytes, progress
        )
        attempts.append(attempt)
        if path:
            picked = (got or {}).get("requested_downloads", [{}])[0]
            return Result(
                path=path,
                title=title,
                ext=os.path.splitext(path)[1].lstrip("."),
                size=os.path.getsize(path),
                height=picked.get("height") or (got or {}).get("height") or height,
                width=picked.get("width") or (got or {}).get("width"),
                duration=duration,
                label=label,
                attempts=attempts,
            )

    fallback = config.AUDIO_FALLBACK if audio_fallback is None else audio_fallback
    if fallback:
        path, got, attempt = _try_rung(
            url, workdir, "audio", "ba/b", max_bytes, progress
        )
        attempts.append(attempt)
        if path:
            return Result(
                path=path,
                title=title,
                ext=os.path.splitext(path)[1].lstrip("."),
                size=os.path.getsize(path),
                height=None,
                duration=duration,
                audio_only=True,
                label="audio",
                attempts=attempts,
            )

    unavailable = [a for a in attempts if a.outcome == "unavailable" and a.detail]
    if len(unavailable) == len(attempts) and unavailable:
        raise DownloadFailed(unavailable[0].detail)
    raise DownloadFailed(
        "even the lowest quality is over the size limit — "
        "try /audio for this one"
    )
