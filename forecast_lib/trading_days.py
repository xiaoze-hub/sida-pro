# -*- coding: utf-8 -*-
"""forecast 独立进程的交易日工具(KI-007, 2026-09-18)。

原先只 `weekday()<5` 判交易日, 法定节假日/调休补班全错 → 预测目标日偏早/偏晚。
优先 import 主应用 `src.core.trading_calendar`(容器内同一代码树); import 失败
(forecast 独立部署无 src)时回落 weekday 并打警告 —— 不假装有日历。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

_try_main = True


def _main_calendar():
    global _try_main
    if not _try_main:
        return None
    try:
        from src.core import trading_calendar as tc  # type: ignore

        return tc
    except Exception:
        _try_main = False
        logger.warning(
            "forecast_lib 无法 import src.core.trading_calendar, 回落 weekday 判交易日(KI-007 降级)"
        )
        return None


def is_trading_day(d: date | datetime) -> bool:
    tc = _main_calendar()
    dd = d.date() if isinstance(d, datetime) else d
    if tc is not None:
        try:
            return tc.is_trading_day(dd)
        except Exception as e:  # noqa: BLE001 — 年份未覆盖等
            logger.warning("trading_calendar 判定失败(%s), 回落 weekday", e)
    return dd.weekday() < 5


def next_trading_days(start: date | datetime, n: int) -> list[date]:
    """从 start 次日起取 n 个交易日。"""
    tc = _main_calendar()
    d = (start.date() if isinstance(start, datetime) else start) + timedelta(days=1)
    out: list[date] = []
    guard = 0
    while len(out) < n and guard < 30:
        guard += 1
        if tc is not None:
            try:
                if tc.is_trading_day(d):
                    out.append(d)
                d = tc.next_trading_day(d) if hasattr(tc, "next_trading_day") else d + timedelta(days=1)
                continue
            except Exception:  # noqa: BLE001
                pass
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out
