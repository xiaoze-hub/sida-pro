"""事务自愈: 吞异常之前先把 PostgreSQL 的 aborted 事务清掉。

## 为什么需要这个模块

PostgreSQL 的事务语义有一个陷阱: 事务里**任何一条** SQL 报错后, 整个事务立刻进入
`aborted` 状态, 之后所有语句一律返回

    InFailedSqlTransaction: current transaction is aborted, commands ignored
    until end of transaction block

而本项目大量使用「best-effort / fail-open」写法: 探测失败就按默认值继续, 不阻断主流程。
典型如会话存活校验、注册模式读取、登录时间戳、用量落库。这些代码如果只

    except Exception:
        pass            # ← 错误就在这里

而不回滚, 那么一条**本可以忽略**的失败会被放大成「这个请求后面所有查询全部 500」。

2026-10-23 生产事故就是这个形态: `auth_session_revocations` 表从未被创建, 每个鉴权
请求的第一条 SELECT 抛 `UndefinedTable` → 事务中止 → `/api/settings`、
`/api/notifications`、`/api/users/admin/list` 成片 500, 用户看到的是「后台/设置/通知的
数据都没了」。SQLite 不中止事务, 所以单测全绿、线上全红。

## 用法

    try:
        db.add(row); db.commit()
    except Exception as e:      # noqa: BLE001
        safe_rollback(db)       # ← 必须在 return/继续之前
        logger.warning(...)

**只吞异常不回滚 = 把局部故障升级成全局故障。**
"""

from __future__ import annotations

from typing import Any


def safe_rollback(db: Any) -> None:
    """回滚当前事务; 连回滚都失败时静默(兜底逻辑不能再抛异常)。"""
    if db is None:
        return
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 - 回滚失败也不能阻断调用方
        pass


__all__ = ["safe_rollback"]
