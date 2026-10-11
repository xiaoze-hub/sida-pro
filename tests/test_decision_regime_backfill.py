"""regime 历史回填（2026-10-11）的钉子。

背景: 生产决策账本 5415 行 regime **100% NULL**（旧样本早于 regime 落库改动），regime 分桶
退化为单桶，『53.85% 是否被状态平均掩盖』无法验证。本文件钉死历史回填口径与纪律:

  ① `resolve_regime_for_day` 与 `current_regime` **同源同映射**（signal_summary cycle 优先、
     market_phase phase 兜底、都缺 → unknown），只是按【信号日】取历史行而非最新行；
  ② 日期归一坑: 账本紧凑 `20260918` vs 快照 ISO `2026-09-18` 必须归一比较（直接 == 永远不等）；
  ③ `backfill_regime` 幂等可重跑: 已填行/不可解析行重跑都不动；**只填明确态**, unknown 留 NULL
     （不写 'unknown' 冒充已归集）；**不改写存储值**（trade_date 字面量保真）；
  ④ 缺列老库/缺失日降级: 不炸、计数显式；
  ⑤ cron: 18:36 注册 + 非交易日跳过（在 test_decision_backfill_cron 里锁）;
  ⑥ 分析脚本 `scripts/regime_bucket_analysis.py` 在回填后数据上真跑通（子进程, 禁真网络）。

禁真网络: 全部走内存 sqlite + 纯函数。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date as _date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import market_regime as mr
from src.core import decision_log as dl

ROOT = Path(__file__).resolve().parent.parent


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture()
def eng():
    """内存库: decision_log(+regime) + signal_summary_daily + market_phase_daily + 情绪序列。"""
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with e.begin() as conn:
        from src.web.migrations import _m175_decision_log, _m184_decision_log_regime
        from src.db.models import MarketPhaseDaily, SignalSummaryDaily

        _m175_decision_log(conn)
        _m184_decision_log_regime(conn)
        SignalSummaryDaily.__table__.create(conn)
        MarketPhaseDaily.__table__.create(conn)
    with e.begin() as conn:
        from src.web.migrations import _m178_market_sentiment_daily

        _m178_market_sentiment_daily(conn)
    return e


def _add_cycle(engine, day_iso: str, cycle: str, conf: str | None = "高") -> None:
    from src.db.models import SignalSummaryDaily

    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        s.add(SignalSummaryDaily(
            snapshot_date=day_iso, stock_market="CN",
            blocks={"sentiment": {"cycle": cycle, "confidence": conf}}, text="x",
        ))
        s.commit()
    finally:
        s.close()


def _add_phase(engine, day_iso: str, phase: str) -> None:
    from src.db.models import MarketPhaseDaily

    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        y, m, d = (int(x) for x in day_iso.split("-"))
        s.add(MarketPhaseDaily(date=_date(y, m, d), phase=phase))
        s.commit()
    finally:
        s.close()


def _add_sentiment(engine, day, *, lu=79, lo=28, ss=506.2, sf=298.63) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO market_sentiment_daily (trade_date, market, limit_up_count, "
                "limit_up_open_count, seal_success_money, seal_fail_money) "
                "VALUES (:d, 'CN', :lu, :lo, :ss, :sf)"
            ),
            {"d": day, "lu": lu, "lo": lo, "ss": ss, "sf": sf},
        )


def _insert(engine, *, trade_date: str, symbol: str = "002361", pid: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO decision_log (id, symbol, trade_date, price_at_signal, signal_kind) "
                "VALUES (:i, :s, :d, 10.0, 'resonance3')"
            ),
            {"i": pid, "s": symbol, "d": trade_date},
        )


def _regime_of(engine, pid: int):
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT regime FROM decision_log WHERE id=:i"), {"i": pid}
        ).scalar()


# ── ① 按信号日解析: 同源同映射 ─────────────────────────────────────────────────
def test_resolve_day_prefers_signal_summary_cycle(eng):
    """当日有 signal_summary → 主源(cycle → normalize_regime), 与 current_regime 同映射。"""
    _add_cycle(eng, "2026-09-18", "退潮")
    _add_phase(eng, "2026-09-18", "ignite")  # 兜底源在场也应被主源盖过
    r = mr.resolve_regime_for_day(eng, "20260918")  # 紧凑信号日
    assert r["available"] is True and r["regime"] == "ebb"
    assert r["source"] == "signal_summary" and r["cycle"] == "退潮" and r["confidence"] == "高"
    assert mr.regime_for_day_key(eng, "20260918") == "ebb"


def test_resolve_day_falls_back_to_market_phase(eng):
    """无 signal_summary → market_phase phase, 经 PHASE_TO_REGIME 映射(ignite→ferment)。"""
    _add_phase(eng, "2026-09-21", "ignite")
    r = mr.resolve_regime_for_day(eng, "2026-09-21")
    assert r["available"] is True and r["regime"] == "ferment" and r["source"] == "market_phase"


def test_resolve_day_missing_is_explicit_unknown(eng):
    """两源都无当日数据 → available=False + regime='unknown'(显式缺数据, 不猜态)。"""
    r = mr.resolve_regime_for_day(eng, "2026-09-25")
    assert r["available"] is False and r["regime"] == "unknown" and r["source"] == "none"
    assert "reason" in r
    assert mr.regime_for_day_key(eng, "2026-09-25") == "unknown"


def test_resolve_day_normalizes_both_date_formats(eng):
    """日期归一: 紧凑信号日 ↔ ISO 快照、ISO 信号日 ↔ 紧凑序列, 都能命中。"""
    _add_cycle(eng, "2026-09-22", "发酵")
    assert mr.regime_for_day_key(eng, "20260922") == "ferment"   # 紧凑信号日 → ISO 快照
    assert mr.regime_for_day_key(eng, "2026-09-22") == "ferment"  # ISO 信号日 → ISO 快照
    # 温度计证据(不参与 regime 判定): 紧凑 trade_date 序列按信号日取到
    _add_sentiment(eng, "20260922")
    r = mr.resolve_regime_for_day(eng, "2026-09-22")
    assert r["temperature"] is not None and r["temperature"]["available"] is True
    assert r["regime"] == "ferment"  # 温度只作证据, 不改 key


# ── ② backfill_regime: 幂等 / 只填明确态 / 值保真 ─────────────────────────────
def test_backfill_fills_only_resolved_and_is_idempotent(eng):
    _add_cycle(eng, "2026-09-18", "退潮")      # ebb
    _add_phase(eng, "2026-09-21", "repair")    # repair(兜底)
    _insert(eng, trade_date="20260918", pid=1)  # 有主源
    _insert(eng, trade_date="20260921", pid=2)  # 兜底源
    _insert(eng, trade_date="20260925", pid=3)  # 无快照 → 应留 NULL

    out = dl.backfill_regime(eng)
    assert out == {"scanned": 3, "filled": 2, "unresolved": 1, "skipped_no_column": 0}
    assert _regime_of(eng, 1) == "ebb"
    assert _regime_of(eng, 2) == "repair"
    assert _regime_of(eng, 3) is None, "缺当日快照 → 保持 NULL, 不写 unknown 冒充"

    # 幂等: 重跑不改已填行, 不双计; 不可解析行仍待补(unresolved 不变)
    out2 = dl.backfill_regime(eng)
    assert out2 == {"scanned": 1, "filled": 0, "unresolved": 1, "skipped_no_column": 0}
    assert _regime_of(eng, 1) == "ebb" and _regime_of(eng, 2) == "repair"

    # 存储值保真: trade_date 字面量不被改写
    with eng.connect() as conn:
        days = [r[0] for r in conn.execute(sa.text(
            "SELECT trade_date FROM decision_log ORDER BY id")).fetchall()]
    assert days == ["20260918", "20260921", "20260925"]


def test_backfill_skips_rows_with_existing_regime(eng):
    """已有 regime 的行不进 pending(不覆盖已归集态)。"""
    _add_cycle(eng, "2026-09-18", "退潮")
    dl.record_signal(eng, signal_kind="resonance3", symbol="K1", trade_date="20260918",
                     price=10.0, regime="发酵")  # 已显式写 ferment
    _insert(eng, trade_date="20260918", symbol="K2", pid=99)  # 另插一行 NULL
    out = dl.backfill_regime(eng)
    assert out["scanned"] == 1 and out["filled"] == 1
    assert _regime_of(eng, 99) == "ebb"
    with eng.connect() as conn:
        reg = conn.execute(sa.text("SELECT regime FROM decision_log WHERE symbol='K1'")).scalar()
    assert reg == "ferment", "已归集态不得被回填覆盖"


def test_backfill_missing_column_is_noop():
    """老库(仅 v175, 无 regime 列): 空跑, 计数显式, 不炸。"""
    e = create_engine("sqlite:///:memory:")
    with e.connect() as conn:
        from src.web.migrations import _m175_decision_log

        _m175_decision_log(conn)
    out = dl.backfill_regime(e)
    assert out == {"scanned": 0, "filled": 0, "unresolved": 0, "skipped_no_column": 1}


def test_backfill_unknown_day_uses_injected_resolver(eng):
    """缺失日降级: 注入解析器返回 unknown → 留 NULL; 单行解析抛异常也不拖垮整批。"""
    _add_cycle(eng, "2026-09-18", "退潮")
    _insert(eng, trade_date="20260918", pid=1)
    _insert(eng, trade_date="20260921", pid=2)

    def _resolver(_engine, day):
        if str(day).endswith("0921"):
            raise RuntimeError("boom-day")
        return "ebb"

    out = dl.backfill_regime(eng, day_resolver=_resolver)
    assert out["scanned"] == 2 and out["filled"] == 1 and out["unresolved"] == 1
    assert _regime_of(eng, 1) == "ebb" and _regime_of(eng, 2) is None


def test_backfill_then_stats_bucket(eng):
    """回填后 stats 的 regime 分桶真的非 unknown(端到端: 回填 → 分桶聚合)。"""
    _add_cycle(eng, "2026-09-18", "退潮")
    dl.record_signal(eng, signal_kind="resonance3", symbol="A", trade_date="20260918", price=10.0)
    dl.record_signal(eng, signal_kind="resonance3", symbol="B", trade_date="20260918", price=10.0)
    assert dl.backfill_regime(eng)["filled"] == 2
    st = dl.stats(eng, min_sample=1)
    buckets = {(r["signal_kind"], r["regime"]): r for r in st["regime_rows"]}
    assert ("resonance3", "ebb") in buckets and buckets[("resonance3", "ebb")]["n_total"] == 2
    assert ("resonance3", "unknown") not in buckets


# ── ③ cron 日作业: 非交易日跳过 / 交易日起作业 ────────────────────────────────
def test_regime_daily_job_skips_non_trading_day(monkeypatch):
    from src.core import trading_calendar as tc

    monkeypatch.setattr(tc, "is_trading_day", lambda d: False)
    spawned = {"n": 0}
    monkeypatch.setattr(dl, "spawn_regime_backfill",
                        lambda **k: spawned.__setitem__("n", spawned["n"] + 1))
    out = dl.regime_backfill_daily_job()
    assert out["skipped"] is True and out["ok"] is True and spawned["n"] == 0


def test_regime_daily_job_spawns_on_trading_day(monkeypatch):
    from src.core import trading_calendar as tc

    monkeypatch.setattr(tc, "is_trading_day", lambda d: True)
    monkeypatch.setattr(dl, "spawn_regime_backfill",
                        lambda **k: {"started": True, "job_id": "jr", **k})
    out = dl.regime_backfill_daily_job()
    assert out["started"] is True and out["job_id"] == "jr" and out["reason"] == "scheduled"


def test_regime_job_kind_distinct_from_outcomes():
    """两个回填作业 kind 必须不同: 否则单飞会互相顶掉。"""
    assert dl.REGIME_BACKFILL_JOB_KIND != dl.BACKFILL_JOB_KIND


# ── ④ 新样本必带 regime(任务2): 落库链对新行确实写 regime ─────────────────────
def test_new_resonance_row_lands_regime(eng):
    """resonance_scan 落库链: record_many 带 regime → 新行 regime 非空(不是 NULL)。"""
    n = dl.record_many(eng, [
        {"signal_kind": "resonance3", "symbol": "N1", "trade_date": "20260918",
         "price": 10.0, "regime": "ebb", "context": {"regime": "ebb"}},
    ], source="resonance_scan")
    assert n == 1
    with eng.connect() as conn:
        reg = conn.execute(sa.text("SELECT regime FROM decision_log WHERE symbol='N1'")).scalar()
    assert reg == "ebb", "新共振样本必须带 regime(任务2)"


def test_new_row_without_regime_is_null_then_backfilled(eng):
    """已知残余洞: 生成侧未传 regime 的新行(gs_cross 等)为 NULL —— 由回填作业逐日覆盖。"""
    dl.record_many(eng, [
        {"signal_kind": "gs_cross", "symbol": "G1", "trade_date": "20260918", "price": 10.0},
    ], source="market_scan_gs")
    with eng.connect() as conn:
        assert conn.execute(sa.text("SELECT regime FROM decision_log WHERE symbol='G1'")).scalar() is None
    _add_cycle(eng, "2026-09-18", "退潮")
    assert dl.backfill_regime(eng)["filled"] == 1
    with eng.connect() as conn:
        assert conn.execute(sa.text("SELECT regime FROM decision_log WHERE symbol='G1'")).scalar() == "ebb"


# ── ⑤ 分析脚本: 回填后数据上真跑通(子进程, 禁真网络) ─────────────────────────
def test_regime_bucket_analysis_script_runs_on_backfilled_fixture(tmp_path):
    """回填后的库落成临时 sqlite, 跑分析脚本, 断言输出含分桶表且小样本标『否(样本不足)』。

    独立构造文件库(内存库不可跨进程) —— 结构与生产一致: decision_log(regime 已回填) +
    signal_summary_daily(当日周期) + market_sentiment_daily(温度)。
    """
    from src.web.migrations import (
        _m175_decision_log, _m178_market_sentiment_daily, _m184_decision_log_regime,
    )

    db = tmp_path / "fixture.db"
    file_eng = create_engine(f"sqlite:///{db}")
    with file_eng.begin() as conn:
        _m175_decision_log(conn)
        _m184_decision_log_regime(conn)
        _m178_market_sentiment_daily(conn)
        from src.db.models import SignalSummaryDaily

        SignalSummaryDaily.__table__.create(conn)
    _add_cycle(file_eng, "2026-09-18", "退潮")
    _add_sentiment(file_eng, "20260918", lu=79, lo=28, ss=506.2, sf=298.63)
    dl.record_signal(file_eng, signal_kind="resonance3", symbol="A", trade_date="20260918", price=10.0)
    dl.record_signal(file_eng, signal_kind="resonance3", symbol="B", trade_date="20260918", price=10.0)
    assert dl.backfill_regime(file_eng)["filled"] == 2
    file_eng.dispose()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [sys.executable, "scripts/regime_bucket_analysis.py", "--db", f"sqlite:///{db}",
         "--kind", "resonance3", "--min-sample", "30"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "按情绪周期 regime 分桶" in out
    assert "ebb" in out, f"回填的 ebb 桶应出现在输出: {out}"
    assert "否(样本不足)" in out, "小样本桶必须显式样本不足, 不造数"
    assert "unknown" not in out.split("按情绪周期 regime 分桶")[1], "无 NULL regime 行退化桶"
