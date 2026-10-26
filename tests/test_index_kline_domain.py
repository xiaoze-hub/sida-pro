"""指数历史K线：参谋此前答「平台没有指数历史K线」——实测库里有 1201 行，是取数路径缺失。

生产实证（2026-10-25）：
  index_daily 000001: 2021-11-08 ~ 2026-10-22，1201 行，OHLC 完整
  2026-05-06 → 10-22：4160.17 → 3658.03，**-12.07%**（最高 4242.57）
  → 用户问的「7月到10月下跌」是真的；参谋答「没有趋势下跌」是错的（它把 000001 当成了平安银行）

本文件钉三件事：
1. 000001 走指数路径（index_daily），不进 klines（那里 000001 是平安银行）；
2. 个股代码仍走原路径，不被指数逻辑误伤；
3. 区间统计真的算出来（不能只说"有数据"却给不出涨跌幅）。
"""

from __future__ import annotations

import pytest

from src.agents.chat import tools_catalog as tc


def test_指数代码走_index_daily_而不是_klines(monkeypatch):
    calls: list[str] = []

    def fake_read(db, key, params, missing, *, label=""):
        calls.append(key)
        if key == "index_daily_bars":
            return [
                {"trade_date": "2026-10-22", "open": 3714.4, "high": 3714.4, "low": 3649.8,
                 "close": 3658.0, "volume": 4.3e10, "amount": None, "name": "上证指数"},
                {"trade_date": "2026-07-01", "open": 4135.0, "high": 4166.0, "low": 4129.0,
                 "close": 4160.1, "volume": 7.0e10, "amount": None, "name": "上证指数"},
            ]
        return []

    monkeypatch.setattr(tc, "_read_safe", fake_read)
    p = tc._kline_payload(None, "000001", "CN", 120)
    assert calls == ["index_daily_bars"], f"000001 应只查 index_daily，实际查了 {calls}"
    assert p["kind"] == "index"
    assert p["name"] == "上证指数"
    assert len(p["bars"]) == 2
    # 必须按日期升序，否则区间涨跌会算反
    assert p["bars"][0]["date"] == "2026-07-01"
    assert "index_daily" in p["bars"][-1]["source"]


def test_个股仍走_klines_不被指数逻辑误伤(monkeypatch):
    calls: list[str] = []

    def fake_read(db, key, params, missing, *, label=""):
        calls.append(key)
        if key == "kline_daily_bars":
            return [{"ts": "2026-10-22 00:00:00", "open": 10, "high": 11, "low": 9,
                     "close": 10.5, "volume": 1000, "source": "tq"}]
        return []

    monkeypatch.setattr(tc, "_read_safe", fake_read)
    p = tc._kline_payload(None, "600111", "CN", 60)
    assert calls == ["kline_daily_bars"]
    assert "kind" not in p
    assert p["bars"][0]["date"] == "2026-10-22"


def test_指数无数据时如实说缺_不补0(monkeypatch):
    monkeypatch.setattr(tc, "_read_safe", lambda *a, **k: [])
    p = tc._kline_payload(None, "000300", "CN", 60)
    assert p["bars"] == []
    assert any("index_daily" in m for m in p["missing"])


def test_指数代码表覆盖首页那几个():
    from src.agents.chat.tools_catalog import _INDEX_CODES

    # 与 src/web/api/market.py MARKET_INDICES / quotes.py is_index 保持同源
    for code in ("000001", "399001", "399006", "000300"):
        assert code in _INDEX_CODES, f"{code} 缺指数映射"


def test_days_ago_iso_是自然日():
    from datetime import date, datetime

    s = tc._days_ago_iso(30)
    d = datetime.strptime(s, "%Y-%m-%d").date()
    assert (date.today() - d).days >= 30  # 取窗口要覆盖 >= days 个自然日


def test_成交量单位不再把股当手():
    """用户红线: 库内 volume 一律是股。原实现 `vol/1e4 万手` 让上证 4.37e10 股
    显示成「4376920万手」(实际 437.7 亿股), 量级错 6 个数量级。"""
    from src.agents.chat.tools_catalog import _vol_text

    assert _vol_text(4.37e10).endswith("亿股")
    assert _vol_text(4.3769e10) == "437.69亿股"
    assert _vol_text(5.2e8) == "5.20亿股"
    assert _vol_text(3.5e4) == "4万股"      # 万元档
    assert _vol_text(800) == "800股"
    assert "手" not in _vol_text(4.37e10)   # 指数没有"手"的概念
