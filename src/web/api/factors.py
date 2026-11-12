"""因子权重 API(M5):只读列表 + 手动覆盖(pin / 设权重 / 开关自动标定)
+ 标定提案(默认只建议、不自动生效)。

响应由 ResponseWrapperMiddleware 统一包成 {code,data,message},路由直接返回原始数据。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.factor_calibration import apply_pending_proposal, calibrate_factor_weights
from src.core.factor_weights import get_all_factor_weights, set_factor_weight
from src.web.database import get_db

router = APIRouter()


class FactorWeightUpdate(BaseModel):
    """手动覆盖入参,均可选(只传要改的字段)。"""

    weight: float | None = None
    is_pinned: bool | None = None
    auto_calibrate: bool | None = None


class ProposalApply(BaseModel):
    """待审提案落地入参:approved_by 必填(人工授权留痕)。"""

    approved_by: str


@router.get("/weights")
def list_weights(db: Session = Depends(get_db)):
    """列出所有市场 × 因子的权重 + 最近 IC/IR 观测 + 待审提案。"""
    return {"items": get_all_factor_weights(db=db)}


@router.post("/weights/{factor_code}/{market}")
def update_weight(
    factor_code: str, market: str, payload: FactorWeightUpdate,
    db: Session = Depends(get_db),
):
    """手动覆盖某因子权重 / pin / 开关自动标定(权重变化写 manual 审计)。"""
    try:
        return set_factor_weight(
            factor_code, market,
            weight=payload.weight, is_pinned=payload.is_pinned,
            auto_calibrate=payload.auto_calibrate, db=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/proposals/{market}/refresh")
def refresh_proposals(market: str, db: Session = Depends(get_db)):
    """显式生成一轮标定提案并持久化到权重行 meta(不改有效权重、不建行)。"""
    res = calibrate_factor_weights(market, mode="suggest", persist_proposals=True, db=db)
    if res.get("error"):
        raise HTTPException(status_code=500, detail=res["error"])
    return res


@router.post("/proposals/{factor_code}/{market}/apply")
def apply_proposal(
    factor_code: str, market: str, payload: ProposalApply,
    db: Session = Depends(get_db),
):
    """人工授权后落地待审提案(复用 manual 路径:写权重 + manual 审计)。"""
    try:
        return apply_pending_proposal(
            factor_code, market, approved_by=payload.approved_by, db=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
