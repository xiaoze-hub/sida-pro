"""主线概念维度(2026-11-09): 概念独立聚合 + 重叠不相加 + 时点/来源诚实标注。

背景: 主线此前只按 `theme > sector > industry` **单一归属**分组, 而 TQ 概念是
"一股多概念" (880xxx 两百多个), 拼成 theme 会把每只票拆成自成一组的孤儿
(实测 37 只 → 37 组 → ranked=0)。修复不改变这个单维聚合口径, 而是**新增**一条
概念独立维度:
  · 一只票可同时进多个概念 → 各概念家数是**独立统计**, 相加 ≠ 涨停总数 (不可累加);
  · 概念内 leader 只是"本概念内连板/成交额排序第一", **不是**该票的专属驱动;
  · 概念归属来自 TQ get_relation 全量返回 (不再只留前 3 个);
  · 数据时点(as_of)/来源(source)必须显式标注, 拿不到就写 None, 不猜测。

本文件钉死上述口径(禁真网络, 纯函数 + 打桩)。
"""
from __future__ import annotations

import sys
import types

import pytest

from src.core import market_mainline as mm


def _z(
    code: str,
    name: str,
    days: int,
    sector: str = "",
    theme: str = "",
    concepts: list[str] | None = None,
    amount: float = 1e8,
) -> dict:
    return {
        "code": code,
        "name": name,
        "days": days,
        "sector": sector,
        "theme": theme,
        "concepts": list(concepts or []),
        "amount": amount,
        "source": "tq",
    }


# ──────────── 契约: 新增键, 旧键不动 ────────────
def test_industry_results_preserved_and_concept_dimension_added():
    """行业/题材结果一个不丢, 概念只是**多一条**维度。"""
    pool = [
        _z("1", "A", 3, sector="航天装备", concepts=["商业航天", "军工"]),
        _z("2", "B", 2, sector="航天装备", concepts=["商业航天"]),
        _z("3", "C", 1, sector="航天装备", concepts=["商业航天", "低空经济"]),
        _z("4", "D", 2, sector="通信设备", concepts=["6G"]),
    ]
    r = mm.aggregate_mainline(pool)
    assert [g["name"] for g in r["ranked_groups"]] == ["航天装备"]  # 行业口径不变
    assert r["ranked_groups"][0]["limit_up_count"] == 3
    assert r["dimension"] == "industry"
    assert [g["name"] for g in r["concept_groups"]] == ["商业航天"]
    assert r["concept_groups"][0]["dimension"] == "concept"


def test_empty_pool_keeps_concept_keys():
    r = mm.aggregate_mainline([])
    assert r["concept_groups"] == []
    assert r["concept_unranked"] == []
    assert r["concept_stats"]["distinct_stocks"] == 0
    assert r["concept_stats"]["summable"] is False


# ──────────── 独立性: 重叠不相加 ────────────
def test_concept_counts_are_independent_and_not_summed():
    """同一只票进两个概念 → 两个概念各 +1, 但总数仍是 4 只, 不能相加当 6 只。"""
    pool = [
        _z("1", "A", 2, sector="军工电子", concepts=["商业航天", "军工"]),
        _z("2", "B", 1, sector="军工电子", concepts=["商业航天"]),
        _z("3", "C", 1, sector="军工电子", concepts=["商业航天", "军工"]),
        _z("4", "D", 1, sector="军工电子", concepts=["军工"]),
    ]
    r = mm.aggregate_mainline(pool)
    groups = {g["name"]: g for g in r["concept_groups"]}
    assert groups["商业航天"]["limit_up_count"] == 3
    assert groups["军工"]["limit_up_count"] == 3

    stats = r["concept_stats"]
    assert stats["distinct_stocks"] == 4
    assert stats["member_count_sum"] == 6  # 3 + 3
    assert stats["overlap_excess"] == 2  # 6 - 4: 两只票各重复计 1 次
    assert stats["multi_concept_stocks"] == 2
    assert stats["summable"] is False
    assert "不可相加" in stats["note"]


def test_same_stock_listed_twice_in_concept_counts_once():
    """同一概念内重复归属(脏数据)只算 1 只 —— 按代码去重。"""
    pool = [
        _z("1", "A", 1, sector="X", concepts=["商业航天", "商业航天"]),
        _z("2", "B", 1, sector="X", concepts=["商业航天"]),
        _z("3", "C", 1, sector="X", concepts=["商业航天"]),
    ]
    r = mm.aggregate_mainline(pool)
    g = {x["name"]: x for x in r["concept_groups"]}["商业航天"]
    assert g["limit_up_count"] == 3
    assert len({c["code"] for c in g["constituents"]}) == 3


# ──────────── 归属诚实: leader 不是专属驱动 ────────────
def test_concept_leader_marked_shared_not_exclusive():
    """连板最高的票同时属于 3 个概念 → 明确标出共享概念, 不许暗示"专属龙头"。"""
    pool = [
        _z("1", "高标", 4, sector="航天装备", concepts=["商业航天", "军工", "低空经济"], amount=9e8),
        _z("2", "B", 1, sector="航天装备", concepts=["商业航天"]),
        _z("3", "C", 1, sector="航天装备", concepts=["商业航天"]),
    ]
    r = mm.aggregate_mainline(pool)
    g = {x["name"]: x for x in r["concept_groups"]}["商业航天"]
    assert g["leader"]["code"] == "1"
    assert "非" in g["leader_basis"] and "专属" in g["leader_basis"]
    assert sorted(g["leader_shared_concepts"]) == sorted(["军工", "低空经济"])
    assert g["exclusive_ratio"] == 0.67  # 3 只里 2 只只属本概念
    # 成分股带 concept_count, 前端能看出"这票同时在几个概念里"
    lead = [c for c in g["constituents"] if c["code"] == "1"][0]
    assert lead["concept_count"] == 3


