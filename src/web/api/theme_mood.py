"""题材情绪分 API(2026-09-12): 榜单 / 题材详情 / 手动扫描。

GET  /api/theme-mood/board?window=20&top=15   Top 题材榜 + 20 日矩阵(读落库)
GET  /api/theme-mood/detail/{block_code}      单题材逐日五维明细
POST /api/theme-mood/scan/run                 手动触发扫描(后台线程)

口径唯一事实源: src/core/theme_mood.py(设计见 docs/research/题材情绪分_设计方案_20260912.md)。
响应信封由全局中间件(src/web/response.py)统一包装。
"""
from __future__ import annotations

import json
import logging
import threading

from src.core.jobs import jobs
from src.core.theme_rotation import daily_top_sets, membership_flags, rotation_series

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter()
_WINDOWS = (10, 20, 30)
_TOPS = (6, 8, 10, 15)
# 轮动口径: 每日 Top-K 的 K。与榜单 top 参数解耦 —— 榜单看"今天谁强",
# 轮动看"窗口内谁进出过", 两者 K 不必相同。
ROTATION_TOP_K = 10


def _read(sql: str, params: dict) -> list[dict]:
    from sqlalchemy import text

    from src.db.session import engine

    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [dict(r._mapping) for r in rows]


def _read_codes(sql: str, params: dict, codes: tuple[str, ...]) -> list[dict]:
    """带 `IN :codes` 的查询(text 参数不能展开元组, 必须 expanding bindparam)。"""
    from sqlalchemy import bindparam, text

    from src.db.session import engine

    with engine.begin() as conn:
        rows = conn.execute(
            text(sql).bindparams(bindparam("codes", expanding=True)),
            {**params, "codes": codes},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def _read_ohlc(dates: list[str], symbols: list[str]) -> dict:
    """klines 日线 OHLC(qfq, 唯一常驻复权维度) → {(date, symbol): {o,h,l,c}}。

    两个 IN 列表都用 expanding bindparam; 任一入参为空短路返回 {}。
    OHLC 任一为 None 的行丢弃(调用方落 candle=None, 不编)。
    """
    if not dates or not symbols:
        return {}
    from sqlalchemy import bindparam, text

    from src.db.session import engine

    stmt = text(
        "SELECT ts, symbol, open, high, low, close FROM klines "
        "WHERE period = '1d' AND adjust = 'qfq' AND ts IN :dates AND symbol IN :codes"
    ).bindparams(bindparam("dates", expanding=True), bindparam("codes", expanding=True))
    with engine.begin() as conn:
        raw = conn.execute(
            stmt, {"dates": tuple(sorted(dates)), "codes": tuple(sorted(symbols))}
        ).fetchall()
    out = {}
    for r in raw:
        m = dict(r._mapping)
        if None in (m["open"], m["high"], m["low"], m["close"]):
            continue
        out[(str(m["ts"]), str(m["symbol"]))] = {
            "o": m["open"], "h": m["high"], "l": m["low"], "c": m["close"]}
    return out


def _loads(v):
    if not v:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _latest_date() -> str | None:
    rows = _read("SELECT MAX(trade_date) AS d FROM theme_mood_daily", {})
    return rows[0]["d"] if rows else None


def _board_data(window: int, top: int) -> dict:
    """Top 榜 + 每题材按共享日期轴对齐的 matrix cells + 强势情绪走势; 无数据 → 空结构。"""
    from src.core.theme_mood import align_cells, market_series, rank_items

    days = _read("SELECT DISTINCT trade_date FROM theme_mood_daily ORDER BY trade_date DESC LIMIT :n",
                 {"n": int(window)})
    dates = sorted([d["trade_date"] for d in days])
    if not dates:
        return {"dates": [], "items": [], "market": [], "rotation": [], "rotation_top_k": ROTATION_TOP_K}
    latest = dates[-1]
    rows = _read("SELECT * FROM theme_mood_daily WHERE trade_date = :d", {"d": latest})
    if not rows:
        return {"dates": [], "items": [], "market": [], "rotation": [], "rotation_top_k": ROTATION_TOP_K}
    prev = dates[-2] if len(dates) > 1 else None
    prev_map: dict[str, float] = {}
    if prev:
        prev_map = {r["block_code"]: r["score"] for r in _read(
            "SELECT block_code, score FROM theme_mood_daily WHERE trade_date = :d", {"d": prev})}
    hist = _read(
        "SELECT block_code, trade_date, score, limit_up_cnt FROM theme_mood_daily WHERE trade_date >= :a"
        " ORDER BY trade_date", {"a": dates[0]}
    )
    cells: dict[str, list[dict]] = {}
    hist_scores: dict[str, list[float]] = {}
    for h in hist:
        cells.setdefault(h["block_code"], []).append(
            {"date": h["trade_date"], "score": h["score"], "limit_up_cnt": h["limit_up_cnt"]})
        if h["score"] is not None:
            hist_scores.setdefault(h["block_code"], []).append(float(h["score"]))
    items = []
    latest_codes = {r["block_code"] for r in rows}
    # 轮动(v0.5.81): 行集合 = 最新一天 ∪ 窗口内进过每日 Top-K 的题材。
    # 只取最新一天的话,"昨天还热今天掉榜"的题材整行消失, 轮动就看不见。
    top_sets = daily_top_sets(hist, top_k=ROTATION_TOP_K)
    flags = membership_flags(dates, top_sets)
    rotation = rotation_series(dates, top_sets)
    union_codes = latest_codes | set(flags)
    meta: dict[str, dict] = {r["block_code"]: r for r in rows}
    if union_codes - latest_codes:
        extra = _read_codes(
            "SELECT block_code, block_name, block_type, MAX(trade_date) AS last_d "
            "FROM theme_mood_daily WHERE trade_date >= :a AND block_code IN :codes "
            "GROUP BY block_code", {"a": dates[0]}, tuple(sorted(union_codes - latest_codes)),
        )
        for e in extra:
            meta[e["block_code"]] = {**e, "score": None, "confidence": None, "core": 0,
                                     "s1": None, "s2": None, "s3": None, "s4": None, "s5": None,
                                     "limit_up_cnt": None, "max_boards": None, "core_stocks": None}
    for code in union_codes:
        r = meta.get(code)
        if r is None:
            continue
        recent = hist_scores.get(code, [])[-3:]
        fl = flags.get(code, {"top_days": 0, "first_top_date": None,
                              "last_top_date": None, "in_top_today": False})
        items.append({
            "block_code": code, "block_name": r["block_name"], "block_type": r["block_type"],
            "score": r["score"],
            "delta": (round((r["score"] or 0) - prev_map[code], 1)
                      if code in prev_map and r["score"] is not None else None),
            "confidence": r["confidence"], "core": bool(r["core"]),
            "s1": r["s1"], "s2": r["s2"], "s3": r["s3"], "s4": r["s4"], "s5": r["s5"],
            "limit_up_cnt": r["limit_up_cnt"], "max_boards": r["max_boards"],
            "core_stocks": _loads(r["core_stocks"]) or [],
            "score3_avg": (round(sum(recent) / len(recent), 1) if recent else None),
            "cells": cells.get(code, []),
            "top_days": fl["top_days"], "first_top_date": fl["first_top_date"],
            "last_top_date": fl["last_top_date"], "in_top_today": fl["in_top_today"],
        })
    ranked = rank_items(items)
    for it in ranked:
        it["cells"] = align_cells(it["cells"], dates)
    return {"dates": dates, "items": ranked, "market": market_series(hist, dates),
            "rotation": rotation, "rotation_top_k": ROTATION_TOP_K}


def _detail_rows(block_code: str, days: int) -> list[dict]:
    rows = _read(
        "SELECT * FROM theme_mood_daily WHERE block_code = :c ORDER BY trade_date DESC LIMIT :n",
        {"c": block_code, "n": int(days)},
    )
    out = []
    for r in reversed(rows):
        out.append({
            "trade_date": r["trade_date"], "score": r["score"], "confidence": r["confidence"],
            "core": bool(r["core"]), "s1": r["s1"], "s2": r["s2"], "s3": r["s3"], "s4": r["s4"], "s5": r["s5"],
            "limit_up_cnt": r["limit_up_cnt"], "touched_cnt": r["touched_cnt"],
            "max_boards": r["max_boards"], "ge2_cnt": r["ge2_cnt"],
            "core_stocks": _loads(r["core_stocks"]) or [],
            "detail": _loads(r["detail"]) or {}, "breadth": _loads(r["breadth"]) or {},
        })
    return out


def _spawn_scan() -> dict:
    """起一次扫描作业。

    2026-09-12: 模块级 `_scan_lock/_scan_running` 换成统一作业框架 —— 单飞复用
    (重复点击拿回同一个 job_id) + 进度落库, /api/jobs 与作业面板里看得见。
    """
    job_id, is_new = jobs.create("theme_mood_scan", "题材情绪分扫描")
    if not is_new:
        return {"started": False, "reason": "扫描进行中", "job_id": job_id}

    def _runner() -> None:
        try:
            from src.core import theme_mood

            jobs.start(job_id, "scanning")
            out = theme_mood.scan(write_days=1, on_progress=jobs.progress_reporter(job_id))
            jobs.succeed(job_id, str(out)[:500])
            logger.info("手动题材情绪扫描完成: %s", out)
        except Exception as e:  # noqa: BLE001
            jobs.fail(job_id, str(e))
            logger.warning("手动题材情绪扫描失败: %s", e)

    threading.Thread(target=_runner, name="theme-mood-scan", daemon=True).start()
    return {"started": True, "reason": None, "job_id": job_id}


@router.get("/ladder")
def get_ladder(window: int = Query(20, ge=5, le=60)):
    """连板梯队(v0.5.81, 老板: "连扳梯队也要做")。

    通达信式天梯: 每日按连板高度分组列个股。连板数**不信任 limit_days 列**(从未落库),
    从事件表自己推: 沿表内日期序列数连续收盘封板日。只算收盘封板, touch 未封不算。
    """
    from src.core.limit_ladder import ladder_window

    days = _read(
        "SELECT DISTINCT trade_date FROM limit_up_events ORDER BY trade_date DESC LIMIT :n",
        {"n": int(window)},
    )
    dates = sorted(d["trade_date"] for d in days)
    if not dates:
        return {"dates": [], "ladder": [], "note": "limit_up_events 无数据"}
    events = _read(
        "SELECT trade_date, symbol, name, is_sealed_close FROM limit_up_events "
        "WHERE trade_date >= :a ORDER BY trade_date", {"a": dates[0]},
    )
    names = {e["symbol"]: e["name"] for e in events if e.get("name")}
    return {"dates": dates, "ladder": ladder_window(dates, events, names),
            "note": "连板数=沿事件表日期序列的连续收盘封板日; 只算收盘封板"}


@router.get("/board")
def get_board(window: int = Query(20), top: int = Query(15)):
    if window not in _WINDOWS or top not in _TOPS:
        raise HTTPException(400, f"window 仅支持 {_WINDOWS}, top 仅支持 {_TOPS}")
    data = _board_data(window, top)
    return {"trade_date": _latest_date(), "window": window, "count": len(data["items"]),
            "dates": data["dates"], "items": data["items"], "market": data["market"],
            "rotation": data["rotation"], "rotation_top_k": data["rotation_top_k"]}


@router.get("/detail/{block_code}")
def get_detail(block_code: str, days: int = Query(20, ge=1, le=60)):
    code = (block_code or "").strip().upper()
    if not code:
        raise HTTPException(400, "block_code 不能为空")
    items = _detail_rows(code, days)
    if not items:
        raise HTTPException(404, f"无该题材数据: {code}")
    return {"block_code": code, "count": len(items), "items": items}


@router.post("/scan/run")
def run_scan():
    return _spawn_scan()
