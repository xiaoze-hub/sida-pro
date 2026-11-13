from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from src.db.session import SessionLocal
from src.db.models import (
    AgentContextRun,
    AgentPredictionOutcome,
    NewsTopicSnapshot,
    StockContextSnapshot,
    User,
)
from src.core.json_safe import to_jsonable

logger = logging.getLogger(__name__)

# SQLSTATE 格式白名单(5 位大写字母/数字)。只允许这种**分类码**出现在日志/对外原因里,
# 数据库异常原文常把 SQL、绑定参数、列值(user_id/用户名/股票代码)带出来。
_SQLSTATE_RE = re.compile(r"^[0-9A-Z]{5}$")


@dataclass(frozen=True)
class OutcomeSaveResult:
    """一次预测后验写入的**真实**结果(不是"调用成功")。

    `written` 只有真的插入了新行才为 True; `bool(result)` 与之一致。调用方据此计数,
    避免把「已评估不改」「本版被拒」「并发被唯一键挡住」当成保存成功上报。

    `scored` 表示这一行是**计分行**(参与后验评估/命中率); 追加的审计版本
    (`status='appended_superseded'`) 是 `written=True, scored=False` —— 存下来了,
    但绝不重复计分。

    status 取值:
      inserted / appended_superseded / unchanged_same_version / kept_evaluated /
      refused_no_owner / refused_bad_scope / refused_bad_key / refused_no_version /
      refused_duplicate_rows / refused_unknown_user / concurrent_duplicate /
      scoring_blocked / db_unavailable / write_failed
    """

    status: str
    written: bool
    scored: bool = False
    reason: str = ""
    record_id: int | None = None

    def __bool__(self) -> bool:  # 只在真写入时为真
        return bool(self.written)

    @property
    def ok(self) -> bool:
        """写入了新行 = True; 其余(unwritten/refused)一律 False。"""
        return bool(self.written)


# 归属桶: 个人 / 系统。系统桶 user_id 恒为 NULL, 绝不猜测绑定真实用户。
SCOPE_USER = "user"
SCOPE_SYSTEM = "system"
SCOPE_UNKNOWN_LEGACY = None  # 加列前的历史行: 归属不可证明, 保留 unknown
# 追加的审计版本: 不是计分行, 不参与评估/命中率
STATUS_SUPERSEDED = "superseded"


def _db_error_kind(exc: BaseException) -> str:
    """把 DB 异常分成可行动的类型(不再一律当"保存失败")。

    - unique: 唯一键冲突(并发同版本 / 并发计分行) —— 可以按幂等或追加处理;
    - foreign_key: 归属用户不存在(users 表无此 id) —— 不能写, 也不能假装是并发;
    - not_null / check: 数据本身不合 schema;
    - pg_<code>: 其它 PG SQLSTATE;
    - other: 其它完整性错误。

    PG 优先用 SQLSTATE(psycopg 的 orig.pgcode), 没有时退回匹配错误文本(sqlite)。

    返回值只用于**分类**, 绝不回显原文: 数据库异常文本常带 SQL、绑定参数、列值
    (user_id / 股票代码 / 用户名), 写进日志或 API reason 就是泄露。
    """
    orig = getattr(exc, "orig", None)
    code = str(getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None) or "").strip()
    # SQLSTATE 只接受 5 位 [0-9A-Z]; 其它内容(驱动把消息塞进 pgcode 之类)一律丢弃。
    if code and _SQLSTATE_RE.fullmatch(code):
        return {
            "23505": "unique",
            "23503": "foreign_key",
            "23502": "not_null",
            "23514": "check",
        }.get(code, f"pg_{code}")
    msg = f"{orig or exc}".lower()
    if "unique constraint" in msg or "duplicate key" in msg or "unique failed" in msg:
        return "unique"
    if "foreign key" in msg:
        return "foreign_key"
    if "not null" in msg:
        return "not_null"
    return "other"


def _scoring_gate(*, bind=None, allow_repair: bool = False):
    """写入/回填前的评分可信度闸门(fail-closed, 2026-11-13)。

    计分键唯一性完全依赖 `scoring_guard.INDEX_SPECS` 的四条部分唯一索引; 它们缺失或定义
    不符时, 写入/回填会制造同键重复计分行(命中率双计)。这里不猜、不兜底: 直接问闸门。

    `bind` 传**这次会话自己的连接/引擎**, 保证体检的就是接下来要写的那个库(而不是 import
    时定下的模块级引擎)。运行期一律 `allow_repair=False`: 业务角色没有 DDL 权限, 热点路径
    不建/删索引。
    """
    from src.core.scoring_guard import check_scoring_health

    return check_scoring_health(bind=bind, allow_repair=allow_repair)


def _session_gate_bind(db):
    """给闸门挑"这次会话正在用的"绑定: 优先复用会话连接(不多 checkout), 退化到 bind。

    测试/包装会话可能没有 `connection()`; 这时回落 `bind`, 保证闸门仍检查同一个库,
    而不是退回模块级默认引擎。
    """
    getter = getattr(db, "connection", None)
    if callable(getter):
        try:
            return getter()
        except Exception:  # noqa: BLE001 — 取连接失败时退回 bind, 由闸门按不可用阻断
            pass
    return getattr(db, "bind", None)


def _scoring_gate_for_session(db):
    """用**本会话的绑定**过闸门; 拿不到绑定就按不可用阻断(绝不退回默认引擎)。"""
    bind = _session_gate_bind(db)
    if bind is None:
        from src.core.scoring_guard import STATUS_BLOCKED, ScoringHealth

        return ScoringHealth(
            status=STATUS_BLOCKED,
            reason_code="unavailable",
            detail="无法确定本次会话的数据库绑定 → 不能断言索引在位, 评分阻断",
        )
    return _scoring_gate(bind=bind)


