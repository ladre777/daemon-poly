"""Job queue shared by the Telegram bot and the web UI.

One worker thread drains the queue, so a burst of pasted links downloads one
at a time instead of saturating the box. Callers submit a job and either wait
on `job.done` (Telegram) or poll it over HTTP (web).
"""

import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from queue import Queue
from typing import Optional

from . import config, downloader
from .downloader import DownloadFailed, Result

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


@dataclass
class Job:
    url: str
    source: str  # "telegram" | "web"
    max_bytes: Optional[int]
    max_height: Optional[int] = None
    audio_only: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = QUEUED
    stage: str = "queued"
    percent: float = 0.0
    result: Optional[Result] = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    done: threading.Event = field(default_factory=threading.Event)
    workdir: str = ""

    def snapshot(self) -> dict:
        data = {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "stage": self.stage,
            "percent": round(self.percent, 1),
            "error": self.error,
        }
        if self.result:
            data.update(
                title=self.result.title,
                size=self.result.size,
                size_human=downloader.human_size(self.result.size),
                quality=self.result.label,
                duration=downloader.human_duration(self.result.duration),
                filename=os.path.basename(self.result.path),
                attempts=[str(a) for a in self.result.attempts],
            )
        return data


class JobManager:
    def __init__(self, workers: int = 1) -> None:
        self._queue: Queue[Job] = Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._workers = workers
        os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    def start(self) -> None:
        for i in range(self._workers):
            threading.Thread(
                target=self._run, name=f"vidgrab-worker-{i}", daemon=True
            ).start()
        threading.Thread(
            target=self._reaper, name="vidgrab-reaper", daemon=True
        ).start()

    def submit(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        job.stage = f"queued (#{self._queue.qsize()})"
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status in (QUEUED, RUNNING)]

    def discard(self, job: Job) -> None:
        """Delete a finished job's files — the caller is done with them."""
        if job.workdir and os.path.isdir(job.workdir):
            shutil.rmtree(job.workdir, ignore_errors=True)
        job.workdir = ""
        with self._lock:
            self._jobs.pop(job.id, None)

    # --- internals ---------------------------------------------------------
    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                self._process(job)
            except Exception as exc:  # never let a worker die on one bad link
                job.status = FAILED
                job.error = f"unexpected error: {exc}"
            finally:
                job.finished_at = time.time()
                job.done.set()
                self._queue.task_done()

    def _process(self, job: Job) -> None:
        job.status = RUNNING
        job.stage = "resolving link"
        job.workdir = os.path.join(config.DOWNLOAD_DIR, job.id)

        def progress(label: str, pct: float) -> None:
            job.stage = f"downloading {label}"
            job.percent = pct

        try:
            job.result = downloader.download(
                job.url,
                max_bytes=job.max_bytes,
                max_height=job.max_height,
                audio_only=job.audio_only,
                progress=progress,
                workdir=job.workdir,
            )
            job.status = DONE
            job.stage = "ready"
            job.percent = 100.0
        except DownloadFailed as exc:
            job.status = FAILED
            job.error = str(exc)
            job.stage = "failed"

    def _reaper(self) -> None:
        """Delete files whose TTL has expired so the disk does not fill up."""
        while True:
            time.sleep(60)
            cutoff = time.time() - config.FILE_TTL_MIN * 60
            with self._lock:
                stale = [
                    j
                    for j in self._jobs.values()
                    if j.finished_at and j.finished_at < cutoff
                ]
            for job in stale:
                self.discard(job)


manager = JobManager()
