# -*- coding: utf-8 -*-
"""连板梯队盘中三态状态机(v0.5.87, spec §3.1)。纯函数, IO 由调用方注入。

三态(每 60s 一轮):
  sealed_now  现价==涨停价(在板上; 封死/排队盘中不分, 诚实标注)
  blown       今日曾 sealed_now, 本轮现价已跌破涨停价(炸板)
  broken      昨日定型 boards>=2 且收盘封板, 今日未触板(断板); 昨首板今未续≠断板
收盘(15:05 后)由 /ladder 切回 finalized(limit_up_events 真值)覆盖盘中态。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TICK = 0.005  # 1 个最小报价单位容差

STATE_SEALED = "sealed_now"
STATE_BLOWN = "blown"
STATE_BROKEN = "broken"
STATE_IDLE = "idle"


def classify(price, limit_px, ever_sealed: bool, prev_boards_yesterday: int) -> str:
    """单股本轮状态。price/limit_px 任一为 None → idle(不猜)。"""
    if price is None or limit_px is None:
        return STATE_IDLE
    if abs(price - limit_px) <= _TICK:
        return STATE_SEALED
    if ever_sealed:
        return STATE_BLOWN
    if prev_boards_yesterday >= 2:
        return STATE_BROKEN
    return STATE_IDLE


def merge_state(prev: dict, round_states: dict, ts: str) -> dict:
    """把本轮三态并入累计态。rec: {ever_sealed, first_at, last_sealed_at, opened_at}。

    first_at 不覆写; 回封(sealed)清 opened_at; blown 仅在 ever_sealed 且首次开板时记 opened_at。
    """
    out = {k: dict(v) for k, v in (prev or {}).items()}
    for sym, st in (round_states or {}).items():
        rec = out.get(sym) or {"ever_sealed": False, "first_at": None,
                              "last_sealed_at": None, "opened_at": None}
        if rec["first_at"] is None:
            rec["first_at"] = ts
        if st == STATE_SEALED:
            rec["ever_sealed"] = True
            rec["last_sealed_at"] = ts
            rec["opened_at"] = None
        elif st == STATE_BLOWN and rec["ever_sealed"] and rec["opened_at"] is None:
            rec["opened_at"] = ts
        out[sym] = rec
    return out


def build_live_day(state: dict, quotes: dict, yesterday_boards: dict,
                   names: dict, date: str) -> dict:
    """盘中列: 在板股按 昨板数+1 分组; 炸板/断板进对应组; 不编 boards。date=紧凑交易日。"""
    groups: dict[int, list[dict]] = {}
    blown, broken = [], []
    for sym, rec in (state or {}).items():
        q = (quotes or {}).get(sym) or {}
        st = classify(q.get("price"), q.get("limit_px"), bool(rec.get("ever_sealed")),
                      (yesterday_boards or {}).get(sym, 0))
        candle = None
        if q.get("open") is not None and q.get("high") is not None and q.get("low") is not None:
            candle = {"o": q["open"], "h": q["high"], "l": q["low"],
                      "c": q.get("price")}
        item = {"symbol": sym, "name": (names or {}).get(sym), "candle": candle,
                "pct": q.get("change_pct"), "first_time": rec.get("first_at")}
        if st == STATE_SEALED:
            groups.setdefault((yesterday_boards or {}).get(sym, 0) + 1, []).append(item)
        elif st == STATE_BLOWN:
            blown.append({**item, "prev_boards": (yesterday_boards or {}).get(sym)})
        elif st == STATE_BROKEN:
            broken.append({**item, "prev_boards": (yesterday_boards or {}).get(sym)})
    rows = []
    for boards in sorted(groups, reverse=True):
        stocks = groups[boards]
        rows.append({"boards": boards, "codes": [s["symbol"] for s in stocks],
                     "names": [s["name"] for s in stocks],
                     "tag": "首板" if boards == 1 else None, "stocks": stocks})
    return {"date": date, "rows": rows, "blown": blown, "broken": broken,
            "provisional": True}


def _is_intraday(now) -> bool:
    """交易时段(含集合竞价后~收盘后 5min 缓冲)才跑 live; 否则 None/降级。"""
    from src.core.trading_calendar import is_trading_day

    dt = now or datetime.now(_CST)
    if not is_trading_day(dt):
        return False
    mins = dt.hour * 60 + dt.minute
    return (9 * 60 + 25 <= mins <= 11 * 60 + 30) or (13 * 60 <= mins <= 15 * 60 + 5)


def scan_tick(now=None, deps=None):
    """60s 一轮: TDX 全市场快照 → 三态 → 合并 Redis 态 → 写回。时段外返回 None。"""
    from src.core.limit_rules import limit_up_price

    d = deps or _default_deps()
    dt = now or datetime.now(_CST)
    if not _is_intraday(dt):
        return None
    date = dt.strftime("%Y%m%d")
    ts = dt.strftime("%H:%M")
    meta = d.meta_fn(d.cli, date)
    try:
        codes = d.universe_fn()
        raw = d.quotes_fn(codes)
    except Exception as e:  # noqa: BLE001
        failed = meta.get("rounds_failed", 0) + 1
        meta = {**meta, "rounds_failed": failed, "stale": failed >= 3}
        d.save_fn(d.cli, date, d.load_fn(d.cli, date), meta)
        logger.warning("ladder-live: 扫描失败(rounds_failed=%s): %s", failed, e)
        return None
    if not raw:
        # TDX 非交易时段/无数据 → 不写态, 保持上一轮(调用方按 stale/降级处理)
        return None
    names = d.names_fn()
    yb = d.yesterday_boards_fn()
    prev_state = d.load_fn(d.cli, date)
    round_states, quotes = {}, {}
    for sym, q in raw.items():
        price = q.get("price")
        last = q.get("last_close")
        limit_px = limit_up_price(sym, last, "ST" in (names.get(sym) or "").upper())
        quotes[sym] = {**q, "limit_px": limit_px,
                       "change_pct": ((price / last - 1.0) * 100.0) if price and last else None}
        rec = prev_state.get(sym) or {}
        round_states[sym] = classify(price, limit_px, bool(rec.get("ever_sealed")),
                                     yb.get(sym, 0))
    state = merge_state(prev_state, round_states, ts)
    meta = {**meta, "rounds_failed": 0, "stale": False, "last_ok": ts}
    d.save_fn(d.cli, date, state, meta)
    live_day = build_live_day(state, quotes, yb, names, date)
    d.save_day_fn(d.cli, date, live_day)
    return {"date": date, "state": state, "quotes": quotes, "meta": meta,
            "live_day": live_day}


def _default_deps():
    from types import SimpleNamespace

    from src.core import ladder_live_state as rst
    from src.core.klines_fullmarket import a_share_universe
    from src.core.tdx_boards import pricevol_only

    def universe_fn():
        from src.collectors.stock_list import get_stock_list

        return a_share_universe(get_stock_list())

    def names_fn():
        from src.collectors.stock_list import get_stock_list

        return {s["symbol"]: s.get("name") for s in get_stock_list() if s.get("symbol")}

    def yesterday_boards_fn():
        # KI-039 棘轮: core 禁 import web → 直连 src.db.session
        from sqlalchemy import text

        from src.core.limit_ladder import _sealed_by_date, boards_for_date
        from src.db.session import SessionLocal

        with SessionLocal() as db:
            dates = [r[0] for r in db.execute(
                text("SELECT DISTINCT trade_date FROM limit_up_events "
                     "ORDER BY trade_date DESC LIMIT 2")).fetchall()]
            if len(dates) < 2:
                return {}
            ev = db.execute(
                text("SELECT trade_date, symbol, is_sealed_close FROM limit_up_events "
                     "WHERE trade_date >= :a"), {"a": dates[0]}).mappings().fetchall()
        return boards_for_date(sorted(dates), _sealed_by_date(list(ev)), sorted(dates)[-1])

    cli = rst.client()
    return SimpleNamespace(cli=cli, quotes_fn=pricevol_only, universe_fn=universe_fn,
                           names_fn=names_fn, yesterday_boards_fn=yesterday_boards_fn,
                           save_fn=rst.save_state, load_fn=rst.load_state,
                           meta_fn=rst.load_meta, save_day_fn=rst.save_day,
                           load_day_fn=rst.load_day)
