"""定时报告订阅 API(2026-08-10 阶段4)。

每个用户可配置"我要收哪些定时报告"(盘前/盘中/复盘/预测)。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.web.database import get_db
from src.web.api.auth import get_current_user
from src.web.models import ReportSubscription, User

router = APIRouter()

REPORT_TYPES = {
    "premarket": "盘前前瞻",
    "intraday": "盘中监控",
    "review": "盘后复盘",
    "prediction": "多模型预测",
}


class SubscriptionUpdate(BaseModel):
    report_type: str
    enabled: bool


def _sub_to_dict(s: ReportSubscription) -> dict:
    return {
        "report_type": s.report_type,
        "enabled": s.enabled,
        "label": REPORT_TYPES.get(s.report_type, s.report_type),
    }


@router.get("/subscriptions")
async def list_subscriptions(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """当前用户的订阅列表(默认全部开启)。"""
    subs = db.query(ReportSubscription).filter(ReportSubscription.user_id == user.id).all()
    sub_map = {s.report_type: s.enabled for s in subs}
    # 未显式订阅的默认开启(首次使用)
    result = [
        {
            "report_type": rt,
            "enabled": sub_map.get(rt, True),
            "label": label,
        }
        for rt, label in REPORT_TYPES.items()
    ]
    return {"subscriptions": result}


@router.put("/subscriptions/{report_type}")
async def update_subscription(
    report_type: str,
    data: SubscriptionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """开启/关闭某类定时报告。"""
    if report_type not in REPORT_TYPES:
        raise HTTPException(400, f"不支持的报告类型: {report_type}")

    sub = db.query(ReportSubscription).filter(
        ReportSubscription.user_id == user.id,
        ReportSubscription.report_type == report_type,
    ).first()
    if sub:
        sub.enabled = data.enabled
    else:
        sub = ReportSubscription(user_id=user.id, report_type=report_type, enabled=data.enabled)
        db.add(sub)
    db.commit()
    return {"report_type": report_type, "enabled": data.enabled}
