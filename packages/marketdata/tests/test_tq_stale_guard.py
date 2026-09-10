"""TQ 陈旧快照防护单测 (2026-09-04, 09-03 漏数事故)。

规则: 最新 bar 日期 < (今天-1天) → 不新鲜(Engine 应降级下一源)。
阈值取 today-1: 盘前/周末/节假日允许差一天, 误杀只多走一次腾讯。
全纯函数断言, 不联网。
"""
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from datetime import datetime

from marketdata.vendors.tq import tq_bars_fresh


def _cst_today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().strftime("%Y%m%d")


def _cst(days_ago: int) -> str:
    d = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=days_ago)
    return d.strftime("%Y%m%d")


def test_fresh_today():
    assert tq_bars_fresh([_cst(2), _cst(0)]) is True


def test_fresh_yesterday_boundary():
    # 昨天允许(盘前/节假日), 今天 TdxW 还没更新也算新鲜
    assert tq_bars_fresh([_cst(5), _cst(1)]) is True


def test_stale_beyond_floor():
    # 2026-09-10 修: floor 已由 today-1 放宽到 **today-3**(见 tq_bars_fresh docstring,
    # 为覆盖周末+1天假期), 故"2 天前"属新鲜; 陈旧须**超过 3 天**。
    assert tq_bars_fresh([_cst(5), _cst(4)]) is False
    assert tq_bars_fresh([_cst(3)]) is True   # 边界: 恰好 today-3 仍算新鲜


def test_empty_is_stale():
    assert tq_bars_fresh([]) is False
    assert tq_bars_fresh(None) is False


def test_dashed_format_accepted():
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    dashed = [(today - timedelta(days=1)).isoformat()]
    assert tq_bars_fresh(dashed) is True


def test_today_floor_sanity():
    # floor = today-1 永不大于 today(规则不会把今天判陈旧)
    assert _cst(0) >= _cst(1)
    assert tq_bars_fresh([_cst(0)]) is True
    assert date.today() is not None and _cst_today() == _cst(0)
