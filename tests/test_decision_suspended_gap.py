"""决策账本**停牌跨期口径标注**(2026-10-15)的钉子 —— 加 t_n_span、标 suspended_gap、排除统计。

生产实证: 账本 T+n 取「信号日起第 n 根**可用** K 线」, 遇长期停牌会把一个月后的收盘当 T+1 ——
symbol=300082 信号日 20260918, 下一根可用 K 线是 2026-10-13(相隔 11 个交易日), `ret_t1=-0.0886`
实为一月后跌幅。用户裁决: **不重算存量数值, 只标注 + 排除统计**。

本文件锁死:
  ① 迁移 v198 幂等加 `t_n_span` 列; 老库(仅 v175/v197)降级不炸;
  ② 跨度口径: 走交易日历(紧凑/ISO 归一, 1=次一交易日), 取不到 → None(不猜);
  ③ 回填同步写 span; span>5 → `outcome_state='suspended_gap'`(覆盖原状态), **收益列原值不动**;
  ④ 胜率/分桶统计(stats / regime_rows / signal_quality / query_log)**排除** suspended_gap,
     并显式标注"含 N 条停牌跨期样本已排除"(N 从库里数);
  ⑤ 存量已填行的 span 由 `backfill_spans` 一次性**幂等**回填(重跑无副作用)。

禁令: 断言强度不得降低; 测试**不触真实网络/真实库**(假 K 线 provider + 内存 sqlite)。
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from src.core import decision_log as dl
from src.core import signal_quality as sq
from src.db.models import Stock
from src.web.migrations import (
    _m175_decision_log,
    _m197_decision_log_outcome_state,
    _m198_decision_log_t_n_span,
)

GAP = dl.OUTCOME_SUSPENDED_GAP


@pytest.fixture(autouse=True)
def _clear_col_cache():
    """按引擎 id 的列存在性缓存会跨用例残留 → 每个用例前清一次。"""
    for c in (dl._state_col_cache, dl._regime_col_cache, dl._span_col_cache):
        c.clear()
    yield
    for c in (dl._state_col_cache, dl._regime_col_cache, dl._span_col_cache):
        c.clear()


def _engine(*, with_state: bool = True, with_span: bool = True, with_regime: bool = False):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m175_decision_log(conn)
        if with_regime:
            from src.web.migrations import _m184_decision_log_regime

            _m184_decision_log_regime(conn)
        if with_state:
            _m197_decision_log_outcome_state(conn)
        if with_span:
            _m198_decision_log_t_n_span(conn)
    return eng


def _iso(day: str) -> str:
    s = str(day).strip()[:10]
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _provider(bars: dict[str, list[tuple[str, float]]]):
    def _p(_engine, symbol: str, start_day: str):
        start = _iso(start_day)
        return [b for b in bars.get(symbol, []) if _iso(b[0]) >= start]

    return _p


def _insert(eng, *, symbol: str, trade_date: str, price: float | None = 10.0,
            kind: str = "resonance3", ret_t1: float | None = None) -> None:
    cols = "symbol, trade_date, price_at_signal, signal_kind"
    vals = ":s, :d, :p, :k"
    params = {"s": symbol, "d": trade_date, "p": price, "k": kind}
    if ret_t1 is not None:
        cols += ", ret_t1, hit_t1"
        vals += ", :r, :h"
        params["r"] = ret_t1
        params["h"] = 1 if ret_t1 > 0 else 0
    with eng.begin() as conn:
        conn.execute(sa.text(f"INSERT INTO decision_log ({cols}) VALUES ({vals})"), params)


def _row(eng, symbol: str):
    with eng.connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT ret_t1, outcome_state, outcome_note, t_n_span "
                "FROM decision_log WHERE symbol = :s"
            ),
            {"s": symbol},
        ).fetchone()


# ─────────────────── ① 迁移 v198 ───────────────────


def test_migration_198_idempotent_and_adds_span_column():
    eng = _engine(with_span=False)
    with eng.begin() as conn:
        _m198_decision_log_t_n_span(conn)
        _m198_decision_log_t_n_span(conn)  # 再调一次不能报错
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(decision_log)"))}
    assert "t_n_span" in cols
    dl._span_col_cache.clear()  # 迁移前若探过列会缓存 False, 加列后清一次再验
    assert dl._has_t_n_span_column(eng) is True


def test_migration_198_skips_when_table_absent():
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        _m198_decision_log_t_n_span(conn)
    assert dl._has_t_n_span_column(eng) is False


# ─────────────────── ② 跨度口径 ───────────────────


def test_trading_span_counts_calendar_days_and_normalizes():
    # 生产实证: 20260918 → 2026-10-13 (跨中秋+国庆) = 11 个交易日
    assert dl._trading_span("20260918", "2026-10-13") == 11
    # 正常票: 次一交易日 → 1
    assert dl._trading_span("2026-09-16", "2026-09-17") == 1
    # 紧凑/ISO 归一后同值(不做归一会永远算不出)
    assert dl._trading_span("20260917", "2026-09-18") == 1
    # 5 个交易日以内(含 5)不算跨期
    assert dl._trading_span("2026-09-16", "2026-09-23") == 5


def test_trading_span_returns_none_when_unavailable():
    assert dl._trading_span("bad", "2026-10-13") is None
    assert dl._trading_span("2026-10-13", "2026-09-18") is None  # 取值日在前 → 无跨度
    assert dl._trading_span("2031-01-02", "2031-01-03") is None  # 日历未覆盖年份 → 不猜


# ─────────────────── ③ 回填同步写 span + 标跨期 ───────────────────


def test_backfill_writes_span_one_for_normal_row():
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16")
    dl.backfill_outcomes(
        eng,
        series_provider=_provider(
            {"AAA": [("2026-09-16", 10.0), ("2026-09-17", 11.0), ("2026-09-18", 10.0)]}
        ),
    )
    r = _row(eng, "AAA")
    assert r[3] == 1, "正常票 T+1 取值日=次一交易日 → span=1"
    assert r[1] is None, "正常行不该被标注"


def test_backfill_marks_suspended_gap_and_keeps_return_value():
    """生产形态(300082): 信号日 20260918 的下一根可用 K 线是一个月后 → 标跨期, 收益不动。"""
    eng = _engine()
    _insert(eng, symbol="300082", trade_date="20260918")
    dl.backfill_outcomes(
        eng,
        series_provider=_provider({"300082": [("2026-09-18", 10.0), ("2026-10-13", 9.114)]}),
    )
    r = _row(eng, "300082")
    assert r[3] == 11, "跨度必须按交易日历数(跨中秋+国庆)=11"
    assert r[0] == pytest.approx(-0.0886), "收益列保持原值(不重算、不改写)"
    assert r[1] == GAP
    assert r[2] and "11 个交易日" in r[2], "note 必须注明跨度"


def test_span_leq_max_not_marked():
    """一周以内的休整(span<=5)不算跨期: 不标 suspended_gap。"""
    eng = _engine()
    _insert(eng, symbol="BBB", trade_date="2026-09-16")
    dl.backfill_outcomes(
        eng,
        series_provider=_provider({"BBB": [("2026-09-16", 10.0), ("2026-09-23", 11.0)]}),
    )
    r = _row(eng, "BBB")
    assert r[3] == 5 and r[1] is None, "span=5 不超阈值 → 不标跨期"


def test_backfill_legacy_without_span_column_degrades():
    """仅 v197(无 t_n_span 列): 照常回填, 不写跨度, 不因缺列炸。"""
    eng = _engine(with_span=False)
    assert dl._has_t_n_span_column(eng) is False
    _insert(eng, symbol="AAA", trade_date="2026-09-16")
    out = dl.backfill_outcomes(
        eng, series_provider=_provider({"AAA": [("2026-09-16", 10.0), ("2026-09-17", 11.0)]})
    )
    assert out["filled"] == 1
    with eng.connect() as conn:
        got = conn.execute(sa.text("SELECT ret_t1 FROM decision_log WHERE symbol='AAA'")).scalar()
    assert float(got) == pytest.approx(0.1)


# ─────────────────── ④ 胜率/分桶统计排除 + 标注条数 ───────────────────


def _build_one_normal_one_gap(eng):
    """造一行正常(span=1, 命中) + 一行跨期(span=11, 未命中), 同 kind 同窗口。"""
    _insert(eng, symbol="OK1", trade_date="2026-09-16")
    _insert(eng, symbol="300082", trade_date="20260918")
    dl.backfill_outcomes(
        eng,
        series_provider=_provider(
            {
                "OK1": [("2026-09-16", 10.0), ("2026-09-17", 11.0)],
                "300082": [("2026-09-18", 10.0), ("2026-10-13", 9.114)],
            }
        ),
    )


def test_stats_excludes_suspended_gap_from_rate_and_annotates_count():
    eng = _engine()
    _build_one_normal_one_gap(eng)
    st = dl.stats(eng, days=400, min_sample=1)
    row = next(r for r in st["rows"] if r["signal_kind"] == "resonance3")
    # n_total 报全量(含跨期行), 让读者看得到全样本
    assert row["n_total"] == 2
    # 命中分母只算正常行(跨期行被排除)
    assert row["horizons"]["t1"]["n"] == 1
    assert row["horizons"]["t1"]["hit_rate"] == 1.0
    # 显式标注条数(N 从库里数)
    assert st["no_data"]["suspended_gap"]["total"] == 1
    assert st["no_data"]["suspended_gap"]["rows_by_kind"] == {"resonance3": 1}
    assert "含 1 条停牌跨期样本已排除" in st["note"]
    assert "含 1 条停牌跨期样本已排除" in st["no_data"]["suspended_gap"]["note"]


def test_regime_rows_exclude_suspended_gap():
    eng = _engine(with_regime=True)
    _build_one_normal_one_gap(eng)
    with eng.begin() as conn:
        conn.execute(sa.text("UPDATE decision_log SET regime = 'ebb'"))
    st = dl.stats(eng, days=400, min_sample=1)
    b = next(r for r in st["regime_rows"] if r["regime"] == "ebb")
    assert b["n_total"] == 2, "n_total 仍是全量"
    assert b["horizons"]["t1"]["n"] == 1, "分桶命中率同样排除跨期行"


def test_stats_legacy_without_state_column_unchanged():
    """只有 v175(无 outcome_state): 不排除(无处可排), 但也不炸。"""
    eng = _engine(with_state=False, with_span=False)
    _insert(eng, symbol="AAA", trade_date="2026-09-16")
    dl.backfill_outcomes(
        eng, series_provider=_provider({"AAA": [("2026-09-16", 10.0), ("2026-09-17", 11.0)]})
    )
    st = dl.stats(eng, days=400, min_sample=1)
    assert st["rows"][0]["horizons"]["t1"]["n"] == 1
    assert st["no_data"]["suspended_gap"]["total"] == 0
    assert st["regime_rows"] == []


def test_query_log_exposes_span_and_excluded_count_keeps_rows():
    eng = _engine()
    _build_one_normal_one_gap(eng)
    page = dl.query_log(eng)
    assert page["total"] == 2, "明细不删行(审计), 跨期标注行照常列出"
    assert page["suspended_gap_excluded"] == 1
    assert "含 1 条停牌跨期样本已排除" in page["note"]
    by_sym = {it["symbol"]: it for it in page["items"]}
    assert by_sym["300082"]["outcome_state"] == GAP
    assert by_sym["300082"]["t_n_span"] == 11
    assert by_sym["300082"]["outcomes"]["t1"]["ret"] == pytest.approx(-0.0886)
    assert by_sym["OK1"]["t_n_span"] == 1 and by_sym["OK1"]["outcome_state"] is None


# ─────────────────── ⑤ 存量已填行的 span 一次性幂等回填 ───────────────────


def test_backfill_spans_fills_existing_rows_and_marks_gap():
    eng = _engine()
    # 已填但缺 spanned 的存量行(模拟 v198 之前已回填的)
    _insert(eng, symbol="OK1", trade_date="2026-09-16", ret_t1=0.1)
    _insert(eng, symbol="300082", trade_date="20260918", ret_t1=-0.0886)
    out = dl.backfill_spans(
        eng,
        series_provider=_provider(
            {
                "OK1": [("2026-09-16", 10.0), ("2026-09-17", 11.0)],
                "300082": [("2026-09-18", 10.0), ("2026-10-13", 9.114)],
            }
        ),
    )
    assert out == {"scanned": 2, "filled": 2, "suspended_gap": 1, "skipped_no_column": 0, "no_series": 0}
    ok = _row(eng, "OK1")
    gap = _row(eng, "300082")
    assert ok[3] == 1 and ok[1] is None
    assert gap[3] == 11 and gap[1] == GAP
    assert gap[0] == pytest.approx(-0.0886), "存量收益列必须保持原值不动"


def test_backfill_spans_idempotent_and_skips_when_column_absent():
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16", ret_t1=0.1)
    prov = _provider({"AAA": [("2026-09-16", 10.0), ("2026-09-17", 11.0)]})
    first = dl.backfill_spans(eng, series_provider=prov)
    assert first["filled"] == 1
    second = dl.backfill_spans(eng, series_provider=prov)
    assert second["scanned"] == 0 and second["filled"] == 0, "幂等: 重跑无副作用"

    legacy = _engine(with_state=False, with_span=False)
    assert dl.backfill_spans(legacy)["skipped_no_column"] == 1


def test_backfill_spans_leaves_span_null_when_series_missing():
    """K 线取不到(信号日那根已不在)→ 留空待下次, 不猜跨度。"""
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16", ret_t1=0.1)
    out = dl.backfill_spans(eng, series_provider=_provider({}))
    assert out["scanned"] == 1 and out["filled"] == 0 and out["no_series"] == 1
    assert _row(eng, "AAA")[3] is None


# ─────────────────── ⑥ 自选池信号质量统计同样排除 ───────────────────


def test_signal_quality_excludes_suspended_gap():
    eng = _engine(with_regime=True)
    Stock.__table__.create(eng)
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO stocks (user_id, symbol, name, market) VALUES ('u1','OK1','OK1','CN')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO stocks (user_id, symbol, name, market) VALUES ('u1','300082','G','CN')"
            )
        )
    _build_one_normal_one_gap(eng)
    res = sq.watchlist_signal_quality(eng, user_id="u1", days=400, min_sample=1)
    assert res["available"] is True
    sig = res["signals"][0]
    assert sig["n_total"] == 2
    assert sig["horizons"]["t1"]["n"] == 1, "自选池胜率同样排除停牌跨期行"
    assert res["suspended_gap_excluded"] == 1
    assert "含 1 条停牌跨期样本已排除" in res["note"]
