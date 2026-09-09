"""Bounded background execution without a thread per queued job."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from threading import Lock, Thread


class QueueFull(Exception):
    """The local research queue has reached its configured capacity."""


class JobQueue:
    """Run at most ``workers`` tasks and retain at most ``pending`` waiting tasks.

    Workers exit when the queue drains. This also lets unused application instances
    release their threads without depending on a server shutdown hook.
    """

    def __init__(self, workers: int, pending: int = 1000) -> None:
        if workers < 1 or pending < 1:
            raise ValueError("Research concurrency and queue capacity must be positive")
        self.workers = workers
        self.pending = pending
        self._active = 0
        self._lock = Lock()
        self._tasks: deque[Callable[[], None]] = deque()

    def submit(self, task: Callable[[], None]) -> None:
        with self._lock:
            if len(self._tasks) >= self.pending:
                raise QueueFull("Research queue is full; retry after existing work finishes")
            self._tasks.append(task)
            if self._active < self.workers:
                self._active += 1
                try:
                    Thread(target=self._run, name="epiq-research", daemon=True).start()
                except BaseException:
                    self._active -= 1
                    self._tasks.pop()
                    raise

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._tasks:
                    self._active -= 1
                    return
                task = self._tasks.popleft()
            try:
                task()
            except Exception:
                logging.getLogger(__name__).exception("Background job failed unexpectedly")
