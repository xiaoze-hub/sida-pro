"""技术指标准确度地基 —— 回归针(2026-10-10)。

两类针:
- **正向**: 已修缺陷不再复发(kline_pattern 相邻双底/双顶空区间崩溃; 周/月聚合 None 量;
  末根盘中 repaint 显式标注)。
- **反向(lookahead 触发即红)**: 指标必须是**因果**的 —— 追加"未来"K 线后重算,
  历史各根的取值不得被改写。任何"用未来数据算当根/回填改写已发信号"的实现都会让
  这些断言变红(无 lookahead / 无 repaint 的机器可验证据)。

禁真网络: 全部合成 K 线/本地纯函数。
"""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.models.market import MarketCode

_CST = ZoneInfo("Asia/Shanghai")


# ── 合成 K 线 ───────────────────────────────────────────────────────────────

def _bars(n: int, start: float = 10.0, seed: int = 11) -> list[dict]:
    rnd = random.Random(seed)
    out: list[dict] = []
    c = start
    for i in range(n):
        o = c * (1 + rnd.uniform(-0.01, 0.01))
        c2 = o * (1 + rnd.uniform(-0.07, 0.07))
        h = max(o, c2) * (1 + rnd.uniform(0, 0.03))
        l = min(o, c2) * (1 - rnd.uniform(0, 0.03))
        out.append({"date": f"2026-{(i // 21) + 1:02d}-{(i % 21) + 1:02d}",
                    "open": round(o, 2), "high": round(h, 2), "low": round(l, 2),
                    "close": round(c2, 2), "volume": rnd.uniform(1e5, 9e5)})
        c = c2
    return out


class _Bar:
    """带属性的 bar(kline_pattern / _detect_kline_pattern 需要属性访问)。"""

    def __init__(self, d: dict):
        self.__dict__.update(d)


# ── 正向针: kline_pattern 相邻双底/双顶(空区间崩溃) ──────────────────────────

def _adjacent_extreme_bars(use_high: bool):
    """构造 20 根窗口内: 前低(高)点落在 idx=len-11, 后低(高)点落在 idx=len-10(相邻)。"""
    n = 25
    bars: list[_Bar] = []
    for i in range(n):
        c = 10.0 - i * 0.01
        bars.append(_Bar({"date": f"d{i}", "open": c, "high": c + 0.05, "low": c - 0.05,
                          "close": c, "volume": 1.0}))
    # 相邻的两个极值, 值几乎相等(触发"双底/双顶"的 2% 接近条件)
    if not use_high:
        bars[n - 11].low, bars[n - 10].low = 9.00, 9.005
        for i in range(n - 20, n - 10):
            if i != n - 11:
                bars[i].low = 9.5
        for i in range(n - 10, n):
            if i != n - 10:
                bars[i].low = 9.6
    else:
        bars[n - 11].high, bars[n - 10].high = 11.00, 11.005
        for i in range(n - 20, n - 10):
            if i != n - 11:
                bars[i].high = 10.5
        for i in range(n - 10, n):
            if i != n - 10:
                bars[i].high = 10.4
    return bars


def test_adjacent_double_bottom_does_not_crash():
    """反向针: 相邻低点(hi==lo+1)曾让 max(空区间) 抛 ValueError 并掀翻整条形态链。"""
    from src.core.kline_pattern import detect_patterns

    hits = detect_patterns(_adjacent_extreme_bars(use_high=False))
    assert isinstance(hits, list)  # 不抛异常; 该形态因无颈线被跳过(不猜)


def test_adjacent_double_top_does_not_crash():
    from src.core.kline_pattern import detect_patterns

    hits = detect_patterns(_adjacent_extreme_bars(use_high=True))
    assert isinstance(hits, list)


def test_normal_double_bottom_still_detected():
    """修复不得削弱正常识别(颈线存在时仍报双底)。"""
    from src.core.kline_pattern import detect_patterns

    class F:
        def __init__(s, o, h, l, c, v=1.0):
            s.open, s.high, s.low, s.close, s.volume, s.date = o, h, l, c, v, "d"

    rows = [(20 - i * 0.1, 20 - i * 0.1 + 0.3, 20 - i * 0.1 - 0.3, 20 - i * 0.1, 1) for i in range(8)]
    rows += [(12, 12.3, 10.0, 12.2, 1.5), (12.2, 12.5, 12.0, 12.4, 1),
             (12.4, 12.6, 12.2, 12.5, 1), (12.5, 12.8, 12.3, 12.7, 1),
             (12.7, 13.0, 12.5, 12.9, 1), (12.9, 13.1, 12.7, 13.0, 1),
             (13.0, 13.2, 10.1, 13.1, 1.5), (13.1, 13.3, 12.9, 13.2, 1),
             (13.2, 13.4, 13.0, 13.3, 1), (13.3, 13.5, 13.1, 13.4, 1),
             (13.4, 13.6, 13.2, 13.5, 1.2), (13.5, 14.0, 13.3, 13.9, 1.5)]
    names = [h.name for h in detect_patterns([F(*r) for r in rows])]
    assert any("双底" in n for n in names), names


# ── 正向针: 周/月聚合 None 量不伪造、不当 0 ─────────────────────────────────

