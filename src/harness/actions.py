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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from src.core.timezone import db_now_naive, to_iso_utc, utc_now

logger = logging.getLogger(__name__)

ALLOWED_KINDS = ("annotation", "watchlist", "alert", "schedule")

#: 真能撤销的动作类型。
#:  - `annotation`：走**标注撤销**（删掉那条 `kline_annotations` 行 + 动作转 rolled_back），
#:    不靠 before_snapshot（`_snapshot` 对它返回空 dict）；
#:  - `watchlist` / `alert` / `schedule`：有 before_snapshot 语义，靠快照回退。
#: 2026-10-29 P2 撤销闭合：annotation 从"不可回滚"改为"可由本人撤销"。
#: 旧实现（2026-10-28 复审）曾把 annotation 挡在外面，因为服务端确实没有删除入口；
#: 现在那个入口就是 `revoke_annotation`，所以 `can_rollback` 必须如实为 true。
ROLLBACKABLE_KINDS: tuple[str, ...] = ("annotation", "watchlist", "alert", "schedule")

#: 靠 apply 前快照回退的动作类型（annotation 例外，见上）。
SNAPSHOT_ROLLBACK_KINDS: tuple[str, ...] = ("watchlist", "alert", "schedule")

_KIND_NAMES: dict[str, str] = {
    "annotation": "标注", "watchlist": "加自选", "alert": "盯盘提醒", "schedule": "定时任务",
}


