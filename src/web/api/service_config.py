"""服务间配置下发 (2026-09-08 T7): forecast 容器的唯一配置通道。

背景: forecast 此前直读主库 sqlite 文件(旧环境变量)拿 JWT/LLM 配置/裁判场景
绑定, 主库切 PG 后三条通道全部静默失效(回落硬编码兜底, 用户设置页配置被无视)。
本端点把 forecast 需要的三类配置一次性下发, 仅服务令牌(X-Service-Token)可通过。

只读; api_key 仅下发给已通过服务鉴权的 forecast 容器。
2026-09-08 风险方案 0.0: 依赖从 get_user_or_service 收紧为 get_service_principal
—— 本端点下发明文 api_key, 用户 JWT(哪怕 admin)也不得读取。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.web.api.auth import get_service_principal
from src.web.database import get_db
from src.web.models import AISceneBinding, AIService, AIModel, AppSettings

logger = logging.getLogger(__name__)
router = APIRouter()

_FORECAST_LLM_KEYS = ("forecast_llm_base_url", "forecast_llm_model", "forecast_llm_api_key")


@router.get("/forecast-config")
def get_forecast_config(
    db: Session = Depends(get_db),
    _principal=Depends(get_service_principal),
):
    """forecast 引擎配置下发(仅服务令牌; 用户 JWT 一律 403)。

    返回:
      - llm: app_settings.forecast_llm_*(情绪打分 LLM); 未配置时各项为空串
      - referee: ai_scene_bindings 的 referee 场景绑定 + 对应 ai_models/ai_services
        连接信息; 未绑定为 null(引擎侧按 abstain 处理, 不回落硬编码模型)
    """
    rows = (
        db.query(AppSettings)
        .filter(AppSettings.key.in_(_FORECAST_LLM_KEYS))
        .all()
    )
    llm = {r.key.removeprefix("forecast_llm_"): (r.value or "") for r in rows}
    llm_out = {
        "base_url": llm.get("base_url", ""),
        "model": llm.get("model", ""),
        "api_key": llm.get("api_key", ""),
    }

    referee = None
    binding = db.query(AISceneBinding).filter(AISceneBinding.scene == "referee").first()
    if binding and binding.model_id:
        row = (
            db.query(AIModel, AIService)
            .join(AIService, AIService.id == AIModel.service_id)
            .filter(AIModel.id == binding.model_id)
            .first()
        )
        if row:
            m, s = row
            referee = {
                "ai_model_id": int(m.id),
                "model": m.model or "",
                "name": m.name or "",
                "base_url": s.base_url or "",
                "api_key": s.api_key or "",
            }

    return {"llm": llm_out, "referee": referee}
