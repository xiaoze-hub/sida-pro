"""原型: **上游运行回执**(upstream run receipt) —— 只记录调用方的显式声明。

## 为什么需要它

题材演化/报告链路的上游(行情网关、K 线、交易日同步)跑没跑、跑成没跑成, 现在只能
靠"报告里有没有数字"倒推。倒推有两个致命问题:

1. **没有运行记录时, 旧数据看起来和新数据一模一样** —— 昨天成功的行会被当成今天
   的完成证据;
2. **"今天 15:00 过了"被当成收盘定稿** —— 时间不是收盘信号, 15:00 之后上游仍可能
   只覆盖部分板块、或只同步了指数没同步个股。

本模块只做一件事: 让调用方把"我这次运行"的**声明**原样、只追加地记下来, 并提供
**只读 readiness**。它刻意不判断这些声明是否真实。

## 硬约束(违反即视为 bug)

1. **只接受显式 Engine / Connection**: 不解析环境变量、不构造引擎、不 import 仓库
   数据库方言层; 不收 ORM Session(会话的第一条 SELECT 可能把事务绑到只读副本)。
2. **只接受显式 UTC aware 时间**: `started_at` 必填、`finished_at` 可为 None;
   naive 或非零偏移一律拒绝。模块内部不读 `datetime.now()` / `time.time()`, 不 sleep,
   不调度, 不联网。
3. **只记声明, 不验真伪**: `close_complete` / `coverage` / `source_data_day` /
   `date_basis` 全是调用方声明; 模块没有校验源系统、没有抓取、没有交叉比对。因此
   readiness 的"完成"状态叫 `complete_declared`, 且永久携带 `claims_verified=False`。
   把这里的输出当成"市场已收盘 / 数据已完整"就是误用。
4. **未知保留**: 缺 `finished_at` / `close_complete` / `coverage` / `source_data_day`
   时落库为 NULL, 读回仍是 None; 绝不填 False、当日日期或 "full"。
5. **时间不参与收盘判断**: 即使回执写着同日 15:30 完成, 只要没有明示
   `close_complete=True` 与覆盖声明, readiness 就是 `completion_unknown`。
6. **不拿旧行冒充**: 某日没有回执 ⇒ `not_run`, 不回退到前一天; 某日最新一次运行
   失败/拒绝 ⇒ `failed`/`refused`, 不回退到更早的成功回执。
7. **只追加、幂等、失败原子回滚**: 一个 `run_key` 只对应一份输入; 同输入重放返回
   `inserted=False`; 同 key 不同输入抛 `ReceiptConflictError`; 任何异常整轮回滚。
   没有 UPDATE / DELETE 通道。
8. **拒绝敏感异常内容**: 不接受 BaseException 对象(可能带 traceback/locals), 也不
   接受形如 `token=...` / `Bearer ...` / 带口令的 DSN 的文本; 拒绝消息不回显原文。
9. **独立 MetaData**: 表名统一 `theme_upstream_receipt_` 前缀, 与生产表不同名;
   生产迁移未接线, 本模块被调用前生产行为一行没变。

## 明确没做的(原型边界)

- 不建生产库、不做迁移、不接 startup / scheduler / 报告;
- 不判断"上游真的完成了"、不判断"覆盖真的完整"、不判断"日期真的来自 K 线";
- 不支持 MySQL 等第三方言(序列化追加只实现 SQLite / PostgreSQL);
- 回执乱序到达时, 顺序基准是**追加序号**而不是墙钟; readiness 会标记
  `finished_out_of_order`, 但不会猜测哪一次"更真"。

## 数据模型

- `theme_upstream_receipt_run`: 一次运行一行(状态、UTC 起止、来源/依据/收盘/覆盖声明);
- `theme_upstream_receipt_decl`: 该次运行携带的附加声明(kind/value), 只追加。

时间以 `YYYY-MM-DDTHH:MM:SS.ffffffZ` 字符串存储, 避免 SQLite 丢失时区后把 UTC
读成 naive 本地时间。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import (
    Boolean,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

__all__ = [
    "CLAIMS_VERIFIED",
    "ORDER_BASIS",
    "READINESS_COMPLETE_DECLARED",
    "READINESS_COMPLETION_UNKNOWN",
    "READINESS_FAILED",
    "READINESS_INCOMPLETE",
    "READINESS_NOT_RUN",
    "READINESS_REFUSED",
    "READINESS_TIME_UNKNOWN",
    "READINESS_WRONG_DAY",
    "REASON_DUPLICATE",
    "ReceiptBindError",
    "ReceiptConflictError",
    "ReceiptDeclaration",
    "ReceiptError",
    "ReceiptOutcome",
    "ReceiptSensitiveContentError",
    "ReceiptValidationError",
    "Readiness",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_REFUSED",
    "THEME_UPSTREAM_RECEIPT_DECL",
    "THEME_UPSTREAM_RECEIPT_METADATA",
    "THEME_UPSTREAM_RECEIPT_RUN",
    "UpstreamReceipt",
    "append_receipt",
    "create_schema",
    "declarations_for_run",
    "format_utc",
    "parse_utc",
    "read_readiness",
    "receipts_for_day",
]

#: 永远为 False: 本模块只记录调用方声明, 不验证真伪。别把 True 写进任何分支。
CLAIMS_VERIFIED = False

#: readiness 的排序基准: 追加序号, 不是墙钟(回执可能乱序到达)。
ORDER_BASIS = "append_sequence"

STATUS_OK = "ok"
STATUS_REFUSED = "refused"
STATUS_FAILED = "failed"
_STATUSES = (STATUS_OK, STATUS_REFUSED, STATUS_FAILED)

READINESS_NOT_RUN = "not_run"
READINESS_FAILED = "failed"
READINESS_REFUSED = "refused"
READINESS_WRONG_DAY = "wrong_day"
READINESS_TIME_UNKNOWN = "time_unknown"
READINESS_COMPLETION_UNKNOWN = "completion_unknown"
READINESS_INCOMPLETE = "incomplete"
READINESS_COMPLETE_DECLARED = "complete_declared"

REASON_DUPLICATE = "duplicate_run_key"
REASON_NO_RECEIPT = "no_receipt_for_day"

_DAY_RE = re.compile(r"^\d{8}$")
_RUN_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_REASON_RE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")

_LOCK_NS = "theme_upstream_receipt"
_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
_MAX_DECLARATIONS = 32
_MAX_TEXT = 255

#: 疑似凭据的文本特征。命中即拒绝整条回执(不做脱敏后落库: 半遮半掩更危险)。
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
        r"|authorization|cookie|session[_-]?id)\b\s*[:=]"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|https?)://"
        r"[^\s:@/]+:[^\s@/]+@"
    ),
    re.compile(r"(?i)\b(?:sk|pk|rk)[_-](?:live|test)[_-][A-Za-z0-9]{8,}"),
    re.compile(r"(?:密码|密钥|口令)\s*[:=：]"),
)


# ── 异常 ────────────────────────────────────────────────────────────────
class ReceiptError(Exception):
    """回执无法安全记录。**在写任何一行之前**抛出。"""


class ReceiptBindError(ReceiptError):
    """没有拿到显式的 Engine / Connection。"""


class ReceiptConflictError(ReceiptError):
    """唯一约束冲突(已回滚): 正常幂等走 run_key 显式查询, 到这里说明真有问题。"""


class ReceiptValidationError(ReceiptError):
    """回执字段不合法。`reason` 是稳定的机器可读原因码。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class ReceiptSensitiveContentError(ReceiptValidationError):
    """回执文本里出现疑似凭据。拒绝落库, 且错误消息不回显原文。"""


