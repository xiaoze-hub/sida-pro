"""A-03 回归: 竞价池 / 机会池必须带"数据日期 + 交易阶段"(2026-10-18 审计 P0)。

缺陷原文: 竞价池响应只有 `{available,count,records,missing_fields,note}`, 既没有
`trade_date` 也没有 `as_of`; 前端用 `new Date()`(浏览器本地时钟)渲染"同步 15:00:12",
周日打开会把上周五 9:25 的池子说成"系统刚刚同步了竞价"。

本文件锁死:
1. 非交易日 15:00 拿到池子 ⇒ `data_date` = 最近交易日, `is_live=False`, `trade_phase=non_trading_day`;
2. 竞价窗口内(09:15-09:25)且数据是今天的 ⇒ `is_live=True`;
3. 交易时段(连续竞价)拿到的是"今日 9:25 已定型结果" ⇒ `is_live=False` 但 `is_today=True`;
4. 空池必须给**可诊断的 reason**(数据源断链 / 非交易日 / 非竞价时段 / 今日无异动), 不许三义合一;
5. `as_of` 来自服务端, 而不是调用方(浏览器)时钟。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from src.core import auction_pool
from src.core.trade_phase import (
    PHASE_AUCTION,
    PHASE_AUCTION_MATCH,
    PHASE_CONTINUOUS,
    PHASE_NOON_BREAK,
    PHASE_NON_TRADING_DAY,
    PHASE_POST_CLOSE,
    PHASE_PRE_OPEN,
    resolve_data_date,
    trade_phase,
)

FRIDAY = date(2026, 10, 16)
SUNDAY = date(2026, 10, 18)


@pytest.fixture(autouse=True)
def _clean():
    auction_pool.clear_cache()
    yield
    auction_pool.clear_cache()


def _rec(code: str = "600111", clock: str | None = "09:25:03") -> dict:
    r = {
        "symbol": code,
        "code": code,
        "name": "北方稀土",
        "gap_pct": 3.2,
        "withdraw_rate": None,
        "volume_ratio": None,
    }
    if clock is not None:
        r["时间"] = clock
    return r


def _payload(monkeypatch, *, records, now: str, last_sync=None, error=None):
    monkeypatch.setattr(auction_pool, "fetch_auction_anomaly", lambda market="CN": list(records))
    monkeypatch.setattr(auction_pool, "_last_sync_at", lambda: last_sync)
    monkeypatch.setattr(auction_pool, "_LAST_FETCH_ERROR", error)
    return auction_pool.fetch_auction_payload("CN", now=datetime.fromisoformat(now))


# ── 交易阶段 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-10-18 15:00", PHASE_NON_TRADING_DAY),   # 周日
        ("2026-10-19 09:00", PHASE_PRE_OPEN),
        ("2026-10-16 09:20", PHASE_AUCTION),
        ("2026-10-16 09:28", PHASE_AUCTION_MATCH),
        ("2026-10-16 10:30", PHASE_CONTINUOUS),
        ("2026-10-16 12:00", PHASE_NOON_BREAK),
        ("2026-10-16 15:30", PHASE_POST_CLOSE),
    ],
)
def test_trade_phase_boundaries(stamp, expected):
    assert trade_phase(datetime.fromisoformat(stamp)) == expected


def test_resolve_data_date_on_sunday_is_friday():
    """周日绝不许把 data_date 说成"今天"。"""
    assert resolve_data_date(now=datetime.fromisoformat("2026-10-18 15:00")) == FRIDAY


# ── 竞价池响应体 ─────────────────────────────────────────────────────────
def test_sunday_payload_is_labeled_as_snapshot_not_live(monkeypatch):
    p = _payload(
        monkeypatch, records=[_rec()], now="2026-10-18 15:00:12",
        last_sync=datetime(2026, 10, 16, 9, 25, 3),
    )
    assert p["data_date"] == "2026-10-16"
    assert p["trade_phase"] == PHASE_NON_TRADING_DAY
    assert p["is_live"] is False
    assert p["is_today"] is False
    assert p["as_of"].startswith("2026-10-18T15:00:12")  # 服务端时钟, 不是浏览器
    assert p["data_synced_at"].startswith("2026-10-16T09:25:03")
    assert p["data_clock"] == "09:25:03"
    assert p["reason"] is None
    assert "非实时" in p["reason_text"]


def test_auction_window_payload_is_live(monkeypatch):
    p = _payload(monkeypatch, records=[_rec()], now="2026-10-16 09:20:05")
    assert p["data_date"] == "2026-10-16"
    assert p["trade_phase"] == PHASE_AUCTION
    assert p["is_live"] is True
    assert "实时" in p["reason_text"]


def test_continuous_session_is_not_live_but_is_today(monkeypatch):
    """10:30 打开: 池子是今天 9:25 的已定型结果 —— 不许自称"实时"。"""
    p = _payload(monkeypatch, records=[_rec()], now="2026-10-16 10:30:00")
    assert p["is_live"] is False
    assert p["is_today"] is True
    assert "已定型" in p["reason_text"]


def test_source_clock_with_full_date_wins(monkeypatch):
    """源端若带完整日期, 以源为准(不按日历猜)。"""
    p = _payload(monkeypatch, records=[_rec(clock="2026-10-15 09:25:03")], now="2026-10-16 09:20:00")
    assert p["data_date"] == "2026-10-15"
    assert p["is_live"] is False


def test_empty_pool_reason_source_unavailable(monkeypatch):
    p = _payload(monkeypatch, records=[], now="2026-10-16 09:20:00", error="TimeoutError: tq gateway")
    assert p["available"] is False and p["count"] == 0
    assert p["reason"] == "source_unavailable"
    assert "不可用" in p["reason_text"]


def test_empty_pool_reason_non_trading_day(monkeypatch):
    p = _payload(monkeypatch, records=[], now="2026-10-18 15:00:00")
    assert p["reason"] == "non_trading_day"
    # 没有任何数据、也没有落库记录 ⇒ 不编数据日
    assert p["data_date"] is None


def test_non_trading_day_keeps_source_error_in_text(monkeypatch):
    """闭市是主导事实, 但运维信号(数据源不可达)不能被吞掉。"""
    p = _payload(monkeypatch, records=[], now="2026-10-18 15:00:00", error="TimeoutError: tq")
    assert p["reason"] == "non_trading_day"
    assert "数据源同时不可达" in p["reason_text"]


def test_empty_pool_uses_last_sync_date(monkeypatch):
    """空池但有历史落库 → 数据日取最后一次落库日, 而不是"今天"。"""
    p = _payload(monkeypatch, records=[], now="2026-10-18 15:00:00", last_sync=datetime(2026, 10, 16, 9, 25, 3))
    assert p["data_date"] == "2026-10-16"
    assert p["data_synced_at"].startswith("2026-10-16T09:25:03")


def test_empty_pool_reason_non_auction_session(monkeypatch):
    p = _payload(monkeypatch, records=[], now="2026-10-16 10:30:00")
    assert p["reason"] == "non_auction_session"


def test_empty_pool_reason_no_anomaly(monkeypatch):
    p = _payload(monkeypatch, records=[], now="2026-10-16 09:20:00")
    assert p["reason"] == "no_anomaly"


def test_payload_keeps_legacy_fields(monkeypatch):
    """老前端读的 available/count/records/missing_fields/note 必须保留。"""
    p = _payload(monkeypatch, records=[_rec()], now="2026-10-16 09:20:00")
    for k in ("available", "count", "records", "missing_fields", "note"):
        assert k in p
    assert p["count"] == 1 and p["available"] is True
