"""Bounded, process-local history sharing and synchronous provider admission."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import threading
import time
from typing import Awaitable, Callable

import pandas as pd


def _consume_exception(future: asyncio.Future) -> None:
    # A cancelled waiter must not leave an unobserved exception on its wrapper.
    if not future.cancelled():
        future.exception()


class HistoryRequestCache:
    """Share promises across loops, never asyncio Tasks/Futures from another loop.

    The producing loop owns the task; its shutdown fails all remaining waiters.
    Caller cancellation is shielded. Only successful snapshots enter the LRU.
    """

    def __init__(self, *, ttl_seconds=60.0, max_entries=128, max_inflight=32):
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._max_inflight = max_inflight
        self._clock = time.monotonic
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple, tuple[pd.DataFrame, float]] = OrderedDict()
        self._inflight: dict[tuple, Future] = {}
        self._tasks: set[asyncio.Task] = set()

    def _copy(self, snapshot, *, cache_hit):
        frame, fetched = snapshot
        result = frame.copy(deep=True)
        result.attrs = deepcopy(frame.attrs)
        result.attrs["cache_hit"] = cache_hit
        result.attrs["cache_age_seconds"] = max(0.0, self._clock() - fetched)
        return result

    async def get(self, key: tuple, producer: Callable[[], Awaitable[pd.DataFrame]]) -> pd.DataFrame:
        with self._lock:
            now = self._clock()
            for stale in [k for k, (_, at) in self._cache.items() if now - at >= self._ttl]:
                del self._cache[stale]
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._copy(self._cache[key], cache_hit=True)
            shared = self._inflight.get(key)
            leader = shared is None
            if leader:
                if len(self._inflight) >= self._max_inflight:
                    raise RuntimeError("history request capacity exhausted")
                shared = Future()
                self._inflight[key] = shared

        if leader:
            task = asyncio.create_task(producer())
            with self._lock:
                self._tasks.add(task)
            task.add_done_callback(lambda done: self._complete(key, shared, done))
        wrapped = asyncio.wrap_future(shared)
        wrapped.add_done_callback(_consume_exception)
        snapshot = await asyncio.shield(wrapped)
        # pandas copies of a shared frame are serialized even across OS threads.
        with self._lock:
            return self._copy(snapshot, cache_hit=False)

    def _complete(self, key, shared, task):
        try:
            frame = task.result()
            snapshot = (frame, self._clock())
        except BaseException as exc:
            error = RuntimeError("history producer event loop stopped") if task.cancelled() else exc
            with self._lock:
                self._inflight.pop(key, None)
                self._tasks.discard(task)
            shared.set_exception(error)
            return
        with self._lock:
            self._cache[key] = snapshot
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
            self._inflight.pop(key, None)
            self._tasks.discard(task)
        shared.set_result(snapshot)


class ProviderUnavailable(RuntimeError):
    """Provider admission was skipped; no thread was started."""


class HistoryProviderPool:
    """One actual worker per source, including timed-out or abandoned calls.

    There is no submission backlog. A stuck source cannot consume another
    source's slot, nor can an expired asyncio timeout free its worker early.
    """

    def __init__(self, sources: tuple[str, ...], *, cooldown_seconds=30.0):
        self._sources = frozenset(sources)
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._threads: set[threading.Thread] = set()
        self._closed = False
        self._retry_at: dict[str, float] = {}
        self._cooldown = cooldown_seconds
        self._clock = time.monotonic

    def failed(self, source: str) -> None:
        with self._lock:
            self._retry_at[source] = self._clock() + self._cooldown

    async def run(self, source: str, producer: Callable, *, timeout: float):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            with self._lock:
                if source not in self._sources:
                    raise ValueError(f"unknown history source: {source}")
                if self._closed:
                    raise ProviderUnavailable("provider pool closed")
                if self._clock() < self._retry_at.get(source, 0.0):
                    raise ProviderUnavailable("health backoff")
                if source not in self._active:
                    self._active.add(source)
                    future = Future()
                    # A stuck third-party SDK retains its slot, but cannot hold
                    # interpreter shutdown hostage through executor atexit joins.
                    thread = threading.Thread(
                        target=self._run, args=(source, producer, future), name=f"cn-history-{source}", daemon=True,
                    )
                    self._threads.add(thread)
                    try:
                        thread.start()
                    except BaseException:
                        self._active.remove(source)
                        self._threads.discard(thread)
                        raise
                    break
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ProviderUnavailable("busy (admission budget exhausted)")
            await asyncio.sleep(min(0.01, remaining))
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(_consume_exception)
        return await asyncio.wait_for(asyncio.shield(wrapped), timeout=max(0, deadline - loop.time()))

    def _run(self, source, producer, future):
        try:
            future.set_result(producer())
        except BaseException as exc:
            future.set_exception(exc)
        finally:
            with self._lock:
                self._active.remove(source)
                self._threads.discard(threading.current_thread())

    def close(self, timeout=1.0):
        """Bound teardown without pretending to have cancelled a running SDK."""
        with self._lock:
            self._closed = True
            threads = list(self._threads)
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
