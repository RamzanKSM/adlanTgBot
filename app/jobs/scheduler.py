import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable


logger = logging.getLogger(__name__)


class AsyncScheduler:
    def __init__(self) -> None:
        self._jobs: list[tuple[str, float, Callable[[], Awaitable[None]]]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    def add_job(self, name: str, interval_seconds: float, callback: Callable[[], Awaitable[None]]) -> None:
        self._jobs.append((name, interval_seconds, callback))

    def start(self) -> None:
        for name, interval, callback in self._jobs:
            self._tasks.append(asyncio.create_task(self._run_job(name, interval, callback)))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run_job(self, name: str, interval: float, callback: Callable[[], Awaitable[None]]) -> None:
        while not self._stop.is_set():
            try:
                await callback()
            except Exception:
                logger.exception("background job failed: %s", name)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue
