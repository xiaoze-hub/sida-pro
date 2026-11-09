# -*- coding: utf-8 -*-
"""主线概念维度上线阻断项(2026-11-09 审计复核后的红→绿钉子)。

审计线索(已逐条复现):
  ① 降级源(东财/wudao/TQ快照)没有概念字段 → 概念维度空, 但响应里**没有任何说明**,
     消费方会把\"源没给概念\"读成\"今日无概念\";
  ② `get_limit_up_pool(date)` 的 date 在 TQ 主源被**完全无视** → market_phase 取\"昨日池\"
     实际拿到今日池(promo_rate 恒 0), 数据日期错配却按当日/昨日呈现;
  ③ 行业分组 `ind = [...] or blocks` → 没有 881 行业块时拿 **880 概念名冒充行业**;
  ④ 行业分组不按代码去重 → 同一只票在池里出现两次就计 2 家;
  ⑤ `concept_source` 硬编码 \"tq:get_relation\", 池子来自东财也照写;
  ⑥ 旧版本 biz_cache/日频快照载荷没有 concept_* 键 → 读缓存时新字段整体消失。

本文件钉住修复后的契约; 全部禁真网络。
"""
from __future__ import annotations

import time

import pytest

from src.core import market_mainline as mm


def test_window_bars_excludes_stocks_without_requested_day():
    from src.collectors.market_sentiment_collector import _window_bars

    bars = {
        '000001.SZ': {'Date': ['20261106', '20261109'], 'Close': [10, 11]},
        '000002.SZ': {'Date': ['20261105', '20261106'], 'Close': [10, 11]},
    }
    window, covered = _window_bars(bars, '20261109')
    assert covered is True
    assert set(window) == {'000001.SZ'}


def _z(code: str, name: str, days: int, sector: str = "", concepts=None, source: str = "tq", **kw) -> dict:
    row = {
        "code": code,
        "name": name,
        "days": days,
        "sector": sector,
        "theme": "",
        "concepts": list(concepts or []),
        "amount": 1e8,
        "source": source,
    }
    row.update(kw)
    return row


# ──────────── ① 降级源无概念: 必须显式告知, 不许静默空 ────────────
def test_degraded_source_declares_concept_unavailable():
    pool = [_z(str(i), f"S{i}", 2, sector="航天装备", source="eastmoney") for i in range(3)]
    r = mm.aggregate_mainline(pool)
    assert r["concept_groups"] == []
    assert r["concept_status"] == "unavailable"
    assert "概念" in r["concept_note"]
    assert "未提供" in r["concept_note"] or "缺失" in r["concept_note"]
    # 不能把\"空\"说成\"没有概念题材\"
    assert r["data_meta"]["concept_source"] is None
    assert r["data_meta"]["concept_status"] == "unavailable"


def test_partial_concepts_are_marked_partial():
    pool = [
        _z("1", "A", 2, sector="X", concepts=["商业航天"]),
        _z("2", "B", 2, sector="X", concepts=["商业航天"]),
        _z("3", "C", 2, sector="X", concepts=["商业航天"]),
        _z("4", "D", 2, sector="X"),  # 无概念归属
    ]
    r = mm.aggregate_mainline(pool)
    assert r["concept_status"] == "partial"
    assert r["concept_stats"]["untagged_stocks"] == 1
    assert "未覆盖" in r["concept_note"] or "缺失" in r["concept_note"]


def test_full_concepts_marked_available():
    pool = [_z(str(i), f"S{i}", 2, sector="X", concepts=["商业航天"]) for i in range(3)]
    r = mm.aggregate_mainline(pool)
    assert r["concept_status"] == "available"
    assert r["concept_note"] == ""


# ──────────── ② 来源不得臆测 ────────────
def test_concept_source_uses_item_block_source():
    pool = [
        _z(str(i), f"S{i}", 2, sector="X", concepts=["商业航天"], block_source="fts:concept")
        for i in range(3)
    ]
    r = mm.aggregate_mainline(pool)
    assert r["data_meta"]["concept_source"] == "fts:concept"


def test_concept_source_not_faked_for_non_tq_pool():
    """东财池子即便（假设）带了概念, 也不能声称来自 TQ。"""
    pool = [_z(str(i), f"S{i}", 2, sector="X", concepts=["商业航天"], source="eastmoney") for i in range(3)]
    r = mm.aggregate_mainline(pool)
    assert r["data_meta"]["concept_source"] != "tq:get_relation"
    assert r["data_meta"]["concept_source"] == "unknown"


