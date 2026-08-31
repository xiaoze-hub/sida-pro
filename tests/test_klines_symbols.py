"""klines_ingestor.get_default_symbols 合并逻辑测试 (2026-08-24)。

业务背景:
- K线 18:00 cron 只拉自选股 -> 候选池当日新进的票没历史 K线,机会页只显示一天
- 修复: 把 entry_candidates 当日 distinct (stock_symbol, stock_market) 并入默认拉取列表
- 关键约束:
  * watchlist 与 candidates 按 (symbol, market) 去重
  * snapshot_date 必须按 CST(Asia/Shanghai) 的"今天"过滤,否则跨时区会漏拉/多拉
  * market 字段缺省 CN
  * watchlist 与 candidates 各自内部也要去重(多用户加同一只股 / 同日重复入池)
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.collectors import klines_ingestor


# ---------- mock helper ----------

def _make_query_mock(rows: list) -> MagicMock:
    """构造 SessionLocal() 的 mock query 链: filter/distinct/all 全部可链式调用。"""
    q = MagicMock()
    q.filter.return_value = q
    q.distinct.return_value = q
    q.all.return_value = rows
    return q


# ---------- 边界工具 ----------
def _make_session_mock(q_watch, q_cand):
    """构造 SessionLocal() 的 mock: with ... as db: 进入后返回 db 自身,且 db.query()
    按调用次序返回 (q_watch, q_cand)。
    """
    db = MagicMock()
    # 让 `with SessionLocal() as db` 的 db 仍是同一个 mock
    db.__enter__ = MagicMock(return_value=db)
    db.__exit__ = MagicMock(return_value=False)
    qlist = [q_watch, q_cand]
    calls = {"i": 0}

    def _side(*args, **kwargs):
        i = calls["i"]
        calls["i"] += 1
        return qlist[i]

    db.query.side_effect = _side
    return db




class _FakeStock:
    """Stock 行代理: .symbol / .market。"""
    def __init__(self, symbol: str, market: str):
        self.symbol = symbol
        self.market = market


class _FakeCandidate:
    """EntryCandidate 行代理。"""
    def __init__(self, stock_symbol: str, stock_market: str, snapshot_date: str):
        self.stock_symbol = stock_symbol
        self.stock_market = stock_market
        self.snapshot_date = snapshot_date


def _today_cst_str() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


# ---------- 测试 ----------

class TestMergeWatchlistAndCandidates:
    """get_default_symbols: 自选股 + 候选池去重合并。"""

    def test_watchlist_only_when_no_candidates(self, monkeypatch):
        """无 entry_candidates 时, 返回 watchlist 去重结果(多用户去重)。"""
        watchlist_rows = [
            _FakeStock("600519", "CN"),
            _FakeStock("002361", "CN"),
            # 多用户各加一只, 应被去重
            _FakeStock("002361", "CN"),
            _FakeStock("300750", "CN"),
        ]
        q_watch = _make_query_mock(watchlist_rows)
        q_cand = _make_query_mock([])  # 候选池空

        db = _make_session_mock(q_watch, q_cand)
        monkeypatch.setattr("src.web.database.SessionLocal", lambda: db)

        result = klines_ingestor.get_default_symbols()
        assert result == [("600519", "CN"), ("002361", "CN"), ("300750", "CN")]
        assert len(result) == 3  # watchlist 内 002361 重复被去重

    def test_candidates_merged_with_watchlist(self, monkeypatch):
        """候选池新进的票,即便不在 watchlist,也要被加入结果。"""
        watchlist_rows = [
            _FakeStock("600519", "CN"),
            _FakeStock("002361", "CN"),
        ]
        today = _today_cst_str()
        candidate_rows = [
            _FakeCandidate("002361", "CN", today),   # 与 watchlist 重复
            _FakeCandidate("688521", "CN", today),   # 新票
            _FakeCandidate("600487", "CN", today),   # 新票
            _FakeCandidate("688521", "CN", today),   # 候选池内部重复
        ]

        q_watch = _make_query_mock(watchlist_rows)
        q_cand = _make_query_mock(candidate_rows)

        db = _make_session_mock(q_watch, q_cand)
        monkeypatch.setattr("src.web.database.SessionLocal", lambda: db)

        result = klines_ingestor.get_default_symbols()
        # 顺序: 先 watchlist 已有, 再候选池新加入的(688521, 600487)
        assert result == [
            ("600519", "CN"),
            ("002361", "CN"),
            ("688521", "CN"),
            ("600487", "CN"),
        ]
        assert len(result) == 4

    def test_distinct_called_on_candidates(self, monkeypatch):
        """候选池查询必须 .distinct() 去重(同票多源入池产生多行)。"""
        today = _today_cst_str()
        q_watch = _make_query_mock([])
        q_cand = _make_query_mock([_FakeCandidate("002361", "CN", today)])

        db = _make_session_mock(q_watch, q_cand)
        monkeypatch.setattr("src.web.database.SessionLocal", lambda: db)

        klines_ingestor.get_default_symbols()

        assert q_cand.distinct.called, "候选池查询必须 .distinct() 去重"
        assert q_cand.filter.called, "候选池查询必须 .filter(snapshot_date=today)"

    def test_today_uses_cst_helper(self, monkeypatch):
        """get_default_symbols 必须调用 _today_cst() 而不是 datetime.now()。"""
        captured = {"called": 0, "value": None}

        def _fake_today_cst():
            captured["called"] += 1
            captured["value"] = "2026-08-24"
            return "2026-08-24"

        q_watch = _make_query_mock([])
        q_cand = _make_query_mock([])

        db = _make_session_mock(q_watch, q_cand)
        monkeypatch.setattr("src.web.database.SessionLocal", lambda: db)
        monkeypatch.setattr(klines_ingestor, "_today_cst", _fake_today_cst)

        klines_ingestor.get_default_symbols()
        assert captured["called"] >= 1
        assert captured["value"] == "2026-08-24"

    def test_empty_market_normalized_to_cn(self, monkeypatch):
        """候选池 stock_market 为空串时, 应规范为 'CN'(与列 default 一致)。"""
        class _BadCandidate:
            stock_symbol = "002361"
            stock_market = ""  # 空
            snapshot_date = _today_cst_str()

        q_watch = _make_query_mock([])
        q_cand = _make_query_mock([_BadCandidate()])

        db = _make_session_mock(q_watch, q_cand)
        monkeypatch.setattr("src.web.database.SessionLocal", lambda: db)

        result = klines_ingestor.get_default_symbols()
        assert ("002361", "CN") in result

    def test_all_empty_returns_empty_list(self, monkeypatch):
        """watchlist 与 candidates 都为空 -> 空 list, 不报错。"""
        q_watch = _make_query_mock([])
        q_cand = _make_query_mock([])

        db = _make_session_mock(q_watch, q_cand)
        monkeypatch.setattr("src.web.database.SessionLocal", lambda: db)

        result = klines_ingestor.get_default_symbols()
        assert result == []


class TestTodayCstHelper:
    """_today_cst 工具:返回 Asia/Shanghai 的今天 YYYY-MM-DD。"""

    def test_returns_iso_date_string(self):
        today = klines_ingestor._today_cst()
        assert isinstance(today, str)
        assert len(today) == 10
        assert today[4] == "-" and today[7] == "-"

    def test_cst_offset_handled(self, monkeypatch):
        """即使 host 物理时钟在 UTC 边界附近, _today_cst 也按 CST 取日期。

        模拟: datetime.now(tz=Asia/Shanghai) 返回 2026-08-24 01:30 → CST 当天 = 2026-08-24。
        不允许在边界情况下返回错误的"昨天"或"明天"。
        """
        from datetime import datetime as _dt

        fixed_cst = _dt(2026, 8, 24, 1, 30, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        class _FakeDatetime:
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return fixed_cst.replace(tzinfo=None)
                return fixed_cst.astimezone(tz)

        # klines_ingestor 在模块顶部 from datetime import datetime,
        # 因此 patch 模块符号就生效。
        monkeypatch.setattr(klines_ingestor, "datetime", _FakeDatetime)
        assert klines_ingestor._today_cst() == "2026-08-24"
