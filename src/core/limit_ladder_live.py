# -*- coding: utf-8 -*-
"""连板梯队盘中状态机(v0.5.87 三态 → v0.5.88 细分, 借鉴 quicktiny 连板天梯)。

状态(classify):
  sealed_now  现价==涨停价(在板; 封死/排队盘中不分)
  blown       今日曾 sealed, 本轮跌破涨停(炸板未回封)
  broken      昨日定型 boards>=2 且收盘封板, 今日未触板(断板; 昨首板今未续≠断板)
  charging    未封但现价已冲至涨停价 70% 幅度以上(冲板)
  idle        其余

累计态(merge_state) rec 字段:
  ever_sealed / first_at / last_sealed_at(=尾封) / opened_at / open_count(开板次数) / resealed(开板后回封)

板型(build_live_day): 一字=开盘即涨停且未开板; T字=开板后回封; 换手=其余封住。
封单/封成比: 由 deps 注入(seal_quality_samples 封单额 / 当日成交额), 缺则 None 不编。
收盘(15:05 后)由 /ladder 切回 finalized(limit_up_events 真值)覆盖盘中态。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TICK = 0.005  # 1 个最小报价单位容差
_CHARGE_RATIO = 0.7  # 冲板阈值: 涨幅达涨停幅度的 70% 以上且未封

STATE_SEALED = "sealed_now"
STATE_BLOWN = "blown"
STATE_BROKEN = "broken"
STATE_CHARGING = "charging"
STATE_IDLE = "idle"


def classify(price, limit_px, ever_sealed: bool, prev_boards_yesterday: int,
             charging_floor=None) -> str:
    """单股本轮状态。price/limit_px 任一为 None → idle(不猜)。"""
    if price is None or limit_px is None:
        return STATE_IDLE
    if abs(price - limit_px) <= _TICK:
        return STATE_SEALED
    if ever_sealed:
        return STATE_BLOWN
    if prev_boards_yesterday >= 2:
        return STATE_BROKEN
    if charging_floor is not None and price >= charging_floor:
        return STATE_CHARGING
    return STATE_IDLE


def merge_state(prev: dict, round_states: dict, ts: str) -> dict:
    """把本轮三态并入累计态。first_at 不覆写; 回封清 opened_at 并置 resealed; 每次炸板 open_count+1。"""
    out = {k: dict(v) for k, v in (prev or {}).items()}
    for sym, st in (round_states or {}).items():
        rec = out.get(sym) or {"ever_sealed": False, "first_at": None, "last_sealed_at": None,
                              "opened_at": None, "open_count": 0, "resealed": False}
        if rec["first_at"] is None:
            rec["first_at"] = ts
        if st == STATE_SEALED:
            if rec["opened_at"] is not None:
                rec["resealed"] = True  # 开板后又封回
            rec["ever_sealed"] = True
            rec["last_sealed_at"] = ts
            rec["opened_at"] = None
        elif st == STATE_BLOWN and rec["ever_sealed"] and rec["opened_at"] is None:
            rec["opened_at"] = ts
            rec["open_count"] = int(rec.get("open_count", 0)) + 1
        out[sym] = rec
    return out


def _plate_type(q: dict, rec: dict, limit_px) -> str | None:
    """一字/ T字 / 换手; 非封住返回 None。open 缺失时不猜一字(回退换手/T字)。"""
    op = q.get("open")
    if op is not None and limit_px is not None and op >= limit_px - _TICK:
        return "一字"
    if rec.get("resealed"):
        return "T字"
    return "换手"


def _stock_tag(st: str, rec: dict, pct) -> str:
    if st == STATE_SEALED:
        return "回封" if rec.get("resealed") else "封住"
    if st == STATE_BLOWN:
        return "炸板"
    if st == STATE_CHARGING:
        return "冲板"
    if st == STATE_BROKEN:
        if pct is not None and pct < -0.5:
            return "水下"
        if pct is not None and abs(pct) <= 0.5:
            return "平盘"
        return "断板"
    return ""


def build_live_day(state: dict, quotes: dict, yesterday_boards: dict,
                   names: dict, date: str, seal_amounts: dict | None = None) -> dict:
    """盘中列: 在板股按 昨板+1 分组; 炸/断/冲 进对应组; 缺 O/H/L 不编影线。"""
    seal_amounts = seal_amounts or {}
    groups: dict[int, list[dict]] = {}
    blown, broken, charging = [], [], []
    for sym, rec in (state or {}).items():
        q = (quotes or {}).get(sym) or {}
        yb = (yesterday_boards or {}).get(sym, 0)
        st = classify(q.get("price"), q.get("limit_px"), bool(rec.get("ever_sealed")),
                      yb, q.get("charging_floor"))
        candle = q.get("candle")
        if candle is None and q.get("open") is not None and q.get("high") is not None and q.get("low") is not None:
            candle = {"o": q["open"], "h": q["high"], "l": q["low"], "c": q.get("price")}
        pct = q.get("change_pct")
        seal_amt = seal_amounts.get(sym)
        if seal_amt is None:
            seal_amt = q.get("fcamo")  # 优先 more_info FCAmo(权威), 回退 seal_quality_samples
        seal_ratio = None
        if seal_amt is not None and q.get("amount"):
            seal_ratio = round(seal_amt / q["amount"], 4)
        item = {
            "symbol": sym, "name": (names or {}).get(sym), "candle": candle,
            "pct": pct, "first_time": rec.get("first_at"),
            "tag": _stock_tag(st, rec, pct),
            "seal_tag": q.get("seal_tag"),
            "dive": q.get("dive"),
            "boards_vendor": q.get("boards_vendor"),
            "plate_type": _plate_type(q, rec, q.get("limit_px")) if st == STATE_SEALED else None,
            "open_count": int(rec.get("open_count", 0)),
            "last_sealed": rec.get("last_sealed_at"),
            "seal_amount": seal_amt, "seal_ratio": seal_ratio,
        }
        if st == STATE_SEALED:
            groups.setdefault(yb + 1, []).append(item)
        elif st == STATE_BLOWN:
            blown.append({**item, "prev_boards": yb})
        elif st == STATE_BROKEN:
            broken.append({**item, "prev_boards": yb})
        elif st == STATE_CHARGING:
            charging.append({**item, "prev_boards": yb})
    rows = []
    for boards in sorted(groups, reverse=True):
        stocks = groups[boards]
        rows.append({"boards": boards, "codes": [s["symbol"] for s in stocks],
                     "names": [s["name"] for s in stocks],
                     "tag": "首板" if boards == 1 else None, "stocks": stocks})
    return {"date": date, "rows": rows, "blown": blown, "broken": broken,
            "charging": charging, "provisional": True}


def _is_intraday(now) -> bool:
    """交易时段(含集合竞价后~收盘后 5min 缓冲)才跑 live; 否则 None/降级。"""
    from src.core.trading_calendar import is_trading_day

    dt = now or datetime.now(_CST)
    if not is_trading_day(dt):
        return False
    mins = dt.hour * 60 + dt.minute
    return (9 * 60 + 25 <= mins <= 11 * 60 + 30) or (13 * 60 <= mins <= 15 * 60 + 5)


def scan_tick(now=None, deps=None):
    """60s 一轮: TDX 全市场快照 → 状态 → 合并 Redis 态 → 写回(含预渲染 live_day)。时段外返回 None。"""
    from src.core.limit_rules import limit_ratio, limit_up_price

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
        return None
    names = d.names_fn()
    yb = d.yesterday_boards_fn()
    seal_amounts = d.seal_amounts_fn() if hasattr(d, "seal_amounts_fn") else {}
    prev_state = d.load_fn(d.cli, date)
    quotes = {}
    for sym, q in raw.items():
        price = q.get("price")
        last = q.get("last_close")
        ratio = limit_ratio(sym, "ST" in (names.get(sym) or "").upper())
        limit_px = limit_up_price(sym, last, "ST" in (names.get(sym) or "").upper())
        floor = last * (1 + (ratio or 0.1) * _CHARGE_RATIO) if (last and ratio) else None
        quotes[sym] = {**q, "limit_px": limit_px, "charging_floor": floor,
                       "change_pct": ((price / last - 1.0) * 100.0) if price and last else None}
    # L2 精修(单股接口, 只对候选池: 接近涨停 或 今日曾封): ZTPrice/FCAmo/五档/快照K/跳水/连板交叉
    from src.core.stock_l2 import fetch_stock_l2_batch, is_dive, seal_quality_tag

    cands = [s for s, q in quotes.items()
             if (q.get("price") and q.get("last_close")
                 and q["price"] >= q["last_close"] * 1.05)
             or (prev_state.get(s) or {}).get("ever_sealed")]
    for sym, l2 in (d.l2_fn(cands) if hasattr(d, "l2_fn") else fetch_stock_l2_batch(cands)).items():
        snap = l2.get("snapshot") or {}
        more = l2.get("more") or {}
        q = quotes[sym]
        zt = more.get("zt_price")
        if zt:
            q["limit_px"] = zt
        if more.get("fcamo") is not None:
            q["fcamo"] = more["fcamo"]
        if snap.get("open") is not None and snap.get("high") is not None and snap.get("low") is not None:
            q["candle"] = {"o": snap["open"], "h": snap["high"], "l": snap["low"],
                           "c": snap.get("now")}
            q["amount"] = q.get("amount") or snap.get("amount")
        rec = prev_state.get(sym) or {}
        q["seal_tag"] = seal_quality_tag(snap.get("buyp") or [], snap.get("buyv") or [],
                                         snap.get("sellp") or [], snap.get("sellv") or [],
                                         q["limit_px"], snap.get("now"),
                                         bool(rec.get("ever_sealed")))
        q["dive"] = is_dive(snap.get("now"), snap.get("before5min"))
        q["boards_vendor"] = more.get("ever_zt_count")
    round_states = {}
    for sym, q in quotes.items():
        price = q.get("price")
        rec = prev_state.get(sym) or {}
        ever = bool(rec.get("ever_sealed"))
        fcamo = q.get("fcamo")
        if fcamo is not None:
            # 官方口径: FCAmo>0 涨停(封), <0 跌停; 0 且未封 → 按价格态
            if fcamo > 0:
                round_states[sym] = STATE_SEALED
            elif ever:
                round_states[sym] = STATE_BLOWN
            else:
                round_states[sym] = classify(price, q.get("limit_px"), ever,
                                             yb.get(sym, 0), q.get("charging_floor"))
        else:
            round_states[sym] = classify(price, q.get("limit_px"), ever,
                                         yb.get(sym, 0), q.get("charging_floor"))
    state = merge_state(prev_state, round_states, ts)
    meta = {**meta, "rounds_failed": 0, "stale": False, "last_ok": ts}
    d.save_fn(d.cli, date, state, meta)
    live_day = build_live_day(state, quotes, yb, names, date, seal_amounts)
    d.save_day_fn(d.cli, date, live_day)
    return {"date": date, "state": state, "quotes": quotes, "meta": meta,
            "live_day": live_day}


def _default_deps():
    from types import SimpleNamespace

    from src.core import ladder_live_state as rst
    from src.core.klines_fullmarket import a_share_universe, limit_up_symbols, merge_universe
    from src.core.tdx_boards import pricevol_only

    def universe_fn():
        from src.collectors.stock_list import get_stock_list

        return merge_universe(a_share_universe(get_stock_list()), limit_up_symbols())

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

    def seal_amounts_fn():
        """今日各股最新封单额(seal_quality_samples); 无则空 dict(不编)。"""
        from sqlalchemy import text

        from src.db.session import SessionLocal

        try:
            with SessionLocal() as db:
                rows = db.execute(
                    text("SELECT DISTINCT ON (symbol) symbol, seal_amount FROM seal_quality_samples "
                         "WHERE ts >= CURRENT_DATE ORDER BY symbol, ts DESC")).fetchall()
            return {str(r[0]): r[1] for r in rows if r[1] is not None}
        except Exception as e:  # noqa: BLE001
            logger.warning("ladder-live: 读封单额失败: %s", e)
            return {}

    cli = rst.client()
    return SimpleNamespace(cli=cli, quotes_fn=pricevol_only, universe_fn=universe_fn,
                           names_fn=names_fn, yesterday_boards_fn=yesterday_boards_fn,
                           seal_amounts_fn=seal_amounts_fn, l2_fn=fetch_stock_l2_batch,
                           save_fn=rst.save_state, load_fn=rst.load_state,
                           meta_fn=rst.load_meta, save_day_fn=rst.save_day,
                           load_day_fn=rst.load_day)
