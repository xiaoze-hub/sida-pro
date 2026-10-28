"""动作回写(2026-10-20, P1)。

「原生融合」的硬指标不是 AI 能读页面, 而是**能改页面**——但绝不能直接改:

    AI 提议(propose) → 落 pending → 用户点确认(apply) → 才真正写库

红线: 这套框架只管「加自选 / 画标注 / 建提醒」这类**可以撤销的低风险动作**。
改持仓、下单、撤单永远不在这里(工具层硬 403)。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ALLOWED_KINDS = ("annotation", "watchlist", "alert", "schedule")

#: 真能撤回的动作类型（有 before_snapshot 语义）。
#: annotation 确认后线已经上图，服务端没有撤销入口 —— 不许假装能回滚
#: （2026-10-28 复审: 旧 `can_rollback = bool(before_snapshot)` 把 "{}" 也当快照,
#: 前端看到 true, 点下去却得到"没有快照，无法回滚（早于 v0.73）"这种错文案）。
ROLLBACKABLE_KINDS: tuple[str, ...] = ("watchlist", "alert", "schedule")

_KIND_NAMES: dict[str, str] = {
    "annotation": "标注", "watchlist": "加自选", "alert": "盯盘提醒", "schedule": "定时任务",
}


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
    # ⚠️ 2026-10-25 CI 拦下：这里**不能**把 ActionError 抛出去。
    # 既有约定是"提议阶段允许参数不全、apply 阶段才校验"（test_agent_p1 /
    # test_agent_regressions 都钉着这条）。diff 只是给用户看的预演，算不出来就留空。
    try:
        diff = preview_diff(db, user_id=user_id, kind=kind, payload=payload)
    except Exception as exc:  # noqa: BLE001
        logger.info("propose 计算 diff 失败(不阻断, 参数不全由 apply 校验): %s", exc)
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
    """确认并执行一次动作，返回**可追溯**的结果（2026-10-28, P2）。

    契约要点（标注类动作）：
      - `annotation_id`：本次真正落库的 `kline_annotations.id`；历史行没有就是 None，
        不猜、不回填；
      - `annotation`：落库明细（id/symbol/market/period/price/ts/label/color/source），
        前端拿它对账「图上那条线」；
      - 归属只看身份 / 动作行的 user_id，payload 改不了；
      - 一条动作只能被确认一次：状态推进用**条件 UPDATE 原子认领**（并发/连点都只有一个赢）。
    """
    from src.core.db_safe import safe_rollback
    from src.db.models import CopilotAction

    row = (
        db.query(CopilotAction)
        .filter(CopilotAction.id == action_id, CopilotAction.user_id == str(user_id))
        .one_or_none()
    )
    if row is None:
        # 越权与不存在给同一句：不泄露"这条动作存在但不属于你"
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

    # 原子认领: 只有仍在 pending 的那一次能把状态推进到 applied。
    # 连点/并发确认的第二个请求 rowcount=0 → 明确拒绝，而不是画出第二条线。
    claimed = (
        db.query(CopilotAction)
        .filter(
            CopilotAction.id == action_id,
            CopilotAction.user_id == str(user_id),
            CopilotAction.status == "pending",
        )
        .update({"status": "applied", "applied_at": datetime.utcnow()},
                synchronize_session=False)
    )
    if not claimed:
        safe_rollback(db)
        raise ActionError("该动作已被处理，请刷新待确认列表")
    db.refresh(row)

    extra: dict = {}
    try:
        result = handler(db, user_id=user_id, payload=payload)
        if isinstance(result, tuple):  # 标注类动作回传 (文案, {annotation_id, annotation})
            result, extra = result
        row.status = "applied"
        row.result = result
        row.annotation_id = extra.get("annotation_id")
        if row.applied_at is None:  # 认领时已写；老库/方言差异兜底
            row.applied_at = datetime.utcnow()
        db.commit()
    except ActionError:
        # 业务拒绝(参数/越权): 整个事务回滚 —— 动作必须仍是 pending, 不留半条标注
        safe_rollback(db)
        raise
    except IntegrityError as exc:
        # 弱引用唯一索引兜底: 并发确认/写入方绕过 _prune 时, 给一句能懂的 400,
        # 而不是把 psycopg2/sqlite 的约束原文当 500 抛给前端。
        safe_rollback(db)
        if "uq_copilot_actions_annotation" in str(exc):
            raise ActionError("这条标注刚被另一个确认占用（并发确认），请重试") from exc
        logger.exception("动作执行撞数据库约束, 已回滚: action=%s kind=%s", action_id, row.kind)
        raise
    except Exception:
        # 未知异常回滚后原样抛出(5xx), 不伪装成 400
        safe_rollback(db)
        logger.exception("动作执行失败, 已回滚: action=%s kind=%s", action_id, row.kind)
        raise

    db.refresh(row)
    out = _to_dict(row)
    out["annotation"] = extra.get("annotation")
    return out


def reject_action(db: Session, *, user_id: Any, action_id: int) -> dict:
    """拒绝一条**仍待确认**的动作。

    2026-10-28: 收紧为只拒绝 pending。此前拒绝已 applied 的动作会把它改成 rejected，
    而标注还在图上 —— 「动作被拒 + 图上有线」是自相矛盾的审计。
    """
    from src.core.db_safe import safe_rollback
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
    rejected = (
        db.query(CopilotAction)
        .filter(
            CopilotAction.id == action_id,
            CopilotAction.user_id == str(user_id),
            CopilotAction.status == "pending",
        )
        .update({"status": "rejected", "applied_at": datetime.utcnow()},
                synchronize_session=False)
    )
    if not rejected:
        safe_rollback(db)
        raise ActionError("该动作已被处理，请刷新待确认列表")
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


#: 图上真能画标注的周期（= 前端 KlineChart 的 KlineInterval）。
#: 写进别的字面量 = 行落库了但按 period 等值查询永远查不到（历史上 market 就这么翻过车）。
ANNOTATION_PERIODS: tuple[str, ...] = ("1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mth")

#: 图上真能画标注的市场代码 = 前端 `?market=` 与读取端等值匹配用的字面量（全大写）。
#: market 列是 VARCHAR(8) 且 PG 比较大小写敏感: 落 "cn" 就等于这条线在图上不存在。
ANNOTATION_MARKETS: tuple[str, ...] = ("CN", "SH", "SZ", "BJ", "HK", "US")

#: URL 风格的周期写法（frontend/src/lib/kline-period.ts 的 `?period=`）→ 图表周期
_PERIOD_ALIASES: dict[str, str] = {
    "m1": "1m", "1min": "1m", "1minute": "1m",
    "m5": "5m", "m15": "15m", "m30": "30m", "m60": "60m",
    "d1": "1d", "day": "1d", "daily": "1d",
    "w1": "1w", "week": "1w", "weekly": "1w",
    "mn": "1mth", "mth": "1mth", "mon": "1mth", "month": "1mth", "monthly": "1mth",
    "1mon": "1mth", "1month": "1mth",
}

#: 大写 M 才是"月线"：国内口径 `1M` = 月线，而 `1m` = 1 分钟。
#: 旧实现先 lower() 再查表 → `1M` 落成 `1m`(1分钟)，图上按 1mth 查永远查不到。
#: 只列"大写 M 与 1 分钟同形、但语义是月"的那几个写法。
_UPPERCASE_MONTH_TOKENS: frozenset[str] = frozenset({"1M", "M", "M1"})

#: 归属与关联只能由服务端决定：payload 里出现这些键 = 试图伪造他人/历史关联，直接拒绝。
_RESERVED_ANNOTATION_KEYS: tuple[str, ...] = ("annotation_id", "user_id", "action_id", "status")

#: kline_annotations 的列宽（models: symbol VARCHAR(12) / ts VARCHAR(32)）。
#: 超长在 SQLite 悄悄收下、在 PG 抛 StringDataRightTruncation(500) —— 必须先变成 400。
_MAX_SYMBOL_LEN = 12
_MAX_TS_LEN = 32

#: 时间点只接受日期/日期时间（前端按 bar 时间对它；别的写法画不上）。
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?)?$")


def _normalize_period(raw: Any) -> str:
    """把周期收敛到图表字面量；不认识就拒绝，**不落一个查不到的值**。"""
    if raw is None or str(raw).strip() == "":
        return "1d"
    token = str(raw).strip()
    if token in _UPPERCASE_MONTH_TOKENS:
        return "1mth"
    p = token.lower()
    p = _PERIOD_ALIASES.get(p, p)
    if p not in ANNOTATION_PERIODS:
        raise ActionError(
            f"不支持的标注周期：{raw}（可用 {'/'.join(ANNOTATION_PERIODS)}；"
            "月线是 1M/1mth，1m 是 1 分钟）"
        )
    return p


def _normalize_market(raw: Any) -> str:
    """market 归一成大写字面量；不认识的直接拒绝（不落一个读取端匹配不上的值）。"""
    mk = str(raw or "").strip().upper()
    if not mk:
        return "CN"
    if mk not in ANNOTATION_MARKETS:
        raise ActionError(
            f"不支持的标注市场：{raw}（可用 {'/'.join(ANNOTATION_MARKETS)}）"
        )
    return mk


def _normalize_ts(raw: Any) -> str | None:
    """时间点：空 → None；超长/非日期时间 → 明确拒绝（不留给数据库抛 500）。"""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise ActionError('标注时间必须是字符串，如 "2026-10-28" 或 "2026-10-28 10:00"')
    ts = raw.strip()
    if not ts:
        return None
    if len(ts) > _MAX_TS_LEN:
        raise ActionError(
            f"标注时间过长（最多 {_MAX_TS_LEN} 个字符，收到 {len(ts)}）：{ts[:16]}…"
        )
    if not _TS_RE.match(ts):
        raise ActionError(f'标注时间格式不合法：{ts}（用 "2026-10-28" 或 "2026-10-28 10:00"）')
    return ts


def _prune_dangling_annotation_links(db: Session) -> int:
    """把指向**已不存在**标注的弱引用链接清成 NULL，返回清理条数。

    为什么必须清: annotation_id 是弱引用(无外键)，标注行被用户删掉/被清理任务带走后
    链接就悬空了。SQLite 的 rowid 会复用 —— 下一条新标注可能拿到同一个 id，于是那条
    悬空链接撞上 uq_copilot_actions_annotation，这次确认(以及此后每一次)都写不进去
    （实测: 删掉最大 id 的标注后，每次确认都 UNIQUE constraint failed）。

    清的只是"指向不存在的行"的链接，不猜、不改真实关联；跨用户也要清，因为唯一索引
    是全局的（别人留下的悬空链接一样会撞）。
    """
    from src.db.models import CopilotAction, KlineAnnotation

    live = db.query(KlineAnnotation.id).filter(
        KlineAnnotation.id == CopilotAction.annotation_id
    ).exists()
    return int(
        db.query(CopilotAction)
        .filter(CopilotAction.annotation_id.isnot(None), ~live)
        .update({"annotation_id": None}, synchronize_session=False)
        or 0
    )


def _apply_annotation(db: Session, *, user_id: Any, payload: dict) -> tuple[str, dict]:
    """把待确认标注真正画到图上，并回传 (文案, {annotation_id, annotation})。

    这里只 flush 不 commit：提交由 `apply_action` 统一做，标注与动作状态必须同一事务，
    否则会出现「动作 applied 但标注没落库」或「标注落库但动作仍 pending」的半截状态。
    """
    from src.db.models import KlineAnnotation

    forged = [k for k in _RESERVED_ANNOTATION_KEYS if k in payload]
    if forged:
        raise ActionError(f"标注动作不接受外部提供的 {'/'.join(forged)}（归属与关联由服务端决定）")

    symbol = str(payload.get("symbol") or "").strip()
    label = str(payload.get("label") or "").strip()
    if not symbol or not label:
        raise ActionError("标注需要 symbol 和 label")
    if len(symbol) > _MAX_SYMBOL_LEN:
        raise ActionError(f"标的代码过长（最多 {_MAX_SYMBOL_LEN} 个字符）：{symbol}")
    period = _normalize_period(payload.get("period"))
    market = _normalize_market(payload.get("market"))
    ts = _normalize_ts(payload.get("ts"))
    price = payload.get("price")
    if price is not None:
        price = _as_price(price)
        if price is None:
            raise ActionError("标注价位不合法（必须是正的有限数）")
    # 水平线要 price、时间点要 ts；两个都没有 = 这条线在图上没有位置,
    # 落库只会制造"确认成功但什么也看不到"+ 读取端拒画的死数据。
    if price is None and ts is None:
        raise ActionError("标注需要价位（price）或时间点（ts）—— 两者都缺，图上没有位置可放")

    # 弱引用兜底: 先把悬空链接(指向已删标注)清掉, 否则 rowid 复用时本次写入必撞唯一索引。
    _prune_dangling_annotation_links(db)

    row = KlineAnnotation(
        user_id=str(user_id),
        symbol=symbol,
        market=market,
        period=period,
        price=price,
        ts=ts,
        label=label[:64],
        note=(payload.get("note") or "")[:500] or None,
        color=str(payload.get("color") or "up")[:16],
        source="ai",
    )
    db.add(row)
    db.flush()  # 拿到 id；真正提交在 apply_action
    detail = {
        "id": row.id, "symbol": row.symbol, "market": row.market, "period": row.period,
        "price": row.price, "ts": row.ts, "label": row.label, "color": row.color,
        "source": row.source,
    }
    return f"已标注 {symbol} @ {price if price is not None else ts}：{label}", {
        "annotation_id": row.id,
        "annotation": detail,
    }


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


def _has_rollback_snapshot(row: Any) -> bool:
    """快照里真有可回滚的内容 —— "{}" 不算（annotation 的 _snapshot 就返回空 dict）。"""
    raw = getattr(row, "before_snapshot", None)
    if not raw:
        return False
    try:
        snap = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(snap, dict) and bool(snap)


def _to_dict(row: Any) -> dict:
    ann_id = getattr(row, "annotation_id", None)
    return {
        "id": row.id,
        "run_id": row.run_id,
        "kind": row.kind,
        "payload": json.loads(row.payload or "{}"),
        "status": row.status,
        "result": row.result,
        # P2(2026-10-28): 标注类动作确认后落到的 kline_annotations.id。
        # 迁移前确认的动作没有这条关联 → None（不猜、不回填）；明细块由 apply 时填充。
        "annotation_id": int(ann_id) if ann_id is not None else None,
        "annotation": None,
        # P1-1.3: diff 给前端展示"会改什么"；snapshot 不给前端（内部回滚用）
        "diff": json.loads(row.diff_json) if getattr(row, "diff_json", None) else [],
        # 只有"这类动作真的能撤回 + 已执行 + 快照里有内容"才算 true。
        # 2026-10-28 复审: 旧写法 bool("{}") 为真 → annotation 动作骗前端说能撤回。
        "can_rollback": (
            row.kind in ROLLBACKABLE_KINDS
            and row.status == "applied"
            and _has_rollback_snapshot(row)
        ),
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
    if row.kind not in ROLLBACKABLE_KINDS:
        # 说清楚"这一类动作本来就不能撤回"，而不是拿"没有快照/早于 v0.73"糊弄用户
        # （标注确认后线已经在图上，服务端还没有撤销入口）。
        raise ActionError(
            f"{_KIND_NAMES.get(row.kind, row.kind)}动作不支持回滚"
            "（标注确认后已上图，如需撤下请在图上删除那条标注）"
        )
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
            # 防御: ROLLBACKABLE_KINDS 扩了但这里没接对应实现时, 明确说"不支持"。
            raise ActionError(f"{_KIND_NAMES.get(row.kind, row.kind)}动作不支持回滚")

        row.status = "rolled_back"
        row.result = "；".join(undone) or "无变化"
        db.commit()
    except ActionError:
        # 业务拒绝原样抛出（不要被下面的兜底包成"回滚失败：ActionError: ..."）
        from src.core.db_safe import safe_rollback
        safe_rollback(db)
        raise
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

