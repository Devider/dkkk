"""Engine gate (ADR-6): LibreOffice recalculations in a bounded ``spawn`` process pool, one at a time per session.

A calculation slower than the timeout kills the pool's process groups — LibreOffice included — and the pool
is recreated, because ``Future.cancel`` cannot stop a running worker (spec §12 п. 5).
"""

import asyncio
import contextlib
import functools
import multiprocessing
import os
import signal
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError
from aigw_service.config import APP_CONFIG
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()


class EngineGate:
    """Bounded pool + per-session lock + timeout; one instance per service process."""

    def __init__(self, workers: int, timeout: float) -> None:
        self.workers = workers
        self.timeout = timeout
        self._pool: ProcessPoolExecutor | None = None
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    # ---------------------------------------------------------------- pool

    def _get_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=self.workers,
                mp_context=multiprocessing.get_context("spawn"),
                # the worker leads its own process group, so soffice it starts can be killed with it;
                # a builtin keeps the spawned worker from importing this package (and APP_CTX)
                initializer=os.setsid,
            )
        return self._pool

    @staticmethod
    def _worker_pids(pool: ProcessPoolExecutor | None) -> list[int]:
        # concurrent.futures keeps no public list of worker processes
        processes = getattr(pool, "_processes", None) or {}
        return list(processes)

    def _kill_pool(self, pool: ProcessPoolExecutor | None) -> None:
        """Kill the pool's process groups (LibreOffice included); drop it if it is still the current pool.

        Calls of other sessions on a pool that was already replaced must not kill the new pool.
        """
        if pool is None:
            return
        if pool is self._pool:
            self._pool = None
        pids = self._worker_pids(pool)
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        pool.shutdown(wait=False, cancel_futures=True)
        logger.warning("Engine pool killed ({} workers) and will be recreated", len(pids))

    async def shutdown(self) -> None:
        """Service stop: no orphan soffice after the service exits."""
        self._kill_pool(self._pool)

    # ---------------------------------------------------------------- session locks

    @contextlib.asynccontextmanager
    async def _session(self, key: str) -> AsyncIterator[None]:
        lock, waiting = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, waiting + 1)
        try:
            async with lock:
                yield
        finally:
            lock, waiting = self._locks[key]
            if waiting <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, waiting - 1)

    # ---------------------------------------------------------------- run

    async def run(self, session_key: str, func: Callable[..., Any], /, **kwargs: Any) -> Any:
        """Run ``func(**kwargs)`` in the pool; ``func`` must be a top-level (picklable) function."""
        async with self._session(session_key):
            loop = asyncio.get_running_loop()
            pool = self._get_pool()
            future = loop.run_in_executor(pool, functools.partial(func, **kwargs))
            try:
                return await asyncio.wait_for(future, self.timeout)
            except TimeoutError:
                self._kill_pool(pool)
                raise ToolError(ErrorCode.ENGINE_TIMEOUT, f"Расчёт не уложился в {self.timeout:g} с") from None
            except BrokenProcessPool:
                self._kill_pool(pool)
                raise ToolError(ErrorCode.ENGINE_UNAVAILABLE) from None


@functools.cache
def default_gate() -> EngineGate:
    return EngineGate(APP_CONFIG.agent.calc_workers, APP_CONFIG.agent.calc_timeout)
