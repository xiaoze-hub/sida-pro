"""题材情绪两个重接口的两级缓存(2026-09-26)。

背景: /theme-mood/ladder 实测 6.5s、/board 2.4~5.8s, 每进一次页面都重算。
本文件钉住四件事:
  1. 第二次调用命中缓存 -> 不再重算(producer 只被调一次)
  2. `as_of` 每次新鲜生成, 不随缓存变旧(否则界面"更新 HH:MM:SS"会骗人)
  3. **空/降级结果不写缓存** —— 抽风结果不能被钉住 30 分钟
  4. TTL 按交易时段区分(盘中短 / 收盘后长)
"""
from __future__ import annotations

import pytest

from src.web.api import theme_mood as tm
from src.web.cache.biz_cache import biz_cache


@pytest.fixture(autouse=True)
def _clean_keys():
    for w in (20, 21):
        biz_cache.delete(f"biz:tm:ladder:{w}:auto", f"biz:tm:board:{w}:15",
                         f"biz:tm:ladder:{w}:finalized")
    yield
    for w in (20, 21):
        biz_cache.delete(f"biz:tm:ladder:{w}:auto", f"biz:tm:board:{w}:15",
                         f"biz:tm:ladder:{w}:finalized")


def test_ladder_second_call_hits_cache(monkeypatch):
    calls = {"n": 0}

    def fake_payload(window, mode):
        calls["n"] += 1
        return {"dates": ["20260924"], "ladder": [{"date": "20260924", "rows": []}],
                "mode": "finalized", "live_day": None, "stale": False}

    monkeypatch.setattr(tm, "_ladder_payload", fake_payload)
    monkeypatch.setattr(tm, "_cache_ttl", lambda: 1800)

    r1 = tm.get_ladder(window=20, mode="auto")
    r2 = tm.get_ladder(window=20, mode="auto")

    assert calls["n"] == 1, "第二次应命中缓存, 不得重算"
    assert r1["cached"] is False and r2["cached"] is True
    assert r1["cache_ttl_s"] == 1800
    assert r1["as_of"] != r2["as_of"], "as_of 必须每次新鲜(否则'更新时刻'是假的)"
    assert r2["ladder"] == r1["ladder"]


def test_ladder_empty_payload_not_cached(monkeypatch):
    """降级/空结果不得写缓存 —— 否则一次抽风要等 TTL 过期才能恢复。"""
    calls = {"n": 0}

    def empty_payload(window, mode):
        calls["n"] += 1
        return {"dates": [], "ladder": [], "mode": "finalized", "as_of": None}

    monkeypatch.setattr(tm, "_ladder_payload", empty_payload)
    monkeypatch.setattr(tm, "_cache_ttl", lambda: 1800)

    tm.get_ladder(window=20, mode="auto")
    tm.get_ladder(window=20, mode="auto")
    assert calls["n"] == 2, "空结果每次都该重算(不缓存)"


def test_board_second_call_hits_cache(monkeypatch):
    calls = {"n": 0}

    def fake_board(window, top):
        calls["n"] += 1
        return {"dates": ["20260924"], "items": [{"block_code": "BK1"}],
                "market": {}, "rotation": [], "rotation_top_k": 3}

    monkeypatch.setattr(tm, "_board_data", fake_board)
    monkeypatch.setattr(tm, "_latest_date", lambda: "20260924")
    monkeypatch.setattr(tm, "_cache_ttl", lambda: 30)

    r1 = tm.get_board(window=20, top=15)
    r2 = tm.get_board(window=20, top=15)
    assert calls["n"] == 1
    assert r1["cached"] is False and r2["cached"] is True
    assert r2["trade_date"] == "20260924", "数据日必须新鲜读, 不能随缓存滞后"


def test_board_empty_not_cached(monkeypatch):
    calls = {"n": 0}

    def empty_board(window, top):
        calls["n"] += 1
        return {"dates": [], "items": [], "market": {}, "rotation": [], "rotation_top_k": 3}

    monkeypatch.setattr(tm, "_board_data", empty_board)
    monkeypatch.setattr(tm, "_latest_date", lambda: "20260924")
    monkeypatch.setattr(tm, "_cache_ttl", lambda: 1800)
    tm.get_board(window=20, top=15)
    tm.get_board(window=20, top=15)
    assert calls["n"] == 2


def test_ttl_intraday_short_closed_long(monkeypatch):
    import datetime as _dt

    from src.core import limit_ladder_live as lll

    class _Monkey:
        """按给定时刻返回 _is_intraday 的判定。"""

        def __init__(self, v):
            self.v = v

        def __call__(self, now=None):
            return self.v

    monkeypatch.setattr(lll, "_is_intraday", _Monkey(True))
    assert tm._cache_ttl() == tm._TTL_INTRADAY

    monkeypatch.setattr(lll, "_is_intraday", _Monkey(False))
    ttl = tm._cache_ttl()
    assert ttl in (tm._TTL_SETTLING, tm._TTL_CLOSED), "收盘后应为 5min(定型中)或 30min"


def test_disabled_mode_rejected():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        tm.get_ladder(window=20, mode="nonsense")
