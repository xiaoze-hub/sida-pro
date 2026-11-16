"""原型: A 题材观察层的**批次证据**存储(只追加) —— 不是生产接线。

## 为什么是原型

现有 `theme_board_daily` / `theme_limitup_daily` 的语义是「一天一套事实」: 唯一键
`(day, board_code)`, 写入走「存在则 setattr 覆盖 + 涨停池先 DELETE 整天再整批 add」。
于是同一天第二次运行会**原地抹掉**第一次的证据, 双方都返回 success; 一次瞬时故障
(网关抖一下)还会把当天永久写死成 `fetch_failed`。缺的不是并发技巧, 而是**运行
(attempt)这个维度**: 证据属于某一次采集, 而不是属于「今天」。

本模块只做一件事: 证明「按批次追加」这套模型在 SQLite / PostgreSQL 上都能落成
**只追加、可幂等、失败原子回滚**的存储。它刻意不接进主链路 —— 主链路的行为变更
要另起改造(读侧选哪一次 run、报告头部 provenance、迁移 252), 不在原型范围内。

## 硬约束(违反即视为 bug)

1. **只有显式传入的隔离 engine / Connection**: 模块不解析环境变量、不构造引擎、
   不 import 仓库的数据库方言层。调用方给哪个库就写哪个库, 绝不「自动找生产」。
2. **独立 `MetaData`**, 表名统一 `theme_obs_` 前缀, 与 251 的生产表不同名 ——
   任何人误对本模块的 MetaData 做 `create_all`, 也不会在真库里建出生产表。
3. **只有 INSERT, 没有 UPDATE 证据的路径**。run 行的最终状态在**插入前就算好**
   (采集在内存里完成), 因此不存在 `running → complete` 这种后天改写; 也不存在
   僵尸 run 行。
4. **不靠 `INSERT OR IGNORE` / `ON CONFLICT DO NOTHING` 遮蔽冲突**: 幂等只由
   `run_key` 唯一键 + 日级锁内的显式查询负责。明细出现唯一冲突 = 真 bug, 必须
   抛 `EvidenceConflictError` 并整轮回滚, 不许静默丢行。
5. **一次写入 = 一个事务**: 拿日锁 → 查 run_key → 取 attempt_seq → INSERT run →
   INSERT 明细 → 同事务复核行数 → COMMIT。任何异常都 `rollback()`, run 行一并消失,
   失败**不烧 attempt_seq**。
6. **`None ≠ [] ≠ 0`**: `fetch_failed` 的成员存 NULL、`empty_ok` 存 `"[]"`、
   涨停池没取到时 `limit_up_count` 留 NULL(不是 0)。三者在库里可区分。

## 与并发的关系

判重的 SELECT 与写入必须在**同一个事务、同一把日级锁**下: PostgreSQL 用
`pg_advisory_xact_lock`, SQLite 用 `BEGIN IMMEDIATE`, 其它方言**拒绝写入**
(宁可用不了, 也不留无锁写路径)。锁 key 由日期 + 固定命名空间经 blake2b 派生,
不用 Python `hash()`(带进程随机化, 跨进程算不同 key, 锁等于没加)。

## 只用不写的能力边界

本模块不含抓取、不含报告、不做用户数据, 也不判断「哪些板块算题材」; 它只是把调用方
已经采到的证据原样、可追责地存下来。

## 设计限制(原型边界, 别当成已完成)

- **PostgreSQL 验证范围有限**: 已在 Unix socket 隔离临时实例测试正常连接并发追加、
  结果已知的重试、注入失败回滚和拒绝 AUTOCOMMIT；commit 中途断连的未知结果、
  生产迁移、TimescaleDB、多进程和权限尚未验收。
- **不接主链路**: 读侧该取哪一次 run、报告头部 provenance、迁移 252、`build_payload`
  的 run 化改造都没做。本模块被调用前, 生产行为一行没变。
- **锁粒度**: PG 同日串行、跨日可并行；SQLite `BEGIN IMMEDIATE` 是库级写锁，跨日也会互斥。阻塞式加锁，无 `lock_busy` 快速失败；调度路径在完成超时保护前不得启用。
- **`attempt_cap` 不写 refused 行**: 超限只在返回值里给 `reason=attempt_cap`, 库里
  不多写。报告里的方案是"记一条 refused run 留痕", 这里选了"不写", 因为重试风暴
  下每来一次就多一行, 会把"证据只增"变成"日志只增"。要留痕应由调用方记日志。
- **没有 UPDATE, 也没有 DELETE**: 连"修正某次 run"的通道都不存在。错了只能再追加
  一个批次(读侧取最新)—— 这是刻意的, 可覆写就会重新出现"证据被抹掉"。
- **无外键**(与 251 的既有风格一致): 明细与 run 的从属关系由同事务行数复核 + 应用层
  保证, 不靠数据库级联。
- **只接受 Engine / Connection**: 不收 ORM Session。路由会话的第一条 SELECT 会把
  事务绑到只读副本(见仓库的 `pin_write_bind` 先例), 本原型干脆用显式绑定的引擎,
  从根上不给这个坑留位置。
- **`captured_at` 是证据时点, `finished_at` 是墙钟写入时刻**: 两者不可互相代替,
  报告用前者的新鲜度、审计看后者的耗时。

"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

__all__ = [
    "AppendOutcome",
    "BOARD_EMPTY",
    "BOARD_FAILED",
    "BOARD_OK",
    "BoardEvidence",
    "DAY_LOCK_NS",
    "EvidenceConflictError",
    "EvidenceError",
    "LimitUpEvidence",
    "MAX_ATTEMPTS_PER_DAY",
    "MIN_COVERAGE_FOR_RANKING",
    "ObservationBatch",
    "POOL_FAILED",
    "POOL_OK",
    "POOL_UNKNOWN",
    "STATUS_COMPLETE",
    "STATUS_PARTIAL",
    "STATUS_REFUSED",
    "THEME_OBS_BOARD",
    "THEME_OBS_LIMITUP",
    "THEME_OBS_METADATA",
    "THEME_OBS_RUN",
    "acquire_day_lock",
    "append_observation",
    "create_schema",
    "day_lock_key",
    "evidence_for_run",
    "latest_evidence",
    "runs_for_day",
]

_CST = ZoneInfo("Asia/Shanghai")

# ── 常量 ────────────────────────────────────────────────────────────────
#: 板块成分三态, 与 251 表同名同义(ok / empty_ok / fetch_failed)。
BOARD_OK = "ok"
BOARD_EMPTY = "empty_ok"
BOARD_FAILED = "fetch_failed"
_BOARD_STATES = (BOARD_OK, BOARD_EMPTY, BOARD_FAILED)

#: 涨停池状态。`unknown` = 没取到(不允许当 0 只涨停), 与 `ok` 严格区分。
POOL_OK = "ok"
POOL_UNKNOWN = "unknown"
POOL_FAILED = "fetch_failed"
_POOL_STATES = (POOL_OK, POOL_UNKNOWN, POOL_FAILED)

#: run 状态。refused = 本轮明确拒绝写入(源不新鲜/交易日错配等), 零明细行。
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_REFUSED = "refused"

#: 历史探索参数；不是有效性门槛，不用于定义批次完整性。
MIN_COVERAGE_FOR_RANKING = 0.5

#: 同日最多几次批次。超过不再追加(防重试风暴把一天写成几百个 run), 返回 attempt_cap。
MAX_ATTEMPTS_PER_DAY = 6

#: 日级锁命名空间, 与其它表的 advisory lock 键错开。
DAY_LOCK_NS = "theme_obs_store"

_DAY_RE = re.compile(r"^\d{8}$")
_RUN_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


# ── 异常 ────────────────────────────────────────────────────────────────
class EvidenceError(Exception):
    """批次证据不合法 / 无法安全写入。**在写任何一行之前**抛出。"""


class EvidenceConflictError(EvidenceError):
    """唯一键冲突(已经回滚)。正常幂等走 run_key 显式查询, 走到这里说明是真 bug。"""


# ── 独立 MetaData + 表(前缀 theme_obs_, 与生产表零重名) ─────────────────
THEME_OBS_METADATA = MetaData()

THEME_OBS_RUN = Table(
    "theme_obs_run",
    THEME_OBS_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("day", String(8), nullable=False),
    Column("attempt_seq", Integer, nullable=False),
    Column("run_key", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("input_partial", Boolean, nullable=False),
    Column("capture_notes", String, nullable=True),
    Column("rule_version", String(32), nullable=False),
    Column("source", String(16), nullable=False),
    Column("source_fresh", Boolean, nullable=True),
    Column("source_trade_date", String(8), nullable=True),
    Column("captured_at", DateTime, nullable=False),
    Column("finished_at", DateTime, nullable=True),
    Column("boards_total", Integer, nullable=False),
    Column("boards_ok", Integer, nullable=False),
    Column("boards_empty_ok", Integer, nullable=False),
    Column("boards_fetch_failed", Integer, nullable=False),
    Column("pool_state", String(16), nullable=True),
    Column("pool_data_day", String(8), nullable=True),
    Column("pool_date_basis", String(32), nullable=True),
    Column("limit_up_count", Integer, nullable=True),
    Column("refused_reason", String(64), nullable=True),
    Column("error", String(255), nullable=True),
    UniqueConstraint("day", "attempt_seq", name="uq_theme_obs_run_day_attempt"),
    UniqueConstraint("run_key", name="uq_theme_obs_run_key"),
    Index("ix_theme_obs_run_day", "day", "attempt_seq"),
)

THEME_OBS_BOARD = Table(
    "theme_obs_board",
    THEME_OBS_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, nullable=False),
    Column("day", String(8), nullable=False),
    Column("board_code", String(16), nullable=False),
    Column("board_name", String(64), nullable=True),
    Column("board_type", String(16), nullable=True),
    Column("data_day", String(8), nullable=True),
    Column("date_basis", String(32), nullable=True),
    Column("source", String(16), nullable=False),
    Column("state", String(16), nullable=False),
    Column("member_count", Integer, nullable=True),
    Column("members_json", String, nullable=True),
    Column("has_quote", Boolean, nullable=True),
    Column("fetched_at", DateTime, nullable=False),
    Column("error", String(255), nullable=True),
    UniqueConstraint("run_id", "board_code", name="uq_theme_obs_board_run_code"),
    Index("ix_theme_obs_board_day", "day"),
    Index("ix_theme_obs_board_run", "run_id"),
)

THEME_OBS_LIMITUP = Table(
    "theme_obs_limitup",
    THEME_OBS_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, nullable=False),
    Column("day", String(8), nullable=False),
    Column("stock_code", String(16), nullable=False),
    Column("stock_name", String(64), nullable=True),
    Column("sector", String(64), nullable=True),
    Column("concepts_json", String, nullable=True),
    Column("block_as_of", String(8), nullable=True),
    Column("block_source", String(32), nullable=True),
    Column("source", String(16), nullable=False),
    Column("fetched_at", DateTime, nullable=False),
    UniqueConstraint("run_id", "stock_code", name="uq_theme_obs_limitup_run_code"),
    Index("ix_theme_obs_limitup_day", "day"),
    Index("ix_theme_obs_limitup_run", "run_id"),
)


# ── 输入模型(纯数据, 无副作用) ─────────────────────────────────────────
@dataclass(frozen=True)
class BoardEvidence:
    """单个板块的一次采集结果。

    `members=None` 与 `members=()` 不同: 前者是取不到(fetch_failed), 后者是
    返回空列表(empty_ok)，但不证明实际没有成员或接口成功。
    """

    board_code: str
    state: str
    board_name: str | None = None
    board_type: str | None = None
    data_day: str | None = None
    date_basis: str | None = None
    members: Sequence[str] | None = None
    has_quote: bool | None = None
    error: str | None = None


@dataclass(frozen=True)
class LimitUpEvidence:
    """一只涨停证券 + 采集时刻能拿到的题材归属(同源同行, 不跨源拼)。

    `concepts=None` 是"没取到归属"(不是"没有概念")；`concepts=()` 是源明确给了空集。
    `block_as_of` 是**归属反查的日期**: TQ 的板块成员关系只有"此刻", 所以它记录的是
    采集当日, 不代表该证券在目标日的历史题材归属 —— 消费方必须显式标注这一点。
    """

    stock_code: str
    stock_name: str | None = None
    sector: str | None = None
    concepts: Sequence[str] | None = None
    block_as_of: str | None = None
    block_source: str | None = None


@dataclass(frozen=True)
class ObservationBatch:
    """一次采集(= 一个批次 = 一个 run)的全部证据, 采集已在内存里完成。"""

    day: str
    run_key: str
    rule_version: str
    source: str
    captured_at: datetime
    boards: Sequence[BoardEvidence] = field(default_factory=tuple)
    limit_up: Sequence[LimitUpEvidence] = field(default_factory=tuple)
    pool_state: str = POOL_UNKNOWN
    pool_data_day: str | None = None
    pool_date_basis: str | None = None
    input_partial: bool = False
    capture_notes: tuple[str, ...] = ()
    source_fresh: bool | None = None
    # 日线日期不等于板块成分日期；每个板块需单列 data_day/date_basis。
    source_trade_date: str | None = None
    refused_reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class AppendOutcome:
    """追加结果。`inserted=False` 时**一行都没写**, 且能说清为什么。"""

    inserted: bool
    reason: str | None
    day: str
    run_key: str
    run_id: int | None = None
    attempt_seq: int | None = None
    status: str | None = None
    coverage: float | None = None
    pool_state: str | None = None
    boards_written: int = 0
    limit_up_written: int = 0


@dataclass(frozen=True)
class _Plan:
    """校验之后的落库计划: 状态、计数、明细行全部在**写第一行之前**定型。"""

    day: str
    run_key: str
    rule_version: str
    source: str
    captured_at: datetime
    status: str
    coverage: float | None
    pool_state: str | None
    pool_data_day: str | None
    pool_date_basis: str | None
    input_partial: bool
    capture_notes: str | None
    limit_up_count: int | None
    source_fresh: bool | None
    source_trade_date: str | None
    refused_reason: str | None
    error: str | None
    board_rows: tuple[dict[str, Any], ...]
    pool_rows: tuple[dict[str, Any], ...]
    boards_ok: int
    boards_empty_ok: int
    boards_fetch_failed: int


# ── 校验(全部在写入之前, 失败一行都不写) ───────────────────────────────
def _require_day(day: Any) -> str:
    if not isinstance(day, str) or not _DAY_RE.match(day):
        raise EvidenceError(f"day 必须是 YYYYMMDD 字符串, 收到 {day!r}")
    try:
        datetime.strptime(day, "%Y%m%d")
    except ValueError as exc:
        raise EvidenceError(f"day 不是合法日期: {day!r}") from exc
    return day


def _require_run_key(run_key: Any) -> str:
    if not isinstance(run_key, str):
        raise EvidenceError("run_key 必须是字符串")
    key = run_key.strip()
    if not key:
        raise EvidenceError("run_key 不能为空: 它是幂等键, 空值等于关掉幂等")
    if not _RUN_KEY_RE.match(key):
        raise EvidenceError(
            f"run_key 只允许字母/数字/_.:- 且长度 ≤64, 收到 {run_key!r}"
        )
    return key


def _require_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} 不能为空")
    value = value.strip()
    if len(value) > limit:
        raise EvidenceError(f"{name} 超长(>{limit}): {value!r}")
    return value


def _naive_beijing(moment: datetime) -> datetime:
    """统一成「北京时间的 naive datetime」, 与 251 表的历史口径一致。

    带时区的输入按真实时区换算, 不是硬丢 tzinfo(硬丢会静默挪动 8 小时)。
    """
    if not isinstance(moment, datetime):
        raise EvidenceError("captured_at 必须是 datetime")
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(_CST).replace(tzinfo=None)


def _board_rows(
    boards: Sequence[BoardEvidence], *, day: str, source: str, fetched_at: datetime
) -> tuple[tuple[dict[str, Any], ...], tuple[int, int, int]]:
    if not isinstance(boards, (list, tuple)):
        raise EvidenceError("boards 必须是序列")
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    ok = empty = failed = 0
    for item in boards:
        code = _require_text(getattr(item, "board_code", None), "board_code", 16)
        if code in seen:
            raise EvidenceError(f"板块代码重复: {code!r}(重复会让 member_count 与覆盖率失真)")
        seen.add(code)

        state = getattr(item, "state", None)
        if state not in _BOARD_STATES:
            raise EvidenceError(
                f"板块 {code} 的 state 必须是 {_BOARD_STATES} 之一, 收到 {state!r}"
            )
        members = getattr(item, "members", None)
        if state == BOARD_OK:
            if not isinstance(members, (list, tuple)) or not members:
                raise EvidenceError(
                    f"板块 {code}: state=ok 必须带非空 members"
                    "(确为空集用 empty_ok, 取不到用 fetch_failed)"
                )
            codes = list(members)
            if len(set(codes)) != len(codes):
                raise EvidenceError(f"板块 {code}: members 里有重复证券代码")
            for c in codes:
                _require_text(c, "member code", 16)
            member_count: int | None = len(codes)
            members_json: str | None = json.dumps(codes, ensure_ascii=False)
            ok += 1
        elif state == BOARD_EMPTY:
            if not isinstance(members, (list, tuple)) or members:
                raise EvidenceError(
                    f"板块 {code}: state=empty_ok 表示源确认为空, 不得携带 members"
                )
            member_count, members_json = 0, "[]"
            empty += 1
        else:  # BOARD_FAILED
            if members is not None:
                raise EvidenceError(
                    f"板块 {code}: state=fetch_failed 表示取不到, members 必须是 None"
                    "(不能把\"取不到\"写成\"没有成员\")"
                )
            member_count, members_json = None, None
            failed += 1

        data_day = getattr(item, "data_day", None)
        date_basis = getattr(item, "date_basis", None)
        if (data_day is None) != (date_basis is None):
            raise EvidenceError("板块 data_day 与 date_basis 必须同时提供或同时未知")
        if data_day is not None:
            data_day = _require_day(data_day)
            date_basis = _require_text(date_basis, "date_basis", 32)
        rows.append(
            {
                "day": day,
                "board_code": code,
                "board_name": getattr(item, "board_name", None),
                "board_type": getattr(item, "board_type", None),
                "data_day": data_day, "date_basis": date_basis,
                "source": source,
                "state": state,
                "member_count": member_count,
                "members_json": members_json,
                "has_quote": getattr(item, "has_quote", None),
                "fetched_at": fetched_at,
                "error": getattr(item, "error", None),
            }
        )
    return tuple(rows), (ok, empty, failed)


def _pool_rows(
    limit_up: Sequence[LimitUpEvidence], *, day: str, source: str, fetched_at: datetime
) -> tuple[dict[str, Any], ...]:
    if not isinstance(limit_up, (list, tuple)):
        raise EvidenceError("limit_up 必须是序列")
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in limit_up:
        code = _require_text(getattr(item, "stock_code", None), "stock_code", 16)
        if code in seen:
            raise EvidenceError(f"涨停池证券代码重复: {code!r}")
        seen.add(code)

        sector = getattr(item, "sector", None)
        sector = _require_text(sector, "sector", 64) if sector is not None else None

        raw_concepts = getattr(item, "concepts", None)
        concepts_json: str | None = None
        if raw_concepts is not None:
            if not isinstance(raw_concepts, (list, tuple)):
                raise EvidenceError(f"{code}: concepts 必须是序列或 None(未取到)")
            cleaned: list[str] = []
            for raw in raw_concepts:
                name = _require_text(raw, "concept", 64)
                if name not in cleaned:
                    cleaned.append(name)
            concepts_json = json.dumps(cleaned, ensure_ascii=False)

        block_as_of = getattr(item, "block_as_of", None)
        block_as_of = _require_day(block_as_of) if block_as_of is not None else None
        block_source = getattr(item, "block_source", None)
        block_source = (
            _require_text(block_source, "block_source", 32) if block_source is not None else None
        )

        rows.append(
            {
                "day": day,
                "stock_code": code,
                "stock_name": getattr(item, "stock_name", None),
                "sector": sector,
                "concepts_json": concepts_json,
                "block_as_of": block_as_of,
                "block_source": block_source,
                "source": source,
                "fetched_at": fetched_at,
            }
        )
    return tuple(rows)


def _build_plan(batch: ObservationBatch) -> _Plan:
    """把批次校验成本次写入的完整计划; 拒绝的情况在这里就结束, 不会留下半套行。"""
    if not isinstance(batch, ObservationBatch):
        raise EvidenceError("batch 必须是 ObservationBatch")

    day = _require_day(batch.day)
    run_key = _require_run_key(batch.run_key)
    rule_version = _require_text(batch.rule_version, "rule_version", 32)
    source = _require_text(batch.source, "source", 16)
    captured_at = _naive_beijing(batch.captured_at)
    if not isinstance(batch.input_partial, bool):
        raise EvidenceError("input_partial 必须为布尔值")
    if not isinstance(batch.capture_notes, (list, tuple)) or any(
            not isinstance(n, str) or len(n) > 255 for n in batch.capture_notes):
        raise EvidenceError("capture_notes 必须是短文本序列")
    capture_notes = json.dumps(list(batch.capture_notes), ensure_ascii=False) if batch.capture_notes else None
    if captured_at.strftime("%Y%m%d") < day:
        raise EvidenceError("采集时刻早于目标日，不能作为该日证据")

    refused_reason = batch.refused_reason
    if refused_reason is not None:
        if not isinstance(refused_reason, str) or not refused_reason.strip():
            raise EvidenceError("refused_reason 必须是非空字符串(说明为什么拒绝本轮写入)")
        refused_reason = refused_reason.strip()
        if len(refused_reason) > 64:
            raise EvidenceError("refused_reason 超长(>64)")
        if batch.boards or batch.limit_up:
            raise EvidenceError(
                "refused 轮次不得携带证据: 要么写证据, 要么明确拒绝 —— "
                "带证据却记 refused 会让库里出现\"被拒绝但仍有数据\"的自相矛盾行"
            )
        if batch.pool_state not in (POOL_UNKNOWN, None):
            raise EvidenceError("refused 轮次的 pool_state 只能是 unknown")

    fresh = batch.source_fresh
    if fresh is not None and not isinstance(fresh, bool):
        raise EvidenceError("source_fresh 只能是 True/False/None(未知)")

    source_trade_date = batch.source_trade_date
    if source_trade_date is not None:
        source_trade_date = _require_day(source_trade_date)

    # 源不新鲜 / 源交易日与目标日不符: 只允许"显式拒绝", 不允许"照常写入" ——
    # 否则昨天的成分会被当成今天的证据, 而报告里看不出这一点。
    if refused_reason is None:
        if fresh is False:
            raise EvidenceError(
                "source_fresh=False(源不新鲜)必须显式 refused: "
                "请置 refused_reason 并清空证据, 不得照常写入"
            )
        if source_trade_date is not None and source_trade_date != day:
            raise EvidenceError(
                f"source_trade_date={source_trade_date} 与 day={day} 不符, 必须显式 refused"
            )

    pool_data_day = _require_day(batch.pool_data_day) if batch.pool_data_day is not None else None
    pool_date_basis = (_require_text(batch.pool_date_basis, "pool_date_basis", 32)
                       if batch.pool_date_basis is not None else None)
    if batch.pool_state == POOL_OK and (pool_data_day != day or pool_date_basis is None):
        raise EvidenceError("pool_state=ok 需要明确的同日来源日期和日期依据；来源声明不等于独立验证")

    if refused_reason is not None:
        pool_state, limit_up_count = POOL_UNKNOWN, None
        pool_rows: tuple[dict[str, Any], ...] = ()
        board_rows: tuple[dict[str, Any], ...] = ()
        counts = (0, 0, 0)
        status = STATUS_REFUSED
        coverage: float | None = None
    else:
        pool_state = batch.pool_state
        if pool_state not in _POOL_STATES:
            raise EvidenceError(f"pool_state 必须是 {_POOL_STATES} 之一, 收到 {pool_state!r}")
        pool_rows = _pool_rows(batch.limit_up, day=day, source=source, fetched_at=captured_at)
        if pool_state == POOL_OK:
            if not pool_rows:
                raise EvidenceError(
                    "pool_state=ok 却没有证券: 空池必须记 unknown/fetch_failed, 不许写 0"
                )
            limit_up_count: int | None = len(pool_rows)
        else:
            if pool_rows:
                raise EvidenceError(
                    f"pool_state={pool_state} 不得携带证券行(取不到就不能写代码)"
                )
            limit_up_count = None  # None = 没取到, 与 0(一只都没涨停)严格区分

        board_rows, counts = _board_rows(
            batch.boards, day=day, source=source, fetched_at=captured_at
        )
        coverage = (counts[0] / len(board_rows)) if board_rows else None
        if not board_rows:
            status = STATUS_PARTIAL  # 没有任何板块证据 ≠ complete
        elif (not batch.input_partial and coverage == 1.0 and pool_state == POOL_OK
              and fresh is True and source_trade_date == day
              and all(r["data_day"] == day and r["date_basis"] for r in board_rows)):
            # 仅表示输入字段自报齐备；不代表市场覆盖、时点或来源真实性已独立验证。
            status = STATUS_COMPLETE
        else:
            status = STATUS_PARTIAL

    error = batch.error
    if error is not None and (not isinstance(error, str) or len(error) > 255):
        raise EvidenceError("error 必须是 <=255 字符的字符串")

    return _Plan(
        day=day,
        run_key=run_key,
        rule_version=rule_version,
        source=source,
        captured_at=captured_at,
        status=status,
        coverage=coverage,
        pool_state=pool_state,
        pool_data_day=pool_data_day,
        pool_date_basis=pool_date_basis,
        input_partial=batch.input_partial,
        capture_notes=capture_notes,
        limit_up_count=limit_up_count,
        source_fresh=fresh,
        source_trade_date=source_trade_date,
        refused_reason=refused_reason,
        error=error,
        board_rows=board_rows,
        pool_rows=pool_rows,
        boards_ok=counts[0],
        boards_empty_ok=counts[1],
        boards_fetch_failed=counts[2],
    )


# ── 日级锁 ──────────────────────────────────────────────────────────────
def day_lock_key(day: str) -> int:
    """`YYYYMMDD` → 稳定的 64 位整数(PG advisory lock key)。

    不用 Python `hash()`: 它带进程随机化, 两个进程会算出不同 key, 锁等于没加。
    """
    digest = hashlib.blake2b(f"{DAY_LOCK_NS}:{day}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _lock_plan(dialect: str) -> tuple[str, str]:
    """按**真实连接的方言**给出日锁动作。其它方言直接拒绝写入。"""
    if dialect == "postgresql":
        return "pg_advisory", "SELECT pg_advisory_xact_lock(:k)"
    if dialect == "sqlite":
        return "begin_immediate", "BEGIN IMMEDIATE"
    raise EvidenceError(
        f"不支持的后端方言 {dialect!r}: 没有日级锁就无法保证只追加, 拒绝写入"
    )


def acquire_day_lock(conn: Connection, day: str) -> str:
    """在事务的**第一条语句**位置取得该交易日的数据库级写锁, 返回方言名。

    - PostgreSQL: `pg_advisory_xact_lock(key)`, 事务结束自动释放(提交/回滚都释放)。
    - SQLite: `BEGIN IMMEDIATE`, 立即拿库级写锁, 而不是等第一条 INSERT 才升级
      (升级时可能直接 SQLITE_BUSY)。已经在事务里再 `BEGIN` 只会得到驱动层乱码,
      所以这里显式拒绝, 要求调用方在干净连接上先加锁。
    """
    dialect = conn.dialect.name
    kind, sql = _lock_plan(dialect)
    if kind == "pg_advisory":
        conn.execute(text(sql), {"k": day_lock_key(day)})
        return dialect

    driver = getattr(conn.connection, "driver_connection", None)
    if getattr(driver, "in_transaction", False):
        raise EvidenceError(
            "SQLite 日锁必须是事务的第一条语句: 连接上已有未结束的事务, "
            "此时 BEGIN IMMEDIATE 无法生效(会在第一条写语句处才升级锁)"
        )
    conn.exec_driver_sql(sql)
    return dialect


# ── DDL(显式传入 engine, 只动自己的 MetaData) ───────────────────────────
def create_schema(bind: Engine | Connection) -> None:
    """在**调用方给的库**上建原型表。生产库不该调用它, 调了也只会多出前缀表。"""
    if not isinstance(bind, (Engine, Connection)):
        raise EvidenceError("create_schema 需要显式的 Engine 或 Connection")
    THEME_OBS_METADATA.create_all(bind, checkfirst=True)


# ── 写入 ────────────────────────────────────────────────────────────────
def _select_run_by_key(conn: Connection, run_key: str) -> Mapping[str, Any] | None:
    stmt = select(THEME_OBS_RUN).where(THEME_OBS_RUN.c.run_key == run_key)
    return conn.execute(stmt).mappings().first()


def _next_attempt_seq(conn: Connection, day: str) -> int:
    stmt = select(func.coalesce(func.max(THEME_OBS_RUN.c.attempt_seq), 0)).where(
        THEME_OBS_RUN.c.day == day
    )
    return int(conn.execute(stmt).scalar_one()) + 1


def _insert_detail_rows(
    conn: Connection,
    *,
    run_id: int,
    board_rows: Sequence[dict[str, Any]],
    pool_rows: Sequence[dict[str, Any]],
) -> None:
    """明细只用普通 INSERT: 冲突是真 bug, 静默跳过会掩盖证据丢失。"""
    if board_rows:
        conn.execute(
            insert(THEME_OBS_BOARD),
            [dict(row, run_id=run_id) for row in board_rows],
        )
    if pool_rows:
        conn.execute(
            insert(THEME_OBS_LIMITUP),
            [dict(row, run_id=run_id) for row in pool_rows],
        )


def _verify_written_counts(
    conn: Connection,
    *,
    run_id: int,
    day: str,
    expected_boards: int,
    expected_pool: int,
) -> None:
    """同事务复核: 行数对不对、有没有串日。不符就抛, 让整个事务回滚。"""
    got_boards = conn.execute(
        select(func.count()).select_from(THEME_OBS_BOARD).where(THEME_OBS_BOARD.c.run_id == run_id)
    ).scalar_one()
    got_pool = conn.execute(
        select(func.count())
        .select_from(THEME_OBS_LIMITUP)
        .where(THEME_OBS_LIMITUP.c.run_id == run_id)
    ).scalar_one()
    if got_boards != expected_boards or got_pool != expected_pool:
        raise EvidenceError(
            f"run {run_id} 明细行数不符: boards {got_boards} != {expected_boards}, "
            f"limitup {got_pool} != {expected_pool}"
        )
    stray = conn.execute(
        select(func.count())
        .select_from(THEME_OBS_BOARD)
        .where(and_(THEME_OBS_BOARD.c.run_id == run_id, THEME_OBS_BOARD.c.day != day))
    ).scalar_one()
    if stray:
        raise EvidenceError(f"run {run_id} 有 {stray} 行板块明细的 day 与 run 不一致")


def _run_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """run 行的对外视图: id → run_id, 并补上派生的 coverage(None = 没有证据)。"""
    data = dict(row)
    run_id = data.pop("id")
    total = data.get("boards_total") or 0
    ok = data.get("boards_ok") or 0
    data["run_id"] = run_id
    data["coverage"] = (ok / total) if total else None
    return data


def _open_bind(bind: Engine | Connection) -> tuple[Connection, bool]:
    if isinstance(bind, Engine):
        return bind.connect(), True
    if isinstance(bind, Connection):
        return bind, False
    raise EvidenceError(
        f"必须显式传入隔离的 Engine 或 Connection, 收到 {type(bind).__name__}"
    )


def append_observation(bind: Engine | Connection, batch: ObservationBatch) -> AppendOutcome:
    """追加一个批次的证据(原子、幂等、不覆盖)。

    返回 `inserted=False` 的两种情形, 都**一行不写**:
    - `run_key_exists`: 同一个 run_key 已提交过(重试/断连重连), 直接报既有状态;
    - `attempt_cap`: 当日批次数已达上限(防重试风暴)。

    异常路径一定 `rollback()`, run 行与明细一并消失 —— 失败不烧 attempt_seq,
    下一次重跑仍然是同一序号。
    """
    # 先校验(纯内存, 一行不写), 再开绑定: 校验失败不该占用/泄漏一条连接。
    plan = _build_plan(batch)
    conn, owns_conn = _open_bind(bind)
    # 不得接管调用方已有事务：函数会 commit/rollback，只能使用干净连接。
    # 检查必须在 try/except 之外，否则拒绝时反而 rollback 了调用方事务。
    if conn.in_transaction():
        if owns_conn:
            conn.close()
        raise EvidenceError("已有事务的连接不可用于独立批次写入")
    if conn.dialect.name == "postgresql":
        driver = getattr(conn.connection, "driver_connection", None)
        # xact advisory lock 在 autocommit 下随语句结束释放，不能保护后续 INSERT。
        # 无法判断驱动状态时也拒绝，不能静默降级为无事务写入。
        if getattr(driver, "autocommit", None) is not False:
            if owns_conn:
                conn.close()
            raise EvidenceError("PostgreSQL 连接必须使用可验证的非 AUTOCOMMIT 事务")
    try:
        acquire_day_lock(conn, plan.day)

        existing = _select_run_by_key(conn, plan.run_key)
        if existing is not None:
            # 幂等键只能代表同一份输入，不能把不同证据误报为成功重试。
            expected = {
                "day": plan.day, "rule_version": plan.rule_version, "source": plan.source,
                "source_fresh": plan.source_fresh, "source_trade_date": plan.source_trade_date,
                "captured_at": plan.captured_at, "status": plan.status,
                "input_partial": plan.input_partial, "capture_notes": plan.capture_notes,
                "pool_state": plan.pool_state, "pool_data_day": plan.pool_data_day,
                "pool_date_basis": plan.pool_date_basis, "limit_up_count": plan.limit_up_count,
                "refused_reason": plan.refused_reason, "error": plan.error,
            }
            run_id_existing = existing["id"]
            old_boards = conn.execute(select(THEME_OBS_BOARD).where(
                THEME_OBS_BOARD.c.run_id == run_id_existing)).mappings().all()
            old_pool = conn.execute(select(THEME_OBS_LIMITUP).where(
                THEME_OBS_LIMITUP.c.run_id == run_id_existing)).mappings().all()
            def canonical(rows, key):
                return sorted(({k: v for k, v in dict(r).items() if k not in ("id", "run_id")}
                               for r in rows), key=lambda row: row[key])
            if (any(existing[k] != v for k, v in expected.items())
                    or canonical(old_boards, "board_code") != canonical(plan.board_rows, "board_code")
                    or canonical(old_pool, "stock_code") != canonical(plan.pool_rows, "stock_code")):
                raise EvidenceConflictError("run_key 已存在但输入证据不同，拒绝作为重试")
            conn.rollback()  # 释放日锁, 且本轮什么都没写
            view = _run_view(existing)
            return AppendOutcome(
                inserted=False,
                reason="run_key_exists",
                day=view["day"],
                run_key=plan.run_key,
                run_id=view["run_id"],
                attempt_seq=view["attempt_seq"],
                status=view["status"],
                coverage=view["coverage"],
                pool_state=view["pool_state"],
            )

        attempt_seq = _next_attempt_seq(conn, plan.day)
        # 拒绝记录与有效/部分采集分别限额；上游持续失败不能耗尽恢复采集的配额。
        category = (THEME_OBS_RUN.c.status == STATUS_REFUSED if plan.status == STATUS_REFUSED
                    else THEME_OBS_RUN.c.status != STATUS_REFUSED)
        category_count = conn.execute(select(func.count()).select_from(THEME_OBS_RUN).where(
            THEME_OBS_RUN.c.day == plan.day, category)).scalar_one()
        if category_count >= MAX_ATTEMPTS_PER_DAY:
            conn.rollback()
            return AppendOutcome(
                inserted=False,
                reason="attempt_cap",
                day=plan.day,
                run_key=plan.run_key,
            )

        result = conn.execute(
            insert(THEME_OBS_RUN).values(
                day=plan.day,
                attempt_seq=attempt_seq,
                run_key=plan.run_key,
                status=plan.status,
                input_partial=plan.input_partial,
                capture_notes=plan.capture_notes,
                rule_version=plan.rule_version,
                source=plan.source,
                source_fresh=plan.source_fresh,
                source_trade_date=plan.source_trade_date,
                captured_at=plan.captured_at,
                # 状态在插入前就已定型: 不存在 running → complete 的后天改写。
                finished_at=datetime.now(_CST).replace(tzinfo=None),
                boards_total=len(plan.board_rows),
                boards_ok=plan.boards_ok,
                boards_empty_ok=plan.boards_empty_ok,
                boards_fetch_failed=plan.boards_fetch_failed,
                pool_state=plan.pool_state,
                pool_data_day=plan.pool_data_day,
                pool_date_basis=plan.pool_date_basis,
                limit_up_count=plan.limit_up_count,
                refused_reason=plan.refused_reason,
                error=plan.error,
            )
        )
        run_id = int(result.inserted_primary_key[0])

        _insert_detail_rows(
            conn,
            run_id=run_id,
            board_rows=plan.board_rows,
            pool_rows=plan.pool_rows,
        )
        _verify_written_counts(
            conn,
            run_id=run_id,
            day=plan.day,
            expected_boards=len(plan.board_rows),
            expected_pool=len(plan.pool_rows),
        )
        conn.commit()
    except IntegrityError as exc:
        conn.rollback()
        raise EvidenceConflictError("批次写入约束冲突，已回滚") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()

    return AppendOutcome(
        inserted=True,
        reason=None,
        day=plan.day,
        run_key=plan.run_key,
        run_id=run_id,
        attempt_seq=attempt_seq,
        status=plan.status,
        coverage=plan.coverage,
        pool_state=plan.pool_state,
        boards_written=len(plan.board_rows),
        limit_up_written=len(plan.pool_rows),
    )


# ── 读侧(只取单次 run, 绝不跨 run 拼接) ────────────────────────────────
_READABLE_STATUSES = (STATUS_COMPLETE, STATUS_PARTIAL)


def runs_for_day(conn: Connection, day: str) -> list[dict[str, Any]]:
    """该交易日的全部批次(按 attempt_seq 升序), 供报告写"当日共 N 次运行"。"""
    day = _require_day(day)
    stmt = (
        select(THEME_OBS_RUN)
        .where(THEME_OBS_RUN.c.day == day)
        .order_by(THEME_OBS_RUN.c.attempt_seq)
    )
    return [_run_view(row) for row in conn.execute(stmt).mappings()]


def evidence_for_run(conn: Connection, run_id: int) -> dict[str, Any] | None:
    """某一次 run 的完整证据(该 run 的明细, 不含任何别的 run)。"""
    run_row = conn.execute(
        select(THEME_OBS_RUN).where(THEME_OBS_RUN.c.id == int(run_id))
    ).mappings().first()
    if run_row is None:
        return None
    boards = conn.execute(
        select(THEME_OBS_BOARD)
        .where(THEME_OBS_BOARD.c.run_id == int(run_id))
        .order_by(THEME_OBS_BOARD.c.board_code)
    ).mappings()
    pool = conn.execute(
        select(THEME_OBS_LIMITUP)
        .where(THEME_OBS_LIMITUP.c.run_id == int(run_id))
        .order_by(THEME_OBS_LIMITUP.c.stock_code)
    ).mappings()
    return {
        "run": _run_view(run_row),
        "boards": [dict(r) for r in boards],
        "limit_up": [dict(r) for r in pool],
    }


def latest_evidence(conn: Connection, day: str) -> dict[str, Any] | None:
    """当日最新一次**可用**批次(complete / partial)的证据。

    规则: 只取单次 run，先按采集时间、再按写入序号排序。采集最新的批次即使是
    partial 也不拿更旧的完整批次冒充；当日只有 refused 轮次时返回 None。
    """
    day = _require_day(day)
    row = conn.execute(
        select(THEME_OBS_RUN)
        .where(
            and_(
                THEME_OBS_RUN.c.day == day,
                THEME_OBS_RUN.c.status.in_(_READABLE_STATUSES),
            )
        )
        .order_by(THEME_OBS_RUN.c.captured_at.desc(), THEME_OBS_RUN.c.attempt_seq.desc())
        .limit(1)
    ).mappings().first()
    if row is None:
        return None
    return evidence_for_run(conn, int(row["id"]))
