"""题材演化 agent 的 SIDA 调度注册适配器(独立接线层)。

本模块只做一件事: 把**已有的** `src.core.theme_evolution_agent.run_once` 挂成 SIDA 内
APScheduler 的一个定时作业。它自己不抓数、不建库、不跑迁移、不接启动流程、不发消息。

为什么单独一层
--------------
`run_once` 的契约是"一次运行、依赖全部显式注入"(`theme_evolution_agent` 的边界声明
里也是这么写的: 不做调度、不做重试)。调度是产品侧的接缝, 把它放进 agent 会让
agent 被迫知道 APScheduler/开关/进程锁; 放进启动流程又会让"默认关闭"变成一句空话。
所以这里是一个**独立适配器**: 只有产品侧显式 `register()` 才会挂上作业。

硬约束(每一条都有测试断言)
--------------------------
1. 默认关闭, 开关只认环境变量 `SIDA_THEME_EVOLUTION_AGENT` 的值**恰好等于** ``"1"``
   (允许首尾空白)。缺省、``"0"``、``"true"``、``"yes"``、空串一律不注册任何作业;
   注册之后开关被改回非 ``"1"``, 已挂上的作业触发时同样不运行。
2. 依赖只有三样且必须显式给: `engine` + `deps`(`run_once` 的取数注入)+ `upstream_ready`
   回调。缺任何一样**都不运行**, 原因记成 unknown; 模块内没有任何默认取数实现,
   因此在任何环境(含本机/CI/生产)"不注入就不联外"。
3. 上游 `ready` 必须是**布尔真值** ``is True``(``1``/``"true"`` 这类 truthy 不算证据),
   且上游报的交易日必须是合法 ``YYYYMMDD`` **并且等于时钟当日**。任一条件拿不到肯定
   证据 → 本轮不运行, 状态 ``skipped``, 未知项写清楚。绝不"用旧日期硬跑一轮"。
4. 锁忙 / 失败不许变成成功: 进程内锁拿不到 → ``skipped``(不是 completed); agent 返回
   refused/partial → 对应状态且 ``ok is False``; agent 抛异常 → ``failed`` 且只留异常
   类型名(消息里可能有 URL/token, 不落原文)。只有 agent 自己 ``ok is True``
   (批次 complete 且按 run_id 读回核对全过)才算 ``completed``。
5. 不建表: 本模块不建引擎、不读数据库配置、不执行迁移、不创建数据表; 库里没有表时
   这一轮只会失败(结构化上报), 不会顺手把表建出来。
6. 多 worker 不声称安全: 这里的 `threading.Lock` 与作业参数 ``max_instances=1``
   都**只在单进程内**有效。多 worker/多副本部署下它们不构成跨进程互斥, 本层输出
   固定 ``cross_process_safe=False``, 跨进程去重不由本层负责(库层自身的锁语义另算)。
7. 无交易、无外发: 结果不含方向/收益/目标/仓位字段, 不生成报告、不发任何对外消息;
   失败只回结构化结果, 由调用方决定是否重新发起。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from src.core import theme_evolution_agent as agent
from src.core import theme_evolution_capture as cap

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TIMEZONE",
    "ENV_FLAG",
    "JOB_ID",
    "LOCK_SCOPE",
    "Registration",
    "REASON_AGENT_EXCEPTION",
    "REASON_AGENT_INVALID_ARGUMENT",
    "REASON_AGENT_PARTIAL",
    "REASON_AGENT_REFUSED",
    "REASON_ALREADY_REGISTERED",
    "REASON_DEPS_MISSING",
    "REASON_FLAG_OFF",
    "REASON_LOCK_BUSY",
    "REASON_UPSTREAM_DAY_MISMATCH",
    "UNKNOWN_UPSTREAM_DAY_MISMATCH",
    "REASON_UPSTREAM_DAY_UNKNOWN",
    "REASON_UPSTREAM_ERROR",
    "REASON_UPSTREAM_MISSING",
    "REASON_UPSTREAM_NOT_READY",
    "REQUIRED_DEPS",
    "RUN_HOUR",
    "RUN_MINUTE",
    "SCHEDULER_LIMITATIONS",
    "SCHEDULER_NAME",
    "SchedulerRun",
    "STATUS_COMPLETED",
    "STATUS_DISABLED",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_REFUSED",
    "STATUS_SKIPPED",
    "ThemeEvolutionScheduler",
    "is_enabled",
]

_CST = ZoneInfo("Asia/Shanghai")

#: enable 开关: 只有值恰好为 "1" 才算打开(其余一律关闭)。
ENV_FLAG = "SIDA_THEME_EVOLUTION_AGENT"

#: 作业标识(重复注册靠它去重)。
JOB_ID = "theme_evolution_agent_daily"
SCHEDULER_NAME = "theme_evolution_agent"

#: 锁的作用域说明 —— 只在本进程内有效, 不是跨进程互斥。
LOCK_SCOPE = "process_local"

DEFAULT_TIMEZONE = "Asia/Shanghai"

#: 触发时刻: 16:10。**不是"上游已定稿"的证明** —— 当日涨停池回填在 15:35 与
#: 16:05 自检之后, 15:20 跑必然读到未定稿数据; 16:10 只是给回填留出窗口。
RUN_HOUR = 16
RUN_MINUTE = 10
#: 进程在触发点前后重启时, APScheduler 默认 ~1s 宽限会把本次作业直接丢掉且无记录;
#: 给 30 分钟补跑窗口, 迟到的那次仍写真实 captured_at。
MISFIRE_GRACE_S = 1800

#: 默认采集路径的最小取数依赖。注入完整 `capture` 替换实现时, 这一组不再强制
#: (两者都没有 = 没有数据来源, 提前拦下来并记 unknown)。
REQUIRED_DEPS = ("fetch_boards", "fetch_members")

#: 由适配器自己决定的 run_once 参数, 注入的 deps 不许覆盖。
_RESERVED_DEPS = ("now", "bind", "day", "slot")

#: 一次调度的结论。
STATUS_DISABLED = "disabled"
STATUS_SKIPPED = "skipped"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_REFUSED = "refused"
STATUS_FAILED = "failed"

#: 结构性原因(稳定字符串; 不含凭据/URL/异常消息)。
REASON_FLAG_OFF = "flag_not_explicit_1"
REASON_ALREADY_REGISTERED = "already_registered"
REASON_DEPS_MISSING = "dependencies_not_injected"
REASON_UPSTREAM_MISSING = "upstream_callback_not_injected"
REASON_UPSTREAM_NOT_READY = "upstream_not_ready"
REASON_UPSTREAM_DAY_UNKNOWN = "upstream_day_unknown"
REASON_UPSTREAM_DAY_MISMATCH = "upstream_day_mismatch"
REASON_UPSTREAM_ERROR = "upstream_check_error"
REASON_LOCK_BUSY = "run_lock_busy"
REASON_AGENT_REFUSED = "agent_refused"
REASON_AGENT_PARTIAL = "agent_partial"
REASON_AGENT_EXCEPTION = "agent_exception"
REASON_AGENT_INVALID_ARGUMENT = "agent_invalid_argument"

#: 未知项标记(unknown 不是 0, 也不是 false: 是"没拿到肯定证据")。
UNKNOWN_UPSTREAM_STATE = "upstream_state_unknown"
UNKNOWN_UPSTREAM_DAY = "upstream_day_unknown"
UNKNOWN_UPSTREAM_DAY_MISMATCH = "upstream_day_mismatch"
UNKNOWN_UPSTREAM_ERROR = "upstream_error"
UNKNOWN_UPSTREAM_MISSING = "upstream_not_injected"
UNKNOWN_ENGINE_MISSING = "engine_not_injected"
UNKNOWN_DEPS_MISSING = "deps_not_injected"
UNKNOWN_LOCK_BUSY = "run_lock_busy"
UNKNOWN_AGENT_EXCEPTION = "agent_exception"
UNKNOWN_AGENT_INVALID = "agent_invalid_argument"
UNKNOWN_NO_RUN = "no_run_attempted"
UNKNOWN_NO_SUCCESS = "no_success_evidence"

#: 每次结果都会带上的边界声明(供运维/报告直接引用)。
SCHEDULER_LIMITATIONS = (
    "本适配器只把作业挂进调用方给的 APScheduler: 不启动进程、不重启服务、不执行迁移、不建表。",
    "enable 开关只认环境变量值恰好为 \"1\"; 缺省或其它取值一律不注册作业, 任何环境都不会因此联外。",
    "取数依赖与库都必须显式注入; 本层没有默认取数实现, 不注入就一轮都不发。",
    "上游 ready 必须是布尔真值 is True, 且上游交易日必须等于时钟当日; 否则不运行并记 unknown。",
    "进程锁与 max_instances=1 都只在单进程内生效: 多 worker/多副本部署下不构成跨进程互斥, "
    "本层不声称跨进程安全。",
    "不生成报告、不发任何对外消息、不接启动流程: 失败只回结构化结果, 由调用方重新发起。",
    "不产生任何方向、收益、目标或仓位字段, 不执行交易动作, 不构成投资建议。",
)

_DAY_RE = re.compile(r"^\d{8}$")


# ── 开关 ────────────────────────────────────────────────────────────────
def is_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """开关是否打开: 只认值 ``"1"``(去首尾空白), 其余一律关闭。"""
    env = os.environ if environ is None else environ
    return str(env.get(ENV_FLAG) or "").strip() == "1"


# ── 输出模型(纯数据, 不可变) ────────────────────────────────────────────
@dataclass(frozen=True)
class SchedulerRun:
    """一次调度触发的结论。

    `ok` 只在 agent 自己 `ok is True` 时为 True; 锁忙/上游缺证据/失败一律 False。
    `unknowns` 只放"没拿到肯定证据"的标记, 不写 0/空值。
    """

    status: str
    ok: bool
    reason: str | None
    day: str | None
    run_id: int | None
    agent_outcome: str | None = None
    verified: bool | None = None
    replayed: bool = False
    trigger_at: datetime | None = None
    unknowns: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    error: str | None = None
    schema_version: str = field(default=agent.AGENT_RULE_VERSION)

    def to_dict(self) -> dict[str, Any]:
        """JSON 安全的审计视图。刻意不含任何方向/收益/目标/仓位字段。"""
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "ok": self.ok,
            "reason": self.reason,
            "day": self.day,
            "run_id": self.run_id,
            "agent_outcome": self.agent_outcome,
            "verified": self.verified,
            "replayed": self.replayed,
            "trigger_at": _iso(self.trigger_at),
            "timezone": DEFAULT_TIMEZONE,
            "lock_scope": LOCK_SCOPE,
            "cross_process_safe": False,
            "unknowns": list(self.unknowns),
            "notes": list(self.notes),
            "error": self.error,
            "contains_trading_advice": False,
            "limitations": list(SCHEDULER_LIMITATIONS),
        }


@dataclass(frozen=True)
class Registration:
    """注册结果。`registered=False` 时 `reason` 说清为什么(开关关/已注册过)。"""

    registered: bool
    job_id: str | None
    reason: str | None
    scheduler_name: str = SCHEDULER_NAME
    lock_scope: str = LOCK_SCOPE
    cross_process_safe: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "registered": self.registered,
            "job_id": self.job_id,
            "reason": self.reason,
            "scheduler_name": self.scheduler_name,
            "lock_scope": self.lock_scope,
            "cross_process_safe": False,
            "notes": list(self.notes),
            "limitations": list(SCHEDULER_LIMITATIONS),
        }


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment is not None else None


def _as_beijing(moment: Any, name: str = "now") -> datetime:
    if not isinstance(moment, datetime):
        raise ValueError(f"{name} 必须是 datetime")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_CST)
    return moment.astimezone(_CST)


def _field(state: Any, name: str) -> Any:
    """上游状态既可以是 Mapping, 也可以是带属性的对象。"""
    if isinstance(state, Mapping):
        return state.get(name)
    return getattr(state, name, None)


def _valid_day(value: Any) -> str | None:
    """只接受合法 ``YYYYMMDD``; 其它(含 int/连字符/不存在的日期)一律 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _DAY_RE.fullmatch(text):
        return None
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return text