# ──────────── ③ 行业不冒充概念 / ④ 去重 ────────────
def test_duplicate_codes_counted_once():
    pool = [
        _z("1", "A", 3, sector="航天装备", concepts=["商业航天"]),
        _z("1", "A", 3, sector="航天装备", concepts=["商业航天"]),  # 脏重复
        _z("2", "B", 1, sector="航天装备"),
        _z("3", "C", 1, sector="航天装备"),
    ]
    r = mm.aggregate_mainline(pool)
    g = {x["name"]: x for x in r["ranked_groups"]}["航天装备"]
    assert g["limit_up_count"] == 3
    assert len({c["code"] for c in g["constituents"]}) == 3
    assert r["total_limit_ups"] == 3
    assert r["concept_stats"]["distinct_stocks"] == 1
    assert r["concept_stats"]["untagged_stocks"] == 2


# ──────────── ⑤ 采集侧: 无 881 行业块时不许拿概念当行业 ────────────
def _patch_tq(monkeypatch, dates, closes):
    import marketdata.vendors.tq as tqmod

    def fake_rpc(method, params, timeout=None):
        if method == "get_stock_list":
            return [{"Code": "600001.SH", "Name": "样本"}]
        if method == "get_market_data":
            return {"600001.SH": {"Close": closes, "Amount": [1] * len(closes), "Date": dates}}
        return {}

    monkeypatch.setattr(tqmod, "tq_rpc", fake_rpc, raising=True)


def test_industry_never_uses_concept_block_as_sector(monkeypatch):
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    _patch_tq(monkeypatch, ["20261107", "20261110"], ["10.0", "11.0"])
    blocks = [{"code": "880301.SH", "name": "商业航天", "type": "gn"}]
    monkeypatch.setattr("src.core.tdx_boards.stock_blocks", lambda sym: blocks, raising=False)

    pool = MarketSentimentCollector()._limit_up_pool_tq()
    assert len(pool) == 1
    assert pool[0]["sector"] == ""  # 行业缺失就空着, 概念绝不顶上
    assert pool[0]["concepts"] == ["商业航天"]


# ──────────── ⑥ 日期参数必须被尊重 ────────────
def test_fallback_pool_enriched_when_relation_available(monkeypatch):
    """降级源(无概念字段)在能安全拿到 get_relation 时补概念; 补不到就空着(不编造)。"""
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    monkeypatch.setattr(
        "src.core.tdx_boards.stock_blocks",
        lambda sym: [
            {"code": "881001.SH", "name": "航天装备", "type": "hy"},
            {"code": "880301.SH", "name": "商业航天", "type": "gn"},
        ],
        raising=False,
    )
    rows = [{"code": "1", "name": "A", "days": 1, "sector": "", "source": "eastmoney"}]
    out = MarketSentimentCollector()._maybe_enrich_fallback(rows, "")
    assert out[0]["sector"] == "航天装备"
    assert out[0]["concepts"] == ["商业航天"]
    assert out[0]["block_source"] == "tq:get_relation"


def test_fallback_pool_not_enriched_for_historical_date(monkeypatch):
    """历史日期的板块成员关系≠当日, 不拿今天的概念盖昨天。"""
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    monkeypatch.setattr(
        "src.core.tdx_boards.stock_blocks",
        lambda sym: [{"code": "880301.SH", "name": "商业航天", "type": "gn"}],
        raising=False,
    )
    rows = [{"code": "1", "name": "A", "days": 1, "source": "eastmoney"}]
    out = MarketSentimentCollector()._maybe_enrich_fallback(rows, "20260105")
    assert "concepts" not in out[0]


def test_tq_pool_honors_explicit_date(monkeypatch):
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    _patch_tq(monkeypatch, ["20261106", "20261109", "20261110"], ["10.0", "11.0", "12.1"])
    monkeypatch.setattr("src.core.tdx_boards.stock_blocks", lambda sym: [], raising=False)

    pool = MarketSentimentCollector()._limit_up_pool_tq("20261109")
    assert len(pool) == 1
    assert pool[0]["as_of"] == "2026-11-09"
    assert pool[0]["days"] == 1  # 截止到 11-09 只有 1 连板, 不许拿 11-10 充数