def save_stock_context_snapshot(
    *,
    symbol: str,
    market: str,
    snapshot_date: str,
    context_type: str,
    payload: dict,
    quality: dict | None = None,
) -> bool:
    db = SessionLocal()
    try:
        payload_safe = to_jsonable(payload or {})
        quality_safe = to_jsonable(quality or {})
        existing = (
            db.query(StockContextSnapshot)
            .filter(
                StockContextSnapshot.symbol == symbol,
                StockContextSnapshot.market == market,
                StockContextSnapshot.snapshot_date == snapshot_date,
                StockContextSnapshot.context_type == context_type,
            )
            .first()
        )
        if existing:
            existing.payload = payload_safe
            existing.quality = quality_safe
        else:
            db.add(
                StockContextSnapshot(
                    symbol=symbol,
                    market=market,
                    snapshot_date=snapshot_date,
                    context_type=context_type,
                    payload=payload_safe,
                    quality=quality_safe,
                )
            )
        db.commit()
        return True
    except Exception as e:
        # 只记异常类型: 数据库异常原文可能带 SQL/绑定参数/列值。
        logger.warning("保存 stock context snapshot 失败: %s", type(e).__name__)
        db.rollback()
        return False
    finally:
        db.close()


def get_recent_stock_context_snapshots(
    *,
    symbol: str,
    market: str,
    context_type: str | None = None,
    days: int = 30,
    limit: int = 30,
) -> list[StockContextSnapshot]:
    db = SessionLocal()
    try:
        cutoff = (date.today() - timedelta(days=max(days, 1))).strftime("%Y-%m-%d")
        q = db.query(StockContextSnapshot).filter(
            StockContextSnapshot.symbol == symbol,
            StockContextSnapshot.market == market,
            StockContextSnapshot.snapshot_date >= cutoff,
        )
        if context_type:
            q = q.filter(StockContextSnapshot.context_type == context_type)
        return (
            q.order_by(StockContextSnapshot.snapshot_date.desc())
            .limit(max(1, limit))
            .all()
        )
    finally:
        db.close()


def save_news_topic_snapshot(
    *,
    snapshot_date: str,
    window_days: int,
    symbols: list[str],
    summary: str,
    topics: list[str],
    sentiment: str,
    coverage: dict | None = None,
) -> bool:
    db = SessionLocal()
    try:
        existing = (
            db.query(NewsTopicSnapshot)
            .filter(
                NewsTopicSnapshot.snapshot_date == snapshot_date,
                NewsTopicSnapshot.window_days == window_days,
            )
            .first()
        )
        payload = to_jsonable(
            {
                "symbols": symbols or [],
                "summary": summary or "",
                "topics": topics or [],
                "sentiment": sentiment or "neutral",
                "coverage": coverage or {},
            }
        )
        if existing:
            existing.symbols = payload["symbols"]
            existing.summary = payload["summary"]
            existing.topics = payload["topics"]
            existing.sentiment = payload["sentiment"]
            existing.coverage = payload["coverage"]
        else:
            db.add(
                NewsTopicSnapshot(
                    snapshot_date=snapshot_date,
                    window_days=int(window_days),
                    symbols=payload["symbols"],
                    summary=payload["summary"],
                    topics=payload["topics"],
                    sentiment=payload["sentiment"],
                    coverage=payload["coverage"],
                )
            )
        db.commit()
        return True
    except Exception as e:
        logger.warning("保存 news topic snapshot 失败: %s", type(e).__name__)
        db.rollback()
        return False
    finally:
        db.close()


def get_latest_news_topic_snapshot(
    *,
    window_days: int = 7,
) -> NewsTopicSnapshot | None:
    db = SessionLocal()
    try:
        return (
            db.query(NewsTopicSnapshot)
            .filter(NewsTopicSnapshot.window_days == int(window_days))
            .order_by(NewsTopicSnapshot.snapshot_date.desc())
            .first()
        )
    finally:
        db.close()


def save_agent_context_run(
    *,
    agent_name: str,
    stock_symbol: str,
    analysis_date: str,
    context_payload: dict,
    quality: dict | None = None,
) -> bool:
    db = SessionLocal()
    try:
        payload_safe = to_jsonable(context_payload or {})
        quality_safe = to_jsonable(quality or {})
        db.add(
            AgentContextRun(
                agent_name=agent_name,
                stock_symbol=stock_symbol or "*",
                analysis_date=analysis_date,
                context_payload=payload_safe,
                quality=quality_safe,
            )
        )
        db.commit()
        return True
    except Exception as e:
        logger.warning("保存 agent context run 失败: %s", type(e).__name__)
        db.rollback()
        return False
    finally:
        db.close()


def list_recent_agent_context_runs(
    *,
    agent_name: str,
    stock_symbol: str | None = None,
    days: int = 30,
    limit: int = 50,
) -> list[AgentContextRun]:
    db = SessionLocal()
    try:
        cutoff = (date.today() - timedelta(days=max(days, 1))).strftime("%Y-%m-%d")
        q = db.query(AgentContextRun).filter(
            AgentContextRun.agent_name == agent_name,
            AgentContextRun.analysis_date >= cutoff,
        )
        if stock_symbol:
            q = q.filter(AgentContextRun.stock_symbol == stock_symbol)
        return q.order_by(AgentContextRun.created_at.desc()).limit(max(1, limit)).all()
    finally:
        db.close()


def _owner_alive_clause():
    """`user_id` 必须指向**现存**的用户行 —— ghost(悬空 id)不算"有归属"。

    为什么需要(2026-11-12 三次修正): 迁移库/旧库可能没有 user_id 外键(见 migrations.m249),
    用户被物理清除后行里会留下悬空 user_id。这些行既不能证明属于谁, 也不再对应任何账号,
    若按"有 user_id = 有归属"处理, 就会被评估、被当成成绩来源(ghost 用户错误计分)。
    判定改为"用户行仍然存在"才叫有归属; 查不到就是 unknown, 只报数, 不评估、不呈现。
    """
    return AgentPredictionOutcome.user_id.in_(select(User.id))


