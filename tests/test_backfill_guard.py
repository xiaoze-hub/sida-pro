# -*- coding: utf-8 -*-
"""回填防污染契约测试(v0.5.80 B3)。

来源是 v0.5.73 的真事故: 稀疏早年事件被当事实回填, EMA+2日确认跨日传播, 把阶段标签
和历史规律一起带偏。这里钉住四条: ①门槛判定与排除清单如实; ②缺日期的行不猜;
③清理只删"实际存在且不可信"的日期, 且 keep 为空时**拒绝执行**; ④表/列名不过白名单就抛。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import src.core.backfill_guard as bg


def test_check_ident_blocks_injection():
    assert bg.check_ident("market_phase_daily") == "market_phase_daily"
    assert bg.check_ident("trade_date") == "trade_date"
    for bad in ("t; DROP TABLE x", "a b", "1abc", "", "tbl-col", "tbl.col"):
        with pytest.raises(ValueError):
            bg.check_ident(bad)


def test_coverage_gate_reports_both_sides():
    counts = {"20240101": 3, "20240102": 60, "20240103": 50, "20240104": 0}
    ok, dropped = bg.coverage_gate(counts, 50)
    assert ok == ["20240102", "20240103"]
    assert dropped == ["20240101", "20240104"]      # 被排除的必须可见, 不能静默


def test_count_by_field_ignores_missing_dates():
    rows = [{"trade_date": "2026-01-05"}, {"trade_date": "2026-01-05"},
            {"trade_date": None}, {"trade_date": "  "}, {}, "不是字典"]
    assert bg.count_by_field(rows, "trade_date") == {"2026-01-05": 2}


@pytest.fixture()
def engine_with_phase_rows():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE market_phase_daily (date TEXT PRIMARY KEY, phase TEXT)"))
        # 库里日期带横杠, keep 集用紧凑格式 → 必须能对上(归一化比较)
        for d in ("2026-01-05", "2026-01-06", "2026-01-07", "2025-01-02"):
            c.execute(text("INSERT INTO market_phase_daily VALUES (:d,'repair')"), {"d": d})
    yield eng
    eng.dispose()


def _dates(engine):
    with engine.connect() as c:
        return sorted(r[0] for r in c.execute(text("SELECT date FROM market_phase_daily")))


def test_purge_deletes_only_untrusted_dates(engine_with_phase_rows):
    removed = bg.purge_rows_outside({"20260105", "20260107"},
                                    table="market_phase_daily", date_col="date",
                                    engine=engine_with_phase_rows)
    assert removed == 2
    assert _dates(engine_with_phase_rows) == ["2026-01-05", "2026-01-07"]


def test_purge_with_empty_keep_refuses_to_wipe_table(engine_with_phase_rows):
    """门槛配错导致 keep 为空时, 绝不能把整张表删空。"""
    assert bg.purge_rows_outside(set(), table="market_phase_daily", date_col="date",
                                 engine=engine_with_phase_rows) == 0
    assert len(_dates(engine_with_phase_rows)) == 4


def test_purge_chunks_deletes(engine_with_phase_rows, monkeypatch):
    """删除分片(避开 SQLite 变量数上限): 分片大小改小后结果不变。"""
    monkeypatch.setattr(bg, "DELETE_CHUNK", 1)
    assert bg.purge_rows_outside({"20260105"}, table="market_phase_daily", date_col="date",
                                 engine=engine_with_phase_rows) == 3
    assert _dates(engine_with_phase_rows) == ["2026-01-05"]


def test_purge_rejects_bad_identifiers(engine_with_phase_rows):
    with pytest.raises(ValueError):
        bg.purge_rows_outside({"20260105"}, table="market_phase_daily; DROP TABLE x",
                              date_col="date", engine=engine_with_phase_rows)
    with pytest.raises(ValueError):
        bg.purge_rows_outside({"20260105"}, table="market_phase_daily",
                              date_col="date) --", engine=engine_with_phase_rows)
