"""Deterministic history caching, failover and worker lifetime tests."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import Mock

import pandas as pd
import pytest

from xclaw.datasources import a_share


def frame():
    return pd.DataFrame([
        {"日期": "2024-01-02", "开盘": 10.0, "收盘": 11.0,
         "最高": 12.0, "最低": 9.0, "成交量": 100.0},
    ])


async def fetch(symbol="600519", **kwargs):
    return await a_share.fetch_cn_history_dataframe(
        symbol, period=kwargs.get("period", "daily"),
        start_date=kwargs.get("start_date", "2024-01-01"),
        end_date=kwargs.get("end_date", "2024-01-31"),
    )


@pytest.fixture
def isolated(monkeypatch):
    from xclaw.datasources._history_requests import HistoryRequestCache, HistoryProviderPool

    cache = HistoryRequestCache(ttl_seconds=60, max_entries=2, max_inflight=2)
    pool = HistoryProviderPool(("pytdx", "baostock", "yfinance", "akshare"))
    monkeypatch.setattr(a_share, "_HISTORY_CACHE", cache)
    monkeypatch.setattr(a_share, "_HISTORY_PROVIDERS", pool)
    providers = {}
    for source in ("pytdx", "baostock", "yfinance", "akshare"):
        providers[source] = Mock(return_value=frame())
        monkeypatch.setattr(a_share, f"_history_from_{source}", providers[source])
    yield cache, pool, providers
    pool.close()


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_equivalent_keys_share_inflight_and_copy_frames(isolated):
    _, _, providers = isolated
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(2)
        return frame()

    providers["pytdx"].side_effect = blocked
    first = asyncio.create_task(fetch())
    try:
        await wait_until(entered.is_set)
        second = asyncio.create_task(fetch("sh600519", start_date="20240101", end_date="20240131"))
        await asyncio.sleep(0)
    finally:
        release.set()
    left, right = await asyncio.gather(first, second)
    assert providers["pytdx"].call_count == 1
    assert providers["baostock"].call_count == 0
    left.loc[0, "收盘"] = -1
    left["derived"] = 1
    left.attrs["source"] = "changed"
    assert right.iloc[0]["收盘"] == 11
    cached = await fetch("600519.SH")
    assert "derived" not in cached
    assert cached.attrs["source"] == "pytdx"
    assert cached.attrs["fetched_at"] == right.attrs["fetched_at"]
    assert cached.attrs["data_as_of"] == "2024-01-02"
    assert cached.attrs["cache_hit"] is True
    assert cached.attrs["cache_age_seconds"] >= 0


async def test_cache_ttl_is_not_sliding_and_lru_is_bounded(isolated, monkeypatch):
    cache, _, providers = isolated
    now = [100.0]
    monkeypatch.setattr(cache, "_clock", lambda: now[0])
    first = await fetch()
    now[0] += 59
    hit = await fetch()
    assert hit.attrs["cache_age_seconds"] == 59
    assert hit.attrs["fetched_at"] == first.attrs["fetched_at"]
    now[0] += 2
    assert (await fetch()).attrs["cache_hit"] is False
    await fetch("000001")
    await fetch("000002")
    assert len(cache._cache) == 2
    await fetch()
    assert providers["pytdx"].call_count == 5


async def test_period_dates_and_exchange_do_not_alias(isolated):
    _, _, providers = isolated
    await fetch("sz000001")
    await fetch("sh000001")
    await fetch("sz000001", period="weekly")
    await fetch("sz000001", end_date="2024-01-30")
    assert providers["pytdx"].call_count == 4


async def test_cancelling_one_waiter_does_not_cancel_shared_production(isolated):
    _, _, providers = isolated
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(2)
        return frame()

    providers["pytdx"].side_effect = blocked
    first = asyncio.create_task(fetch())
    try:
        await wait_until(entered.is_set)
        second = asyncio.create_task(fetch())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
    assert not (await second).empty
    assert providers["pytdx"].call_count == 1


def test_cache_and_singleflight_work_across_event_loops(isolated):
    _, _, providers = isolated
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(2)
        return frame()

    providers["pytdx"].side_effect = blocked

    async def join():
        task = asyncio.create_task(fetch())
        await asyncio.sleep(0)
        joined.set()
        return await task

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(lambda: asyncio.run(fetch()))
        try:
            assert entered.wait(2)
            second = executor.submit(lambda: asyncio.run(join()))
            assert joined.wait(2)
            assert not second.done()
        finally:
            release.set()
        assert first.result(timeout=2).attrs["source"] == "pytdx"
        assert second.result(timeout=2).attrs["source"] == "pytdx"
    assert asyncio.run(fetch()).attrs["cache_hit"] is True
    assert providers["pytdx"].call_count == 1


async def test_invalid_provider_frame_falls_back_and_backs_off(isolated, monkeypatch):
    _, pool, providers = isolated
    now = [100.0]
    monkeypatch.setattr(pool, "_clock", lambda: now[0])
    providers["pytdx"].return_value = pd.DataFrame({"wrong": [1]})
    assert (await fetch()).attrs["source"] == "baostock"
    assert (await fetch("000001")).attrs["source"] == "baostock"
    assert providers["pytdx"].call_count == 1
    now[0] += 31
    providers["pytdx"].return_value = frame()
    assert (await fetch("000002")).attrs["source"] == "pytdx"


async def test_empty_symbol_data_does_not_poison_provider_health(isolated):
    _, _, providers = isolated
    providers["pytdx"].return_value = pd.DataFrame()
    assert (await fetch()).attrs["source"] == "baostock"
    providers["pytdx"].return_value = frame()
    assert (await fetch("000001")).attrs["source"] == "pytdx"


async def test_independent_queries_wait_briefly_for_preferred_source(isolated):
    _, _, providers = isolated

    def brief(*args):
        time.sleep(0.03)
        return frame()

    providers["pytdx"].side_effect = brief
    rows = await asyncio.gather(fetch("600519"), fetch("000001"))
    assert [row.attrs["source"] for row in rows] == ["pytdx", "pytdx"]
    assert providers["baostock"].call_count == 0


async def test_abandoned_sdk_does_not_block_process_teardown(isolated):
    _, pool, _ = isolated
    entered, release = threading.Event(), threading.Event()

    def sdk():
        entered.set()
        release.wait(2)
        return frame()

    try:
        with pytest.raises(TimeoutError):
            await pool.run("baostock", sdk, timeout=0.02)
        assert entered.is_set()
        assert "baostock" in pool._active
        assert all(thread.daemon for thread in pool._threads)
        started = time.monotonic()
        pool.close(timeout=0.02)
        assert time.monotonic() - started < 0.1
    finally:
        release.set()


async def test_timeout_keeps_source_slot_until_real_thread_finishes(isolated, monkeypatch):
    _, pool, providers = isolated
    release = threading.Event()
    now = [100.0]
    monkeypatch.setattr(pool, "_clock", lambda: now[0])
    monkeypatch.setattr(a_share, "_PROVIDER_TIMEOUT_SECONDS", 0.03)

    def blocked(*args):
        assert release.wait(2)
        return frame()

    providers["pytdx"].side_effect = blocked
    try:
        assert (await fetch()).attrs["source"] == "baostock"
        now[0] += 31  # Even after backoff, the timed-out thread still owns its slot.
        assert (await fetch("000001")).attrs["source"] == "baostock"
        assert providers["pytdx"].call_count == 1
    finally:
        release.set()
    await wait_until(lambda: not pool._active)
    assert (await fetch("000002")).attrs["source"] == "pytdx"


async def test_total_budget_stops_failover_and_does_not_cache_failures(isolated, monkeypatch):
    cache, _, providers = isolated
    release = threading.Event()
    monkeypatch.setattr(a_share, "_PROVIDER_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(a_share, "_HISTORY_TOTAL_TIMEOUT_SECONDS", 0.04)

    def blocked(*args):
        assert release.wait(2)
        return frame()

    providers["pytdx"].side_effect = blocked
    start = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="budget"):
            await fetch()
        assert time.monotonic() - start < 0.5
        assert providers["baostock"].call_count == 0
        assert not cache._cache
        assert not cache._inflight
    finally:
        release.set()


async def test_history_output_preserves_source_and_snapshot_time(isolated):
    from xclaw.tools import ToolContext
    from xclaw.tools.stock_history import StockHistoryTool

    result = await StockHistoryTool().execute(
        {"symbol": "600519", "start_date": "2024-01-01", "end_date": "2024-01-31"},
        ToolContext(chat_id=1, channel="web"),
    )
    assert not result.is_error
    assert "数据截至: 2024-01-02" in result.content
    assert "获取时间:" in result.content


async def test_history_cache_copies_nested_metadata(isolated):
    cache, _, _ = isolated

    async def produce():
        data = frame()
        data.attrs["provenance"] = {"items": ["original"]}
        return data

    first = await cache.get(("test",), produce)
    first.attrs["provenance"]["items"].append("mutated")
    assert (await cache.get(("test",), produce)).attrs["provenance"] == {"items": ["original"]}


async def test_inflight_capacity_and_failure_fanout_do_not_cache_errors(isolated):
    cache, _, _ = isolated
    release = asyncio.Event()
    calls = []

    async def fail():
        calls.append(1)
        await release.wait()
        raise ValueError("fixture failure")

    tasks = [asyncio.create_task(cache.get((key,), fail)) for key in (1, 1, 2)]
    await wait_until(lambda: len(calls) == 2)
    try:
        with pytest.raises(RuntimeError, match="capacity"):
            await cache.get((3,), fail)
    finally:
        release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, ValueError) for result in results)
    assert not cache._cache
    assert not cache._inflight
    assert not cache._tasks

    async def success():
        return frame()

    assert not (await cache.get((1,), success)).empty


async def test_all_waiters_cancelled_still_observe_late_failure(isolated):
    cache, _, _ = isolated
    loop = asyncio.get_running_loop()
    unhandled = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail():
        entered.set()
        await release.wait()
        raise ValueError("late failure")

    task = asyncio.create_task(cache.get((1,), fail))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await wait_until(lambda: not cache._inflight)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not unhandled
    finally:
        loop.set_exception_handler(old_handler)


def test_owner_loop_shutdown_fails_waiters_and_allows_new_loop(isolated):
    cache, _, _ = isolated

    async def abandoned():
        entered = asyncio.Event()

        async def produce():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(cache.get((1,), produce))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return cache._inflight[(1,)]

    shared = asyncio.run(abandoned())
    assert shared.done()
    assert "event loop stopped" in str(shared.exception())
    assert not cache._inflight

    async def retry():
        async def produce():
            return frame()
        return await cache.get((1,), produce)

    assert not asyncio.run(retry()).empty


async def test_worker_capacity_is_process_wide_and_cancellation_keeps_slots(isolated):
    _, pool, _ = isolated
    release = threading.Event()
    entered = []
    lock = threading.Lock()

    def blocked():
        with lock:
            entered.append(1)
        assert release.wait(2)

    tasks = [asyncio.create_task(pool.run(source, blocked, timeout=1))
             for source in ("pytdx", "baostock", "yfinance", "akshare") * 5]
    try:
        await wait_until(lambda: len(entered) == 4)
        await asyncio.sleep(0)
        assert len(pool._active) == 4
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert len(pool._active) == 4
        assert len(entered) == 4
    finally:
        release.set()
    await wait_until(lambda: not pool._active)


def test_baostock_global_session_lock_is_held_through_logout(monkeypatch):
    import sys
    from types import SimpleNamespace

    release, entered = threading.Event(), threading.Event()
    events = []
    lock = threading.Lock()
    monkeypatch.setattr(a_share, "_BAOSTOCK_LOCK", lock)

    def login():
        assert lock.locked()
        events.append("login")
        return SimpleNamespace(error_code="0")

    def query(*args, **kwargs):
        assert lock.locked()
        events.append("query")
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(error_code="0", next=lambda: False)

    def logout():
        assert lock.locked()
        events.append("logout")

    monkeypatch.setitem(sys.modules, "baostock", SimpleNamespace(login=login, logout=logout, query_history_k_data_plus=query))
    symbol = a_share.normalize_cn_symbol("600519")
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(a_share._history_from_baostock, symbol, "daily", "2024-01-01", "2024-01-31")
        try:
            assert entered.wait(2)
            second = executor.submit(a_share._history_from_baostock, symbol, "daily", "2024-01-01", "2024-01-31")
            assert events == ["login", "query"]
        finally:
            release.set()
        first.result(timeout=2)
        second.result(timeout=2)
    assert events == ["login", "query", "logout"] * 2
    assert not lock.locked()


def test_pytdx_empty_bars_are_not_a_source_wide_failure(monkeypatch):
    from types import SimpleNamespace

    api = SimpleNamespace(connect=lambda *args, **kwargs: True,
                          get_security_bars=lambda *args: [], disconnect=lambda: None)
    monkeypatch.setattr("pytdx.hq.TdxHq_API", lambda: api)
    with pytest.raises(a_share._NoHistoryData):
        a_share._history_from_pytdx(
            a_share.normalize_cn_symbol("600519"), "daily", "2024-01-01", "2024-01-31",
        )


async def test_invalid_request_never_spends_provider_budget(isolated):
    _, _, providers = isolated
    for kwargs in ({"start_date": "2024-02-30"}, {"start_date": "2025-01-01"}):
        with pytest.raises(ValueError):
            await fetch(**kwargs)
    assert all(provider.call_count == 0 for provider in providers.values())


async def test_total_budget_is_shared_not_reset_for_each_provider(isolated, monkeypatch):
    from types import SimpleNamespace

    _, pool, _ = isolated
    now, admitted = [100.0], []
    monkeypatch.setattr(a_share, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: now[0]),
        TimeoutError=TimeoutError,
    ))

    async def expire(source, producer, *, timeout):
        admitted.append((source, timeout))
        now[0] += timeout
        raise TimeoutError()

    monkeypatch.setattr(pool, "run", expire)
    with pytest.raises(RuntimeError, match="budget"):
        await fetch()
    assert admitted == [("pytdx", 3.0), ("baostock", 3.0), ("yfinance", 3.0), ("akshare", 1.0)]
