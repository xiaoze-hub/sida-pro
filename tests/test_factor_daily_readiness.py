"""因子日快照只有同日有效日线才可打目标日标签。"""
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src.collectors import factor_backfill, factor_daily


class _PoolSession:
    """只替代不存在于 SQLite 的 PostgreSQL 池查询，记录真实写入模型。"""

    def __init__(self, *symbols):
        self.symbols = symbols
        self.rows = []
        self.commits = 0

    def get_bind(self):
        return self

    @contextmanager
    def connect(self):
        yield self

    def execute(self, _query):
        return SimpleNamespace(fetchall=lambda: [(s,) for s in self.symbols])

    def query(self, _model):
        return SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(first=lambda: None))

    def add(self, row):
        self.rows.append(row)

    def commit(self):
        self.commits += 1


def _bars(last, count=70):
    last = date.fromisoformat(last)
    return [
        {"date": (last - timedelta(days=i)).isoformat(), "close": 10 + (count-i)/100,
         "high": 12.0, "low": 9.0, "volume": 1000 + i}
        for i in range(count - 1, -1, -1)
    ]


@pytest.mark.parametrize("last_day", ["2026-11-26", "2026-11-27"])
def test_daily_snapshot_output_requires_the_target_bar(monkeypatch, last_day):
    db = _PoolSession("600001")
    monkeypatch.setattr("src.db.klines_repo.load_qfq_bars", lambda *_args, **_kwargs: _bars(last_day))
    result = factor_daily.collect_once(db, trade_date="20261127")
    expected = int(last_day == "2026-11-27")
    assert result["written"] == len(db.rows) == expected
    if expected:
        assert db.rows[0].trade_date == "20261127"
        assert db.rows[0].momentum_20 is not None
        assert db.commits == 1
    else:
        assert db.commits == 0


def test_daily_explicit_date_computes_without_next_day_bar(monkeypatch):
    db = _PoolSession("600001")
    bars = _bars("2026-11-27")
    next_bar = {"date": "2026-11-30", "close": 50.0, "high": 50.0, "low": 50.0, "volume": 9000}
    monkeypatch.setattr("src.db.klines_repo.load_qfq_bars", lambda *_args, **_kwargs: [*bars, next_bar])
    assert factor_daily.collect_once(db, trade_date="20261127")["written"] == 1
    assert db.rows[0].momentum_20 == factor_daily.compute_factors(bars)["momentum_20"]


def test_daily_rejects_conflicting_explicit_trade_date_and_cutoff():
    result = factor_daily.collect_once(None, trade_date="20261127", asof="20261126")
    assert result == {"written": 0, "skipped": 0, "reason": "conflicting_trade_date"}


@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf")])
def test_target_bar_with_invalid_close_is_not_evidence(monkeypatch, value):
    db = _PoolSession("600001")
    bars = _bars("2026-11-27")
    bars[-1]["close"] = value
    monkeypatch.setattr("src.db.klines_repo.load_qfq_bars", lambda *_a, **_kw: bars)
    assert factor_daily.collect_once(db, asof="20261127") == {
        "written": 0, "skipped": 1, "reason": "no_samples",
    }
    assert not db.rows


def test_asof_alone_labels_the_snapshot_with_its_actual_day(monkeypatch):
    db = _PoolSession("600001")
    monkeypatch.setattr("src.db.klines_repo.load_qfq_bars", lambda *_a, **_kw: _bars("2026-11-26"))
    assert factor_daily.collect_once(db, asof="20261126")["written"] == 1
    assert db.rows[0].trade_date == "20261126"


def test_backfill_does_not_invent_row_for_missing_target_day(monkeypatch):
    db = _PoolSession("600001")
    monkeypatch.setattr(factor_backfill, "_trading_dates", lambda *_args, **_kwargs: ["20261126", "20261127"])
    monkeypatch.setattr("src.db.klines_repo.load_qfq_bars", lambda *_args, **_kwargs: _bars("2026-11-26"))
    result = factor_backfill.backfill(db)
    assert result["written"] == 1
    assert [row.trade_date for row in db.rows] == ["20261126"]