def _outcome_key_rows(
    db,
    *,
    scope: str,
    user_id: str,
    agent_name: str,
    stock_symbol: str,
    stock_market: str,
    prediction_date: str,
    horizon_days: int,
):
    """取同一键下的**全部**版本行(含 superseded), 按 id 升序。

    个人桶按 `user_id` 精确过滤; system 桶要求 `user_id IS NULL` 且
    `owner_scope='system'` —— 两类桶互不重叠, system 桶永远不会把真实用户的行算进来,
    也永远不会被"猜"成某个真实用户。
    """
    q = db.query(AgentPredictionOutcome).filter(
        AgentPredictionOutcome.agent_name == agent_name,
        AgentPredictionOutcome.stock_symbol == stock_symbol,
        AgentPredictionOutcome.stock_market == stock_market,
        AgentPredictionOutcome.prediction_date == prediction_date,
        AgentPredictionOutcome.horizon_days == horizon_days,
    )
    if scope == SCOPE_USER:
        q = q.filter(AgentPredictionOutcome.user_id == user_id)
    else:
        q = q.filter(
            AgentPredictionOutcome.user_id.is_(None),
            AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
        )
    return q.order_by(AgentPredictionOutcome.id.asc()).all()


def _build_outcome_row(
    *,
    scope: str,
    user_id: str,
    agent_name: str,
    stock_symbol: str,
    stock_market: str,
    prediction_date: str,
    horizon_days: int,
    action: str,
    action_label: str,
    confidence: float | None,
    trigger_price: float | None,
    meta_payload: dict,
    outcome_status: str,
    run_trace_id: str,
) -> AgentPredictionOutcome:
    return AgentPredictionOutcome(
        user_id=(user_id or None) if scope == SCOPE_USER else None,
        owner_scope=scope,
        run_trace_id=run_trace_id,
        agent_name=agent_name,
        stock_symbol=stock_symbol,
        stock_market=stock_market,
        prediction_date=prediction_date,
        horizon_days=horizon_days,
        action=action or "watch",
        action_label=action_label or "观望",
        confidence=confidence,
        trigger_price=trigger_price,
        outcome_status=outcome_status,
        meta=meta_payload,
    )


