# -*- coding: utf-8 -*-
"""高开分档统计(v0.5.80 A7)测试。

钉住四条: ①档位边界与缺数处理; ②"开盘即涨停买不进"必须剔除(否则结论建立在假交易上);
③样本不足 / 前后半段不同向 → 不许给判断; ④SQL 读路径在真库(SQLite 窗口函数)上跑得通。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.db.session as dbs
from src.core.gap_study import (
    GAP_BUCKETS, MIN_SAMPLES, bucket_of, is_unfillable, run_study, summarize,
)


def _row(gap, r_day, r_next=None, sym="600001", day="2026-01-05"):
    return {"symbol": sym, "date": day, "gap_pct": gap, "r_day": r_day, "r_next": r_next}


def _rows(n, gap, r_day, r_next=None, month="01"):
    """造 n 行同档位观测, 日期落在指定月份(便于把样本分到前/后半段)。"""
    return [_row(gap, r_day, r_next, day=f"2026-{month}-{3 + i:02d}") for i in range(n)]


def test_bucket_boundaries_and_missing():
    assert bucket_of(None) is None
    assert bucket_of(float("nan")) is None
    assert bucket_of(10000.0) is None          # 越界不塞进两端档
    assert bucket_of(-0.5) == "低开"
    assert bucket_of(0.0) == "平开"            # 左闭
    assert bucket_of(1.99) == "平开"
    assert bucket_of(2.0) == "小幅高开"
    assert bucket_of(9.4) == "强高开"
    assert bucket_of(9.5) == "一字附近高开"
    assert [b[0] for b in GAP_BUCKETS] == [b["bucket"] for b in summarize([])["buckets"]]


def test_unfillable_only_when_open_at_limit_price():
    assert is_unfillable("600001", 10.0, 11.00) is True      # 主板一字开
    assert is_unfillable("600001", 10.0, 10.99) is True      # 容一分内四舍五入
    assert is_unfillable("600001", 10.0, 10.90) is False
    assert is_unfillable("300001", 10.0, 11.00) is False     # 创业板 20% → 11% 还能买
    assert is_unfillable("300001", 10.0, 12.00) is True
    assert is_unfillable("999999", 10.0, 11.00) is False     # 识别不了的代码不武断剔除
    assert is_unfillable("600001", None, 11.0) is False


def test_summarize_labels_only_when_enough_and_time_stable():
    good = _rows(MIN_SAMPLES, 6.0, -1.8, -2.5)               # 前后半段同为负 → 可判
    big = {b["bucket"]: b for b in summarize(good)["buckets"]}["大幅高开"]
    assert big["enough_samples"] is True and big["stable"] is True
    assert big["verdict"] == "negative"
    assert big["day"]["n"] == MIN_SAMPLES and big["day"]["mean"] == pytest.approx(-1.8)
    assert big["next"]["mean"] == pytest.approx(-2.5)

    thin = {b["bucket"]: b for b in summarize(_rows(10, 6.0, -1.8, -2.5))["buckets"]}["大幅高开"]
    assert thin["verdict"] == "insufficient"                 # 样本不足 → 不下结论

    flip = _rows(40, 6.0, -2.0, month="01") + _rows(40, 6.0, +2.0, month="06")
    b = {x["bucket"]: x for x in summarize(flip)["buckets"]}["大幅高开"]
    assert b["enough_samples"] is True and b["stable"] is False
    assert b["verdict"] == "unstable"                        # 够样本但前后不同向, 不许贴徽标


def test_summarize_missing_next_day_stays_none():
    b = {x["bucket"]: x for x in summarize(_rows(MIN_SAMPLES, 6.0, -1.8))["buckets"]}["大幅高开"]
    assert b["day"]["n"] == MIN_SAMPLES
    assert b["next"]["n"] == 0 and b["next"]["mean"] is None   # 没有次日样本 ≠ 收益为 0


def test_run_study_reports_unavailable_instead_of_empty_table(monkeypatch):
    """读不到数据时必须 available=False + 原因, 不能给一张空表冒充"没有风险"。"""
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("src.core.gap_study._load_observations", boom)
    out = run_study(days=60)
    assert out["available"] is False and "db down" in out["reason"] and out["buckets"] == []


@pytest.fixture()
def memory_klines(monkeypatch):
    """真 SQLite + 窗口函数读路径(顺带证明这段 SQL 在 PG/SQLite 两边都成立)。

    日期必须落在 run_study 的窗口内, 所以按"今天往前推"生成。
    """
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    d0 = date.today() - timedelta(days=20)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE klines (symbol TEXT, ts TEXT, open REAL, high REAL, "
                       "low REAL, close REAL, period TEXT, adjust TEXT, market TEXT)"))
        # 600001 主板: 连续 8 天高开 6% 后当日收跌(可成交)
        prev = 10.0
        for i in range(8):
            op = round(prev * 1.06, 2)
            cl = round(op * 0.97, 2)
            c.execute(text("INSERT INTO klines VALUES (:s,:t,:o,:h,:l,:c,:p,:a,:m)"),
                      {"s": "600001", "t": (d0 + timedelta(days=i)).isoformat(),
                       "o": op, "h": op, "l": cl, "c": cl, "p": "1d", "a": "qfq", "m": "CN"})
            prev = cl
        # 600002: 第二天开盘即涨停(11.0 = 10.0×1.10) → 买不进, 必须被剔除
        c.execute(text("INSERT INTO klines VALUES "
                       "('600002',:t1,10.0,10.0,9.8,10.0,'1d','qfq','CN')"),
                  {"t1": d0.isoformat()})
        c.execute(text("INSERT INTO klines VALUES "
                       "('600002',:t2,11.0,11.0,10.6,10.6,'1d','qfq','CN')"),
                  {"t2": (d0 + timedelta(days=1)).isoformat()})
    monkeypatch.setattr(dbs, "SessionLocal", sessionmaker(bind=eng))
    yield eng
    eng.dispose()


def test_load_and_run_from_db_excludes_unfillable(memory_klines):
    out = run_study(days=60, min_samples=5)
    assert out["available"] is True, out.get("reason")
    assert out["excluded_limit_up"] == 1                      # 600002 那天买不进去
    assert out["universe"]["symbols"] == 1                    # 只剩 600001
    big = {b["bucket"]: b for b in out["buckets"]}["大幅高开"]
    assert big["day"]["n"] >= 1 and big["day"]["mean"] < 0    # 高开 6% 当日平均收跌
    assert "非全 A 股" in out["universe"]["note"]              # 样本偏差必须随结果一起给
