"""TQ 市场情绪日序列: 解析/落库/基线 与 网关参数契约。

fixture `tq_sc_series_sample.json` 是从真实 TQ 网关抓下来的 **未改写** 原始响应
(20260813~20260923, 30 个交易日, 34 张表), 所以这些断言同时是数据口径的回归锁。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from src.collectors import tq_sentiment_series as tss

FIXTURE = Path(__file__).parent / "fixtures" / "tq_sc_series_sample.json"


@pytest.fixture
def sample_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def patched_series(monkeypatch, sample_payload):
    """把 vendor 的 sc_series 换成 fixture, 隔离网络。"""
    import marketdata.vendors.tq as tqv

    calls: list[dict] = []

    def _fake(tables, *, start_time="", end_time="", code=""):
        calls.append({"tables": list(tables), "start_time": start_time, "end_time": end_time})
        return {t: v for t, v in sample_payload.items() if t in set(tables)}

    monkeypatch.setattr(tqv, "sc_series", _fake)
    return calls


def test_fixture_is_utf8_pipe_valid(sample_payload):
    """fixture 自检: 34 张表 / 窗口 30 交易日 —— 防止被误改成空文件。"""
    assert len(sample_payload) == 34
    dates = sorted({r["Date"] for r in sample_payload["SC3"]})
    assert len(dates) == 30
    assert dates[0] == "20260813" and dates[-1] == "20260923"


def test_parse_maps_official_sc_numbers(patched_series):
    """SC 编号 → 列名 的口径锁(官方 SCJYVALUE 文档)。"""
    rows = tss.fetch_sentiment_series(start_time="20260801")
    by_date = {r["trade_date"]: r for r in rows}

    latest = by_date["20260923"]
    # SC3 = 沪深涨停股个数: 涨停/曾涨停(炸板)
    assert latest["limit_up_count"] == 34
    assert latest["limit_up_open_count"] == 22
    # SC23 = 连板家数: 含ST及未开板新股 / 不含
    assert latest["streak_count"] == 15
    assert latest["streak_count_ex"] == 15
    # SC24 = 涨跌停股个数(不含ST及新股)
    assert latest["hard_limit_up_count"] == 34
    assert latest["hard_limit_down_count"] == 6

    # SC15 = 打板资金 封板成功/失败(亿元) —— 2026-09-22 失败额首次超过成功额
    d22 = by_date["20260922"]
    assert d22["seal_success_money"] == pytest.approx(423.39)
    assert d22["seal_fail_money"] == pytest.approx(561.59)
    assert d22["seal_fail_money"] > d22["seal_success_money"]

    # SC16 = 龙虎榜 买入总金额/卖出总金额(亿元)
    assert by_date["20260922"]["lhb_buy"] > 0
    assert by_date["20260922"]["lhb_sell"] > 0
    # SC1 = 两融余额(万元) 量级自检: 万亿级应落在 1e8 万元之上
    assert by_date["20260922"]["margin_fin_balance"] > 1e8
    # SC27 = 央行公开市场净投放(亿元), 可为负
    assert "pboc_net_injection" in by_date["20260923"]
    # extra 收敛未标定的 SC28-35(注意: 扩展表并非每个交易日都有值,
    # 20260923 只有 SC30/SC33/SC35, SC28 的末日是 20260922 —— 缺失就是缺失, 不补 0)
    ex_latest = json.loads(latest.get("extra") or "{}")
    assert set(ex_latest) <= {f"SC{i}" for i in range(28, 36)}
    assert set(ex_latest) == {"SC30", "SC33", "SC35"}
    ex_22 = json.loads(by_date["20260922"].get("extra") or "{}")
    assert "SC28" in ex_22 and len(ex_22["SC28"]) == 2


def test_rows_are_sorted_and_int_columns_are_int(patched_series):
    rows = tss.fetch_sentiment_series(start_time="20260801")
    dates = [r["trade_date"] for r in rows]
    assert dates == sorted(dates)
    for r in rows:
        for col in tss._INT_COLUMNS:
            if r.get(col) is not None:
                assert isinstance(r[col], int), f"{col} 应为 int, 实际 {type(r[col])}"


def test_sync_upserts_idempotently(patched_series):
    """同一份数据跑两次不应产生重复行(唯一键 trade_date+market)。"""
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        first = tss.sync_sentiment_series(db, start_time="20260801")
        assert "error" not in first, first
        assert first["rows"] == 30
        n1 = db.execute(text("SELECT COUNT(*) FROM market_sentiment_daily")).scalar()
        second = tss.sync_sentiment_series(db, start_time="20260801")
        assert second["rows"] == 30
        n2 = db.execute(text("SELECT COUNT(*) FROM market_sentiment_daily")).scalar()
        assert n1 == n2 == 30

        row = db.execute(
            text(
                "SELECT limit_up_count, limit_up_open_count, streak_count "
                "FROM market_sentiment_daily WHERE trade_date='20260923' AND market='CN'"
            )
        ).first()
        assert row == (34, 22, 15)
    finally:
        # 清理, 避免污染其他测试对 market_sentiment_daily 的计数
        db.execute(text("DELETE FROM market_sentiment_daily"))
        db.commit()
        db.close()


def test_baseline_computes_pct_rank(patched_series):
    """基线: 最新值 vs 近 20 交易日均值 + 分位。涨停家数从 105 掉到 34 应落在低分位。"""
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        tss.sync_sentiment_series(db, start_time="20260801")
        out = tss.latest_with_baseline(db, days=20)
        assert out["asof"] == "20260923"
        assert out["latest"]["trade_date"] == "20260923"
        assert out["sample_days"] == 20

        b = out["baseline"]["limit_up_count"]
        assert b["current"] == 34 and b["current_date"] == "20260923"
        assert b["mean"] > 34, "近 20 日均值应高于退潮日 34 家"
        assert b["pct_rank"] < 50, "退潮日涨停家数应处于低分位"
        assert b["max"] >= 105
    finally:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        db.commit()
        db.close()


def test_baseline_survives_column_absent_on_latest_row(patched_series):
    """回归锁: SC1(两融)/SC15(打板资金) 比 SC3 晚一天发布, 最新行这两列是 None。

    早期实现按"最新整行有值才给基线" → 这两列基线整体丢失。现在逐列独立取
    各自最近非空值, 必须仍然给出 baseline 且 current_date = T-1。
    """
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        tss.sync_sentiment_series(db, start_time="20260801")
        rows = db.execute(text(
            "SELECT seal_success_money, margin_fin_balance FROM market_sentiment_daily "
            "WHERE trade_date='20260923'")).first()
        assert rows == (None, None), "fixture 前提: 最新日这两列本就为空"

        out = tss.latest_with_baseline(db, days=20)
        bs = out["baseline"]["seal_success_money"]
        assert bs["current_date"] == "20260922", "应回落到 T-1 的最新值"
        assert bs["current"] == pytest.approx(423.39)
        assert bs["mean"] > 0
        # 打板失败资金同样要有基线(2026-09-22 失败额已超成功额)
        bf = out["baseline"]["seal_fail_money"]
        assert bf["current"] == pytest.approx(561.59)
        assert bf["current"] > out["baseline"]["seal_success_money"]["current"]
    finally:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        db.commit()
        db.close()


def test_spine_excludes_non_trading_days(monkeypatch):
    """回归锁: SC13(分红)/SC14(募资)/SC27(央行净投放) 按自然日发布, 会把周末塞进行集。

    行集必须以 SC3(每个交易日必有值)为骨架, 否则周末的 NULL 情绪列会污染 20 日均值基线。
    """
    import marketdata.vendors.tq as tqv

    payload = {
        # 骨架: 只有 2 个交易日
        "SC3": [
            {"Date": "20260918", "Value": ["79.00", "28.00"]},
            {"Date": "20260922", "Value": ["64.00", "29.00"]},
        ],
        # 自然日序列, 含周末 20260919(六)/20260920(日)
        "SC27": [
            {"Date": "20260918", "Value": ["-500.00", "0.00"]},
            {"Date": "20260919", "Value": ["0.00", "0.00"]},
            {"Date": "20260920", "Value": ["1200.00", "0.00"]},
            {"Date": "20260922", "Value": ["350.00", "0.00"]},
        ],
        # 骨架外日期的打板资金也应被丢弃
        "SC15": [
            {"Date": "20260920", "Value": ["1.00", "2.00"]},
            {"Date": "20260922", "Value": ["423.39", "561.59"]},
        ],
    }
    monkeypatch.setattr(
        tqv,
        "sc_series",
        lambda tables, *, start_time="", end_time="", code="": {
            t: v for t, v in payload.items() if t in set(tables)
        },
    )
    rows = tss.fetch_sentiment_series(start_time="20260901")
    assert [r["trade_date"] for r in rows] == ["20260918", "20260922"], "周末必须被剔除"
    by_date = {r["trade_date"]: r for r in rows}
    # 骨架日上仍能合并到自然日发布的数据(20260918 是周五, 有 SC27)
    assert by_date["20260918"]["pboc_net_injection"] == pytest.approx(-500.0)
    assert by_date["20260922"]["seal_fail_money"] == pytest.approx(561.59)


def test_baseline_empty_when_no_sample():
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        db.commit()
        out = tss.latest_with_baseline(db, days=20)
        assert out == {"asof": None, "latest": None, "baseline": {}, "sample_days": 0}
    finally:
        db.close()


def test_pro_series_always_sends_end_time(monkeypatch):
    """网关参数契约锁: 带了 start_time 却不带 end_time 会整批
    ErrorId=10 'json has no end_time' —— 这是实际踩过的坑, 不能再犯。"""
    import marketdata.vendors.tq as tqv

    captured: dict = {}

    def _fake_rpc(method, params, timeout=None):
        captured["method"] = method
        captured["params"] = dict(params)
        return {"SC3": [{"Date": "20260923", "Value": ["34.00", "22.00"]}]}

    monkeypatch.setattr(tqv, "_rpc", _fake_rpc)

    out = tqv.sc_series(["SC3"], start_time="20260801")
    assert out == {"SC3": [{"Date": "20260923", "Value": ["34.00", "22.00"]}]}
    assert captured["params"]["table_list"] == ["SC3"]
    assert captured["params"]["end_time"] == datetime.now(
        ZoneInfo("Asia/Shanghai")
    ).strftime("%Y%m%d")
    assert captured["params"]["code"] == tqv._SC_FALLBACK_CODE
    # 显式传入时以传入值为准
    tqv.sc_series(["SC3"], start_time="20260801", end_time="20260901")
    assert captured["params"]["end_time"] == "20260901"


def test_null_tables_are_dropped_not_zeroed(monkeypatch):
    """表无数据时服务端给 Value:null —— 必须整张丢掉, 不能当 0 落库。"""
    import marketdata.vendors.tq as tqv

    monkeypatch.setattr(
        tqv,
        "_rpc",
        lambda method, params, timeout=None: {
            "SC3": [{"Date": "20260923", "Value": ["34.00", "22.00"]}],
            "SC20": None,
            "ErrorId": "0",
        },
    )
    out = tqv.sc_series(["SC3", "SC20"])
    assert "SC20" not in out and "ErrorId" not in out
    assert "SC3" in out
