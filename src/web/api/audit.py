"""操作审计 API(2026-08-15 阶段3): owner 视角审计日志。

- log_audit(db, user, action, detail, ip): 通用写审计入口, 供 auth/个人中心/导出等模块调用
- GET /api/audit?limit=200: 最近审计日志(仅 owner; 按时间倒序)

路由挂载约定: app.py 中 include_router(audit.router, prefix="/api/audit", tags=["audit"]),
本文件路由路径为空串, 挂载后即 /api/audit。

循环依赖说明: 本文件模块级 import auth.require_owner; auth.py 内对 log_audit 使用
函数内延迟 import —— 两侧不同时模块级互引, 任意导入顺序均安全。
"""
import logging
import traceback

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.web.database import get_db, SessionLocal
from src.web.models import AuditLog
from src.web.api.auth import require_owner

router = APIRouter()
logger = logging.getLogger(__name__)


def log_audit(db: Session, user, action: str, detail: str = "", ip: str = "") -> bool:
    """写入一条审计日志(关键写操作: 登录/注册/修改资料/改密/用户管理/配置修改/导出等)。

    修复 2026-08-21: **审计写入失败必须使用独立 session**,绝不能污染主请求 session。
    原实现 `db.add(...) + db.commit()` 直接复用主 session,导致审计表写锁竞争时
    SQLAlchemy 抛 `PendingRollbackError` → 主请求后续 lazy load 失败 → 整个端点挂死
    (前端看到"加载中")。

    现在:
    - 用独立 SessionLocal() 写入,失败 rollback 该独立 session,**不污染调用方 db**
    - 失败只 log error,不 raise(审计是 best-effort,不应阻塞业务)
    - 返回 bool 标识是否成功,调用方可选判断

    user 可为 User 对象或 None(系统任务); username 取 user.username。
    """
    user_id = getattr(user, "id", None)
    username = getattr(user, "username", "") or ""
    entry = AuditLog(
        user_id=user_id,
        username=username,
        action=action,
        detail=detail or "",
        ip=ip or "",
    )
    audit_db = SessionLocal()
    try:
        audit_db.add(entry)
        audit_db.commit()
        return True
    except Exception as e:
        # 审计失败必须 log error,不能静默吞(memory #22 教训)
        audit_db.rollback()
        logger.error(
            "audit write failed: action=%s user=%s err=%s\n%s",
            action, username, e, traceback.format_exc(),
        )
        return False
    finally:
        audit_db.close()


@router.get("")
def list_audit(
    limit: int = Query(200, ge=1, le=1000),
    user: str = Query("", description="按用户名筛选, 空=全部用户"),
    owner=Depends(require_owner),
    db: Session = Depends(get_db),
):
    """最近审计日志(仅 owner): 按时间倒序, 默认最近 200 条; 可按用户名筛选。"""
    q = db.query(AuditLog)
    if user.strip():
        q = q.filter(AuditLog.username == user.strip())
    rows = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).all()
    return {
        "logs": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": r.username,
                "action": r.action,
                "detail": r.detail,
                "ip": r.ip,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
        "users": [u[0] for u in db.query(AuditLog.username).distinct().order_by(AuditLog.username).all() if u[0]],
    }