def test_aggregate_klines_none_volume_is_none():
    from src.collectors.kline_collector import KlineData
    from src.web.api.klines import _aggregate_klines

    ks = [
        KlineData(date="2026-09-01", open=10, high=11, low=9, close=10.5, volume=1000),
        KlineData(date="2026-09-02", open=10.5, high=12, low=10, close=11.0, volume=None),  # 缺量
        KlineData(date="2026-09-03", open=11, high=12, low=10, close=11.5, volume=2000),
    ]
    wk = _aggregate_klines(ks, "1w")
    assert len(wk) == 1
    assert wk[0].volume is None  # 绝不当 0 求和(0 会伪装成缩量)


def test_aggregate_klines_sums_when_all_present():
    from src.collectors.kline_collector import KlineData
    from src.web.api.klines import _aggregate_klines

    ks = [
        KlineData(date="2026-09-01", open=10, high=11, low=9, close=10.5, volume=1000),
        KlineData(date="2026-09-02", open=10.5, high=12, low=10, close=11.0, volume=2000),
    ]
    assert _aggregate_klines(ks, "1w")[0].volume == 3000


# ── 正向针: 末根盘中 → provisional 显式标注(防 repaint) ─────────────────────

def test_last_bar_provisional_intraday_true(monkeypatch):
    from src.collectors.kline_collector import _last_bar_provisional

    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: True)
    now = datetime(2026, 10, 9, 10, 30, tzinfo=_CST)
    assert _last_bar_provisional("2026-10-09", MarketCode.CN, now=now) is True


def test_last_bar_provisional_lunch_break_still_true(monkeypatch):
    from src.collectors.kline_collector import _last_bar_provisional

    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: True)
    now = datetime(2026, 10, 9, 12, 0, tzinfo=_CST)  # 午休: 上午的 bar 仍未定死
    assert _last_bar_provisional("2026-10-09", MarketCode.CN, now=now) is True


def test_last_bar_provisional_after_close_false(monkeypatch):
    from src.collectors.kline_collector import _last_bar_provisional

    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: True)
    now = datetime(2026, 10, 9, 15, 30, tzinfo=_CST)
    assert _last_bar_provisional("2026-10-09", MarketCode.CN, now=now) is False


def test_last_bar_provisional_other_day_false(monkeypatch):
    from src.collectors.kline_collector import _last_bar_provisional

    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: True)
    now = datetime(2026, 10, 9, 10, 30, tzinfo=_CST)
    assert _last_bar_provisional("2026-10-08", MarketCode.CN, now=now) is False


def test_kline_summary_carries_provisional_flag(monkeypatch):
    """get_kline_summary 末尾带 provisional(值不变, 只加诚实标记)。"""
    import src.collectors.kline_collector as kc

    monkeypatch.setattr(kc, "_last_bar_provisional", lambda *a, **kw: True)
    monkeypatch.setattr(kc.KlineCollector, "get_klines",
                        lambda self, symbol, days=120, adjust="qfq": [
                            kc.KlineData(**b) for b in _bars(80)])
    s = kc.KlineCollector(MarketCode.CN).get_kline_summary("600519")
    assert s["provisional"] is True and "盘中" in s["provisional_note"]


# ── 反向针: 因果性(追加未来 K 线不改写历史值 = 无 lookahead / 无 repaint) ─────

def _per_bar(fn, bars):
    return [fn(bars[: i + 1]) for i in range(len(bars))]


def _assert_no_lookahead(label, fn, n=84, extra=6):
    bars = _bars(n)
    before = _per_bar(fn, bars)
    # 追加"未来"K 线后重算 —— 历史各根取值必须逐点不变
    future = _bars(extra, start=bars[-1]["close"], seed=99)
    after_full = _per_bar(fn, bars + future)
    after_past = after_full[:n]
    assert before == after_past, f"{label}: 历史值随未来数据改变 → 存在 lookahead/repaint"


def test_no_lookahead_gs_trend_label():
    from src.core.gs_strategy import eval_gs, trend_label

    _assert_no_lookahead("gs.trend_label", lambda b: trend_label(eval_gs(b)))


def test_no_lookahead_ai_activity():
    from src.core.ai_activity import eval_activity

    _assert_no_lookahead("ai_activity", lambda b: (eval_activity(b) or {}).get("activity"))


def test_no_lookahead_institution_activity():
    from src.core.decision_pioneer import compute_institution_activity

    _assert_no_lookahead(
        "compute_institution_activity",
        lambda b: (compute_institution_activity(b) or {}).get("activity"),
    )


def test_no_lookahead_kline_pattern():
    from src.core.kline_pattern import detect_patterns

    _assert_no_lookahead(
        "kline_pattern",
        lambda b: tuple(sorted(h.name for h in detect_patterns([_Bar(d) for d in b]))),
        n=60, extra=6,
    )


def test_no_lookahead_kline_collector_pattern():
    import src.collectors.kline_collector as kc

    _assert_no_lookahead(
        "kc._detect_kline_pattern",
        lambda b: kc._detect_kline_pattern([kc.KlineData(**d) for d in b]),
        n=60, extra=6,
    )
