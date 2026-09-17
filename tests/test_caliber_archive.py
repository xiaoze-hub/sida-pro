"""B5 口径快照档案的钉子(2026-09-18)。

钉什么:
  ① 迁移 v174 幂等 + 唯一键(symbol, trade_date, source, field_key);
  ② record_symbol: 源不可用 → value=NULL + available=0 + 记 reason(不是 0!);
     源可用 → 每字段一行, 主字段缺失也留一行(便于回答"这天这个源说了什么");
  ③ 重复采集是 UPDATE 不是堆行(唯一键 + ON CONFLICT);
  ④ drift_series: 每源一条序列、**不合成单一权威数字**、跨源比较显式声明比的字段、
     没留痕的日期如实「该日未留痕」(不插值/不补 0)。
"""
from __future__ import annotations

import sqlalchemy as sa
import pytest

from src.web.api import caliber_archive as ca
from src.web.migrations import _m174_caliber_snapshots


@pytest.fixture()
def engine():
    eng = sa.create_engine("sqlite://")
    _m174_caliber_snapshots(eng.connect())  # SQLite: 迁移函数接受 Connection
    return eng


def _snap(*, l2=None, dark=None, em=None, notes=None):
    """构造 build_caliber_compare 的返回值(形状与真实端点一致)。"""
    notes = notes or {}

    def wrap(key, name, caliber, unit, fields):
        return {
            "key": key,
            "name": name,
            "caliber": caliber,
            "unit": unit,
            "available": any(f.get("value") is not None for f in fields),
            "fields": fields,
            "note": notes.get(key, ""),
        }

    sources = [
        wrap(
            "thsdk_l2",
            "明盘 L2",
            "分档汇总",
            "元",
            [] if l2 is None else [{"label": "主力净流入", "value": l2}, {"label": "撤买额", "value": None}],
        ),
        wrap(
            "tencent_dark",
            "暗盘逐笔",
            "逐笔主动",
            "元",
            []
            if dark is None
            else [
                {"label": "全量主动净额", "value": dark},
                {"label": "主力净额（≥20万）", "value": dark},
            ],
        ),
        wrap(
            "eastmoney_flow",
            "东财四档",
            "四档归类",
            "元",
            [] if em is None else [{"label": "主力净流入", "value": em}],
        ),
    ]
    return {
        "symbol": "002361",
        "sources": sources,
        "available_count": sum(1 for s in sources if s["available"]),
    }


def test_migration_is_idempotent(engine):
    with engine.connect() as conn:
        _m174_caliber_snapshots(conn)  # 第二次调用必须直接返回, 不报错
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(caliber_snapshots)"))}
    assert {"symbol", "trade_date", "source", "field_key", "value", "available", "reason", "quality"} <= cols


def test_record_unavailable_source_writes_null_not_zero(engine, monkeypatch):
    monkeypatch.setattr(
        "src.web.api.caliber_compare.build_caliber_compare",
        lambda symbol: _snap(l2=None, dark=None, em=None, notes={"thsdk_l2": "TQ 未返回数据"}),
    )
    res = ca.record_symbol(engine, "002361", trade_date="2026-09-18")
    assert res["rows"] == 3  # 三个源各留一条"这天没有"的痕
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT source, value, available, reason FROM caliber_snapshots")).fetchall()
    for _src, value, available, reason in rows:
        assert value is None, "取不到必须写 NULL"
        assert available == 0
        assert reason  # 必须写清为什么没有
    assert res["available_by_source"] == {"thsdk_l2": 0, "tencent_dark": 0, "eastmoney_flow": 0}


def test_record_available_and_idempotent(engine, monkeypatch):
    monkeypatch.setattr(
        "src.web.api.caliber_compare.build_caliber_compare",
        lambda symbol: _snap(l2=100.0, dark=200.0, em=300.0),
    )
    ca.record_symbol(engine, "002361", trade_date="2026-09-18")
    with engine.connect() as conn:
        first = conn.execute(sa.text("SELECT COUNT(*) FROM caliber_snapshots")).scalar()
    # 同一天再采一次: 走 UPDATE, 行数不涨
    monkeypatch.setattr(
        "src.web.api.caliber_compare.build_caliber_compare",
        lambda symbol: _snap(l2=111.0, dark=200.0, em=300.0),
    )
    ca.record_symbol(engine, "002361", trade_date="2026-09-18")
    with engine.connect() as conn:
        second = conn.execute(sa.text("SELECT COUNT(*) FROM caliber_snapshots")).scalar()
        l2_rows = conn.execute(
            sa.text(
                "SELECT value FROM caliber_snapshots WHERE source='thsdk_l2' AND field_key='主力净流入'"
            )
        ).fetchall()
    assert first == second, "重复采集必须 UPDATE, 不能堆行"
    assert [float(r[0]) for r in l2_rows] == [111.0], "值应被刷新为新值"


def test_record_marks_suspect_quality(engine, monkeypatch):
    monkeypatch.setattr(
        "src.web.api.caliber_compare.build_caliber_compare",
        lambda symbol: _snap(dark=1.0, notes={"tencent_dark": "⚠️ 该源自标「数据可疑」"}),
    )
    ca.record_symbol(engine, "002361", trade_date="2026-09-18")
    with engine.connect() as conn:
        q = conn.execute(
            sa.text("SELECT DISTINCT quality FROM caliber_snapshots WHERE source='tencent_dark'")
        ).fetchall()
    assert [r[0] for r in q] == ["suspect"], "源自标可疑必须如实透传, 不能吞掉"


def test_drift_series_is_per_source_and_not_averaged(engine, monkeypatch):
    monkeypatch.setattr(
        "src.web.api.caliber_compare.build_caliber_compare",
        lambda symbol: _snap(l2=100.0, dark=150.0, em=200.0),
    )
    monkeypatch.setattr(ca, "_cst_today", lambda: "2026-09-18")
    ca.record_symbol(engine, "002361", trade_date="2026-09-18")

    out = ca.drift_series(engine, "002361", days=30)
    assert out["archived_days"] == 1
    day = out["series"][0]
    # 每源各说各的 —— 100 / 150 / 200 都在, 没有被平均成 150
    assert day["sources"]["thsdk_l2"]["value"] == 100.0
    assert day["sources"]["tencent_dark"]["value"] == 150.0
    assert day["sources"]["eastmoney_flow"]["value"] == 200.0
    assert "value" not in out, "顶层不许出现单一'权威数字'"

    comp = {(c["left"]["source"], c["right"]["source"]): c for c in out["comparisons"]}
    pair = comp[("thsdk_l2", "eastmoney_flow")]
    assert pair["both_available_days"] == 1
    assert pair["mean_abs_diff"] == 100.0
    assert pair["left"]["field"] == "主力净流入" and pair["right"]["field"] == "主力净流入"
    assert "口径差异" in pair["note"]


def test_drift_series_marks_missing_day_honestly(engine, monkeypatch):
    monkeypatch.setattr(ca, "_cst_today", lambda: "2026-09-18")
    out = ca.drift_series(engine, "002361", days=30)
    assert out["series"] == []
    assert out["archived_days"] == 0
    # 没有任何比较数据时不许编出 0 差异
    for c in out["comparisons"]:
        assert c["mean_abs_diff"] is None
        assert c["max_abs_diff"] is None
        assert c["both_available_days"] == 0


def test_archive_symbols_default_and_cap(engine):
    codes = ca.get_archive_symbols(engine)
    assert codes == list(ca.DEFAULT_ARCHIVE_SYMBOLS)
    assert len(codes) <= ca.MAX_SYMBOLS_PER_RUN