def test_tq_pool_returns_empty_when_date_not_covered(monkeypatch):
    """请求的交易日不在日线里 → 空池(让带日期参数的备源接手), 绝不返回最新一天冒充。"""
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    _patch_tq(monkeypatch, ["20261109", "20261110"], ["10.0", "11.0"])
    monkeypatch.setattr("src.core.tdx_boards.stock_blocks", lambda sym: [], raising=False)

    assert MarketSentimentCollector()._limit_up_pool_tq("20261106") == []


def test_tq_pool_without_date_keeps_latest_and_true_as_of(monkeypatch):
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    _patch_tq(monkeypatch, ["20261109", "20261110"], ["10.0", "11.0"])
    monkeypatch.setattr("src.core.tdx_boards.stock_blocks", lambda sym: [], raising=False)

    pool = MarketSentimentCollector()._limit_up_pool_tq()
    assert pool and pool[0]["as_of"] == "2026-11-10"


def test_get_limit_up_pool_passes_date_to_tq_main_source(monkeypatch):
    """主源必须收到调用方显式日期(market_phase 的\"昨日池\"不能拿今日数据)。"""
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    seen: list = []

    def fake_tq(self, date=None):
        seen.append(date)
        return [{"code": "1", "name": "A", "days": 1}]

    monkeypatch.setattr(MarketSentimentCollector, "_limit_up_pool_tq", fake_tq, raising=True)
    pool = MarketSentimentCollector().get_limit_up_pool("20261106")
    assert pool and seen == ["20261106"]


# ──────────── ⑦ rank_change / 快照按数据日, 不按墙钟 ────────────
def test_rank_change_and_snapshot_use_data_date(monkeypatch):
    from src.web.api import market_mainline as api

    calls: dict[str, str] = {}

    monkeypatch.setattr(api, "_load_yesterday_ranks", lambda d: calls.setdefault("y", d) or {})
    monkeypatch.setattr(api, "_upsert_today_snapshot", lambda d, g: calls.setdefault("snap", d))

    out = api._enrich_with_rank_change([{"name": "AI算力", "score": 90.0}], as_of="2026-11-06")
    assert out[0]["rank"] == 1
    assert calls == {"y": "2026-11-06", "snap": "2026-11-06"}


def test_rank_change_without_as_of_does_not_write_snapshot(monkeypatch):
    """数据时点未知 → 不落\"今日\"快照, rank_change 也不假装有昨日对比。"""
    from src.web.api import market_mainline as api

    written: list = []
    monkeypatch.setattr(api, "_load_yesterday_ranks", lambda d: {"AI算力": 2})
    monkeypatch.setattr(api, "_upsert_today_snapshot", lambda d, g: written.append(d))

    out = api._enrich_with_rank_change([{"name": "AI算力", "score": 90.0}], as_of=None)
    assert out[0]["rank_change"] is None
    assert written == []


# ──────────── ⑧ 旧版本缓存载荷: 新字段补齐并说明 ────────────
def test_legacy_cached_payload_gets_concept_keys_with_notice():
    from src.web.api import market_mainline as api

    legacy = {
        "total_groups": 2,
        "ranked_groups": [{"name": "AI算力", "rank": 1}],
        "unranked": [],
        "note": "",
        "cache_ts": time.time(),
    }
    out = api._normalize_payload(legacy)
    assert out["concept_groups"] == []
    assert out["concept_unranked"] == []
    assert out["concept_stats"]["summable"] is False
    assert out["concept_stats"]["legacy_payload"] is True
    assert "旧版本" in out["concept_stats"]["note"]
    assert out["concept_status"] == "unavailable"
    assert "旧版本" in out["concept_note"]
    assert out["dimension"] == "industry"
    assert out["data_meta"]["as_of"] is None  # 不补造交易日
    assert out["ranked_groups"] == legacy["ranked_groups"]  # 旧数据原样保留


def test_normalize_payload_leaves_new_payload_untouched():
    from src.web.api import market_mainline as api

    new = mm.aggregate_mainline([_z(str(i), f"S{i}", 2, sector="X", concepts=["商业航天"]) for i in range(3)])
    new["cache_ts"] = time.time()
    out = api._normalize_payload(new)
    assert out["concept_status"] == "available"
    assert out["concept_stats"]["legacy_payload"] is False
    assert [g["name"] for g in out["concept_groups"]] == ["商业航天"]
