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
    if kind == "annotation":
        # 复审阻断 ②（2026-10-28）：标的类型不可证（000xxx 整段 / 纯指数前缀）的标注
        # 在确认端**必然**被拒绝（`_apply_annotation`）—— 在这里就拦住，不制造
        # 「点了确认也画不上去」的 pending。参数不全仍不拦（既有契约：由 apply 校验）。
        _guard_annotation_target(payload.get("symbol"))
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
        # 弱引用唯一索引兜底：并发确认 / 写入方绕过守卫 / 手工改过的库，
        # 都要给一句能懂的 400，而不是把 psycopg2/sqlite 的约束原文当 500 抛给前端。
        # 2026-10-28 复审驳回 ③：旧判断只查索引名 —— SQLite 的报错里没有索引名，必然漏判。
        safe_rollback(db)
        if _is_annotation_link_violation(exc):
            raise ActionError(_link_conflict_message(db)) from exc
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

    第四条（2026-10-28 复审阻断 ②）：**标的类型不可证就不提议**。000xxx 整段与纯指数
    前缀的标注在确认端必然被拒（表里没有 instrument 列），提议出来就是一条点了确认
    也画不上去的 pending。返回 `[]`（与"矩阵不可用"同语义）。

    返回提议出的动作 dict（含 `dedup` 标记复用/新建）。
    """
    sym = str(symbol or "").strip()
    if not sym:
        return []
    # 复审阻断 ②：000xxx 整段 / 纯指数前缀的标注**永远无法确认**（确认端必拒），
    # 所以这里一条 pending 都不提议 —— 连情景矩阵都不必算。
    # 返回 [] 的语义是"这次没有可提议的关键位"，与"矩阵不可用"一致，调用方无需特殊处理。
    if instrument_ambiguous(sym):
        logger.info("关键位不提议 %s：标的类型不可证（000xxx/指数前缀），确认端必拒", sym)
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

#: A 股族：`CN` = "A 股、未指明交易所"，`SH`/`SZ`/`BJ` = 具体交易所。
#: 族内**不是**随便等价：只有同一只股票才能互证，proof 见 `provable_markets`。
A_SHARE_MARKETS: tuple[str, ...] = ("CN", "SH", "SZ", "BJ")

#: A 股**个股**代码前缀 → 交易所。只收能唯一确定交易所的个股前缀；
#: 指数前缀（399/880/881/999）与 000xxx（指数/个股同号段）一律不推导，
#: 见 `instrument_ambiguous`。
EXCHANGE_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("60", "SH"), ("68", "SH"), ("90", "SH"),
    ("00", "SZ"), ("20", "SZ"), ("30", "SZ"),
    ("43", "BJ"), ("83", "BJ"), ("87", "BJ"),
)

#: **纯**指数代码前缀：深证系列(399)、通达信板块(880/881)、上证综指(999999)。
INDEX_ONLY_PREFIXES: tuple[str, ...] = ("399", "880", "881", "999")

#: 指数/个股**同号**代码段：上证/中证指数系列与深市主板股票都占 000xxx
#: （000001=上证指数/平安银行、000002=A股指数/万科A、000003=B股指数…）。
#:
#: 为什么是"整段"而不是一张撞号名单：名单只能列出已知的那几个，000002/000003 这类
#: 名单外的同号代码就漏过去了 —— 而 000xxx 这一段**本来都**可能是上证系列指数。
#: 服务端标注行没有 instrument 列，也没有任何能证明"这一行说的是指数还是个股"的字段，
#: 所以这里不做猜测（不从编码反推标的类型，那只是伪证）：整段默认不可证、默认拒画。
AMBIGUOUS_SYMBOL_PREFIXES: tuple[str, ...] = ("000",)

#: 命中即"代码段本身就分不清指数/个股"。允许带交易所后缀（000002.SZ 仍然不可证：
#: 标注行只存代码字符串，没有列能证明它当初指的是哪一类标的）。
_AMBIGUOUS_RE = re.compile(
    r"^(?:%s)\d{3}(?!\d)" % "|".join(AMBIGUOUS_SYMBOL_PREFIXES + INDEX_ONLY_PREFIXES)
)


def instrument_ambiguous(symbol: str) -> bool:
    """该代码**在标注行里**无法证明标的类型（指数还是个股）。

    唯一能证明类型的是标的本身（页面的 instrument / 库里的 instrument 列），
    标注行两者都没有 —— 于是 000xxx 整段和纯指数前缀一律按"不可证"处理：

      - 不推导交易所（`exchange_of` 返回 None）；
      - 不与任何市场互证（`provable_markets` 只认自己）；
      - 画图/严格读取端（`market_strict`/`mine_only`/`action_linked`/`ids`）直接不给行；
        **通用读取（不带严格参数的历史回看、报表、诊断）保持既有行为照旧返回**；
      - 写入端直接拒绝、前端不发画图请求不上图（前后端同一张表）。

    这是**保守拒绝**，不是"猜测类型"：没有证据时不画，而不是"大概是股票吧"。
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    return bool(_AMBIGUOUS_RE.match(sym))