# ──────────── 过滤与门槛沿用 ────────────
def test_broad_concept_filtered():
    """融资融券这类"宽基概念"同样剔除, 不当主线。"""
    pool = [
        _z("1", "A", 2, sector="X", concepts=["商业航天"]),
        _z("2", "B", 2, sector="X", concepts=["商业航天"]),
        _z("3", "C", 2, sector="X", concepts=["商业航天"]),
        _z("4", "D", 2, sector="X", concepts=["融资融券"]),
        _z("5", "E", 2, sector="X", concepts=["融资融券"]),
        _z("6", "F", 2, sector="X", concepts=["融资融券"]),
    ]
    r = mm.aggregate_mainline(pool)
    names = {g["name"] for g in r["concept_groups"]}
    assert "商业航天" in names
    assert "融资融券" not in names
    assert r["concept_stats"]["broad_filtered"] == 1


def test_concept_below_min_goes_unranked():
    pool = [
        _z("1", "A", 2, sector="X", concepts=["商业航天"]),
        _z("2", "B", 2, sector="X", concepts=["商业航天"]),
        _z("3", "C", 2, sector="X", concepts=["商业航天"]),
        _z("4", "D", 2, sector="X", concepts=["小众概念"]),
    ]
    r = mm.aggregate_mainline(pool)
    assert [g["name"] for g in r["concept_groups"]] == ["商业航天"]
    un = {g["name"]: g for g in r["concept_unranked"]}
    assert un["小众概念"]["limit_up_count"] == 1
    assert un["小众概念"]["score"] is None


# ──────────── 数据时点 / 来源 ────────────
def test_data_meta_reports_as_of_and_sources():
    pool = [
        _z("1", "A", 2, sector="X", concepts=["商业航天"], ),
        _z("2", "B", 2, sector="X", concepts=["商业航天"]),
        _z("3", "C", 2, sector="X", concepts=["商业航天"]),
    ]
    for p in pool:
        p["as_of"] = "2026-11-09"
    r = mm.aggregate_mainline(pool)
    meta = r["data_meta"]
    assert meta["as_of"] == "2026-11-09"
    assert meta["pool_source"] == "tq"
    assert meta["concept_source"] == "tq:get_relation"
    assert meta["computed_at"]  # 计算时点(ISO)
    assert meta["as_of_note"] == ""


def test_data_meta_does_not_invent_as_of():
    """池子没带交易日 → as_of=None + 明确说明, 不拿"今天"冒充。"""
    pool = [_z("1", "A", 2, sector="X", concepts=["商业航天"])]
    r = mm.aggregate_mainline(pool)
    assert r["data_meta"]["as_of"] is None
    assert "未知" in r["data_meta"]["as_of_note"]


def test_pool_without_concepts_has_empty_concept_dimension():
    """东财/wudao 源没有概念字段 → 概念维度为空, 不能凭行业编概念。"""
    pool = [
        _z("1", "A", 2, sector="航天装备"),
        _z("2", "B", 2, sector="航天装备"),
        _z("3", "C", 2, sector="航天装备"),
    ]
    r = mm.aggregate_mainline(pool)
    assert r["concept_groups"] == []
    assert r["concept_stats"]["tagged_stocks"] == 0
    assert r["data_meta"]["concept_source"] is None


# ──────────── 采集侧: 概念全量, 不再只留前 3 ────────────
def test_collector_keeps_full_concept_membership(monkeypatch):
    """TQ get_relation 返回 6 个概念 → 全存(此前只留前 3, 主线会漏题材)。"""
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    concept_names = ["商业航天", "军工", "低空经济", "大飞机", "卫星导航", "无人机"]

    def fake_rpc(method, params, timeout=None):
        if method == "get_stock_list":
            return [{"Code": "600001.SH", "Name": "样本"}]
        if method == "get_market_data":
            return {"600001.SH": {"Close": ["10.0", "11.0"], "Amount": ["1", "1"], "Date": ["20261107", "20261110"]}}
        return {}

    import marketdata.vendors.tq as tqmod

    monkeypatch.setattr(tqmod, "tq_rpc", fake_rpc, raising=True)
    monkeypatch.setattr(
        "src.core.tdx_boards.stock_blocks",
        lambda sym: (
            [{"code": "881001.SH", "name": "航天装备", "type": "hy"}]
            + [{"code": f"880{100 + i}.SH", "name": n, "type": "gn"} for i, n in enumerate(concept_names)]
        ),
        raising=False,
    )
    pool = MarketSentimentCollector()._limit_up_pool_tq()
    assert len(pool) == 1
    row = pool[0]
    assert row["sector"] == "航天装备"
    assert row["concepts"] == concept_names  # 全量, 有序, 未截断
    assert row["as_of"] == "2026-11-10"  # 取自真实日线日期, 不编造
    assert row["block_source"] == "tq:get_relation"