# ── 独立 MetaData + 表(前缀 theme_upstream_receipt_, 与生产表零重名) ─────
THEME_UPSTREAM_RECEIPT_METADATA = MetaData()

THEME_UPSTREAM_RECEIPT_RUN = Table(
    "theme_upstream_receipt_run",
    THEME_UPSTREAM_RECEIPT_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("day", String(8), nullable=False),
    Column("attempt_seq", Integer, nullable=False),
    Column("run_key", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("source", String(64), nullable=False),
    Column("started_at_utc", String(32), nullable=False),
    Column("finished_at_utc", String(32), nullable=True),
    Column("source_data_day", String(8), nullable=True),
    Column("date_basis", String(64), nullable=True),
    Column("close_complete", Boolean, nullable=True),
    Column("coverage", String(64), nullable=True),
    Column("reason", String(64), nullable=True),
    Column("detail", String(255), nullable=True),
    UniqueConstraint("day", "attempt_seq", name="uq_theme_upstream_receipt_attempt"),
    UniqueConstraint("run_key", name="uq_theme_upstream_receipt_run_key"),
    Index("ix_theme_upstream_receipt_day", "day", "attempt_seq"),
)

THEME_UPSTREAM_RECEIPT_DECL = Table(
    "theme_upstream_receipt_decl",
    THEME_UPSTREAM_RECEIPT_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("value", String(255), nullable=False),
    UniqueConstraint("run_id", "seq", name="uq_theme_upstream_receipt_decl_seq"),
    Index("ix_theme_upstream_receipt_decl_run", "run_id"),
)


# ── 输入 / 输出模型(纯数据, 无副作用) ───────────────────────────────────
@dataclass(frozen=True)
class ReceiptDeclaration:
    """一条附加声明。`kind` 是机器可读标签, `value` 是调用方原话(经敏感扫描)。"""

    kind: str
    value: str


@dataclass(frozen=True)
class UpstreamReceipt:
    """一次上游运行的**声明**。字段缺失(None)表示未知, 不是 False / 0 / 当日。"""

    day: str
    run_key: str
    status: str
    source: str
    started_at: datetime
    finished_at: datetime | None = None
    source_data_day: str | None = None
    date_basis: str | None = None
    close_complete: bool | None = None
    coverage: str | None = None
    reason: str | None = None
    detail: str | None = None
    declarations: Sequence[ReceiptDeclaration] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReceiptOutcome:
    """追加结果。`inserted=False` 时一行都没写, 并说明原因(目前只有幂等重放)。"""

    inserted: bool
    reason: str | None
    day: str
    run_key: str
    run_id: int | None = None
    attempt_seq: int | None = None
    status: str | None = None
    declarations_written: int = 0
    claims_verified: bool = False


@dataclass(frozen=True)
class Readiness:
    """只读 readiness。`state` 描述**声明层面**能看到什么, 不是市场事实。

    `claims_verified` 恒为 False: 本模块没有、也不准备有独立验证通道。
    """

    day: str
    state: str
    reason: str | None
    run_key: str | None
    attempt_seq: int | None
    status: str | None
    source: str | None
    started_at: datetime | None
    finished_at: datetime | None
    source_data_day: str | None
    date_basis: str | None
    declared_close_complete: bool | None
    declared_coverage: str | None
    declaration_count: int
    attempt_count: int
    order_basis: str
    finished_out_of_order: bool
    claims_verified: bool
    notes: tuple[str, ...]


# ── 时间 ────────────────────────────────────────────────────────────────
def format_utc(value: datetime) -> str:
    """UTC aware datetime → 固定宽度 ISO 字符串(SQLite 不丢时区的唯一办法)。"""
    return value.astimezone(timezone.utc).strftime(_TS_FMT)


def parse_utc(value: Any) -> datetime | None:
    """库里的字符串 → UTC aware datetime; 缺失或损坏都返回 None(未知, 不猜)。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _require_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ReceiptValidationError(f"bad_{field_name}", f"{field_name} 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReceiptValidationError("naive_timestamp", f"{field_name} 必须是 UTC aware 时间")
    if value.utcoffset() != timedelta(0):
        raise ReceiptValidationError(
            "not_utc", f"{field_name} 必须是 UTC(偏移 0); 非 UTC 时区请调用方先换算")
    return value.astimezone(timezone.utc)


# ── 校验 helper(全部纯内存) ─────────────────────────────────────────────
def _require_day(value: Any) -> str:
    if not isinstance(value, str) or not _DAY_RE.match(value):
        raise ReceiptValidationError("bad_day", f"day 必须是 YYYYMMDD, 收到 {value!r}")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ReceiptValidationError("bad_day", f"day 不是有效日期: {value!r}") from exc
    return value


def _reject_sensitive(field_name: str, value: str | None) -> None:
    if not value:
        return
    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            # 错误消息只给字段名, 不回显命中文本。
            raise ReceiptSensitiveContentError(
                "sensitive_content", f"{field_name} 含疑似凭据, 拒绝写入")


def _optional_text(value: Any, field_name: str, *, max_len: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReceiptValidationError(f"bad_{field_name}", f"{field_name} 必须是字符串")
    text_value = value.strip()
    if not text_value:
        return None
    if len(text_value) > max_len:
        raise ReceiptValidationError(f"bad_{field_name}", f"{field_name} 超长(>{max_len})")
    if any(ord(ch) < 32 for ch in text_value):
        raise ReceiptValidationError(f"bad_{field_name}", f"{field_name} 含控制字符")
    _reject_sensitive(field_name, text_value)
    return text_value


def _plan_receipt(receipt: Any) -> dict[str, Any]:
    """把一条回执校验成落库计划。失败时**一行都没写**。"""
    if not isinstance(receipt, UpstreamReceipt):
        raise ReceiptValidationError("bad_receipt", "receipt 必须是 UpstreamReceipt")

    day = _require_day(receipt.day)

    run_key = receipt.run_key
    if not isinstance(run_key, str) or not _RUN_KEY_RE.match(run_key):
        raise ReceiptValidationError(
            "bad_run_key", "run_key 必须是 1-64 位 [A-Za-z0-9_.:-]")

    status = receipt.status
    if status not in _STATUSES:
        raise ReceiptValidationError("bad_status", f"status 必须是 {_STATUSES} 之一")

    source = receipt.source
    if not isinstance(source, str) or not _SOURCE_RE.match(source):
        raise ReceiptValidationError(
            "bad_source", "source 必须是 1-64 位 [A-Za-z0-9_.:-]")

    started_at = _require_utc(receipt.started_at, "started_at")
    finished_at = (
        _require_utc(receipt.finished_at, "finished_at")
        if receipt.finished_at is not None else None
    )
    if finished_at is not None and finished_at < started_at:
        raise ReceiptValidationError(
            "finished_before_started", "finished_at 早于 started_at, 回执自相矛盾")

    source_data_day = (
        _require_day(receipt.source_data_day)
        if receipt.source_data_day is not None else None
    )
    date_basis = _optional_text(receipt.date_basis, "date_basis", max_len=64)
    coverage = _optional_text(receipt.coverage, "coverage", max_len=64)

    close_complete = receipt.close_complete
    if close_complete is not None and type(close_complete) is not bool:
        raise ReceiptValidationError(
            "bad_close_complete", "close_complete 只能是 True/False/None(未知)")

    reason = receipt.reason
    if reason is not None:
        if not isinstance(reason, str) or not _REASON_RE.match(reason):
            raise ReceiptValidationError(
                "bad_reason", "reason 必须是 1-64 位小写原因码 [a-z0-9_.:-]")
    if status in (STATUS_REFUSED, STATUS_FAILED) and reason is None:
        raise ReceiptValidationError("reason_required", f"status={status} 必须给出 reason")

    detail = receipt.detail
    if isinstance(detail, BaseException):
        # 异常对象可能带 traceback / locals / 请求头; 永不落库, 也不回显 str(exc)。
        raise ReceiptValidationError(
            "exception_object", "detail 不接受异常对象, 请传已脱敏的字符串")
    detail = _optional_text(detail, "detail", max_len=_MAX_TEXT)

    raw_decls = receipt.declarations
    if raw_decls is None:
        raw_decls = ()
    if not isinstance(raw_decls, (list, tuple)):
        raise ReceiptValidationError("bad_declarations", "declarations 必须是序列")
    if len(raw_decls) > _MAX_DECLARATIONS:
        raise ReceiptValidationError(
            "bad_declarations", f"declarations 最多 {_MAX_DECLARATIONS} 条")
    declarated: list[ReceiptDeclaration] = []
    for item in raw_decls:
        if not isinstance(item, ReceiptDeclaration):
            raise ReceiptValidationError(
                "bad_declarations", "declarations 元素必须是 ReceiptDeclaration")
        if not isinstance(item.kind, str) or not _KIND_RE.match(item.kind):
            raise ReceiptValidationError(
                "bad_declaration_kind", f"声明 kind 不合法: {item.kind!r}")
        value = _optional_text(item.value, "declaration_value", max_len=_MAX_TEXT)
        if value is None:
            raise ReceiptValidationError(
                "bad_declaration_value", "声明 value 不能为空")
        declarated.append(ReceiptDeclaration(kind=item.kind, value=value))

    return {
        "day": day,
        "run_key": run_key,
        "status": status,
        "source": source,
        "started_at_utc": format_utc(started_at),
        "finished_at_utc": format_utc(finished_at) if finished_at else None,
        "source_data_day": source_data_day,
        "date_basis": date_basis,
        "close_complete": close_complete,
        "coverage": coverage,
        "reason": reason,
        "detail": detail,
        "declarations": tuple(declarated),
    }


# ── bind / 事务 / DDL ───────────────────────────────────────────────────
def _open_bind(bind: Any) -> tuple[Connection, bool]:
    if isinstance(bind, Engine):
        return bind.connect(), True
    if isinstance(bind, Connection):
        return bind, False
    raise ReceiptBindError(
        f"必须显式传入隔离的 Engine 或 Connection, 收到 {type(bind).__name__}")


def create_schema(bind: Engine | Connection) -> None:
    """在**调用方给的库**上建原型表。模块从不自动建表, 也不碰生产迁移。"""
    if not isinstance(bind, (Engine, Connection)):
        raise ReceiptBindError("create_schema 需要显式的 Engine 或 Connection")
    THEME_UPSTREAM_RECEIPT_METADATA.create_all(bind, checkfirst=True)


def _day_lock_key(day: str) -> int:
    digest = hashlib.blake2b(
        f"{_LOCK_NS}:{day}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _serialize_day(conn: Connection, day: str) -> None:
    """同一天的 attempt_seq 计算与写入必须串行; 不支持的方言拒绝写入。"""
    dialect = conn.dialect.name
    if dialect == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _day_lock_key(day)})
        return
    if dialect == "sqlite":
        driver = getattr(conn.connection, "driver_connection", None)
        if getattr(driver, "in_transaction", False):
            raise ReceiptError("SQLite 写锁必须是事务第一条语句; 连接上已有事务")
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        return
    raise ReceiptError(f"不支持的后端方言 {dialect!r}: 没有序列化追加就拒绝写入")


# ── 写入 ────────────────────────────────────────────────────────────────
def _select_by_run_key(conn: Connection, run_key: str) -> Mapping[str, Any] | None:
    stmt = select(THEME_UPSTREAM_RECEIPT_RUN).where(
        THEME_UPSTREAM_RECEIPT_RUN.c.run_key == run_key)
    return conn.execute(stmt).mappings().first()


def _next_attempt_seq(conn: Connection, day: str) -> int:
    stmt = select(func.coalesce(func.max(THEME_UPSTREAM_RECEIPT_RUN.c.attempt_seq), 0)).where(
        THEME_UPSTREAM_RECEIPT_RUN.c.day == day)
    return int(conn.execute(stmt).scalar_one()) + 1


def _insert_declarations(
    conn: Connection,
    *,
    run_id: int,
    declarations: Sequence[ReceiptDeclaration],
) -> None:
    """附加声明只用普通 INSERT; 冲突是真 bug, 不许静默丢行。"""
    if not declarations:
        return
    conn.execute(
        insert(THEME_UPSTREAM_RECEIPT_DECL),
        [
            {"run_id": run_id, "seq": seq, "kind": item.kind, "value": item.value}
            for seq, item in enumerate(declarations, start=1)
        ],
    )


def _check_written_counts(
    conn: Connection, *, run_id: int, expected_declarations: int,
) -> None:
    """同事务复核: 声明行数不符就抛, 让整轮回滚, 不留下"缺行但成功"的 run。"""
    got = int(conn.execute(
        select(func.count()).select_from(THEME_UPSTREAM_RECEIPT_DECL).where(
            THEME_UPSTREAM_RECEIPT_DECL.c.run_id == run_id)
    ).scalar_one())
    if got != expected_declarations:
        raise ReceiptError(
            f"run {run_id} 声明行数不符: {got} != {expected_declarations}")


def _same_optional_bool(stored: Any, wanted: bool | None) -> bool:
    if wanted is None or stored is None:
        return stored is None and wanted is None
    return bool(stored) is wanted


def _same_payload(
    conn: Connection, existing: Mapping[str, Any], plan: Mapping[str, Any],
) -> bool:
    """同一 run_key 必须对应同一份输入; 否则重放会把不同证据误报成成功。"""
    if (
        existing["day"] != plan["day"]
        or existing["status"] != plan["status"]
        or existing["source"] != plan["source"]
        or existing["started_at_utc"] != plan["started_at_utc"]
        or existing["finished_at_utc"] != plan["finished_at_utc"]
        or existing["source_data_day"] != plan["source_data_day"]
        or existing["date_basis"] != plan["date_basis"]
        or existing["coverage"] != plan["coverage"]
        or existing["reason"] != plan["reason"]
        or existing["detail"] != plan["detail"]
        or not _same_optional_bool(existing["close_complete"], plan["close_complete"])
    ):
        return False
    rows = conn.execute(
        select(THEME_UPSTREAM_RECEIPT_DECL.c.seq,
               THEME_UPSTREAM_RECEIPT_DECL.c.kind,
               THEME_UPSTREAM_RECEIPT_DECL.c.value)
        .where(THEME_UPSTREAM_RECEIPT_DECL.c.run_id == existing["id"])
        .order_by(THEME_UPSTREAM_RECEIPT_DECL.c.seq)
    ).all()
    stored = tuple((int(r.seq), r.kind, r.value) for r in rows)
    wanted = tuple(
        (seq, item.kind, item.value)
        for seq, item in enumerate(plan["declarations"], start=1)
    )
    return stored == wanted


def append_receipt(bind: Engine | Connection, receipt: UpstreamReceipt) -> ReceiptOutcome:
    """只追加一条回执。幂等键是 `run_key`; 返回 `inserted=False` 表示重放, 一行没写。"""
    plan = _plan_receipt(receipt)  # 纯内存; 校验失败不占用/泄漏连接
    conn, owns_conn = _open_bind(bind)
    # 函数会 commit/rollback, 绝不能接管调用方已有事务。
    if conn.in_transaction():
        if owns_conn:
            conn.close()
        raise ReceiptError("已有事务的连接不可用于独立回执写入")
    if conn.dialect.name == "postgresql":
        driver = getattr(conn.connection, "driver_connection", None)
        if getattr(driver, "autocommit", None) is not False:
            if owns_conn:
                conn.close()
            raise ReceiptError("PostgreSQL 连接必须使用可验证的非 AUTOCOMMIT 事务")

    try:
        _serialize_day(conn, plan["day"])

        existing = _select_by_run_key(conn, plan["run_key"])
        if existing is not None:
            same = _same_payload(conn, existing, plan)
            conn.rollback()  # 释放锁, 且本轮什么都没写
            if not same:
                raise ReceiptConflictError(
                    f"run_key={plan['run_key']} 已存在但输入不同, 拒绝当作重放")
            return ReceiptOutcome(
                inserted=False,
                reason=REASON_DUPLICATE,
                day=existing["day"],
                run_key=plan["run_key"],
                run_id=int(existing["id"]),
                attempt_seq=int(existing["attempt_seq"]),
                status=existing["status"],
            )

        attempt_seq = _next_attempt_seq(conn, plan["day"])
        result = conn.execute(
            insert(THEME_UPSTREAM_RECEIPT_RUN).values(
                day=plan["day"],
                attempt_seq=attempt_seq,
                run_key=plan["run_key"],
                status=plan["status"],
                source=plan["source"],
                started_at_utc=plan["started_at_utc"],
                finished_at_utc=plan["finished_at_utc"],
                source_data_day=plan["source_data_day"],
                date_basis=plan["date_basis"],
                close_complete=plan["close_complete"],
                coverage=plan["coverage"],
                reason=plan["reason"],
                detail=plan["detail"],
            )
        )
        run_id = int(result.inserted_primary_key[0])
        _insert_declarations(
            conn, run_id=run_id, declarations=plan["declarations"])
        _check_written_counts(
            conn, run_id=run_id, expected_declarations=len(plan["declarations"]))
        conn.commit()
    except IntegrityError as exc:
        conn.rollback()
        raise ReceiptConflictError("回执写入约束冲突, 已回滚") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()

    return ReceiptOutcome(
        inserted=True,
        reason=None,
        day=plan["day"],
        run_key=plan["run_key"],
        run_id=run_id,
        attempt_seq=attempt_seq,
        status=plan["status"],
        declarations_written=len(plan["declarations"]),
    )


# ── 读侧(只读, 不跨日/不跨 run 拼接) ───────────────────────────────────
def _load_run_rows(conn: Connection, day: str) -> list[dict[str, Any]]:
    stmt = (
        select(THEME_UPSTREAM_RECEIPT_RUN)
        .where(THEME_UPSTREAM_RECEIPT_RUN.c.day == day)
        .order_by(THEME_UPSTREAM_RECEIPT_RUN.c.attempt_seq)
    )
    return [dict(row) for row in conn.execute(stmt).mappings()]


def _declaration_counts(conn: Connection, run_ids: Sequence[int]) -> dict[int, int]:
    if not run_ids:
        return {}
    rows = conn.execute(
        select(THEME_UPSTREAM_RECEIPT_DECL.c.run_id, func.count())
        .where(THEME_UPSTREAM_RECEIPT_DECL.c.run_id.in_(list(run_ids)))
        .group_by(THEME_UPSTREAM_RECEIPT_DECL.c.run_id)
    ).all()
    return {int(run_id): int(count) for run_id, count in rows}


def _run_view(row: Mapping[str, Any], declaration_count: int) -> dict[str, Any]:
    return {
        "run_id": int(row["id"]),
        "day": row["day"],
        "attempt_seq": int(row["attempt_seq"]),
        "run_key": row["run_key"],
        "status": row["status"],
        "source": row["source"],
        "started_at": parse_utc(row["started_at_utc"]),
        "finished_at": parse_utc(row["finished_at_utc"]),
        "source_data_day": row["source_data_day"],
        "date_basis": row["date_basis"],
        "close_complete": row["close_complete"],
        "coverage": row["coverage"],
        "reason": row["reason"],
        "detail": row["detail"],
        "declaration_count": declaration_count,
    }


def receipts_for_day(bind: Engine | Connection, day: str) -> list[dict[str, Any]]:
    """该目标日的全部回执, 按追加序号升序。不补、不跨日、不拼接。"""
    day = _require_day(day)
    conn, owns_conn = _open_bind(bind)
    try:
        rows = _load_run_rows(conn, day)
        counts = _declaration_counts(conn, [int(r["id"]) for r in rows])
        return [_run_view(r, counts.get(int(r["id"]), 0)) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def declarations_for_run(bind: Engine | Connection, run_id: int) -> list[dict[str, Any]]:
    """某次回执的全部声明行(按 seq 升序)。"""
    conn, owns_conn = _open_bind(bind)
    try:
        rows = conn.execute(
            select(THEME_UPSTREAM_RECEIPT_DECL)
            .where(THEME_UPSTREAM_RECEIPT_DECL.c.run_id == int(run_id))
            .order_by(THEME_UPSTREAM_RECEIPT_DECL.c.seq)
        ).mappings()
        return [
            {"seq": int(r["seq"]), "kind": r["kind"], "value": r["value"]}
            for r in rows
        ]
    finally:
        if owns_conn:
            conn.close()


def _state_for(latest: Mapping[str, Any], day: str) -> tuple[str, str | None]:
    """最新一次回执 → readiness 状态。只读声明, 不碰市场、不碰时钟。"""
    status = latest["status"]
    if status == STATUS_FAILED:
        return READINESS_FAILED, latest["reason"] or "unspecified_failure"
    if status == STATUS_REFUSED:
        return READINESS_REFUSED, latest["reason"] or "unspecified_refusal"

    source_data_day = latest["source_data_day"]
    if source_data_day is not None and source_data_day != day:
        return READINESS_WRONG_DAY, "source_day_mismatch"

    started_raw = latest["started_at_utc"]
    finished_raw = latest["finished_at_utc"]
    if started_raw is None:
        return READINESS_TIME_UNKNOWN, "started_at_unknown"
    if parse_utc(started_raw) is None:
        return READINESS_TIME_UNKNOWN, "timestamp_unreadable"
    if finished_raw is None:
        return READINESS_TIME_UNKNOWN, "finished_at_unknown"
    if parse_utc(finished_raw) is None:
        return READINESS_TIME_UNKNOWN, "timestamp_unreadable"
    start = parse_utc(started_raw)
    finish = parse_utc(finished_raw)
    china = timezone(timedelta(hours=8))
    if (start is None or finish is None or start > finish
            or start.astimezone(china).strftime("%Y%m%d") != day
            or finish.astimezone(china).strftime("%Y%m%d") != day):
        return READINESS_TIME_UNKNOWN, "timestamp_day_mismatch_or_order_invalid"

    # 走到这里: 状态 ok、时间可用、日期没有明确冲突。剩下全看声明是否在场。
    if source_data_day is None:
        return READINESS_COMPLETION_UNKNOWN, "source_day_undeclared"
    if not latest["date_basis"]:
        return READINESS_COMPLETION_UNKNOWN, "date_basis_undeclared"
    if latest["close_complete"] is None:
        return READINESS_COMPLETION_UNKNOWN, "close_complete_undeclared"
    if not latest["coverage"]:
        return READINESS_COMPLETION_UNKNOWN, "coverage_undeclared"
    if latest["coverage"] == "partial":
        return READINESS_INCOMPLETE, "coverage_declared_partial"
    if latest["coverage"] != "complete":
        return READINESS_COMPLETION_UNKNOWN, "coverage_not_declared_complete"
    if latest["close_complete"] is False:
        return READINESS_INCOMPLETE, "source_reports_incomplete"
    return READINESS_COMPLETE_DECLARED, None


def _finished_out_of_order(rows: Sequence[Mapping[str, Any]]) -> bool:
    known = [
        parsed for parsed in (parse_utc(r["finished_at_utc"]) for r in rows)
        if parsed is not None
    ]
    return any(later < earlier for earlier, later in zip(known, known[1:]))


def read_readiness(bind: Engine | Connection, day: str) -> Readiness:
    """只读 readiness: 当天没有回执就是 not_run, 不回退到别的日期。

    返回的 state 只描述**调用方声明**能支持什么。`complete_declared` 不等于市场
    收盘定稿, 也不等于覆盖完整 —— 本模块没有验证通道, `claims_verified` 恒 False。
    """
    day = _require_day(day)
    conn, owns_conn = _open_bind(bind)
    try:
        rows = _load_run_rows(conn, day)
        counts = _declaration_counts(conn, [int(r["id"]) for r in rows])
    finally:
        if owns_conn:
            conn.close()

    out_of_order = _finished_out_of_order(rows)

    if not rows:
        return Readiness(
            day=day,
            state=READINESS_NOT_RUN,
            reason=REASON_NO_RECEIPT,
            run_key=None,
            attempt_seq=None,
            status=None,
            source=None,
            started_at=None,
            finished_at=None,
            source_data_day=None,
            date_basis=None,
            declared_close_complete=None,
            declared_coverage=None,
            declaration_count=0,
            attempt_count=0,
            order_basis=ORDER_BASIS,
            finished_out_of_order=False,
            claims_verified=CLAIMS_VERIFIED,
            notes=(
                "no_fallback_to_other_days",
                "caller_declarations_not_verified",
            ),
        )

    latest = rows[-1]
    state, reason = _state_for(latest, day)
    notes = ["caller_declarations_not_verified"]
    if state == READINESS_COMPLETE_DECLARED:
        notes.append("time_not_used_as_close_signal")
    if out_of_order:
        notes.append("finished_order_anomaly")
        if state == READINESS_COMPLETE_DECLARED:
            state, reason = READINESS_COMPLETION_UNKNOWN, "finished_order_anomaly"

    return Readiness(
        day=day,
        state=state,
        reason=reason,
        run_key=latest["run_key"],
        attempt_seq=int(latest["attempt_seq"]),
        status=latest["status"],
        source=latest["source"],
        started_at=parse_utc(latest["started_at_utc"]),
        finished_at=parse_utc(latest["finished_at_utc"]),
        source_data_day=latest["source_data_day"],
        date_basis=latest["date_basis"],
        declared_close_complete=latest["close_complete"],
        declared_coverage=latest["coverage"],
        declaration_count=counts.get(int(latest["id"]), 0),
        attempt_count=len(rows),
        order_basis=ORDER_BASIS,
        finished_out_of_order=out_of_order,
        claims_verified=CLAIMS_VERIFIED,
        notes=tuple(notes),
    )
