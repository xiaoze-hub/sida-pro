"""行情溯源契约（2026-10-29 复审 F3 / B3）：WS 帧必须带**行情源自带的**报价时刻，
市场状态必须给**独立的市场当地日期**。

复审发现的两条真实缺陷（本文件把结论钉在接口层，不靠人读注释）：

1. `src/web/api/quote_stream.py::_fetch_batch_quotes` 只下发 `{price, change_pct,
   prev_close, name}` —— 没有行情源报时。前端 `applyQuoteTicks` 于是把每个"价格变了"
   的 tick 的时刻判成未知（这是它唯一诚实的做法），5s 一帧的 WS 全程拿不到时效，
   20s 轮询之间的真破位大部分时段看不见。
2. `/stocks/markets/status` 只给 `local_time`（`HH:MM`）—— 没有市场当地**日期**，
   防线判定无法证明"报价与市场墙钟是同一天"，跨日只能退化成"时点未知"。

本文件钉的口径：
- 只有行情源报文里**确实带报时**（`Quote.quote_time`，腾讯 parts[30]）才透传
  `quote_time` / `quote_date` / `daily_pnl_period`；缺报时就是 `None` + `unknown`，
  **绝不用服务端发送时间（`time.time()` / `datetime.now()`）冒充行情源时刻**；
- TQ 快照不提供报时 ⇒ `Quote.quote_time is None`（旧实现填 `datetime.now()`，
  等于把"我们收到的时间"当"行情发生的时间"）；
- `/stocks/markets/status` 每个市场带 `local_date`（该市场时区的当天日期），
  `local_time` 仍是 `HH:mm`；取状态失败的市场 `local_date` 为 `None`（不编日期）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.web.api import quote_stream as qs
from src.web.api.stocks import MARKETS, get_market_status

CN = ZoneInfo("Asia/Shanghai")


def _cn_today(offset_days: int = 0) -> str:
    return (datetime.now(CN).date() + timedelta(days=offset_days)).isoformat()


# ---------------------------------------------------------------------------
# /stocks/markets/status · local_date 独立字段
# ---------------------------------------------------------------------------

def test_market_status_has_independent_local_date_per_market():
    """每个市场的 local_date 必须是**该市场时区**的当天，且与 HH:MM 自洽。"""
    rows = {row["code"]: row for row in get_market_status()}
    assert {"CN", "HK", "US"} <= set(rows)

    for code in ("CN", "HK", "US"):
        row = rows[code]
        tz = ZoneInfo(row["timezone"])
        now = datetime.now(tz)
        assert row["local_date"] == now.date().isoformat(), code
        # 旧字段语义不变：仍是 HH:MM，不带日期
        assert len(row["local_time"]) == 5 and row["local_time"][2] == ":", (code, row["local_time"])
        # 日期 + 时分合起来必须接近"现在"（同一天，容 2 分钟跨分钟边界）
        parsed = datetime.fromisoformat(f"{row['local_date']}T{row['local_time']}:00")
        drift = abs((now.replace(tzinfo=None, second=0, microsecond=0) - parsed).total_seconds())
        assert drift <= 120, (code, row["local_date"], row["local_time"], drift)


def test_market_status_error_row_has_no_fabricated_date(monkeypatch):
    """取状态抛错的市场：local_date 必须是 None，不许拿本机日期顶上。"""

    class _Broken:
        name = "坏市场"
        timezone = "Asia/Shanghai"
        sessions: list = []

        def get_tz(self):
            raise RuntimeError("tz broken")

        def is_trading_time(self):
            raise RuntimeError("tz broken")

    broken_key = next(iter(MARKETS))
    monkeypatch.setitem(MARKETS, broken_key, _Broken)
    rows = get_market_status()
    broken = [r for r in rows if r["code"] == broken_key.value]
    assert broken and broken[0]["status"] == "unknown"
    assert broken[0]["local_date"] is None
    assert broken[0]["local_time"] == "--:--"


# ---------------------------------------------------------------------------
# WS 帧 · 只透传行情源自带的报时
# ---------------------------------------------------------------------------

def _quote_row(symbol: str, *, quote_time=None, quote_date=None, price=10.0) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "market": "CN",
        "current_price": price,
        "change_pct": 1.0,
        "prev_close": 9.9,
        "quote_time": quote_time,
        "quote_date": quote_date,
        "source": "tencent",
    }


def _patch_rows(monkeypatch, rows):
    import src.core.marketdata_client as mc

    monkeypatch.setattr(mc, "md_quote_rows", lambda symbols, market: list(rows))


def test_ws_payload_passes_through_source_quote_time(monkeypatch):
    """源带报时 → 帧里 quote_time/quote_date/daily_pnl_period 一起下（价格与时刻同源）。"""
    stamp = f"{_cn_today()}T10:15:00+08:00"
    _patch_rows(monkeypatch, [_quote_row("600519", quote_time=stamp, quote_date=_cn_today())])

    data = asyncio.run(qs._fetch_batch_quotes({"CN": ["600519"]}))
    item = data["600519"]
    assert item["price"] == 10.0
    assert item["quote_time"] == stamp
    assert item["quote_date"] == _cn_today()
    assert item["daily_pnl_period"] == "today"


def test_ws_payload_labels_previous_day_quote(monkeypatch):
    """跨日昨收：quote_date=昨天 ⇒ previous_trading_day，不许标 today。"""
    yesterday = _cn_today(-1)
    _patch_rows(monkeypatch, [_quote_row("600519", quote_time=f"{yesterday}T15:00:00+08:00",
                                         quote_date=yesterday)])

    item = asyncio.run(qs._fetch_batch_quotes({"CN": ["600519"]}))["600519"]
    assert item["quote_date"] == yesterday
    assert item["daily_pnl_period"] == "previous_trading_day"


def test_ws_payload_never_fabricates_time(monkeypatch):
    """源没报时（TQ/降级源）⇒ 三个字段都是 None/unknown，绝不用服务端时间补一个。"""
    _patch_rows(monkeypatch, [_quote_row("600519", quote_time=None, quote_date=None)])

    before = datetime.now(CN).replace(microsecond=0)
    item = asyncio.run(qs._fetch_batch_quotes({"CN": ["600519"]}))["600519"]
    assert item["quote_time"] is None
    assert item["quote_date"] is None
    assert item["daily_pnl_period"] == "unknown"
    # 防御：不许出现"看起来像刚发生"的时间戳
    assert item["quote_time"] != before.isoformat()


def test_ws_payload_drops_inconsistent_stamp(monkeypatch):
    """quote_date 与 quote_time 的日期段不一致（上游串源）⇒ 两个都不透传，期间 unknown。"""
    stamp = f"{_cn_today()}T10:15:00+08:00"
    _patch_rows(monkeypatch, [_quote_row("600519", quote_time=stamp, quote_date=_cn_today(-1))])

    item = asyncio.run(qs._fetch_batch_quotes({"CN": ["600519"]}))["600519"]
    assert item["quote_time"] is None
    assert item["quote_date"] is None
    assert item["daily_pnl_period"] == "unknown"


def test_ws_broadcast_frame_carries_provenance(monkeypatch):
    """真正的下行帧（envelope quote.tick）里带得到这些字段 —— 前端才消费得到。"""
    monkeypatch.setenv("REDIS_DISABLED", "1")
    stamp = f"{_cn_today()}T10:15:00+08:00"
    _patch_rows(monkeypatch, [_quote_row("600519", quote_time=stamp, quote_date=_cn_today())])
    payload = {"type": "quotes", "data": asyncio.run(qs._fetch_batch_quotes({"CN": ["600519"]}))}

    q: asyncio.Queue = asyncio.Queue()
    with qs._subscribers_lock:
        qs._subscribers[999999] = (q, None)
    try:
        qs._broadcast(payload)
        frame = q.get_nowait()
    finally:
        with qs._subscribers_lock:
            qs._subscribers.pop(999999, None)

    assert frame["topic"] == "quote.tick"
    item = frame["payload"]["data"]["600519"]
    assert item["quote_time"] == stamp
    assert item["quote_date"] == _cn_today()
    assert item["daily_pnl_period"] == "today"


# ---------------------------------------------------------------------------
# TQ 快照没有报时 ⇒ quote_time 必须是 None
# ---------------------------------------------------------------------------

def test_tq_snapshot_does_not_fake_quote_time(monkeypatch):
    """TQ `get_market_snapshot` 不带报时字段：`quote_time` 必须为 None（旧实现填 now()）。"""
    pytest.importorskip("marketdata")
    import marketdata.vendors.tq as tqmod
    from marketdata.symbol import Symbol

    snap = {
        "Now": "10.10", "LastClose": "9.58", "Open": "9.77", "Max": "10.54", "Min": "9.59",
        "Volume": "1784413", "Amount": "182560.53", "Average": "10.23",
        "Inside": "778965", "Outside": "1005449", "ErrorId": "0",
    }

    def fake_rpc(method, params, timeout=None, **kw):
        return dict(snap) if method == "get_market_snapshot" else {}

    monkeypatch.setattr(tqmod, "_rpc", fake_rpc)
    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("002361", "CN")], {})
    assert len(quotes) == 1
    assert quotes[0].quote_time is None, "TQ 不提供报时，服务端时间不能冒充"
