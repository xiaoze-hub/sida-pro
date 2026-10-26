"""市场级排行工具（get_market_ranking）与腾讯排行 vendor 的回归钉。

背景：通达信问小达配额用尽（`MCP error -32603: Your usage quota has been reached`），
「今日主力净流入前10」「今日涨幅前10的概念板块」开始答不出来。腾讯排行接口免配额、
实时、个股+板块全覆盖，本轮接成 `get_market_ranking`。

这里的用例**全部 mock 掉网络**（`_fetch_page`），只钉契约与边界，不打真实接口：
CI 不该因为"今天没人交易"或"腾讯限流"变红。
"""

from __future__ import annotations

import asyncio

import pytest

from marketdata.vendors import tencent_rank as tr
from src.agents.chat.registry import CHAT_TOOL_REGISTRY, chat_tool_schemas


def _row(name, zdf="1.00", zljlr="10000", **kw):
    r = {"code": f"pt{name}", "name": name, "zdf": zdf, "zljlr": zljlr,
         "hsl": "2.0", "lb": "1.1", "zxj": "100.0", "ltsz": "500.0", "zsz": "600.0",
         "zgb": "10/20", "lzg": {"name": f"{name}龙头", "zdf": "9.90"}}
    r.update(kw)
    return r


# ── vendor：分页与排序 ──────────────────────────────────────────────


def test_服务端排序只拉够limit就停(monkeypatch):
    calls = []

    def fake(path, params, sort_type, offset):
        calls.append(offset)
        return [_row(f"A{offset+i}") for i in range(100)]

    monkeypatch.setattr(tr, "_fetch_page", fake)
    res = tr.fetch_board_rank("stock", "change", 5)
    assert res["available"] is True
    assert len(res["rows"]) == 5
    assert calls == [0], "只要 5 条却翻了第二页 = 浪费请求"


def test_分页不满一页为终止条件(monkeypatch):
    """越界返回空列表且不报错，所以翻页终止只能靠"这页不满 100 条"。

    用 main_inflow（服务端不支持排序 → 必须拉全量）来验分页，因为其它 metric
    只拉到够 limit 就停，验不出翻页边界。
    """
    calls = []
    pages = {0: [_row(f"B{i}", zljlr=str(1000 - i)) for i in range(100)],
             100: [_row("C1", zljlr="5000")]}

    def fake(path, params, sort_type, offset):
        calls.append(offset)
        return pages.get(offset, [])

    monkeypatch.setattr(tr, "_fetch_page", fake)
    res = tr.fetch_board_rank("concept", "main_inflow", 2)
    assert calls == [0, 100], f"短页即停，不该继续翻；实际 {calls}"
    assert [r["name"] for r in res["rows"]] == ["C1", "B0"], "全量拉完后本地按净流入排序"


def test_主力净流入走本地排序且拉全量(monkeypatch):
    pages = {
        0: [_row("小", zljlr="100"), _row("大", zljlr="900000")],
        100: [],
    }

    def fake(path, params, sort_type, offset):
        # 服务端不支持按 zljlr 排，所以永远用 PriceRatio 拉
        assert sort_type == "PriceRatio"
        return pages.get(offset, [])

    monkeypatch.setattr(tr, "_fetch_page", fake)
    res = tr.fetch_board_rank("concept", "main_inflow", 2)
    assert [r["name"] for r in res["rows"]] == ["大", "小"], "必须本地按主力净流入降序"
    assert res["metric"] == "main_inflow"


def test_坏数值不崩_null_透传(monkeypatch):
    monkeypatch.setattr(tr, "_fetch_page", lambda *a: [_row("X", zdf="--", zljlr="-")])
    res = tr.fetch_board_rank("stock", "change", 1)
    assert res["available"] is True
    assert res["rows"][0]["change_pct"] is None
    assert res["rows"][0]["main_inflow"] is None


def test_非法scope与metric如实报错(monkeypatch):
    monkeypatch.setattr(tr, "_fetch_page", lambda *a: [])
    r1 = tr.fetch_board_rank("etf", "change", 5)
    r2 = tr.fetch_board_rank("stock", "bogus", 5)
    assert r1["available"] is False and "scope" in r1["reason"]
    assert r2["available"] is False and "metric" in r2["reason"]


