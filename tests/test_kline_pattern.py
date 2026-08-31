"""kline_pattern 模块测试(合成 K 线验证各形态识别)。"""

from __future__ import annotations

import pytest


class FakeBar:
    def __init__(self, o, h, l, c, v=1.0, date="2026-08-01"):
        self.open, self.high, self.low, self.close, self.volume, self.date = o, h, l, c, v, date


def make_bars(rows):
    """rows: [(o,h,l,c,v), ...] → [FakeBar...]"""
    return [FakeBar(*r, date=f"2026-08-{i+1:02d}") for i, r in enumerate(rows)]


def test_golden_needle():
    """金针探底: 低位极长下影线。"""
    from src.core.kline_pattern import detect_patterns
    # 前 5 根阴跌,最后一根长下影(下影=5, 实体=1)
    bars = make_bars([
        (10, 10.2, 9.8, 9.9, 1), (9.9, 10.0, 9.5, 9.6, 1), (9.6, 9.7, 9.2, 9.3, 1),
        (9.3, 9.4, 8.9, 9.0, 1), (9.0, 9.1, 8.6, 8.7, 1),
        (8.2, 9.0, 7.0, 8.8, 2),  # 实体=0.6 下影=1.2(2x) 收盘8.8 在窗口下半区
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert "金针探底" in names, f"应识别金针探底,实际 {names}"


def test_three_red_soldiers():
    """红三兵: 三连阳收盘递增。"""
    from src.core.kline_pattern import detect_patterns
    # 前 3 根横盘背景 + 3 根连阳
    bars = make_bars([
        (10, 10.1, 9.9, 10.0, 1), (10, 10.1, 9.9, 10.0, 1), (10, 10.1, 9.9, 10.0, 1),
        (10, 10.5, 9.8, 10.4, 1), (10.4, 10.9, 10.2, 10.8, 1.2), (10.8, 11.4, 10.7, 11.3, 1.5),
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert "红三兵" in names, f"应识别红三兵,实际 {names}"


def test_double_needle_bottom():
    """双针探底: 两根长下影低点接近。"""
    from src.core.kline_pattern import detect_patterns
    # 两根长下影: 实体0.6 下影1.3,低点 9.0/9.05 接近
    bars = make_bars([
        (10, 10.2, 9.9, 10.1, 1), (10.1, 10.3, 9.9, 10.2, 1), (10.2, 10.4, 9.9, 10.3, 1),
        (10.3, 11.0, 9.0, 10.9, 1.5),  # 实体0.6 下影1.3
        (10.9, 11.5, 9.05, 11.4, 1.5),  # 实体0.5 下影1.85
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert "双针探底" in names, f"应识别双针探底,实际 {names}"


def test_limit_up_double_cannon():
    """涨停双响炮: 涨停→整理→涨停。"""
    from src.core.kline_pattern import detect_patterns
    bars = make_bars([
        (10, 10.1, 9.9, 10.0, 1), (10, 10.1, 9.9, 10.0, 1), (10, 10.1, 9.9, 10.0, 1),
        (10, 11.2, 9.9, 11.2, 3),   # 涨停
        (11.2, 11.3, 11.0, 11.1, 1),  # 整理
        (11.1, 12.3, 11.0, 12.3, 3),  # 涨停
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert "涨停双响炮" in names, f"应识别涨停双响炮,实际 {names}"


def test_no_pattern_on_flat():
    """横盘无形态。"""
    from src.core.kline_pattern import detect_patterns
    bars = make_bars([(10, 10.1, 9.9, 10.0, 1)] * 10)
    hits = detect_patterns(bars)
    assert len(hits) == 0, f"横盘不应有形态,实际 {[h.name for h in hits]}"


def test_format_empty():
    from src.core.kline_pattern import format_patterns
    assert "未识别到" in format_patterns([])


# ============ 看跌形态测试(2026-08-10 第二篇学习文) ============

def test_three_crows():
    """三只乌鸦: 顶部三根连续阴线。"""
    from src.core.kline_pattern import detect_patterns
    # 前 3 根上涨背景 + 顶部三连阴(实体中等,收在相对高位)
    bars = make_bars([
        (10, 10.5, 9.9, 10.4, 1), (10.4, 11.0, 10.3, 10.9, 1), (10.9, 11.5, 10.8, 11.4, 1),
        (11.4, 11.5, 10.6, 10.7, 1.5), (10.7, 10.8, 9.9, 10.0, 1.5), (10.0, 10.1, 9.3, 9.4, 1.5),
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert "三只乌鸦" in names, f"应识别三只乌鸦,实际 {names}"


def test_bearish_engulfing_rain():
    """倾盆大雨: 大阳线后低开大阴线。"""
    from src.core.kline_pattern import detect_patterns
    bars = make_bars([
        (10, 10.3, 9.9, 10.2, 1), (10.2, 10.5, 10.1, 10.4, 1), (10.4, 10.7, 10.3, 10.6, 1),
        (10.6, 11.5, 10.5, 11.4, 2),   # 大阳线(实体0.8)
        (11.0, 11.1, 10.3, 10.4, 2),    # 低开大阴线(实体0.6,收在窗口中部偏上)
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert "倾盆大雨" in names, f"应识别倾盆大雨,实际 {names}"


def test_evening_star():
    """黄昏之星: 长阳→星线→长阴。"""
    from src.core.kline_pattern import detect_patterns
    bars = make_bars([
        (10, 10.3, 9.9, 10.2, 1), (10.2, 10.5, 10.1, 10.4, 1),
        (10.4, 11.5, 10.3, 11.4, 2),   # 长阳
        (11.4, 11.45, 11.35, 11.4, 1),  # 星线(小实体)
        (11.2, 11.3, 10.6, 10.7, 2),    # 长阴(跌但仍在相对高位)
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert "黄昏之星" in names, f"应识别黄昏之星,实际 {names}"


def test_short_black_three():
    """黑三兵: 三根连续下跌小阴线(不在高位不识别)。"""
    from src.core.kline_pattern import detect_patterns
    # 底部横盘后三连阴(前5日无上涨 → 不识别)
    bars = make_bars([
        (8.0, 8.05, 7.95, 8.0, 1), (8.0, 8.05, 7.95, 8.0, 1), (8.0, 8.05, 7.95, 8.0, 1),
        (7.9, 7.95, 7.85, 7.9, 1), (7.85, 7.9, 7.8, 7.85, 1), (7.8, 7.85, 7.75, 7.8, 1),
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert "黑三兵" not in names, f"低位不应识别黑三兵,实际 {names}"


# ============ 经典形态测试(同花顺《K线形态大全》可量化部分) ============

def _gen_series(rows):
    """rows: [(o,h,l,c,v),...] → FakeBar 列表"""
    return make_bars(rows)


def test_double_bottom_breakout():
    """双底突破(W底): 两个相近低点后突破颈线。"""
    from src.core.kline_pattern import detect_patterns
    # 20根: 下跌→低点10→反弹→低点10.1→突破
    rows = [(20 - i * 0.1, 20 - i * 0.1 + 0.3, 20 - i * 0.1 - 0.3, 20 - i * 0.1, 1) for i in range(8)]
    rows += [(12, 12.3, 10.0, 12.2, 1.5), (12.2, 12.5, 12.0, 12.4, 1),
             (12.4, 12.6, 12.2, 12.5, 1), (12.5, 12.8, 12.3, 12.7, 1),
             (12.7, 13.0, 12.5, 12.9, 1), (12.9, 13.1, 12.7, 13.0, 1),
             (13.0, 13.2, 10.1, 13.1, 1.5), (13.1, 13.3, 12.9, 13.2, 1),
             (13.2, 13.4, 13.0, 13.3, 1), (13.3, 13.5, 13.1, 13.4, 1),
             (13.4, 13.6, 13.2, 13.5, 1.2), (13.5, 14.0, 13.3, 13.9, 1.5)]
    hits = detect_patterns(_gen_series(rows))
    names = [h.name for h in hits]
    # 双底要求两个低点接近(10.0 vs 10.1)且突破颈线
    # 颈线=中间反弹高点≈13.0,收盘13.9>13.0 ✓
    assert any("双底" in n for n in names), f"应识别双底突破,实际 {names}"


# ============ 八大进场信号测试(2026-08-10 学习文) ============

def test_morning_star():
    """早晨之星: 长阴→星线→长阳(低位)。"""
    from src.core.kline_pattern import detect_patterns
    bars = make_bars([
        (10, 10.2, 9.9, 10.1, 1), (10.1, 10.3, 10.0, 10.2, 1), (10.2, 10.4, 10.1, 10.3, 1),
        (10.0, 10.1, 8.0, 8.1, 2),   # 长阴
        (8.1, 8.2, 8.0, 8.1, 0.5),   # 星线
        (8.2, 10.5, 8.1, 10.4, 2),   # 长阳
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert "早晨之星" in names, f"应识别早晨之星,实际 {names}"


def test_double_hammer_bottom():
    """大锤和小锤: 底部连续两根长下影。"""
    from src.core.kline_pattern import detect_patterns
    bars = make_bars([
        (10, 10.2, 9.9, 10.1, 1), (10.1, 10.3, 10.0, 10.2, 1), (10.2, 10.4, 10.1, 10.3, 1),
        (10.3, 10.5, 8.0, 10.4, 1.5),  # 长下影1
        (10.4, 10.6, 8.1, 10.5, 1.5),  # 长下影2(低点接近)
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert any(n in ("大锤和小锤", "双针探底", "金针探底") for n in names), f"应识别底部锤子类形态,实际 {names}"


def test_bullish_engulfing_small_yin():
    """大阳包小阴: 大阳吞没前日小阴。"""
    from src.core.kline_pattern import detect_patterns
    bars = make_bars([
        (10, 10.2, 9.9, 10.1, 1), (10.1, 10.3, 10.0, 10.2, 1),
        (10.0, 10.1, 9.6, 9.7, 1),  # 小阴
        (9.4, 10.8, 9.3, 10.7, 2),  # 大阳包住
    ])
    hits = detect_patterns(bars)
    names = [h.name for h in hits]
    assert "大阳包小阴" in names, f"应识别大阳包小阴,实际 {names}"


# ============ TA-Lib 标准形态测试(2026-08-10 接入) ============

def test_talib_detect_returns_list():
    """TA-Lib 识别返回列表(字段齐全)。"""
    from src.collectors.kline_collector import _detect_talib_patterns, KlineData
    from datetime import datetime, timedelta
    import random
    random.seed(42)
    bars = []
    base = datetime(2026, 7, 1)
    price = 10.0
    for i in range(60):
        price += random.uniform(-0.3, 0.35)
        o, c = price, price + random.uniform(-0.2, 0.25)
        bars.append(KlineData(
            date=(base + timedelta(days=i)).strftime('%Y-%m-%d'),
            open=o, high=max(o, c) + 0.1, low=min(o, c) - 0.1,
            close=c, volume=10000,
        ))
    result = _detect_talib_patterns(bars)
    assert isinstance(result, list)
    for p in result:
        assert p["cn_name"] and p["signal"] in ("看涨", "看跌") and p["strength"] > 0
        assert p["source"] == "talib"
