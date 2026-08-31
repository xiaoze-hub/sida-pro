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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.core.thsdk_board import (
    _extract_block_metrics,
    compute_rotation,
    fetch_block_constituents,
    fetch_block_detail,
)
from src.web.database import get_db
from src.web.models import Board, BoardDaily

logger = logging.getLogger(__name__)

router = APIRouter(tags=["boards"])

# 允许的板块类型
_VALID_TYPES = ("industry", "concept")


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