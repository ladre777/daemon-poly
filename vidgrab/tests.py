"""Self-contained checks: `python -m vidgrab.tests`.

The ladder is the part worth pinning down, so the download attempts are
faked and only the stepping logic is exercised — no network needed.
"""

import os
import tempfile

from . import config, downloader
from .downloader import Attempt, DownloadFailed

CASES: list[tuple[str, object]] = []


def check(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn

    return wrap


class FakeSource:
    """Stands in for _try_rung: sizes[label] bytes, or absent = unavailable."""

    def __init__(self, sizes: dict[str, int]):
        self.sizes = sizes
        self.tried: list[str] = []

    def __call__(self, url, workdir, label, fmt, max_bytes, progress):
        self.tried.append(label)
        size = self.sizes.get(label)
        if size is None:
            return None, None, Attempt(label, "unavailable", detail="no such format")
        if max_bytes and size > max_bytes:
            return None, None, Attempt(label, "too_large", size=size)
        path = os.path.join(workdir, f"{label}.mp4")
        os.makedirs(workdir, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"\0" * min(size, 1024))
        return path, {"requested_downloads": [{"height": 0}]}, Attempt(
            label, "ok", size=size
        )


def run_ladder(sizes: dict[str, int], max_bytes: int | None, info=None, **kwargs):
    fake = FakeSource(sizes)
    original_try, original_probe = downloader._try_rung, downloader.probe
    downloader._try_rung = fake
    downloader.probe = lambda url: info or {"title": "clip", "duration": 60}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = downloader.download(
                "https://example.com/v", max_bytes=max_bytes, workdir=tmp, **kwargs
            )
        return result, fake
    finally:
        downloader._try_rung, downloader.probe = original_try, original_probe


MB = 1024 * 1024


@check("takes the top rung when it already fits")
def _():
    result, fake = run_ladder({"1080p": 10 * MB}, 48 * MB)
    assert result.label == "1080p", result.label
    assert fake.tried == ["1080p"], fake.tried
    assert not result.stepped_down


@check("steps down until a rung fits")
def _():
    sizes = {"1080p": 500 * MB, "720p": 120 * MB, "480p": 30 * MB, "360p": 12 * MB}
    result, fake = run_ladder(sizes, 48 * MB)
    assert result.label == "480p", result.label
    assert fake.tried[:3] == ["1080p", "720p", "480p"], fake.tried
    assert result.stepped_down


@check("skips formats the site does not offer")
def _():
    result, fake = run_ladder({"480p": 20 * MB}, 48 * MB)
    assert result.label == "480p", result.label


@check("falls back to audio when no rung fits")
def _():
    sizes = {h: 900 * MB for h in ("1080p", "720p", "480p", "360p", "240p")}
    sizes["audio"] = 4 * MB
    result, fake = run_ladder(sizes, 48 * MB, audio_fallback=True)
    assert result.audio_only and result.label == "audio", result.label
    assert fake.tried[-1] == "audio"


@check("reports failure when even audio is too big")
def _():
    sizes = {h: 900 * MB for h in ("1080p", "720p", "480p", "360p", "240p", "audio")}
    try:
        run_ladder(sizes, 48 * MB, audio_fallback=True)
    except DownloadFailed as exc:
        assert "size limit" in str(exc), exc
    else:
        raise AssertionError("expected DownloadFailed")


@check("/q caps the starting rung")
def _():
    sizes = {"720p": 30 * MB, "480p": 10 * MB, "1080p": 10 * MB}
    result, fake = run_ladder(sizes, 48 * MB, max_height=720)
    assert "1080p" not in fake.tried, fake.tried
    assert result.label == "720p", result.label


@check("no size limit keeps the top rung")
def _():
    result, _ = run_ladder({"1080p": 4096 * MB}, None)
    assert result.label == "1080p", result.label


@check("audio_only skips the video ladder entirely")
def _():
    result, fake = run_ladder({"audio": 3 * MB}, 48 * MB, audio_only=True)
    assert fake.tried == ["audio"], fake.tried
    assert result.audio_only


@check("refuses media longer than the duration cap")
def _():
    try:
        run_ladder(
            {"1080p": MB}, 48 * MB, info={"title": "long", "duration": 99 * 3600}
        )
    except DownloadFailed as exc:
        assert "limit" in str(exc), exc
    else:
        raise AssertionError("expected DownloadFailed")


@check("human_size and human_duration read sensibly")
def _():
    assert downloader.human_size(48 * MB) == "48.0MB"
    assert downloader.human_duration(3725) == "1:02:05"
    assert downloader.human_duration(75) == "1:15"


def main() -> int:
    failures = 0
    for name, case in CASES:
        try:
            case()
            print(f"  ok   {name}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL {name}: {exc}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
