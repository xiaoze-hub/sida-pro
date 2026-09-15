"""高价值接口调用日志(2026-09-15 C.1)。

L2/暗盘/机会/预测等接口每次调用落一条 high_value_api_logs,
供后台用量报表(C.2)和异常检测(单 key 突增 10 倍自动冻结)使用。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def log_high_value_call(
    db: Session,
    user,
    api_name: str,
    symbol: str = "",
) -> None:
    """记录一次高价值接口调用(best-effort, 失败不影响主流程)。"""
    try:
        from src.db.models import HighValueApiLog

        db.add(HighValueApiLog(
            user_id=getattr(user, "id", "") or "",
            username=getattr(user, "username", "") or "",
            api_name=api_name,
            symbol=symbol or "",
        ))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("高价值接口日志落库失败: %r", e)


def check_spike_and_alert(
    db: Session,
    user_id: str,
    api_name: str,
    window_minutes: int = 10,
    spike_threshold: float = 10.0,
) -> bool:
    """检测单用户单接口突增(对比前一个等长窗口)。突增则告警并返回 True。"""
    try:
        from src.db.models import HighValueApiLog, AuditLog, User

        now = datetime.now(timezone.utc)
        window = timedelta(minutes=window_minutes)
        cur_start = now - window
        prev_start = cur_start - window

        cur = db.query(HighValueApiLog).filter(
            HighValueApiLog.user_id == user_id,
            HighValueApiLog.api_name == api_name,
            HighValueApiLog.created_at >= cur_start,
        ).count()
        prev = db.query(HighValueApiLog).filter(
            HighValueApiLog.user_id == user_id,
            HighValueApiLog.api_name == api_name,
            HighValueApiLog.created_at >= prev_start,
            HighValueApiLog.created_at < cur_start,
        ).count()

        # 前窗口 <10 次时基数太小, 不告警(避免新用户首次使用误报)
        if prev < 10:
            return False
        if cur < prev * spike_threshold:
            return False

        # 突增: 记审计 + 通知 admin
        user = db.query(User).filter(User.id == user_id).first()
        username = user.username if user else user_id
        detail = f"接口 {api_name} 突增: 前{window_minutes}min {prev}次 → 本{window_minutes}min {cur}次({spike_threshold:.0f}x)"
        logger.warning("[高价值接口突增] user=%s %s", username, detail)
        db.add(AuditLog(
            user_id=user_id,
            username=username,
            action="hv_api_spike",
            detail=detail,
        ))
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("突增检测失败: %r", e)
        return False
