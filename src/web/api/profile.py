"""个人中心 API(2026-08-15 SIDA 完整度评估 P1): 个人资料 + 我的数据。

- GET /api/profile        → 当前用户信息(nickname/avatar/username/role/created_at)
- PUT /api/profile        → 更新昵称/头像(昵称 1-32 字; 头像 base64 data URL <200KB 或空串清空)
- GET /api/profile/stats  → 我的数据: 预测命中率 / 自选数 / 持仓数 / 影子账户画像有无
- 修改密码复用 /api/auth/change-password(前端安全区直接调用)

头像存储策略(与 /settings/avatar 一致): DB(users.avatar, String(255))只存文件名,
图片本体落盘到 data/avatars/, GET 时转 data URL 返回。避免 200KB 级 base64 塞进
users 行(导出/审计等并行子任务也在读 users 表, 保持行内轻量)。

预测命中率说明: agent_prediction_outcomes 表无 user_id 列(记录维度是
agent_name/stock_symbol/prediction_date), 无法按当前用户过滤 → 统计全平台并显式标注
scope=global。命中定义: outcome_status='evaluated' 且有收益的记录中, buy/add 且
收益>0、reduce/sell/avoid 且收益<0 记命中; watch/hold/alert 等中性动作不计入分母。
"""
import base64
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import AgentPredictionOutcome, Position, Stock, User

router = APIRouter()

NICKNAME_MAX_LEN = 32
AVATAR_MAX_BYTES = 200 * 1024  # 200KB(解码后)

# 方向性动作 → 命中方向; 中性动作(watch/hold/alert/未知)不计入命中率
_BULLISH_ACTIONS = {"buy", "add"}
_BEARISH_ACTIONS = {"reduce", "sell", "avoid"}


# ── 头像落盘/读取(与 settings.py 的 ui_avatar 同套机制, 按用户隔离文件名) ──────────

def _avatar_dir() -> str:
    d = os.path.join(os.environ.get("DATA_DIR", "./data"), "avatars")
    os.makedirs(d, exist_ok=True)
    return d


def _remove_avatar_file(fname: str | None) -> None:
    if not fname:
        return
    try:
        os.remove(os.path.join(_avatar_dir(), fname))
    except OSError:
        pass


def _read_avatar_data_url(fname: str | None) -> str:
    """从文件名读回 data URL; 无头像/文件丢失返回空串。"""
    if not fname:
        return ""
    path = os.path.join(_avatar_dir(), fname)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    mime = "image/png" if fname.lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _save_avatar(value: str, user_id: str, old_fname: str | None) -> str | None:
    """校验并落盘头像 data URL, 返回新文件名; 空串=清空返回 None。"""
    value = (value or "").strip()
    if not value:
        _remove_avatar_file(old_fname)
        return None

    if not (value.startswith("data:") and "," in value):
        raise HTTPException(400, "头像需为 data URL")
    header, b64 = value.split(",", 1)
    if "image/" not in header:
        raise HTTPException(400, "头像需为图片 data URL")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, "头像数据无效")
    if not raw:
        raise HTTPException(400, "头像数据无效")
    if len(raw) > AVATAR_MAX_BYTES:
        raise HTTPException(400, "头像不能超过 200KB")

    ext = "png" if "image/png" in header else "jpg"
    fname = f"avatar_u{user_id}.{ext}"
    if old_fname and old_fname != fname:
        _remove_avatar_file(old_fname)
    try:
        with open(os.path.join(_avatar_dir(), fname), "wb") as f:
            f.write(raw)
    except OSError:
        raise HTTPException(500, "头像保存失败")
    return fname


# ── 数据组装 ──────────────────────────────────────────────────────────

def _profile_to_dict(user: User) -> dict:
    return {
        "username": user.username,
        "nickname": user.nickname or "",
        "avatar": _read_avatar_data_url(user.avatar),
        "role": user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _prediction_hit_stats(db: Session) -> dict:
    """预测命中率(agent_prediction_outcomes 无 user_id → 全平台统计, 标注 scope=global)。"""
    rows = (
        db.query(
            AgentPredictionOutcome.action,
            AgentPredictionOutcome.outcome_return_pct,
        )
        .filter(
            AgentPredictionOutcome.outcome_status == "evaluated",
            AgentPredictionOutcome.outcome_return_pct.isnot(None),
        )
        .all()
    )
    hit = 0
    total = 0
    for action, ret in rows:
        action = (action or "").lower()
        if action in _BULLISH_ACTIONS:
            total += 1
            if ret > 0:
                hit += 1
        elif action in _BEARISH_ACTIONS:
            total += 1
            if ret < 0:
                hit += 1
        # 中性动作(watch/hold/alert/未知)不计入
    return {
        "hit_count": hit,
        "total": total,
        "hit_rate": round(hit / total * 100, 1) if total else None,
        "scope": "global",
        "note": "预测记录无用户维度, 按全平台统计",
    }


# ── API ───────────────────────────────────────────────────────────────

class ProfileUpdate(BaseModel):
    nickname: str | None = None
    avatar: str | None = None


@router.get("")
def get_profile(user: User = Depends(get_current_user)):
    """当前用户信息(nickname/avatar/username/role/created_at)。"""
    return _profile_to_dict(user)


@router.put("")
def update_profile(
    data: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新昵称/头像。昵称 1-32 字; avatar 为 base64 data URL(<200KB)或空串清空。"""
    if data.nickname is not None:
        nickname = data.nickname.strip()
        if not nickname:
            raise HTTPException(400, "昵称不能为空")
        if len(nickname) > NICKNAME_MAX_LEN:
            raise HTTPException(400, "昵称最多 32 个字")
        user.nickname = nickname

    if data.avatar is not None:
        user.avatar = _save_avatar(data.avatar, user.id, old_fname=user.avatar)

    db.commit()
    db.refresh(user)
    # 审计(2026-08-15 评审 B 补覆盖)
    try:
        from src.web.api.audit import log_audit
        parts = []
        if data.nickname is not None:
            parts.append("昵称")
        if data.avatar is not None:
            parts.append("头像")
        if parts:
            log_audit(db, user, "update_profile", detail="更新" + "/".join(parts), ip="")
    except Exception:
        pass
    return _profile_to_dict(user)


@router.get("/stats")
def profile_stats(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """我的数据: 预测命中率(全局)/自选数/持仓数/影子账户画像有无。"""
    watchlist_count = (
        db.query(func.count(Stock.id))
        .filter(or_(Stock.user_id == user.id, Stock.user_id.is_(None)))
        .scalar()
        or 0
    )
    position_count = (
        db.query(func.count(Position.id))
        .filter(or_(Position.user_id == user.id, Position.user_id.is_(None)))
        .scalar()
        or 0
    )
    shadow_profile = user.shadow_profile_json or {}
    return {
        "prediction": _prediction_hit_stats(db),
        "watchlist_count": watchlist_count,
        "position_count": position_count,
        "has_shadow_profile": bool(shadow_profile),
    }
