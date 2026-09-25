"""市场广度日序列采集/渲染测试（stub 假库，不连真 DB）。"""

import pytest

try:
    from src.collectors import market_breadth_series as M
    from src.core.market_breadth import BreadthBar
except ImportError:  # pragma: no cover
    from collectors import market_breadth_series as M  # type: ignore
    from core.market_breadth import BreadthBar  # type: ignore


class _FakeRow:
    def __init__(self, vals):
        self._v = vals

    def __getitem__(self, i):
        return self._v[i]


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeDb:
    """只记录 execute 的 SQL/参数；不连任何数据库。"""

    def __init__(self, rows=None):
        self.executed = []
        self.committed = False
        self.rolled_back = False
        self._rows = rows or []

    def execute(self, sql, params=None):
        self.executed.append((str(sql), params))
        return _FakeResult(self._rows)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _full_days(n=30, symbols=4000):
    return [
        BreadthBar(
            trade_date=f"2026-08-{i + 1:02d}",
            up=symbols // 2 + i,
            down=symbols // 2,
            flat=10,
            up_volume=1.0e9 + i,
            down_volume=8.0e8,
        )
        for i in range(n)
    ]


def _patch_loader(monkeypatch, bars):
    monkeypatch.setattr(M, "load_bars_from_pg", lambda db, days=M.DEFAULT_DAYS: bars)


def test_no_data_is_honest_and_writes_nothing(monkeypatch):
    db = _FakeDb()
    _patch_loader(monkeypatch, [])
    out = M.sync_breadth_series(db)
    assert out["ok"] is False and out["reason"] == "no_data" and out["written"] == 0
    assert db.executed == [] and db.committed is False


def test_thin_day_is_skipped_not_written(monkeypatch):
    db = _FakeDb()
    thin = [BreadthBar(trade_date="2026-09-24", up=200, down=200, flat=5,
                       up_volume=1.0, down_volume=1.0)]
    _patch_loader(monkeypatch, thin)
    out = M.sync_breadth_series(db)
    assert out["ok"] is True and out["written"] == 0 and out["skipped_thin"] == 1
    assert db.executed == []


def test_full_day_written_with_upsert_and_indicators(monkeypatch):
    db = _FakeDb()
    _patch_loader(monkeypatch, _full_days(30))
    out = M.sync_breadth_series(db)
    assert out["ok"] is True and out["written"] == 30 and out["skipped_thin"] == 0
    assert db.committed is True
    sql, params = db.executed[-1]
    assert "INSERT INTO market_breadth_daily" in sql and "ON CONFLICT (trade_date)" in sql
    assert params["symbols"] == 4039 and params["source"] == "pg_klines"
    assert params["adl"] is not None and params["up_count"] > 0
    assert params["bti_thrust"] in (0, 1)


def _patch_stored(monkeypatch, bars):
    monkeypatch.setattr(M, "_load_stored_bars", lambda db, days=M.DEFAULT_DAYS: bars)


def test_latest_with_percentile_flags_complete(monkeypatch):
    _patch_stored(monkeypatch, _full_days(30))
    out = M.latest_with_percentile(_FakeDb())
    assert out["ok"] is True and out["complete"] is True
    assert out["symbols"] == 4039
    assert "自研口径" in out["caliber"]
    assert out["sentiment_score"] is not None


def test_latest_with_percentile_thin_day_marked_incomplete(monkeypatch):
    _patch_stored(monkeypatch, [BreadthBar("2026-09-24", 100, 100, 0, 1.0, 1.0)])
    out = M.latest_with_percentile(_FakeDb())
    assert out["ok"] is True and out["complete"] is False


def test_render_text_shows_dash_for_missing_never_zero():
    txt = M.render_text({
        "ok": True, "trade_date": "2026-09-24", "up": 10, "down": 5, "flat": 1,
        "symbols": 16, "sentiment_score": None, "adl": None, "adl_pct": None,
        "adr": None, "adr_pct": None, "arms": None, "arms_pct": None,
        "bti": None, "bti_pct": None, "bti_thrust": False, "mcl": None, "mcl_pct": None,
        "mcl_summation": None, "stix": None, "stix_pct": None,
        "bars_used": 30, "complete": True,
        "caliber": "自研口径（教科书定义）；非通达信客户端公式，数值可能有差异",
    })
    assert "上涨 10 / 下跌 5 / 平盘 1" in txt
    assert "情绪温度：—/100" in txt
    assert txt.count("—") >= 7          # 六个指标 + 温度, 一律"—"而非 0
    assert "自研口径" in txt and "不是 0" in txt


def test_render_text_incomplete_day_warns():
    txt = M.render_text({
        "ok": True, "trade_date": "2026-09-24", "up": 1, "down": 1, "flat": 0,
        "symbols": 2, "complete": False, "bars_used": 5, "sentiment_score": 50,
    })
    assert "未收全" in txt


def test_render_text_bti_thrust_flagged():
    txt = M.render_text({
        "ok": True, "trade_date": "2026-09-24", "up": 1, "down": 1, "flat": 0,
        "symbols": 2, "complete": True, "bars_used": 30, "sentiment_score": 60,
        "bti": 63.0, "bti_pct": 90.0, "bti_thrust": True, "mcl": -1.0, "mcl_summation": -9.5,
    })
    assert "thrust" in txt and "累计 -9.50" in txt


def test_latest_with_percentile_no_stored_data_is_honest(monkeypatch):
    _patch_stored(monkeypatch, [])
    out = M.latest_with_percentile(_FakeDb())
    assert out["ok"] is False and out["reason"] == "no_data" and "sync_breadth_series" in out["hint"]


def test_sentiment_score_written_per_row(monkeypatch):
    """回归: 曾经 sentiment_score 只在 latest_summary 算, 落库全为 NULL。"""
    db = _FakeDb()
    _patch_loader(monkeypatch, _full_days(30))
    M.sync_breadth_series(db)
    params = [p for _s, p in db.executed if p]
    # 样本 <20 的前若干日分位确实算不出(该为 None, 这是对的); 近期必须都有值。
    assert params[-1]["sentiment_score"] is not None
    assert sum(1 for p in params if p["sentiment_score"] is not None) >= 10
    assert all(p["sentiment_score"] is not None for p in params[-10:])


def test_complete_judged_relative_to_recent_median(monkeypatch):
    """薄的最新日(远低于近期中位)必须 complete=False, 但其原始数字照常报出。"""
    bars = _full_days(30)                      # 每日 4010+ 只
    bars.append(BreadthBar("2026-09-24", up=1000, down=1100, flat=43, up_volume=1.0, down_volume=1.0))
    _patch_stored(monkeypatch, bars)
    out = M.latest_with_percentile(_FakeDb())
    assert out["ok"] is True
    assert out["complete"] is False            # 2143 < 4010*0.8 = 3208
    assert out["symbols"] == 2143 and out["trade_date"] == "2026-09-24"
    assert out["complete_threshold"] >= 3208
    assert "未收全" in M.render_text(out)


def test_complete_true_when_last_day_is_full(monkeypatch):
    _patch_stored(monkeypatch, _full_days(30))
    out = M.latest_with_percentile(_FakeDb())
    assert out["complete"] is True


def test_render_text_empty_summary_returns_blank():
    assert M.render_text({"ok": False, "reason": "no_data"}) == ""
    assert M.render_text({}) == ""
