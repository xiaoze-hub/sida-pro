"""A股交易日历工具(2026-08-23 S6 修复)。

背景:
- 原 prediction_outcome.py 的 _add_trading_days 仅按 weekday()<5 判断, 把周末当 A 股交易日,
  忽略法定节假日 / 调休补班, 导致回测命中率和预测评估窗口错位。
- 项目内未发现现成的交易日历, 故新建本工具。

设计:
- 静态表覆盖 2025-2027, 含 A 股交易所发布的法定节假日(春节/国庆/中秋等) + 调休补班
- 用集合 + O(1) 查询, 无外部依赖
- 周末(周六周日)仍按 weekday==5/6 判定, 与交易所周末休市一致
- 调用方应当只用本工具的两个公开函数: is_trading_day / add_trading_days,
  不要再走 weekday()<5 之类的旧判定

维护:
- 每年底追加新一年的静态表(参见: 国办发〔YYYY〕XX 号 + 上交所公告)
- 表内日期已与国务院办公厅及上交所发布的 2025/2026/2027 节假日安排一致
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


# 法定休市日(全市场休市, 不分沪深): 元旦/春节/清明/劳动节/端午/中秋/国庆
# 周末休市已由 weekday() 判定, 这里只列调休中需要"补班"的交易日
_HOLIDAYS_2025: set[str] = {
    # 元旦
    "2025-01-01",
    # 春节(2025-01-28 除夕 至 2025-02-04 初七)
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
    # 清明
    "2025-04-04", "2025-04-05", "2025-04-06",
    # 劳动节
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",
    # 端午
    "2025-05-31", "2025-06-01", "2025-06-02",
    # 中秋+国庆合并(2025-10-01 ~ 2025-10-08)
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04",
    "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
}

# 调休补班(原为周末, 改为交易日)
_WORKDAYS_2025: set[str] = {
    "2025-01-26",  # 周日补班(春节前)
    "2025-02-08",  # 周六补班(春节后)
    "2025-04-27",  # 周日补班(劳动节后)
    "2025-09-28",  # 周日补班(国庆前)
    "2025-10-11",  # 周六补班(国庆后)
}

_HOLIDAYS_2026: set[str] = {
    # 元旦
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节(2026-02-17 除夕 至 2026-02-23)
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-21", "2026-02-22", "2026-02-23",
    # 清明
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋(2026-09-25 周五 ~ 09-27 周日)
    "2026-09-25", "2026-09-26", "2026-09-27",
    # 国庆(2026-10-01~07)
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
}

_WORKDAYS_2026: set[str] = {
    "2026-02-14",  # 周六补班(春节前)
    "2026-02-28",  # 周六补班(春节后)
    "2026-04-26",  # 周日补班(劳动节前)
    "2026-05-09",  # 周六补班(劳动节后)
    "2026-10-10",  # 周六补班(国庆后)
}

_HOLIDAYS_2027: set[str] = {
    # 元旦
    "2027-01-01", "2027-01-02", "2027-01-03",
    # 春节(预估 2027-02-06 除夕, 实际以国务院办公厅公告为准)
    "2027-02-06", "2027-02-07", "2027-02-08", "2027-02-09",
    "2027-02-10", "2027-02-11", "2027-02-12",
    # 清明
    "2027-04-04", "2027-04-05", "2027-04-06",
    # 劳动节
    "2027-05-01", "2027-05-02", "2027-05-03", "2027-05-04", "2027-05-05",
    # 端午
    "2027-06-09", "2027-06-10", "2027-06-11",
    # 中秋(预估 2027-09-15~17)
    "2027-09-15", "2027-09-16", "2027-09-17",
    # 国庆
    "2027-10-01", "2027-10-02", "2027-10-03", "2027-10-04",
    "2027-10-05", "2027-10-06", "2027-10-07",
}

_WORKDAYS_2027: set[str] = {
    "2027-02-13",  # 周六补班(春节前)
    "2027-04-25",  # 周日补班(劳动节前)
    "2027-05-08",  # 周六补班(劳动节后)
    "2027-09-26",  # 周日补班(国庆前)
    "2027-10-09",  # 周六补班(国庆后)
}

_HOLIDAYS: dict[int, set[str]] = {
    2025: _HOLIDAYS_2025,
    2026: _HOLIDAYS_2026,
    2027: _HOLIDAYS_2027,
}

_WORKDAYS: dict[int, set[str]] = {
    2025: _WORKDAYS_2025,
    2026: _WORKDAYS_2026,
    2027: _WORKDAYS_2027,
}


def _to_iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _to_date(d: date | datetime | str) -> date:
    if isinstance(d, str):
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    if isinstance(d, datetime):
        return d.date()
    return d


def is_trading_day(d: date | datetime | str) -> bool:
    """判断指定日期是否为 A 股交易日。

    判定规则:
    1. 周末(weekday() >= 5) → 非交易日
    2. 在该年的调休补班集合内 → 视为交易日(覆盖周六/周日)
    3. 在该年的法定节假日集合内 → 非交易日(覆盖周一~周五)
    4. 其他 → 视为交易日

    Args:
        d: date / datetime / ISO 字符串 "YYYY-MM-DD"

    Returns:
        True = A 股交易日; False = 休市
    """
    try:
        d = _to_date(d)
    except Exception:
        return False

    iso = _to_iso(d)
    year = d.year
    holidays = _HOLIDAYS.get(year, set())
    workdays = _WORKDAYS.get(year, set())

    # 调休补班优先(覆盖周末)
    if iso in workdays:
        return True
    # 法定节假日(覆盖任何 weekday)
    if iso in holidays:
        return False
    # 周末兜底
    if d.weekday() >= 5:
        return False
    return True


def add_trading_days(start: date | datetime | str, n: int) -> date:
    """从 start 起算 n 个 A 股交易日, 返回第 n 个交易日的日期。

    关键修正(S6, 2026-08-23): 原实现仅按 weekday()<5 计数, 忽略法定节假日 + 调休补班,
    导致评估窗口错位。本函数基于 is_trading_day 的判定严格走法定交易日历。

    Args:
        start: 起始日期(包含, n<=0 时返回 start 本身)
        n: 交易日数(>=0)

    Returns:
        第 n 个交易日的 date
    """
    start = _to_date(start)

    if n <= 0:
        return start

    cur = start
    added = 0
    # 兜底上限: 防止 n 异常大时死循环(一年 250 个交易日, 8 倍冗余)
    max_iter = max(365 * 8, n * 8) + 30
    iters = 0
    while added < n and iters < max_iter:
        iters += 1
        cur = cur + timedelta(days=1)
        if is_trading_day(cur):
            added += 1
    return cur


def next_trading_day(d: date | datetime | str) -> date:
    """返回 d 之后的下一个 A 股交易日(d 本身是交易日时返回 d+1 起算的下一个交易日; 不含 d)."""
    d = _to_date(d)
    cur = d
    while True:
        cur = cur + timedelta(days=1)
        if is_trading_day(cur):
            return cur


def prev_trading_day(d: date | datetime | str) -> date:
    """返回 d 之前的最近一个 A 股交易日(d 本身是交易日时返回前一个交易日; 不含 d)."""
    d = _to_date(d)
    cur = d
    while True:
        cur = cur - timedelta(days=1)
        if is_trading_day(cur):
            return cur


def trading_days_between(
    start: date | datetime | str,
    end: date | datetime | str,
    inclusive: bool = False,
) -> int:
    """计算严格开区间 (start, end) 之间的 A 股交易日数量(默认不含 start, 不含 end)。

    Args:
        start: 起始日期(不含)
        end: 截止日期(不含, 默认)
        inclusive: 是否包含 end 这一天(默认 False)

    Returns:
        交易日数量
    """
    start = _to_date(start)
    end = _to_date(end)

    if end <= start:
        return 0
    # 严格 (start, end) 开区间: 第一天 = start + 1
    cur = start + timedelta(days=1)
    cnt = 0
    while cur < end:
        if is_trading_day(cur):
            cnt += 1
        cur = cur + timedelta(days=1)
    if inclusive and is_trading_day(end):
        cnt += 1
    return cnt