class ActionError(Exception):
    """动作不可执行(参数缺失/越权/未知类型)。文案直接给用户看。

    `status_code` 是给 API 层的显式状态（默认 400）：撤销类操作需要区分
    404（不存在/不属于本账号，不泄露他人数据）与 409（状态冲突/无主/并发失败），
    这样前端能按状态给出可执行提示，而不是把一切都当"参数错误"。
    """

    def __init__(self, message: str = "", *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = int(status_code or 400)


def propose(db: Session, *, user_id: Any, kind: str, payload: dict, run_id: str | None = None) -> dict:
    from src.db.models import CopilotAction

    if kind not in ALLOWED_KINDS:
        raise ActionError(f"不支持的动作类型：{kind}")
    if not payload:
        raise ActionError("动作内容为空")
    if kind == "annotation":
        # 2026-10-28 P2 类型链：标的类型是**显式字段**（`payload["instrument"]`）。
        # 提议阶段就走与确认端同一把闸（`resolve_instrument`），不制造"点了确认必然 400"
        # 的 pending；000xxx 只要声明 instrument='stock'/'index' 就合法（旧实现整段拒）。
        # 参数不全仍不拦（既有契约：由 apply 校验）。
        _guard_annotation_target(
            payload.get("symbol"), payload.get("market") or "CN", payload.get("instrument")
        )
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
    return _to_dict(row, db=db)


def list_pending(db: Session, user_id: Any, limit: int = 20) -> list[dict]:
    from src.db.models import CopilotAction

    rows = (
        db.query(CopilotAction)
        .filter(CopilotAction.user_id == str(user_id), CopilotAction.status == "pending")
        .order_by(CopilotAction.id.desc())
        .limit(limit)
        .all()
    )
    return [_to_dict(r, db=db) for r in rows]


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
        .update({"status": "applied", "applied_at": db_now_naive()},
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
        if extra.get("alert"):
            # 2026-11-07 复审 B1：**这次动作实际写下的是哪一条规则**必须跟着动作走。
            # 旧撤销靠"该标的最新一条规则"反推，用户在确认之后自己又建了一条同标的的
            # 提醒时，撤销就把用户那条删了。这里把 rule_id / stock_id / 是否新建 一起
            # 存进快照（`applied` 段），撤销只认这个 ID；没有它就不猜（fail-closed）。
            try:
                base = json.loads(row.before_snapshot or "{}") or {}
            except (TypeError, ValueError):
                base = {}
            base["applied"] = extra["alert"]
            row.before_snapshot = json.dumps(base, ensure_ascii=False)
        row.status = "applied"
        row.result = result
        row.annotation_id = extra.get("annotation_id")
        if row.applied_at is None:  # 认领时已写；老库/方言差异兜底
            row.applied_at = db_now_naive()
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
    out = _to_dict(row, db=db)
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
        .update({"status": "rejected", "applied_at": db_now_naive()},
                synchronize_session=False)
    )
    if not rejected:
        safe_rollback(db)
        raise ActionError("该动作已被处理，请刷新待确认列表")
    db.commit()
    db.refresh(row)
    return _to_dict(row, db=db)


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
    instrument: str | None = None,
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

    第四条（2026-10-28 P2 类型链）：**类型必须能定下来**，而且定下来就必须能确认。

      - `instrument` 由调用方按**当前视图**给出（页面报上来的 `?type=stock|index`）；
      - 没给时只在**仅凭代码可证**时推导（`provable_instrument`：600519 → stock、
        399001 → index）；推导不出（000xxx）就不提议 —— 不猜类型、不生成点了确认
        也画不上去的 pending；
      - 类型不合法（比如把 399001 声明成 stock）同样不提议，返回 `[]`。

    返回 `[]` 的语义是"这次没有可提议的关键位"，与"矩阵不可用"一致，调用方无需特殊处理。

    返回提议出的动作 dict（含 `dedup` 标记复用/新建）。
    """
    sym = str(symbol or "").strip()
    if not sym:
        return []
    try:
        inst = resolve_instrument(sym, market, instrument)
    except ActionError as exc:
        logger.info("关键位不提议 %s：%s", sym, exc)
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
            # 类型来自视图/代码证明（上面已校验）——落库后读取端按它精确过滤
            "instrument": inst,
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
        changed = {k: old.get(k) for k in ("price", "note", "color", "instrument")} != {
            k: payload[k] for k in ("price", "note", "color", "instrument")
        }
        if changed:
            hit.payload = json.dumps({**old, **payload}, ensure_ascii=False)
            db.commit()
            db.refresh(hit)
        proposed.append({**_to_dict(hit, db=db), "dedup": True})
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
    # 与 `POST /api/stocks` 同一把配额尺子：AI「加自选」不能比 API 多加一只。
    _enforce_quotas(db, user_id=user_id, need_alert_slot=False, need_watchlist_slot=True)
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

#: 标的类型（`kline_annotations.instrument` 与前端 `KlineChart` 的 `instrument` prop 同一口径）。
INSTRUMENT_STOCK = "stock"
INSTRUMENT_INDEX = "index"
ANNOTATION_INSTRUMENTS: tuple[str, ...] = (INSTRUMENT_STOCK, INSTRUMENT_INDEX)

#: A 股**个股**代码前缀 → 交易所。000xxx 也在内：深市主板股票确实占这一段
#: （000858=五粮液、000001=平安银行），能不能当个股用要看**标的类型**，不是看前缀。
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
#: 这一段**仅凭代码**分不清指数还是个股 —— 所以 `provable_instrument` 对整段返回 None。
#: 但"代码分不清"不等于"不能用"：类型是请求/提议里的一个**显式字段**，落库后按它隔离。
#: 旧实现把"代码分不清"当成"整段禁用"，把 000858 这种正常深市个股一起误杀了。
AMBIGUOUS_SYMBOL_PREFIXES: tuple[str, ...] = ("000",)

#: 命中即"代码段本身就分不清指数/个股"。允许带交易所后缀（000002.SZ 仍然分不清：
#: 标注行只存代码字符串）。
_AMBIGUOUS_RE = re.compile(
    r"^(?:%s)\d{3}(?!\d)" % "|".join(AMBIGUOUS_SYMBOL_PREFIXES + INDEX_ONLY_PREFIXES)
)


def _symbol_key(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def instrument_ambiguous(symbol: str) -> bool:
    """该**代码段**是否无法证明标的类型（000xxx / 纯指数前缀）。

    语义（2026-10-28 P2 类型链）：这只说明"不能从代码反推类型"，**不是**"不许用"。
    000858 是正常深市个股，只要请求/提议显式声明 `instrument='stock'` 就能落库、能上图；
    000001 声明 `index` 与声明 `stock` 会落成两条互不可见的行（读取端按 instrument 过滤）。
    """
    sym = _symbol_key(symbol)
    if not sym:
        return False
    return bool(_AMBIGUOUS_RE.match(sym))


def stock_exchange_of(symbol: str) -> str | None:
    """按**个股**代码段推导交易所（000xxx 也算个股段，返回 SZ）；不在个股段 → None。"""
    sym = _symbol_key(symbol)
    if len(sym) != 6 or not sym.isdigit():
        return None
    for prefix, exchange in EXCHANGE_BY_PREFIX:
        if sym.startswith(prefix):
            return exchange
    return None


def _is_index_code(symbol: str) -> bool:
    """是否在**服务端指数代码表**里（单一事实源 `src/core/index_codes.py`）。"""
    from src.core.index_codes import INDEX_CODES

    return _symbol_key(symbol) in INDEX_CODES


def provable_instrument(symbol: str, market: str = "CN") -> str | None:
    """**仅凭代码**能证明的标的类型；证明不了返回 None（不猜）。

      - 000xxx 整段 → None：上证/中证指数与深市主板股票同号，代码本身没有信息量；
      - 指数代码表（399001/000300/000001…）或纯指数前缀（399/880/881/999）→ `index`；
      - 个股代码段（60/68/90/00/20/30/43/83/87）→ `stock`；
      - 其它（港股/美股代码、未知段）→ None（本表只管 A 股）。

    用途只有一个：**历史行**（`instrument IS NULL`，迁移前没有类型）能不能被采纳。
    可证才采纳（600519 只可能是个股），不可证就不画（000001 可能是平安银行，也可能是
    上证指数）—— 不猜、也不回填。新写入的行一律带显式类型，不走这条推导。
    """
    sym = _symbol_key(symbol)
    if len(sym) != 6 or not sym.isdigit():
        return None
    if str(market or "CN").strip().upper() not in A_SHARE_MARKETS:
        return None
    if sym.startswith(AMBIGUOUS_SYMBOL_PREFIXES):
        return None
    if sym.startswith(INDEX_ONLY_PREFIXES) or _is_index_code(sym):
        return INSTRUMENT_INDEX
    if stock_exchange_of(sym) is not None:
        return INSTRUMENT_STOCK
    return None


def exchange_of(symbol: str, instrument: str = INSTRUMENT_STOCK) -> str | None:
    """从代码+类型**证明**所属交易所；证明不了返回 None。

    个股：按个股代码段（000xxx → SZ）。指数：不做交易所推导 —— A 股指数的市场口径
    是 `CN`（`MARKET_INDICES` 里 A 股指数全部是 CN），拿 SH/SZ 去要求它只会自相矛盾。
    """
    if str(instrument or "").strip().lower() == INSTRUMENT_INDEX:
        return None
    return stock_exchange_of(symbol)


def normalize_instrument(raw: Any) -> str | None:
    """类型字面量归一；空 → None（"没声明"）；其它取值直接拒绝（不落一个读不回来的值）。"""
    token = str(raw or "").strip().lower()
    if not token:
        return None
    if token not in ANNOTATION_INSTRUMENTS:
        raise ActionError(
            f"不支持的标的类型：{raw}（可用 {'/'.join(ANNOTATION_INSTRUMENTS)}）"
        )
    return token


def _instrument_refusal(symbol: str, market: str = "CN") -> ActionError:
    """唯一文案：代码段分不清指数/个股且这次没声明类型（提议端与确认端共用）。"""
    return ActionError(
        f"标注目标 {symbol} 属于 000xxx 指数/个股同号段，仅凭代码无法证明是个股还是指数。"
        '请显式声明 instrument：个股传 instrument="stock"，指数传 instrument="index"'
        f"（市场 {market or 'CN'}）；不声明就不落库，也不上图。"
    )


def resolve_instrument(symbol: str, market: str = "CN", claimed: Any = None) -> str:
    """写入口径：决定这一行**可以落库**的标的类型；不合法/不可证 → `ActionError`。

    规则（每条都能讲清依据，不做"大概是股票吧"的猜测）：

      - 声明了 `index`：代码必须在服务端指数表或纯指数前缀里（399001/880xxx/999999/
        指数表内的 000001…），否则拒绝 —— 不能把 000858 说成指数；A 股指数的市场口径
        是 `CN`，传 SH/SZ 也拒绝（`MARKET_INDICES` 就是这么定的）。
      - 声明了 `stock`：代码必须落在个股代码段（000xxx 可以 —— 000001 就是平安银行，
        000858 是五粮液）；给了具体交易所时还必须与代码段一致（000001+SH 拒）；
        399/880/881/999 段一律拒（那几段没有个股）。
      - 没声明：只有**仅凭代码可证**时才推导（600519 → stock、399001 → index）；
        000xxx 不可证 ⇒ 拒绝并要求声明（见 `_instrument_refusal`）。
      - 非 A 股（HK/US）：服务端没有代码表可证，必须显式声明，按声明记录；读取端仍按
        instrument 严格隔离。这不是"证明类型"，只是"记录调用方对当前视图的声明"。

    **为什么允许 000001 声明成 stock**：平安银行确实存在，而它只在上证指数图上才有
    另一重身份。承认这条声明不是伪证 —— 类型是调用方对"我正在看哪张图"的陈述，服务端
    校验的是"这条陈述在这个代码段上是否合法"，并在落库后**按类型严格隔离**，而不是
    假装能从代码证明类型。
    """
    sym = _symbol_key(symbol)
    mk = str(market or "CN").strip().upper() or "CN"
    inst = normalize_instrument(claimed)

    if mk not in A_SHARE_MARKETS:
        if inst is None:
            raise ActionError(
                f"{mk} 标的的标注需要显式声明 instrument（stock/index）——"
                "服务端没有该市场的代码表，无法从代码推导类型"
            )
        return inst

    if inst is None:
        proof = provable_instrument(sym, mk)
        if proof is None:
            raise _instrument_refusal(sym, mk)
        inst = proof

    if inst == INSTRUMENT_INDEX:
        if not (sym.startswith(INDEX_ONLY_PREFIXES) or _is_index_code(sym)):
            raise ActionError(
                f"{sym} 不在指数代码表里（{'/'.join(INDEX_ONLY_PREFIXES)} 段或服务端指数表），"
                "无法证明它是指数，不能声明为 index"
            )
        if mk != "CN":
            raise ActionError(
                f"{sym} 是指数（A 股指数不分交易所），标注市场口径必须是 CN，收到 {mk}；"
                "这条线在指数图上永远查不到，已拒绝"
            )
        return INSTRUMENT_INDEX

    exchange = stock_exchange_of(sym)
    if exchange is None:
        raise ActionError(
            f"{sym} 不在个股代码段（60/68/90/00/20/30/43/83/87），"
            "399/880/881/999 段只属于指数，不能声明为 stock"
        )
    if mk in A_SHARE_MARKETS and mk != "CN" and mk != exchange:
        raise ActionError(
            f"标注市场与代码矛盾：{sym} 属于 {exchange}，收到 {mk}；"
            "这条线会落到查不到的图上，已拒绝"
        )
    return INSTRUMENT_STOCK


def provable_markets(symbol: str, market: str, instrument: str = INSTRUMENT_STOCK) -> list[str]:
    """读取端要查的市场集合：`market_strict` 也按**可证明的同一标的**展开，不是字符串相等。

    为什么不能要求字符串相等（2026-10-28 安全复审驳回 ①）：写在提议里的是交易所
    （`SH`/`SZ`/`BJ`），而页面只有 `?market=CN`（A 股整体）。落库 `SH` 后前端按 `CN`
    严格读取永远为空 —— 「确认成功但图上没有线」。反过来直接做族归并（任何 A 股代码
    互相匹配）又会把 000001 的指数线画到 000001 个股图上。

    所以规则是"能证才等"（与前端 `provableMarkets` 逐条一致）：

      - 非 A 股族（HK/US/…）：只认自己；
      - 指数（`instrument='index'`）：只认 `CN`（A 股指数的市场口径就是 CN）；
      - 个股 `CN`：可与"代码推导出的那一个交易所"互证；
      - 个股 `SH`/`SZ`/`BJ`：只与自身互证；代码推导出的交易所正好是它时，`CN` 行也算同一只；
      - 推导不出交易所（非个股段）：**不做任何互证**。
    """
    mk = str(market or "").strip().upper() or "CN"
    if mk not in A_SHARE_MARKETS:
        return [mk]
    if str(instrument or "").strip().lower() == INSTRUMENT_INDEX:
        return ["CN"]
    exchange = exchange_of(symbol, INSTRUMENT_STOCK)
    if mk == "CN":
        return ["CN", exchange] if exchange and exchange != "CN" else ["CN"]
    if exchange == mk:
        return [mk, "CN"]
    return [mk]


def market_matches_symbol(
    symbol: str, market: str, instrument: str = INSTRUMENT_STOCK
) -> bool | None:
    """写入端一致性：`True` 一致 / `False` 可证明矛盾 / `None` 无法证明（不拦）。"""
    mk = str(market or "").strip().upper() or "CN"
    if str(instrument or "").strip().lower() == INSTRUMENT_INDEX:
        if mk not in A_SHARE_MARKETS:
            return None
        return mk == "CN"
    exchange = exchange_of(symbol, INSTRUMENT_STOCK)
    if exchange is None:
        return None
    if mk == "CN":
        return True
    if mk not in A_SHARE_MARKETS:
        return None  # HK/US 行不参与 A 股交易所推导
    return mk == exchange


def _guard_annotation_target(
    raw_symbol: Any, market: Any = "CN", claimed: Any = None
) -> str | None:
    """标注目标必须能定出一个**合法**的标的类型，定不出就在写库前拒绝（两端共用）。

    2026-10-28 P2 类型链之前：000xxx 整段直接拒（连 000858 一起误杀）。
    现在：类型是显式字段，`resolve_instrument` 校验"这条声明是否合法"，不合法才拒。

    空/缺失 symbol **不**在这里拦：提议阶段允许参数不全（既有契约，由 apply 校验）。

    返回解析出的类型（None = 没给 symbol，没法判）。
    """
    symbol = str(raw_symbol or "").strip()
    if not symbol:
        return None
    return resolve_instrument(symbol, market, claimed)


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

#: **日粒度**时间点（时间点标注上图只认它）：2026-10-28 P2 恢复 marker 图层时定下的口径。
#: 带时分的时间点要映射到分钟柱时间戳，而那依赖交易所时区（docs/TIMEZONES.md）——
#: 映射错一根就是把标记画到错的 K 线上，所以宁可不支持，也不猜。
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    period = _normalize_period(payload.get("period"))
    market = _normalize_market(payload.get("market"))
    # 2026-10-28 P2 类型链：**标的类型必须落库**（`instrument` 列）。
    # 类型来自提议（payload["instrument"]，按视图声明）或仅凭代码可证的推导；
    # 000xxx 没声明类型就没法证明是个股还是指数 ⇒ 拒绝（不猜，也不整段误杀 000858）。
    # 同一把闸也拦在提议端（`_guard_annotation_target`），历史 pending 在这里兜底。
    instrument = _guard_annotation_target(symbol, market, payload.get("instrument"))
    if market_matches_symbol(symbol, market, instrument or INSTRUMENT_STOCK) is False:
        raise ActionError(
            f"标注市场与代码矛盾：{symbol} 属于 {exchange_of(symbol, instrument or INSTRUMENT_STOCK)}，"
            f"收到 {market}；这条线会落到查不到的图上，已拒绝"
        )
    ts = _normalize_ts(payload.get("ts"))
    price = payload.get("price")
    if price is not None:
        price = _as_price(price)
        if price is None:
            raise ActionError("标注价位不合法（必须是正的有限数）")
    # 时间点标注（2026-10-28 P2 恢复）：图上确有 marker 图层（L4 事件/GS 信号同一个
    # `createSeriesMarkers` 通道），所以"ts-only 一定画不出来"的旧结论不再成立。
    # 但只能恢复**能证明落在哪根 K 线**的那一部分：
    #   - 日粒度（`YYYY-MM-DD`）→ 落在当天的 BusinessDay 上，不涉及时区换算；
    #   - 带时分 → 分钟/小时柱的时间戳依赖交易所时区口径（见 docs/TIMEZONES.md），
    #     映射错一根就是把标记画到错的 K 线上 = 伪造定位。所以**暂不支持**，明确拒绝。
    if price is None and ts is not None and not _DATE_ONLY_RE.match(ts):
        raise ActionError(
            "时间点标注只支持日粒度（YYYY-MM-DD）：带时分的 ts 无法证明落在哪一根 K 线"
            "（分钟柱时间戳依赖交易所时区口径），会画到错的 K 线上。请改写日期或改用价位标注。"
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
        instrument=instrument,
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
        "id": row.id, "symbol": row.symbol, "market": row.market,
        # 类型回显：前端拿它和当前 K 线的 instrument 逐字比对（同号指数/个股靠它隔离）
        "instrument": row.instrument, "period": row.period,
        "price": row.price, "ts": row.ts, "label": row.label, "color": row.color,
        "source": row.source,
    }
    return f"已标注 {symbol} @ {price if price is not None else ts}：{label}", {
        "annotation_id": row.id,
        "annotation": detail,
    }


def _my_watchlist_row(db: Session, *, user_id: Any, symbol: Any, market: Any) -> Any:
    """本人在**同一市场**上的自选行（不存在返回 None）。

    必须带 market：`stocks` 只保证 (user_id, symbol, market) 语义唯一，代码串在多市场
    重名（00700 港股 / 某些 A 股代码段就撞过）。只按 symbol 取会让提醒挂到别的市场的
    那条自选上 —— 用户以为在盯港股，实际盯的是另一只票。
    """
    from src.db.models import Stock

    return (
        db.query(Stock)
        .filter(
            Stock.user_id == str(user_id),
            Stock.symbol == str(symbol or "").strip(),
            Stock.market == market,
        )
        .first()
    )


def _alert_market(payload: dict) -> str:
    """提醒标的的市场口径：大写字面量，空 → CN（与 stocks 写入端一致）。"""
    return str(payload.get("market") or "CN").strip().upper() or "CN"


#: 只有这两种比较符是「一条价格线」—— 也是 `_alert_condition` 唯一会写出的东西。
_ALERT_PRICE_OPS = (">=", "<=")


def _alert_condition(direction: str, price: Any) -> dict:
    """本次动作要写入的触发条件（结构必须匹配 `price_alert_engine.eval_rule`）。"""
    return {
        "op": "and",
        "items": [{
            "type": "price",
            "op": ">=" if direction == "above" else "<=",
            "value": float(price),
        }],
    }


def _normalized_condition(condition: Any) -> Any:
    """条件比的归一化：只用于**相等判定**，不用于写库。

    已落库的 JSON 可能被 SQLite/PG 读成略有差异的形态（int → float），拿字面量比会
    把"没变"判成"变了"。这里统一数值类型后再比，避免误判成"用户改过"。
    """
    if not isinstance(condition, dict):
        return condition
    items = condition.get("items")
    if not isinstance(items, list):
        return condition
    out = []
    for item in items:
        if isinstance(item, dict):
            v = item.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                item = {**item, "value": float(v)}
        out.append(item)
    return {**condition, "items": out}


def _alert_rule_expired(rule: Any) -> bool:
    """规则是否已过期（与引擎 `_can_trigger` 的 `expire_at` 判定同口径）。"""
    exp = getattr(rule, "expire_at", None)
    if not exp:
        return False
    try:
        from src.core.timezone import to_utc

        return to_utc(datetime.now(timezone.utc)) > to_utc(exp)
    except Exception:  # noqa: BLE001
        # 比不出来就按"已过期"处理：宁可多建一条能被看见的提醒，也不猜。
        return True


def _alert_rule_will_never_fire(rule: Any) -> bool:
    """这条规则即使改价格也不会再触发 —— 过期，或 once 已触发过。

    与 `price_alert_engine._can_trigger` 的两个提前返回一一对应：
    `expired` / `once_triggered`。
    """
    if _alert_rule_expired(rule):
        return True
    return str(getattr(rule, "repeat_mode", "") or "") == "once" and bool(
        getattr(rule, "last_trigger_at", None)
    )


def _is_updatable_alert_rule(rule: Any) -> bool:
    """这条既有规则能否被「就地改写」为一条价格线 —— 不能就新增，绝不动它。

    **为什么要有这个判断**（2026-11-07 复审 B3）：把"同标的已有规则就更新"当默认行为，
    等于用一条 `>= x` 静默覆盖用户的复合条件（`or` 组 / 多条件组合），还会把用户**主动
    停用**的规则重新启用。diff 只能显示一条价格，覆盖却发生了几件事 —— 用户确认的是
    A，执行的是 B。宁可多建一条（多占一个提醒名额，disclose 在 diff 里），也不动用户
    配置过的规则。

    可更新 = 启用中 + **没有过期时间** + 单条 price 条件 + `and`/无组操作 + 比较符在
    `>=`/`<=` + 数值阈值 + **还能真正触发**（2026-11-07 二次复审）：

    `price_alert_engine._can_trigger` 对 `expire_at` 已过的规则返回 `expired`，对
    `repeat_mode=="once"` 且 `last_trigger_at` 非空的规则返回 `once_triggered` ——
    这两种规则改完价格也永远不会再触发。把它们判成"可更新"，等于让用户确认了一条
    永不响的提醒（比多建一条更坏：用户以为设好了）。所以一律新增独立提醒。

    三次复审把门槛再收紧一格：`expire_at` 在**未来**的规则同样不更新 —— 它今天会响，
    但到点后静默失效，而 diff 只谈价格。规则上的过期时间是用户设的，本次动作不改它、
    也不该让它成为一条没披露的失效承诺。
    """
    if rule is None or not bool(getattr(rule, "enabled", False)):
        return False
    # 2026-11-07 三次复审: **任何**带 `expire_at` 的规则都不就地改写 —— 不只是已过期。
    # apply 只改条件，`expire_at` 会原样留下来：一条"1 小时后到期"的提醒被就地更新后，
    # 用户以为设好了，一小时后它自己失效，而 diff 里一个字都没提。过期时间属于用户的
    # 配置，改写别人的规则时既不能清也不能背着一句没说的失效承诺 ⇒ 新增独立提醒。
    if getattr(rule, "expire_at", None):
        return False
    if _alert_rule_will_never_fire(rule):
        return False
    group = getattr(rule, "condition_group", None)
    if not isinstance(group, dict):
        return False
    if str(group.get("op") or "and").lower() != "and":
        return False
    items = group.get("items")
    if not isinstance(items, list) or len(items) != 1:
        return False
    item = items[0]
    if not isinstance(item, dict):
        return False
    if str(item.get("type") or "price") != "price":
        return False
    if str(item.get("op")) not in _ALERT_PRICE_OPS:
        return False
    value = item.get("value")
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _latest_alert_rule(db: Session, *, user_id: Any, stock_id: Any) -> Any:
    """该自选上**最新一条**规则（没有返回 None）。只用来"找候选"，不当作归属证明。"""
    from src.db.models import PriceAlertRule

    return (
        db.query(PriceAlertRule)
        .filter(PriceAlertRule.user_id == str(user_id), PriceAlertRule.stock_id == stock_id)
        .order_by(PriceAlertRule.id.desc())
        .first()
    )


def _rule_brief(rule: Any) -> str:
    """规则的给人看的一句话摘要（diff 用）。解析不了就如实说，不编。"""
    parts: list[str] = []
    if not bool(getattr(rule, "enabled", True)):
        parts.append("已停用")
    # 2026-11-07 二次复审: "为什么不覆盖它"必须说出真正的原因 —— 过期/一次性的规则
    # 即使条件长得像可更新，也永远不会再响。
    if _alert_rule_expired(rule):
        parts.append("已过期（不会触发）")
    elif getattr(rule, "expire_at", None):
        # 三次复审: 未来到期的规则也不能就地更新 —— 把"到点会自己失效"说出来，
        # 否则用户以为这条提醒会一直盯着。
        parts.append(f"带过期时间（{str(rule.expire_at)[:16]} 之后失效）")
    elif str(getattr(rule, "repeat_mode", "") or "") == "once" and getattr(rule, "last_trigger_at", None):
        parts.append("一次性提醒已触发（不会再次触发）")
    group = getattr(rule, "condition_group", None) or {}
    items = group.get("items") if isinstance(group, dict) else None
    items = items if isinstance(items, list) else []
    if len(items) == 1 and isinstance(items[0], dict) and str(items[0].get("type") or "price") == "price":
        parts.append(f"{items[0].get('op')} {items[0].get('value')}")
    elif items:
        parts.append(f"{len(items)} 个条件（{group.get('op') or 'and'}）")
    else:
        parts.append("条件无法解析")
    return "，".join(parts)


def _quota_subject(db: Session, user_id: Any) -> Any:
    """取当前账号行，用于配额判定。

    账号行查不到时按 **member** 口径判定 —— 这是最严的普通账号档（不是 pro/owner），
    既不会因为查不到就放行，也不凭空发明一个"pro 权限"。
    """
    from src.db.models import User

    row = db.query(User).filter(User.id == str(user_id)).first()
    if row is not None:
        return row

    class _MemberStub:
        id = str(user_id)
        role = "member"
        username = ""

    logger.warning("配额判定: user_id=%s 没有账号行, 按 member 口径处理", user_id)
    return _MemberStub()


def _enforce_quotas(
    db: Session, *, user_id: Any, need_alert_slot: bool, need_watchlist_slot: bool
) -> None:
    """确认执行前的配额闸门（与直接 API 路径同一把尺子）。

    `check_alert_quota` / `check_watchlist_quota` 在 HTTP 层抛 `HTTPException(403)`；
    harness 的动作语义是 `ActionError`（带 status_code），在这里翻译，避免配额拒绝
    变成 500。放在动作认领之后、写业务表之前，失败时 apply_action 会整笔回滚、
    动作回到 pending。
    """
    from fastapi import HTTPException

    from src.core.permissions import check_alert_quota, check_watchlist_quota

    user = _quota_subject(db, user_id)
    try:
        # 演示账号的上限判定(`_demo_watchlist_block`)同样先数后写 —— 锁放在所有
        # 计数之前，两条 check_* 内部也会各自取同一把锁(同事务内重复取锁无副作用)。
        from src.core.permissions import ROLE_OWNER, ROLE_PRO, lock_quota_row, normalize_role

        if normalize_role(getattr(user, "role", None)) not in (ROLE_PRO, ROLE_OWNER):
            lock_quota_row(db, user)
        if need_alert_slot:
            check_alert_quota(db, user)
        if need_watchlist_slot:
            # 演示/guest 账号在 `POST /api/stocks`、`/adopt` 上走的是 guest_strategy
            # 的 watchlist_limit（默认 1），不是会员档的 10。AI 动作必须同口径，
            # 否则参谋面板成了绕过演示上限的后门。
            from src.db.models import Stock

            blocked = _demo_watchlist_block(db, user, Stock)
            if blocked:
                raise HTTPException(403, blocked)
            # 提醒动作会顺带建自选 —— 这条副作用同样占自选名额，不能白送。
            check_watchlist_quota(db, user)
    except HTTPException as exc:
        raise ActionError(str(exc.detail), status_code=int(exc.status_code or 403)) from exc


def _demo_watchlist_block(db: Session, user: Any, stock_model: Any) -> str | None:
    """演示/guest 账号的自选上限文案（与 `POST /api/stocks` 同一口径）；不适用返回 None。"""
    if str(getattr(user, "username", "")) != "demo" and str(getattr(user, "role", "")) != "guest":
        return None
    try:
        from src.core import free_tier

        limit = int(free_tier.guest_strategy(db).get("watchlist_limit", 1))
    except Exception:  # noqa: BLE001
        limit = 1
    if limit <= 0:
        return None
    own = db.query(stock_model).filter(stock_model.user_id == str(user.id)).count()
    if own >= limit:
        return f"演示账号仅可添加 {limit} 只自选股，请先删除当前自选再确认这个动作。"
    return None


def _apply_alert(db: Session, *, user_id: Any, payload: dict) -> tuple[str, dict]:
    """建/更新一条价格提醒规则（2026-10-20, P2；2026-11-07 补配额、披露、可撤销归属）。

    规则必须挂在一只**自选**上（表结构是 stock_id 外键），所以先确保自选里有这只票，
    没有就顺带加进去 —— 用户点「盯一下」时不会想要"先去自选添加再来一次"。
    这个副作用必须**明说**（结果文案 + diff + 工具提示词三处一致），因为它同时
    消耗用户的自选名额。

    更新口径（B3 收紧）：只有当该标的最新一条规则是**启用中的单条价格条件**时才就地
    改写；复合条件 / 已停用的规则一个字都不动，改为新增一条独立提醒。这样 diff 里
    "update" 说的就是真的只改了一条价格线。

    返回值第二项是本次动作**实际写下**的事实（规则 ID / 是否新建 / 自选 ID），由
    `apply_action` 落进动作快照 —— 撤销只删这里记下的那条，绝不按"最新一条"猜。
    """
    from src.db.models import PriceAlertRule

    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise ActionError("盯盘提醒需要 symbol")
    price = payload.get("price")
    if price is None:
        raise ActionError("盯盘提醒需要 price（触发价）")
    direction = str(payload.get("direction") or "above")
    if direction not in ("above", "below"):
        raise ActionError("direction 只能是 above / below")
    market = _alert_market(payload)
    arrow = "上破" if direction == "above" else "下破"

    stock = _my_watchlist_row(db, user_id=user_id, symbol=symbol, market=market)
    latest = _latest_alert_rule(db, user_id=user_id, stock_id=stock.id) if stock is not None else None
    target = latest if _is_updatable_alert_rule(latest) else None
    # 「更新既有规则」不占新名额 —— 到上限的用户仍可改自己的条件；只有真的新增才查配额。
    _enforce_quotas(
        db, user_id=user_id,
        need_alert_slot=target is None,
        need_watchlist_slot=stock is None,
    )

    added_watchlist = False
    if stock is None:
        from src.db.models import Stock

        stock = Stock(
            user_id=str(user_id), symbol=symbol,
            name=str(payload.get("name") or symbol), market=market,
            sort_order=0,
        )
        db.add(stock)
        db.flush()
        added_watchlist = True

    # 必须匹配 price_alert_engine.eval_rule 的期望结构，否则规则入库但永远不触发
    # （engine: {"op": "and"|"or", "items": [{"type","op","value"}]}）
    condition = _alert_condition(direction, price)
    updated = target is not None
    before_state: dict | None = None
    if target is None:
        rule = PriceAlertRule(
            user_id=str(user_id),
            stock_id=stock.id,
            name=str(payload.get("label") or f"{symbol} {arrow} {price}")[:60],
            enabled=True,
            condition_group=condition,
            market_hours_mode="trading_only",
            repeat_mode="once",
        )
        db.add(rule)
    else:
        rule = target
        # 撤销要能把这三样都写回去 —— 只存 condition 的话，"顺便启用"就恢复不了。
        before_state = {
            "condition": _normalized_condition(rule.condition_group),
            "enabled": bool(rule.enabled),
            "name": rule.name,
        }
        rule.condition_group = condition
        rule.enabled = True
        if payload.get("label"):
            rule.name = str(payload["label"])[:60]
    # 只 flush，不 commit：由 apply_action 统一提交，配额/约束失败时整笔回滚。
    db.flush()

    facts: dict[str, Any] = {
        "rule_id": int(rule.id),
        "rule_created": not updated,
        "rule_condition": _normalized_condition(condition),
        "stock_id": int(stock.id),
        "stock_created": bool(added_watchlist),
    }
    if before_state is not None:
        facts["rule_before"] = before_state

    head = f"已更新盯盘提醒：{symbol} {arrow} {price}" if updated else f"已建盯盘提醒：{symbol} {arrow} {price}"
    if added_watchlist:
        return f"{head}（原来不在自选，已一并加入自选：{market}）", {"alert": facts}
    return head, {"alert": facts}


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


def _to_dict(row: Any, *, db: Session | None = None) -> dict:
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
        # 只有"这类动作真的能撤销 + 已执行 + 真的还有东西可撤"才算 true。
        # 2026-10-28 复审: 旧写法 bool("{}") 为真 → annotation 动作骗前端说能撤回。
        # 2026-10-29: annotation 现在真能撤销，但前提是关联的标注行**仍存在且属主是我**。
        # 2026-10-29 二次复审: 这个前提**必须实测**才敢说 true —— 不传 `db` 时无从证明
        # 行还在（行可能已被删、主键可能已被别人复用），所以按 false 处理（宁可少报，
        # 不许拿一个过期整数骗前端"可撤销"）。所有调用点都必须把 session 传进来。
        "can_rollback": _can_rollback(row, db=db),
        "created_at": str(row.created_at or ""),
        "applied_at": str(row.applied_at or ""),
    }


def _annotation_link_alive(db: Session, row: Any) -> bool:
    """标注类动作的关联是否仍然可撤销：行在、属主是我。"""
    from src.db.models import KlineAnnotation

    ann_id = getattr(row, "annotation_id", None)
    if ann_id is None:
        return False
    return (
        db.query(KlineAnnotation.id)
        .filter(
            KlineAnnotation.id == int(ann_id),
            KlineAnnotation.user_id == str(getattr(row, "user_id", "")),
        )
        .first()
        is not None
    )


def _alert_rollback_ref(snapshot: dict) -> tuple[Any, Any, Any, bool]:
    """从快照取「撤哪条、凭什么」的原始证据：(rule_id, stock_id, 条件, 是否老记录)。"""
    facts = _alert_facts(snapshot)
    if facts:
        return (
            facts.get("rule_id"), facts.get("stock_id"),
            facts.get("rule_condition"), False,
        )
    return (
        snapshot.get("rule_id"), snapshot.get("stock_id"),
        snapshot.get("rule_condition"), True,
    )


def _same_id(a: Any, b: Any) -> bool:
    """编号相等（库里 int、JSON 里 int/str 都可能，按数值比，不从字符串形态猜）。"""
    if a is None or b is None:
        return False
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return False


def _alert_rollback_rule(
    db: Session, *, user_id: Any, payload: dict, snapshot: dict
) -> tuple[Any, str | None]:
    """撤销目标的**唯一**判据 —— `can_rollback` 与真正撤销共用这一份。

    2026-11-07 三次复审（虚假可撤销的根因）：两个函数各写一遍判断，就会分叉 ——
    老快照缺 `stock_id` 时 `_alert_rollback_provable` 跳过归属比对报 true，而
    `_rollback_alert` 拿 `None` 去比必然 409；前端给了一个点了必失败的按钮。
    现在只有这一份：证据四件套（规则 ID、属主、所属自选、本次写入的条件）缺一不可。

    返回 `(rule, deny)`：
      * `(row, None)`  → 可以动这一行；
      * `(None, None)` → 快照里的规则已经不在了，撤销是幂等空操作（算成功）；
      * `(None, 原因)` → 证明不了 / 已被用户改动 ⇒ 一行不动（fail-closed）。
    """
    from src.db.models import PriceAlertRule

    rid, sid, expected, legacy = _alert_rollback_ref(snapshot)
    if rid is None or not expected:
        return None, (
            "这条提醒动作早于本次修复的记录格式：快照里没有本次动作写下的规则 ID/条件。"
            "按「该标的最新一条」猜删会误删你后来在同标的建的提醒，因此本次撤销"
            "不改动任何规则（fail-closed）；如需清理请到提醒列表手动删除。"
        )
    if sid is None:
        # 没记所属自选就证明不出这条规则属于本次动作：既不敢删，也不该报"可撤销"。
        return None, (
            "这条提醒动作的快照没有记录所属自选，无法证明规则归属，"
            "拒绝改动任何规则（fail-closed）；如需清理请到提醒列表手动删除。"
        )
    try:
        rule = db.query(PriceAlertRule).filter(PriceAlertRule.id == int(rid)).first()
    except (TypeError, ValueError):
        return None, f"提醒编号 {rid!r} 不是有效 ID，无法定位规则，拒绝改动（fail-closed）"
    if rule is None:
        return None, None
    if str(rule.user_id) != str(user_id) or not _same_id(rule.stock_id, sid):
        return None, (
            f"提醒 #{int(rule.id)} 的属主或所属自选与这条动作的记录不一致（编号可能已被复用），"
            "拒绝改动这条规则（fail-closed）"
        )
    if legacy:
        ok = _legacy_condition_matches(payload, rule)
    else:
        ok = _normalized_condition(rule.condition_group) == _normalized_condition(expected)
    if not ok:
        return None, (
            f"提醒 #{int(rule.id)} 的条件已不再是这条动作写入的值（你在确认之后改过它），"
            "撤销会覆盖掉你的修改，因此本次不撤、动作状态保持不变（fail-closed）；"
            "如确实要清理请到提醒列表手动处理。"
        )
    return rule, None


def _alert_rollback_provable(db: Session, row: Any) -> bool:
    """撤销这条提醒动作**真的会成功**吗 —— 直接用 `_alert_rollback_rule` 的结论。

    2026-11-07 二次复审（虚假可撤销）：此前 alert 只看"快照非空"就报 true，老记录
    （没有 `applied` 段、也没有 `rule_id`）点下去必得 409，前端却显示"可撤销"。
    三次复审：判定改为与真正撤销**同一函数**，不再各写一份会分叉的规则。
    """
    raw = getattr(row, "before_snapshot", None)
    if not raw:
        return False
    try:
        snap = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(snap, dict) or not snap:
        return False
    try:
        payload = json.loads(getattr(row, "payload", None) or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    _rule, deny = _alert_rollback_rule(
        db, user_id=getattr(row, "user_id", ""), payload=payload, snapshot=snap
    )
    return deny is None


def _can_rollback(row: Any, *, db: Session | None = None) -> bool:
    """`can_rollback` 的唯一判定口径（2026-10-29；2026-11-07 二次复审补 alert）。"""
    if getattr(row, "status", None) != "applied":
        return False
    if getattr(row, "kind", None) == "annotation":
        if getattr(row, "annotation_id", None) is None:
            return False
        # 没有 session 就证明不了"这条线还在、还是我的" ⇒ 不许说 true（见 `_to_dict`）。
        return False if db is None else _annotation_link_alive(db, row)
    if getattr(row, "kind", None) == "alert":
        # 同上：没有 session 就证明不了快照里的规则 ID 还在、还没被改动 ⇒ false。
        return False if db is None else _alert_rollback_provable(db, row)
    return (
        getattr(row, "kind", None) in SNAPSHOT_ROLLBACK_KINDS
        and _has_rollback_snapshot(row)
    )


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
    """提醒动作的字段级 diff —— 必须与 `_apply_alert` **同源判定**。

    判定共用 `_is_updatable_alert_rule`：diff 说 update，apply 才真的只改一条价格线；
    既有规则是复合条件/已停用时 diff 说 add，并额外用「现有提醒」这一条明说它保持原样。
    diff 与执行口径一旦分叉，用户确认的就是他看不到的那件事。
    """
    symbol = str(payload.get("symbol") or "").strip()
    price = payload.get("price")
    direction = str(payload.get("direction") or "above")
    if not symbol or price is None:
        raise ActionError("盯盘提醒需要 symbol 与 price")
    market = _alert_market(payload)
    op = ">=" if direction == "above" else "<="
    stock = _my_watchlist_row(db, user_id=user_id, symbol=symbol, market=market)
    latest = _latest_alert_rule(db, user_id=user_id, stock_id=stock.id) if stock is not None else None
    target = latest if _is_updatable_alert_rule(latest) else None

    before_rule = None
    if target is not None:
        items = (target.condition_group or {}).get("items") or []
        before_rule = f"{items[0].get('op')} {items[0].get('value')}" if items else None
    after_rule = f"{op} {price}"
    diffs = [{
        "field": f"{symbol} 触发条件",
        "before": before_rule,
        "after": after_rule,
        "change": "noop" if before_rule == after_rule else ("update" if before_rule else "add"),
    }]
    if latest is not None and target is None:
        # 关键披露：不会覆盖这条既有规则，只会新增一条。用户点确认前必须知道
        # "我原有的复合条件/停用/过期/一次性已触发的状态不动，但会多一条提醒（多占一个名额）"。
        # 2026-11-07 复审: 带 expire_at 的旧规则**仍然启用**（只是到点自己失效），
        # 所以到期前两条规则会同时命中同一条件 → 重复触发/重复推送。必须写进 diff，
        # 否则用户以为"新增一条"，实际得到的是"双份提醒"。
        expire_note = ""
        expire_at = getattr(latest, "expire_at", None)
        if expire_at is not None and bool(getattr(latest, "enabled", False)):
            expire_note = (
                f"它带过期时间 {expire_at} 且仍然启用，到期前两条规则会同时命中同一条件，"
                "提醒/推送会重复触发；要避免重复，请先停用或删除旧规则再确认本次新建。"
            )
        diffs.append({
            "field": "现有提醒",
            "before": _rule_brief(latest),
            "after": "保持原样（复合条件、已停用、带过期时间、已过期或一次性已触发的规则不覆盖）；"
                     "本次另建一条独立提醒" + expire_note,
            "change": "noop",
        })
    elif target is not None:
        # 2026-11-07 二次复审: apply 会按 label 改写 rule.name —— 改名同样是"会改什么"，
        # diff 漏了它就等于让用户确认了一件看不到的修改。
        label = str(payload.get("label") or "").strip()[:60]
        if label and label != str(target.name or ""):
            diffs.append({
                "field": "提醒名称",
                "before": str(target.name or "") or None,
                "after": label,
                "change": "update",
            })
    # 2026-11-07: 真实副作用必须进 diff。确认这个动作会在标的不在自选时**顺手建自选**，
    # 既占自选名额也会改变用户的自选列表 —— 用户在点确认前有权看到。
    if stock is None:
        diffs.append({
            "field": "自选",
            "before": None,
            "after": f"一并加入自选（{symbol} / {market}）",
            "change": "add",
        })
    return diffs


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
    from src.db.models import Stock

    if kind == "watchlist":
        symbol = str(payload.get("symbol") or "").strip()
        row = db.query(Stock).filter(Stock.user_id == str(user_id), Stock.symbol == symbol).first()
        return {"stock_id": row.id if row else None, "existed": row is not None}
    if kind == "alert":
        symbol = str(payload.get("symbol") or "").strip()
        market = _alert_market(payload)
        stock = _my_watchlist_row(db, user_id=user_id, symbol=symbol, market=market)
        rule = _latest_alert_rule(db, user_id=user_id, stock_id=stock.id) if stock is not None else None
        return {
            "stock_existed": stock is not None,
            "stock_id": stock.id if stock else None,
            "stock_market": market,
            "rule_id": rule.id if rule else None,
            "rule_condition": (rule.condition_group if rule else None),
            # 2026-11-07: 只有条件不够 —— apply 还会把规则改成 enabled=True 并按 label 改名，
            # 撤销要能把这两样一起写回去，否则"撤销"会留下一条被重新启用的规则。
            "rule_enabled": bool(rule.enabled) if rule else None,
            "rule_name": rule.name if rule else None,
            "rule_updatable": _is_updatable_alert_rule(rule),
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


# ── 标注撤销（2026-10-29, P2 撤销闭合）───────────────────────────────────
# 为什么要有它：确认动作把线画到图上之后，此前服务端**没有任何撤销入口**
# （`rollback_action` 对 annotation 直接 400，图上也没有删除按钮）。用户点错价位/标的
# 只能永久留着那条线 —— 「可以撤销的低风险动作」这条红线就名不副实了。
#
# 四条纪律：
#   1. **只撤本人的**：`annotation.user_id` 必须等于当前账号；他人标注按 404 处理
#      （与"不存在"同一句，不泄露他人数据是否存在）；
#   2. **无主不撤**：`user_id IS NULL` 是全局行，它不属于任何账号，服务端不替谁删；
#   3. **只撤已确认的**：必须有 `copilot_actions.annotation_id` 反查到的确认动作，
#      且动作属主一致；人工直插/AI 直插且没有确认动作的行不在本条契约里；
#   4. **状态明确**：撤销成功 = 标注行删除 + 动作 `rolled_back` + 审计留痕（含被删行快照）；
#      重复撤销/并发撤销给 404 或 409，绝不静默"成功"。

#: 撤销原因写进审计的自由文本上限。
_MAX_REVOKE_REASON = 200


def _coerce_annotation_id(raw: Any) -> int:
    """路径/调用方给的标注 id 必须是正整数；bool 不算（True 会被 int() 收下）。"""
    if isinstance(raw, bool):
        raise ActionError(f"标注 id 必须是正整数：{raw}", status_code=400)
    try:
        aid = int(raw)
    except (TypeError, ValueError):
        raise ActionError(f"标注 id 必须是正整数：{raw}", status_code=400) from None
    if aid <= 0:
        raise ActionError(f"标注 id 必须是正整数：{raw}", status_code=400)
    return aid


def _annotation_lookup_denied(annotation_id: Any) -> str:
    """「不存在」与「不是你的」共用**同一句**（2026-10-29 二次复审）。

    两种情况的文案只要有差别，404 的 `detail` 就变成存在性探针：攻击者逐号试，
    靠回显就能枚举出"这条线存在、只是不属于我"。所以这里只有一个模板，
    调用方不许自己拼。
    """
    return f"标注 #{annotation_id} 不存在或不属于当前账号"


def _load_owned_annotation(db: Session, *, user_id: Any, annotation_id: Any) -> Any:
    """取一条**属于当前账号**的标注；不存在/他人的按 404，无主的按 409。"""
    from src.db.models import KlineAnnotation

    aid = _coerce_annotation_id(annotation_id)
    row = db.query(KlineAnnotation).filter(KlineAnnotation.id == aid).one_or_none()
    if row is None:
        raise ActionError(_annotation_lookup_denied(aid), status_code=404)
    owner = row.user_id
    if owner is None:
        # 无主行是"存在且不属于任何人"，与"不是你的"不是一类事实：它无法被任何账号
        # 撤销，只能由运维处理。它不是越权探针的答案（任何账号得到同一句）。
        raise ActionError(
            f"标注 #{aid} 是无主标注（全局行，user_id 为空）：它不属于任何账号，"
            "服务端不能代替某个账号撤销它",
            status_code=409,
        )
    if str(owner) != str(user_id):
        # 与不存在同句：不泄露"这条标注存在、但不是你的"
        raise ActionError(_annotation_lookup_denied(aid), status_code=404)
    return row


def _confirming_action_for(db: Session, *, user_id: Any, annotation_id: int) -> Any:
    """反查这条标注的确认动作（`copilot_actions.annotation_id`，唯一索引）。"""
    from sqlalchemy.exc import MultipleResultsFound

    from src.db.models import CopilotAction

    try:
        action = (
            db.query(CopilotAction)
            .filter(CopilotAction.annotation_id == int(annotation_id))
            .one_or_none()
        )
    except MultipleResultsFound:
        # 唯一索引失效的库（手工改过/迁移没跑）：一条线被多条动作认领，拒绝猜测归属
        raise ActionError(
            f"标注 #{annotation_id} 被多条动作关联（唯一索引失效），归属无法确定，拒绝撤销",
            status_code=409,
        ) from None
    if action is None:
        raise ActionError(
            f"标注 #{annotation_id} 没有关联的确认动作（不是参谋确认画上去的），不在撤销范围",
            status_code=409,
        )
    if str(action.user_id) != str(user_id):
        raise ActionError(
            f"标注 #{annotation_id} 与确认动作的归属不一致（数据异常），拒绝撤销",
            status_code=409,
        )
    return action


def _target_from_payload(payload: dict) -> dict:
    """动作 payload 归一成与标注行可比的形状（与 `_apply_annotation` 同一把尺）。

    `instrument` 必须一起比（2026-10-29 二次复审）：`000001` 的指数线与个股线是同
    symbol/同 market、价格也能相同，但它们是**两条互不可见的通道**（读取端按
    instrument 过滤）。漏比类型 = 主键复用后把另一个通道的线当自己的删掉。
    """
    symbol = str(payload.get("symbol") or "").strip()
    market = _normalize_market(payload.get("market"))
    return {
        "symbol": symbol,
        "label": str(payload.get("label") or "").strip()[:64],
        "period": _normalize_period(payload.get("period")),
        "market": market,
        # 与 apply 端同一个入口：解析不出/不合法就在写入口被拒过，这里同样抛 ActionError
        "instrument": _guard_annotation_target(symbol, market, payload.get("instrument")),
        "price": _as_price(payload.get("price")) if payload.get("price") is not None else None,
        "ts": _normalize_ts(payload.get("ts")),
    }


#: 价位比较容差（比对与条件 DELETE 共用，避免两边尺子不同）。
_PRICE_EPS = 1e-9


def _same_price(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < _PRICE_EPS
    except (TypeError, ValueError):
        return False


def _matched_target(annotation: Any, action: Any) -> dict | None:
    """这条标注是不是**这次动作**画出来的那条；是则返回归一后的目标字段。

    返回 dict 而不是 bool：调用方要拿**同一份**归一字段去做条件 DELETE，
    否则"比对用的值"和"删除用的值"各写一遍，迟早漂移成新的 TOCTOU。

    `annotation_id` 是无外键弱引用：标注被删后 SQLite 会用同一个 id 建新行，
    旧链接就直接指向了别人的另一条线。撤销前必须核对目标字段 —— 不核就是拿一个
    过期的整数删数据。
    """
    try:
        payload = json.loads(action.payload or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        want = _target_from_payload(payload)
    except ActionError:
        return None
    if (
        annotation.symbol == want["symbol"]
        and annotation.label == want["label"]
        and annotation.period == want["period"]
        and annotation.market == want["market"]
        and annotation.instrument == want["instrument"]
        and annotation.ts == want["ts"]
        and _same_price(annotation.price, want["price"])
    ):
        return want
    return None


def _annotation_matches_action(annotation: Any, action: Any) -> bool:
    """`_matched_target` 的布尔形态（保留既有调用面）。"""
    return _matched_target(annotation, action) is not None


def _revoke_annotation_row(
    db: Session, *, user_id: Any, annotation: Any, action: Any, reason: str = ""
) -> dict:
    """在一个事务里：条件认领动作 → 条件删除标注 → 提交 → 写审计。

    `apply_action` 是「条件 UPDATE 认领 pending → 落标注 → 同一事务提交」；
    撤销是它的镜像：条件 UPDATE 认领 applied → 删标注 → 同一事务提交。
    任何一步没拿到行都整笔回滚，绝不留下「动作 rolled_back 但线还在图上」。

    认领时一并把 `copilot_actions.annotation_id` 置空：弱引用指向的行已被删除，
    留着就是悬空链接（会挡住主键复用方言上下一次确认）。撤销对象由 `result`
    与 `AuditLog.detail.annotation` 追溯。
    """
    from src.core.db_safe import safe_rollback
    from src.db.models import CopilotAction, KlineAnnotation

    aid = int(annotation.id)
    reason_text = str(reason or "")[:_MAX_REVOKE_REASON]

    if action.kind != "annotation":
        raise ActionError(
            f"动作 #{action.id} 不是标注动作（kind={action.kind}），不能走标注撤销",
            status_code=409,
        )
    if str(action.user_id) != str(user_id):
        raise ActionError(
            f"标注 #{aid} 与确认动作的归属不一致（数据异常），拒绝撤销",
            status_code=409,
        )
    if action.status == "rolled_back":
        raise ActionError(
            f"标注 #{aid} 对应的动作已是 rolled_back 状态（已撤销）", status_code=409
        )
    if action.status != "applied":
        raise ActionError(
            f"确认动作当前状态是 {action.status}，只有 applied 的标注能撤销",
            status_code=409,
        )
    want = _matched_target(annotation, action)
    if want is None:
        raise ActionError(
            f"标注 #{aid} 与确认动作 #{action.id} 记录的目标不一致（编号可能已被复用），"
            "拒绝删除这条标注",
            status_code=409,
        )

    snapshot = {
        "id": aid, "symbol": annotation.symbol, "market": annotation.market,
        "instrument": annotation.instrument, "period": annotation.period,
        "price": annotation.price, "ts": annotation.ts, "label": annotation.label,
        "note": annotation.note, "color": annotation.color, "source": annotation.source,
        "user_id": str(annotation.user_id),
    }

    try:
        # 原子认领：只有仍是 applied 的那一次能把状态推进到 rolled_back。
        # 并发/连点撤销的第二个请求 rowcount=0 → 明确 409，而不是删两次。
        # 同时把弱引用 annotation_id 置空：行已经删了，留着一个指向不存在行的整数
        # 正是 `_guard_annotation_id` 要处理的悬空链接（SQLite 复用主键时还会挡住
        # 别的账号下一次确认）。撤销对象记录在 result 与审计里，不在行上留指针。
        claimed = (
            db.query(CopilotAction)
            .filter(
                CopilotAction.id == action.id,
                CopilotAction.user_id == str(user_id),
                CopilotAction.status == "applied",
            )
            .update({"status": "rolled_back", "annotation_id": None},
                    synchronize_session=False)
        )
        if not claimed:
            safe_rollback(db)
            raise ActionError("该标注刚被另一个请求撤销，请刷新", status_code=409)

        # 条件 DELETE 必须复述**比对过的那一整份目标**，不能只按 id+user_id
        # （2026-10-29 二次复审 TOCTOU）：读→比对→删之间行可能被改写/复用，只比 id
        # 就会拿一个过期的身份删掉现在的另一条线。rowcount=0 ⇒ 窗口里变了，整笔回滚。
        delete_cond = [
            KlineAnnotation.id == aid,
            KlineAnnotation.user_id == str(user_id),   # NULL 行永不相等 ⇒ 无主行不会被删
            KlineAnnotation.symbol == want["symbol"],
            KlineAnnotation.label == want["label"],
            KlineAnnotation.period == want["period"],
            KlineAnnotation.market == want["market"],
            KlineAnnotation.instrument == want["instrument"],
        ]
        if want["price"] is None:
            delete_cond.append(KlineAnnotation.price.is_(None))
        else:
            # 与 `_same_price` 用同一容差：比对通过了，删除条件也必须按同一把尺判定，
            # 否则"1e-9 内相等"的行会比对通过、却在 DELETE 处拿 0 行 → 假冲突。
            delete_cond.append(
                func.abs(KlineAnnotation.price - float(want["price"])) < _PRICE_EPS
            )
        delete_cond.append(
            KlineAnnotation.ts.is_(None) if want["ts"] is None
            else KlineAnnotation.ts == want["ts"]
        )
        deleted = (
            db.query(KlineAnnotation)
            .filter(*delete_cond)
            .delete(synchronize_session=False)
        )
        if not deleted:
            safe_rollback(db)
            raise ActionError(
                "标注在本次撤销的读取与删除之间被改动（内容或归属已变更），"
                "本次撤销未生效，请刷新后重试",
                status_code=409,
            )

        action.result = f"已撤销标注 #{aid}：{snapshot['label']}（{snapshot['symbol']}）"
        # 审计与删除**同一事务**（2026-10-29 二次复审）：行删掉之后审计是唯一快照源，
        # 先提交删除再 best-effort 写审计 = 审计一失败快照就永久丢失（谁删了哪条线
        # 再也查不回来）。所以审计只 add、不单独提交，任何异常都整笔回滚。
        _stage_rollback_audit(
            db, user_id=user_id, action_id=int(action.id), kind="annotation",
            undone=[f"已撤销标注 #{aid}：{snapshot['label']}"],
            reason=reason_text,
            extra={"annotation_id": aid, "annotation": snapshot,
                   "action_status": "rolled_back"},
        )
        db.commit()
    except ActionError:
        safe_rollback(db)
        raise
    except Exception:
        # 未知异常回滚后原样抛出（5xx），不伪装成 400；也不留半截状态
        safe_rollback(db)
        logger.exception("标注撤销失败, 已回滚: annotation=%s action=%s", aid, action.id)
        raise

    db.refresh(action)
    return {
        "ok": True,
        "status": "revoked",
        "annotation_id": aid,
        "action_id": int(action.id),
        "action_status": "rolled_back",
        "symbol": snapshot["symbol"],
        "label": snapshot["label"],
        "reason": reason_text,
        # 这里是**对外 JSON 的 UTC 时刻**(带 Z 后缀), 不走库口径 —— 库里存北京 naive,
        # 但这条快照字段是给调用方/审计读的绝对时刻, 必须真的是 UTC。
        "revoked_at": to_iso_utc(utc_now()),
    }


def revoke_annotation(db: Session, *, user_id: Any, annotation_id: Any, reason: str = "") -> dict:
    """撤销一条**本人确认产生**的已上图标注（2026-10-29, P2）。

    入口按标注 id 寻址：`DELETE /api/copilot/annotations/{annotation_id}`。
    越权/无主/无确认动作/编号复用/并发冲突全部显式拒绝，且一行不改（见模块注释）。
    """
    annotation = _load_owned_annotation(db, user_id=user_id, annotation_id=annotation_id)
    action = _confirming_action_for(db, user_id=user_id, annotation_id=int(annotation.id))
    return _revoke_annotation_row(
        db, user_id=user_id, annotation=annotation, action=action, reason=reason
    )


def _alert_facts(snapshot: dict) -> dict:
    """apply 时随动作落库的「本次实际写下」事实（老记录没有这一段）。"""
    facts = snapshot.get("applied")
    return facts if isinstance(facts, dict) else {}


def _legacy_condition_matches(payload: dict, rule: Any) -> bool:
    """老记录（快照没存"本次写入的条件"）里，判断规则当前值是否仍是本次动作写下的。

    payload 缺 price 时**证明不出**，返回 False（调用方按"已被改动、不覆盖"处理）——
    fail-closed 的方向永远是"不动用户数据"。
    """
    price = payload.get("price")
    if price is None:
        return False
    try:
        expected = _alert_condition(str(payload.get("direction") or "above"), price)
    except (TypeError, ValueError):
        return False
    return _normalized_condition(rule.condition_group) == _normalized_condition(expected)


def _rollback_alert(db: Session, *, user_id: Any, payload: dict, snapshot: dict) -> list[str]:
    """撤销一次提醒动作 —— **只撤本次动作确认属于它的那条规则**。

    硬约束（2026-11-07 复审 B1/B2；三次复审统一判据）:

    1. **归属只认 ID**：`apply` 时把 `rule_id` 写进快照 `applied` 段。撤销按这个 ID
       取行，再核对属主 / 自选 / 内容。绝不"取该标的最新一条"—— 用户在确认之后自己
       建的同标的提醒会被那条逻辑删掉。
    2. **证明不了就不动（fail-closed）**：证据四件套（规则 ID、属主、所属自选、本次
       写入的条件）由 `_alert_rollback_rule` 一处判定 —— 与 `can_rollback` 完全同源，
       不会出现"前端说可撤、点下去必 409"。缺任何一项都 409、一行不删，不猜。
    3. **内容被改过就不删**：规则当前条件等于本次动作写下的值才删。用户随后改过条件
       / 停用过，说明这条规则已被用户接管，删它就是销毁用户的编辑 ⇒ 同样 409 拒绝。
    4. **自选行保留**：动作顺带建的自选**不删**。`Stock.positions` / `Stock.agents` 是
       `cascade="all, delete-orphan"`，删自选会连带删掉用户后来加的持仓、监控配置；
       为了让"撤销干净"而毁用户数据，次序反了。保留这件事写进撤销结果，不静默。
    """
    from src.db.models import Stock

    undone: list[str] = []
    facts = _alert_facts(snapshot)
    rid, sid, _expected, _legacy = _alert_rollback_ref(snapshot)

    rule, deny = _alert_rollback_rule(
        db, user_id=user_id, payload=payload, snapshot=snapshot
    )
    if deny is not None:
        raise ActionError(deny, status_code=409)

    if rule is None:
        undone.append(f"提醒 #{int(rid)} 已不存在，无需恢复")
    elif facts:
        # ── 新记录：ID 明确，按 `applied` 段恢复 ──────────────────────────
        rule_id = int(rule.id)
        if facts.get("rule_created"):
            db.delete(rule)
            db.flush()
            undone.append(f"已删除本次新建的盯盘提醒 #{rule_id}")
        else:
            before = facts.get("rule_before") or {}
            if before.get("condition") is not None:
                rule.condition_group = before["condition"]
            if before.get("enabled") is not None:
                rule.enabled = bool(before["enabled"])
            if before.get("name") is not None:
                rule.name = before["name"]
            db.flush()
            undone.append(f"已恢复提醒 #{rule_id} 的触发条件与启用状态")
    else:
        # ── 老记录：只有快照，写回快照里的原值 ────────────────────────────
        rule_id = int(rule.id)
        rule.condition_group = snapshot.get("rule_condition")
        if snapshot.get("rule_enabled") is not None:
            rule.enabled = bool(snapshot["rule_enabled"])
        if snapshot.get("rule_name"):
            rule.name = snapshot["rule_name"]
        db.flush()
        undone.append(f"已恢复提醒 #{rule_id} 的触发条件")
        if snapshot.get("rule_enabled") is None:
            undone.append("历史快照未记录启用状态，如该规则原本停用请手动停用")

    stock_id = facts.get("stock_id") if facts else sid
    stock_created = bool(facts.get("stock_created")) if facts else False
    if stock_created and stock_id is not None:
        still = db.query(Stock).filter(Stock.id == int(stock_id), Stock.user_id == str(user_id)).first()
        if still is not None:
            # 明确保留 + 说明：删自选会级联删用户持仓/监控。
            undone.append(
                f"自选 {still.symbol}（{still.market}）已保留：删除自选会连带删掉挂在这只票上的"
                "持仓/监控等数据，撤销不碰它；如不需要请到自选页手动移出"
            )
    return undone


def rollback_action(db: Session, *, user_id: Any, action_id: int, reason: str = "") -> dict:
    """把一次已 apply 的动作退回到 apply 之前的状态，并写审计。

    支持四类：标注（删掉那条线 + 动作转 rolled_back）/ 加自选 / 建提醒 / 建定时任务。
    撤销只删"这次动作新建的东西"或写回快照值，**不碰用户其它数据**。
    """
    from src.db.models import CopilotAction, Stock

    row = (
        db.query(CopilotAction)
        .filter(CopilotAction.id == action_id, CopilotAction.user_id == str(user_id))
        .one_or_none()
    )
    if row is None:
        raise ActionError("动作不存在", status_code=404)
    if row.status != "applied":
        raise ActionError(f"只有 applied 的动作能撤销，当前是 {row.status}", status_code=409)
    if row.kind not in ROLLBACKABLE_KINDS:
        raise ActionError(f"{_KIND_NAMES.get(row.kind, row.kind)}动作不支持回滚")
    if row.kind == "annotation":
        # 2026-10-29：标注走**删行**而不是快照回退（`_snapshot` 对它返回空 dict）。
        if row.annotation_id is None:
            raise ActionError(
                "这条标注动作早于 v231 的标注关联契约，没有可撤销的标注记录；"
                "无法确定要删哪条线，因此不撤",
                status_code=409,
            )
        annotation = _load_owned_annotation(db, user_id=user_id,
                                            annotation_id=row.annotation_id)
        revoked = _revoke_annotation_row(
            db, user_id=user_id, annotation=annotation, action=row, reason=reason
        )
        out = _to_dict(row, db=db)
        out["revoked_annotation"] = revoked
        return out
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
            undone.extend(
                _rollback_alert(db, user_id=user_id, payload=payload, snapshot=snapshot)
            )
        elif row.kind == "schedule":
            from src.core.copilot_schedule import delete as _delete

            sid = snapshot.get("schedule_id")
            if sid is None:
                _delete(db, user_id=user_id, schedule_id=(payload.get("id") or 0))
                undone.append("已删除本次新建的定时任务")
            else:
                undone.append("定时任务早于本次动作，未改动")
        else:
            # 防御: SNAPSHOT_ROLLBACK_KINDS 扩了但这里没接对应实现时, 明确说"不支持"。
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
    return _to_dict(row, db=db)


def _rollback_audit_detail(*, action_id: int, kind: str, undone: list[str],
                           reason: str, extra: dict | None = None) -> dict[str, Any]:
    """撤销审计的 detail 结构（严格/宽松两条路径共用，避免两边漂移）。"""
    detail: dict[str, Any] = {
        "action_id": action_id, "kind": kind,
        "undone": undone, "reason": (reason or "")[:200],
    }
    if extra:
        detail.update(extra)
    return detail


def _stage_rollback_audit(db: Session, *, user_id: Any, action_id: int, kind: str,
                          undone: list[str], reason: str, extra: dict | None = None) -> None:
    """把撤销审计挂到**当前事务**上：只 `add`，不提交、不吞异常。

    调用方（标注撤销）必须在同一个 `commit()` 里带上审计：审计失败要整笔回滚，
    否则"行删了但审计没落库"就是永久丢快照。

    ⚠️ `AuditLog.detail` 必须保持**不限长**（模型是 Text，存量库迁移 v233 放宽）：
    这里的 detail 是整份标注快照 JSON，超过 255 时 PG 会 StringDataRightTruncation，
    而它和删除在同一事务 ⇒ 整笔回滚 ⇒ 这条线永远撤不掉。**不许**为了迁就列宽截断。
    """
    from src.db.models import AuditLog

    db.add(AuditLog(
        user_id=str(user_id),
        action="copilot_action_rollback",
        detail=json.dumps(
            _rollback_audit_detail(action_id=action_id, kind=kind, undone=undone,
                                   reason=reason, extra=extra),
            ensure_ascii=False,
        ),
    ))


def _audit_rollback(db: Session, *, user_id: Any, action_id: int, kind: str,
                    undone: list[str], reason: str, extra: dict | None = None) -> None:
    """回滚/撤销本身也要留痕 —— 否则审计里只看到 apply，看不到撤销。

    口径保持 2026-10-29 之前的样子：**best-effort**（动作已提交，审计失败只告警）。
    只有标注撤销改成"审计与删除同一事务"（见 `_stage_rollback_audit`），
    其余三类动作的既有行为不受影响。
    """
    try:
        _stage_rollback_audit(db, user_id=user_id, action_id=action_id, kind=kind,
                              undone=undone, reason=reason, extra=extra)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        from src.core.db_safe import safe_rollback
        safe_rollback(db)
        logger.warning("动作回滚审计写入失败(不阻断): %s", exc)

