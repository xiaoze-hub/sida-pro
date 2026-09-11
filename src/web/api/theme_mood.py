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

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter()
_scan_lock = threading.Lock()
_scan_running = False
_WINDOWS = (10, 20, 30)
_TOPS = (6, 8, 10, 15)


def _read(sql: str, params: dict) -> list[dict]:
    from sqlalchemy import text

    from src.db.session import engine

    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [dict(r._mapping) for r in rows]


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


def _board_rows(window: int, top: int) -> list[dict]:
    """Top 榜 + 每题材 matrix cells; 无数据 → []。"""
    from src.core.theme_mood import rank_items

    days = _read("SELECT DISTINCT trade_date FROM theme_mood_daily ORDER BY trade_date DESC LIMIT :n",
                 {"n": int(window)})
    dates = sorted([d["trade_date"] for d in days])
    if not dates:
        return []
    latest = dates[-1]
    rows = _read("SELECT * FROM theme_mood_daily WHERE trade_date = :d", {"d": latest})
    if not rows:
        return []
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
    for r in rows:
        code = r["block_code"]
        recent = hist_scores.get(code, [])[-3:]
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
        })
    return rank_items(items)[: int(top)]


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
    global _scan_running
    with _scan_lock:
        if _scan_running:
            return {"started": False, "reason": "扫描进行中"}
        _scan_running = True

    def _runner() -> None:
        global _scan_running
        try:
            from src.core import theme_mood

            logger.info("手动题材情绪扫描完成: %s", theme_mood.scan(write_days=1))
        except Exception as e:  # noqa: BLE001
            logger.warning("手动题材情绪扫描失败: %s", e)
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=_runner, name="theme-mood-scan", daemon=True).start()
    return {"started": True, "reason": None}


@router.get("/board")
def get_board(window: int = Query(20), top: int = Query(15)):
    if window not in _WINDOWS or top not in _TOPS:
        raise HTTPException(400, f"window 仅支持 {_WINDOWS}, top 仅支持 {_TOPS}")
    items = _board_rows(window, top)
    return {"trade_date": _latest_date(), "window": window, "count": len(items), "items": items}


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