def save_agent_prediction_outcome(
    *,
    agent_name: str,
    stock_symbol: str,
    stock_market: str,
    prediction_date: str,
    horizon_days: int,
    action: str,
    action_label: str,
    confidence: float | None = None,
    trigger_price: float | None = None,
    meta: dict | None = None,
    user_id: str | None = None,
    run_trace_id: str | None = None,
    owner_scope: str | None = None,
) -> OutcomeSaveResult:
    """写入一条待后验的预测记录 —— **只追加, 绝不改写既有结果**（2026-11-12 二次修正）。

    规则（每条都保守、可验证）:
      1. 归属桶只由调用方声明: `owner_scope='user'` + 显式 `user_id`
         (来自 `context.user.id`), 或 `owner_scope='system'`(无会话的系统调度运行)。
         **绝不猜测绑定真实用户**: system 桶的 user_id 恒为 NULL; 传了 user_id 又要
         system 桶 → 直接拒写(`refused_bad_scope`)。个人桶缺 user_id → 拒写
         (`refused_no_owner`), 不静默归到某个账号或系统桶。
      2. 同一键(用户/系统, agent, symbol, market, date, horizon)只有**一条计分行**, 且
         计分版本 = **最早写入的那一版**(先到先得; 后续重跑不能因看到了更多行情而替换):
         - 没有计分行 → 本版插入为 `pending`, 是计分行(`inserted`, scored=True);
         - 已有计分行、且 `run_trace_id` 与它相同 → 幂等, 不写(`unchanged_same_version`);
         - 已有计分行、版本不同 → **追加**一条 `superseded` 审计版本(`appended_superseded`,
           written=True/scored=False): 信息不丢, 但绝不覆盖、绝不重复计分;
         - 已有计分行且它是终态(已评估等) → 同样只追加 superseded 审计版本
           (`appended_superseded`), 绝不改成绩;
         - 同键出现**多条计分行**(历史脏数据/索引被手工删过) → `refused_duplicate_rows`:
           无法判定哪条是计分行, 不挑一行改, 也不再插第三行, 记 ERROR 要求人工核查。
      3. 版本身份是 `run_trace_id`, 缺失则拒写(`refused_no_version`) —— 没有版本号就
         无法证明"这是同一次运行", 追加会把同一份结果重复存成两版。
      4. DB 兜底: `ux_prediction_outcome_user_active_key` / `_run_key`(system 桶同理)
         保证"至多一条计分行 / 同版本不重复"。并发下按错误类型分派:
         unique → 先看是不是同版本并发(幂等), 不是则只追加 superseded;
         foreign_key → `refused_unknown_user`(归属用户不存在); 其它 → `write_failed`。

    返回值是 `OutcomeSaveResult`(不是 bool): `written` 只在**真的写入了行**时为 True,
    `scored` 只有计分行为 True。调用方据此计数, 杜绝「没写却报 saved」和「审计版本被
    当成计分行重复计数」。
    """
    uid = str(user_id).strip() if user_id is not None else ""
    trace = str(run_trace_id).strip() if run_trace_id is not None else ""
    scope = str(owner_scope).strip().lower() if owner_scope is not None else ""
    horizon = max(1, int(horizon_days))
    market = (stock_market or "CN").strip().upper() or "CN"
    date_str = str(prediction_date or "").strip()

    if scope not in ("", SCOPE_USER, SCOPE_SYSTEM):
        logger.error(
            "预测后验不写入(未知归属桶 %r): %s/%s/%s h=%s —— 只接受 user/system",
            scope,
            agent_name,
            stock_symbol,
            date_str,
            horizon,
        )
        return OutcomeSaveResult(
            status="refused_bad_scope", written=False, reason=f"未知 owner_scope={scope!r}"
        )
    if not scope:
        # 未显式声明时只按"有没有用户"区分, 绝不猜具体是谁; 并明确留日志。
        scope = SCOPE_USER if uid else SCOPE_SYSTEM
        logger.info(
            "预测后验未显式声明 owner_scope, 按存在性判为 %s 桶(不绑定任何真实用户): %s/%s/%s",
            scope,
            agent_name,
            stock_symbol,
            date_str,
        )
    if scope == SCOPE_USER and not uid:
        logger.warning(
            "预测后验不写入(个人桶缺少归属用户): %s/%s/%s h=%s —— 无用户维度的行无法隔离, "
            "只能污染统计; 调用方必须传 context.user.id",
            agent_name,
            stock_symbol,
            date_str,
            horizon,
        )
        return OutcomeSaveResult(
            status="refused_no_owner", written=False, reason="个人桶缺少可核验的归属用户"
        )
    if scope == SCOPE_SYSTEM and uid:
        logger.error(
            "预测后验不写入(system 桶不接受真实用户 id): user=%s %s/%s/%s h=%s —— "
            "系统运行不得冒充/绑定某个账号",
            uid,
            agent_name,
            stock_symbol,
            date_str,
            horizon,
        )
        return OutcomeSaveResult(
            status="refused_bad_scope",
            written=False,
            reason="system 桶的 user_id 必须为空(不得绑定真实用户)",
        )
    if not date_str:
        logger.error(
            "预测后验不写入(报告日为空): %s/%s h=%s scope=%s user=%r",
            agent_name,
            stock_symbol,
            horizon,
            scope,
            uid or None,
        )
        return OutcomeSaveResult(status="refused_bad_key", written=False, reason="prediction_date 为空")
    if not trace:
        logger.warning(
            "预测后验不写入(缺少 run_trace_id, 无法证明版本): %s/%s/%s h=%s scope=%s user=%r",
            agent_name,
            stock_symbol,
            date_str,
            horizon,
            scope,
            uid or None,
        )
        return OutcomeSaveResult(
            status="refused_no_version",
            written=False,
            reason="缺少 run_trace_id: 同一结果会被重复追加成多版",
        )

    # 评分闸门(fail-closed, 2026-11-13): 计分键唯一性只由 m250 的四条部分唯一索引保证。
    # 索引缺失/定义错误时并发或重跑会写出第二条计分行(双计分), 因此**拒写**而不是"先写着
    # 看"。这里只读巡检(复用本会话连接, 不再额外 checkout); 运行期禁止 DDL, 修复只由
    # 启动/迁移角色或运维显式执行。
    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001 — 引擎/池不可用: 按不可用拒写, 不外抛
        logger.error("预测后验写入无法建立数据库会话: %s", type(exc).__name__)
        return OutcomeSaveResult(
            status="db_unavailable", written=False, reason="数据库不可用(无法建立会话)"
        )
    health = _scoring_gate_for_session(db)
    if health.blocked:
        logger.error(
            "预测后验不写入(评分阻断: %s): %s/%s/%s h=%s scope=%s user=%r —— 唯一约束不可信, "
            "写入会制造同键重复计分行; 先由启动/迁移角色或运维恢复索引再重试",
            health.reason_code,
            agent_name,
            stock_symbol,
            date_str,
            horizon,
            scope,
            uid or None,
        )
        db.close()
        return OutcomeSaveResult(
            status="scoring_blocked",
            written=False,
            reason=f"评分阻断({health.reason_code}): {health.detail}",
        )

    try:
        meta_safe = to_jsonable(meta or {})
        if not isinstance(meta_safe, dict):
            meta_safe = {}
        meta_payload = {
            **meta_safe,
            "run_trace_id": trace,
            "owner_scope": scope,
        }
        common = dict(
            scope=scope,
            user_id=uid,
            agent_name=agent_name,
            stock_symbol=stock_symbol,
            stock_market=market,
            prediction_date=date_str,
            horizon_days=horizon,
            action=action,
            action_label=action_label,
            confidence=confidence,
            trigger_price=trigger_price,
            meta_payload=meta_payload,
        )

        rows = _outcome_key_rows(
            db,
            scope=scope,
            user_id=uid,
            agent_name=agent_name,
            stock_symbol=stock_symbol,
            stock_market=market,
            prediction_date=date_str,
            horizon_days=horizon,
        )

        # 同一版本重复写入: 幂等(不新增、不改写)
        for r in rows:
            if (str(r.run_trace_id or "").strip()) == trace:
                logger.info(
                    "预测后验同一版本重复写入(幂等, 不新增/不改写): id=%s scope=%s user=%r "
                    "%s/%s/%s h=%s trace=%s",
                    r.id,
                    scope,
                    uid or None,
                    agent_name,
                    stock_symbol,
                    date_str,
                    horizon,
                    trace,
                )
                return OutcomeSaveResult(
                    status="unchanged_same_version",
                    written=False,
                    record_id=r.id,
                    reason="同一 run_trace_id 的重复写入",
                )

        active = [
            r for r in rows if str(r.outcome_status or "") != STATUS_SUPERSEDED
        ]
        if len(active) > 1:
            logger.error(
                "预测后验同键已有多条计分行(历史脏数据, 归属/版本不可判定), 本次不写入: "
                "scope=%s user=%r %s/%s/%s h=%s ids=%s",
                scope,
                uid or None,
                agent_name,
                stock_symbol,
                date_str,
                horizon,
                [r.id for r in active],
            )
            return OutcomeSaveResult(
                status="refused_duplicate_rows",
                written=False,
                reason="同键已有多条计分行, 无法判定计分版本",
            )

        if active:
            keep = active[0]
            keep_status = str(keep.outcome_status or "pending")
            row = _build_outcome_row(
                **common, outcome_status=STATUS_SUPERSEDED, run_trace_id=trace
            )
            db.add(row)
            db.commit()
            logger.warning(
                "预测后验追加审计版本(不改写计分行, 不参与计分): new_id=%s keep_id=%s "
                "keep_status=%s scope=%s user=%r %s/%s/%s h=%s 既有trace=%r 本次trace=%r",
                row.id,
                keep.id,
                keep_status,
                scope,
                uid or None,
                agent_name,
                stock_symbol,
                date_str,
                horizon,
                str(keep.run_trace_id or "") or None,
                trace,
            )
            return OutcomeSaveResult(
                status="appended_superseded",
                written=True,
                scored=False,
                record_id=row.id,
                reason=f"同键已有计分行 id={keep.id}(status={keep_status}), 本版仅留档",
            )

        row = _build_outcome_row(**common, outcome_status="pending", run_trace_id=trace)
        db.add(row)
        db.commit()
        return OutcomeSaveResult(
            status="inserted", written=True, scored=True, record_id=row.id
        )
    except IntegrityError as e:
        db.rollback()
        kind = _db_error_kind(e)
        if kind == "foreign_key":
            logger.error(
                "预测后验写入被外键拒绝(归属用户不存在?): scope=%s user=%r %s/%s/%s h=%s kind=%s",
                scope,
                uid or None,
                agent_name,
                stock_symbol,
                date_str,
                horizon,
                kind,
            )
            return OutcomeSaveResult(
                status="refused_unknown_user",
                written=False,
                reason="归属用户不存在(user_id 外键校验失败)",
            )
        if kind != "unique":
            # 只报分类: 完整性错误原文会带 SQL / 绑定参数(含 user_id、股票代码)。
            logger.error(
                "预测后验写入违反完整性约束: scope=%s user=%r %s/%s/%s h=%s kind=%s",
                scope,
                uid or None,
                agent_name,
                stock_symbol,
                date_str,
                horizon,
                kind,
            )
            return OutcomeSaveResult(
                status="write_failed", written=False, reason=f"完整性约束({kind})"
            )
        # 唯一键: 重新读一次, 判断"同版本并发"还是"计分行并发"
        try:
            rows = _outcome_key_rows(
                db,
                scope=scope,
                user_id=uid,
                agent_name=agent_name,
                stock_symbol=stock_symbol,
                stock_market=market,
                prediction_date=date_str,
                horizon_days=horizon,
            )
        except Exception:  # noqa: BLE001 - 读失败也要按保守路径返回, 不能假装成功
            rows = []
        for r in rows:
            if str(r.run_trace_id or "").strip() == trace:
                logger.info(
                    "预测后验并发同版本(唯一键挡住, 已有同一 trace): id=%s trace=%s",
                    r.id,
                    trace,
                )
                return OutcomeSaveResult(
                    status="unchanged_same_version",
                    written=False,
                    record_id=r.id,
                    reason="并发写入同一 run_trace_id",
                )
        if any(str(r.outcome_status or "") != STATUS_SUPERSEDED for r in rows):
            # 另一进程刚插了计分行: 本版只能作为审计版本追加一次
            try:
                row = _build_outcome_row(
                    scope=scope,
                    user_id=uid,
                    agent_name=agent_name,
                    stock_symbol=stock_symbol,
                    stock_market=market,
                    prediction_date=date_str,
                    horizon_days=horizon,
                    action=action,
                    action_label=action_label,
                    confidence=confidence,
                    trigger_price=trigger_price,
                    meta_payload=meta_payload,
                    outcome_status=STATUS_SUPERSEDED,
                    run_trace_id=trace,
                )
                db.add(row)
                db.commit()
                logger.warning(
                    "预测后验并发下追加审计版本(计分行已在别处写入): new_id=%s scope=%s "
                    "user=%r %s/%s/%s h=%s trace=%s",
                    row.id,
                    scope,
                    uid or None,
                    agent_name,
                    stock_symbol,
                    date_str,
                    horizon,
                    trace,
                )
                return OutcomeSaveResult(
                    status="appended_superseded",
                    written=True,
                    scored=False,
                    record_id=row.id,
                    reason="并发下本版仅留档(计分行已存在)",
                )
            except IntegrityError as e2:
                db.rollback()
                logger.warning(
                    "预测后验并发追加仍被唯一键拒绝: scope=%s user=%r %s/%s/%s h=%s kind=%s",
                    scope,
                    uid or None,
                    agent_name,
                    stock_symbol,
                    date_str,
                    horizon,
                    _db_error_kind(e2),
                )
        logger.warning(
            "预测后验并发重复(DB 唯一键挡住), 本次没有写入: scope=%s user=%r %s/%s/%s h=%s kind=%s",
            scope,
            uid or None,
            agent_name,
            stock_symbol,
            date_str,
            horizon,
            _db_error_kind(e),
        )
        return OutcomeSaveResult(
            status="concurrent_duplicate", written=False, reason="并发写入被唯一键拒绝"
        )
    except OperationalError as e:
        db.rollback()
        logger.error(
            "预测后验写入失败(数据库不可用/锁冲突): scope=%s user=%r %s/%s/%s h=%s kind=%s",
            scope,
            uid or None,
            agent_name,
            stock_symbol,
            date_str,
            horizon,
            _db_error_kind(e),
        )
        return OutcomeSaveResult(
            status="db_unavailable", written=False, reason="数据库不可用/锁冲突"
        )
    except Exception as e:
        db.rollback()
        logger.warning("保存 prediction outcome 失败: %s", type(e).__name__)
        return OutcomeSaveResult(
            status="write_failed", written=False, reason=type(e).__name__
        )
    finally:
        db.close()


