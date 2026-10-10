# -*- coding: utf-8 -*-
"""连板梯队新鲜度自检 + 补数幂等回归(2026-10-11 P1 数据断档修复)。

背景: TQ 断链期(2026-10-08~09) limit_up_events 写入方静默失败, 表停在 20260930,
连板梯队页面几天不更新。回归目标:
- 期望最新交易日按**真实交易日历**(剔除"补班周末"偏差)判定;
- 缺失交易日枚举正确(节假日/周末不算缺口);
- 有缺口才补数(无缺口零副作用), 补完复检转绿; 补数 TQ 直连**只补缺失日**且幂等。

全 mock / 临时测试库, 禁真实网络。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import ladder_freshness as lf  # noqa: E402


# ─────────────── ① 期望交易日(真实日历, 剔除补班周末) ───────────────

@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 10, 11, 9, 0), "20261009"),  # 周日 → 回退上一交易日(跳过 10-10 补班周六)
        (datetime(2026, 10, 10, 9, 0), "20261009"),  # 补班周六: 日历记交易日, 但 A 股周末不开市
        (datetime(2026, 10, 9, 16, 0), "20261009"),  # 周五
        (datetime(2026, 10, 8, 16, 0), "20261008"),  # 周四(国庆后首日)
        (datetime(2026, 10, 3, 9, 0), "20260930"),   # 国庆假期中 → 回退到 9-30
        (datetime(2026, 9, 30, 17, 0), "20260930"),
        (datetime(2026, 9, 25, 9, 0), "20260924"),   # 中秋假期 → 回退
    ],
)
def test_expected_latest_trading_date(now, expected):
    assert lf.expected_latest_trading_date(now) == expected


# ─────────────── ② 缺失交易日枚举 ───────────────

def test_missing_trading_days_basic():
    assert lf.missing_trading_days("20260930", "20261009") == ["20261008", "20261009"]
    assert lf.missing_trading_days("20261008", "20261009") == ["20261009"]
    assert lf.missing_trading_days("20261009", "20261009") == []
    # 0925 是中秋假期, 不是交易日 → 不计入缺口
    assert lf.missing_trading_days("20260924", "20260930") == ["20260928", "20260929", "20260930"]


def test_missing_trading_days_bad_input():
    assert lf.missing_trading_days(None, "20261009") == []
    assert lf.missing_trading_days("20260930", "bogus") == []


# ─────────────── ③ check: 有/无缺口 + 空表诚实态 ───────────────

def test_check_stale_then_fresh():
    now = datetime(2026, 10, 11, 9, 0)
    stale = lf.check(now, latest_fn=lambda: "20260930")
    assert stale["ok"] is False and stale["stale"] is True
    assert stale["missing"] == ["20261008", "20261009"]

    fresh = lf.check(now, latest_fn=lambda: "20261009")
    assert fresh["ok"] is True and fresh["stale"] is False and fresh["missing"] == []


def test_check_empty_table_is_not_ok():
    """空表不以"无缺口"冒充, 显式 ok=False(不下结论)。"""
    r = lf.check(datetime(2026, 10, 11, 9, 0), latest_fn=lambda: None)
    assert r["ok"] is False and r["latest"] is None and r["stale"] is True


# ─────────────── ④ guard: 有缺口才补 + 补完复检 ───────────────

def test_guard_noop_when_fresh():
    calls = []
    r = lf.guard(datetime(2026, 10, 11, 9, 0),
                 latest_fn=lambda: "20261009",
                 backfill_fn=lambda days: calls.append(days) or {"ok": True, "saved": 0, "failed": 0})
    assert r["ok"] is True and r["backfilled"] == 0 and r["recovered"] is True
    assert calls == []  # 无缺口 → 零副作用


def test_guard_backfills_then_recovers():
    state = {"latest": "20260930"}
    seen = {}

    def _bf(days):
        seen["days"] = list(days)
        state["latest"] = "20261009"  # 补数后表推进
        return {"ok": True, "saved": 37, "failed": 0}

    r = lf.guard(datetime(2026, 10, 11, 9, 0), latest_fn=lambda: state["latest"], backfill_fn=_bf)
    assert seen["days"] == ["20261008", "20261009"]
    assert r["ok"] is True and r["recovered"] is True
    assert r["latest_before"] == "20260930" and r["latest"] == "20261009"
    assert r["backfilled"] == 37


def test_guard_still_stale_after_backfill_reports_not_ok():
    r = lf.guard(datetime(2026, 10, 11, 9, 0),
                 latest_fn=lambda: "20260930",
                 backfill_fn=lambda days: {"ok": False, "saved": 0, "failed": 5})
    assert r["ok"] is False and r["recovered"] is False
    assert r["backfilled"] == 0 and "仍缺" in (r["reason"] or "")


# ─────────────── ⑤ backfill_missing: 只补缺失日 + 幂等(测试库) ───────────────

_SYM = "009999"


def _bars():
    return [
        {"date": "20260930", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0},
        {"date": "20261008", "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0},
        {"date": "20261009", "open": 12.10, "high": 12.10, "low": 12.10, "close": 12.10},
    ]


@pytest.fixture()
def _cleanup_events():
    yield
    from sqlalchemy import text

    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM limit_up_events WHERE symbol = :s"), {"s": _SYM})
        db.commit()
    finally:
        db.close()


def test_backfill_missing_only_missing_days_and_idempotent(monkeypatch, _cleanup_events):
    import src.core.demon_factors as df

    monkeypatch.setattr(df, "_tq_bars_direct",
                        lambda sym, days, *, raise_on_error=False: _bars())

    first = lf.backfill_missing(["20261008", "20261009"], symbols=[_SYM], names={_SYM: "测试"})
    assert first["saved"] == 2 and first["failed"] == 0

    # 重跑幂等: 已存在的行不再写入
    second = lf.backfill_missing(["20261008", "20261009"], symbols=[_SYM], names={_SYM: "测试"})
    assert second["saved"] == 0

    from sqlalchemy import text

    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT trade_date FROM limit_up_events WHERE symbol = :s ORDER BY trade_date"),
            {"s": _SYM},
        ).fetchall()
    finally:
        db.close()
    assert [r[0] for r in rows] == ["20261008", "20261009"]  # 只落缺失日, 未越界


def test_backfill_missing_tq_failure_counted(monkeypatch, _cleanup_events):
    import src.core.demon_factors as df

    def _boom(sym, days, *, raise_on_error=False):
        raise RuntimeError("TQ 断链")

    monkeypatch.setattr(df, "_tq_bars_direct", _boom)
    r = lf.backfill_missing(["20261009"], symbols=[_SYM], names={})
    assert r["failed"] == 1 and r["saved"] == 0 and r["ok"] is False


def test_backfill_missing_no_days_noop():
    assert lf.backfill_missing([])["saved"] == 0
