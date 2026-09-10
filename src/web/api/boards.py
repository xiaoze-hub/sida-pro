"""板块数据 API(阶段2.2, v0.3.0): 板块/概念列表 + 板块详情 + 成分股 + 轮动排序。

端点(全部需登录, 挂载前缀 /api/boards):
    GET /api/boards?type=industry|concept   板块/概念列表(DB, 每日 cron 同步)
    GET /api/boards/rotation?days=5         板块轮动排序(compute_rotation)
    GET /api/boards/{block_code}            板块详情(含今日 change_pct/fund_net)
    GET /api/boards/{block_code}/constituents  成分股(thsdk 实时, 1 小时缓存)

路由顺序注意: /rotation 静态路径必须先于 /{block_code} 声明,
否则 FastAPI 会把 "rotation" 当作 block_code 匹配。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from src.core.quote_snapshots import in_trading_window
from src.core.thsdk_board import (
    _extract_block_metrics,
    compute_rotation,
    fetch_block_constituents,
    fetch_block_detail,
    fetch_block_snapshots,
)
from src.web.database import get_db
from src.web.models import Board, BoardDaily

logger = logging.getLogger(__name__)

router = APIRouter(tags=["boards"])

# 允许的板块类型
_VALID_TYPES = ("industry", "concept")

# ── 实时模式(2026-09-10 盘中实时化): 交易时段内热力图 60s 自动刷新 ─────────────
# 仅 "扩展" 档(涨幅/主力净流入/量比/涨速), 面积量能仍用日线成交额(日内相对大小稳定)。
# 60s 服务端缓存 + 单飞(拉取 ~10s, 单 worker 内并发请求只拉一次); 失败静默回落日线。
_LIVE_TTL_S = 60.0
_LIVE_CACHE: dict[str, tuple[float, dict]] = {}
_LIVE_LOCK = threading.Lock()


def _clear_live_cache() -> None:
    """清空实时快照缓存(测试隔离/手动刷新用)。"""
    with _LIVE_LOCK:
        _LIVE_CACHE.clear()


def _live_snapshot(btype: str, codes: list[str]) -> dict | None:
    """60s 缓存的实时快照; 失败返回 None(调用方回落日线, 不报错)。"""
    hit = _LIVE_CACHE.get(btype)
    if hit is not None and time.time() - hit[0] < _LIVE_TTL_S:
        return hit[1]
    with _LIVE_LOCK:  # 单飞: 并发只拉一次, 后到者等锁后取缓存
        hit = _LIVE_CACHE.get(btype)
        if hit is not None and time.time() - hit[0] < _LIVE_TTL_S:
            return hit[1]
        try:
            snaps = fetch_block_snapshots(codes, modes=("扩展",))
        except Exception as e:  # noqa: BLE001 - 实时失败静默回落
            logger.warning("板块实时快照拉取失败(%s): %s", btype, e)
            return None
        if not snaps:
            return None
        _LIVE_CACHE[btype] = (time.time(), snaps)
        return snaps


def _merge_live(items: list[dict], snaps: dict) -> int:
    """实时字段就地覆盖(缺失字段保留日线值); 返回命中数量。"""
    merged = 0
    for it in items:
        s = snaps.get(it.get("block_code"))
        if not s:
            continue
        if s.get("change_pct") is not None:
            it["change_pct"] = s["change_pct"]
        if s.get("fund_net") is not None:
            it["fund_net"] = s["fund_net"]
        it["volume_ratio"] = s.get("volume_ratio")
        it["speed"] = s.get("speed")
        it["live"] = True
        merged += 1
    return merged


@router.get("")
def list_boards(
    type: str = Query("industry", description="industry / concept"),
    db: Session = Depends(get_db),
):
    """板块/概念列表(来自 Boards 表, cron 每日同步)。"""
    btype = (type or "industry").lower()
    if btype not in _VALID_TYPES:
        raise HTTPException(400, f"type 仅支持 {'/'.join(_VALID_TYPES)}")

    try:
        rows = (
            db.query(Board)
            .filter(Board.board_type == btype)
            .order_by(Board.name)
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("查询板块列表失败: %s", e)
        raise HTTPException(502, f"查询板块列表失败: {e}")

    return [
        {
            "block_code": b.block_code,
            "name": b.name or "",
            "board_type": b.board_type or "",
            "last_synced_at": b.last_synced_at.isoformat() if b.last_synced_at else None,
        }
        for b in rows
    ]


@router.get("/rotation")
def board_rotation(
    days: int = Query(5, ge=1, le=30, description="统计窗口天数"),
):
    """板块轮动排序(强度分 0-100 降序)。"""
    try:
        rows = compute_rotation(days=days)
        return {"days": max(1, int(days)), "items": rows}
    except Exception as e:  # noqa: BLE001
        logger.warning("板块轮动接口异常: %s", e)
        raise HTTPException(502, f"板块轮动计算失败: {e}")


@router.get("/heatmap")
def board_heatmap(
    type: str = Query("industry", description="industry / concept"),
    live: str = Query("auto", description="auto=交易时段内自动实时 / 1=强制实时 / 0=仅日线"),
    db: Session = Depends(get_db),
):
    """全板块最新日线一次拉取(treemap 热力图数据源, P1-1 2026-09-10);

    live 模式(2026-09-10 盘中实时化): auto 时交易时段内用 thsdk "扩展" 批量实时快照覆盖
    (涨跌幅/资金净流入/量比/涨速), 面积量能仍用日线成交额; 非时段/失败回落日线。
    响应带 live/as_of 标注(前端据此显示"实时 HH:MM"/"数据截至 date")。

    每板块取 BoardDaily 最新一行(逐板块 latest, 非全局同日 —— 同步不全时不丢板块);
    Boards 已注册但无日线的板块也返回(has_daily=False + 指标 None, 前端显式标注无数据)。
    trade_date = 有日线项中的最新日期, 供 UI 基准日标注。

    路由顺序注意: 本静态路径必须先于 /{block_code} 声明, 否则 "heatmap" 会被当作 block_code。
    """
    btype = (type or "industry").lower()
    if btype not in _VALID_TYPES:
        raise HTTPException(400, f"type 仅支持 {'/'.join(_VALID_TYPES)}")
    live_mode = (live or "auto").lower()
    if live_mode not in ("auto", "1", "0"):
        raise HTTPException(400, "live 仅支持 auto/1/0")

    try:
        boards = db.query(Board).filter(Board.board_type == btype).all()
        latest = (
            db.query(
                BoardDaily.block_code,
                func.max(BoardDaily.date).label("max_date"),
            )
            .group_by(BoardDaily.block_code)
            .subquery()
        )
        daily_rows = (
            db.query(BoardDaily)
            .join(
                latest,
                and_(
                    BoardDaily.block_code == latest.c.block_code,
                    BoardDaily.date == latest.c.max_date,
                ),
            )
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("查询板块热力图数据失败: %s", e)
        raise HTTPException(502, f"查询板块热力图数据失败: {e}")

    daily_by_code = {r.block_code: r for r in daily_rows}
    items: list[dict[str, Any]] = []
    for b in boards:
        d = daily_by_code.get(b.block_code)
        items.append(
            {
                "block_code": b.block_code,
                "name": b.name or "",
                "board_type": b.board_type or "",
                "change_pct": d.change_pct if d is not None else None,
                "fund_net": d.fund_net if d is not None else None,
                "volume": d.volume if d is not None else None,
                "date": d.date.strftime("%Y-%m-%d") if d is not None and d.date else None,
                "has_daily": d is not None,
                # 实时字段(仅 live 命中的项有值; 前端据此做异动高亮)
                "volume_ratio": None,
                "speed": None,
                "live": False,
            }
        )

    # 实时覆盖: auto=交易时段内, "1"=强制; "0"=不实时。失败回落日线(静默, 不报错)
    use_live = live_mode == "1" or (live_mode == "auto" and in_trading_window())
    live_merged = 0
    as_of: str | None = None
    if use_live:
        snaps = _live_snapshot(btype, [i["block_code"] for i in items])
        if snaps:
            live_merged = _merge_live(items, snaps)
            if live_merged > 0:
                as_of = datetime.now(timezone.utc).isoformat()

    # 排序: 涨跌幅降序, 无数据排最后(Python 侧排, 不依赖方言 NULLS LAST 支持)
    items.sort(key=lambda x: (x["change_pct"] is None, -(x["change_pct"] or 0.0), x["name"]))

    dates = [i["date"] for i in items if i["date"]]
    return {
        "type": btype,
        "trade_date": max(dates) if dates else None,
        "count": len(items),
        "live": live_merged > 0,
        "live_count": live_merged,
        "as_of": as_of,
        "items": items,
    }


@router.get("/{block_code}")
def board_detail(
    block_code: str,
    db: Session = Depends(get_db),
):
    """板块详情: Board 元信息 + 最新日线指标(今日 change_pct/fund_net)。

    DB 无该板块时尝试实时拉取 thsdk(bucket 兜底); 都失败返回 404。
    """
    result: dict[str, Any] = {}
    code = block_code.strip()
    if not code:
        raise HTTPException(400, "block_code 不能为空")

    board = None
    daily = None
    try:
        board = db.query(Board).filter(Board.block_code == code).first()
        daily = (
            db.query(BoardDaily)
            .filter(BoardDaily.block_code == code)
            .order_by(BoardDaily.date.desc())
            .first()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("查询板块详情失败 %s: %s", code, e)
        board = None
        daily = None

    result["block_code"] = code
    name: str = ""
    btype: str = ""
    if board is not None:
        name = str(board.name or "")
        btype = str(board.board_type or "")
    result["name"] = name
    result["board_type"] = btype

    if daily is not None:
        result["today"] = {
            "date": daily.date.strftime("%Y-%m-%d"),
            "change_pct": daily.change_pct,
            "fund_net": daily.fund_net,
            "volume": daily.volume,
        }
        result["has_daily"] = True
        result["live"] = False
    else:
        result["today"] = None
        result["has_daily"] = False
        # DB 无日线 → 尝试实时拉取(1 小时缓存)
        detail = fetch_block_detail(code)
        if not detail:
            raise HTTPException(404, f"板块不存在或数据不可用: {code}")
        metrics = _extract_block_metrics(detail)
        result["today"] = {
            "date": None,
            "change_pct": metrics["change_pct"],
            "fund_net": metrics["fund_net"],
            "volume": metrics["volume"],
        }
        result["live"] = True
    return result


@router.get("/{block_code}/constituents")
def board_constituents(block_code: str):
    """板块成分股(thsdk 实时, 1 小时缓存)。失败返回 404。"""
    code = block_code.strip()
    if not code:
        raise HTTPException(400, "block_code 不能为空")
    rows = fetch_block_constituents(code)
    if rows is None:
        raise HTTPException(404, f"板块成分股拉取失败: {code}")
    # 成分股可能很多, 抽样返回前 300 条避免响应过大(完整实时数据已在缓存)
    return {"block_code": code, "count": len(rows), "items": rows[:300]}