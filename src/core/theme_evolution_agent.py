"""题材演化 agent: **产品内定时 agent 的一次运行**编排(依赖注入, 默认不联网)。

## 这个模块做什么

把已有三块拼成一次**可追责的运行**, 自己不做任何别的事:

1. 用调用方注入的取数函数调用 `theme_evolution_capture.capture_day` —— 落库的是
   **同一个批次**(同一次 attempt), 本层不新建批次、不补采、不重试;
2. 拿该批次的 `run_id` 去 `theme_evolution_store.evidence_for_run` **读回并核对**:
   run 归属、日期、run_key、状态、板块/涨停池行数、行内 run_id 与日期是否全部指向
   本次 run。任何一项不符就**整份证据作废**(`evidence=None`), 绝不把别的批次的
   行拼进来, 也绝不把"残留的几行"当成该批次;
3. 把调用方**显式注入**的事后标签事件与日K线交给 `theme_evolution_timeline`;
   没注入就是 `unknown`, 注入但不合法就是 `invalid`(带稳定 code), 两者都不产出
   时间线, 也不去任何地方"补"数据。

## 硬约束(违反即视为 bug)

1. **默认不联网**: 取数依赖全部由调用方注入; 不注入时 capture 结构化拒绝, 本层
   如实报 `refused`, 一次外部调用都不发。模块 import 期不拉行情/HTTP 客户端。
2. **不自建库**: 库只来自调用方显式传入的 Engine/Connection。本模块不解析配置、
   不建引擎、不建表、不改注册与迁移流程 —— 它只调用别人已经准备好的东西。
3. **时间显式**: `now` 必填; 模块内不读系统时钟。`generated_at` 未显式提供时标未知，不能拿采集时刻冒充生成时刻。
4. **拒绝 ≠ 成功**: 采集拒绝(依赖缺失/源不新鲜/目标日错配)、采集异常、落库拒绝
   (attempt 上限)、读回不符 —— 一律 `ok is False`; `ok` 只在"批次状态 complete
   且读回核对全过"时为真。
5. **只认一次 run**: 证据按 `run_id` 取, 行内 `run_id`/`day` 必须一致; 串批次、
   缺行、查无此 run、refused 却带证据行 —— 全部按失败处理, 且证据作废。
6. **未知就是 unknown**: 池子未知 ⇒ 计数 `None`(不是 0); 板块取不到 ⇒ 成员
   `None`; 时间线输入缺失 ⇒ `timeline=None` + `state=unknown`; 全部进 `unknowns`。
7. **无交易语义**: 输出没有方向/收益/目标/仓位字段, 也没有任何仓位买卖措辞;
   `contains_trading_advice` 恒为 False。

## 边界 / 未覆盖(随每次结果一起返回)

见 `AGENT_LIMITATIONS`; 简言之: **不做调度、不做重试、不做告警**; 证据只覆盖本次
run; 时间线的原始文件/摘要/证券身份由调用方声明, 本层不核验; 事后标签的时点
可回放性未证实; 未接产品注册与迁移流程, 启用仍需独立接线与验收。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Connection, Engine

from src.core import theme_evolution_capture as cap
from src.core import theme_evolution_timeline as tl

__all__ = [
    "AGENT_LIMITATIONS",
    "AGENT_RULE_VERSION",
    "AgentRun",
    "GENERATED_AT_EXPLICIT",
    "GENERATED_AT_FROM_NOW",
    "NOTE_REPLAYED",
    "OUTCOME_COMPLETE",
    "OUTCOME_FAILED",
    "OUTCOME_PARTIAL",
    "OUTCOME_REFUSED",
    "REASON_BIND_MISSING",
    "REASON_CAPTURE_EXCEPTION",
    "REASON_CAPTURE_NO_RESULT",
    "REASON_CAPTURE_REFUSED",
    "REASON_READBACK_MISMATCH",
    "REASON_TIMELINE_INPUTS_MISSING",
    "REASON_TIMELINE_NO_BATCH",
    "TIMELINE_INVALID",
    "TIMELINE_OK",
    "TIMELINE_SKIPPED",
    "TIMELINE_UNKNOWN",
    "VERIFY_BOARDS_COUNT_MISMATCH",
    "VERIFY_DAY_MISMATCH",
    "VERIFY_FOREIGN_ROWS",
    "VERIFY_LIMIT_UP_COUNT_MISMATCH",
    "VERIFY_POOL_COUNT_NOT_NULL",
    "VERIFY_POOL_STATE_MISMATCH",
    "VERIFY_READBACK_NOT_A_MAPPING",
    "VERIFY_READ_EXCEPTION",
    "VERIFY_REFUSED_WITH_EVIDENCE",
    "VERIFY_RUN_ID_MISMATCH",
    "VERIFY_RUN_KEY_MISMATCH",
    "VERIFY_RUN_MISSING",
    "VERIFY_RUN_NOT_FOUND",
    "VERIFY_STATUS_MISMATCH",
    "run_once",
]

_CST = ZoneInfo("Asia/Shanghai")

#: 本层的规则版本(与 capture / store / timeline 各自版本并列, 不互相顶替)。
AGENT_RULE_VERSION = "theme_evo_agent_v1"

#: 一次运行的结论。refused = 本轮没有写入任何证据; failed = 没跑成或核对不过。
OUTCOME_COMPLETE = "complete"
OUTCOME_PARTIAL = "partial"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"

#: 时间线状态。unknown = 没注入输入; invalid = 注入但不可用; skipped = 没有可附的批次。
TIMELINE_OK = "ok"
TIMELINE_UNKNOWN = "unknown"
TIMELINE_INVALID = "invalid"
TIMELINE_SKIPPED = "skipped"

#: 生成时刻的来源标记: 显式传入 / 取自本次 now。
GENERATED_AT_EXPLICIT = "explicit"
GENERATED_AT_FROM_NOW = "same_as_now"

#: 运行级原因(结构性, 不含凭据/URL/异常消息)。
REASON_BIND_MISSING = "bind_not_injected"
REASON_CAPTURE_EXCEPTION = "capture_exception"
REASON_CAPTURE_NO_RESULT = "capture_no_result"
REASON_CAPTURE_REFUSED = "capture_refused"
REASON_READBACK_MISMATCH = "readback_mismatch"
REASON_TIMELINE_INPUTS_MISSING = "timeline_inputs_not_injected"
REASON_TIMELINE_NO_BATCH = "batch_not_readable"

#: 读回核对失败码(稳定字符串, 便于断言与日志聚合)。
VERIFY_RUN_NOT_FOUND = "run_not_found"
VERIFY_READ_EXCEPTION = "read_exception"
VERIFY_READBACK_NOT_A_MAPPING = "readback_not_a_mapping"
VERIFY_RUN_MISSING = "run_row_missing"
VERIFY_RUN_ID_MISMATCH = "run_id_mismatch"
VERIFY_RUN_KEY_MISMATCH = "run_key_mismatch"
VERIFY_DAY_MISMATCH = "day_mismatch"
VERIFY_CAPTURED_AT_MISMATCH = "captured_at_mismatch"
VERIFY_SOURCE_FLAG_MISMATCH = "source_fresh_mismatch"
VERIFY_SOURCE_TRADE_DATE_MISMATCH = "source_trade_date_mismatch"
VERIFY_STATUS_MISMATCH = "status_mismatch"
VERIFY_BOARDS_COUNT_MISMATCH = "boards_count_mismatch"
VERIFY_LIMIT_UP_COUNT_MISMATCH = "limit_up_count_mismatch"
VERIFY_FOREIGN_ROWS = "foreign_rows_in_evidence"
VERIFY_REFUSED_WITH_EVIDENCE = "refused_run_carries_evidence"
VERIFY_POOL_STATE_MISMATCH = "pool_state_mismatch"
VERIFY_POOL_COUNT_NOT_NULL = "pool_count_not_null_for_unknown_pool"

#: 说明性 note。
NOTE_REPLAYED = "replay_no_new_evidence"

#: 每次结果都会带上的边界声明(供报告直接引用)。
AGENT_LIMITATIONS = (
    "本层只编排一次运行: 不做调度、不做重试、不做告警; 失败只能由调用方重新发起。",
    "证据只来自本次 run_id 的明细: 不跨批次拼接、不借用更早的 attempt、不跨日拼证据。",
    "时间线输入(事后标签事件与日K线)由调用方显式注入; 本层不读任何行情源, "
    "不核验其原始文件、引用摘要与证券身份，也未证明时间线与采集批次关联。",
    "涨停原因标签是供应商事后整理的字段, 时点可回放性未证实: 输出固定标 "
    "point_in_time_verified=false, 不可用于盘中回放。",
    "未知一律保留为 unknown/None(不是 0): 池子未知不等于 0 只涨停, 板块取不到不等于空集。",
    "本层不产生方向、收益或目标字段, 也不做持有与否的判断, 不构成投资建议, "
    "不执行任何交易动作。",
    "未接产品注册与迁移流程: 本模块不自建库、不改迁移与启动流程, "
    "产品内启用仍需独立接线与验收。",
)

_DAY_RE_LEN = 8
_MAX_NOTES = 64


# ── 输出模型(纯数据, 不可变) ───────────────────────────────────────────
@dataclass(frozen=True)
class AgentRun:
    """一次 agent 运行的完整结论。

    `ok` 只在「批次状态 complete **且** 读回核对全过」时为 True;
    `verified` 为 None 表示本轮没有可核对的写入(如落库被拒)。
    `evidence` 只含本次 `run_id` 的明细; 核对不通过时一律为 None。
    """

    outcome: str
    ok: bool
    reason: str | None
    run_id: int | None
    attempt_seq: int | None
    inserted: bool | None
    replayed: bool
    day: str
    run_key: str
    slot: str
    source: str | None
    rule_version: str | None
    captured_at: datetime | None
    generated_at: datetime | None
    generated_at_source: str
    batch_status: str | None
    verified: bool | None
    verification_failures: tuple[str, ...] = ()
    verification_checks: int = 0
    evidence: Mapping[str, Any] | None = None
    timeline: Mapping[str, Any] | None = None
    timeline_state: str = TIMELINE_UNKNOWN
    timeline_reason: str | None = REASON_TIMELINE_INPUTS_MISSING
    unknowns: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    error: str | None = None
    schema_version: str = field(default=AGENT_RULE_VERSION)

    # ── 结构化视图(日志/审计/报告头部用) ────────────────────────────
    def evidence_summary(self) -> dict[str, Any] | None:
        """只回行数与池子状态: 明细在 `evidence` 里, 这里不重复结论。"""
        if self.evidence is None:
            return None
        run = self.evidence.get("run") or {}
        return {
            "boards": len(self.evidence.get("boards") or ()),
            "limit_up": len(self.evidence.get("limit_up") or ()),
            "pool_state": run.get("pool_state"),
            "pool_count": run.get("limit_up_count"),
        }

    def timeline_summary(self) -> dict[str, Any]:
        payload = self.timeline or {}
        return {
            "state": self.timeline_state,
            "reason": self.timeline_reason,
            # 外部注入的时间线未与采集批次做证据关联核验。
            "run_id": None,
            "association": "unverified_independent_input",
            "dates": len(payload.get("timeline") or ()),
            "events": int((payload.get("provenance") or {}).get("events") or 0),
        }

    def to_dict(self) -> dict[str, Any]:
        """JSON 安全的审计视图。刻意不含任何方向/收益/目标/仓位字段。"""
        payload = {
            "schema_version": self.schema_version,
            "outcome": self.outcome,
            "ok": self.ok,
            "reason": self.reason,
            "run_id": self.run_id,
            "attempt_seq": self.attempt_seq,
            "inserted": self.inserted,
            "replayed": self.replayed,
            "day": self.day,
            "run_key": self.run_key,
            "slot": self.slot,
            "source": self.source,
            "rule_version": self.rule_version,
            "captured_at": _iso(self.captured_at),
            "generated_at": _iso(self.generated_at),
            "generated_at_source": self.generated_at_source,
            "timezone": "Asia/Shanghai",
            "batch_status": self.batch_status,
            "verified": self.verified,
            "verification": {
                "ok": self.verified,
                "checks": self.verification_checks,
                "failures": list(self.verification_failures),
            },
            "evidence": self.evidence_summary(),
            "timeline": self.timeline_summary(),
            "unknowns": list(self.unknowns),
            "notes": list(self.notes),
            "error": self.error,
            "contains_trading_advice": False,
            "limitations": list(AGENT_LIMITATIONS),
        }
        return payload


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment is not None else None


# ── 入参校验(纯函数; 失败就抛, 不静默改口径) ──────────────────────────
def _beijing(moment: Any, field_name: str) -> datetime:
    """带时区的输入按真实时区换算成北京 naive 时间; 硬丢 tzinfo 会静默挪 8 小时。"""
    if not isinstance(moment, datetime):
        raise ValueError(f"{field_name} 必须是 datetime, 收到 {moment!r}")
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(_CST).replace(tzinfo=None)


def _require_day(day: Any) -> str:
    if not isinstance(day, str) or len(day) != _DAY_RE_LEN or not day.isdigit():
        raise ValueError(f"day 必须是 YYYYMMDD 字符串, 收到 {day!r}")
    try:
        datetime.strptime(day, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"day 不是合法日期: {day!r}") from exc
    return day


# ── 读回(只读; 库仍是调用方显式给的那个) ────────────────────────────────
def _read_evidence(bind: Engine | Connection, run_id: int) -> Mapping[str, Any] | None:
    """按 run_id 读回**该批次**的明细(不读当日其它 attempt)。"""
    from src.core.theme_evolution_store import evidence_for_run

    if isinstance(bind, Engine):
        with bind.connect() as conn:
            return evidence_for_run(conn, run_id)
    return evidence_for_run(bind, run_id)


def _verify(
    evidence: Any,
    *,
    run_id: int,
    day: str,
    run_key: str,
    append: Any,
    outcome: Any,
) -> tuple[tuple[str, ...], int]:
    """逐项核对读回的证据是否**就是本次 run**. 返回 (失败码, 检查项数)。"""
    failures: list[str] = []
    checks = 0

    if evidence is None:
        return (VERIFY_RUN_NOT_FOUND,), 1
    if not isinstance(evidence, Mapping):
        return (VERIFY_READBACK_NOT_A_MAPPING,), 1

    run = evidence.get("run")
    boards = list(evidence.get("boards") or ())
    limit_up = list(evidence.get("limit_up") or ())
    checks += 1
    if not isinstance(run, Mapping):
        return tuple(failures + [VERIFY_RUN_MISSING]), checks

    checks += 1
    if run.get("run_id") != run_id:
        failures.append(VERIFY_RUN_ID_MISMATCH)
    checks += 1
    if run.get("run_key") != run_key:
        failures.append(VERIFY_RUN_KEY_MISMATCH)
    checks += 1
    if run.get("day") != day:
        failures.append(VERIFY_DAY_MISMATCH)
    # 采集时刻/源状态是这次采集的"指纹": 对不上说明读回的不是同一次输入。
    checks += 1
    if run.get("captured_at") != getattr(outcome, "captured_at", None):
        failures.append(VERIFY_CAPTURED_AT_MISMATCH)
    checks += 1
    if run.get("source_fresh") != getattr(outcome, "source_fresh", None):
        failures.append(VERIFY_SOURCE_FLAG_MISMATCH)
    checks += 1
    if run.get("source_trade_date") != getattr(outcome, "source_trade_date", None):
        failures.append(VERIFY_SOURCE_TRADE_DATE_MISMATCH)
    checks += 1
    if run.get("status") != getattr(append, "status", None):
        failures.append(VERIFY_STATUS_MISMATCH)
    checks += 1
    if run.get("pool_state") != getattr(append, "pool_state", None):
        failures.append(VERIFY_POOL_STATE_MISMATCH)
    checks += 1
    # run 行自己的计数才是该批次的权威事实: 明细行数必须与它一致。
    boards_total = run.get("boards_total")
    if not isinstance(boards_total, int) or len(boards) != boards_total:
        failures.append(VERIFY_BOARDS_COUNT_MISMATCH)
    checks += 1
    if run.get("pool_state") == cap.POOL_OK:
        if not isinstance(run.get("limit_up_count"), int) \
                or len(limit_up) != run.get("limit_up_count"):
            failures.append(VERIFY_LIMIT_UP_COUNT_MISMATCH)
    elif limit_up:
        failures.append(VERIFY_LIMIT_UP_COUNT_MISMATCH)

    # 本次是新写入时, store 回报的写入行数也必须与 run 行、明细三方一致。
    if getattr(append, "inserted", None) is True:
        checks += 1
        expected_boards = boards_total if isinstance(boards_total, int) else -1
        if int(getattr(append, "boards_written", -1)) != expected_boards:
            failures.append(VERIFY_BOARDS_COUNT_MISMATCH)
        expected_pool = int(run.get("limit_up_count") or 0)
        if int(getattr(append, "limit_up_written", -1)) != expected_pool:
            failures.append(VERIFY_LIMIT_UP_COUNT_MISMATCH)

    # 行必须属于本次 run 且落在同一交易日: 串批次/串日就是拼证据。
    checks += 1
    foreign = [
        row for row in (*boards, *limit_up)
        if row.get("run_id") != run_id or row.get("day") != day
    ]
    if foreign:
        failures.append(VERIFY_FOREIGN_ROWS)

    # refused 批次在库里必须零明细: 带证据的"拒绝"是自相矛盾的记录。
    checks += 1
    if run.get("status") == cap.STATUS_REFUSED and (boards or limit_up):
        failures.append(VERIFY_REFUSED_WITH_EVIDENCE)

    # 池子非 ok 时计数必须是 NULL: 0 只涨停与"没取到"不能混。
    checks += 1
    if run.get("pool_state") != cap.POOL_OK and run.get("limit_up_count") is not None:
        failures.append(VERIFY_POOL_COUNT_NOT_NULL)

    return tuple(failures), checks


# ── unknown 汇总(显式列出"不知道"的东西) ────────────────────────────────
def _capture_unknowns(outcome: Any) -> list[str]:
    out: list[str] = []
    if getattr(outcome, "source_fresh", None) is None:
        out.append("source_fresh_unknown")
    if getattr(outcome, "source_trade_date", None) is None:
        out.append("source_trade_date_unknown")
    pool_state = getattr(outcome, "pool_state", None)
    if pool_state != cap.POOL_OK:
        out.append(f"pool_state_{pool_state}")
    if getattr(outcome, "pool_count", None) is None:
        out.append("pool_count_unknown")
    boards = tuple(getattr(outcome, "boards", ()) or ())
    unknown_dates = sum(1 for b in boards if getattr(b, "data_day", None) is None)
    if unknown_dates:
        out.append(f"boards_with_unknown_date:{unknown_dates}")
    if getattr(outcome, "status", None) != cap.STATUS_COMPLETE:
        out.append("batch_not_complete")
    return out


# ── 时间线(只在有可用批次时挂上; 输入必须显式注入) ──────────────────────
def _build_timeline(
    *,
    events: Sequence[Mapping[str, Any]] | None,
    klines: Sequence[Mapping[str, Any]] | None,
    verified_code_map: Mapping[str, str] | None,
    generated_at: datetime | None,
) -> tuple[Mapping[str, Any] | None, str, str | None, list[str]]:
    """→ (时间线 | None, state, reason, unknowns)。不补数据、不猜输入。"""
    missing: list[str] = []
    if events is None:
        missing.append("timeline_events_not_injected")
    if klines is None:
        missing.append("timeline_klines_not_injected")
    if missing:
        return None, TIMELINE_UNKNOWN, REASON_TIMELINE_INPUTS_MISSING, missing

    try:
        payload = tl.build_timeline(
            events,
            klines,
            verified_code_map=verified_code_map,
            generated_at=generated_at.isoformat(timespec="seconds") if generated_at else None,
        )
    except tl.TimelineInputError as exc:
        # code 是稳定的; 异常消息可能带原始值, 只留 code, 不落原文。
        return None, TIMELINE_INVALID, exc.code, [f"timeline_invalid:{exc.code}"]
    except Exception as exc:  # noqa: BLE001 —— 时间线坏了不该抹掉已落库的批次
        reason = f"timeline_error:{type(exc).__name__}"
        return None, TIMELINE_INVALID, reason, [f"timeline_invalid:{reason}"]

    unknowns: list[str] = []
    provenance = payload.get("provenance") or {}
    if provenance.get("point_in_time_verified") is not True:
        # 事后标签: 时点不可回放, 这是随结果一起说清的未知项。
        unknowns.append("timeline_point_in_time_unverified")
    return payload, TIMELINE_OK, None, unknowns


# ── 主函数: 一次运行 ────────────────────────────────────────────────────
def run_once(
    *,
    now: datetime,
    bind: Engine | Connection | None = None,
    day: str | None = None,
    slot: str = cap.DEFAULT_SLOT,
    fetch_boards: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    fetch_members: Callable[[str], Any] | None = None,
    fetch_pool: Callable[[str], Any] | None = None,
    fetch_trade_date: Callable[[], Any] | None = None,
    source_fresh: bool | None = None,
    source_trade_date: str | None = None,
    max_boards: int | None = None,
    budget_s: float = cap.FETCH_BUDGET_S,
    monotonic: Callable[[], float] | None = None,
    events: Sequence[Mapping[str, Any]] | None = None,
    klines: Sequence[Mapping[str, Any]] | None = None,
    verified_code_map: Mapping[str, str] | None = None,
    generated_at: datetime | None = None,
    capture: Callable[..., Any] | None = None,
    reader: Callable[[Engine | Connection, int], Any] | None = None,
) -> AgentRun:
    """跑一次: 采集落库(同一批次) → 按 run_id 读回核对 → 可选挂时间线。

    参数里 `capture` / `reader` 只是给测试与上层替换实现用的注入点, 缺省即真实
    capture 与 store 读侧。`now` 为采集起点；`generated_at` 不给时标未知。

    拒绝(采集拒绝/落库被拒)与失败(异常/核对不过)都返回 `ok=False` 的结构化结果;
    只有入参本身非法(day/slot/now/generated_at)才抛 `ValueError`。
    """
    captured_at = _beijing(now, "now")
    target = _require_day(day) if day is not None else captured_at.strftime("%Y%m%d")
    run_key = cap.run_key_for(target, slot)  # 同时校验 slot 形态
    if generated_at is None:
        generated = None
        generated_source = "unknown"
    else:
        generated = _beijing(generated_at, "generated_at")
        if generated < captured_at:
            raise ValueError("generated_at 不能早于采集起点")
        generated_source = GENERATED_AT_EXPLICIT

    def _result(**over: Any) -> AgentRun:
        base: dict[str, Any] = dict(
            outcome=OUTCOME_FAILED,
            ok=False,
            reason=None,
            run_id=None,
            attempt_seq=None,
            inserted=None,
            replayed=False,
            day=target,
            run_key=run_key,
            slot=slot,
            source=None,
            rule_version=None,
            captured_at=None,
            generated_at=generated,
            generated_at_source=generated_source,
            batch_status=None,
            verified=None,
            timeline_state=TIMELINE_SKIPPED,
            timeline_reason=REASON_TIMELINE_NO_BATCH,
        )
        base.update(over)
        return AgentRun(**base)

    # ① 库必须是显式给的: 没有库就没有读回, 也就没有可报告的运行。
    if bind is None:
        return _result(reason=REASON_BIND_MISSING, error=REASON_BIND_MISSING,
                       unknowns=("bind_not_injected",))

    # ② 采集: 落库的是同一个批次。入参错误照抛, 运行期异常结构化上报。
    capture_fn = capture if callable(capture) else cap.capture_day
    try:
        outcome = capture_fn(
            bind,
            now=captured_at,
            day=target,
            slot=slot,
            fetch_boards=fetch_boards,
            fetch_members=fetch_members,
            fetch_pool=fetch_pool,
            fetch_trade_date=fetch_trade_date,
            source_fresh=source_fresh,
            source_trade_date=source_trade_date,
            max_boards=max_boards,
            budget_s=budget_s,
            monotonic=monotonic,
        )
    except ValueError:
        raise  # day/slot/now 已在上方校验; 走到这里是调用方传了非法依赖
    except Exception as exc:  # noqa: BLE001 —— 只留类型名, 消息里可能有 URL/token
        reason = f"{REASON_CAPTURE_EXCEPTION}:{type(exc).__name__}"
        return _result(reason=reason, error=reason,
                       unknowns=("capture_failed",))

    append = getattr(outcome, "append", None)
    if outcome is None or append is None:
        return _result(reason=REASON_CAPTURE_NO_RESULT, error=REASON_CAPTURE_NO_RESULT,
                       unknowns=("capture_not_persisted",))

    base_meta: dict[str, Any] = dict(
        source=getattr(outcome, "source", None) or cap.SOURCE,
        rule_version=getattr(outcome, "rule_version", None) or cap.RULE_VERSION,
        captured_at=getattr(outcome, "captured_at", None),
        batch_status=getattr(append, "status", None),
        attempt_seq=getattr(append, "attempt_seq", None),
        inserted=getattr(append, "inserted", None),
    )

    inserted = getattr(append, "inserted", None)
    persist_reason = getattr(append, "reason", None)
    if inserted is False and persist_reason != "run_key_exists":
        # 落库被拒(如当日 attempt 上限): 没有可核对的写入, 不许说成功。
        return _result(
            **base_meta,
            outcome=OUTCOME_REFUSED,
            reason=f"persistence_refused:{persist_reason}",
            verified=None,
            unknowns=tuple(_capture_unknowns(outcome)) + ("no_persisted_batch",),
            notes=tuple(getattr(outcome, "notes", ()))[:_MAX_NOTES],
        )

    replayed = inserted is False
    run_id = getattr(append, "run_id", None)
    if not isinstance(run_id, int):
        return _result(
            **base_meta,
            reason="persist_without_run_id",
            error="persist_without_run_id",
            unknowns=("no_run_id",),
        )

    # ③ 按 run_id 读回并核对: 只认本次 run, 核对不过就作废整份证据。
    reader_fn = reader if callable(reader) else _read_evidence
    try:
        evidence = reader_fn(bind, run_id)
    except Exception:  # noqa: BLE001 —— 读失败是 failed, 不是"没有证据"
        failures: tuple[str, ...] = (VERIFY_READ_EXCEPTION,)
        checks = 1
        evidence = None
    else:
        failures, checks = _verify(evidence, run_id=run_id, day=target,
                                   run_key=run_key, append=append, outcome=outcome)
        if failures:
            evidence = None

    verified: bool = not failures
    if failures:
        outcome_code = OUTCOME_FAILED
        reason: str | None = REASON_READBACK_MISMATCH
    elif getattr(outcome, "status", None) == cap.STATUS_REFUSED:
        outcome_code = OUTCOME_REFUSED
        reason = REASON_CAPTURE_REFUSED
    elif getattr(outcome, "status", None) == cap.STATUS_COMPLETE:
        outcome_code = OUTCOME_COMPLETE
        reason = None
    else:
        outcome_code = OUTCOME_PARTIAL
        reason = None

    # ④ 时间线: 只在有可用批次时挂; 输入缺失/非法一律显式 unknown/invalid。
    unknowns: list[str] = list(_capture_unknowns(outcome))
    notes: list[str] = list(getattr(outcome, "notes", ()) or ())
    if replayed:
        notes.append(NOTE_REPLAYED)
    if len(notes) > _MAX_NOTES:
        notes = notes[:_MAX_NOTES] + [f"notes_truncated:{len(notes) - _MAX_NOTES}"]

    timeline_payload: Mapping[str, Any] | None = None
    timeline_state = TIMELINE_SKIPPED
    timeline_reason: str | None = REASON_TIMELINE_NO_BATCH
    if outcome_code in (OUTCOME_COMPLETE, OUTCOME_PARTIAL) and evidence is not None:
        (timeline_payload, timeline_state,
         timeline_reason, timeline_unknowns) = _build_timeline(
            events=events, klines=klines, verified_code_map=verified_code_map,
            generated_at=generated,
        )
        unknowns.extend(timeline_unknowns)
    else:
        unknowns.append("timeline_skipped_no_batch")

    return AgentRun(
        outcome=outcome_code,
        ok=(outcome_code == OUTCOME_COMPLETE and verified),
        reason=reason,
        run_id=run_id,
        attempt_seq=getattr(append, "attempt_seq", None),
        inserted=inserted,
        replayed=replayed,
        day=target,
        run_key=run_key,
        slot=slot,
        source=base_meta["source"],
        rule_version=base_meta["rule_version"],
        captured_at=base_meta["captured_at"],
        generated_at=generated,
        generated_at_source=generated_source,
        batch_status=getattr(append, "status", None),
        verified=verified,
        verification_failures=failures,
        verification_checks=checks,
        evidence=evidence,
        timeline=timeline_payload,
        timeline_state=timeline_state,
        timeline_reason=timeline_reason,
        unknowns=tuple(unknowns),
        notes=tuple(notes),
        error=(reason if outcome_code == OUTCOME_FAILED else None),
    )
