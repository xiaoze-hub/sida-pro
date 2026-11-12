"""后验计分可信度闸门 —— fail-closed(2026-11-13)。

## 为什么需要

`agent_prediction_outcomes` 的"至多一条计分行 / 同版本只存一行"**不是代码约定, 是 DB 层的
四条部分唯一索引**(m250 / models 的 `__table_args__`):

    ux_prediction_outcome_user_active_key    (user_id, agent, symbol, market, date, horizon)
                                             WHERE user_id IS NOT NULL AND status <> 'superseded'
    ux_prediction_outcome_user_run_key       (… , run_trace_id)  WHERE user_id/run_trace_id NOT NULL
    ux_prediction_outcome_system_active_key  (agent, symbol, market, date, horizon)
                                             WHERE owner_scope='system' AND status <> 'superseded'
    ux_prediction_outcome_system_run_key     (…, run_trace_id)   WHERE owner_scope='system' AND run_trace_id NOT NULL

审计复现的问题: 这四条索引**缺失**(或被人用同名但定义不同的索引顶替)时, 应用层照旧评估、
照旧统计 —— 并发/重跑会写出第二条计分行, 同一个键被算两次, 命中率凭空翻倍; 而
`schema_migrations.success=1` 不会因为"索引没建出来"而回滚(迁移为不删数据而跳过建索引只记
ERROR), 所以"迁移成功"绝不能当成"约束在位"。

## 本模块的契约

1. `check_scoring_health()` 只看 **schema 目录**(索引 DDL)与 **聚合计数**, 不看任何用户行、
   不返回任何 user_id —— 索引/状态诊断本身不泄露其他用户。
2. 四条索引任何一条 `missing` 或**定义不符**(唯一性/列/谓词, 不只看名字)→ `blocked`;
   调用方必须显示 `scoring_blocked` 并**不评估、不回填、不计分**。
3. `allow_repair=True`(启动 / 评估 / 显式维护)时按需**幂等重建**: 先统计该索引谓词下的
   重复键组数, `0` 才 `CREATE UNIQUE INDEX`; 有重复就**不建**, 如实报
   `duplicate_rows`。定义不符的同名索引先 `DROP INDEX`(它本来就不是我们要的约束)再建。
   **全程不 UPDATE / DELETE 任何数据行**, 也不做任何回填。
4. 拿不到连接 / 检查本身抛错 → 同样 `blocked`(`unavailable` / `check_failed`)。宁可显示
   "评分阻断", 也不输出一个可能双计的可信命中率。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

TABLE = "agent_prediction_outcomes"

# 索引 DDl 只接受我们自己建的标识符; 巡检时用它把名字挡在 SQL 拼接之外。
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# 判定所需的列(m250 之前的老库没有后三列): 缺列 = 结构不完整, 评分不可信。
_REQUIRED_COLUMNS = ("user_id", "owner_scope", "run_trace_id")

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"

IDX_OK = "ok"
IDX_MISSING = "missing"
IDX_MISMATCH = "mismatch"


@dataclass(frozen=True)
class IndexSpec:
    """一条"计分可信"所依赖的部分唯一索引的**语义**定义(不是名字)。"""

    name: str
    columns: tuple[str, ...]
    where: str
    description: str

    @property
    def violation_sql(self) -> str:
        """该谓词下"重复键组数" —— 只做 COUNT, 不读写任何行内容。"""
        cols = ", ".join(self.columns)
        return (
            "SELECT COUNT(*) FROM ("
            f"SELECT 1 FROM {TABLE} WHERE {self.where} "
            f"GROUP BY {cols} HAVING COUNT(*) > 1) d"
        )


INDEX_SPECS: tuple[IndexSpec, ...] = (
    IndexSpec(
        name="ux_prediction_outcome_user_active_key",
        columns=(
            "user_id",
            "agent_name",
            "stock_symbol",
            "stock_market",
            "prediction_date",
            "horizon_days",
        ),
        where="user_id IS NOT NULL AND outcome_status <> 'superseded'",
        description="个人桶: 每个键至多一条计分行",
    ),
    IndexSpec(
        name="ux_prediction_outcome_user_run_key",
        columns=(
            "user_id",
            "agent_name",
            "stock_symbol",
            "stock_market",
            "prediction_date",
            "horizon_days",
            "run_trace_id",
        ),
        where="user_id IS NOT NULL AND run_trace_id IS NOT NULL",
        description="个人桶: 同一 run_trace_id 只存一行",
    ),
    IndexSpec(
        name="ux_prediction_outcome_system_active_key",
        columns=(
            "agent_name",
            "stock_symbol",
            "stock_market",
            "prediction_date",
            "horizon_days",
        ),
        where="owner_scope = 'system' AND outcome_status <> 'superseded'",
        description="system 桶: 每个键至多一条计分行",
    ),
    IndexSpec(
        name="ux_prediction_outcome_system_run_key",
        columns=(
            "agent_name",
            "stock_symbol",
            "stock_market",
            "prediction_date",
            "horizon_days",
            "run_trace_id",
        ),
        where="owner_scope = 'system' AND run_trace_id IS NOT NULL",
        description="system 桶: 同一 run_trace_id 只存一行",
    ),
)

_SPEC_BY_NAME = {s.name: s for s in INDEX_SPECS}


@dataclass(frozen=True)
class IndexState:
    """单条索引的实测状态。`detail` 只描述**差异类别**, 不回显库里的任意 DDL。"""

    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class ScoringHealth:
    """一次评分可信度检查的结果。`blocked=True` 时调用方**必须**停止评估/计分。"""

    status: str
    reason_code: str
    detail: str
    indexes: tuple[IndexState, ...] = ()
    repaired: tuple[str, ...] = ()
    allow_repair: bool = False
    duplicate_blocked: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.status != STATUS_OK

    def as_payload(self) -> dict:
        """给 API/日志用的字典。

        **不含任何用户数据**: 只有索引名、状态类别与聚合计数; 明确标 `contains_user_data=False`,
        便于调用方与审计复核"状态检查没有泄露其他用户"。
        """
        return {
            "status": self.status,
            "scoring_blocked": self.blocked,
            "reason_code": self.reason_code,
            "reason": self.detail,
            "indexes": [
                {"name": i.name, "status": i.status, "detail": i.detail} for i in self.indexes
            ],
            "repaired": list(self.repaired),
            # 因存量重复键组而未重建的索引(需要人工核查; 系统不会自动清理数据)
            "duplicate_blocked": list(self.duplicate_blocked),
            "table": TABLE,
            "contains_user_data": False,
        }


# ── DDL / 谓词归一化 ────────────────────────────────────────────────────────


def _canon_sql(sql: str) -> str:
    """把 DDL 片段归一化到可比较的形式(PG 会加 `::text` 与成堆括号, SQLite 不会)。

    只用于**比较**, 从不用于执行。保留字符串字面量的单引号(谓词里的 'superseded' /
    'system' 是语义的一部分)。
    """
    s = (sql or "").strip().lower()
    for noise in ("::text", "::character varying", "::varchar", "::bpchar"):
        s = s.replace(noise, "")
    s = s.replace("!=", "<>")
    for ch in '"`[]':
        s = s.replace(ch, "")
    s = re.sub(r"\s+", "", s)
    return s.replace("(", "").replace(")", "")


_INDEX_DDL_RE = re.compile(
    r"create\s+(?P<unique>unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?"
    r"(?P<name>[^(\s]+)\s+on\s+(?P<table>[^(\s]+)\s*(?:using\s+\w+\s*)?"
    r"\((?P<cols>.+?)\)\s*(?:where\s+(?P<where>.+))?$",
    re.IGNORECASE | re.DOTALL,
)


def _split_top_level(raw: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in raw:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    parts.append(cur)
    return [p for p in (x.strip() for x in parts) if p]


def _canon_column(raw: str) -> str:
    c = raw.strip().strip('"').strip("`")
    while c.startswith("(") and c.endswith(")"):
        c = c[1:-1].strip()
    return c.lower()


def parse_index_ddl(ddl: str) -> dict | None:
    """把 `CREATE [UNIQUE] INDEX ... ON ... (cols) [WHERE pred]` 解析成结构化描述。

    解析不出来 → None(调用方按"无法证明等价"处理 = 阻断)。
    """
    if not ddl:
        return None
    m = _INDEX_DDL_RE.search(ddl.strip())
    if not m:
        return None
    table = m.group("table").strip().strip('"')
    if "." in table:
        table = table.rsplit(".", 1)[-1]
    cols = [_canon_column(c) for c in _split_top_level(m.group("cols") or "")]
    return {
        "name": m.group("name").strip().strip('"'),
        "table": table,
        "unique": bool(m.group("unique")),
        "columns": cols,
        "where": (m.group("where") or "").strip(),
    }


# ── 目录读取 ───────────────────────────────────────────────────────────────


def _dialect_name(conn: Connection) -> str:
    return str(getattr(conn.dialect, "name", "") or "")


def _has_table(conn: Connection, table: str) -> bool:
    if _dialect_name(conn) == "postgresql":
        row = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t LIMIT 1"
            ),
            {"t": table},
        ).first()
    else:
        row = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t LIMIT 1"),
            {"t": table},
        ).first()
    return bool(row)


def _table_columns(conn: Connection) -> set[str]:
    if _dialect_name(conn) == "postgresql":
        rows = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": TABLE},
        ).fetchall()
        return {str(r[0]) for r in rows}
    rows = conn.execute(text(f"PRAGMA table_info({TABLE})")).fetchall()
    return {str(r[1]) for r in rows}


def _all_index_ddls(conn: Connection) -> dict[str, str]:
    """一次取回本表**全部**索引的 DDL(名字 → 原文)。只读目录, 不碰数据。

    一次查全(而不是四条索引各查一次)是为了把写入热路径上的目录查询压到固定 3 次
    (表存在 / 列存在 / 索引定义) —— 见模块末尾"性能"说明。
    """
    if _dialect_name(conn) == "postgresql":
        rows = conn.execute(
            text(
                """
