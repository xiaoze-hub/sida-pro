"""盘中监控的"触发用户"链路回归(2026-10-29 三审 R7, 先证明再修)。

**证明缺陷存在**(修前):

- `StockData`(src/models/market.py)是纯行情 dataclass, **没有 id / user 字段**;
- `IntradayMonitor.collect()` 的返回 dict 也从来没有 `user_id`;
- 于是 `build_prompt` 里 `_uid = data.get("user_id") or pos_info.get("user_id")` 恒为 None
  (`data` 里既没 `user_id` 也没 `position` 键), 后面的
  `load_defense_lines(symbol, user_id=None)` 只查共享桶 → **用户自己设的防守线永不触发**,
  `push_notification(user_id=None)` 还把风险告警推成"无归属"。

**修复口径**: 不按 symbol 反查 Stock/Position 归属(同 symbol 多租户都有行, 反查就是
"猜归属"), 而是在 `collect()` 里把 `context.user.id` 作为**标量**放进 data, 下游只认它。

本文件同时锁住: 链条断了(没有 context.user)时**不猜** —— uid 为 None, 只走共享桶。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.agents.intraday_monitor import IntradayMonitorAgent
from src.models.market import MarketCode, StockData


def _stock_data(symbol: str = "600111") -> StockData:
    return StockData(
        symbol=symbol,
        name="测试",
        market=MarketCode.CN,
        current_price=9.5,
        change_pct=-1.2,
        change_amount=-0.1,
        volume=1000.0,
        turnover=1e6,
        open_price=9.6,
        high_price=9.7,
        low_price=9.4,
        prev_close=9.6,
    )


class _Watchlist(list):
    pass


@pytest.fixture()
def monitor():
    return IntradayMonitorAgent()


def _fake_context(uid: str | None):
    from src.agents.base import PortfolioInfo

    return SimpleNamespace(
        user=(SimpleNamespace(id=uid) if uid else None),
        watchlist=[SimpleNamespace(symbol="600111", name="测试", market=MarketCode.CN)],
        # 用真实 PortfolioInfo(空组合)而不是鸭子类型替身: prompt 里会读它的一堆字段。
        portfolio=PortfolioInfo(accounts=[]),
    )


def test_collect_carries_trigger_user_id(monitor, monkeypatch):
    """collect() 必须把 context.user.id 作为标量带进 data(修前完全没有这个键)。"""
    import src.agents.intraday_monitor as im

    async def fake_build_for_symbols(self, **_k):
        return {}

    monkeypatch.setattr(im.SignalPackBuilder, "build_for_symbols", fake_build_for_symbols)
    monkeypatch.setattr(im, "is_market_trading", lambda m, dt=None: True)
    monkeypatch.setattr(im, "get_latest_analysis", lambda **_k: None)
    monkeypatch.setattr(im, "get_analysis", lambda **_k: None)

    class _CB:
        async def build_symbol_contexts(self, **_k):
            return {"symbols": {}, "quality_overview": {}}

    monkeypatch.setattr(im, "ContextBuilder", _CB)

    # 市场情绪采集器会连外网/网关(测试里必须替身, 否则单条用例几十秒)
    import src.collectors.market_sentiment_collector as msc

    class _Senti:
        def get_sentiment_summary(self):
            return {}

        def get_sector_rotation(self, top_n=12):
            return []

        def get_index_snapshot(self):
            return []

    monkeypatch.setattr(msc, "MarketSentimentCollector", _Senti)

    data = asyncio.run(monitor.collect(_fake_context("uid-777")))
    assert data.get("user_id") == "uid-777", "触发用户必须显式沿链条下传"


def test_collect_without_session_user_has_no_guess(monitor, monkeypatch):
    """没有会话用户时不许猜 uid(None = 共享桶), 也不能从 symbol 反查归属。"""
    import src.agents.intraday_monitor as im

    async def fake_build_for_symbols(self, **_k):
        return {}

    monkeypatch.setattr(im.SignalPackBuilder, "build_for_symbols", fake_build_for_symbols)
    monkeypatch.setattr(im, "is_market_trading", lambda m, dt=None: True)
    monkeypatch.setattr(im, "get_latest_analysis", lambda **_k: None)
    monkeypatch.setattr(im, "get_analysis", lambda **_k: None)

    class _CB:
        async def build_symbol_contexts(self, **_k):
            return {"symbols": {}, "quality_overview": {}}

    monkeypatch.setattr(im, "ContextBuilder", _CB)

    # 市场情绪采集器会连外网/网关(测试里必须替身, 否则单条用例几十秒)
    import src.collectors.market_sentiment_collector as msc

    class _Senti:
        def get_sentiment_summary(self):
            return {}

        def get_sector_rotation(self, top_n=12):
            return []

        def get_index_snapshot(self):
            return []

    monkeypatch.setattr(msc, "MarketSentimentCollector", _Senti)

    data = asyncio.run(monitor.collect(_fake_context(None)))
    assert data.get("user_id") is None


def _prompt_data(user_id):
    """防守线告警只读 data 里的 kline_data + user_id; 其余给空, 不联网。"""
    return {
        "user_id": user_id,
        # 故意塞一个"别人的" position 块: 即使有它也不能改归属(不猜)
        "position": {"user_id": "someone-else"},
        "kline_data": [],
    }


def test_format_alert_uses_chain_user_for_defense_lines(monitor, monkeypatch):
    """防守线查询必须用链条上的 uid(修前恒 None → 只查共享桶, 私有防线永不报警)。"""
    import src.agents.intraday_monitor as im

    seen: list = []

    def fake_load_defense_lines(symbol, user_id=None):
        seen.append((symbol, user_id))
        return [{"defense_price": 10.0, "cost_price": 10.5, "user_id": user_id}]

    monkeypatch.setattr(im, "load_defense_lines", fake_load_defense_lines)
    monkeypatch.setattr(im, "push_notification", lambda *a, **k: 1, raising=False)

    out = monitor._format_human_readable_content(
        _stock_data(), {"action_label": "持有"}, "{}", data=_prompt_data("uid-777")
    )
    assert seen and seen[0][1] == "uid-777", seen
    assert "防守线" in out, out


def test_format_alert_never_guesses_owner_from_position_block(monitor, monkeypatch):
    """链条上没有 uid 时, 也不许拿 position 块里的 user_id 猜归属。"""
    import src.agents.intraday_monitor as im

    seen: list = []

    def fake_load_defense_lines(symbol, user_id=None):
        seen.append((symbol, user_id))
        return []

    monkeypatch.setattr(im, "load_defense_lines", fake_load_defense_lines)
    monitor._format_human_readable_content(
        _stock_data(), {"action_label": "持有"}, "{}", data=_prompt_data(None)
    )
    assert seen and seen[0][1] is None, seen
