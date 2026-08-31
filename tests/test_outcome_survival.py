"""P1/P2 修复复现测试(2026-08-23 判断准确性大修)。

P1: refresh_entry_candidates 幂等 upsert — 二轮刷新后消失的候选标 retired
    而非物理删除, 候选 ID 稳定, EntryCandidateOutcome 不再被 FK CASCADE 连坐删。
P2: 共振加分真正进评分 — 两条评分路径(_score_suggestion/_score_market_scan_candidate)
    从种子 meta 读 resonance_bonus(此前一条路径读错对象、一条路径完全没读)。

使用 9999xx 伪代码隔离真实数据, 测试后清理(防 panwatch.db 污染)。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from src.core import entry_candidates as ec
from src.web.database import SessionLocal
from src.web.models import EntryCandidate, EntryCandidateOutcome

TODAY = date.today().strftime("%Y-%m-%d")
SYMS = ["999901", "999902", "999903"]


def _seed(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "market": "CN",
        "stock_name": f"测试{symbol}",
        "candidate_source": "market_scan",
        "source_agent": "market_scan",
        "source_suggestion_id": None,
        "source_trace_id": "",
        "quote_seed": {"current_price": 10.0, "change_pct": 1.5, "turnover": 2e9},
        "action": "buy",
        "action_label": "建仓",
        "signal": "测试信号",
        "reason": "测试种子",
        "meta": {"source": "market_scan"},
        "strategy_tags_seed": ["trend_follow"],
    }


def _patch_network(monkeypatch, seeds: dict[str, dict]):
    """隔离全部网络/DB 依赖, 让 refresh 只处理给定种子。"""
    monkeypatch.setattr(ec, "_load_latest_suggestions", lambda **kw: [])
    monkeypatch.setattr(ec, "_load_market_scan_inputs", lambda **kw: {})
    monkeypatch.setattr(ec, "_load_holding_keys", lambda: set())
    monkeypatch.setattr(ec, "_load_multi_source_seeds", lambda snapshot: {})
    monkeypatch.setattr(ec, "_load_quote_fallbacks", lambda key_set: {})
    monkeypatch.setattr(ec, "_load_kline_fallbacks", lambda key_set, **kw: {})

    def fake_md_stock_data(symbols, market):
        return [
            SimpleNamespace(
                symbol=s,
                current_price=10.0,
                change_pct=1.5,
                turnover=2e9,
            )
            for s in symbols
            if s in SYMS
        ]

    monkeypatch.setattr(ec, "md_stock_data", fake_md_stock_data)
    # input_map 直接由种子构成(绕过 market_scan_map 合并细节)
    return dict(seeds)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    db = SessionLocal()
    try:
        ids = [
            r.id
            for r in db.query(EntryCandidate)
            .filter(EntryCandidate.stock_symbol.in_(SYMS))
            .all()
        ]
        if ids:
            db.query(EntryCandidateOutcome).filter(
                EntryCandidateOutcome.candidate_id.in_(ids)
            ).delete(synchronize_session=False)
        db.query(EntryCandidate).filter(
            EntryCandidate.stock_symbol.in_(SYMS)
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


class TestOutcomeSurvival:
    def test_second_refresh_retires_instead_of_delete(self, monkeypatch):
        """P1: 二轮刷新后 A 标 retired 非 deleted, ID 不变, outcome 存活。"""
        _patch_network(monkeypatch, {})
        seeds_r1 = {f"CN:{s}": _seed(s) for s in SYMS[:2]}
        monkeypatch.setattr(
            "src.core.entry_candidates._load_market_scan_inputs",
            lambda **kw: dict(seeds_r1),
        )
        ec.refresh_entry_candidates(snapshot_date=TODAY, max_kline_symbols=0)

        db = SessionLocal()
        try:
            a1 = (
                db.query(EntryCandidate)
                .filter(EntryCandidate.stock_symbol == SYMS[0])
                .first()
            )
            assert a1 is not None and a1.status == "active", "第一轮 A 应 active 入池"
            a_id = a1.id
            db.add(
                EntryCandidateOutcome(
                    candidate_id=a_id,
                    snapshot_date=TODAY,
                    stock_symbol=SYMS[0],
                    stock_market="CN",
                    horizon_days=1,
                    outcome_status="pending",
                )
            )
            db.commit()
        finally:
            db.close()

        # 第二轮: B 保留, A 消失, C 新增
        seeds_r2 = {f"CN:{s}": _seed(s) for s in (SYMS[1], SYMS[2])}
        monkeypatch.setattr(
            "src.core.entry_candidates._load_market_scan_inputs",
            lambda **kw: dict(seeds_r2),
        )
        ec.refresh_entry_candidates(snapshot_date=TODAY, max_kline_symbols=0)

        db = SessionLocal()
        try:
            a2 = (
                db.query(EntryCandidate)
                .filter(EntryCandidate.stock_symbol == SYMS[0])
                .first()
            )
            assert a2 is not None, "P1: A 不应被物理删除"
            assert a2.id == a_id, "P1: 存活行 ID 必须稳定(信号/因子外键依赖它)"
            assert a2.status == "retired", "P1: 消失的候选应标 retired"
            outcome = (
                db.query(EntryCandidateOutcome)
                .filter(EntryCandidateOutcome.candidate_id == a_id)
                .count()
            )
            assert outcome == 1, "P1: 后验结果不应被级联删除"
            b = (
                db.query(EntryCandidate)
                .filter(EntryCandidate.stock_symbol == SYMS[1])
                .first()
            )
            assert b is not None and b.status == "active", "B 应保持 active"
        finally:
            db.close()


class TestResonanceScoring:
    def test_market_scan_resonance_bonus(self):
        base, _ = ec._score_market_scan_candidate(
            action="buy", quote={"current_price": 10.0, "change_pct": 2.0, "turnover": 3e9},
            kline={"trend": "多头排列"}, strategy_tags=["trend_follow"],
        )
        boosted, evidence = ec._score_market_scan_candidate(
            action="buy", quote={"current_price": 10.0, "change_pct": 2.0, "turnover": 3e9},
            kline={"trend": "多头排列"}, strategy_tags=["trend_follow"],
            resonance_meta={
                "resonance_bonus": 8.0,
                "resonance_count": 2,
                "resonance_sources": ["market_scan", "tdx"],
            },
        )
        assert boosted == pytest.approx(base + 8.0)
        assert any("共振" in e for e in evidence)

    def test_suggestion_resonance_reads_seed_meta(self):
        """P2: ORM suggestion.meta 无共振字段时, 应从种子 meta 生效。"""

        class FakeSuggestion:
            meta = {}
            created_at = None
            signal = "测试信号"
            reason = "测试"
            action = "buy"
            action_label = "建仓"

            @property
            def context_quality_score(self):
                return None

        base, _ = ec._score_suggestion(
            action="buy", suggestion=FakeSuggestion(), quote={}, kline={}
        )
        boosted, evidence = ec._score_suggestion(
            action="buy", suggestion=FakeSuggestion(), quote={}, kline={},
            resonance_meta={
                "resonance_bonus": 8.0,
                "resonance_count": 3,
                "resonance_sources": ["market_scan", "tdx", "wencai"],
            },
        )
        assert boosted == pytest.approx(base + 8.0)
        assert any("共振×3" in e for e in evidence)