SELECT ic.relname AS name, pg_get_indexdef(i.indexrelid) AS ddl, i.indisvalid AS valid
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_class tc ON tc.oid = i.indrelid
JOIN pg_namespace n ON n.oid = ic.relnamespace
WHERE tc.relname = :t AND n.nspname = 'public'
"""
            ),
            {"t": TABLE},
        ).fetchall()
        out: dict[str, str] = {}
        for r in rows:
            if not r[1]:
                continue
            # 无效索引(建到一半/被标记 invalid): 不能当成"约束在位"
            out[str(r[0])] = str(r[1]) if bool(r[2]) else f"-- invalid index: {r[1]}"
        return out
    rows = conn.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=:t"),
        {"t": TABLE},
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows if r[1]}


def _index_ddl(conn: Connection, name: str) -> str | None:
    """单条索引的 `CREATE INDEX` 原文(给测试/排障用); 不存在返回 None。"""
    return _all_index_ddls(conn).get(name)


def _check_spec(ddls: dict[str, str], spec: IndexSpec) -> IndexState:
    """校验一条索引的**真实定义**: 唯一性 + 列集合 + 谓词(不只看名字)。"""
    ddl = ddls.get(spec.name)
    if ddl is None:
        return IndexState(spec.name, IDX_MISSING, "索引不存在")
    if ddl.startswith("-- invalid index:"):
        return IndexState(spec.name, IDX_MISMATCH, "索引存在但无效(indisvalid=false)")
    parsed = parse_index_ddl(ddl)
    if parsed is None:
        return IndexState(spec.name, IDX_MISMATCH, "定义无法解析, 无法证明语义等价")
    if parsed["table"] != TABLE:
        return IndexState(spec.name, IDX_MISMATCH, "索引建在别的表上")
    if not parsed["unique"]:
        return IndexState(spec.name, IDX_MISMATCH, "不是唯一索引(允许同键重复行)")
    if sorted(parsed["columns"]) != sorted(spec.columns):
        return IndexState(spec.name, IDX_MISMATCH, "列不符(与计分键不一致)")
    if _canon_sql(parsed["where"]) != _canon_sql(spec.where):
        return IndexState(spec.name, IDX_MISMATCH, "谓词不符(部分索引范围与计分口径不一致)")
    return IndexState(spec.name, IDX_OK, "")


def inspect_index_specs(conn: Connection) -> tuple[IndexState, ...]:
    """只读巡检四条索引。不建、不删、不改任何东西。"""
    ddls = _all_index_ddls(conn)
    return tuple(_check_spec(ddls, spec) for spec in INDEX_SPECS)


# ── 按需幂等重建(无重复才建; 不清理数据) ─────────────────────────────────


def _duplicate_group_count(conn: Connection, spec: IndexSpec) -> int:
    """该索引谓词下的重复键组数 —— 纯 COUNT/聚合, 不返回键值、不含 user_id。"""
    row = conn.execute(text(spec.violation_sql)).first()
    try:
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001 — 统计失败按"有冲突"处理(保守)
        return 1


def _create_spec_index(conn: Connection, spec: IndexSpec) -> None:
    cols = ", ".join(spec.columns)
    conn.execute(
        text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {spec.name} ON {TABLE} ({cols}) "
            f"WHERE {spec.where}"
        )
    )


def _drop_index(conn: Connection, name: str) -> None:
    if not _IDENT_RE.match(name):
        raise ValueError(f"拒绝操作非本模块索引名: {name!r}")
    prefix = "public." if _dialect_name(conn) == "postgresql" else ""
    conn.execute(text(f"DROP INDEX IF EXISTS {prefix}{name}"))


def _repair_one(conn: Connection, state: IndexState) -> tuple[bool, str]:
    """尝试把一条 missing/mismatch 的索引恢复成预期定义。

    返回 `(是否已恢复, 失败原因码)`。**绝不**为了建索引去删/改任何数据行。
    """
    spec = _SPEC_BY_NAME[state.name]
    dup = _duplicate_group_count(conn, spec)
    if dup:
        return False, "duplicate_rows"
    try:
        # 每条索引的修复单独一个 SAVEPOINT: 某一条建失败(并发撞名/锁超时)时只回滚这一条,
        # 不把外层事务(启动体检 / 迁移 / 评估)一起打进 aborted 状态。
        with conn.begin_nested():
            if state.status == IDX_MISMATCH:
                # 同名但语义不符的索引: 它本来就不是我们要的约束, 先丢掉再重建。
                # 只删索引, 不碰数据。
                logger.warning(
                    "[scoring] 索引 %s 定义错误(%s) → 删除该索引后按预期定义重建(不动数据)",
                    state.name,
                    state.detail,
                )
                _drop_index(conn, state.name)
            _create_spec_index(conn, spec)
    except Exception as exc:  # noqa: BLE001 — 并发建索引/锁冲突都会走到这里, 交给复核判定
        logger.warning("[scoring] 重建索引 %s 未成功(将复核): %r", state.name, exc)
        return False, "repair_failed"
    logger.warning(
        "[scoring] 已按预期定义重建索引 %s (%s); 数据行未做任何回填/清理",
        state.name,
        spec.description,
    )
    return True, ""


def _blocked(
    reason_code: str,
    detail: str,
    states: tuple[IndexState, ...] = (),
    repaired: tuple[str, ...] = (),
    allow_repair: bool = False,
    duplicate_blocked: tuple[str, ...] = (),
) -> ScoringHealth:
    return ScoringHealth(
        status=STATUS_BLOCKED,
        reason_code=reason_code,
        detail=detail,
        indexes=states,
        repaired=repaired,
        allow_repair=allow_repair,
        duplicate_blocked=duplicate_blocked,
    )


def _health_from_states(
    states: tuple[IndexState, ...],
    *,
    repaired: tuple[str, ...],
    allow_repair: bool,
    duplicate_blocked: tuple[str, ...] = (),
) -> ScoringHealth:
    """把一次巡检结果翻成闸门结论(全部 ok 才是 ok, 否则一律 blocked)。"""
    bad = [s for s in states if s.status != IDX_OK]
    if not bad:
        detail = "四条后验计分唯一索引均在位, 唯一性/列/谓词逐条校验通过"
        if repaired:
            detail += f"; 本次按需重建: {', '.join(repaired)}"
        return ScoringHealth(
            status=STATUS_OK,
            reason_code="",
            detail=detail,
            indexes=states,
            repaired=repaired,
            allow_repair=allow_repair,
            duplicate_blocked=duplicate_blocked,
        )
    codes: list[str] = []
    if any(s.status == IDX_MISSING for s in bad):
        codes.append("index_missing")
    if any(s.status == IDX_MISMATCH for s in bad):
        codes.append("index_definition_mismatch")
    if duplicate_blocked:
        codes.append("duplicate_rows")
    detail = "后验计分唯一索引不可信 → 评分阻断(不评估、不回填、不计分): " + "; ".join(
        f"{s.name}: {s.detail}" for s in bad
    )
    if duplicate_blocked:
        detail += (
            f"; 其中 {', '.join(duplicate_blocked)} 在该谓词下已有重复键组, "
            "按契约不建索引、不删除也不改写任何行(需人工核查)"
        )
    if repaired:
        detail += f"; 本次已尝试重建但仍未全部就位: {', '.join(repaired)}"
    if not allow_repair:
        detail += "; 本次为只读检查, 未做任何修复(重启或评估/维护路径会按需幂等重建)"
    return _blocked(
        "+".join(codes), detail, states, repaired, allow_repair, duplicate_blocked
    )


def _check_with_conn(conn: Connection, *, allow_repair: bool) -> ScoringHealth:
    if not _has_table(conn, TABLE):
        return _blocked(
            "table_missing",
            f"表 {TABLE} 不存在 → 没有可评估的后验记录, 评分链路不可信(评分阻断)",
            allow_repair=allow_repair,
        )
    missing_cols = [c for c in _REQUIRED_COLUMNS if c not in _table_columns(conn)]
    if missing_cols:
        return _blocked(
            "schema_incomplete",
            f"表 {TABLE} 缺列 {', '.join(missing_cols)} → 计分键/归属桶无法定义(评分阻断)",
            allow_repair=allow_repair,
        )

    states = inspect_index_specs(conn)
    repaired: list[str] = []
    duplicate_blocked: list[str] = []
    if allow_repair and any(s.status != IDX_OK for s in states):
        for state in states:
            if state.status == IDX_OK:
                continue
            ok, reason = _repair_one(conn, state)
            if ok:
                repaired.append(state.name)
            elif reason == "duplicate_rows":
                duplicate_blocked.append(state.name)
                logger.error(
                    "[scoring] 索引 %s 缺失/定义错误, 且该谓词下已存在重复键组: "
                    "不建索引、不删不改任何数据行(需人工核查这批重复行)",
                    state.name,
                )
        # 并发下的 DDL 冲突/锁等待: 复核一次实测状态, 不信"我以为建好了"
        states = inspect_index_specs(conn)

    return _health_from_states(
        states,
        repaired=tuple(repaired),
        allow_repair=allow_repair,
        duplicate_blocked=tuple(duplicate_blocked),
    )


def _default_bind():
    """默认检查对象: **context_store 实际在用的那个库**(测试/多库部署会替换会话工厂)。

    为什么不用 `src.db.session.engine`: 它是 import 时的模块级常量, monkeypatch / 多库
    部署下可能与真正写入的库不是同一个 —— 那样闸门就会体检错库, 得出"索引在位"的假结论。
    """
    try:
        from src.core import context_store as _cs

        factory = getattr(_cs, "SessionLocal", None)
        bound = getattr(factory, "kw", {}).get("bind") if factory is not None else None
        if bound is not None:
            return bound
    except Exception:  # noqa: BLE001 — 解析失败退回到默认引擎
        pass
    from src.db.session import engine as _engine

    return _engine


def check_scoring_health(bind: Engine | Connection | None = None, *, allow_repair: bool = False):
    """评分可信度闸门: 返回 `ScoringHealth`。**永远不会**抛异常给调用方。

    `bind` 可以是 Engine / Connection; 省略时用应用默认引擎(`src.db.session.engine`,
    写库)。

    `allow_repair=True` 时, 对缺失或定义错误的索引按需**幂等重建**(前提: 该谓词下无重复
    键组), 全程不 UPDATE / DELETE 任何数据行。只读路径(`allow_repair=False`)只巡检。
    """
    engine = None
    conn = None
    if isinstance(bind, Connection):
        conn = bind
    elif bind is not None:
        engine = bind
    else:
        try:
            engine = _default_bind()
        except Exception as exc:  # noqa: BLE001
            logger.error("[scoring] 拿不到数据库引擎, 评分按阻断处理: %r", exc)
            return _blocked("unavailable", f"无法获取数据库引擎: {type(exc).__name__}")

    try:
        if conn is not None:
            return _check_with_conn(conn, allow_repair=allow_repair)
        with engine.begin() as new_conn:
            return _check_with_conn(new_conn, allow_repair=allow_repair)
    except Exception as exc:  # noqa: BLE001 — 检查失败绝不等于"没问题"
        logger.error("[scoring] 评分可信度检查失败, 按阻断处理: %r", exc)
        return _blocked(
            "check_failed",
            f"评分可信度检查失败({type(exc).__name__}: {exc}) → 不能断言索引在位, 评分阻断",
            allow_repair=allow_repair,
        )


def scoring_guard_payload(bind: Engine | Connection | None = None, *, allow_repair: bool = False) -> dict:
    """给 API 用的便捷函数(只返回不含用户数据的字典)。"""
    return check_scoring_health(bind, allow_repair=allow_repair).as_payload()


# ── 性能(为什么这样够用, 以及"要更快"的保守做法) ────────────────────────────
#
# 一次 `check_scoring_health(allow_repair=False)` 固定 3 条**目录/meta 查询**:
#   1. 表是否存在(`sqlite_master` / `information_schema.tables`)
#   2. 本表列集合(判断 m249/m250 的 user_id/owner_scope/run_trace_id 是否齐全)
#   3. 本表全部索引 DDL(一次取回, 在 Python 里比对唯一性/列/谓词)
# 实测(SQLite, 300 次): ≈1 ms/次; PG 同一份查询计划是 3 次索引/目录扫描, 也是毫秒级,
# 与"每写一条预测跑一次"相比可以接受(一次盘前运行几十条, 合计几十毫秒)。
#
# **刻意不加 TTL 缓存**: 缓存会把"刚被删掉索引"的那几秒当成 ok —— 那正是要拦的窗口。
# 若将来写入路径真的成了瓶颈, 保守做法(而不是放松判断):
#   a) 只在**批处理边界**检查一次(例如 premarket_outlook 一次运行开始时调一次, 之后复用
#      同一个 `ScoringHealth` 结果对象传给循环内的写入), 结果按"运行"复用, 不跨运行缓存;
#   b) 或按 `(dialect, db_url)` 缓存, 且 TTL 只在 `allow_repair=True` 的成功结果上生效,
#      并在任何 `IntegrityError` 后立即失效;
#   c) 绝不把缓存结果用于"缺索引"的否定判断 —— 否定判断必须现查(否则会长期误阻断)。
