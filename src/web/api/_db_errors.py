"""数据库错误的方言无关分类(2026-10-29 七审)。

并发收口需要把"可重试的冲突"和"真故障"分开:
- 唯一约束冲突(23505) / 外键冲突(23503): 预检之后目标行被别人改/删, 是冲突 → 409;
- 锁等待超时(55P03) / 死锁(40P01): 并发事务排队, 是冲突 → 409 让调用方重试;
- 其它 IntegrityError/OperationalError: 真故障, 原样抛(上层 500 + 日志)。

PG 上看 SQLSTATE, SQLite 上退回原文匹配(SQLite 没有 SQLSTATE, 是库级写锁)。
"""
from __future__ import annotations

from sqlalchemy.exc import DBAPIError

_UNIQUE_TEXT = ("unique constraint failed", "duplicate key value", "unique violation")
_FK_TEXT = ("foreign key constraint",)
_LOCK_TEXT = (
    "could not obtain lock",
    "lock timeout",
    "locknotavailable",
    "deadlock detected",
    "database is locked",
)


def sqlstate(exc: BaseException) -> str | None:
    """取 PG SQLSTATE: psycopg2 用 .pgcode, psycopg3 用 .sqlstate。"""
    orig = getattr(exc, "orig", None) or exc
    for attr in ("pgcode", "sqlstate"):
        state = getattr(orig, attr, None)
        if state:
            return str(state)
    return None


def _text(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None) or exc
    return str(orig).lower()


def is_unique_violation(exc: BaseException) -> bool:
    if sqlstate(exc) == "23505":
        return True
    text = _text(exc)
    return any(marker in text for marker in _UNIQUE_TEXT)


def is_fk_violation(exc: BaseException) -> bool:
    if sqlstate(exc) == "23503":
        return True
    text = _text(exc)
    return any(marker in text for marker in _FK_TEXT)


def is_lock_conflict(exc: BaseException) -> bool:
    """锁等待超时/死锁: 该重试, 不该 500。"""
    if sqlstate(exc) in {"55P03", "40P01"}:
        return True
    text = _text(exc)
    return any(marker in text for marker in _LOCK_TEXT)


def is_db_conflict(exc: BaseException) -> bool:
    """任何一个"再来一次可能成功"的数据库冲突。"""
    return isinstance(exc, DBAPIError) and (
        is_unique_violation(exc) or is_fk_violation(exc) or is_lock_conflict(exc)
    )
