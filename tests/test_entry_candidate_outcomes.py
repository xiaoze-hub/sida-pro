"""entry_candidates outcome 验证覆盖率修复测试。

背景(2026-08): 验证机制原实现按 snapshot_date 降序(最新优先) + limit 截断扫描集,
候选只有"变老"才到期 —— 候选池按日增长且无清理, 池子超过 limit 后最老的候选被
永久挤出扫描窗口, 5/10 日 horizon 永远不会被验证。本测试锁定修复后的行为:
  1. 到期且未验证的候选必然被处理(最老优先, limit 只是单轮处理上限, 不丢候选);
  2. 今日新增/全部已验证的候选不拉 K 线;
  3. no_base_price(验证失败)原地重试, 不重复插入;
  4. K 线拉取失败计数并留待下轮重试, 不静默;
  5. count_missing_candidate_outcomes 只读缺口报告可观测。
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.core.entry_candidates as ec
from src.web import models  # noqa: F401  注册 ORM
from src.web.database import Base
from src.web.models import EntryCandidate, EntryCandidateOutcome


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/test.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _mk_candidate(session, symbol: str, snap: date, *, score: float = 80.0) -> int:
    """落库一个 active 候选, 返回 id。entry 区间 9.5~10.5 → base=10.0。"""
    c = EntryCandidate(
        stock_symbol=symbol,
        stock_market="CN",
        stock_name=symbol,
        snapshot_date=snap.strftime("%Y-%m-%d"),
        status="active",
        score=score,
        action="buy",
        action_label="建仓",
        signal="放量突破",
        candidate_source="market_scan",
        strategy_tags=["trend_follow"],
        entry_low=9.5,
        entry_high=10.5,
        stop_loss=8.0,
        target_price=12.0,
    )
    session.add(c)
    session.commit()
    return c.id


def _kline_rows(n_days_back: int = 40, close: float = 11.0) -> list[SimpleNamespace]:
    """最近 n_days_back 天(含今天)的日线, 收盘价固定 close。"""
    today = date.today()
    return [
        SimpleNamespace(date=(today - timedelta(days=i)).isoformat(), close=close)
        for i in range(n_days_back, -1, -1)
    ]


class FakeKlineCollector:
    """可编程的 KlineCollector 替身: 记录调用, 可配置抛异常。"""

    calls: list[tuple[str, int | None]] = []
    fail_next: bool = False

    def __init__(self, market):
        self.market = market

    def get_klines(self, symbol, days=None):
        FakeKlineCollector.calls.append((symbol, days))
        if FakeKlineCollector.fail_next:
            raise RuntimeError("kline source down")
        return _kline_rows()


@pytest.fixture(autouse=True)
def _reset_fake_kline():
    FakeKlineCollector.calls = []
    FakeKlineCollector.fail_next = False
    yield


def _eval(session_factory, **kwargs):
    """用测试 SessionLocal 跑一轮候选后验评估(K线用 FakeKlineCollector)。"""
    orig_session = ec.SessionLocal
    orig_collector = ec.KlineCollector
    ec.SessionLocal = session_factory
    ec.KlineCollector = FakeKlineCollector
    try:
        return ec.evaluate_entry_candidate_outcomes(**kwargs)
    finally:
        ec.SessionLocal = orig_session
        ec.KlineCollector = orig_collector


def _missing(session_factory, **kwargs):
    orig = ec.SessionLocal
    ec.SessionLocal = session_factory
    try:
        return ec.count_missing_candidate_outcomes(**kwargs)
    finally:
        ec.SessionLocal = orig


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

class TestDueOnlyAndOldestFirst:
    def test_only_due_unverified_horizons_get_outcomes(self, tmp_path, monkeypatch):
        """到期且未验证的 horizon 才产出 outcome; 未到期/今日新增/超窗口的不动。"""
        engine = _engine(tmp_path)
        factory = sessionmaker(bind=engine)
        session = factory()
        today = date.today()

        old_id = _mk_candidate(session, "600001", today - timedelta(days=10))  # h1/h3/h5/h10 全到期
        mid_id = _mk_candidate(session, "600002", today - timedelta(days=3))   # h1/h3 到期
        new_id = _mk_candidate(session, "600003", today)                       # 不可能到期
        stale_id = _mk_candidate(session, "600004", today - timedelta(days=50))  # 超 45 天窗口
        session.close()

        stats = _eval(factory)

        assert stats["total_candidates"] == 3  # 超窗口的 stale 不进入扫描集
        assert stats["evaluated"] == 6  # old: 4 + mid: 2
        assert stats["kline_failures"] == 0

        session = factory()
        rows = session.query(EntryCandidateOutcome).all()
        pairs = {(r.candidate_id, r.horizon_days) for r in rows}
        assert pairs == {
            (old_id, 1), (old_id, 3), (old_id, 5), (old_id, 10),
            (mid_id, 1), (mid_id, 3),
        }
        # 今日新增候选: 一个 outcome 都没有
        assert not any(r.candidate_id == new_id for r in rows)
        # 超窗口候选: 一个 outcome 都没有
        assert not any(r.candidate_id == stale_id for r in rows)
        # 数值正确: base=10.0, close=11.0 → +10%
        for r in rows:
            assert r.base_price == 10.0
            assert r.outcome_price == 11.0
            assert r.outcome_return_pct == pytest.approx(10.0)
            assert r.outcome_status == "evaluated"
        # 重跑幂等: 不再新增
        session.close()
        stats2 = _eval(factory)
        assert stats2["evaluated"] == 0
        assert stats2["missing_due_pairs_after"] == 0

    def test_oldest_due_candidate_wins_when_pool_exceeds_cap(self, tmp_path):
        """回归: limit 是单轮处理上限而非扫描上限 —— 最老的到期候选优先, 不丢候选。"""
        engine = _engine(tmp_path)
        factory = sessionmaker(bind=engine)
        session = factory()
        today = date.today()
        # 3 个候选同日快照(均 4 个 horizon 到期), score 区分先后; 单轮上限 1 → 三轮全验证
        ids = [
            _mk_candidate(session, f"6001{i:02d}", today - timedelta(days=10), score=float(90 - i))
            for i in range(3)
        ]
        session.close()

        rounds = []
        for _ in range(3):
            rounds.append(_eval(factory, limit=1)["evaluated"])
        assert rounds == [4, 4, 4]  # 每轮一个候选: h1/h3/h5/h10

        stats = _eval(factory)
        assert stats["evaluated"] == 0
        assert stats["missing_due_pairs_after"] == 0
        session = factory()
        pairs = {(r.candidate_id, r.horizon_days) for r in session.query(EntryCandidateOutcome).all()}
        assert pairs == {(cid, h) for cid in ids for h in (1, 3, 5, 10)}
        session.close()

    def test_fully_verified_candidate_skips_kline_fetch(self, tmp_path):
        """全部 horizon 已验证的候选不再拉 K 线。"""
        engine = _engine(tmp_path)
        factory = sessionmaker(bind=engine)
        session = factory()
        today = date.today()
        cid = _mk_candidate(session, "600101", today - timedelta(days=10))
        for h in (1, 3, 5, 10):
            session.add(EntryCandidateOutcome(
                candidate_id=cid,
                snapshot_date=(today - timedelta(days=10)).strftime("%Y-%m-%d"),
                stock_symbol="600101",
                stock_market="CN",
                candidate_source="market_scan",
                horizon_days=h,
                target_date=(today - timedelta(days=10 - h)).strftime("%Y-%m-%d"),
                base_price=10.0,
                outcome_price=11.0,
                outcome_return_pct=10.0,
                outcome_status="evaluated",
            ))
        session.commit()
        session.close()

        orig_collector = ec.KlineCollector
        ec.KlineCollector = FakeKlineCollector
        try:
            stats = _eval(factory)
        finally:
            ec.KlineCollector = orig_collector
        assert stats["evaluated"] == 0
        assert FakeKlineCollector.calls == []  # 一个 K 线请求都没有


class TestFailureHandling:
    def test_no_base_price_retried_in_place(self, tmp_path):
        """no_base_price(曾验证失败)的 (候选,horizon) 原地更新重试, 不重复插入。"""
        engine = _engine(tmp_path)
        factory = sessionmaker(bind=engine)
        session = factory()
        today = date.today()
        cid = _mk_candidate(session, "600201", today - timedelta(days=5))
        # 预置一条失败记录: h1 曾因无 base price 失败
        session.add(EntryCandidateOutcome(
            candidate_id=cid,
            snapshot_date=(today - timedelta(days=5)).strftime("%Y-%m-%d"),
            stock_symbol="600201",
            stock_market="CN",
            candidate_source="market_scan",
            horizon_days=1,
            target_date=(today - timedelta(days=4)).strftime("%Y-%m-%d"),
            base_price=None,
            outcome_price=11.0,
            outcome_return_pct=None,
            outcome_status="no_base_price",
        ))
        session.commit()
        session.close()

        stats = _eval(factory)
        assert stats["evaluated"] == 3  # h1 原地重试 + h3/h5 正常补验

        session = factory()
        rows = (
            session.query(EntryCandidateOutcome)
            .filter(EntryCandidateOutcome.candidate_id == cid, EntryCandidateOutcome.horizon_days == 1)
            .all()
        )
        assert len(rows) == 1  # 原地更新, 没有重复插入
        assert rows[0].outcome_status == "evaluated"
        assert rows[0].outcome_return_pct == pytest.approx(10.0)
        session.close()

    def test_kline_failure_counted_and_retried_next_round(self, tmp_path):
        """K 线拉取失败: 计数+不产出 outcome+下轮自动重试。"""
        engine = _engine(tmp_path)
        factory = sessionmaker(bind=engine)
        session = factory()
        today = date.today()
        cid = _mk_candidate(session, "600202", today - timedelta(days=5))
        session.close()

        orig_collector = ec.KlineCollector
        ec.KlineCollector = FakeKlineCollector
        try:
            FakeKlineCollector.fail_next = True
            stats = _eval(factory)
            assert stats["kline_failures"] >= 1
            assert stats["evaluated"] == 0

            session = factory()
            assert session.query(EntryCandidateOutcome).count() == 0  # 失败不留半成品
            session.close()

            # 数据源恢复 → 下轮补上
            FakeKlineCollector.fail_next = False
            stats2 = _eval(factory)
            assert stats2["kline_failures"] == 0
            assert stats2["evaluated"] == 3  # h1/h3/h5
            assert stats2["missing_due_pairs_after"] == 0
        finally:
            ec.KlineCollector = orig_collector


class TestMissingReport:
    def test_count_missing_candidate_outcomes(self, tmp_path):
        """只读缺口报告: per_horizon 缺口 + 最老样本。"""
        engine = _engine(tmp_path)
        factory = sessionmaker(bind=engine)
        session = factory()
        today = date.today()
        a_id = _mk_candidate(session, "600301", today - timedelta(days=10))  # 全缺 h1..h10
        b_id = _mk_candidate(session, "600302", today - timedelta(days=5))   # 只缺 h3/h5
        _mk_candidate(session, "600303", today)                              # 未到期, 不算缺口
        # b 已验 h1
        session.add(EntryCandidateOutcome(
            candidate_id=b_id,
            snapshot_date=(today - timedelta(days=5)).strftime("%Y-%m-%d"),
            stock_symbol="600302",
            stock_market="CN",
            candidate_source="market_scan",
            horizon_days=1,
            target_date=(today - timedelta(days=4)).strftime("%Y-%m-%d"),
            base_price=10.0,
            outcome_price=11.0,
            outcome_return_pct=10.0,
            outcome_status="evaluated",
        ))
        session.commit()
        session.close()

        rep = _missing(factory)
        assert rep["total_active_in_window"] == 3
        assert rep["missing_pairs"] == 6  # a:4 + b:2
        assert rep["per_horizon"] == {"1": 1, "3": 2, "5": 2, "10": 1}
        # 最老缺失候选排最前
        assert rep["samples"][0]["candidate_id"] == a_id
        assert rep["samples"][0]["missing_horizons"] == [1, 3, 5, 10]
        assert {s["candidate_id"] for s in rep["samples"]} == {a_id, b_id}

    def test_missing_report_zero_when_all_verified(self, tmp_path):
        """缺口为 0 = 所有到期候选均已后验(剩余缺口只剩未到期)。"""
        engine = _engine(tmp_path)
        factory = sessionmaker(bind=engine)
        session = factory()
        today = date.today()
        cid = _mk_candidate(session, "600303", today - timedelta(days=10))
        for h in (1, 3, 5, 10):
            session.add(EntryCandidateOutcome(
                candidate_id=cid,
                snapshot_date=(today - timedelta(days=10)).strftime("%Y-%m-%d"),
                stock_symbol="600303",
                stock_market="CN",
                candidate_source="market_scan",
                horizon_days=h,
                target_date=(today - timedelta(days=10 - h)).strftime("%Y-%m-%d"),
                base_price=10.0,
                outcome_price=11.0,
                outcome_return_pct=10.0,
                outcome_status="evaluated",
            ))
        session.commit()
        session.close()

        rep = _missing(factory)
        assert rep["missing_pairs"] == 0
        assert rep["samples"] == []