def exchange_of(symbol: str) -> str | None:
    """从 A 股代码**证明**所属交易所；证明不了（指数/个股同号、未知前缀）返回 None。

    这里刻意"能不推就不推"：猜错一个交易所就会把线画到另一只标的身上。
    """
    sym = str(symbol or "").strip().upper()
    if len(sym) != 6 or not sym.isdigit():
        return None
    if instrument_ambiguous(sym):
        return None
    for prefix, exchange in EXCHANGE_BY_PREFIX:
        if sym.startswith(prefix):
            return exchange
    return None


def provable_markets(symbol: str, market: str) -> list[str]:
    """读取端要查的市场集合：`market_strict` 也按**可证明的同一标的**展开，不是字符串相等。

    为什么不能要求字符串相等（2026-10-28 安全复审驳回 ①）：写在提议里的是交易所
    （`SH`/`SZ`/`BJ`），而页面只有 `?market=CN`（A 股整体）。落库 `SH` 后前端按 `CN`
    严格读取永远为空 —— 「确认成功但图上没有线」。反过来直接做族归并（任何 A 股代码
    互相匹配）又会把 000001 的指数线画到 000001 个股图上。

    所以规则是"能证才等"（与前端 `provableMarkets` 逐条一致）：

      - 非 A 股族（HK/US/…）：只认自己；
      - `CN`：可与"代码推导出的那一个交易所"互证；
      - `SH`/`SZ`/`BJ`：只与自身互证；代码推导出的交易所正好是它时，`CN` 行也算同一只；
      - 推导不出交易所（000xxx 整段、纯指数前缀、非个股前缀）：**不做任何互证**。
    """
    mk = str(market or "").strip().upper() or "CN"
    if mk not in A_SHARE_MARKETS:
        return [mk]
    exchange = exchange_of(symbol)
    if mk == "CN":
        return ["CN", exchange] if exchange and exchange != "CN" else ["CN"]
    if exchange == mk:
        return [mk, "CN"]
    return [mk]


def market_matches_symbol(symbol: str, market: str) -> bool | None:
    """写入端一致性：`True` 一致 / `False` 可证明矛盾 / `None` 无法证明（不拦）。"""
    exchange = exchange_of(symbol)
    if exchange is None:
        return None
    mk = str(market or "").strip().upper() or "CN"
    if mk == "CN":
        return True
    if mk not in A_SHARE_MARKETS:
        return None  # HK/US 行不参与 A 股交易所推导
    return mk == exchange


def _ambiguous_annotation_message(symbol: str) -> str:
    """标的类型不可证时的**唯一**文案（提议端与确认端共用，不许两边各写一套）。"""
    return (
        f"标注目标 {symbol} 无法证明是指数还是个股：000xxx 整段与上证/中证指数系列同号，"
        "标注行不记录标的类型，前端默认不上图。本次未落库 —— "
        "请改用不含指数同号段的代码（600xxx/002xxx/300xxx/830xxx 等）。"
    )


def _guard_annotation_target(raw_symbol: Any) -> None:
    """标注目标必须**能证明标的类型**，证明不了就在写库前拒绝。

    复审阻断 ②（2026-10-28）：确认端（`_apply_annotation`）早就拒绝 000xxx 整段，
    但提议端照旧落 pending —— 用户在「待确认动作」里看到一条点了确认必然 400 的动作，
    既是死数据也是误导。所以两端走同一把闸：

      - `propose()` / `propose_scenario_annotations()`：提议阶段直接拒绝（不产生 pending）；
      - `_apply_annotation()`：确认阶段兜底（防历史 pending / 绕过工具直接写库）。

    空/缺失 symbol **不**在这里拦：提议阶段允许参数不全（既有契约，由 apply 校验）。
    """
    symbol = str(raw_symbol or "").strip()
    if symbol and instrument_ambiguous(symbol):
        raise ActionError(_ambiguous_annotation_message(symbol))


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