def mark_agent_prediction_outcome(
    *,
    record_id: int,
    outcome_price: float | None,
    outcome_return_pct: float | None,
    status: str = "evaluated",
    db=None,
    health=None,
) -> bool:
    """回填某条预测的评估结果（只有归属可证明的**计分行**才允许写）。

    P0(2026-11-12): 加列之前的历史行 `user_id IS NULL` = 归属不可证明。回填它们等于给
    一条不知道属于谁的预测记成绩, 统计里无法隔离 → 直接拒绝并留 WARNING, 让它们保持
    pending(不评估、不冒充成绩), 由人工决定, 不做静默回填。

    二次修正: 追加的审计版本(`outcome_status='superseded'`)也拒绝回填 —— 否则同一键
    会有两条"evaluated", 命中率重复计数; 计分永远只落在唯一那条计分行上。

    三次修正(2026-11-13): 回填**前**先过评分闸门。唯一索引缺失/定义错误时, "这是一条合法
    计分行"本身就无法证明(同键可能已有第二条), 此时回填同样是不可信成绩 → 拒绝并记 ERROR。

    批量评估(2026-11-13): `db` / `health` 允许调用方传入**同一个会话**与批次开始时**只算
    一次**的闸门结论, 避免每一行都开新会话/新连接、把连接池耗干。传了 `health` 就只认它
    (调用方负责在批次边界现查, 不允许用缓存跨批次)。
    """
    own_db = db is None
    if own_db:
        try:
            db = SessionLocal()
        except Exception as exc:  # noqa: BLE001 — 引擎/池不可用: 拒绝回填, 不外抛
            logger.error(
                "拒绝回填预测后验(id=%s): 无法建立数据库会话(%s)",
                record_id,
                type(exc).__name__,
            )
            return False
    try:
        gate = health if health is not None else _scoring_gate_for_session(db)
        if gate.blocked:
            logger.error(
                "拒绝回填预测后验(id=%s): 评分阻断(%s) —— %s",
                record_id,
                gate.reason_code,
                gate.detail,
            )
            return False
        rec = (
            db.query(AgentPredictionOutcome)
            .filter(AgentPredictionOutcome.id == int(record_id))
            .first()
        )
        if not rec:
            return False
        if not (rec.user_id or "").strip():
            logger.warning(
                "拒绝回填无归属的历史/系统预测(id=%s scope=%r %s/%s/%s h=%s): "
                "无法证明属于哪个账户, 不评估、不改写",
                rec.id,
                rec.owner_scope,
                rec.agent_name,
                rec.stock_symbol,
                rec.prediction_date,
                rec.horizon_days,
            )
            return False
        if str(rec.outcome_status or "") == STATUS_SUPERSEDED:
            logger.warning(
                "拒绝回填审计版本(id=%s %s/%s/%s h=%s): 计分只落在唯一计分行, "
                "回填审计版本会造成同键重复计分",
                rec.id,
                rec.agent_name,
                rec.stock_symbol,
                rec.prediction_date,
                rec.horizon_days,
            )
            return False
        rec.outcome_price = outcome_price
        rec.outcome_return_pct = outcome_return_pct
        rec.outcome_status = status
        rec.evaluated_at = datetime.now()
        db.commit()
        return True
    except OperationalError as e:
        logger.error(
            "回填 prediction outcome 失败(数据库不可用): id=%s err=%s",
            record_id,
            _db_error_kind(e),
        )
        db.rollback()
        return False
    except IntegrityError as e:
        logger.error(
            "回填 prediction outcome 违反完整性约束: id=%s kind=%s",
            record_id,
            _db_error_kind(e),
        )
        db.rollback()
        return False
    except Exception as e:
        logger.warning("更新 prediction outcome 失败: %s", type(e).__name__)
        db.rollback()
        return False
    finally:
        if own_db:
            db.close()


