# -*- coding: utf-8 -*-
"""全市场日线(qfq)回填/每日增量(v0.5.87, 老板 2026-09-13 批)。

动机: klines(qfq) 原先只覆盖自选/扫描池(~168 只), 连板天梯的逐股日K 对库外个股只能显占位。
本模块把覆盖扩到全 A 股: 一次性回填(脚本) + 每日盘后增量(job), 均走既有
`klines_ingestor.ingest_symbol`(marketdata engine 单链单标签, ON CONFLICT 自愈), 不新造取数链。

纯函数部分(宇宙过滤/需补判定)与 IO 分离, 可单测; run_backfill 的 ingest 依赖注入。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

# A 股代码前缀(沪主板/深主板/创业/科创)。
# 注意: 不含 83/87/43/92 —— 那些前缀混入大量新三板挂牌(非交易所), 2026-09-13 实测宇宙膨胀到 12012;
# 北交等涨停池个股由 limit_up_symbols() 并入, 不靠前缀猜。
_A_SHARE_PREFIXES = ("60", "00", "30", "68")


def a_share_universe(stocks: Iterable[dict]) -> list[str]:
    """从 stock_list 条目筛出沪深创科代码(6 位 + 白名单前缀 + market=CN)。"""
    out = []
    for s in stocks or []:
        sym = str(s.get("symbol") or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        if (s.get("market") or "CN") != "CN":
            continue
        if sym[:2] not in _A_SHARE_PREFIXES:
            continue
        out.append(sym)
    return sorted(set(out))


def limit_up_symbols() -> list[str]:
    """涨停池历史去重个股(含北交等), 保证天梯逐股日K 有覆盖。"""
    from sqlalchemy import text

    from src.db.session import SessionLocal

    with SessionLocal() as db:
        rows = db.execute(text("SELECT DISTINCT symbol FROM limit_up_events")).fetchall()
    return sorted({str(r[0]) for r in rows if r[0]})


def merge_universe(main: list[str], pool: list[str]) -> list[str]:
    """沪深创科 ∪ 涨停池个股, 去重排序。"""
    return sorted(set(main) | set(pool))


def filter_needing(universe: list[str], coverage: dict[str, int], min_days: int) -> list[str]:
    """coverage={symbol: 已有 qfq 日线行数}; 返回行数不足 min_days 的(需补/需回填)。"""
    return [s for s in universe if coverage.get(s, 0) < min_days]


def load_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {"done": []}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {"done": list(data.get("done") or [])}
    except (json.JSONDecodeError, OSError):
        return {"done": []}


def save_state(path: str, done: list[str]) -> None:
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"done": done}, fh, ensure_ascii=False)
    os.replace(tmp, path)


async def run_backfill(
    universe: list[str],
    *,
    days: int,
    ingest: Callable,
    concurrency: int = 8,
    done: set[str] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """对 universe 中未完成的符号跑 ingest(symbol, days); 信号量限并发; 返回统计。

    ingest 签名: async (symbol: str, days: int) -> dict(含 ingested)。
    done: 已完成集合(原地更新, 调用方负责持久化)。失败符号不进 done(下轮重试)。
    """
    done = done if done is not None else set()
    todo = [s for s in universe if s not in done]
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    stats = {"total": len(todo), "ok": 0, "fail": 0, "failed_symbols": []}
    completed = 0

    async def one(symbol: str) -> None:
        nonlocal completed
        async with sem:
            try:
                res = await ingest(symbol, days)
                ing = int((res or {}).get("ingested", 0) or 0)
                if ing > 0:
                    done.add(symbol)
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1
                    stats["failed_symbols"].append(symbol)
            except Exception as e:  # noqa: BLE001
                logger.warning("fullmarket kline fail %s: %s", symbol, e)
                stats["fail"] += 1
                stats["failed_symbols"].append(symbol)
            completed += 1
            if on_progress:
                on_progress(completed, stats["total"])

    await asyncio.gather(*(one(s) for s in todo))
    return stats
