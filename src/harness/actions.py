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

ALLOWED_KINDS = ("annotation", "watchlist", "alert", "schedule")


class ActionError(Exception):
    """动作不可执行(参数缺失/越权/未知类型)。文案直接给用户看。"""


def propose(db: Session, *, user_id: Any, kind: str, payload: dict, run_id: str | None = None) -> dict:
    from src.db.models import CopilotAction

    if kind not in ALLOWED_KINDS:
        raise ActionError(f"不支持的动作类型：{kind}")
    if not payload:
        raise ActionError("动作内容为空")
    # P1-1.3: 提议时就把"会改什么"算出来给用户看。diff 算失败不能拦住提议
    # （有些动作类型没有 diff），但要在结果里如实为空，不能编。
    try:
        diff = preview_diff(db, user_id=user_id, kind=kind, payload=payload)
    except ActionError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("propose 计算 diff 失败(不阻断): %s", exc)
        diff = []
    row = CopilotAction(
        run_id=run_id,
        user_id=str(user_id),
        kind=kind,
        payload=json.dumps(payload, ensure_ascii=False),
        status="pending",
        diff_json=json.dumps(diff, ensure_ascii=False) if diff else None,
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
    handler = {
        "watchlist": _apply_watchlist,
        "annotation": _apply_annotation,
        "alert": _apply_alert,
        "schedule": _apply_schedule,
    }.get(row.kind)
    if handler is None:
        raise ActionError(f"{row.kind} 动作暂未开放执行")

    # P1-1.3: apply 前拍快照（propose→confirm 之间状态可能已变，快照必须在 apply 前）
    try:
        row.before_snapshot = json.dumps(
            _snapshot(db, user_id=user_id, kind=row.kind, payload=payload), ensure_ascii=False
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("apply 前拍快照失败(不阻断执行, 但该动作将无法回滚): %s", exc)
        row.before_snapshot = None

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


# -- 情景矩阵 → 图上标注(2026-10-22, P1 原生融合) ------------------------------

#: 情景矩阵能落到图上的关键位：只有这两条是**数据算出**的，不许临时加别的线。
_SCENARIO_LEVELS: tuple[tuple[str, str, str, str], ...] = (
    ("support", "支撑位", "up", "情景矩阵支撑位（MA60/筹码带下沿/区间低）"),
    ("resist", "阻力位", "down", "情景矩阵阻力位（MA20/筹码带上沿/区间高）"),
)


def _as_price(v: Any) -> float | None:
    """关键位必须是正的有限数；字符串数字容忍，bool/None/NaN/inf/<=0 一律不画。"""
    import math

    if v is None or isinstance(v, bool):
        return None
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or p <= 0:
        return None
    return round(p, 2)


def _pending_annotations(db: Session, user_id: Any, symbol: str) -> list[tuple[Any, dict]]:
    """该用户在这只票上**仍待确认**的标注提议 [(row, payload)]。

    去重必须在 payload 里比 symbol/label，而 payload 是 Text(JSON)：跨 sqlite/PG 都
    不能依赖 JSON 函数，所以拉 pending 行到 Python 侧过滤（单用户 pending 量级很小）。
    """
    from src.db.models import CopilotAction

    rows = (
        db.query(CopilotAction)
        .filter(
            CopilotAction.user_id == str(user_id),
            CopilotAction.kind == "annotation",
            CopilotAction.status == "pending",
        )
        .order_by(CopilotAction.id.asc())
        .all()
    )
    out: list[tuple[Any, dict]] = []
    for row in rows:
        try:
            payload = json.loads(row.payload or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and str(payload.get("symbol") or "").strip() == symbol:
            out.append((row, payload))
    return out


def propose_scenario_annotations(
    db: Session,
    *,
    user_id: Any,
    symbol: str,
    market: str = "CN",
    matrix: dict | None = None,
    run_id: str | None = None,
) -> list[dict]:
    """把情景矩阵算出的关键位提议成**待确认**的 K 线标注(2026-10-22, P1)。

    为什么要有它：情景矩阵已经把支撑/阻力算得很准，但结论只活在聊天里——用户看到
    「阻力 35.8」还得自己回图上画。G4 要求「结论落在图上」，这一层就是那条线。

    三条纪律：
      1. 只提议，不落 KlineAnnotation（用户点确认才画）；
      2. 关键位只来自 `matrix["levels"]`，缺失/非法就不画（不猜、不补均价）；
      3. **按 (user_id, symbol, label) 去重**：同一档位只留一条 pending，价格变了就
         原地更新 payload，而不是每问一次就在待确认列表里堆一条。

    返回提议出的动作 dict（含 `dedup` 标记复用/新建）。
    """
    sym = str(symbol or "").strip()
    if not sym:
        return []
    if matrix is None:
        try:
            from src.core.scenario_engine import build_matrix

            matrix = build_matrix(db, sym, market)
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(matrix, dict) or not matrix.get("available"):
        return []
    levels = matrix.get("levels")
    if not isinstance(levels, dict):
        return []

    existing = _pending_annotations(db, user_id, sym)
    proposed: list[dict] = []
    for key, label, color, note in _SCENARIO_LEVELS:
        price = _as_price(levels.get(key))
        if price is None:
            continue
        payload = {
            "symbol": sym,
            "market": str(market or "CN"),
            "label": label,
            "price": price,
            "color": color,
            "note": note,
            "source": "scenario_matrix",
        }
        hit = next((row for row, p in existing if str(p.get("label") or "").strip() == label), None)
        if hit is None:
            proposed.append({**propose(db, user_id=user_id, kind="annotation",
                                      payload=payload, run_id=run_id), "dedup": False})
            continue
        try:
            old = json.loads(hit.payload or "{}")
        except (TypeError, ValueError):
            old = {}
        if not isinstance(old, dict):
            old = {}
        # 价格/文案/颜色任一漂移才写库——重复调用不产生写事务，方便在每次诊断里调用。
        if {k: old.get(k) for k in ("price", "note", "color")} != {k: payload[k] for k in ("price", "note", "color")}:
            hit.payload = json.dumps({**old, **payload}, ensure_ascii=False)
            db.commit()
            db.refresh(hit)
        proposed.append({**_to_dict(hit), "dedup": True})
    return proposed


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


def _apply_alert(db: Session, *, user_id: Any, payload: dict) -> str:
    """建一条价格提醒规则（2026-10-20, P2）。

    规则必须挂在一只**自选**上（表结构是 stock_id 外键），所以先确保自选里有这只票，
    没有就顺带加进去 —— 用户点「盯一下」时不会想要"先去自选添加再来一次"。
    """
    from src.db.models import PriceAlertRule, Stock

    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise ActionError("盯盘提醒需要 symbol")
    price = payload.get("price")
    if price is None:
        raise ActionError("盯盘提醒需要 price（触发价）")
    direction = str(payload.get("direction") or "above")
    if direction not in ("above", "below"):
        raise ActionError("direction 只能是 above / below")

    stock = (
        db.query(Stock)
        .filter(Stock.user_id == str(user_id), Stock.symbol == symbol)
        .first()
    )
    if stock is None:
        stock = Stock(
            user_id=str(user_id), symbol=symbol,
            name=str(payload.get("name") or symbol), market=str(payload.get("market") or "CN"),
        )
        db.add(stock)
        db.flush()

    rule = PriceAlertRule(
        user_id=str(user_id),
        stock_id=stock.id,
        name=str(payload.get("label") or f"{symbol} {'上破' if direction == 'above' else '下破'} {price}")[:60],
        enabled=True,
        # 必须匹配 price_alert_engine.eval_rule 的期望结构，否则规则入库但永远不触发
        # （engine: {"op": "and"|"or", "items": [{"type","op","value"}]}）
        condition_group={
            "op": "and",
            "items": [{
                "type": "price",
                "op": ">=" if direction == "above" else "<=",
                "value": float(price),
            }],
        },
        market_hours_mode="trading_only",
        repeat_mode="once",
    )
    db.add(rule)
    db.commit()
    return f"已建盯盘提醒：{symbol} {'上破' if direction == 'above' else '下破'} {price}"


def _to_dict(row: Any) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "kind": row.kind,
        "payload": json.loads(row.payload or "{}"),
        "status": row.status,
        "result": row.result,
        # P1-1.3: diff 给前端展示"会改什么"；snapshot 不给前端（内部回滚用）
        "diff": json.loads(row.diff_json) if getattr(row, "diff_json", None) else [],
        "can_rollback": bool(getattr(row, "before_snapshot", None)) and row.status == "applied",
        "created_at": str(row.created_at or ""),
        "applied_at": str(row.applied_at or ""),
    }


def _apply_schedule(db: Session, *, user_id: Any, payload: dict) -> str:
    """建一个会话定时任务（2026-10-21, P0）。

    与 alert 的区别：alert 盯价格，schedule 按时间点产出内容（竞价摘要/收盘小结）。
    仍然遵守 propose → 用户确认 → 才落库。
    """
    from src.core.copilot_schedule import create as _create

    kind = str(payload.get("kind") or "auction_digest")
    at = str(payload.get("at_hhmm") or "09:25")
    try:
        row = _create(
            db, user_id, kind=kind, at_hhmm=at,
            weekdays=str(payload.get("weekdays") or "mon-fri"),
            conversation_id=payload.get("conversation_id"),
            payload={"symbols": payload.get("symbols") or []},
        )
    except ValueError as e:
        raise ActionError(str(e)) from e
    label = "竞价摘要" if kind == "auction_digest" else "收盘小结"
    return f"已创建定时任务 #{row['id']}：{label} 每{row['weekdays']} {row['at_hhmm']} 推送到会话"

# ── P1-1.3 dry-run diff + 回滚（2026-10-25）───────────────────────────────
# 为什么要有 diff：propose 只写"我要加自选 600111"，用户点确认前**看不到会改什么**。
# 这不是 UI 问题，是授权问题 —— 让人确认一件描述不清的事，等于没确认。
#
# 为什么要有回滚：apply 之后发现选错了标的/价位，只能手工再改一次，
# 而且两次操作在审计里是两件互不相关的事。回滚才是"这次动作"的闭合。

# 支持 diff 的动作类型（annotation 是画图批注，没有"改前值"，不做 diff）
DIFF_KINDS = ("watchlist", "alert", "schedule")


def preview_diff(db: Session, *, user_id: Any, kind: str, payload: dict) -> list[dict]:
    """propose 阶段算字段级 diff（before/after）。**只读，不落库。**

    返回 [{field, before, after, change}]，change ∈ add / update / noop。
    缺值一律 `None`，**不写成 0**（"没有设置"和"设置为 0"是两件事）。
    """
    if kind not in DIFF_KINDS:
        return []
    if kind == "watchlist":
        return _diff_watchlist(db, user_id=user_id, payload=payload)
    if kind == "alert":
        return _diff_alert(db, user_id=user_id, payload=payload)
    return _diff_schedule(db, user_id=user_id, payload=payload)


def _diff_watchlist(db: Session, *, user_id: Any, payload: dict) -> list[dict]:
    from src.db.models import Stock

    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise ActionError("缺少 symbol")
    row = db.query(Stock).filter(Stock.user_id == str(user_id), Stock.symbol == symbol).first()
    return [{
        "field": "自选",
        "before": "已在自选" if row else None,
        "after": "加入自选",
        "change": "noop" if row else "add",
    }]


def _diff_alert(db: Session, *, user_id: Any, payload: dict) -> list[dict]:
    from src.db.models import PriceAlertRule, Stock

    symbol = str(payload.get("symbol") or "").strip()
    price = payload.get("price")
    direction = str(payload.get("direction") or "above")
    if not symbol or price is None:
        raise ActionError("盯盘提醒需要 symbol 与 price")
    op = ">=" if direction == "above" else "<="
    stock = db.query(Stock).filter(Stock.user_id == str(user_id), Stock.symbol == symbol).first()
    existing = None
    if stock is not None:
        existing = (
            db.query(PriceAlertRule)
            .filter(PriceAlertRule.user_id == str(user_id), PriceAlertRule.stock_id == stock.id)
            .first()
        )
    before_rule = None
    if existing is not None:
        items = (existing.condition_group or {}).get("items") or []
        before_rule = f"{items[0].get('op')} {items[0].get('value')}" if items else None
    after_rule = f"{op} {price}"
    return [{
        "field": f"{symbol} 触发条件",
        "before": before_rule,
        "after": after_rule,
        "change": "noop" if before_rule == after_rule else ("update" if before_rule else "add"),
    }]


def _diff_schedule(db: Session, *, user_id: Any, payload: dict) -> list[dict]:
    from src.core.copilot_schedule import list_for_user

    kind = str(payload.get("kind") or "auction_digest")
    at = str(payload.get("at_hhmm") or "09:25")
    try:
        rows = list_for_user(db, user_id=user_id) or []
    except Exception:  # noqa: BLE001
        rows = []
    existing = next((r for r in rows if str(r.get("kind")) == kind), None)
    before = (existing or {}).get("at_hhmm")
    return [{
        "field": f"{kind} 时间",
        "before": before,
        "after": at,
        "change": "noop" if before == at else ("update" if before else "add"),
    }]


def _snapshot(db: Session, *, user_id: Any, kind: str, payload: dict) -> dict:
    """apply 前拍快照（回滚要用）。只存回滚必需的键，不存整行。"""
    from src.db.models import PriceAlertRule, Stock

    if kind == "watchlist":
        symbol = str(payload.get("symbol") or "").strip()
        row = db.query(Stock).filter(Stock.user_id == str(user_id), Stock.symbol == symbol).first()
        return {"stock_id": row.id if row else None, "existed": row is not None}
    if kind == "alert":
        symbol = str(payload.get("symbol") or "").strip()
        stock = db.query(Stock).filter(Stock.user_id == str(user_id), Stock.symbol == symbol).first()
        rule = None
        if stock is not None:
            rule = (
                db.query(PriceAlertRule)
                .filter(PriceAlertRule.user_id == str(user_id), PriceAlertRule.stock_id == stock.id)
                .order_by(PriceAlertRule.id.desc())
                .first()
            )
        return {
            "stock_existed": stock is not None,
            "stock_id": stock.id if stock else None,
            "rule_id": rule.id if rule else None,
            "rule_condition": (rule.condition_group if rule else None),
        }
    if kind == "schedule":
        from src.core.copilot_schedule import list_for_user

        kind_s = str(payload.get("kind") or "auction_digest")
        try:
            rows = list_for_user(db, user_id=user_id) or []
        except Exception:  # noqa: BLE001
            rows = []
        existing = next((r for r in rows if str(r.get("kind")) == kind_s), None)
        return {"schedule_id": (existing or {}).get("id"), "at_hhmm": (existing or {}).get("at_hhmm")}
    return {}


def rollback_action(db: Session, *, user_id: Any, action_id: int, reason: str = "") -> dict:
    """把一次已 apply 的动作退回到 apply 之前的状态，并写审计。

    只支持**能安全撤销**的三类：加自选 / 建提醒 / 建定时任务。
    撤销只删"这次动作新建的东西"或写回快照值，**不碰用户其它数据**。
    """
    from src.db.models import CopilotAction, PriceAlertRule, Stock

    row = (
        db.query(CopilotAction)
        .filter(CopilotAction.id == action_id, CopilotAction.user_id == str(user_id))
        .one_or_none()
    )
    if row is None:
        raise ActionError("动作不存在")
    if row.status != "applied":
        raise ActionError(f"只有 applied 的动作能回滚，当前是 {row.status}")
    snapshot = json.loads(row.before_snapshot or "{}")
    if not snapshot:
        raise ActionError("这条动作没有快照，无法回滚（早于 v0.73 的记录）")

    payload = json.loads(row.payload or "{}")
    undone: list[str] = []
    try:
        if row.kind == "watchlist":
            if not snapshot.get("existed"):
                sym = str(payload.get("symbol") or "")
                obj = db.query(Stock).filter(Stock.user_id == str(user_id), Stock.symbol == sym).first()
                if obj is not None:
                    db.delete(obj)
                    undone.append(f"已移出自选：{sym}")
        elif row.kind == "alert":
            rid = snapshot.get("rule_id")
            if rid is None:
                # 这次新建了规则 → 删掉它
                created = (
                    db.query(PriceAlertRule)
                    .filter(PriceAlertRule.user_id == str(user_id))
                    .order_by(PriceAlertRule.id.desc())
                    .first()
                )
                if created is not None:
                    db.delete(created)
                    undone.append("已删除本次新建的盯盘提醒")
            elif snapshot.get("rule_condition"):
                target = db.query(PriceAlertRule).filter(PriceAlertRule.id == rid).first()
                if target is not None:
                    target.condition_group = snapshot["rule_condition"]
                    undone.append("已恢复提醒触发条件")
        elif row.kind == "schedule":
            from src.core.copilot_schedule import delete as _delete

            sid = snapshot.get("schedule_id")
            if sid is None:
                _delete(db, user_id=user_id, schedule_id=(payload.get("id") or 0))
                undone.append("已删除本次新建的定时任务")
            else:
                undone.append("定时任务早于本次动作，未改动")
        else:
            raise ActionError(f"{row.kind} 动作不支持回滚")

        row.status = "rolled_back"
        row.result = "；".join(undone) or "无变化"
        db.commit()
    except Exception as exc:  # noqa: BLE001
        from src.core.db_safe import safe_rollback
        safe_rollback(db)
        raise ActionError(f"回滚失败：{type(exc).__name__}: {exc}") from exc

    _audit_rollback(db, user_id=user_id, action_id=action_id, kind=row.kind,
                    undone=undone, reason=reason)
    db.refresh(row)
    return _to_dict(row)


def _audit_rollback(db: Session, *, user_id: Any, action_id: int, kind: str,
                    undone: list[str], reason: str) -> None:
    """回滚本身也要留痕 —— 否则审计里只看到 apply，看不到撤销。"""
    try:
        from src.db.models import AuditLog

        db.add(AuditLog(
            user_id=str(user_id),
            action="copilot_action_rollback",
            detail=json.dumps({
                "action_id": action_id, "kind": kind,
                "undone": undone, "reason": (reason or "")[:200],
            }, ensure_ascii=False),
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        from src.core.db_safe import safe_rollback
        safe_rollback(db)
        logger.warning("动作回滚审计写入失败(不阻断): %s", exc)

