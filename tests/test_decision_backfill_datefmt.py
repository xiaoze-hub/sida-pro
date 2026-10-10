"""决策账本回填日期格式错配(2026-10-10 生产实测 5294 行 0 填) 的回归钉子。

生产真形态: `decision_log.trade_date` 落库是**紧凑格式** `20260925`, 而 `_close_series_pg`
返回的序列日期是 **ISO** `2026-09-25` —— 回填里 `series[0][0] != str(day)` 两种字面量
永远不等 → 每行都跳过 → 0 填(命中率恒『样本不足』)。既有测试的假 provider 把两种格式
写成一致所以测不出。

本文件用真实形态钉死: 紧凑账本 + ISO 序列必须能填; 且不改写存储值(字面量保真)。
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from src.core import decision_log as dl


@pytest.fixture()
def eng():
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with e.begin() as conn:
        from src.web.migrations import _m175_decision_log

        _m175_decision_log(conn)
    return e


def _insert(conn, *, trade_date: str, base: float = 10.0, pid: int = 1) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO decision_log (id, symbol, trade_date, price_at_signal, signal_kind) "
            "VALUES (:i, '002361', :d, :p, 'resonance3')"
        ),
        {"i": pid, "d": trade_date, "p": base},
    )


_ISO_SERIES = [
    ("2026-09-25", 10.0),
    ("2026-09-28", 10.5),  # T+1 +5%
    ("2026-09-29", 10.2),  # T+2
    ("2026-09-30", 10.1),  # T+3
    ("2026-10-05", 9.9),   # T+4
    ("2026-10-06", 11.0),  # T+5
]


def _iso_provider(_engine, _symbol: str, _start_day: str):
    """模拟真实 _close_series_pg: ISO 日期序列(与紧凑账本日期必然不等的字面量)。"""
    return list(_ISO_SERIES)


def test_compact_trade_date_with_iso_series_fills(eng):
    """生产真形态: 账本紧凑 20260925 + 系列 ISO → 必须填上(修前此处 0 填)。"""
    with eng.begin() as conn:
        _insert(conn, trade_date="20260925", base=10.0, pid=1)
    res = dl.backfill_outcomes(eng, series_provider=_iso_provider)
    assert res["filled"] == 1, f"紧凑账本+ISO 系列应回填成功: {res}"
    with eng.connect() as conn:
        row = conn.execute(
            sa.text("SELECT ret_t1, hit_t1, ret_t5, hit_t5, trade_date FROM decision_log WHERE id=1")
        ).fetchone()
    assert row is not None
    ret_t1, hit_t1, ret_t5, hit_t5, trade_date = row
    assert ret_t1 == pytest.approx(0.05, abs=1e-6)  # (10.5-10)/10
    assert hit_t1 == 1
    assert ret_t5 == pytest.approx(0.10, abs=1e-6)  # (11.0-10)/10
    assert hit_t5 == 1
    assert trade_date == "20260925", "字面量保真: 存储值不得被改写成 ISO"


def test_iso_trade_date_still_fills(eng):
    """兼容: 账本若是 ISO 日期也照常能填(_norm_day 透传)。"""
    with eng.begin() as conn:
        _insert(conn, trade_date="2026-09-25", base=10.0, pid=2)
    res = dl.backfill_outcomes(eng, series_provider=_iso_provider)
    assert res["filled"] == 1


def test_norm_day_unit():
    assert dl._norm_day("20260925") == "2026-09-25"
    assert dl._norm_day("2026-09-25") == "2026-09-25"
    assert dl._norm_day(" 20260925 ") == "2026-09-25"
    assert dl._norm_day("garbage") == "garbage"  # 不猜不修, 原样透传


def test_mismatch_day_still_skipped(eng):
    """语义不变: 系列首根不是信号日当根(停牌/缺K线) 依旧不硬填。"""
    with eng.begin() as conn:
        _insert(conn, trade_date="20260925", base=10.0, pid=3)

    def _off_by_one(_e, _s, _d):
        return list(_ISO_SERIES[1:])  # 首根是 2026-09-28, 不是信号日

    res = dl.backfill_outcomes(eng, series_provider=_off_by_one)
    assert res["filled"] == 0
    with eng.connect() as conn:
        row = conn.execute(sa.text("SELECT ret_t1 FROM decision_log WHERE id=3")).fetchone()
    assert row[0] is None