#: 「一动作一标注」唯一索引名（v231）。冲突诊断按名字 + 方言原文双判。
ANNOTATION_LINK_INDEX = "uq_copilot_actions_annotation"

#: 唯一索引冲突的两种来源：并发确认（同一个 id 被两次写入）与历史悬空链接（id 复用）。
_CONFLICT_RETRY_HINT = "并发确认，请重试"


def _is_annotation_link_violation(exc: IntegrityError) -> bool:
    """这次 IntegrityError 是不是「一动作一标注」唯一索引撞的。

    两种方言的报错形状完全不同，旧实现只 `in str(exc)` 查索引名 ——
      - PostgreSQL：`duplicate key value violates unique constraint "uq_copilot_actions_annotation"`
        含索引名，旧判断侥幸命中；
      - SQLite：`UNIQUE constraint failed: copilot_actions.annotation_id` **不含索引名**，
        旧判断必然漏判 → 落进 `raise` 变成 500，前端拿到数据库原文。
    """
    parts = [str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(str(orig))
        # psycopg2 把约束名放在 diag 里；SQLite 没有 diag。
        diag = getattr(orig, "diag", None)
        if getattr(diag, "constraint_name", None) == ANNOTATION_LINK_INDEX:
            return True
        if getattr(diag, "column_name", None) == "annotation_id":
            return True
    text = " ".join(parts).lower()
    if ANNOTATION_LINK_INDEX in text:
        return True
    folded = text.replace(" ", "").replace("\n", "")
    return "uniqueconstraintfailed:copilot_actions.annotation_id" in folded


def _dangling_annotation_links(db: Session, *, limit: int = 5) -> tuple[int, list[int]]:
    """**只读**：全库有多少条动作关联指向已不存在的标注（可证明悬空），以及前几条。

    唯一索引是全局的，所以判断"撞的是谁"必须看全库；但这里只看不写 —— 别人的历史
    关联是他人的审计记录，服务端不跨用户改写（2026-10-28 安全复审驳回 ④）。
    """
    from src.db.models import CopilotAction, KlineAnnotation

    alive = db.query(KlineAnnotation.id).filter(KlineAnnotation.id == CopilotAction.annotation_id).exists()
    query = db.query(CopilotAction.id).filter(CopilotAction.annotation_id.isnot(None), ~alive)
    total = int(query.count() or 0)
    ids = [int(i) for (i,) in query.order_by(CopilotAction.id.asc()).limit(limit).all()]
    return total, ids


def _link_conflict_message(db: Session) -> str:
    """唯一索引冲突时给用户/运维的**可执行**说明（不是数据库原文，也不是"重试"了事）。"""
    total, ids = _dangling_annotation_links(db)
    if total:
        sample = "、".join(f"#{i}" for i in ids)
        return (
            f"这条标注的编号与历史动作关联冲突（{ANNOTATION_LINK_INDEX}）：库里有 {total} 条动作"
            f"的标注关联指向已删除的标注（如动作 {sample}），本次未落库、也未改动任何历史记录。"
            "服务端不跨账号改写过关联，请由对应账号/运维清理后再试。"
        )
    return f"这条标注刚被另一个确认占用（{_CONFLICT_RETRY_HINT}），本次未落库。"


def _guard_annotation_id(db: Session, *, user_id: Any) -> str | None:
    """插入标注前处理「悬空链接占住下一条主键」的情形；返回拒绝原因（None = 可以插入）。

    背景：`annotation_id` 是无外键的弱引用，标注行被删/被清理任务带走后链接就悬空了。
    SQLite 的 rowid 会复用（新行拿 max(id)+1，正是刚被删掉的那个号）⇒ 悬空链接撞
    uq_copilot_actions_annotation，**这一次以及此后每一次**确认都写不进去。

    安全边界（2026-10-28 复审驳回 ④：旧实现是全局跨用户 UPDATE，不可接受）：
      - **PostgreSQL（生产）**：序列不复用，新 id 不可能撞上历史链接 ⇒ 这里既不读也不写，
        交给唯一索引兜底；真撞了说明数据被手工改过，默认**拒绝并报告**，不猜测性修复；
      - 只在会复用主键的方言上做预检，且清理范围**仅本人**、仅可证明悬空的行；
      - 占号的是别人 ⇒ 一行都不改，返回报告文案（默认拒绝）。
    """
    from src.db.models import CopilotAction, KlineAnnotation

    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        return None  # 生产语义：不可证明有歧义 ⇒ 不动任何历史

    from sqlalchemy import func

    next_id = int(db.query(func.max(KlineAnnotation.id)).scalar() or 0) + 1
    if db.query(KlineAnnotation.id).filter(KlineAnnotation.id == next_id).first() is not None:
        return None  # 号还活着：下一条自动 id 会顺延，撞不上
    holders = (
        db.query(CopilotAction)
        .filter(CopilotAction.annotation_id == next_id)
        .order_by(CopilotAction.id.asc())
        .all()
    )
    if not holders:
        return None
    foreign = [h for h in holders if str(h.user_id) != str(user_id)]
    if foreign:
        sample = "、".join(f"#{h.id}" for h in foreign[:5])
        return (
            f"标注编号 #{next_id} 已被历史动作的关联占住（动作 {sample}，不属于本账号，"
            "其标注已被删除）。服务端不跨账号改写历史关联，本次未落库。"
        )
    # 全是本人可证明悬空的链接：只清自己这几行的关联列（状态/结果一律不动）
    cleared = (
        db.query(CopilotAction)
        .filter(
            CopilotAction.user_id == str(user_id),
            CopilotAction.id.in_([h.id for h in holders]),
            CopilotAction.annotation_id == next_id,
        )
        .update({"annotation_id": None}, synchronize_session=False)
        or 0
    )
    if cleared:
        logger.info("清理本人悬空标注关联 %s 条（编号 #%s，仅本账号）", cleared, next_id)
    return None


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
    # 2026-10-28 二轮复审：指数/个股同号（000xxx 整段）的标注行**无法证明标的类型**
    # （表里没有 instrument 列），读取端与前端都默认拒画 ⇒ 这里也默认拒绝落库。
    # 不这样做就是「确认成功、图上永远没有线」的死数据 —— 和 ts-only 同一条原则。
    # 同一把闸也拦在提议端（`_guard_annotation_target`），历史 pending 在这里兜底。
    _guard_annotation_target(symbol)
    period = _normalize_period(payload.get("period"))
    market = _normalize_market(payload.get("market"))
    if market_matches_symbol(symbol, market) is False:
        raise ActionError(
            f"标注市场与代码矛盾：{symbol} 属于 {exchange_of(symbol)}，收到 {market}；"
            "这条线会落到查不到的图上，已拒绝"
        )
    ts = _normalize_ts(payload.get("ts"))
    price = payload.get("price")
    if price is not None:
        price = _as_price(price)
        if price is None:
            raise ActionError("标注价位不合法（必须是正的有限数）")
    # 2026-10-28 复审驳回 ②：ts-only 确认后落库了，但图上**没有时间图层**可画 ——
    # 唯一图层是水平价位线，读取端只能拒画。宁可确认时明确拒绝，也不制造
    # "确认成功、图上永远没有"的死数据。
    if price is None and ts is not None:
        raise ActionError(
            "时间点标注（ts）暂不支持上图：图表目前只有水平价位线这一种图层，"
            "只给 ts 会落成画不出来的死数据。请改用价位（price）标注水平线。"
        )
    # 水平线要 price、时间点要 ts；两个都没有 = 这条线在图上没有位置。
    if price is None and ts is None:
        raise ActionError("标注需要价位（price）或时间点（ts）—— 两者都缺，图上没有位置可放")

    # 弱引用兜底：先看"下一条主键是否被悬空链接占着"（只在复用主键的方言上，且仅本人）。
    blocked = _guard_annotation_id(db, user_id=user_id)
    if blocked:
        raise ActionError(blocked)

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
        # 说清楚"这一类动作本来就不能撤回"，而不是拿"没有快照/早于 v0.73"糊弄用户。
        # 2026-10-28 复审阻断 ④：旧文案让用户"在图上删除那条标注" —— 图上**没有**删除入口，
        # 服务端也没有撤销/删除图注的接口（`undoSupported=false`）。指一条不存在的路
        # 比直接说"撤不回去"更糟，所以这里只陈述事实，不给不存在的操作指引。
        raise ActionError(
            f"{_KIND_NAMES.get(row.kind, row.kind)}动作不支持回滚"
            "（标注确认后线已经在图上；服务端没有撤销/删除图注的接口，前端也没有删除入口，"
            "这条线目前无法撤下）"
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