def mark_system_prediction_outcome(
    *,
    record_id: int,
    outcome_price: float,
    outcome_return_pct: float,
    audit_meta: dict,
    expected_run_trace_id: str | None = None,
    db=None,
    health=None,
) -> bool:
    """回填 **system 桶**的成熟对账结果 —— 与个人/历史行硬隔离, 条件更新保证幂等。

    为什么单独一条函数(2026-11-13):
      * `mark_agent_prediction_outcome` 对 `user_id IS NULL` 一律拒绝 —— 这是个人成绩的
        正确闸门, 但 system 桶的 user_id **恒为 NULL**, 于是 system 行永远无法持久化,
        页面只能永远显示 pending。这里补的是\"system 桶自己的回填入口\", 不是放宽那条
        个人闸门: 本函数在 SQL 的 WHERE 里同时要求
        `user_id IS NULL AND owner_scope='system' AND outcome_status='pending'`,
        个人行、注销留档(detached)、历史无归属(双 NULL)都不可能被它命中。

    幂等/并发:
      * 条件更新(`outcome_status='pending'`)只允许 pending → evaluated 一次; 两个进程
        同时读到同一 pending 行时, 只有一个 `rowcount == 1`, 另一个返回 False(已被别人写完),
        不会二次计分、不会覆盖已写结果;
      * 调用方必须传入从待处理行读到的 `expected_run_trace_id`(有值时), 版本被替换过的
        行不会被旧批次误写。

    事务:
      * 本函数**不 commit / 不 rollback** —— 由调用方把整批写入放进一个事务, 失败时统一
        rollback, 避免\"评估了一半\"的假账。数据库异常原样抛出给调用方处理。
      * `health` 可选: 调用方传入批次边界只查一次的评分闸门结论; 不传则现查。闸门阻断
        时拒绝写入(唯一索引不可信时, 无法证明这一行仍是唯一计分行)。
    """
    if db is None:
        raise ValueError("mark_system_prediction_outcome 需要调用方传入会话(批量事务由调用方收口)")

    gate = health if health is not None else _scoring_gate_for_session(db)
    if gate.blocked:
        logger.error(
            "拒绝回填 system 后验(id=%s): 评分阻断(%s) —— %s",
            record_id,
            gate.reason_code,
            gate.detail,
        )
        return False

    rid = int(record_id)
    try:
        price = float(outcome_price)
        ret = float(outcome_return_pct)
    except Exception:  # noqa: BLE001 — 坏数值不写入, 由调用方记为 unknown
        logger.error("拒绝回填 system 后验(id=%s): 非数值结果", record_id)
        return False
    if price <= 0 or not math.isfinite(price):
        logger.error("拒绝回填 system 后验(id=%s): 成交价非法", record_id)
        return False
    if not math.isfinite(ret):
        logger.error("拒绝回填 system 后验(id=%s): 收益率非法", record_id)
        return False

    stmt = (
        update(AgentPredictionOutcome)
        .where(
            AgentPredictionOutcome.id == rid,
            # 硬隔离: 只允许 system 桶的 pending 计分行被写
            AgentPredictionOutcome.user_id.is_(None),
            AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
            AgentPredictionOutcome.outcome_status == "pending",
        )
        .values(
            outcome_price=price,
            outcome_return_pct=ret,
            outcome_status="evaluated",
            evaluated_at=datetime.now(),
            meta=dict(audit_meta or {}),
        )
    )
    trace = str(expected_run_trace_id).strip() if expected_run_trace_id else ""
    if trace:
        stmt = stmt.where(AgentPredictionOutcome.run_trace_id == trace)

    result = db.execute(stmt)
    return int(result.rowcount or 0) == 1


