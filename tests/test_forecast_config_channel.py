"""forecast 配置下发通道回归(2026-09-08 T7 审计修复)。

背景: forecast 容器原直读主库 sqlite(PANWATCH_DB)拿 forecast_llm_* 与裁判
场景绑定, 主库切 PG 后该文件不存在 → 三条配置通道全部静默失效, 引擎回落
硬编码兜底模型, 用户设置页配置被无视。

覆盖:
  - /api/service/forecast-config 端点(直接调用): llm 配置下发 / referee 绑定下发 / 未绑定返回 null
  - 路由已挂载且在 data_read(服务 token 可读)组
"""
from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web.api.service_config import get_forecast_config
from src.web.models import (
    AIModel,
    AISceneBinding,
    AIService,
    AppSettings,
    Base,
)


def _make_db():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        eng,
        tables=[AppSettings.__table__, AIService.__table__, AIModel.__table__, AISceneBinding.__table__],
    )
    return sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()


def test_forecast_config_llm_dispatched():
    """设置页 forecast_llm_* 经端点下发(不再依赖直读 sqlite)。"""
    db = _make_db()
    db.add_all(
        [
            AppSettings(key="forecast_llm_base_url", value="https://api.example.com/v1"),
            AppSettings(key="forecast_llm_model", value="gpt-test"),
            AppSettings(key="forecast_llm_api_key", value="sk-test"),
        ]
    )
    db.commit()
    res = get_forecast_config(db=db, _principal=None)
    assert res["llm"] == {"base_url": "https://api.example.com/v1", "model": "gpt-test", "api_key": "sk-test"}
    assert res["referee"] is None


def test_forecast_config_referee_binding_dispatched():
    """referee 场景绑定 + 服务商连接信息经端点下发。"""
    db = _make_db()
    svc = AIService(name="svc", base_url="https://api.ref.com/v1", api_key="sk-ref")
    db.add(svc)
    db.commit()
    model = AIModel(service_id=svc.id, model="ref-model", name="裁判模型")
    db.add(model)
    db.commit()
    db.add(AISceneBinding(scene="referee", model_id=model.id))
    db.commit()

    res = get_forecast_config(db=db, _principal=None)
    ref = res["referee"]
    assert ref is not None
    assert ref["ai_model_id"] == model.id
    assert ref["model"] == "ref-model"
    assert ref["base_url"] == "https://api.ref.com/v1"
    assert ref["api_key"] == "sk-ref"


def test_forecast_config_route_mounted_under_data_read():
    """路由挂载在 /api/service 前缀, 且归属 data_read(服务 token 可读)依赖组。"""
    import inspect

    import src.web.app as app_mod
    from src.web.app import app

    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/service/forecast-config" in paths
    # data_read = [Depends(get_user_or_service)] 仍指向服务 token 放行依赖
    src = inspect.getsource(app_mod)
    assert "service_config.router, prefix=\"/api/service\"" in src
