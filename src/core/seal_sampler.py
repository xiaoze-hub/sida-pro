"""封单成色盘中采样任务(批次A A1, 2026-09-06 28号)。

每 60s 对当日涨停池股票采样一行:
- fetch_tq_l2 → BCancel/SCancel/TotalBVol/TotalSVol/L2TicNum(累计字段, 差分口径)
- TQ get_market_snapshot → Now/LastClose(现价/昨收 → limit_rules 算涨停价 → is_sealed)
- 涨停池(wudao 源)自带 name(判 ST)/amount(封单额, 元)
写 seal_quality_samples 表(唯一键 symbol+ts, 幂等), 供 seal_quality.compute_from_series 消费。

诚实标注:
- BCancel/SCancel 走差分口径依据 TQ9 实测(收盘后字段不归零, 当日累计);
  周一盘中实测若发现为快照值, 口径改为直接采样值(docs/innov-dev-plan.md 风险项)。
- is_sealed 用昨收×涨停幅度推算, ST 由名称判断; 采样池来自涨停池(已在板),
  误判影响面仅 seal_success_rate, 不影响撤单率主线。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_MAX_SYMBOLS_PER_TICK = 30
_TABLE = "seal_quality_samples"


def _now_cst() -> datetime:
    return datetime.now(_CST)


def default_limit_up_items() -> list[dict]:
    """当日涨停池 → [{symbol, name, seal_amount, first_time, days}]。失败返回空。"""
    try:
        from src.collectors.market_sentiment_collector import MarketSentimentCollector

        pool = MarketSentimentCollector().get_limit_up_pool() or []
        items = []
        for p in pool:
            code = str(p.get("code") or "").strip()
            if len(code) == 6 and code.isdigit():
                items.append({
                    "symbol": code,
                    "name": str(p.get("name") or ""),
                    "seal_amount": p.get("amount"),
                    "first_time": p.get("first_time"),
                    "days": p.get("days"),
                })
        return items
    except Exception as e:  # noqa: BLE001
        logger.warning("封单采样: 涨停池获取失败: %s", e)
        return []


def sample_symbol(item: dict) -> dict | None:
    """采样单只涨停股。任一关键源失败 → None(不写半行)。"""
    symbol = str(item.get("symbol") or "").strip()
    if not symbol:
        return None
    try:
        from src.core.decision_pioneer import fetch_tq_l2

        l2 = fetch_tq_l2(symbol)
        if not l2:
            return None
        if any(l2.get(k) is None for k in ("cancel_buy", "cancel_sell", "total_buy_vol", "total_sell_vol")):
            return None

        price = last_close = None
        try:
            from marketdata.symbol import Symbol as _Symbol
            from marketdata.vendors.tq import to_tq_code as _to_tq, tq_rpc as _tq_rpc

            tqc = _to_tq(_Symbol.parse(symbol, "CN"))
            if tqc:
                snap = _tq_rpc("get_market_snapshot", {"stock_code": tqc})
                if isinstance(snap, dict):
                    price = _to_f(snap.get("Now"))
                    last_close = _to_f(snap.get("LastClose"))
        except Exception as e:  # noqa: BLE001
            logger.debug("封单采样 %s 快照失败: %s", symbol, e)

        limit_price = None
        is_sealed = None
        if last_close and last_close > 0:
            from src.core.limit_rules import limit_up_price

            is_st = "ST" in str(item.get("name") or "").upper()
            limit_price = limit_up_price(symbol, last_close, is_st=is_st)
            if limit_price is not None and price is not None:
                is_sealed = price >= limit_price - 1e-6

        return {
            "ts": _now_cst().isoformat(timespec="seconds"),
            "symbol": symbol,
            "market": "CN",
            "price": price,
            "last_close": last_close,
            "limit_price": limit_price,
            "is_sealed": None if is_sealed is None else (1 if is_sealed else 0),
            "cancel_buy": l2.get("cancel_buy"),
            "cancel_sell": l2.get("cancel_sell"),
            "total_buy_vol": l2.get("total_buy_vol"),
            "total_sell_vol": l2.get("total_sell_vol"),
            "l2_tick_num": l2.get("l2_tick_num"),
            "l2_order_num": l2.get("l2_order_num"),
            "seal_amount": _to_f(item.get("seal_amount")),
            "extra": json.dumps(
                {"name": item.get("name"), "first_time": item.get("first_time"), "days": item.get("days"),
                 "note": "seal_amount 来自涨停池(wudao); BCancel/SCancel 为当日累计, 指标用窗口差分"},
                ensure_ascii=False),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("封单采样 %s 失败: %s", symbol, e)
        return None


def _to_f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def store_samples(rows: list[dict]) -> int:
    """写库(symbol+ts 唯一, 先查后插, 单进程调度下无竞态)。返回实际写入数。"""
    if not rows:
        return 0
    from sqlalchemy import text

    from src.web.database import SessionLocal

    saved = 0
    db = SessionLocal()
    try:
        for r in rows:
            try:
                exists = db.execute(
                    text(f"SELECT 1 FROM {_TABLE} WHERE symbol = :symbol AND ts = :ts"),
                    {"symbol": r["symbol"], "ts": r["ts"]},
                ).first()
                if exists:
                    continue
                db.execute(
                    text(
                        f"INSERT INTO {_TABLE} (ts, symbol, market, price, last_close, limit_price, is_sealed,"
                        " cancel_buy, cancel_sell, total_buy_vol, total_sell_vol, l2_tick_num, l2_order_num,"
                        " seal_amount, extra) VALUES (:ts, :symbol, :market, :price, :last_close, :limit_price,"
                        " :is_sealed, :cancel_buy, :cancel_sell, :total_buy_vol, :total_sell_vol, :l2_tick_num,"
                        " :l2_order_num, :seal_amount, :extra)"
                    ),
                    r,
                )
                saved += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("封单采样写库失败 %s@%s: %s", r.get("symbol"), r.get("ts"), e)
                db.rollback()
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("封单采样写库整体失败: %s", e)
        db.rollback()
    finally:
        db.close()
    return saved


def sample_tick(symbols: list[str] | None = None) -> dict:
    """调度入口(60s): 交易时段内采样涨停池 → 写库。返回统计(日志/测试用)。"""
    out: dict = {"sampled": 0, "stored": 0, "skipped": "n/a"}
    try:
        from src.models.market import MARKETS, MarketCode

        mdef = MARKETS.get(MarketCode.CN)
        if mdef is not None and not mdef.is_trading_time():
            out["skipped"] = "non-trading-time"
            return out
    except Exception as e:  # noqa: BLE001
        logger.debug("封单采样交易时段判断失败(默认继续): %s", e)

    items: list[dict]
    if symbols:
        items = [{"symbol": s, "name": "", "seal_amount": None} for s in symbols]
    else:
        items = default_limit_up_items()
    items = items[:_MAX_SYMBOLS_PER_TICK]
    rows = [r for r in (sample_symbol(it) for it in items) if r]
    out["sampled"] = len(rows)
    out["stored"] = store_samples(rows)
    if rows:
        logger.info("封单成色采样: %s/%s 写入", out["stored"], len(items))
    # F2 L2 事件流(2026-09-06): 暗盘聚簇≥100万 / 封单成色异常 → 全局渠道推送(节流一次/日)
    try:
        from src.core.l2_event_stream import eval_tick

        out["events"] = eval_tick([r["symbol"] for r in rows] or [it.get("symbol") for it in items])
    except Exception as e:  # noqa: BLE001
        logger.debug(f"L2 事件流评估失败(不影响采样): {e}")
    return out


def get_recent_samples(symbol: str, limit: int = 120) -> list[dict]:
    """API 用: 当日(按最新样本日期)该股全部采样, ts 升序。"""
    from sqlalchemy import text

    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                f"SELECT ts, symbol, market, price, last_close, limit_price, is_sealed, cancel_buy,"
                " cancel_sell, total_buy_vol, total_sell_vol, l2_tick_num, l2_order_num, seal_amount, extra"
                f" FROM {_TABLE} WHERE symbol = :symbol ORDER BY ts DESC LIMIT :lim"
            ),
            {"symbol": symbol, "lim": int(limit)},
        ).fetchall()
    finally:
        db.close()
    out = []
    for r in reversed(rows):
        d = dict(r._mapping)
        d["is_sealed"] = None if d.get("is_sealed") is None else bool(d["is_sealed"])
        out.append(d)
    if out:
        today = str(out[-1]["ts"])[:10]
        out = [d for d in out if str(d["ts"])[:10] == today]
    return out