def test_网络异常不抛_返回不可用(monkeypatch):
    def boom(*a):
        raise TimeoutError("timed out")

    monkeypatch.setattr(tr, "_fetch_page", boom)
    res = tr.fetch_board_rank("stock", "change", 5)
    assert res["available"] is False
    assert "TimeoutError" in res["reason"]


def test_空列表不当成今天没有(monkeypatch):
    monkeypatch.setattr(tr, "_fetch_page", lambda *a: [])
    res = tr.fetch_board_rank("concept", "change", 5)
    assert res["available"] is False
    assert res["rows"] == []


def test_板块带涨跌家数与领涨股_个股不带(monkeypatch):
    monkeypatch.setattr(tr, "_fetch_page", lambda *a: [_row("半导体")])
    board = tr.fetch_board_rank("concept", "change", 1)["rows"][0]
    stock = tr.fetch_board_rank("stock", "change", 1)["rows"][0]
    assert board["up_down"] == "10/20"
    assert board["leader"]["name"] == "半导体龙头"
    assert stock["up_down"] is None and stock["leader"] is None


# ── 工具层 ────────────────────────────────────────────────────────


def test_limit被夹在1到50(monkeypatch):
    got = []

    def fake(scope, metric, limit):
        got.append(limit)
        return {"available": True, "reason": "", "caliber": "c", "scope": scope,
                "metric": metric, "rows": [], "count": 0}

    import marketdata.vendors.tencent_rank as mod
    monkeypatch.setattr(mod, "fetch_board_rank", fake)
    fn = CHAT_TOOL_REGISTRY["get_market_ranking"].handler
    asyncio.run(fn(None, {"limit": 999}))
    asyncio.run(fn(None, {"limit": -3}))
    assert got == [50, 1], f"越界必须夹到 1..50，实际 {got}"


def test_工具输出必须带口径与涨跌家数说明(monkeypatch):
    import marketdata.vendors.tencent_rank as mod

    monkeypatch.setattr(mod, "fetch_board_rank", lambda s, m, l: {
        "available": True, "reason": "", "caliber": "腾讯排行 · 实时快照 · 非复权",
        "scope": "concept", "metric": "main_inflow", "count": 1,
        "rows": [{"code": "pt1", "name": "人工智能", "change_pct": 3.05, "main_inflow": 992800.0,
                  "turnover_rate": 2.8, "up_down": "890/1040",
                  "leader": {"name": "利亚德", "change_pct": 20.0}}],
    })
    out = asyncio.run(CHAT_TOOL_REGISTRY["get_market_ranking"].handler(None, {"scope": "concept"}))
    assert "口径" in out and "非复权" in out
    assert "99.28亿" in out
    assert "不是涨停家数" in out, "板块涨跌家数必须显式排除涨停家数误解"


def test_取数失败如实说_不当作没有(monkeypatch):
    import marketdata.vendors.tencent_rank as mod

    monkeypatch.setattr(mod, "fetch_board_rank", lambda s, m, l: {
        "available": False, "reason": "腾讯排行拉取失败: TimeoutError: timed out",
        "rows": [], "caliber": "c", "scope": s, "metric": m, "count": 0,
    })
    out = asyncio.run(CHAT_TOOL_REGISTRY["get_market_ranking"].handler(None, {}))
    assert "取不到数据" in out and "TimeoutError" in out
    assert "今天没有" in out


# ── 注册表钉子 ─────────────────────────────────────────────────────


def test_工具数77且新工具在末尾():
    assert len(CHAT_TOOL_REGISTRY) == 77
    assert list(CHAT_TOOL_REGISTRY)[-1] == "get_market_ranking", "新工具必须追加末尾，否则索引钉子整体错位"
    assert len(chat_tool_schemas()) == 76


def test_新工具有非空caliber():
    assert CHAT_TOOL_REGISTRY["get_market_ranking"].caliber.strip()
