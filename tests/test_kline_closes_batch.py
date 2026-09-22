"""批量收盘(给列表行 sparkline 用)的口径钉子(2026-09-22)。

三条硬口径(改了就是 bug):
1. **只读 PG, 不联网** —— 列表页几十行, 每行一次联网抓取会把页面拖成几十秒; 库里没有的就如实进
   `missing`(前端该行不画 sparkline);
2. **绝不补 0 / 绝不编造平线** —— 缺数据只能"没有", 不能是 0 或一条假平线;
3. **上限保护 DB** —— 一次最多 60 只, 超了直接 400(而不是把库压死)。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.web.api import klines as K


class _K:
    def __init__(self, close):
        self.close = close


def _patch_pg(monkeypatch, mapping: dict[str, list[float] | None]):
    """把 PG 直读替换成指定数据; None 表示"库里没有"。"""
    def fake(symbol, market_code, days):
        closes = mapping.get(symbol)
        if closes is None:
            return None, None
        return [_K(c) for c in closes], "2026-09-19"

    monkeypatch.setattr(K, "_pg_klines", fake)
    # 缓存不可用不影响断言(避免跨用例污染)
    monkeypatch.setattr(K, "_CLOSES_TTL", 0)


def test_returns_closes_and_missing(monkeypatch):
    _patch_pg(monkeypatch, {"002361": [10.1, 10.5, 10.9], "600519": None})
    out = K.get_closes_batch(symbols="002361,600519", days=20)
    assert [i["symbol"] for i in out["items"]] == ["002361"]
    assert out["items"][0]["closes"] == [10.1, 10.5, 10.9]
    assert out["items"][0]["asof"] == "2026-09-19"
    # 关键: 没数据的标的**进 missing**, 不是补 0
    assert out["missing"] == ["600519"]
    assert all(0 not in i["closes"] or True for i in out["items"])


def test_missing_is_not_zero_filled(monkeypatch):
    """缺数据 ≠ 0: 宁可"没有", 不许造一条平线出来。"""
    _patch_pg(monkeypatch, {"002361": None})
    out = K.get_closes_batch(symbols="002361")
    assert out["items"] == []
    assert out["missing"] == ["002361"]


def test_single_point_is_treated_as_missing(monkeypatch):
    """只有 1 个点画不出形状 ⇒ 如实算"没有"(不硬画一个点)。"""
    _patch_pg(monkeypatch, {"002361": [10.1]})
    out = K.get_closes_batch(symbols="002361")
    assert out["items"] == []
    assert out["missing"] == ["002361"]


def test_pg_only_never_hits_network(monkeypatch):
    """**只读 PG**: 库里有缺口时也不许触发联网抓取(列表页不能被上游拖死)。"""
    _patch_pg(monkeypatch, {"002361": None})

    def boom(*_a, **_k):
        raise AssertionError("列表页的 sparkline 数据不该触发联网抓取")

    from src.collectors import kline_collector

    monkeypatch.setattr(kline_collector.KlineCollector, "get_klines", boom)
    out = K.get_closes_batch(symbols="002361")
    assert out["missing"] == ["002361"]


def test_symbol_cap_protects_db(monkeypatch):
    _patch_pg(monkeypatch, {})
    with pytest.raises(HTTPException) as ei:
        K.get_closes_batch(symbols=",".join(f"{i:06d}" for i in range(K.MAX_CLOSES_SYMBOLS + 1)))
    assert ei.value.status_code == 400
    assert str(K.MAX_CLOSES_SYMBOLS) in str(ei.value.detail)


def test_empty_symbols_rejected():
    with pytest.raises(HTTPException) as ei:
        K.get_closes_batch(symbols="  , ")
    assert ei.value.status_code == 400


def test_days_is_clamped(monkeypatch):
    """天数夹取在 2..120(负数/超大值不许直达 SQL)。"""
    seen: list[int] = []

    def fake(symbol, market_code, days):
        seen.append(days)
        return [_K(1.0), _K(2.0)], "2026-09-19"

    monkeypatch.setattr(K, "_pg_klines", fake)
    monkeypatch.setattr(K, "_CLOSES_TTL", 0)
    K.get_closes_batch(symbols="002361", days=-5)
    K.get_closes_batch(symbols="002361", days=9999)
    assert seen == [2, 120]
