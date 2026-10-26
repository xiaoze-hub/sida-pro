"""全市场情绪温度历史端点测试（假 SessionLocal，不连真 DB）。"""

import asyncio


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows=None, boom=False):
        self._rows, self._boom = rows or [], boom

    def execute(self, sql, params=None):
        if self._boom:
            raise RuntimeError("relation \"market_breadth_daily\" does not exist")
        return _Rows(self._rows)

    def close(self):
        pass


def _row(d, up, down, flat, syms, temp):
    # 顺序: trade_date, up, down, flat, symbols, adl, adr, arms, bti, bti_thrust, mcl, mcl_summation, stix, up_ratio, sentiment_score
    return (d, up, down, flat, syms, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0.0, temp)


def _call(monkeypatch, rows=None, boom=False):
    import src.db.session as dbs

    monkeypatch.setattr(dbs, "SessionLocal", lambda: _FakeSession(rows, boom))
    from src.web.api.market_data import market_breadth_history

    return asyncio.run(market_breadth_history(days=60))


def test_table_missing_is_honest(monkeypatch):
    out = _call(monkeypatch, boom=True)
    assert out["ok"] is False and out["reason"] == "no_table"
    assert out["items"] == [] and "hint" in out


def test_no_rows_is_honest(monkeypatch):
    out = _call(monkeypatch, rows=[])
    assert out["ok"] is False and out["reason"] == "no_data"


def test_ascending_and_percentile_and_coverage_note(monkeypatch):
    rows = [_row("2026-09-24", 458, 1618, 67, 2143, 39),
            _row("2026-09-23", 1525, 3156, 87, 4768, 42),
            _row("2026-09-22", 2034, 2632, 144, 4810, 52)]
    out = _call(monkeypatch, rows=rows)
    assert out["ok"] is True
    assert [i["date"] for i in out["items"]] == ["2026-09-22", "2026-09-23", "2026-09-24"]  # 升序可直接画线
    assert out["latest"]["date"] == "2026-09-24"
    # 样本 <5 天不给分位（3 天算百分位没有意义）-> None, 前端显示 '
    assert out["temperature_percentile"] is None
    assert "仅覆盖 2143 只" in out["note"]        # 覆盖度偏低必须显式警示
    assert out["coverage"]["symbols"] == 2143
    assert "自算" in out["caliber"] and "非实时" in out["caliber"]


def test_full_coverage_has_no_note(monkeypatch):
    out = _call(monkeypatch, rows=[_row("2026-09-24", 1120, 4306, 151, 5577, 30)])
    assert out["note"] == ""


def test_percentile_needs_enough_samples(monkeypatch):
    """样本 >=5 天才给分位; 39 在 5 天里最小 -> 20.0。"""
    rows = [_row("2026-09-24", 458, 1618, 67, 2143, 39),
            _row("2026-09-23", 1525, 3156, 87, 4768, 42),
            _row("2026-09-22", 2034, 2632, 144, 4810, 52),
            _row("2026-09-21", 1000, 3000, 100, 4900, 45),
            _row("2026-09-18", 900, 3100, 90, 4900, 47)]
    out = _call(monkeypatch, rows=rows)
    assert out["temperature_percentile"] == 20.0
