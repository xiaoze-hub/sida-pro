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

from fastapi import APIRouter, Depends, HTTPException, Query
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
    return _accuracy_board(db, since="")["overall"]


def _accuracy_board(db: Session, since: str = "", min_n: int = 1) -> dict:
    """分 Agent 命中榜(2026-09-08 方向3: 回测闭环的上半场 — 先看见谁准)。

    同 _prediction_hit_stats 口径: buy/add 涨>0 命中、reduce/sell/avoid 跌<0 命中,
    中性动作不计分母。since=YYYY-MM-DD 过滤 prediction_date(字符串比较)。
    样本 <min_n 的 agent 标 qualified=false(不参与排名, 防 1 中 1 刷榜)。
    """
    q = (
        db.query(
            AgentPredictionOutcome.agent_name,
            AgentPredictionOutcome.action,
            AgentPredictionOutcome.outcome_return_pct,
        )
        .filter(
            AgentPredictionOutcome.outcome_status == "evaluated",
            AgentPredictionOutcome.outcome_return_pct.isnot(None),
        )
    )
    if since:
        q = q.filter(AgentPredictionOutcome.prediction_date >= since)
    rows = q.all()

    per: dict[str, dict] = {}
    ohit = otot = 0
    oret_sum = 0.0
    oret_n = 0
    for agent, action, ret in rows:
        action = (action or "").lower()
        if action in _BULLISH_ACTIONS:
            good = ret > 0
        elif action in _BEARISH_ACTIONS:
            good = ret < 0
        else:
            continue
        otot += 1
        oret_sum += ret
        oret_n += 1
        cell = per.setdefault(agent or "unknown", {"hit": 0, "total": 0, "ret_sum": 0.0})
        cell["total"] += 1
        cell["ret_sum"] += ret
        if good:
            ohit += 1
            cell["hit"] += 1

    agents = []
    for agent, cell in per.items():
        n = cell["total"]
        agents.append({
            "agent": agent,
            "hit": cell["hit"],
            "total": n,
            "hit_rate": round(cell["hit"] / n * 100, 1) if n else None,
            "avg_return_pct": round(cell["ret_sum"] / n, 2) if n else None,
            "qualified": n >= min_n,
        })
    agents.sort(key=lambda a: (not a["qualified"], -(a["hit_rate"] or -1)))
    return {
        "since": since or None,
        "scope": "global",
        "note": "预测记录无用户维度, 按全平台统计",
        "overall": {
            "hit_count": ohit,
            "total": otot,
            "hit_rate": round(ohit / otot * 100, 1) if otot else None,
            "avg_return_pct": round(oret_sum / oret_n, 2) if oret_n else None,
            "scope": "global",
        },
        "agents": agents,
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


@router.get("/stats/accuracy")
def prediction_accuracy_board(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
    min_n: int = Query(5, ge=1, le=100),
):
    """分 Agent 命中榜(2026-09-08 方向3): 谁准谁不准, 一眼见。

    days=回看窗口(默认30天); min_n=参评最低样本(默认5, 防小样本刷榜)。
    下半场(因子自动降权)另开, 先把"看见"做实。
    """
    from datetime import date, timedelta

    since = (date.today() - timedelta(days=days)).isoformat()
    return _accuracy_board(db, since=since, min_n=min_n)


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
