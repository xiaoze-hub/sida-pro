"""动作回写(2026-10-20, P1)。

「原生融合」的硬指标不是 AI 能读页面, 而是**能改页面**——但绝不能直接改:

    AI 提议(propose) → 落 pending → 用户点确认(apply) → 才真正写库

红线: 这套框架只管「加自选 / 画标注 / 建提醒」这类**可以撤销的低风险动作**。
改持仓、下单、撤单永远不在这里(工具层硬 403)。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

ALLOWED_KINDS = ("annotation", "watchlist", "alert")


class ActionError(Exception):
    """动作不可执行(参数缺失/越权/未知类型)。文案直接给用户看。"""


def propose(db: Session, *, user_id: Any, kind: str, payload: dict, run_id: str | None = None) -> dict:
    from src.db.models import CopilotAction

    if kind not in ALLOWED_KINDS:
        raise ActionError(f"不支持的动作类型：{kind}")
    if not payload:
        raise ActionError("动作内容为空")
    row = CopilotAction(
        run_id=run_id,
        user_id=str(user_id),
        kind=kind,
        payload=json.dumps(payload, ensure_ascii=False),
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


def list_pending(db: Session, user_id: Any, limit: int = 20) -> list[dict]:
    from src.db.models import CopilotAction

    rows = (
        db.query(CopilotAction)
        .filter(CopilotAction.user_id == str(user_id), CopilotAction.status == "pending")
        .order_by(CopilotAction.id.desc())
        .limit(limit)
        .all()
    )
    return [_to_dict(r) for r in rows]


def apply_action(db: Session, *, user_id: Any, action_id: int) -> dict:
    from src.db.models import CopilotAction

    row = (
        db.query(CopilotAction)
        .filter(CopilotAction.id == action_id, CopilotAction.user_id == str(user_id))
        .one_or_none()
    )
    if row is None:
        raise ActionError("动作不存在")
    if row.status != "pending":
        raise ActionError(f"该动作已是 {row.status} 状态")

    payload = json.loads(row.payload or "{}")
    handler = {"watchlist": _apply_watchlist, "annotation": _apply_annotation}.get(row.kind)
    if handler is None:
        raise ActionError(f"{row.kind} 动作暂未开放执行（P2 接入盯盘任务）")

    result = handler(db, user_id=user_id, payload=payload)
    row.status = "applied"
    row.result = result
    row.applied_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _to_dict(row)


def reject_action(db: Session, *, user_id: Any, action_id: int) -> dict:
    from src.db.models import CopilotAction

    row = (
        db.query(CopilotAction)
        .filter(CopilotAction.id == action_id, CopilotAction.user_id == str(user_id))
        .one_or_none()
    )
    if row is None:
        raise ActionError("动作不存在")
    row.status = "rejected"
    row.applied_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _to_dict(row)


# -- handlers ---------------------------------------------------------------

def _apply_watchlist(db: Session, *, user_id: Any, payload: dict) -> str:
    from src.db.models import Stock

    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise ActionError("缺少 symbol")
    exists = (
        db.query(Stock)
        .filter(Stock.user_id == str(user_id), Stock.symbol == symbol)
        .first()
    )
    if exists:
        return f"{symbol} 已在自选"
    db.add(Stock(
        user_id=str(user_id),
        symbol=symbol,
        name=str(payload.get("name") or symbol),
        market=str(payload.get("market") or "CN"),
        sort_order=0,
    ))
    return f"已加入自选：{symbol}"


def _apply_annotation(db: Session, *, user_id: Any, payload: dict) -> str:
    from src.db.models import KlineAnnotation

    symbol = str(payload.get("symbol") or "").strip()
    label = str(payload.get("label") or "").strip()
    if not symbol or not label:
        raise ActionError("标注需要 symbol 和 label")
    price = payload.get("price")
    row = KlineAnnotation(
        user_id=str(user_id),
        symbol=symbol,
        market=str(payload.get("market") or "CN"),
        period=str(payload.get("period") or "1d"),
        price=float(price) if price is not None else None,
        ts=payload.get("ts"),
        label=label[:64],
        note=(payload.get("note") or "")[:500] or None,
        color=str(payload.get("color") or "up")[:16],
        source="ai",
    )
    db.add(row)
    db.commit()
    return f"已标注 {symbol} @ {price if price is not None else payload.get('ts')}：{label}"


def _to_dict(row: Any) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "kind": row.kind,
        "payload": json.loads(row.payload or "{}"),
        "status": row.status,
        "result": row.result,
        "created_at": str(row.created_at or ""),
        "applied_at": str(row.applied_at or ""),
    }
