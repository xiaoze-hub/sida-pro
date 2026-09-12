"""全市场日线回填: 宇宙过滤/需补判定/并发 runner(失败不进 done)。"""
import asyncio

import pytest

from src.core.klines_fullmarket import a_share_universe, filter_needing, run_backfill


def test_a_share_universe_filters_non_stock():
    stocks = [
        {"symbol": "600519", "market": "CN"},      # 沪主板 收
        {"symbol": "000001", "market": "CN"},      # 深主板 收
        {"symbol": "300750", "market": "CN"},      # 创业 收
        {"symbol": "688981", "market": "CN"},      # 科创 收
        {"symbol": "832000", "market": "CN"},      # 新三板 排(非交易所)
        {"symbol": "510300", "market": "CN"},      # 基金 排
        {"symbol": "123456", "market": "CN"},      # 转债 排
        {"symbol": "600519", "market": "CN"},      # 去重
        {"symbol": "00700", "market": "HK"},       # 非CN 排
        {"symbol": "abc", "market": "CN"},         # 非6位 排
    ]
    assert a_share_universe(stocks) == ["000001", "300750", "600519", "688981"]


def test_merge_universe_adds_pool_symbols():
    from src.core.klines_fullmarket import merge_universe

    assert merge_universe(["600519", "000001"], ["830001", "000001"]) == [
        "000001", "600519", "830001"]


def test_filter_needing_uses_coverage():
    uni = ["A", "B", "C"]
    cov = {"A": 500, "B": 3}
    assert filter_needing(uni, cov, min_days=250) == ["B", "C"]


@pytest.mark.asyncio
async def test_run_backfill_ok_and_fail_and_done():
    calls = []

    async def ingest(symbol, days):
        calls.append((symbol, days))
        if symbol == "BAD":
            return {"ingested": 0}
        return {"ingested": 10}

    done = set()
    stats = await run_backfill(["A", "B", "BAD"], days=500, ingest=ingest, concurrency=2, done=done)
    assert stats["ok"] == 2 and stats["fail"] == 1
    assert stats["failed_symbols"] == ["BAD"]
    assert done == {"A", "B"}                      # 失败不进 done(下轮重试)
    assert sorted(c for c, _ in calls) == ["A", "B", "BAD"]
    assert all(d == 500 for _, d in calls)


@pytest.mark.asyncio
async def test_run_backfill_skips_already_done():
    calls = []

    async def ingest(symbol, days):
        calls.append(symbol)
        return {"ingested": 1}

    done = {"A"}
    await run_backfill(["A", "B"], days=10, ingest=ingest, done=done)
    assert calls == ["B"]
    assert done == {"A", "B"}


@pytest.mark.asyncio
async def test_run_backfill_exception_counts_fail_not_raise():
    async def ingest(symbol, days):
        raise RuntimeError("vendor down")

    done = set()
    stats = await run_backfill(["A"], days=10, ingest=ingest, done=done)
    assert stats["fail"] == 1 and stats["ok"] == 0 and done == set()
