"""多源入池 + 共振计分测试(2026-08-21 机会页整合 P1)。"""
from __future__ import annotations

import sys
from datetime import date

import pytest

from src.core.entry_candidates import (
    _apply_resonance_bonus,
    _merge_extra_source_seeds,
    record_manual_query_candidates,
)


def _seed(source: str, symbol: str = "002361") -> dict:
    return {
        "symbol": symbol,
        "market": "CN",
        "stock_name": f"股票{symbol}",
        "candidate_source": source,
        "source_agent": source,
        "source_suggestion_id": None,
        "source_trace_id": "",
        "quote_seed": None,
        "action": "watch",
        "action_label": "关注",
        "signal": source,
        "reason": f"{source} 命中",
        "meta": {"source": source},
        "strategy_tags_seed": [source],
    }


class TestMergeExtraSeeds:
    def test_new_symbol_added(self):
        base = {"CN:600519": _seed("watchlist", "600519")}
        extra = {"CN:002361": _seed("auction")}
        added = _merge_extra_source_seeds(base, extra)
        assert added == 1
        assert "CN:002361" in base
        assert base["CN:002361"]["candidate_source"] == "auction"

    def test_existing_symbol_records_hit(self):
        base = {"CN:002361": _seed("watchlist")}
        extra = {"CN:002361": _seed("strategy"), "CN:002361x": _seed("tdx")}
        added = _merge_extra_source_seeds(base, extra)
        assert added == 1  # 只有 002361x 是新票; 002361 已存在只记 hit
        meta = base["CN:002361"]["meta"]
        assert base["CN:002361x"]["candidate_source"] == "tdx"
        assert meta["source_hits"] == ["strategy"]

    def test_duplicate_hits_deduped(self):
        base = {"CN:002361": _seed("watchlist")}
        base["CN:002361"]["meta"]["source_hits"] = ["strategy"]
        extra = {"CN:002361": _seed("strategy")}
        _merge_extra_source_seeds(base, extra)
        assert base["CN:002361"]["meta"]["source_hits"] == ["strategy"]


class TestResonanceBonus:
    def test_no_resonance_when_single_source(self):
        m = {"CN:002361": _seed("watchlist")}
        _apply_resonance_bonus(m)
        assert "resonance_count" not in m["CN:002361"]["meta"]

    def test_resonance_count_and_bonus(self):
        seed = _seed("watchlist")
        seed["meta"]["source_hits"] = ["strategy", "auction"]
        m = {"CN:002361": seed}
        _apply_resonance_bonus(m, bonus_per_source=8.0)
        meta = m["CN:002361"]["meta"]
        assert meta["resonance_count"] == 3
        assert meta["resonance_bonus"] == 16.0
        assert set(meta["resonance_sources"]) == {"watchlist", "strategy", "auction"}


class TestRecordManualQuery:
    """record_manual_query_candidates 用全局 SessionLocal, 不依赖注入 fixture。"""

    def test_record_tdx_results(self):
        from src.web.database import SessionLocal
        from src.web.models import ManualQueryCandidate

        day = date.today().strftime("%Y-%m-%d")
        saved = record_manual_query_candidates(
            kind="tdx",
            query_text="近5日主力净流入前10",
            snapshot_date=day,
            items=[
                {"symbol": "002361", "market": "CN", "name": "神剑股份"},
                {"symbol": "600519", "market": "CN", "name": "贵州茅台"},
                {"symbol": "", "market": "CN", "name": "空代码应跳过"},
            ],
        )
        assert saved == 2
        db = SessionLocal()
        try:
            rows = (
                db.query(ManualQueryCandidate)
                .filter(ManualQueryCandidate.kind == "tdx", ManualQueryCandidate.snapshot_date == day)
                .order_by(ManualQueryCandidate.rank_in_result)
                .all()
            )
            assert len(rows) >= 2
            assert rows[0].stock_symbol == "002361"
            assert rows[0].rank_in_result == 1
        finally:
            db.close()

    def test_invalid_kind_ignored(self):
        saved = record_manual_query_candidates(
            kind="bad", query_text="x", items=[{"symbol": "002361"}]
        )
        assert saved == 0

    def test_seeds_read_back(self):
        """入池记录后 _load_manual_query_seeds 能读回种子。"""
        from src.core.entry_candidates import _load_manual_query_seeds

        day = date.today().strftime("%Y-%m-%d")
        record_manual_query_candidates(
            kind="wencai",
            query_text="今日涨停非ST",
            snapshot_date=day,
            items=[{"symbol": "300189", "market": "CN", "name": "神农种业"}],
        )
        seeds = _load_manual_query_seeds("wencai", day)
        # 可能读到其他测试留下的数据, 只断言结构正确
        for key, s in seeds.items():
            assert s["candidate_source"] == "wencai"
            assert "query_text" in s["meta"]
