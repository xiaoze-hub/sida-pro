"""决策账本「盘前分析」账：潜伏表解析 + 真实信号 + 事后表现（2026-11-11）。

钉三件诚实口径：
① 表格解析不到 → parsed=false、不补标的；
② 信号取不到 → null（前端显示无数据），不补 0；
③ 事后表现只用收盘价；止损只有写成具体价位才判定，条件式止损明说「未判定」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.db.models import AnalysisHistory
from src.web.api import decisions as dec

REPORT = """
事件驱动前瞻（双轨制 · 潜伏表）
| 事件 | 受益板块 | 代码 | 名称 | 受益逻辑 | 强度 | 埋伏方式 | 分批计划 | 止损 | 目标 | 证伪条件 |
|---|---|---|---|---|---|---|---|---|---|---|
| PCB涨价 | 覆铜板 | 600183 | 生益科技 | 龙头 | A | 回调埋伏 | 1/3 | 35.60 | +10% | 提价落空 |
| 化工提价 | 化学制品 | 600722 | 金牛化工**【妖股先锋·极妖】** | 股性记忆 | B+ | 竞价观察 | 1/4 | -5% | +8% | 品种无关 |
"""


class _User:
    id = "u-test"
    role = "owner"


PREOPEN_RAW = {
    # 有带时区采样时点的冻结凭证 → 这一期才算「盘前候选」, 事后表现才对账
    "timestamp": "2026-11-11T08:20:00+08:00",
    "demon_factors_frozen_at": "2026-11-11T08:20:00+08:00",
    "demon_factors_preopen": True,
    "demon_factors": {
        "600183": {"total": 88.0, "grade": "妖", "factor_date": "20261110"},
    },
}


def _seed(db, date="2026-11-11", content=REPORT, raw_data=None):
    rec = AnalysisHistory(
        user_id=None,
        agent_name="premarket_outlook",
        stock_symbol="*",
        analysis_date=date,
        title=f"【盘前分析】{date}",
        content=content,
        raw_data=raw_data or {},
        status="success",
    )
    db.add(rec)
    db.commit()
    return rec


@pytest.fixture()
def patched(monkeypatch):
    """把端点用的 SessionLocal / 收盘序列换成测试替身。"""
    from src.db.session import SessionLocal, get_read_engine
    from src.core import decision_log

    monkeypatch.setattr("src.db.session.SessionLocal", SessionLocal)
    monkeypatch.setattr("src.db.session.get_read_engine", get_read_engine)
    series = {
        "600183": [("2026-11-10", 10.0), ("2026-11-11", 10.5), ("2026-11-12", 10.2),
                   ("2026-11-13", 10.8), ("2026-11-14", 11.3)],
        "600722": [("2026-11-10", 5.0), ("2026-11-11", 5.2), ("2026-11-12", 5.1),
                   ("2026-11-13", 5.3), ("2026-11-14", 5.4)],
    }
    monkeypatch.setattr(decision_log, "_close_series_pg", lambda eng, sym, start: series.get(sym, []))
    return series


def _clean_db():
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM analysis_history WHERE agent_name='premarket_outlook'"))
        db.commit()
    finally:
        db.close()


def test_endpoint_returns_parsed_rows_with_signals_and_outcome(patched):
    _clean_db()
    from src.db.session import SessionLocal

    db = SessionLocal()
    _seed(db, raw_data=PREOPEN_RAW)
    db.close()

    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    assert out["available"] is True
    item = out["items"][0]
    assert item["date"] == "2026-11-11" and item["parsed"] is True
    rows = {r["code"]: r for r in item["rows"]}
    assert set(rows) == {"600183", "600722"}

    sur = rows["600183"]
    # 收盘 10.5 vs 前收 10.0 → 当日 +5%
    assert sur["outcome"]["t0"] == pytest.approx(5.0, abs=0.01)
    # T+1: 10.2 vs 10.5 → -2.86%
    assert sur["outcome"]["t1"] == pytest.approx(-2.86, abs=0.02)
    # 止损 35.60 是价位 → 按收盘价判定(全部远低于 35.6 → True)
    assert sur["outcome"]["stop_breached"] is True
    assert sur["stop_price"] == 35.60
    # 信号块即使取不到也要有结构（不补 0）
    assert "demon" in sur["signals"] and "price" in sur["signals"]

    hua = rows["600722"]
    assert hua["outcome"]["stop_breached"] is None
    assert "条件式" in (hua["outcome"]["note"] or "")
    assert hua["tags"] and "妖股先锋" in hua["tags"][0]


def test_endpoint_without_table_is_not_parsed(patched):
    _clean_db()
    from src.db.session import SessionLocal

    db = SessionLocal()
    _seed(db, date="2026-11-10", content="今日无事件驱动前瞻，只有指数点评。")
    db.close()

    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    item = out["items"][0]
    assert item["parsed"] is False
    assert item["rows"] == [] and item["row_count"] == 0


def test_outcome_for_missing_day_is_null_not_zero():
    seq = [("2026-11-09", 10.0), ("2026-11-10", 10.5)]
    o = dec._outcome_for(seq, "2026-11-11", None)
    assert o["t0"] is None and o["t1"] is None and o["t3"] is None
    assert "无K线" in o["note"]


def test_outcome_for_empty_series_is_null():
    o = dec._outcome_for([], "2026-11-11", 10.0)
    assert o["t0"] is None and o["stop_breached"] is None
    assert o["note"] == "无日线数据"


def test_endpoint_no_records_is_available_false(patched):
    _clean_db()
    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    assert out["available"] is False
    assert out["items"] == []
