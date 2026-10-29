"""持仓/账户的租户作用域口径(唯一实现, 2026-10-29 跨租户越权修复)。

## 三种归属

对任一归属对象 X(`Account` 或 `Position`), 按 X.user_id 分三类:

- private: `X.user_id == 当前用户`   → 本人可读**可写**;
- shared : `X.user_id IS NULL`       → 历史遗留/全局共享: 所有登录用户**可读**, 仅 owner **可写**;
- foreign: `X.user_id` 是别的用户     → 不可见(调用方统一 404, 不给存在性信号)。

## 持仓可见 = 父账户可见 AND 持仓自身可见

两条缺一不可, 这正是此前两个实测越权的根因:

- 只按 `Position.user_id` 过滤(旧 list/update/defense/delete/reorder/export/chat):
  他人**私有**账户里 `user_id=NULL` 的持仓会被任何用户读到、改掉、删掉;
- 只按 `Account.user_id` 过滤(旧 `load_portfolio_for_stock`): 共享账户里别人建的持仓
  会被所有人当成自己的, 单只分析的 AI 上下文因此读到他人持仓。

标的(自选, `Stock.user_id`)用**同一套三分类**: 自己的 / 历史共享(NULL) / 他人私有。
2026-10-29 三审补: 建仓时传别人的 `stock_id` 能把持仓挂到他人自选行上(对方删除自选
会级联删掉我的持仓), 所以 `stock_user_scope_*` 与持仓口径必须同源。

## 没有"默认全量"

`user_id=None` 只对应历史共享桶(两侧都必须 `IS NULL`), 绝不放开成全表。
缺少用户会话的路径(服务令牌、后台线程)因此自然 fail-closed, 不会默默继承全部账户。

本模块只依赖 `src.db.models`(db 层), 不 import src.web / FastAPI —— HTTP 语义
(404/403 文案)留在 `src/web/api`, 便于 `src/core`、`src/bootstrap`、`src/web` 共用同一口径。
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Query, Session

from src.db.models import Account, Position, Stock

__all__ = [
    "account_user_scope_clause",
    "position_user_scope_clause",
    "stock_user_scope_clause",
    "user_scope_accounts",
    "user_scope_positions",
    "user_scope_stocks",
    "account_user_scope_visible",
    "account_user_scope_writable",
    "position_user_scope_visible",
    "position_user_scope_writable",
    "stock_user_scope_visible",
    "stock_user_scope_writable",
]


def _visibility_clause(column, user_id: str | None):
    """归属条件的唯一写法: 本人 + 共享(NULL); user_id=None 时只留共享桶。"""
    if user_id is None:
        return column.is_(None)
    return or_(column == user_id, column.is_(None))


def account_user_scope_clause(user_id: str | None):
    """`Account.user_id` 的可见条件(自己的 + 历史 NULL; 无用户则仅 NULL)。"""
    return _visibility_clause(Account.user_id, user_id)


def position_user_scope_clause(user_id: str | None):
    """`Position.user_id` 的可见条件, 必须与 `account_user_scope_clause` 同时使用。"""
    return _visibility_clause(Position.user_id, user_id)


def stock_user_scope_clause(user_id: str | None):
    """`Stock.user_id` 的可见条件(自选/标的归属)。

    自选是**用户私有数据**: `Stock.user_id=NULL` 是历史共享/全局默认项(所有人可见,
    仅 owner 可改), 他人私有的 Stock 行不可见。`user_id=None` 时只留共享桶,
    绝不放开成全表 —— 与持仓两侧口径一致。
    """
    return _visibility_clause(Stock.user_id, user_id)


def user_scope_accounts(
    db: Session, user_id: str | None, *, enabled_only: bool = False
) -> Query:
    """当前租户可见的账户查询。"""
    q = db.query(Account).filter(account_user_scope_clause(user_id))
    if enabled_only:
        q = q.filter(Account.enabled.is_(True))
    return q


def user_scope_positions(
    db: Session,
    user_id: str | None,
    *,
    account_id: int | None = None,
    stock_id: int | None = None,
    stock_ids=None,
    enabled_accounts_only: bool = False,
) -> Query:
    """当前租户可见的持仓查询: 账户可见 **且** 持仓可见, 一次都不放宽。"""
    q = (
        db.query(Position)
        .join(Account, Account.id == Position.account_id)
        .filter(account_user_scope_clause(user_id), position_user_scope_clause(user_id))
    )
    if enabled_accounts_only:
        q = q.filter(Account.enabled.is_(True))
    if account_id is not None:
        q = q.filter(Account.id == account_id)
    if stock_id is not None:
        q = q.filter(Position.stock_id == stock_id)
    if stock_ids is not None:
        q = q.filter(Position.stock_id.in_(stock_ids))
    return q


def user_scope_stocks(db: Session, user_id: str | None) -> Query:
    """当前租户可见的标的(自选)查询。"""
    return db.query(Stock).filter(stock_user_scope_clause(user_id))


# ---------------------------------------------------------------- 对象级判定
# 端点已按主键取到对象时(或关系已加载), 用下面的纯函数判定, 避免再发一次 SQL。


def account_user_scope_visible(account: Account | None, user_id: str | None) -> bool:
    """账户可读: 自己是归属人, 或为历史共享账户。"""
    if account is None:
        return False
    if account.user_id is None:
        return True
    return user_id is not None and account.user_id == user_id


def account_user_scope_writable(account: Account | None, user_id: str | None, role: str | None) -> bool:
    """账户可写: 本人账户; 共享账户仅 owner(删共享账户会带走他人持仓, 不能给 member)。"""
    if account is None:
        return False
    if account.user_id is None:
        return role == "owner"
    return user_id is not None and account.user_id == user_id


def position_user_scope_visible(
    position: Position | None, account: Account | None, user_id: str | None
) -> bool:
    """持仓可读 = 父账户可读 AND 持仓可读。账户必须显式给出, 缺失即不可见。"""
    if position is None or not account_user_scope_visible(account, user_id):
        return False
    if position.user_id is None:
        return True
    return user_id is not None and position.user_id == user_id


def position_user_scope_writable(
    position: Position | None,
    account: Account | None,
    user_id: str | None,
    role: str | None,
) -> bool:
    """持仓可写: 先满足可读, 再区分本人持仓 / 历史共享持仓(仅 owner)。"""
    if position is None or not position_user_scope_visible(position, account, user_id):
        return False
    if position.user_id is None:
        return role == "owner"
    return True


def stock_user_scope_visible(stock: Stock | None, user_id: str | None) -> bool:
    """标的(自选)可读: 自己建的, 或历史共享(NULL)项。"""
    if stock is None:
        return False
    if stock.user_id is None:
        return True
    return user_id is not None and str(stock.user_id) == str(user_id)


def stock_user_scope_writable(
    stock: Stock | None, user_id: str | None, role: str | None
) -> bool:
    """标的(自选)可写: 自己的; 共享默认项仅 owner(删共享行会波及所有人的绑定/提醒)。"""
    if stock is None:
        return False
    if stock.user_id is None:
        return role == "owner"
    return user_id is not None and str(stock.user_id) == str(user_id)

