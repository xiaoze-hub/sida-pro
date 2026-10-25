"""参谋工作台写入型接口（2026-10-25）。

为什么单开一个模块而不是塞进 `src/web/api/copilot.py`：后者承担只读视图，
且 `src/web/api/chat.py` 有 2310 行硬护栏——所有"新增长期逻辑"一律落新文件。

本模块当前只做一件事：**让用户选择参谋用哪个模型**。

为什么不是"只加个下拉就好"：`chat.py` 的 `SendMessageBody` 没有 model 字段，
改它需要动 chat.py（行数护栏挡住）。但 chat.py 组装客户端时走的是
`get_model_for_scene(db, "chat", user=user)`——所以只要把"用户选中"这一层
做在解析器里（`user_ai_prefs` 表），选择就自然生效，chat.py 一行不用改。

越权防线：只能在**该用户已获授权**的模型里选。解析时按 `users.permissions.model_access`
校验，deny_all 直接失效、granted 只认 model_ids 内的模型。用户选不了自己没权限的模型。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.web.api.auth import get_current_user
from src.web.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot-workbench"])

# 参谋对话固定走 chat 场景（与 chat.py 的 get_model_for_scene(db, "chat", ...) 一致）
SCENE = "chat"


class ModelChoiceBody(BaseModel):
    # None = 清除选择，回到"跟随场景绑定"
    model_id: int | None = None


def _visible_models(db: Session, user: Any) -> list[dict]:
    """该用户可选/可见的模型清单（与解析器同一套授权口径）。

    这里不返回 api_key / base_url —— 前端只需要展示与选择，凭证不出后端。
    """
    from src.core.ai_client import _get_model_access  # noqa: PLC2701
    from src.db.models import AIModel, AIService

    access = _get_model_access(user) or {}
    mode = str(access.get("mode") or "inherit")
    if mode == "deny_all":
        return []

    q = db.query(AIModel)
    if mode == "granted":
        ids = [int(i) for i in (access.get("model_ids") or []) if str(i).isdigit()]
        if not ids:
            return []
        q = q.filter(AIModel.id.in_(ids))

    rows = q.order_by(AIModel.is_default.desc(), AIModel.id.asc()).all()
    svc_names = {
        s.id: s.name
        for s in db.query(AIService).filter(
            AIService.id.in_({r.service_id for r in rows if r.service_id})
        )
    }
    return [
        {
            "id": r.id,
            "model": r.model,
            "service": svc_names.get(r.service_id) or "--",
            "is_default": bool(r.is_default),
        }
        for r in rows
    ]


def _current_selection(db: Session, user: Any) -> dict:
    """当前生效的模型 + 来源（pref / scene / default）。

    `source` 必须如实说：用户看到"跟随场景绑定"就不能实际在用某个偏好模型。
    """
    from src.core.ai_client import get_model_for_scene, resolve_scene_model_with_source

    resolved = resolve_scene_model_with_source(db, SCENE, user=user)
    if resolved is not None:
        return resolved

    # 兜底：解析器未给出结构化结果时，至少把模型名报出去（不猜来源）
    model = get_model_for_scene(db, SCENE, user=user)
    if model is None:
        return {"source": "none", "model_id": None, "model": None, "service": None}
    if isinstance(model, dict):
        return {
            "source": "byok",
            "model_id": None,
            "model": model.get("model"),
            "service": model.get("name"),
        }
    return {
        "source": "scene",
        "model_id": getattr(model, "id", None),
        "model": getattr(model, "model", None),
        "service": None,
    }


@router.get("/model")
def get_copilot_model(
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """参谋页模型选择器数据：当前生效模型 + 来源 + 可选清单。"""
    return {
        "scene": SCENE,
        "current": _current_selection(db, user),
        "models": _visible_models(db, user),
    }


@router.put("/model")
def set_copilot_model(
    body: ModelChoiceBody,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """设置/清除本人在 chat 场景的模型选择。

    - `model_id=None` → 清除偏好，回到跟随场景绑定
    - 不在可见清单内 → 403（越权，不落库）
    - demo/guest → 403（与解析器一致：零授权）
    """
    from src.core.ai_client import _get_model_access  # noqa: PLC2701
    from src.core.copilot_model_prefs import clear_pref, set_pref

    uid = getattr(user, "id", None)
    if uid is None:
        raise HTTPException(status_code=401, detail="未登录")

    username = getattr(user, "username", None)
    role = getattr(user, "role", None)
    if username == "demo" or role == "guest":
        raise HTTPException(status_code=403, detail="访客账户不能选择模型")

    if body.model_id is None:
        clear_pref(db, uid, SCENE)
        return {"ok": True, "current": _current_selection(db, user)}

    allowed = {m["id"] for m in _visible_models(db, user)}
    if body.model_id not in allowed:
        access = _get_model_access(user) or {}
        raise HTTPException(
            status_code=403,
            detail=f"模型 {body.model_id} 不在你可用的模型范围内"
            f"（授权模式 {access.get('mode') or 'inherit'}）",
        )

    try:
        set_pref(db, uid, SCENE, int(body.model_id))
    except Exception as exc:  # noqa: BLE001 —— 写库失败必须回滚, 否则会话脏
        from src.core.db_safe import safe_rollback

        safe_rollback(db)
        logger.warning("保存模型偏好失败: %s", exc)
        raise HTTPException(status_code=500, detail="保存模型选择失败") from exc

    return {"ok": True, "current": _current_selection(db, user)}