def list_pending_prediction_outcomes(
    *,
    max_horizon_days: int = 10,
    limit: int = 300,
    user_id: str | None = None,
    db=None,
) -> list[AgentPredictionOutcome]:
    """待评估的后验记录 —— **只返回归属可证明的个人计分行**（2026-11-12 三次修正）。

    - 历史行 `user_id IS NULL` 无法证明属于谁 → 不取, 保持 pending(不评估、不冒充成绩),
      条数由 `count_unowned_pending_prediction_outcomes` 单独报出;
    - ghost 行(有 user_id 但 users 里已无此人, 通常是迁移库缺外键 + 用户被清除)→ 不取,
      条数由 `count_ghost_user_prediction_outcomes` 报出(否则等于给一个不存在的账号计分);
    - system 桶(user_id 为空 + owner_scope='system')是独立统计口径, 不并入任何个人成绩
      → 不在这里取, 条数由 `count_system_pending_prediction_outcomes` 报出;
    - 追加的审计版本(status='superseded')不是计分行 → 永不评估, 否则同键重复计分。

    `user_id` 可选: 传了就只取该用户的行(供按用户范围触发的维护评估使用)。
    `db` 可选: 批量评估传入**同一个会话**, 不再为每条查询新建连接。
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        today = date.today().strftime("%Y-%m-%d")
        q = db.query(AgentPredictionOutcome).filter(
            and_(
                AgentPredictionOutcome.outcome_status == "pending",
                AgentPredictionOutcome.horizon_days <= max_horizon_days,
                AgentPredictionOutcome.prediction_date <= today,
                AgentPredictionOutcome.user_id.isnot(None),
                _owner_alive_clause(),
                or_(
                    AgentPredictionOutcome.owner_scope.is_(None),
                    AgentPredictionOutcome.owner_scope == SCOPE_USER,
                ),
            )
        )
        uid = str(user_id).strip() if user_id is not None else ""
        if uid:
            q = q.filter(AgentPredictionOutcome.user_id == uid)
        return (
            q.order_by(
                AgentPredictionOutcome.prediction_date.asc(),
                AgentPredictionOutcome.created_at.asc(),
            )
            .limit(limit)
            .all()
        )
    finally:
        if own_db:
            db.close()


def count_ghost_user_prediction_outcomes(*, max_horizon_days: int = 10, db=None) -> int:
    """user_id 指向**已不存在用户**的待评估行数(ghost) —— 只用于如实报告。

    这些行在迁移库缺外键、用户被物理清除后出现: 有 user_id 可它已经不对应任何账号。
    既不能评估(给谁记成绩?), 也不该被清理顺手删掉(归属问题没查清就消失, 事后无法复核)。
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        today = date.today().strftime("%Y-%m-%d")
        return int(
            db.query(AgentPredictionOutcome)
            .filter(
                AgentPredictionOutcome.outcome_status == "pending",
                AgentPredictionOutcome.horizon_days <= max_horizon_days,
                AgentPredictionOutcome.prediction_date <= today,
                AgentPredictionOutcome.user_id.isnot(None),
                ~_owner_alive_clause(),
            )
            .count()
        )
    finally:
        if own_db:
            db.close()


def count_detached_prediction_outcomes() -> int:
    """账号已注销后留下的行数(user_id 已被置空, owner_scope='user') —— 只报数。

    `ON DELETE SET NULL` 的必然结果(新库外键 / 迁移库触发器 / 清理任务都按这个语义执行):
    行本身保留(历史预测不因账号注销而消失), 但归属不可再证明, 所以不评估、不计入任何成绩。
    与"加列前的历史行"(user_id 与 owner_scope 双 NULL)分开报, 避免把"账号注销"误读成
    "数据清洗过"。
    """
    db = SessionLocal()
    try:
        return int(
            db.query(AgentPredictionOutcome)
            .filter(
                AgentPredictionOutcome.user_id.is_(None),
                AgentPredictionOutcome.owner_scope == SCOPE_USER,
            )
            .count()
        )
    finally:
        db.close()


def count_unowned_pending_prediction_outcomes(*, max_horizon_days: int = 10, db=None) -> int:
    """无归属(pending 且 user_id 为空、owner_scope 也为空)的历史行数 —— 只用于如实报告。"""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        today = date.today().strftime("%Y-%m-%d")
        return int(
            db.query(AgentPredictionOutcome)
            .filter(
                AgentPredictionOutcome.outcome_status == "pending",
                AgentPredictionOutcome.horizon_days <= max_horizon_days,
                AgentPredictionOutcome.prediction_date <= today,
                AgentPredictionOutcome.user_id.is_(None),
                AgentPredictionOutcome.owner_scope.is_(None),
            )
            .count()
        )
    finally:
        if own_db:
            db.close()


def count_system_pending_prediction_outcomes(*, max_horizon_days: int = 10, db=None) -> int:
    """system 桶的待评估行数 —— 独立统计, 从不并入任何个人成绩。"""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        today = date.today().strftime("%Y-%m-%d")
        return int(
            db.query(AgentPredictionOutcome)
            .filter(
                AgentPredictionOutcome.outcome_status == "pending",
                AgentPredictionOutcome.horizon_days <= max_horizon_days,
                AgentPredictionOutcome.prediction_date <= today,
                AgentPredictionOutcome.user_id.is_(None),
                AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
            )
            .count()
        )
    finally:
        if own_db:
            db.close()


def list_agent_prediction_outcomes(
    *,
    agent_name: str | None = None,
    stock_symbol: str | None = None,
    status: str | None = None,
    days: int = 90,
    limit: int = 200,
    user_id: str | None = None,
) -> list[AgentPredictionOutcome]:
    """按用户读取后验记录（不做跨账户读取）。

    `user_id` 是读取范围:**只返回该用户的行**(个人页 / 导出走这条)。没有 user_id 时
    返回空列表并留 WARNING —— 这张表没有任何"全平台成绩"口径是可信的(历史行归属不可
    证明), 所以不提供"不过滤"的读取方式。
    """
    uid = str(user_id).strip() if user_id is not None else ""
    if not uid:
        logger.warning(
            "list_agent_prediction_outcomes 未指定用户 → 返回空(本表不允许跨账户/无归属读取): "
            "agent=%s symbol=%s",
            agent_name,
            stock_symbol,
        )
        return []
    db = SessionLocal()
    try:
        cutoff = (date.today() - timedelta(days=max(days, 1))).strftime("%Y-%m-%d")
        q = db.query(AgentPredictionOutcome).filter(
            AgentPredictionOutcome.prediction_date >= cutoff,
            AgentPredictionOutcome.user_id == uid,
            # uid 对应的用户行必须仍存在: 悬空 id(ghost)的行不呈现给任何人
            _owner_alive_clause(),
        )
        if agent_name:
            q = q.filter(AgentPredictionOutcome.agent_name == agent_name)
        if stock_symbol:
            q = q.filter(AgentPredictionOutcome.stock_symbol == stock_symbol)
        if status:
            q = q.filter(AgentPredictionOutcome.outcome_status == status)
        return (
            q.order_by(
                AgentPredictionOutcome.prediction_date.desc(),
                AgentPredictionOutcome.created_at.desc(),
            )
            .limit(max(1, limit))
            .all()
        )
    finally:
        db.close()