# ── 适配器本体 ──────────────────────────────────────────────────────────
class ThemeEvolutionScheduler:
    """把 `agent.run_once` 注册成 SIDA APScheduler 作业的薄适配器。

    参数(全部显式, 没有默认取数实现):
    - `engine`: 调用方准备的 Engine/Connection(**本层不建库、不建表**);
    - `deps`: 传给 `run_once` 的取数注入(至少 `fetch_boards` / `fetch_members`);
    - `upstream_ready`: 上游就绪回调, 签名 ``(now) -> {"ready": bool, "day": "YYYYMMDD"}``;
    - `scheduler`: 可注入的 APScheduler(缺省时自己 new 一个 BackgroundScheduler,
      **只挂作业不启动**);
    - `now`: 时钟注入(测试用模拟时钟);
    - `slot`: 采集槽位, 缺省与 capture 一致。
    """

    def __init__(
        self,
        *,
        engine: Any = None,
        deps: Mapping[str, Any] | None = None,
        upstream_ready: Callable[[datetime], Any] | None = None,
        scheduler: Any = None,
        timezone: str = DEFAULT_TIMEZONE,
        now: Callable[[], datetime] | None = None,
        slot: str = cap.DEFAULT_SLOT,
    ) -> None:
        injected = dict(deps or {})
        shadowed = [key for key in _RESERVED_DEPS if key in injected]
        if shadowed:
            raise ValueError(f"deps 不许覆盖由适配器决定的参数: {sorted(shadowed)}")
        self.engine = engine
        self.deps: dict[str, Any] = injected
        self.upstream_ready = upstream_ready
        self.timezone = timezone
        self.slot = slot
        self._now = now if callable(now) else (lambda: datetime.now(_CST))
        self.scheduler = scheduler if scheduler is not None else BackgroundScheduler(timezone=timezone)
        # 进程内互斥: 与 max_instances=1 一样只在单进程有效, 不是跨进程锁。
        self._lock = threading.Lock()

    # ── 注册 ────────────────────────────────────────────────────────────
    def register(self) -> Registration:
        """按开关把作业挂进 APScheduler。默认关闭 → 一个作业都不挂。"""
        if not is_enabled():
            return Registration(registered=False, job_id=None, reason=REASON_FLAG_OFF,
                                notes=("开关未显式为 1: 不注册",))
        if self.scheduler.get_job(JOB_ID) is not None:
            # 重复注册(同进程二次接线/另一个实例共用同一个 APScheduler)不堆第二个作业。
            return Registration(registered=False, job_id=JOB_ID,
                                reason=REASON_ALREADY_REGISTERED,
                                notes=("同一作业已存在: 保持单个 job",))
        self.scheduler.add_job(
            self._job,
            "cron",
            day_of_week="mon-fri",
            hour=RUN_HOUR,
            minute=RUN_MINUTE,
            id=JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=MISFIRE_GRACE_S,
        )
        logger.info("[theme-evo-agent] 已注册作业 %s (工作日 %02d:%02d, 默认不启动)",
                    JOB_ID, RUN_HOUR, RUN_MINUTE)
        return Registration(registered=True, job_id=JOB_ID, reason=None,
                            notes=("注册成功: 是否 start() 由产品侧决定",))

    # ── 作业入口(APScheduler 调用时不传参) ─────────────────────────────
    def _job(self) -> dict[str, Any]:
        try:
            result = self.run_now()
            # APScheduler 不保证作业返回值被应用持久化；仅记录安全摘要，不记录原始数据。
            known_reasons = {value for name, value in globals().items()
                             if name.startswith("REASON_") and isinstance(value, str)}
            safe_reason = result.reason if result.reason in known_reasons else "unknown"
            logger.info("[theme-evo-agent] status=%s reason=%s day=%s run_id=%s",
                        result.status, safe_reason, result.day, result.run_id)
            return result.to_dict()
        except Exception as exc:  # noqa: BLE001 —— 作业异常不许掀翻调度器
            logger.warning("[theme-evo-agent] 作业异常: %s", type(exc).__name__)
            return SchedulerRun(
                status=STATUS_FAILED, ok=False, reason=REASON_AGENT_EXCEPTION,
                day=None, run_id=None,
                unknowns=(UNKNOWN_AGENT_EXCEPTION, UNKNOWN_NO_RUN),
                error=type(exc).__name__,
            ).to_dict()

    # ── 一次调度 ────────────────────────────────────────────────────────
    def run_now(self, now: datetime | None = None) -> SchedulerRun:
        """触发一次: 开关 → 依赖 → 锁 → 上游证据 → agent.run_once。

        任何一步拿不到肯定证据都**不运行**并返回结构化结论; 不抛异常(入参非法除外)。
        """
        if not is_enabled():
            return SchedulerRun(status=STATUS_DISABLED, ok=False, reason=REASON_FLAG_OFF,
                                day=None, run_id=None, unknowns=(UNKNOWN_NO_RUN,))

        gaps, upstream_missing = self._dependency_gaps()
        if gaps:
            return SchedulerRun(status=STATUS_REFUSED, ok=False, reason=REASON_DEPS_MISSING,
                                day=None, run_id=None,
                                unknowns=tuple(gaps) + (UNKNOWN_NO_RUN,))
        if upstream_missing:
            return SchedulerRun(status=STATUS_REFUSED, ok=False,
                                reason=REASON_UPSTREAM_MISSING,
                                day=None, run_id=None,
                                unknowns=(UNKNOWN_UPSTREAM_MISSING, UNKNOWN_NO_RUN))

        trigger = _as_beijing(self._now() if now is None else now)

        if not self._lock.acquire(blocking=False):
            # 上一轮还在跑: 跳过, 不排队、不冒充成功。
            logger.warning("[theme-evo-agent] 上一轮未结束, 跳过本次触发")
            return SchedulerRun(status=STATUS_SKIPPED, ok=False, reason=REASON_LOCK_BUSY,
                                day=None, run_id=None, trigger_at=trigger,
                                unknowns=(UNKNOWN_LOCK_BUSY, UNKNOWN_NO_RUN))
        try:
            return self._run_guarded(trigger)
        finally:
            self._lock.release()

    # ── 内部: 依赖与上游 ────────────────────────────────────────────────
    def _dependency_gaps(self) -> tuple[list[str], bool]:
        """返回 (库/取数缺口标记, 上游回调是否缺失)。缺口不补, 只据实上报。"""
        gaps: list[str] = []
        if self.engine is None:
            gaps.append(UNKNOWN_ENGINE_MISSING)
        if not self._has_source_deps():
            gaps.append(UNKNOWN_DEPS_MISSING)
        return gaps, not callable(self.upstream_ready)

    def _has_source_deps(self) -> bool:
        """有数据来源吗: 显式 `capture` 替换实现, 或默认路径的取数函数二选一。"""
        if callable(self.deps.get("capture")):
            return True
        return all(callable(self.deps.get(key)) for key in REQUIRED_DEPS)

    def _run_guarded(self, trigger: datetime) -> SchedulerRun:
        expected_day = trigger.strftime("%Y%m%d")
        upstream = self.upstream_ready
        if not callable(upstream):  # run_now 已拦过; 这里只保证不会 None() 崩掉
            return SchedulerRun(status=STATUS_REFUSED, ok=False, reason=REASON_UPSTREAM_MISSING,
                                day=None, run_id=None, trigger_at=trigger,
                                unknowns=(UNKNOWN_UPSTREAM_MISSING, UNKNOWN_NO_RUN))
        try:
            state = upstream(trigger)
        except Exception as exc:  # noqa: BLE001 —— 只留类型名, 不落消息原文
            return SchedulerRun(status=STATUS_SKIPPED, ok=False, reason=REASON_UPSTREAM_ERROR,
                                day=None, run_id=None, trigger_at=trigger,
                                unknowns=(UNKNOWN_UPSTREAM_ERROR, UNKNOWN_NO_RUN),
                                error=type(exc).__name__)

        ready = _field(state, "ready")
        if ready is not True:
            # truthy(1/"true")不是证据: 只要不是布尔 True 就不运行。
            return SchedulerRun(status=STATUS_SKIPPED, ok=False, reason=REASON_UPSTREAM_NOT_READY,
                                day=_valid_day(_field(state, "day")), run_id=None,
                                trigger_at=trigger,
                                unknowns=(UNKNOWN_UPSTREAM_STATE, UNKNOWN_NO_RUN))

        day = _valid_day(_field(state, "day"))
        if day is None:
            return SchedulerRun(status=STATUS_SKIPPED, ok=False, reason=REASON_UPSTREAM_DAY_UNKNOWN,
                                day=None, run_id=None, trigger_at=trigger,
                                unknowns=(UNKNOWN_UPSTREAM_DAY, UNKNOWN_NO_RUN))
        if day != expected_day:
            # 上游还停在上一交易日(或时钟与上游不同步): 不许拿旧日期硬跑。
            return SchedulerRun(status=STATUS_SKIPPED, ok=False, reason=REASON_UPSTREAM_DAY_MISMATCH,
                                day=day, run_id=None, trigger_at=trigger,
                                unknowns=(UNKNOWN_UPSTREAM_DAY_MISMATCH, UNKNOWN_NO_RUN))

        return self._invoke_agent(trigger=trigger, day=day)

    def _invoke_agent(self, *, trigger: datetime, day: str) -> SchedulerRun:
        try:
            out = agent.run_once(bind=self.engine, now=trigger, day=day,
                                 slot=self.slot, **self.deps)
        except ValueError as exc:
            # 入参非法(slot/时钟): 结构化失败, 不说成"跑过了"。
            return SchedulerRun(status=STATUS_FAILED, ok=False, reason=REASON_AGENT_INVALID_ARGUMENT,
                                day=day, run_id=None, trigger_at=trigger,
                                unknowns=(UNKNOWN_AGENT_INVALID, UNKNOWN_NO_RUN),
                                error=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 —— 只留类型名
            logger.warning("[theme-evo-agent] agent 异常: %s", type(exc).__name__)
            return SchedulerRun(status=STATUS_FAILED, ok=False, reason=REASON_AGENT_EXCEPTION,
                                day=day, run_id=None, trigger_at=trigger,
                                unknowns=(UNKNOWN_AGENT_EXCEPTION, UNKNOWN_NO_RUN),
                                error=type(exc).__name__)

        base: dict[str, Any] = dict(
            day=out.day,
            run_id=out.run_id,
            agent_outcome=out.outcome,
            verified=out.verified,
            replayed=out.replayed,
            trigger_at=trigger,
            notes=tuple(out.notes),
        )
        if out.ok is True:
            return SchedulerRun(status=STATUS_COMPLETED, ok=True, reason=None,
                                unknowns=tuple(out.unknowns), **base)
        if out.outcome == agent.OUTCOME_REFUSED:
            # 有落库证据但批次被标 refused / 或采集拒绝: 一律不算成功。
            return SchedulerRun(status=STATUS_REFUSED, ok=False, reason=REASON_AGENT_REFUSED,
                                unknowns=_with_marker(out.unknowns, UNKNOWN_NO_SUCCESS),
                                error=out.reason, **base)
        if out.outcome == agent.OUTCOME_PARTIAL:
            return SchedulerRun(status=STATUS_PARTIAL, ok=False, reason=REASON_AGENT_PARTIAL,
                                unknowns=_with_marker(out.unknowns, UNKNOWN_NO_SUCCESS),
                                error=out.reason, **base)
        return SchedulerRun(status=STATUS_FAILED, ok=False,
                            reason=out.reason or REASON_AGENT_EXCEPTION,
                            unknowns=_with_marker(out.unknowns, UNKNOWN_NO_SUCCESS),
                            error=out.reason, **base)


def _with_marker(unknowns: tuple[str, ...], marker: str) -> tuple[str, ...]:
    return unknowns if marker in unknowns else unknowns + (marker,)
