# -*- coding: utf-8 -*-
"""分钟K线 1m 滚动入库(v0.5.49): 腾讯 mkline 解析 + klines_minute 写入链路。

覆盖:
  - fetch_tencent_minute_kline: 真实 payload 字段序解析(2026-09-10 实测格式)、
    手→股 ×100、脏行跳过、WAF 拦截/取数失败返回空、未知周期返回空
  - collect_minute_klines_once: 时段守卫跳过 → 拉取+upsert 落库(period='1m',
    adjust='') → 重跑幂等(行数不翻倍, 同键覆盖) → OHLC 违例柱过滤
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/marketdata/src"))

import src.db.session as dbs  # noqa: E402
from marketdata.types import Bar  # noqa: E402
from marketdata.vendors import kline as vk  # noqa: E402
from src.core import klines_minute as km  # noqa: E402
from src.web.migrations import (  # noqa: E402
    _m125_klines_table,
    _m137_klines_adjust_dimension,
    _m150_klines_amount_column,
)

_MKLINE_PAYLOAD = (
    '{"code":0,"msg":"","data":{"sh600000":{"qt":{"sh600000":[]},'
    '"m1":[["202609101500","9.34","9.35","9.35","9.34","8212.00",{},"0.25"],'
    '["202609101459","9.34","9.34","9.34","9.34","0.00",{},"0.00"],'
    '["garbage","1","2","3","4","5"],'
    '["202609101458","x","y","z","w","6"]],'
    '"prec":2}}}'
)


# ---------------------------------------------------------------------------
# vendor: fetch_tencent_minute_kline
# ---------------------------------------------------------------------------
def test_mkline_parse_real_payload(monkeypatch):
    monkeypatch.setattr(vk, "market_get", lambda *a, **k: _MKLINE_PAYLOAD)
    bars = vk.fetch_tencent_minute_kline("sh600000", 3, "1m")
    assert len(bars) == 2  # 脏行(garbage/非数值)跳过
    b = bars[0]
    assert b.date == "2026-09-10 15:00"
    assert (b.open, b.close, b.high, b.low) == (9.34, 9.35, 9.35, 9.34)
    assert b.volume == 821200.0  # 手 → 股(×100 口径铁律)


def test_mkline_unknown_period_empty(monkeypatch):
    monkeypatch.setattr(vk, "market_get", lambda *a, **k: _MKLINE_PAYLOAD)
    assert vk.fetch_tencent_minute_kline("sh600000", 3, "2m") == []


def test_mkline_waf_blocked_empty(monkeypatch):
    monkeypatch.setattr(vk, "market_get", lambda *a, **k: "... waf.tencent.com/501page.html ...")
    assert vk.fetch_tencent_minute_kline("sh600000", 3, "1m") == []


def test_mkline_fetch_none_empty(monkeypatch):
    monkeypatch.setattr(vk, "market_get", lambda *a, **k: None)
    assert vk.fetch_tencent_minute_kline("sh600000", 3, "1m") == []


# ---------------------------------------------------------------------------
# 写入链路: klines_minute.collect_minute_klines_once
# ---------------------------------------------------------------------------
def _mk_klines_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m125_klines_table(conn)
        _m137_klines_adjust_dimension(conn)
        _m150_klines_amount_column(conn)
    return eng


def _bars():
    return [
        Bar(date="2026-09-10 09:30", open=9.30, close=9.35, high=9.36, low=9.29, volume=821200.0),
        Bar(date="2026-09-10 09:31", open=9.35, close=9.34, high=9.35, low=9.30, volume=1000.0),
        Bar(date="2026-09-10 09:32", open=9.34, close=9.40, high=9.41, low=9.50, volume=10.0),  # OHLC 违例
    ]


def _patch_common(monkeypatch, trading=True):
    eng = _mk_klines_engine()
    monkeypatch.setattr(dbs, "engine", eng)
    monkeypatch.setattr(
        "src.core.quote_snapshots.in_trading_window", lambda now=None: trading
    )
    monkeypatch.setattr(
        "src.collectors.klines_ingestor.get_default_symbols",
        lambda: [("600000", "CN"), ("000001", "SZ")],  # SZ 应被过滤(分钟仅 CN)
    )
    seen = {}

    def _fake_fetch(tcode, count=320, period="1m"):
        seen["tcode"], seen["count"], seen["period"] = tcode, count, period
        return _bars()

    monkeypatch.setattr("marketdata.vendors.kline.fetch_tencent_minute_kline", _fake_fetch)
    return eng, seen


def test_collect_once_writes_minute_rows(monkeypatch):
    eng, seen = _patch_common(monkeypatch)
    res = km.collect_minute_klines_once()
    assert seen["tcode"] == "sh600000" and seen["period"] == "1m" and seen["count"] == 320
    assert res.get("symbols") == 1 and res.get("written") == 2  # OHLC 违例柱被滤掉
    with eng.connect() as conn:
        rows = conn.execute(
            text("SELECT symbol, market, period, source, adjust, volume FROM klines ORDER BY ts")
        ).all()
    assert [r[0] for r in rows] == ["600000", "600000"]
    assert all(r[2] == "1m" and r[3] == "tencent" and r[4] == "" for r in rows)
    assert rows[0][5] == 821200


def test_collect_once_replay_idempotent(monkeypatch):
    eng, _ = _patch_common(monkeypatch)
    assert km.collect_minute_klines_once().get("written") == 2
    # 同键重拉: upsert 覆盖, 行数不翻倍
    assert km.collect_minute_klines_once().get("written") == 2
    with eng.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM klines")).scalar()
    assert n == 2


def test_collect_once_skips_out_of_window(monkeypatch):
    eng, seen = _patch_common(monkeypatch, trading=False)
    res = km.collect_minute_klines_once()
    assert res == {"skipped": "non_trading"}
    assert not seen  # 未触发任何拉取
    with eng.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM klines")).scalar()
    assert n == 0


def test_collect_once_never_raises(monkeypatch):
    eng, _ = _patch_common(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr(km, "_engine", _boom)
    res = km.collect_minute_klines_once()
    assert res == {"error": "engine down"} or res.get("symbols") == 0
