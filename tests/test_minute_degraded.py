# -*- coding: utf-8 -*-
"""KI-042: 分时源故障 ≠ 真空态(非交易日/停牌)。

`GET /quotes/minute/{symbol}` 此前把腾讯源故障与"真无分时"共用 `points: []`,
前端文案「暂无分时数据(非交易日或停牌)」把源抖误归因为停牌。本批:
- `_tencent_minute` 返回三元组, points=None 表示源故障(带 note)
- 接口响应带 `degraded` + `note`; 真空态 degraded=false
"""
import asyncio
from unittest.mock import patch

from src.web.api import quotes as quotes_mod


def _get_minute(symbol: str = "600519", market: str = "CN") -> dict:
    return asyncio.run(quotes_mod.get_minute(symbol, market))


def test_minute_source_failure_is_degraded_not_empty_silence():
    """源故障 → degraded=true + note 原文, 不是静默空列表。"""
    quotes_mod._MINUTE_CACHE.clear()
    with patch.object(quotes_mod, "_tencent_minute",
                      return_value=(None, None, "分时源(腾讯)暂不可用")):
        data = _get_minute()
    assert data["points"] == []
    assert data["degraded"] is True
    assert data["note"] == "分时源(腾讯)暂不可用"
    quotes_mod._MINUTE_CACHE.clear()


def test_minute_true_empty_is_not_degraded():
    """真空态(接口成功但无行) → degraded=false, 无 note。"""
    quotes_mod._MINUTE_CACHE.clear()
    with patch.object(quotes_mod, "_tencent_minute", return_value=([], None, None)):
        data = _get_minute()
    assert data["points"] == []
    assert data["degraded"] is False
    assert data["note"] is None
    quotes_mod._MINUTE_CACHE.clear()


def test_minute_success_has_points_and_not_degraded():
    quotes_mod._MINUTE_CACHE.clear()
    pts = [{"t": "0930", "price": 10.0, "avg": 10.0, "volume": 1}]
    with patch.object(quotes_mod, "_tencent_minute", return_value=(pts, 9.9, None)):
        data = _get_minute()
    assert data["points"] == pts
    assert data["degraded"] is False
    quotes_mod._MINUTE_CACHE.clear()


def test_minute_cache_preserves_degraded_flag():
    """故障缓存命中时必须仍带 degraded/note(否则 TTL 内故障被伪装成真空态)。"""
    quotes_mod._MINUTE_CACHE.clear()
    with patch.object(quotes_mod, "_tencent_minute",
                      return_value=(None, None, "分时源(腾讯)暂不可用")):
        _get_minute()
        data = _get_minute()  # 第二次走缓存
    assert data["degraded"] is True
    assert data["note"] == "分时源(腾讯)暂不可用"
    quotes_mod._MINUTE_CACHE.clear()


def test_tencent_minute_exception_returns_none_with_note(monkeypatch):
    """网络异常 → points=None(故障), 不是 []."""
    def boom(req, timeout=None):
        raise TimeoutError("connect timeout")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    points, prev, note = quotes_mod._tencent_minute("600519", "CN")
    assert points is None
    assert note == "分时源(腾讯)暂不可用"
