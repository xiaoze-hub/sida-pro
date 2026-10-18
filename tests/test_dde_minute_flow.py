# -*- coding: utf-8 -*-
"""分时突破逐分钟 DDE 大单序列(P3 补差 A, 2026-10-10) 单测。

覆盖:
  - 采样落库**幂等**(同 sample_ts upsert 不重复、delta 不双算)
  - delta 构造(区间增量 = 本 cum − 上一样本 cum; 当日首个样本 = 其 cum)
  - fetch_dde_series 读库构造**真序列**(解锁「突」)
  - 「突」『DDE大单持续流入』正/反例(**采样节拍粗于 1m 仍按样本判定**)
  - 非交易日 / 非时段 **显式无数据**
  - 采样作业诚实性(ok=False → failed)
禁真网络: 取数用 FakeL2(纯 pandas), DB 用内存 sqlite。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

_CST = ZoneInfo("Asia/Shanghai")


class FakeL2:
    """mock THSDKL2: 代码表 + 批量 DDE(纯 pandas, 不触网)。"""

    def __init__(self, codes=None, nets=None):
        self._codes = codes or []      # [(ths_code, name)]
        self._nets = nets or {}        # 6 位 → 主力净流入(元)

    def get_stock_cn_lists(self):
        return pd.DataFrame(
            {"代码": [c for c, _ in self._codes], "名称": [n for _, n in self._codes]}
        )

    def get_dde_flow(self, codelist, market="USZA", detail=False):
        rows = []
        for c in codelist.split(","):
            rows.append({
                "代码": f"{market}{c}",
                "主力净流入": float(self._nets.get(c, 0.0)),
                "主力净量": 0.01,
                "总金额": 1e7,
            })
        return pd.DataFrame(rows)


class EmptyL2:
    def get_stock_cn_lists(self):
        return pd.DataFrame({"代码": [], "名称": []})

    def get_dde_flow(self, *a, **k):
        return pd.DataFrame()


@pytest.fixture()
def dde_db(monkeypatch):
    """内存 sqlite: 建 app_jobs + dde_minute_flow + war_report_daily; 打补丁 session。"""
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    from src.web.migrations import (
        _m165_app_jobs_table,
        _m185_dde_minute_flow_table,
        _m186_war_report_daily_table,
    )

    with eng.begin() as conn:
        _m165_app_jobs_table(conn)
        _m185_dde_minute_flow_table(conn)
        _m186_war_report_daily_table(conn)

    import src.db.session as dbs

    monkeypatch.setattr(dbs, "engine", eng)
    monkeypatch.setattr(dbs, "SessionLocal", sessionmaker(bind=eng))
    return eng


# ────────────────────────── 阈值配置层 ──────────────────────────
def test_minute_dde_sample_threshold_default_and_env(monkeypatch):
    from src.core import thresholds

    assert thresholds.snapshot()["minute_dde_sample_min"]["source"] == "default"
    assert thresholds.value("minute_dde_sample_min") == 5.0
    monkeypatch.setenv("SIDA_THRESHOLD_MINUTE_DDE_SAMPLE_MIN", "3")
    assert thresholds.value("minute_dde_sample_min") == 3.0
    assert thresholds.source("minute_dde_sample_min") == "env"


# ────────────────────────── 采样落库 + delta + 幂等 ──────────────────────────
def test_dde_sample_writes_delta_and_idempotent(dde_db):
    from src.core import dde_sampler as s

    l2 = FakeL2(
        [("USZA000001", "A"), ("USHA600000", "B")],
        {"000001": 100e4, "600000": -50e4},
    )
    r1 = s.sample_dde_once(l2=l2, now=datetime(2026, 10, 9, 9, 35), sample_ts="09:35")
    assert r1["ok"] is True and r1["written"] == 2

    with dde_db.begin() as conn:
        rows = conn.execute(
            text("SELECT symbol, cum_net_wan, delta_net_wan FROM dde_minute_flow "
                 "WHERE sample_ts='09:35' ORDER BY symbol")
        ).fetchall()
    # 首个样本 delta = 其 cum(当日从开盘累计)
    d = {r[0]: (r[1], r[2]) for r in rows}
    assert d["000001"] == (100.0, 100.0)
    assert d["600000"] == (-50.0, -50.0)

    # 幂等: 重跑同一样本 → 不新增行, delta 不双算
    s.sample_dde_once(l2=l2, now=datetime(2026, 10, 9, 9, 35), sample_ts="09:35")
    with dde_db.begin() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM dde_minute_flow")).scalar()
    assert n == 2

    # 第二样本: cum 升到 160万 → delta = 60万
    l2._nets["000001"] = 160e4
    s.sample_dde_once(l2=l2, now=datetime(2026, 10, 9, 9, 40), sample_ts="09:40")
    with dde_db.begin() as conn:
        row = conn.execute(
            text("SELECT cum_net_wan, delta_net_wan FROM dde_minute_flow "
                 "WHERE symbol='000001' AND sample_ts='09:40'")
        ).fetchone()
    assert row[0] == 160.0 and row[1] == 60.0


def test_dde_sample_empty_source_returns_ok_false(dde_db):
    from src.core import dde_sampler as s

    out = s.sample_dde_once(l2=EmptyL2(), now=datetime(2026, 10, 9, 9, 35))
    assert out["ok"] is False and "空" in out["reason"]


# ────────────────────────── fetch_dde_series 读库构造真序列 ──────────────────────────
def test_fetch_dde_series_constructs_sequence_from_db(dde_db):
    from src.core.minute_breakthrough import fetch_dde_series

    today = datetime.now(_CST).strftime("%Y-%m-%d")
    with dde_db.begin() as conn:
        for ts, delta in (("09:35", 100.0), ("09:40", 60.0), ("09:45", 30.0)):
            conn.execute(
                text(
                    "INSERT INTO dde_minute_flow "
                    "(trade_date, market, symbol, sample_ts, cum_net_wan, delta_net_wan, source) "
                    "VALUES (:d, 'CN', '000001', :t, :c, :x, 'thsdk_dde')"
                ),
                {"d": today, "t": ts, "c": delta, "x": delta},
            )
    series = fetch_dde_series("000001")
    assert [x["time"] for x in series] == ["09:35", "09:40", "09:45"]
    # delta 万元 → 元
    assert series[0]["main_net"] == pytest.approx(100.0 * 1e4)
    assert series[-1]["main_net"] == pytest.approx(30.0 * 1e4)


# ────────────────────────── 腾讯逐笔逐分钟 DDE 兜底(2026-10-18) ──────────────────────────
def test_tick_dde_series_per_minute_main_net(monkeypatch):
    """腾讯逐笔 → 逐分钟主力净额: 剔除竞价、只算主力单、B/S 定符号、无主力单分钟记 0。"""
    from src.core import minute_breakthrough as mbt

    ticks = [
        {"t": "09:25:03", "amt": 900_000.0, "vol": 5000, "d": "B"},   # 竞价撮合 → 剔除
        {"t": "09:30:10", "amt": 250_000.0, "vol": 100, "d": "B"},    # 主力买
        {"t": "09:30:40", "amt": 300_000.0, "vol": 100, "d": "S"},    # 主力卖
        {"t": "09:31:05", "amt": 50_000.0, "vol": 10, "d": "B"},      # 非主力 → 该分钟记 0
        {"t": "09:32:00", "amt": 100_000.0, "vol": 700, "d": "B"},    # 金额不足但 ≥600 手 → 主力买
    ]
    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: True)
    monkeypatch.setattr("src.core.dark_flow._fetch_all_ticks", lambda code: ticks)

    series = mbt._fetch_tencent_tick_dde_series("600111", "CN")
    assert [x["time"] for x in series] == ["09:30", "09:31", "09:32"]
    assert series[0]["main_net"] == pytest.approx(250_000.0 - 300_000.0)
    assert series[1]["main_net"] == pytest.approx(0.0)  # 完整序列: 无主力单 ≠ 缺失
    assert series[2]["main_net"] == pytest.approx(100_000.0)


def test_tick_dde_series_skipped_on_non_trading_day(monkeypatch):
    """非交易日不取逐笔(腾讯返回"最近交易日", 套到今天即错日序列)。"""
    from src.core import minute_breakthrough as mbt

    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: False)
    monkeypatch.setattr(
        "src.core.dark_flow._fetch_all_ticks",
        lambda code: pytest.fail("非交易日不该翻逐笔"),
    )
    assert mbt._fetch_tencent_tick_dde_series("600111", "CN") == []


def test_fetch_dde_series_falls_back_to_tencent_tick(monkeypatch, dde_db):
    """采样表无行 → 腾讯逐笔序列兜底, 且不再白跑 thsdk/TQ 快照。"""
    from src.core import minute_breakthrough as mbt

    monkeypatch.setattr(mbt, "_read_dde_series_db", lambda *a, **k: [])
    monkeypatch.setattr(
        mbt,
        "_fetch_tencent_tick_dde_series",
        lambda *a, **k: [{"time": "09:30", "main_net": 1_000_000.0}],
    )
    monkeypatch.setattr(
        mbt, "_fetch_thsdk_dde_wan", lambda *a, **k: pytest.fail("不应回退快照")
    )

    series, source = mbt.fetch_dde_series_with_source("600111", "CN")
    assert source == "tencent_tick"
    assert series == [{"time": "09:30", "main_net": 1_000_000.0}]
    # 兼容入口仍只返回序列
    assert mbt.fetch_dde_series("600111", "CN") == series


def test_fetch_minute_breakthrough_degraded_note_is_factual(monkeypatch):
    """DDE 与分钟数据都空 → note 只陈述事实(序列为空), 不编造原因。"""
    from src.core import minute_breakthrough as mbt

    monkeypatch.setattr(mbt, "fetch_minute_bars", lambda *a, **k: _tu_bars())
    monkeypatch.setattr(mbt, "fetch_dde_series_with_source", lambda *a, **k: ([], None))

    out = mbt.fetch_minute_breakthrough("600111", "CN")
    assert out["available"] is False and out["dde_source"] is None
    assert "逐分钟 DDE 序列为空" in out["note"]
    assert "按铁律不出信号" in out["note"]


def test_fetch_dde_series_uses_tick_when_samples_too_few(monkeypatch, dde_db):
    """早盘采样只有 1-2 点(不足以判「持续」) → 用逐分钟逐笔补全, 不被短采样卡住。"""
    from src.core import minute_breakthrough as mbt

    short = [{"time": "09:35", "main_net": 1.0e6}, {"time": "09:40", "main_net": 2.0e6}]
    monkeypatch.setattr(mbt, "_read_dde_series_db", lambda *a, **k: list(short))
    monkeypatch.setattr(
        mbt,
        "_fetch_tencent_tick_dde_series",
        lambda *a, **k: [{"time": "09:30", "main_net": 5.0e5}, {"time": "09:31", "main_net": 6.0e5}],
    )

    series, source = mbt.fetch_dde_series_with_source("600111", "CN")
    assert source == "tencent_tick"
    assert len(series) == 2


def test_fetch_dde_series_keeps_short_samples_when_tick_unavailable(monkeypatch, dde_db):
    """逐笔不可用 → 短采样仍原样返回(显式降级但不丢已有证据)。"""
    from src.core import minute_breakthrough as mbt

    short = [{"time": "09:35", "main_net": 1.0e6}]
    monkeypatch.setattr(mbt, "_read_dde_series_db", lambda *a, **k: list(short))
    monkeypatch.setattr(mbt, "_fetch_tencent_tick_dde_series", lambda *a, **k: [])

    series, source = mbt.fetch_dde_series_with_source("600111", "CN")
    assert source == "minute_sample"
    assert series == short


# ────────────────────────── 「突」持续流入 正/反例(采样节拍粗于 1m) ──────────────────────────
def _mb(t, o, h, l, c, v):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _tu_bars():
    """16 分钟窄幅盘整(09:30..09:45) + 09:46 放量突破前高(同 P3 用例)。"""
    bars = [_mb(f"09:{30 + i:02d}", 10.0, 10.02, 9.99, 10.0, 1000) for i in range(16)]
    bars.append(_mb("09:46", 10.0, 10.4, 9.99, 10.35, 5000))
    return bars


def test_minute_breakthrough_dde_sustained_inflow_positive_sampled():
    """5min 采样点(非逐分钟)全部正流入 → 「突」触发(按样本判定持续)。"""
    from src.core.minute_breakthrough import compute_breakthrough

    dde = [
        {"time": "09:35", "main_net": 3_000_000},
        {"time": "09:40", "main_net": 3_000_000},
        {"time": "09:45", "main_net": 3_000_000},
    ]
    r = compute_breakthrough(_tu_bars(), dde)
    assert r["available"] is True and r["degraded"] is False
    assert r["signal_type"] == "突"
    assert "DDE大单持续流入" in r["met_conditions"]


def test_minute_breakthrough_dde_sustained_inflow_negative_sampled():
    """采样点含负流入 → 「突」不成立(持续流入条件挡住)。"""
    from src.core.minute_breakthrough import compute_breakthrough

    dde = [
        {"time": "09:35", "main_net": 3_000_000},
        {"time": "09:40", "main_net": -1_000_000},
        {"time": "09:45", "main_net": 3_000_000},
    ]
    r = compute_breakthrough(_tu_bars(), dde)
    assert r["available"] is True
    assert r["signal_type"] != "突"


def test_minute_breakthrough_dde_insufficient_samples_no_tu():
    """采样点不足 consec 个 → 不构成「持续流入」(不编造)。"""
    from src.core.minute_breakthrough import compute_breakthrough

    dde = [{"time": "09:45", "main_net": 3_000_000}]
    r = compute_breakthrough(_tu_bars(), dde)
    assert r["available"] is True
    assert r["signal_type"] != "突"


# ────────────────────────── 非交易时段显式无数据 ──────────────────────────
def test_dde_sample_job_non_trading_day_explicit():
    from src.core.dde_sampler import run_dde_sample_job

    out = run_dde_sample_job(now=datetime(2026, 10, 11, 10, 0))  # 周日
    assert out["ok"] is True and out["skipped"] == "non_trading_day"
    assert "无数据" in out["note"]
    assert "job_id" not in out  # 非交易时段不建作业行


def test_dde_sample_job_non_trading_time_explicit():
    from src.core.dde_sampler import run_dde_sample_job

    out = run_dde_sample_job(now=datetime(2026, 10, 9, 12, 0))  # 交易日午休
    assert out["ok"] is True and out["skipped"] == "non_trading_time"
    assert "无数据" in out["note"]


# ────────────────────────── 采样作业诚实性 ──────────────────────────
def test_dde_sample_job_failed_on_empty_source(dde_db):
    import src.core.jobs as J
    from src.core.dde_sampler import run_dde_sample_job

    out = run_dde_sample_job(now=datetime(2026, 10, 9, 10, 0), l2=EmptyL2())
    assert out["ok"] is False
    row = J.JobStore().get(out["job_id"])
    assert row["status"] == "failed"
    assert row["error"]


def test_dde_sample_job_succeeded_and_records(dde_db):
    import src.core.jobs as J
    from src.core.dde_sampler import JOB_KIND, run_dde_sample_job

    l2 = FakeL2([("USZA000001", "A")], {"000001": 10e4})
    out = run_dde_sample_job(now=datetime(2026, 10, 9, 10, 0), l2=l2)
    assert out["ok"] is True and out["written"] == 1
    row = J.JobStore().get(out["job_id"])
    assert row["status"] == "succeeded"
    assert row["kind"] == JOB_KIND
