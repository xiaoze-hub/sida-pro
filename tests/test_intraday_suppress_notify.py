"""静默触发(suppress_notify)不得真发外部告警(2026-10-30 OCR error 回归)。

缺陷: 防守线击穿的外发发生在 `_format_human_readable_content`(build 阶段),
而 `suppress_notify` 只在 `analyze()` 之后才短路 —— 一次性"仅生成建议"的触发
(stocks.py: stock_id<=0 → suppress_notify=True)照样把「🚨 防守线击穿」发到微信/钉钉。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agents import intraday_monitor as IM


def _stock():
    return SimpleNamespace(
        name="贵州茅台",
        symbol="600519",
        current_price=9.0,
        change_pct=-2.0,
        market_code="CN",
    )


@pytest.fixture()
def quiet(monkeypatch):
    """把与本用例无关的外部取数全部静音, 只留防守线→外发这条链。"""
    monkeypatch.setattr(IM, "_main_intent_summary", lambda symbol: "")
    monkeypatch.setattr(
        IM,
        "load_defense_lines",
        lambda symbol, user_id=None: [{"defense_price": 10.0, "cost_price": 11.0}],
    )
    monkeypatch.setattr(
        IM,
        "evaluate_defense_breach",
        lambda **kw: {"breached": True, "current_price": 9.0, "defense_price": 10.0, "loss_pct": -18.2},
    )
    for name in ("decision_pioneer_text", "sentiment_context", "news_context"):
        if hasattr(IM, name):
            monkeypatch.setattr(IM, name, lambda *a, **k: "")
    return monkeypatch


def _data(suppress: bool):
    return {
        "user_id": "1",
        "notify_channel_ids": [7],
        "suppress_notify": suppress,
        "kline_data": [],
    }


def test_suppressed_trigger_writes_in_app_only(quiet):
    calls = []
    import src.core.notify_center as nc

    quiet.setattr(nc, "push_notification", lambda *a, **k: calls.append(k) or 1)

    IM.IntradayMonitorAgent._format_human_readable_content(
        IM.IntradayMonitorAgent.__new__(IM.IntradayMonitorAgent),
        _stock(),
        {"action_label": "减仓", "signal": "破位", "reason": "跌破防线"},
        "{}",
        _data(suppress=True),
    )

    assert calls, "站内记录仍应写入(只是不外发)"
    assert calls[0]["also_push"] is False, "静默触发不得外发"
    assert tuple(calls[0]["channel_ids"]) == (), "静默触发应带空选择集(fail-closed)"


def test_normal_trigger_still_pushes(quiet):
    calls = []
    import src.core.notify_center as nc

    quiet.setattr(nc, "push_notification", lambda *a, **k: calls.append(k) or 1)

    IM.IntradayMonitorAgent._format_human_readable_content(
        IM.IntradayMonitorAgent.__new__(IM.IntradayMonitorAgent),
        _stock(),
        {"action_label": "减仓", "signal": "破位", "reason": "跌破防线"},
        "{}",
        _data(suppress=False),
    )

    assert calls, "正常触发应保留告警"
    assert calls[0]["also_push"] is True
    assert tuple(calls[0]["channel_ids"]) == (7,)
