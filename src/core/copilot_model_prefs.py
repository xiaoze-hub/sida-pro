"""参谋/对话场景的「用户显式模型选择」存取（2026-10-25）。

为什么需要这张表：`ai_scene_bindings` 是**全局**场景绑定（一处改，全员变），
用户想「这轮用便宜模型 / 这轮用强模型」没有地方落。`user_ai_prefs` 只记
`(user_id, scene) → model_id`，解析器在**授权范围内**优先用它。

设计红线：
  - 只存选择，不存凭证（key / base_url 一律不进这张表）；
  - **不校验授权**由调用方负责？不 —— 写入口（API）校验，读入口（解析器）也校验，
    两处都查：API 防越权写入，解析器防历史脏数据越权生效；
  - 缺失 = 跟随场景绑定（不要用 model_id=0 之类的哨兵值）。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def get_pref(db: Session, user_id, scene: str) -> int | None:
    """读用户在某场景选中的 AIModel.id；未设置返回 None。"""
    from src.db.models import UserAiPref

    uid = str(user_id or "")
    if not uid:
        return None
    try:
        row = (
            db.query(UserAiPref)
            .filter(UserAiPref.user_id == uid, UserAiPref.scene == scene)
            .first()
        )
    except Exception:  # noqa: BLE001 —— 表未迁移时不能让整个解析器挂掉
        from src.core.db_safe import safe_rollback

        safe_rollback(db)
        logger.debug("读取模型偏好失败(表可能未迁移): scene=%s", scene, exc_info=True)
        return None
    return int(row.model_id) if row is not None and row.model_id is not None else None


def set_pref(db: Session, user_id, scene: str, model_id: int) -> None:
    """写入/覆盖用户选择（幂等）。"""
    from src.db.models import UserAiPref

    uid = str(user_id or "")
    if not uid:
        raise ValueError("user_id 不能为空")
    row = (
        db.query(UserAiPref)
        .filter(UserAiPref.user_id == uid, UserAiPref.scene == scene)
        .first()
    )
    if row is None:
        row = UserAiPref(user_id=uid, scene=scene, model_id=int(model_id))
        db.add(row)
    else:
        row.model_id = int(model_id)
        row.updated_at = datetime.utcnow()
    db.commit()


def clear_pref(db: Session, user_id, scene: str) -> bool:
    """清除用户选择（回到跟随场景绑定）。返回是否真的删掉了行。"""
    from src.db.models import UserAiPref

    uid = str(user_id or "")
    if not uid:
        return False
    row = (
        db.query(UserAiPref)
        .filter(UserAiPref.user_id == uid, UserAiPref.scene == scene)
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
