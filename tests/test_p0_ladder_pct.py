"""P0(2026-10-18) 连板梯队个股涨跌幅 + 旧快照口径门禁。

审计依据: docs/research/SIDA_PRO全界面UI合理性与字段排版审计重构规划.md §3.1-F06 / §5.2 / §9-P0。
生产实测(修复前): `/theme-mood/ladder` 1,222 只个股 `pct` 全 null → 单页 ≈820 处 `--`。
"""
from __future__ import annotations

import pytest

import src.core.limit_strength_shadow as strength_shadow
import src.web.api.theme_mood as api


DATES = ["20260910", "20260911"]


def _fake_read_factory(events):
    def fake_read(sql: str, params):
        if "DISTINCT trade_date" in sql:
            return [{"trade_date": d} for d in reversed(DATES)]
        return [e for e in events if e["trade_date"] >= params["a"]]

    return fake_read


def _events():
    return [
        # 事件表自带 prev_close/close_price(精确: 10.0 → 11.0)
        {"trade_date": "20260910", "symbol": "C", "name": "CC", "is_sealed_close": True,
         "prev_close": 10.0, "close_price": 11.1},
        {"trade_date": "20260911", "symbol": "C", "name": "CC", "is_sealed_close": True,
         "prev_close": 10.0, "close_price": 11.0},
        # 事件表缺 prev_close/close_price → 回退 klines 上一交易日收盘
        {"trade_date": "20260911", "symbol": "D", "name": "DD", "is_sealed_close": True,
         "prev_close": None, "close_price": None},
        # 两处都没有 → pct None(不编 0)
        {"trade_date": "20260911", "symbol": "E", "name": "EE", "is_sealed_close": True,
         "prev_close": None, "close_price": None},
    ]


@pytest.fixture
def ladder(monkeypatch):
    monkeypatch.setattr(api, "_read", _fake_read_factory(_events()))
    monkeypatch.setattr(api, "_read_ohlc", lambda dates, symbols: {
        ("20260910", "D"): {"o": 4.9, "h": 5.1, "l": 4.8, "c": 5.0},
        ("20260911", "D"): {"o": 5.4, "h": 5.5, "l": 5.3, "c": 5.5},
    })
    monkeypatch.setattr(api, "_live_snapshot", lambda: None)
    monkeypatch.setattr(strength_shadow, "labels_map", lambda dates: {})
    return api._ladder_payload(20, "finalized")


def _stocks_by_day(payload):
    out = {}
    for day in payload["ladder"]:
        out[day["date"]] = {s["symbol"]: s for row in day["rows"] for s in row["stocks"]}
    return out


def test_ladder_stocks_carry_pct_from_event_prev_close(ladder):
    stocks = _stocks_by_day(ladder)["20260911"]
    assert stocks["C"]["pct"] == pytest.approx(10.0, abs=0.01)
    # 历史日同样有值(不是只有最新日才补)
    assert _stocks_by_day(ladder)["20260910"]["C"]["pct"] == pytest.approx(11.0, abs=0.01)


def test_ladder_pct_falls_back_to_kline_prev_close(ladder):
    stocks = _stocks_by_day(ladder)["20260911"]
    assert stocks["D"]["pct"] == pytest.approx(10.0, abs=0.01)
    assert stocks["D"]["candle"]["c"] == 5.5          # 蜡烛仍在(C 侧行为不变)


def test_ladder_pct_is_none_when_no_source_exists(ladder):
    stocks = _stocks_by_day(ladder)["20260911"]
    assert "pct" in stocks["E"]                        # 键必须在(前端据此判口径)
    assert stocks["E"]["pct"] is None


def test_every_stock_has_pct_key(ladder):
    """验收判据: 1,222 只 pct 不再是"字段不存在", 而是有值或显式 None。"""
    for stocks in _stocks_by_day(ladder).values():
        assert all("pct" in s for s in stocks.values())


# ── 旧快照门禁 ───────────────────────────────────────────────────────


def test_snapshot_has_pct_detects_new_and_old_caliber():
    old = {"ladder": [{"rows": [{"stocks": [{"symbol": "C", "name": "CC", "candle": None}]}]}]}
    new = {"ladder": [{"rows": [{"stocks": [{"symbol": "C", "name": "CC", "candle": None, "pct": 10.0}]}]}]}
    assert api._snapshot_has_pct(old) is False
    assert api._snapshot_has_pct(new) is True
    assert api._snapshot_has_pct({"ladder": []}) is False


def test_old_snapshot_is_not_served(monkeypatch):
    """旧口径快照(无 pct)必须拒服 → 现算 + 后台补写, 否则修复上线后页面仍全空。"""
    old_payload = {
        "dates": DATES,
        "ladder": [{"date": DATES[-1], "rows": [
            {"boards": 1, "codes": ["C"], "names": ["CC"],
             "stocks": [{"symbol": "C", "name": "CC", "candle": None}]},
        ]}],
    }
    monkeypatch.setattr(api, "_read_snapshot_row", lambda w, m: (old_payload, DATES[-1]))
    assert api._snapshot_base(20, "finalized") is None

    new_payload = {
        "dates": DATES,
        "ladder": [{"date": DATES[-1], "rows": [
            {"boards": 1, "codes": ["C"], "names": ["CC"],
             "stocks": [{"symbol": "C", "name": "CC", "candle": None, "pct": 10.0}]},
        ]}],
    }
    monkeypatch.setattr(api, "_read_snapshot_row", lambda w, m: (new_payload, DATES[-1]))
    monkeypatch.setattr(api, "_latest_event_date", lambda: DATES[-1])
    assert api._snapshot_base(20, "finalized") is new_payload