def cleanup_context_data(
    *,
    snapshot_days: int = 180,
    topic_days: int = 180,
    context_run_days: int = 180,
    outcome_days: int = 365,
    dry_run: bool = False,
) -> dict:
    """按保留期清理 context 数据（全局维护操作, 只应由服务端管理员/调度触发）。

    二次修正:
    - `dry_run=True` 只统计将被删除的条数, **不执行任何删除**(维护接口默认走这条路);
    - 后验记录里 `user_id IS NULL AND owner_scope IS NULL` 的**历史未知归属行永不删除**:
      归属不可证明的数据只能保留 unknown, 不能借"清理"顺手洗掉(条数记在
      `legacy_unowned_preserved`, 只统计不删除);
    - 其余各行仍按保留期删除(与旧行为一致), 不修改行内容, 只删过期行。

    三次修正(归属完整性): 另外两类**归属不可证明**的行同样只报数、不删除 ——
      - `ghost_user_preserved`: user_id 指向已不存在的用户(迁移库缺外键 + 用户被清除时出现);
      - `detached_account_preserved`: 账号注销后 user_id 被 SET NULL 的留档(owner_scope='user')。
    让它们随保留期一起消失, 等于把"归属问题"当"过期数据"处理掉, 事后无从复核。
    """
    db = SessionLocal()
    deleted = {
        "stock_context_snapshots": 0,
        "news_topic_snapshots": 0,
        "agent_context_runs": 0,
        "agent_prediction_outcomes": 0,
        "legacy_unowned_preserved": 0,
        "ghost_user_preserved": 0,
        "detached_account_preserved": 0,
        "dry_run": bool(dry_run),
    }
    try:
        snapshot_cutoff = (
            date.today() - timedelta(days=max(1, int(snapshot_days)))
        ).strftime("%Y-%m-%d")
        topic_cutoff = (
            date.today() - timedelta(days=max(1, int(topic_days)))
        ).strftime("%Y-%m-%d")
        context_run_cutoff = (
            date.today() - timedelta(days=max(1, int(context_run_days)))
        ).strftime("%Y-%m-%d")
        outcome_cutoff = (
            date.today() - timedelta(days=max(1, int(outcome_days)))
        ).strftime("%Y-%m-%d")

        # 历史未知归属行: 永不清理, 只如实报数
        deleted["legacy_unowned_preserved"] = int(
            db.query(AgentPredictionOutcome)
            .filter(
                AgentPredictionOutcome.prediction_date < outcome_cutoff,
                AgentPredictionOutcome.user_id.is_(None),
                AgentPredictionOutcome.owner_scope.is_(None),
            )
            .count()
        )
        # 归属不可证明的另两类: 同样保留(不删), 分别报数
        deleted["ghost_user_preserved"] = int(
            db.query(AgentPredictionOutcome)
            .filter(
                AgentPredictionOutcome.prediction_date < outcome_cutoff,
                AgentPredictionOutcome.user_id.isnot(None),
                ~_owner_alive_clause(),
            )
            .count()
        )
        deleted["detached_account_preserved"] = int(
            db.query(AgentPredictionOutcome)
            .filter(
                AgentPredictionOutcome.prediction_date < outcome_cutoff,
                AgentPredictionOutcome.user_id.is_(None),
                AgentPredictionOutcome.owner_scope == SCOPE_USER,
            )
            .count()
        )

        # 可删范围: 归属用户仍在的行(按保留期) + system 桶(审计留档, 有界增长)
        prunable = or_(
            and_(
                AgentPredictionOutcome.user_id.isnot(None),
                _owner_alive_clause(),
            ),
            AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
        )

        if dry_run:
            deleted["stock_context_snapshots"] = int(
                db.query(StockContextSnapshot)
                .filter(StockContextSnapshot.snapshot_date < snapshot_cutoff)
                .count()
            )
            deleted["news_topic_snapshots"] = int(
                db.query(NewsTopicSnapshot)
                .filter(NewsTopicSnapshot.snapshot_date < topic_cutoff)
                .count()
            )
            deleted["agent_context_runs"] = int(
                db.query(AgentContextRun)
                .filter(AgentContextRun.analysis_date < context_run_cutoff)
                .count()
            )
            deleted["agent_prediction_outcomes"] = int(
                db.query(AgentPredictionOutcome)
                .filter(
                    AgentPredictionOutcome.prediction_date < outcome_cutoff,
                    prunable,
                )
                .count()
            )
            return deleted

        deleted["stock_context_snapshots"] = (
            db.query(StockContextSnapshot)
            .filter(StockContextSnapshot.snapshot_date < snapshot_cutoff)
            .delete(synchronize_session=False)
        )
        deleted["news_topic_snapshots"] = (
            db.query(NewsTopicSnapshot)
            .filter(NewsTopicSnapshot.snapshot_date < topic_cutoff)
            .delete(synchronize_session=False)
        )
        deleted["agent_context_runs"] = (
            db.query(AgentContextRun)
            .filter(AgentContextRun.analysis_date < context_run_cutoff)
            .delete(synchronize_session=False)
        )
        deleted["agent_prediction_outcomes"] = (
            db.query(AgentPredictionOutcome)
            .filter(
                AgentPredictionOutcome.prediction_date < outcome_cutoff,
                prunable,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return deleted
    except Exception as e:
        logger.warning("清理 context 数据失败: %s", type(e).__name__)
        db.rollback()
        return deleted
    finally:
        db.close()
