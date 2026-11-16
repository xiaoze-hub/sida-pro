"""题材演化观察层 A 通道: **采集编排**(纯函数 + 注入依赖, 默认不联网)。

## 这个模块干什么

把「调用方注入的取数函数 + 显式传入的当前时间」编排成一份**可追责的证据批次**:

1. 按板块保存采集结果(ok / empty_ok / fetch_failed) **和该板块自己的源有效日期**;
2. 逐行核验涨停池的 `as_of`, 只有全部行都指向目标日才敢用;
3. 把批次交给 `theme_evolution_store.append_observation` 只追加落库(可选)。

它**不**抓数据(**依赖全部注入, 不注入就拒绝**)、**不**开库(库是显式参数)、
**不**出报告、**不**做题材分类、**不**给任何交易方向。

## 为什么需要它(而不是直接用 `theme_evolution.collect_snapshot`)

现有 `collect_snapshot` 的问题都集中在"日期的归属"上, 而报告依赖这些日期:

- 板块成分来自**实时**接口, 没有 as-of 版本 ⇒ 只能证明"采集那一刻", 不能当历史日;
- 涨停池会**回退到更早日期**(源会连续探 6 天), 且部分来源没有 `as_of` ⇒ 昨天/上周的
  池子极易被当成今天的池子;
- 空列表既可能是"取数失败"也可能是"真的 0 只涨停", 调用方无从区分;
- 同一天重跑会原地覆盖当天快照(见 store 模块的说明)。

## 硬约束(违反即视为 bug)

1. **默认不联网**: `fetch_boards` / `fetch_members` 不注入 ⇒ 结构化 refused
   (`deps_not_injected`), 一次外部调用都不发。模块 import 期也不得拉进行情/采集器/
   HTTP 客户端。
2. **时间显式**: `now` 是必填关键字参数, 模块内不读系统时钟(单调时钟也只用来算预算,
   可注入)。
3. **当前成分不能冒充历史日**: 目标日 ≠ `now` 所在(北京)日 ⇒ refused
   (`current_members_wrong_day`), 而且**一次取数都不做**。历史日需要独立的 as-of 来源。
4. **逐行核 `as_of`**: 任一行缺 `as_of` / 解析不了 / 多行不一致 / 与目标日不符 ⇒
   池状态**只**记 `unknown`, 不写任何代码, `pool_count=None`。旧池绝不顶到目标日。
5. **失败不是 0**: 取数失败或空列表 ⇒ `None`(落库 NULL), 不是 0。空列表无法区分
   "失败"与"真的没有", 一律 `unknown`。
6. **日期未知就写未知**: 板块只返回成员列表时, `data_day`/`date_basis` 都是 `None`,
   **绝不**用目标日或行情最新日填充 —— "行情最新日"不是"成分有效日"。
7. **不造分类、不给建议**: 输出字段里没有 rank/score/kind/theme/advice 之类派生判断;
   本模块不存在报告渲染或建议构造函数。
8. **只追加 / 同日可恢复**: 同一天补采是**新的 attempt**(旧批次不动); 同一个
   `run_key` 重跑幂等。

## 与 store 的关系

本模块只产出 `CaptureOutcome` 并可选调用 `theme_evolution_store.append_observation`;
库结构与事务语义全在 store 里(独立 MetaData、`theme_obs_*` 前缀、日级锁)。

## 边界 / 未覆盖

- **未接主链路**: 没有任何入口调用它; `market_phase` / 调度 / 迁移一行没改。
- **不验证来源真实性**: `as_of` 是**源自报**的, 本模块只能保证"行内一致且等于目标日",
  不能证明供应商没撒谎; `as_of_basis` 只做记录与混杂检测。
- **取数完整度**: 涨停池 `pool_coverage` 未逐行自报 complete 时批次为 partial；
  源自报 complete 也不等于完整性已经过独立验证。
- **预算**: 只在板块循环里按注入的单调时钟卡总预算; 涨停池取数不计入(调用方应自带超时)。
- **状态口径**: 目录异常或截断时采集结果为 partial，并向 store 传递 input_partial，
  不允许只凭保留下来的行将该批次升级为 complete；具体缺口随 capture_notes 留存。
- **不处理并发**: 并发安全全在 store 的日级锁里; 本模块不重试、不退避。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

__all__ = [
    "BASIS_UNKNOWN",
    "BoardCapture",
    "CaptureOutcome",
    "DEFAULT_SLOT",
    "FETCH_BUDGET_S",
    "POOL_FAILED",
    "POOL_OK",
    "POOL_UNKNOWN",
    "PoolRow",
    "REASON_CATALOG_EMPTY",
    "REASON_CATALOG_FAILED",
    "REASON_DEPS_MISSING",
    "REASON_SOURCE_NOT_UPDATED",
    "REASON_SOURCE_STALE",
    "REASON_WRONG_DAY",
    "RULE_VERSION",
    "SOURCE",
    "STATE_EMPTY",
    "STATE_FAILED",
    "STATE_OK",
    "STATUS_COMPLETE",
    "STATUS_PARTIAL",
    "STATUS_REFUSED",
    "capture_day",
    "plan_capture",
    "run_key_for",
]

_CST = ZoneInfo("Asia/Shanghai")

#: 规则版本, 与 store / 报告同一口径。
RULE_VERSION = "theme_evo_v1"
#: 证据来源标记(通达信板块目录 + 成分)。
SOURCE = "tq"
#: 默认批次槽位: 同日同一槽位的重跑是幂等的。
DEFAULT_SLOT = "daily"
#: 板块循环的总时间预算(秒), 超预算的板块记 fetch_failed。
FETCH_BUDGET_S = 300.0
#: 日期依据未知时的字面值: 日期本身已由各行 `as_of` 一致确认, 但**怎么**确认的未知。
BASIS_UNKNOWN = "unknown_source"

#: 板块三态, 与 store / 251 表同名同义。
STATE_OK = "ok"
STATE_EMPTY = "empty_ok"
STATE_FAILED = "fetch_failed"

#: 涨停池三态。unknown = 无法证明是目标日的池子(缺/混杂/错日/空), 不是 0 只涨停。
POOL_OK = "ok"
POOL_UNKNOWN = "unknown"
POOL_FAILED = "fetch_failed"

#: 批次状态。
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_REFUSED = "refused"

#: 拒绝原因(结构化, 不含任何凭据/URL)。
REASON_DEPS_MISSING = "deps_not_injected"
REASON_WRONG_DAY = "current_members_wrong_day"
REASON_CATALOG_EMPTY = "board_catalog_empty"
REASON_CATALOG_FAILED = "board_catalog_failed"
REASON_SOURCE_STALE = "source_stale"
REASON_SOURCE_NOT_UPDATED = "source_not_updated_for_day"

_DAY_RE = re.compile(r"^\d{8}$")
_SLOT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,20}$")
_MAX_DATE_BASIS = 32
_MAX_TEXT = 64


# ── 输出模型(纯数据, 不可变) ───────────────────────────────────────────
@dataclass(frozen=True)
class BoardCapture:
    """单个板块的一次采集结果 + **它自己的**源有效日期。

    `data_day`/`date_basis` 同为 None = 日期未知(源没自报, 且不能拿别的日期顶替)。
    """

    board_code: str
    state: str
    board_name: str | None = None
    board_type: str | None = None
    members: tuple[str, ...] | None = None
    data_day: str | None = None
    date_basis: str | None = None
    error: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def member_count(self) -> int | None:
        """None = 没取到(不是 0 个成员)。"""
        return None if self.members is None else len(self.members)


@dataclass(frozen=True)
class CaptureOutcome:
    """一次采集编排的结果。`append` 非 None 表示已尝试落库(其 inserted 才是事实)。"""

    day: str
    run_key: str
    status: str
    captured_at: datetime
    reason: str | None = None
    source_fresh: bool | None = None
    source_trade_date: str | None = None
    boards: tuple[BoardCapture, ...] = ()
    boards_ok: int = 0
    boards_empty_ok: int = 0
    boards_fetch_failed: int = 0
    pool_state: str = POOL_UNKNOWN
    pool_count: int | None = None
    pool_data_day: str | None = None
    pool_date_basis: str | None = None
    pool_error: str | None = None
    notes: tuple[str, ...] = ()
    append: Any | None = None
    #: 池证券(含采集时刻的题材归属)。只在 pool_state=ok 时存在; 不进对外视图, 落库时才用。
    _pool_codes: tuple["PoolRow", ...] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def boards_total(self) -> int:
        return len(self.boards)

    @property
    def coverage(self) -> float | None:
        """ok 占已尝试板块的比例; 没有任何板块时为 None(不是 1.0)。"""
        total = len(self.boards)
        return (self.boards_ok / total) if total else None

    def to_dict(self) -> dict[str, Any]:
        """结构化视图(日志/审计用)。刻意不含任何派生判断字段。"""
        append = None
        if self.append is not None:
            append = {
                "inserted": bool(getattr(self.append, "inserted", False)),
                "reason": getattr(self.append, "reason", None),
                "run_id": getattr(self.append, "run_id", None),
                "attempt_seq": getattr(self.append, "attempt_seq", None),
                "status": getattr(self.append, "status", None),
                "boards_written": getattr(self.append, "boards_written", 0),
                "limit_up_written": getattr(self.append, "limit_up_written", 0),
            }
        return {
            "day": self.day,
            "run_key": self.run_key,
            "status": self.status,
            "reason": self.reason,
            "captured_at": self.captured_at.isoformat(timespec="seconds"),
            "source_fresh": self.source_fresh,
            "source_trade_date": self.source_trade_date,
            "boards_total": self.boards_total,
            "boards_ok": self.boards_ok,
            "boards_empty_ok": self.boards_empty_ok,
            "boards_fetch_failed": self.boards_fetch_failed,
            "coverage": self.coverage,
            "pool_state": self.pool_state,
            "pool_count": self.pool_count,
            "pool_data_day": self.pool_data_day,
            "pool_date_basis": self.pool_date_basis,
            "notes": list(self.notes),
            "boards": [
                {
                    "board_code": b.board_code,
                    "board_name": b.board_name,
                    "board_type": b.board_type,
                    "state": b.state,
                    "data_day": b.data_day,
                    "date_basis": b.date_basis,
                    "member_count": b.member_count,
                    "error": b.error,
                    "notes": list(b.notes),
                }
                for b in self.boards
            ],
            "append": append,
        }

    def as_store_batch(self):
        """转成 `theme_evolution_store.ObservationBatch`(懒 import, 不建任何库对象)。"""
        from src.core.theme_evolution_store import (
            BoardEvidence,
            LimitUpEvidence,
            ObservationBatch,
        )

        if self.status == STATUS_REFUSED:
            return ObservationBatch(
                day=self.day,
                run_key=self.run_key,
                rule_version=RULE_VERSION,
                source=SOURCE,
                captured_at=self.captured_at,
                boards=(),
                limit_up=(),
                pool_state=POOL_UNKNOWN,
                source_fresh=self.source_fresh,
                source_trade_date=self.source_trade_date,
                refused_reason=self.reason or REASON_CATALOG_FAILED,
            )
        return ObservationBatch(
            day=self.day,
            run_key=self.run_key,
            rule_version=RULE_VERSION,
            source=SOURCE,
            captured_at=self.captured_at,
            boards=tuple(
                BoardEvidence(
                    board_code=b.board_code,
                    state=b.state,
                    board_name=b.board_name,
                    board_type=b.board_type,
                    data_day=b.data_day,
                    date_basis=b.date_basis,
                    members=b.members,
                    has_quote=None,
                    error=b.error,
                )
                for b in self.boards
            ),
            limit_up=tuple(
                LimitUpEvidence(
                    stock_code=row.code,
                    stock_name=row.name,
                    sector=row.sector,
                    concepts=row.concepts,
                    block_as_of=row.block_as_of,
                    block_source=row.block_source,
                )
                for row in (self._pool_codes or ())
            ),
            pool_state=self.pool_state,
            pool_data_day=self.pool_data_day,
            pool_date_basis=self.pool_date_basis,
            input_partial=self.status != STATUS_COMPLETE,
            capture_notes=self.notes,
            source_fresh=self.source_fresh,
            source_trade_date=self.source_trade_date,
            error=self.pool_error,
        )

# ── 校验/归一 ───────────────────────────────────────────────────────────
def _require_day(day: Any) -> str:
    if not isinstance(day, str) or not _DAY_RE.match(day):
        raise ValueError(f"day 必须是 YYYYMMDD 字符串, 收到 {day!r}")
    try:
        datetime.strptime(day, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"day 不是合法日期: {day!r}") from exc
    return day


def _naive_beijing(moment: Any) -> datetime:
    """带时区的输入按真实时区换算成北京 naive 时间(硬丢 tzinfo 会静默挪 8 小时)。"""
    if not isinstance(moment, datetime):
        raise ValueError("now 必须是 datetime")
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(_CST).replace(tzinfo=None)


def _norm_ymd(value: Any) -> str | None:
    """源自报日期 → `YYYYMMDD`; 解析不了就是 None(不猜)。"""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text = text.replace("-", "")
    if not _DAY_RE.match(text):
        return None
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return text


def _norm_code(value: Any) -> str:
    """保留来源标识符原文；非法值不修补、不剥离市场后缀。"""
    return value if isinstance(value, str) and value and value == value.strip() else ""


def _short(value: Any, limit: int = _MAX_TEXT) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def run_key_for(day: str, slot: str = DEFAULT_SLOT) -> str:
    """稳定的幂等键: 同一天 + 同一槽位 ⇒ 同一把键(重跑不重复写行)。"""
    day = _require_day(day)
    if not isinstance(slot, str) or not _SLOT_RE.match(slot):
        raise ValueError(f"slot 只允许字母/数字/_.- 且长度 ≤20, 收到 {slot!r}")
    return f"{RULE_VERSION}:{day}:{slot}"


# ── 拒绝(一条不写, 且说清原因) ─────────────────────────────────────────
def _refused(
    *,
    day: str,
    run_key: str,
    captured_at: datetime,
    reason: str,
    source_fresh: bool | None,
    source_trade_date: str | None,
    note: str,
) -> CaptureOutcome:
    return CaptureOutcome(
        day=day,
        run_key=run_key,
        status=STATUS_REFUSED,
        reason=reason,
        captured_at=captured_at,
        source_fresh=source_fresh,
        source_trade_date=source_trade_date,
        pool_state=POOL_UNKNOWN,
        notes=(f"refused:{reason}", note),
    )


# ── 板块 ────────────────────────────────────────────────────────────────
def _parse_member_result(raw: Any) -> tuple[list[Any] | None, Any, Any, str | None]:
    """→ (原始成员序列 | None, data_day, date_basis, 错误)。

    `None` = 取不到; 空序列 = 返回为空(不证明源成功或实际没有成员); Mapping 可额外自报日期。
    """
    if raw is None:
        return None, None, None, "constituents_unavailable"
    if isinstance(raw, Mapping):
        if "members" not in raw:
            return None, None, None, "members_missing_in_result"
        members = raw.get("members")
        if not isinstance(members, (list, tuple)):
            return None, None, None, "members_missing_or_invalid"
        return list(members), raw.get("data_day"), raw.get("date_basis"), None
    if isinstance(raw, (list, tuple, set, frozenset)):
        return list(raw), None, None, None
    return None, None, None, "unsupported_member_result"


def _board_date(
    data_day: Any, date_basis: Any, *, day: str
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """板块自报日期 → (data_day, date_basis, notes)。

    未知/半份/非法/晚于目标日 ⇒ 两者都 None。**绝不用目标日或行情日补齐。**
    """
    raw_day = str(data_day or "").strip()
    raw_basis = str(date_basis or "").strip()
    if not raw_day and not raw_basis:
        return None, None, ("board_date_unknown",)
    if not raw_day or not raw_basis:
        return None, None, ("board_date_incomplete",)
    parsed = _norm_ymd(raw_day)
    if parsed is None:
        return None, None, ("board_date_unparsable",)
    if len(raw_basis) > _MAX_DATE_BASIS:
        return None, None, ("board_date_incomplete",)
    if parsed > day:
        return None, None, ("board_date_after_target",)
    if parsed < day:
        # 真实的更早日期照实保存, 但明确标出它不是目标日。
        return parsed, raw_basis, ("board_date_before_target",)
    return parsed, raw_basis, ()


def _collect_board(
    entry: Mapping[str, Any],
    *,
    day: str,
    fetch_members: Callable[[str], Any],
) -> BoardCapture:
    code = str(entry.get("code") or "").strip()[:16]
    name = _short(entry.get("name"))
    board_type = _short(entry.get("board_type"), 16)
    try:
        raw = fetch_members(code)
    except Exception as exc:  # noqa: BLE001 —— 单板块失败不能打断整轮
        # 只留异常类型名: 消息里可能带 URL/token, 不进日志也不落库。
        return BoardCapture(
            board_code=code, state=STATE_FAILED, board_name=name, board_type=board_type,
            error=f"member_fetch_error:{type(exc).__name__}",
            notes=("board_fetch_failed",),
        )

    members, data_day, date_basis, error = _parse_member_result(raw)
    if error is not None or members is None:
        return BoardCapture(
            board_code=code, state=STATE_FAILED, board_name=name, board_type=board_type,
            error=error or "constituents_unavailable",
            notes=("board_fetch_failed",),
        )

    codes = [_norm_code(c) for c in members]
    if any(not c for c in codes):
        return BoardCapture(board_code=code, state=STATE_FAILED, board_name=name,
                            board_type=board_type, error="invalid_member_identifier",
                            notes=("board_fetch_failed",))
    notes: list[str] = []
    if len(set(codes)) != len(codes):
        notes.append("member_duplicates_removed")
    codes = sorted(set(codes))

    parsed_day, parsed_basis, date_notes = _board_date(data_day, date_basis, day=day)
    notes.extend(date_notes)

    if not codes:
        # 空响应只代表返回值为空；不证明接口成功或板块实际没有成员。
        return BoardCapture(
            board_code=code, state=STATE_EMPTY, board_name=name, board_type=board_type,
            members=(), data_day=parsed_day, date_basis=parsed_basis, notes=tuple(notes),
        )
    return BoardCapture(
        board_code=code, state=STATE_OK, board_name=name, board_type=board_type,
        members=tuple(codes), data_day=parsed_day, date_basis=parsed_basis,
        notes=tuple(notes),
    )


# ── 涨停池: 逐行核 as_of ────────────────────────────────────────────────
@dataclass(frozen=True)
class PoolRow:
    """涨停池的一行: 证券 + **采集时刻**能拿到的题材归属(同源同行)。

    归属字段来自源对同一行的富化(`tq:get_relation`), 不是跨源拼出来的。
    `block_as_of` 是反查归属的日期; TQ 成员关系只有"此刻", 所以它通常等于采集日,
    **不代表**该证券在目标日的历史题材归属。
    """

    code: str
    name: str | None
    sector: str | None = None
    concepts: tuple[str, ...] | None = None
    block_as_of: str | None = None
    block_source: str | None = None


def _parse_labels(row: Mapping[str, Any], notes: list[str]) -> tuple[
        str | None, tuple[str, ...] | None, str | None, str | None]:
    """从池行取归属字段。形状不对只记 unknown, 不猜、不把 dict 当字符串。"""
    raw_sector = row.get("sector")
    sector = str(raw_sector).strip()[:64] if isinstance(raw_sector, str) and raw_sector.strip() else None

    raw_concepts = row.get("concepts")
    concepts: tuple[str, ...] | None
    if raw_concepts is None:
        concepts = None
    elif isinstance(raw_concepts, (list, tuple)):
        cleaned: list[str] = []
        for item in raw_concepts:
            if not isinstance(item, str):
                notes.append("pool_concepts_malformed")
                concepts = None
                break
            name = item.strip()[:64]
            if name and name not in cleaned:
                cleaned.append(name)
        else:
            concepts = tuple(cleaned)
    else:
        notes.append("pool_concepts_malformed")
        concepts = None

    raw_as_of = row.get("block_as_of")
    block_as_of = _norm_ymd(raw_as_of) if raw_as_of is not None else None
    if raw_as_of is not None and block_as_of is None:
        notes.append("pool_block_as_of_unparsable")

    raw_source = row.get("block_source")
    block_source = (
        str(raw_source).strip()[:32]
        if isinstance(raw_source, str) and raw_source.strip() else None
    )
    return sector, concepts, block_as_of, block_source


def _parse_pool(
    rows: Any, *, day: str
) -> tuple[str, tuple[PoolRow, ...] | None, str | None, str | None,
           tuple[str, ...]]:
    """→ (pool_state, rows | None, data_day, date_basis, notes)。

    只有"每一行都有合法 `as_of`、且全部等于目标日、每行都有代码"才敢记 `ok`。
    归属字段只搬运, 不参与日期判定(它证明不了历史成分)。
    """
    if rows is None:
        return POOL_UNKNOWN, None, None, None, ("pool_none_unverifiable",)
    if not isinstance(rows, (list, tuple)):
        return POOL_UNKNOWN, None, None, None, ("pool_not_a_sequence",)
    if not rows:
        # 空列表无法区分"取数失败"与"真的 0 只涨停" ⇒ unknown(不是 0)。
        return POOL_UNKNOWN, None, None, None, ("pool_empty_unverifiable",)

    codes: list[PoolRow] = []
    seen: set[str] = set()
    dates: list[str] = []
    bases: set[str] = set()
    notes: list[str] = []
    has_basis_gap = False

    for row in rows:
        if not isinstance(row, Mapping):
            return POOL_UNKNOWN, None, None, None, ("pool_row_not_mapping",)
        code = _norm_code(row.get("code") or row.get("stock_code"))
        if not code:
            return POOL_UNKNOWN, None, None, None, ("pool_row_without_code",)
        raw_as_of = row.get("as_of")
        if raw_as_of is None or not str(raw_as_of).strip():
            return POOL_UNKNOWN, None, None, None, ("pool_as_of_missing",)
        parsed_as_of = _norm_ymd(raw_as_of)
        if parsed_as_of is None:
            return POOL_UNKNOWN, None, None, None, ("pool_as_of_unparsable",)
        dates.append(parsed_as_of)
        basis = str(row.get("as_of_basis") or "").strip()
        if basis:
            bases.add(basis[:_MAX_DATE_BASIS])
        else:
            has_basis_gap = True
        if code in seen:
            notes.append("pool_duplicate_code")
            continue
        seen.add(code)
        sector, concepts, block_as_of, block_source = _parse_labels(row, notes)
        codes.append(PoolRow(
            code=code,
            name=_short(row.get("name")),
            sector=sector,
            concepts=concepts,
            block_as_of=block_as_of,
            block_source=block_source,
        ))

    if len(set(dates)) > 1:
        return POOL_UNKNOWN, None, None, None, ("pool_as_of_mixed",)
    if dates[0] != day:
        # 源回退到了更早的交易日的池子: 那是**那一天**的池子, 不能当目标日。
        return POOL_UNKNOWN, None, None, None, (
            f"pool_as_of_wrong_day:{dates[0]}",
        )

    # 请求日只代表提问参数，不是响应中证券实际数据日；混杂/缺失同样不可入当日池。
    if has_basis_gap or bases != {"kline"}:
        return POOL_UNKNOWN, None, None, None, ("pool_date_basis_unverifiable",)
    date_basis = "kline"
    return POOL_OK, tuple(codes), day, date_basis, tuple(notes)


# ── 编排(纯函数: 只调注入的函数, 不建库、不发请求) ─────────────────────
def plan_capture(
    *,
    now: datetime,
    day: str | None = None,
    fetch_boards: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    fetch_members: Callable[[str], Any] | None = None,
    fetch_pool: Callable[[str], Any] | None = None,
    fetch_trade_date: Callable[[], Any] | None = None,
    source_fresh: bool | None = None,
    source_trade_date: str | None = None,
    slot: str = DEFAULT_SLOT,
    max_boards: int | None = None,
    budget_s: float = FETCH_BUDGET_S,
    monotonic: Callable[[], float] | None = None,
) -> CaptureOutcome:
    """把注入的取数结果编排成一个证据批次(**不落库**)。

    拒绝(一条不写)的六种情形: 依赖未注入 / 目标日不是今天 / 板块目录为空 /
    板块目录取失败 / 源不新鲜 / 源交易日与目标日不符。
    """
    captured_at = _naive_beijing(now)
    today = captured_at.strftime("%Y%m%d")
    target = today if day is None else _require_day(day)
    run_key = run_key_for(target, slot)
    fresh = source_fresh if isinstance(source_fresh, bool) else None

    trade_day: str | None = None

    # ① 依赖: 没有取数函数就没有数据, 绝不回退到模块内的默认实现(默认不联网)。
    if not callable(fetch_boards) or not callable(fetch_members):
        return _refused(
            day=target, run_key=run_key, captured_at=captured_at,
            reason=REASON_DEPS_MISSING, source_fresh=fresh, source_trade_date=trade_day,
            note="未注入 fetch_boards/fetch_members: 本模块默认不联网, 拒绝采集",
        )
    # ② 当前成分不能当别的日期的成分。
    if target != today:
        return _refused(
            day=target, run_key=run_key, captured_at=captured_at,
            reason=REASON_WRONG_DAY, source_fresh=fresh, source_trade_date=trade_day,
            note=(
                f"目标日 {target} 不是当前日 {today}: 实时成分接口没有 as-of 版本, "
                "历史成分需独立来源和时点证据"
            ),
        )
    # ③ 源新鲜度/交易日对齐(拿旧数据冒充今天 = 数据日错配)。
    if isinstance(source_trade_date, str) and source_trade_date.strip():
        trade_day = _norm_ymd(source_trade_date)
    elif callable(fetch_trade_date):
        try:
            trade_day = _norm_ymd(fetch_trade_date())
        except Exception:  # noqa: BLE001
            trade_day = None
    if fresh is False:
        return _refused(
            day=target, run_key=run_key, captured_at=captured_at,
            reason=REASON_SOURCE_STALE, source_fresh=fresh, source_trade_date=trade_day,
            note="源自报不新鲜(客户端未更新) → 不落当日证据",
        )
    if trade_day is not None and trade_day != target:
        return _refused(
            day=target, run_key=run_key, captured_at=captured_at,
            reason=REASON_SOURCE_NOT_UPDATED, source_fresh=fresh,
            source_trade_date=trade_day,
            note=f"源最新交易日 {trade_day} 与目标日 {target} 不符 → 拒绝落库(数据日错配)",
        )

    # ④ 板块目录。
    try:
        catalog = fetch_boards()
    except Exception as exc:  # noqa: BLE001
        return _refused(
            day=target, run_key=run_key, captured_at=captured_at,
            reason=REASON_CATALOG_FAILED, source_fresh=fresh, source_trade_date=trade_day,
            note=f"板块目录取数失败({type(exc).__name__}) → 拒绝写空快照",
        )
    if not isinstance(catalog, (list, tuple)) or not catalog:
        return _refused(
            day=target, run_key=run_key, captured_at=captured_at,
            reason=REASON_CATALOG_EMPTY, source_fresh=fresh, source_trade_date=trade_day,
            note="板块目录为空 → 不落空快照(避免把取数失败写成'没有板块')",
        )

    started = (monotonic or time.monotonic)()
    all_entries = list(catalog)
    limit = None if max_boards is None else int(max_boards)
    scope_limited = limit is not None and len(all_entries) > limit
    entries = all_entries[:limit] if limit is not None else all_entries
    boards: list[BoardCapture] = []
    notes: list[str] = []
    seen: set[str] = set()
    catalog_anomaly = False
    if scope_limited:
        # 只采了一部分目录 ≠ 采完了: 明确标出, 并且本轮不算 complete。
        notes.append(f"scope_truncated:{len(entries)}/{len(all_entries)}")

    for entry in entries:
        if not isinstance(entry, Mapping):
            catalog_anomaly = True
            notes.append("board_entry_invalid:not_a_mapping")
            continue
        code = entry.get("code")
        if not isinstance(code, str) or not code or code != code.strip() or len(code) > 16:
            catalog_anomaly = True
            notes.append("board_entry_invalid:identifier")
            continue
        if code in seen:
            catalog_anomaly = True
            notes.append(f"duplicate_board_code:{code}")
            continue
        seen.add(code)
        if (monotonic or time.monotonic)() - started > budget_s:
            boards.append(
                BoardCapture(
                    board_code=code[:16], state=STATE_FAILED,
                    board_name=_short(entry.get("name")),
                    board_type=_short(entry.get("board_type"), 16),
                    error="budget_exhausted", notes=("board_budget_exhausted",),
                )
            )
            notes.append(f"budget_exhausted:{code}")
            continue
        boards.append(_collect_board(entry, day=target, fetch_members=fetch_members))

    if not boards:
        return _refused(
            day=target, run_key=run_key, captured_at=captured_at,
            reason=REASON_CATALOG_EMPTY, source_fresh=fresh, source_trade_date=trade_day,
            note="板块目录里的条目全部无法识别(无 code) → 不落空快照",
        )

    pool_coverage_complete = False
    # ⑤ 涨停池。
    if not callable(fetch_pool):
        pool_state, pool_codes, pool_day, pool_basis = POOL_UNKNOWN, None, None, None
        pool_error = None
        notes.append("pool_fetch_not_injected")
    else:
        try:
            pool_raw = fetch_pool(target)
        except Exception as exc:  # noqa: BLE001
            pool_state, pool_codes, pool_day, pool_basis = POOL_FAILED, None, None, None
            pool_error = f"pool_fetch_failed:{type(exc).__name__}"
            notes.append(pool_error)
        else:
            pool_state, pool_codes, pool_day, pool_basis, pool_notes = _parse_pool(
                pool_raw, day=target
            )
            pool_error = None
            notes.extend(pool_notes)
            if pool_state == POOL_OK:
                declarations = [row.get("pool_coverage") for row in pool_raw]
                first_coverage = declarations[0]
                if isinstance(first_coverage, dict):
                    requested = first_coverage.get("requested_codes")
                    fetched = first_coverage.get("fetched_codes")
                    failures = first_coverage.get("batch_failures")
                    # 仅校验源自身声明是否一致，不证明市场覆盖真实完整。
                    pool_coverage_complete = (
                        first_coverage.get("status") == "complete"
                        and isinstance(requested, int) and not isinstance(requested, bool)
                        and isinstance(fetched, int) and not isinstance(fetched, bool)
                        and isinstance(failures, int) and not isinstance(failures, bool)
                        and requested > 0 and fetched >= requested and failures == 0
                        and all(value == first_coverage for value in declarations)
                    )
                else:
                    pool_coverage_complete = all(value == "complete" for value in declarations)
                if not pool_coverage_complete:
                    notes.append("pool_coverage_unknown")
        if pool_state != POOL_OK:
            notes.append(f"pool_state:{pool_state}")

    ok = sum(1 for b in boards if b.state == STATE_OK)
    empty = sum(1 for b in boards if b.state == STATE_EMPTY)
    failed = sum(1 for b in boards if b.state == STATE_FAILED)

    # 完整 = 每个板块都是 ok 且**自报**目标日的日期/依据 + 池子同日可证 + 源自报新鲜
    # 且源交易日对齐 + 目录无异常。任何一项不成立就只是 partial。
    complete = (
        ok == len(boards)
        and failed == 0
        and empty == 0
        and pool_state == POOL_OK
        and pool_coverage_complete
        and fresh is True
        and trade_day == target
        and pool_basis not in (None, BASIS_UNKNOWN)
        and not catalog_anomaly
        and not scope_limited
        and all(b.data_day == target and b.date_basis for b in boards)
    )
    status = STATUS_COMPLETE if complete else STATUS_PARTIAL
    if status == STATUS_PARTIAL and not notes:
        notes.append("partial:inputs_not_self_declared_complete")

    return CaptureOutcome(
        day=target,
        run_key=run_key,
        status=status,
        captured_at=captured_at,
        reason=None,
        source_fresh=fresh,
        source_trade_date=trade_day,
        boards=tuple(boards),
        boards_ok=ok,
        boards_empty_ok=empty,
        boards_fetch_failed=failed,
        pool_state=pool_state,
        pool_count=(len(pool_codes) if pool_codes is not None else None),
        pool_data_day=pool_day,
        pool_date_basis=pool_basis,
        pool_error=pool_error,
        notes=tuple(notes),
        _pool_codes=(pool_codes if pool_state == POOL_OK else None),
    )


# ── 落库(库是显式参数; 本模块不建连接、不读环境) ────────────────────────
def capture_day(bind, **kwargs) -> CaptureOutcome:
    """编排 + 只追加落库。`bind` 必须是调用方显式给的 Engine / Connection。

    同日补采 = 新的 attempt(旧批次不动); 同一 `run_key`(同日同槽位)重跑是幂等的,
    由 store 的显式查询负责, 这里不重试、不吞异常。
    """
    if bind is None:
        raise ValueError(
            "capture_day 需要显式传入 Engine/Connection(本模块不解析配置、不自建连接)"
        )
    outcome = plan_capture(**kwargs)
    from src.core.theme_evolution_store import append_observation

    append = append_observation(bind, outcome.as_store_batch())
    notes = outcome.notes
    if append.status is not None and append.status != outcome.status:
        # store 只看实际写入的板块行, 目录异常(重复/非法条目)它看不到 → 记差异, 不掩盖。
        notes = notes + (f"persisted_status_differs:{append.status}",)
    return replace(outcome, notes=notes, append=append)
