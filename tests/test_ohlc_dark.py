# -*- coding: utf-8 -*-
"""OHLC 分摊暗盘(APZJ 对照项) 单测。

覆盖:
  - 收盘位置公式(典型价 / 金额单位 / 分摊比例)
  - 一字板(H==L)三种形态
  - 脏数据跳过(不编造)
  - 多周期窗口
  - 空输入显式"无数据"
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core.ohlc_dark import allocate_bar, ohlc_dark_net  # noqa: E402


def _bar(o, h, l, c, v, date="2026-08-31"):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v, "date": date}


# ---------------------------------------------------------------------------
# 单根分摊
# ---------------------------------------------------------------------------


def test_allocate_bar_close_at_high_all_buy():
    """收盘=最高 → pos=1, 全部分摊为买。"""
    a = allocate_bar(o=10.0, h=11.0, l=10.0, c=11.0, volume=1000)
    tp = (11.0 + 10.0 + 2 * 11.0) / 4
    assert a.pos == pytest.approx(1.0)
    assert a.amount == pytest.approx(1000 * tp)
    assert a.buy == pytest.approx(a.amount)
    assert a.sell == pytest.approx(0.0)
    assert a.net == pytest.approx(a.amount)


def test_allocate_bar_close_at_low_all_sell():
    """收盘=最低 → pos=0, 全部分摊为卖。"""
    a = allocate_bar(o=10.0, h=11.0, l=9.0, c=9.0, volume=1000)
    assert a.pos == pytest.approx(0.0)
    assert a.buy == pytest.approx(0.0)
    assert a.net == pytest.approx(-a.amount)


def test_allocate_bar_close_mid_half_split():
    """收盘在区间中点 → pos=0.5, 买卖各半, 净额≈0。"""
    a = allocate_bar(o=10.0, h=11.0, l=9.0, c=10.0, volume=2000)
    assert a.pos == pytest.approx(0.5)
    assert a.net == pytest.approx(0.0, abs=1e-6)


def test_allocate_bar_amount_unit_is_yuan():
    """金额 = 股 × 典型价(元), 验证单位口径。"""
    a = allocate_bar(o=10.0, h=10.4, l=9.6, c=10.2, volume=1_000_000)
    tp = (10.4 + 9.6 + 2 * 10.2) / 4
    assert a.amount == pytest.approx(1_000_000 * tp)


def test_allocate_bar_rejects_invalid():
    """非数/负值/零量 → None(跳过, 不编造)。"""
    assert allocate_bar(o=0, h=1, l=0, c=1, volume=100) is None      # o<=0
    assert allocate_bar(o=1, h=1, l=1, c=1, volume=0) is None        # 量=0
    assert allocate_bar(o=1, h=1, l=2, c=1, volume=100) is None      # H<L 脏数据
    assert allocate_bar(o=None, h=1, l=0, c=1, volume=100) is None   # 非数


# ---------------------------------------------------------------------------
# 一字板
# ---------------------------------------------------------------------------


def test_limit_up_bar_full_buy():
    """一字涨停(H==L==C>O) → 全买。"""
    a = allocate_bar(o=10.0, h=11.0, l=11.0, c=11.0, volume=500)
    assert a.pos is None
    assert a.net == pytest.approx(a.amount)


def test_limit_down_bar_full_sell():
    """一字跌停(H==L==C<O) → 全卖。"""
    a = allocate_bar(o=11.0, h=10.0, l=10.0, c=10.0, volume=500)
    assert a.pos is None
    assert a.net == pytest.approx(-a.amount)


def test_flat_bar_neutral():
    """一字平盘(H==L==C==O) → 中性, 净额 0。"""
    a = allocate_bar(o=10.0, h=10.0, l=10.0, c=10.0, volume=500)
    assert a.pos is None
    assert a.net == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 多周期聚合
# ---------------------------------------------------------------------------


def test_ohlc_dark_net_sums_window():
    bars = [
        _bar(10, 11, 10, 11, 1000, "2026-08-27"),   # 全买
        _bar(11, 11, 10, 10, 1000, "2026-08-28"),   # 全卖
        _bar(10, 11, 9, 10, 2000, "2026-08-31"),    # 中性
    ]
    r = ohlc_dark_net(bars)
    a1 = allocate_bar(10, 11, 10, 11, 1000)
    a2 = allocate_bar(11, 11, 10, 10, 1000)
    assert r["dark_net"] == pytest.approx(a1.net + a2.net + 0.0, rel=1e-9)
    assert r["bars_used"] == 3
    assert r["approximation"] is True


def test_ohlc_dark_net_days_window():
    bars = [
        _bar(10, 11, 10, 11, 1000, "2026-08-27"),
        _bar(11, 11, 10, 10, 1000, "2026-08-28"),
        _bar(10, 11, 9, 11, 1000, "2026-08-31"),
    ]
    r1 = ohlc_dark_net(bars, days=1)
    r3 = ohlc_dark_net(bars, days=3)
    assert r1["bars_used"] == 1
    assert r3["bars_used"] == 3
    # 1 日 = 最后那根(全买)
    assert r1["dark_net"] > 0


def test_ohlc_dark_net_skips_invalid_bars():
    bars = [
        _bar(10, 11, 10, 11, 1000),
        {"open": 0, "high": 1, "low": 0, "close": 1, "volume": 100},  # 脏
        _bar(10, 11, 9, 10, 1000),
    ]
    r = ohlc_dark_net(bars)
    assert r["bars_used"] == 2
    assert r["bars_skipped"] == 1


def test_ohlc_dark_net_empty_marks_no_data():
    r = ohlc_dark_net([])
    assert r["dark_net"] is None
    assert r["note"] == "无数据"
    assert r["approximation"] is True


def test_ohlc_dark_net_all_invalid_marks_no_data():
    r = ohlc_dark_net([{"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0}])
    assert r["dark_net"] is None
    assert r["bars_skipped"] == 1


def test_ohlc_dark_net_direction_follows_close_position():
    """方向正确性: 连续收高的日子 → 净额为正; 连续收低 → 为负。"""
    up = [_bar(10, 11 + i * 0.1, 10, 10.9 + i * 0.1, 1000) for i in range(5)]
    down = [_bar(11, 11, 10 - i * 0.1, 10.1 - i * 0.1, 1000) for i in range(5)]
    assert ohlc_dark_net(up)["dark_net"] > 0
    assert ohlc_dark_net(down)["dark_net"] < 0
