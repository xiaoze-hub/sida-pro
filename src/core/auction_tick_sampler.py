# -*- coding: utf-8 -*-
"""竞价过程帧采样器(2026-10-18 P0-a, 方案 §7.2② / §8 批次 1)。

问题
----
`auction_snapshots` 的唯一键是 (trade_date, symbol, market), 注释写明"竞价在
9:15-9:25 唯一 → 重跑覆盖"。该假设**只在"结果"维度成立**: 试盘价抬升、9:20 前
撤单、虚假封单这些**过程**恰恰是分时变化的, 被"唯一"二字抹掉了。于是竞价策略
回测只能拿到 9:25 终值, 拿不到过程(方案 §5.2)。

本模块职责
----------
1. **采样**: 交易日 9:15-9:25 每 60s 一帧, 全市场分批取 TQ 快照 + `get_more_info`
   的竞价字段(`OpenAmo/OpenZAF/OpenZTBuy/BCancel/SCancel`), 写
   `auction_tick_snapshots`(append-only, 同帧幂等覆盖);
2. **回放**: `load_frames` / `replay` 按 (trade_date, symbol) 还原逐分钟序列;
3. **保留期**: `purge_old_frames`(默认 365 天, 方案 §9)。

诚实边界
--------
- 非竞价时段/非交易日**不采样**(显式 skip, 不写空帧);
- `unmatched_vol` / `withdraw_rate` 由一档买卖量与累计撤单推导, 输入缺失写 NULL
  —— 不用 0 冒充"没有撤单";
- 单票失败只跳过该票, 不拖垮整轮; 整轮 0 帧 → `ok=False`(作业判 failed, 断链可见)。

与 `auction_snapshots` 的关系: 后者保持"终值定稿"不动(已有读取方), 本表补过程。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.db.models import AuctionTickSnapshot
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

#: 竞价窗口(含 9:25 撮合那一分钟)。与 `trading_calendar.is_auction_time` 同口径,
#: 但这里**包含** 9:25:00-9:25:59 的撮合帧 —— 终值与过程都要留。
WINDOW_START = "09:15:00"
WINDOW_END = "09:25:59"
#: 保留期(天)。竞价表体量小(5.9 万行/日), 方案建议 1~3 年, 默认取 365。
RETENTION_DAYS = 365
#: 单轮最多采样多少只(0=全市场)。默认 800: TQ 逐票 2 次 RPC, 800 只约 40~60s,
#: 能在 9:15-9:25 的每分钟调度里跑完(全市场 5400 只一轮要 5+ 分钟, 会把
#: `max_instances=1` 的后续帧全部跳过 —— 帧数比覆盖面更重要)。env 可调。
MAX_SYMBOLS = int(os.environ.get("AUCTION_TICK_MAX_SYMBOLS", "800") or 0)
#: 每只之间小睡(秒), 让 TQ 客户端有喘息; 全市场一轮 5400 只 ≈ 11 分钟会超出窗口,
#: 故默认只在 `MAX_SYMBOLS` 内有意义 —— 生产通过 env 设定(如 800)。
PER_SYMBOL_SLEEP = float(os.environ.get("AUCTION_TICK_SLEEP_S", "0") or 0)

_LIMIT_THRESHOLD = {
    "default": 9.8,
    "gem": 19.8,      # 创业板/科创板
    "bse": 29.8,      # 北交所
}


def _f(v) -> float | None:
    if v is None or v == "" or v == "--":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def limit_threshold(symbol: str) -> float:
    s = str(symbol or "")
    if s.startswith(("300", "301", "688", "689")):
        return _LIMIT_THRESHOLD["gem"]
    if s.startswith(("8", "4", "9")):
        return _LIMIT_THRESHOLD["bse"]
    return _LIMIT_THRESHOLD["default"]


def classify_verdict(open_pct: float | None, symbol: str = "") -> str:
    """帧级判定(只描述可观测状态, 不给买卖建议)。"""
    if open_pct is None:
        return "unknown"
    th = limit_threshold(symbol)
    if open_pct >= th:
        return "limit_up_open"
    if open_pct <= -th:
        return "limit_down_open"
    if open_pct >= 3.0:
        return "strong_open"
    if open_pct <= -3.0:
        return "weak_open"
    return "flat_open"


def withdraw_rate(cancel_cum: float | None, match_vol: float | None) -> float | None:
    """撤单率 = 累计撤单 / (累计撤单 + 已匹配量)。

    为什么不用"本帧撤单增量/本帧成交量": 竞价早期成交量极小甚至为 0, 增量比值会
    爆成几十倍, 回测里等于噪声。累计口径天然落在 0~1, 且单调可比。
    输入缺失或两者皆 0 → None(不把"没数据"写成 0%)。
    """
    if cancel_cum is None or match_vol is None:
        return None
    denom = abs(cancel_cum) + abs(match_vol)
    if denom <= 0:
        return None
    return round(max(0.0, cancel_cum) / denom, 4)


def frame_from_snapshot(
    symbol: str,
    row: dict,
    *,
    tick_ts: str,
    market: str = "CN",
    source: str = "tq",
    captured_at: float | None = None,
) -> dict | None:
    """TQ 竞价读数 → 一行帧(纯函数, 单测直接喂 dict)。

    `row` 期望键(全部可缺): `last_close/open/open_pct/open_amount/open_limit_buy/
    buy1/buy1_vol/sell1/sell1_vol/cancel_buy/cancel_sell/vol`。
    `ref_price` 取 `open`(竞价虚拟撮合价), 缺失退回 `buy1`(买一价), 再无 → None。
    整帧没有任何价格信息 → 返回 None(不落空帧)。
    """
    ref_price = _f(row.get("open"))
    if ref_price in (None, 0.0):
        ref_price = _f(row.get("buy1"))
    if ref_price in (None, 0.0):
        return None

    prev_close = _f(row.get("last_close"))
    bid1_price = _f(row.get("buy1"))
    ask1_price = _f(row.get("sell1"))
    bid1_vol = _f(row.get("buy1_vol"))
    ask1_vol = _f(row.get("sell1_vol"))
    match_vol = _f(row.get("vol"))

    # 一档买卖量差 → 未匹配量(正=买剩)。只有一侧有量时写 NULL, 不硬凑符号。
    unmatched = None
    if bid1_vol is not None and ask1_vol is not None:
        unmatched = round(bid1_vol - ask1_vol, 2)

    cancel_buy = _f(row.get("cancel_buy"))
    cancel_sell = _f(row.get("cancel_sell"))
    cancel_cum = None
    if cancel_buy is not None or cancel_sell is not None:
        cancel_cum = round((cancel_buy or 0.0) + (cancel_sell or 0.0), 2)

    open_pct = _f(row.get("open_pct"))
    if open_pct is None and ref_price and prev_close:
        open_pct = round((ref_price / prev_close - 1.0) * 100.0, 2)

    ts = str(tick_ts or "")
    return {
        "trade_date": "",  # 由调用方/落库层按交易日补(避免跨日误标)
        "symbol": symbol,
        "market": (market or "CN").upper(),
        "tick_ts": ts,
        "time_bucket": ts[:5],
        "ref_price": ref_price,
        "match_vol": match_vol,
        "unmatched_vol": unmatched,
        "bid1_price": bid1_price,
        "bid1_vol": bid1_vol,
        "ask1_price": ask1_price,
        "ask1_vol": ask1_vol,
        "open_amount": _f(row.get("open_amount")),
        "open_limit_buy": _f(row.get("open_limit_buy")),
        "prev_close": prev_close,
        "open_pct": open_pct,
        "cancel_cum": cancel_cum,
        "withdraw_rate": withdraw_rate(cancel_cum, match_vol),
        "verdict": classify_verdict(open_pct, symbol),
        "source": source,
        "captured_at": float(captured_at if captured_at is not None else time.time()),
        "raw": {
            "last_close": row.get("last_close"),
            "open": row.get("open"),
            "open_pct": row.get("open_pct"),
            "open_amount": row.get("open_amount"),
            "open_limit_buy": row.get("open_limit_buy"),
            "buy1": row.get("buy1"),
            "buy1_vol": row.get("buy1_vol"),
            "sell1": row.get("sell1"),
            "sell1_vol": row.get("sell1_vol"),
            "vol": row.get("vol"),
            "cancel_buy": row.get("cancel_buy"),
            "cancel_sell": row.get("cancel_sell"),
        },
    }


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def upsert_frames(
    frames: Iterable[dict],
    *,
    trade_date: str | None = None,
    session: Session | None = None,
    session_factory=SessionLocal,
) -> int:
    """幂等写入帧(同 (交易日, 标的, 帧时刻, 来源) 原地覆盖)。返回写入行数。"""
    items = [f for f in (frames or []) if f and f.get("symbol") and f.get("tick_ts")]
    if not items:
        return 0

    def _run(sess: Session) -> int:
        n = 0
        for f in items:
            td = trade_date or f.get("trade_date")
            if not td:
                continue
            values = {k: v for k, v in f.items() if k != "trade_date"}
            values["trade_date"] = td
            row = sess.scalar(
                select(AuctionTickSnapshot).where(
                    AuctionTickSnapshot.trade_date == td,
                    AuctionTickSnapshot.symbol == f["symbol"],
                    AuctionTickSnapshot.market == (values.get("market") or "CN"),
                    AuctionTickSnapshot.tick_ts == f["tick_ts"],
                    AuctionTickSnapshot.source == (values.get("source") or "tq"),
                )
            )
            if row is None:
                sess.add(AuctionTickSnapshot(**values))
            else:
                for k, v in values.items():
                    setattr(row, k, v)
            n += 1
        if n:
            sess.commit()
        return n

    if session is not None:
        return _run(session)
    with session_factory() as sess:
        return _run(sess)


# --------------------------------------------------------------------------- #
# 读(过程回放)
# --------------------------------------------------------------------------- #
def load_frames(
    trade_date: str,
    *,
    symbol: str | None = None,
    market: str = "CN",
    session: Session | None = None,
    session_factory=SessionLocal,
) -> list[dict]:
    """读某交易日的过程帧(按 tick_ts 升序)。**不**回源 —— 历史帧只存在于本表。"""

    def _run(sess: Session) -> list[dict]:
        stmt = select(AuctionTickSnapshot).where(
            AuctionTickSnapshot.trade_date == trade_date
        )
        if symbol:
            stmt = stmt.where(
                AuctionTickSnapshot.symbol == symbol,
                AuctionTickSnapshot.market == (market or "CN").upper(),
            )
        stmt = stmt.order_by(
            AuctionTickSnapshot.symbol.asc(), AuctionTickSnapshot.tick_ts.asc()
        )
        return [_row_to_dict(r) for r in sess.scalars(stmt).all()]

    if session is not None:
        return _run(session)
    with session_factory() as sess:
        return _run(sess)


def _row_to_dict(r: AuctionTickSnapshot) -> dict:
    return {
        "trade_date": r.trade_date,
        "symbol": r.symbol,
        "market": r.market,
        "tick_ts": r.tick_ts,
        "time_bucket": r.time_bucket,
        "ref_price": r.ref_price,
        "match_vol": r.match_vol,
        "unmatched_vol": r.unmatched_vol,
        "bid1_price": r.bid1_price,
        "bid1_vol": r.bid1_vol,
        "ask1_price": r.ask1_price,
        "ask1_vol": r.ask1_vol,
        "open_amount": r.open_amount,
        "open_limit_buy": r.open_limit_buy,
        "prev_close": r.prev_close,
        "open_pct": r.open_pct,
        "cancel_cum": r.cancel_cum,
        "withdraw_rate": r.withdraw_rate,
        "verdict": r.verdict,
        "source": r.source,
        "captured_at": r.captured_at,
    }


def replay(trade_date: str, symbol: str, *, market: str = "CN", **kw) -> dict:
    """单票竞价过程回放摘要: 帧序列 + 首末价/撤单率/判定变化。

    只陈述数据, 不推断"主力意图"(那是上层分析的事)。
    """
    frames = load_frames(trade_date, symbol=symbol, market=market, **kw)
    if not frames:
        return {
            "available": False,
            "trade_date": trade_date,
            "symbol": symbol,
            "frames": [],
            "note": "该交易日无竞价过程帧(采样器未运行或该票未覆盖)",
        }
    prices = [f["ref_price"] for f in frames if f["ref_price"] is not None]
    rates = [f["withdraw_rate"] for f in frames if f["withdraw_rate"] is not None]
    return {
        "available": True,
        "trade_date": trade_date,
        "symbol": symbol,
        "frame_count": len(frames),
        "first_ts": frames[0]["tick_ts"],
        "last_ts": frames[-1]["tick_ts"],
        "first_price": prices[0] if prices else None,
        "last_price": prices[-1] if prices else None,
        "price_change_pct": (
            round((prices[-1] / prices[0] - 1.0) * 100.0, 2)
            if len(prices) >= 2 and prices[0] else None
        ),
        "max_withdraw_rate": max(rates) if rates else None,
        "verdicts": sorted({f["verdict"] for f in frames if f.get("verdict")}),
        "frames": frames,
    }


def purge_old_frames(
    *, keep_days: int = RETENTION_DAYS, today: str | None = None,
    session: Session | None = None, session_factory=SessionLocal,
) -> int:
    """清理超过保留期的帧(按 trade_date 文本比较)。返回删除行数。"""
    base = date.fromisoformat(today) if today else date.today()
    cutoff = (base - timedelta(days=max(1, int(keep_days)))).isoformat()

    def _run(sess: Session) -> int:
        res = sess.execute(
            delete(AuctionTickSnapshot).where(AuctionTickSnapshot.trade_date < cutoff)
        )
        sess.commit()
        return int(res.rowcount or 0)

    if session is not None:
        return _run(session)
    with session_factory() as sess:
        return _run(sess)


# --------------------------------------------------------------------------- #
# 采样(网络边界)
# --------------------------------------------------------------------------- #
def _tq_code(symbol: str) -> str:
    from marketdata.symbol import Symbol
    from marketdata.vendors.tq import to_tq_code

    return to_tq_code(Symbol.parse(symbol, "CN")) or ""


def fetch_frame_rows(symbols: list[str], *, limit: int = 0) -> dict[str, dict]:
    """逐票取 TQ 快照 + more_info 竞价字段。返回 {symbol: row}; 失败票跳过。

    与 `auction_collector.fetch_auction_snapshots_tq` 同源, 但额外带出
    `BCancel/SCancel`(撤单率)与 `Vol`(已匹配量), 这是过程回放的两个关键量。
    """
    from marketdata.vendors import tq as _tq

    out: dict[str, dict] = {}
    syms = [s for s in (symbols or []) if s]
    if limit and limit > 0:
        syms = syms[:limit]
    for sym in syms:
        try:
            code = _tq_code(sym)
            if not code:
                continue
            snap = _tq._rpc("get_market_snapshot", {"stock_code": code})
            if not isinstance(snap, dict):
                continue
            try:
                mi = _tq._rpc("get_more_info", {"stock_code": code}) or {}
            except Exception:  # noqa: BLE001 - 扩展字段缺失不致命
                mi = {}
            if not isinstance(mi, dict):
                mi = {}
            out[sym] = {
                "last_close": snap.get("LastClose"),
                "open": snap.get("Open"),
                "vol": snap.get("Vol"),
                "buy1": (snap.get("Buyp") or [None])[0],
                "buy1_vol": (snap.get("Buyv") or [None])[0],
                "sell1": (snap.get("Sellp") or [None])[0],
                "sell1_vol": (snap.get("Sellv") or [None])[0],
                "open_pct": mi.get("OpenZAF"),
                "open_amount": mi.get("OpenAmo"),
                "open_limit_buy": mi.get("OpenZTBuy"),
                "cancel_buy": mi.get("BCancel"),
                "cancel_sell": mi.get("SCancel"),
            }
        except Exception as e:  # noqa: BLE001 - 单票失败不影响其余
            logger.debug("竞价帧采样失败 %s: %s", sym, e)
        if PER_SYMBOL_SLEEP > 0:
            time.sleep(PER_SYMBOL_SLEEP)
    return out


def pool_symbols(max_symbols: int | None = None) -> list[str]:
    """竞价采样池: **近期涨停池优先** + 全市场补齐到 `max_symbols`。

    为什么不是"全市场按代码切前 800": 竞价过程的价值集中在"昨日涨停/连板"这类
    有资金博弈的票上(方案 §6.1 第一问就是"昨天涨停的票今天竞价强弱如何");
    按代码切会系统性漏掉 300/688 段。涨停池(历史去重)是现成的、零额外成本的热池。
    `max_symbols<=0` → 全市场(不补也不截)。
    """
    cap = MAX_SYMBOLS if max_symbols is None else int(max_symbols)
    out: list[str] = []
    try:
        from src.core.klines_fullmarket import limit_up_symbols

        out = [s for s in (limit_up_symbols() or []) if s]
    except Exception as e:  # noqa: BLE001 - 热池不可用不影响主流程
        logger.debug("竞价采样池: 涨停池读取失败 %s", e)

    if cap <= 0 or len(out) < cap:
        try:
            from src.collectors.stock_list import get_stock_list

            seen = set(out)
            for item in get_stock_list() or []:
                sym = str(item.get("symbol") or "")
                if len(sym) == 6 and sym.isdigit() and sym not in seen:
                    out.append(sym)
                    seen.add(sym)
                    if 0 < cap <= len(out):
                        break
        except Exception as e:  # noqa: BLE001
            logger.warning("竞价采样池: 全市场名单读取失败 %s", e)

    return out[:cap] if cap > 0 else out


def sample_once(
    *,
    symbols: list[str] | None = None,
    now: datetime | None = None,
    tick_ts: str | None = None,
    fetcher: Callable[[list[str]], dict[str, dict]] | None = None,
) -> dict:
    """采一帧全市场并落库。返回统计(不抛)。"""
    from src.core.trade_phase import cn_now

    dt = cn_now(now)
    trade_date = dt.date().isoformat()
    ts = tick_ts or dt.strftime("%H:%M:%S")

    if symbols is None:
        symbols = pool_symbols()

    if MAX_SYMBOLS > 0:
        symbols = symbols[:MAX_SYMBOLS]
    if not symbols:
        return {"ok": False, "reason": "采样池为空(涨停池与全市场名单均不可用)", "frames": 0}

    fetch = fetcher or (lambda syms: fetch_frame_rows(syms))
    try:
        rows = fetch(symbols) or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"竞价快照取数失败: {e}", "frames": 0}

    frames = []
    for sym, row in rows.items():
        frame = frame_from_snapshot(sym, row, tick_ts=ts, market="CN")
        if frame:
            frame["trade_date"] = trade_date
            frames.append(frame)

    persisted = 0
    persist_error = None
    if frames:
        try:
            persisted = upsert_frames(frames, trade_date=trade_date)
        except Exception as e:  # noqa: BLE001
            persist_error = str(e) or e.__class__.__name__
            logger.warning("竞价帧落库失败(%s %s): %s", trade_date, ts, e)

    ok = persisted > 0
    return {
        "ok": ok,
        "trade_date": trade_date,
        "tick_ts": ts,
        "symbols": len(symbols),
        "sampled": len(rows),
        "frames": len(frames),
        "persisted": persisted,
        "persist_error": persist_error,
        "reason": None if ok else (
            persist_error or "本轮无可用竞价帧(非竞价时段/客户端未推快照)"
        ),
    }


def run_auction_tick_job(
    *,
    now: datetime | None = None,
    symbols: list[str] | None = None,
    fetcher: Callable[[list[str]], dict[str, dict]] | None = None,
) -> dict:
    """cron 入口(9:15-9:25 每分钟): 窗口/交易日自守卫 → 采一帧 → 落库。"""
    from src.core.jobs import jobs
    from src.core.trade_phase import cn_now
    from src.core.trading_calendar import is_trading_day

    dt = cn_now(now)
    if not is_trading_day(dt.date()):
        return {"ok": True, "skipped": "non_trading_day", "frames": 0}
    hhmmss = dt.strftime("%H:%M:%S")
    if not (WINDOW_START <= hhmmss <= WINDOW_END):
        # 窗口外**不记作业**(否则每分钟一条无意义记录), 直接跳过并说明
        return {"ok": True, "skipped": "outside_auction_window",
                "now": hhmmss, "frames": 0}

    job_id, is_new = jobs.create("auction_tick_sampler", "竞价过程帧采样")
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id}
    purged = None
    if hhmmss.startswith("09:15"):
        # 每日首帧顺带做保留期清理(365 天), 避免再加一个 job; 失败不影响采样
        try:
            purged = purge_old_frames()
        except Exception as e:  # noqa: BLE001
            logger.warning("竞价帧保留期清理失败: %s", e)
    try:
        out = sample_once(now=now, tick_ts=hhmmss, symbols=symbols, fetcher=fetcher)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("竞价帧采样作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    if purged is not None:
        out["purged"] = purged
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="竞价帧采样: ")
    return out
