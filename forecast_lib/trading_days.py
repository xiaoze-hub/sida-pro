# -*- coding: utf-8 -*-
"""forecast 独立进程的交易日工具(KI-007, 2026-09-18; 2026-09-18 二次修 D5)。

**为什么自带静态表而不是 import 主应用**: 8010 是纯计算服务, 依赖方向只允许
8000→8010(`tests/test_w36_dependency_direction.py` 明确禁止 `forecast_lib` 里出现
`import src.*`), 且 `Dockerfile.forecast` 只 COPY `forecast_server.py` + `forecast_lib/`
(镜像里根本没有 src/) —— 原先那套 "优先 import src.core.trading_calendar, 失败回落
weekday" 的写法在真实 8010 部署里**必然走回落分支**, 等于 KI-007 没修。

**表怎么来的**: 与 `src/core/trading_calendar.py` 的 2025-2028 静态表逐日对齐(生成后
逐条核对)。两表由 `tests/test_calendar_parity.py` 逐日比对 2024-01-01 ~ 2028-12-31,
**任一侧改了表而另一侧没跟 → CI 必红**, 所以"复制一份"不会悄悄漂移。

维护: 每年底追加下一年(国办发节假日安排 + 上交所公告); 改完必须让上面那条 parity 用例过。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# 法定休市日(周末休市由 weekday 判定, 这里只列调休产生的休市)
_HOLIDAYS: dict[int, set[str]] = {
    2025: {
        "2025-01-01",
        "2025-01-28",
        "2025-01-29",
        "2025-01-30",
        "2025-01-31",
        "2025-02-01",
        "2025-02-02",
        "2025-02-03",
        "2025-02-04",
        "2025-04-04",
        "2025-04-05",
        "2025-04-06",
        "2025-05-01",
        "2025-05-02",
        "2025-05-03",
        "2025-05-04",
        "2025-05-05",
        "2025-05-31",
        "2025-06-01",
        "2025-06-02",
        "2025-10-01",
        "2025-10-02",
        "2025-10-03",
        "2025-10-04",
        "2025-10-05",
        "2025-10-06",
        "2025-10-07",
        "2025-10-08",
    },
    2026: {
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
        "2026-02-17",
        "2026-02-18",
        "2026-02-19",
        "2026-02-20",
        "2026-02-21",
        "2026-02-22",
        "2026-02-23",
        "2026-04-04",
        "2026-04-05",
        "2026-04-06",
        "2026-05-01",
        "2026-05-02",
        "2026-05-03",
        "2026-05-04",
        "2026-05-05",
        "2026-06-19",
        "2026-06-20",
        "2026-06-21",
        "2026-09-25",
        "2026-09-26",
        "2026-09-27",
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
    },
    2027: {
        "2027-01-01",
        "2027-01-02",
        "2027-01-03",
        "2027-02-06",
        "2027-02-07",
        "2027-02-08",
        "2027-02-09",
        "2027-02-10",
        "2027-02-11",
        "2027-02-12",
        "2027-04-04",
        "2027-04-05",
        "2027-04-06",
        "2027-05-01",
        "2027-05-02",
        "2027-05-03",
        "2027-05-04",
        "2027-05-05",
        "2027-06-09",
        "2027-06-10",
        "2027-06-11",
        "2027-09-15",
        "2027-09-16",
        "2027-09-17",
        "2027-10-01",
        "2027-10-02",
        "2027-10-03",
        "2027-10-04",
        "2027-10-05",
        "2027-10-06",
        "2027-10-07",
    },
    2028: {
        "2028-01-01",
        "2028-01-02",
        "2028-01-03",
        "2028-01-26",
        "2028-01-27",
        "2028-01-28",
        "2028-01-29",
        "2028-01-30",
        "2028-01-31",
        "2028-02-01",
        "2028-04-04",
        "2028-04-05",
        "2028-04-06",
        "2028-05-01",
        "2028-05-02",
        "2028-05-03",
        "2028-05-04",
        "2028-05-05",
        "2028-05-28",
        "2028-05-29",
        "2028-05-30",
        "2028-09-12",
        "2028-09-13",
        "2028-09-14",
        "2028-10-01",
        "2028-10-02",
        "2028-10-03",
        "2028-10-04",
        "2028-10-05",
        "2028-10-06",
        "2028-10-07",
    },
}

_WORKDAYS: dict[int, set[str]] = {
    2025: {
        "2025-01-26",
        "2025-02-08",
        "2025-04-27",
        "2025-09-28",
        "2025-10-11",
    },
    2026: {
        "2026-02-14",
        "2026-02-28",
        "2026-04-26",
        "2026-05-09",
        "2026-10-10",
    },
    2027: {
        "2027-02-13",
        "2027-04-25",
        "2027-05-08",
        "2027-09-26",
        "2027-10-09",
    },
    2028: {
        "2028-01-22",
        "2028-02-05",
        "2028-04-29",
        "2028-05-06",
        "2028-09-30",
    },
}
_WORKDAY_SET: set[str] = {d for s in _WORKDAYS.values() for d in s}
_HOLIDAY_SET: set[str] = {d for s in _HOLIDAYS.values() for d in s}
_COVERED_YEARS: set[int] = set(_HOLIDAYS) | set(_WORKDAYS)


class TradingCalendarError(RuntimeError):
    """静态表未覆盖该年份(与主应用同口径: 显式报错, 禁止回落 weekday 推测)。"""


def _as_date(d: date | datetime) -> date:
    return d.date() if isinstance(d, datetime) else d


def is_trading_day(d: date | datetime) -> bool:
    """是否 A 股交易日(周末休市 + 法定休市 + 调休补班)。

    与主应用 `src/core/trading_calendar.is_trading_day` 同口径; 表外年份抛错, 不猜。
    """
    dd = _as_date(d)
    if dd.year not in _COVERED_YEARS:
        raise TradingCalendarError(f"交易日历未覆盖 {dd.year} 年, 请补静态表")
    iso = dd.isoformat()
    if iso in _WORKDAY_SET:  # 调休补班: 原为周末, 改为交易日
        return True
    if iso in _HOLIDAY_SET:
        return False
    return dd.weekday() < 5


def is_trading_day_or_weekday(d: date | datetime) -> bool:
    """宽松版: 表外年份回落 weekday(仅供"绝不抛异常"的老调用点, 新代码用 is_trading_day)。"""
    try:
        return is_trading_day(d)
    except TradingCalendarError:
        return _as_date(d).weekday() < 5


def next_trading_day(d: date | datetime) -> date:
    """下一个交易日(严格大于 d)。表外年份抛错。"""
    cur = _as_date(d) + timedelta(days=1)
    for _ in range(30):
        try:
            if is_trading_day(cur):
                return cur
        except TradingCalendarError:
            raise
        cur += timedelta(days=1)
    raise TradingCalendarError(f"{d} 起 30 天内找不到交易日(表可能有洞)")


def next_trading_days(start: date | datetime, n: int) -> list[date]:
    """从 start 次日起取 n 个交易日(保持既有签名; 表外年份回落 weekday 并保持原语义)。"""
    d = _as_date(start) + timedelta(days=1)
    out: list[date] = []
    guard = 0
    while len(out) < n and guard < 60:
        guard += 1
        if is_trading_day_or_weekday(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def add_trading_days(start: date | datetime, n: int) -> date:
    """start 之后第 n 个交易日(n=0 返回 start 本身, 与主应用同口径)。"""
    cur = _as_date(start)
    for _ in range(abs(n)):
        cur = next_trading_day(cur) if n > 0 else _prev_trading_day(cur)
    return cur


def _prev_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    for _ in range(30):
        if is_trading_day_or_weekday(cur):
            return cur
        cur -= timedelta(days=1)
    raise TradingCalendarError(f"{d} 前 30 天内找不到交易日(表可能有洞)")
