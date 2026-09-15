"""Skill Gateway — 对外开放 skill 的 HTTP API(2026-09-15)。

设计(任务清单 Phase 1):
- `POST /api/keys` 手机号/微信标识领 AppKey(PG 落库, key_hash + 额度 + 状态)
- `POST /api/skills/{name}/run` 鉴权后执行 skill, **只返回结果字段, skill 原文绝不外泄**
- `X-API-Key` 鉴权: 无 key→401, 禁用/欠费→403
- Redis 限流: `skill_quota:{key}:{day}` 免费 100/天, `skill_burst:{key}:{minute}` 20/分
- 计量: `skill_usage` 表
- 新人 trial: 10 天 500 次/天

红线:
- 密钥不进代码/日志; key_hash 用 sha256+盐
- 只开放白名单工具(行情/技术/资金等); 持仓/自选/通知/网页抓取等禁止
- LLM 工具只回结构化结果, 不回 prompt 原文
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.agents.chat.registry import CHAT_TOOL_REGISTRY
from src.web.database import get_db
from src.web.models import SkillApiKey, SkillUsage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["skill-gateway"])

# 盐: 环境变量优先, 缺省用随机(每次进程重启会换, 只影响新 key 校验 — 生产必须设)
_KEY_SALT = os.getenv("SKILL_KEY_SALT", "")

# ── 开放白名单 ──────────────────────────────────────────────────────
# Phase 1: 只开放行情/技术/资金/新闻类; 个人数据与 SSRF 面一律禁止
OPEN_SKILLS: dict[str, dict[str, Any]] = {
    # name: {tier_min: free|trial|pro, slow: bool}
    "get_stock_quote": {"tier_min": "free", "slow": False},
    "get_technical_analysis": {"tier_min": "free", "slow": False},
    "get_main_intent": {"tier_min": "free", "slow": False},
    "get_decision_pioneer": {"tier_min": "free", "slow": False},
    "get_rally_analysis": {"tier_min": "free", "slow": False},
    "get_capital_flow": {"tier_min": "free", "slow": False},
    "get_market_news": {"tier_min": "free", "slow": False},
    "get_kline_patterns": {"tier_min": "free", "slow": False},
    "get_auction_data": {"tier_min": "free", "slow": False},
    "get_sentiment_cycle": {"tier_min": "free", "slow": False},
    "get_market_anomalies": {"tier_min": "free", "slow": False},
    "get_northbound": {"tier_min": "free", "slow": False},
    "get_hot_stocks": {"tier_min": "free", "slow": False},
    "get_main_flow_compare": {"tier_min": "free", "slow": False},
    "get_opportunities": {"tier_min": "free", "slow": False},
    "get_strategy_signals": {"tier_min": "free", "slow": False},
    "get_fundamentals_detail": {"tier_min": "free", "slow": False},
    "get_forecast": {"tier_min": "pro", "slow": True},
    "get_delta_series": {"tier_min": "pro", "slow": True},
    "get_orderbook": {"tier_min": "pro", "slow": True},
    "get_event_catalyst": {"tier_min": "pro", "slow": True},
    "get_intent_explain": {"tier_min": "pro", "slow": True},
    "get_factor_ic_report": {"tier_min": "pro", "slow": True},
}

# 明确禁止外放(个人数据 / SSRF / 账号依赖)
BLOCKED_SKILLS = {
    "get_portfolio", "get_watchlist", "get_notifications", "get_stock_suggestions",
    "get_web_content", "tdx_wenda", "get_irm_qa",
}

# 档位默认日限
TIER_DAILY_LIMIT = {"trial": 500, "free": 100, "pro": 5000}
TIER_RANK = {"free": 0, "trial": 1, "pro": 2}
TRIAL_DAYS = 10

# 风险提示(合规: 所有返回强制带)
RISK_DISCLAIMER = (
    "本结果由算法/数据源生成, 仅供参考, 不构成任何投资建议; "
    "股市有风险, 入市需谨慎。"
)


# ── Key 工具 ────────────────────────────────────────────────────────

def _hash_key(raw_key: str) -> str:
    salt = _KEY_SALT or "sida-skill-gw"
    return hashlib.sha256(f"{salt}:{raw_key}".encode("utf-8")).hexdigest()


def _gen_key() -> str:
    """生成 sk_ 前缀的 AppKey(明文只在创建响应里出现一次)。"""
    return "sk_" + secrets.token_urlsafe(32)


# ── 限流 ────────────────────────────────────────────────────────────

_MEM_DAY: dict[str, tuple[str, int]] = {}  # (day, count)
_MEM_MIN: dict[str, tuple[int, int]] = {}  # (minute_bucket, count)


def _redis_client():
    """同步 Redis(biz_cache 同款); 不可用返回 None, 限流回退内存。"""
    try:
        if os.getenv("REDIS_DISABLED", "").lower() in ("1", "true", "yes"):
            return None
        from redis import Redis

        url = os.getenv("REDIS_URL", "redis://localhost:6379/0").strip()
        rc = Redis.from_url(
            url, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=1.0, socket_timeout=2.0,
        )
        rc.ping()
        return rc
    except Exception:  # noqa: BLE001
        return None


def _check_rate_limit(key_hash: str, daily_limit: int, minute_limit: int = 20) -> None:
    """超限抛 HTTPException 429。Redis 失败时回退进程内存(单机兜底)。"""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    minute = int(time.time() // 60)
    day_key = f"skill_quota:{key_hash[:16]}:{day}"
    min_key = f"skill_burst:{key_hash[:16]}:{minute}"

    rc = _redis_client()
    if rc is not None:
        try:
            d = int(rc.incr(day_key))
            if d == 1:
                rc.expire(day_key, 86400)
            if d > daily_limit:
                raise HTTPException(429, f"日配额已用尽({daily_limit} 次/天), 请升级 Pro 或明日再试")
            m = int(rc.incr(min_key))
            if m == 1:
                rc.expire(min_key, 60)
            if m > minute_limit:
                raise HTTPException(429, f"触发频率限制({minute_limit} 次/分), 请稍后重试")
            return
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug("redis 限流失败, 回退内存: %r", e)

    # 内存兜底
    d_count = _MEM_DAY.get(key_hash)
    if not d_count or d_count[0] != day:
        _MEM_DAY[key_hash] = (day, 1)
        d_n = 1
    else:
        d_n = d_count[1] + 1
        _MEM_DAY[key_hash] = (day, d_n)
    if d_n > daily_limit:
        raise HTTPException(429, f"日配额已用尽({daily_limit} 次/天)")
    m_count = _MEM_MIN.get(key_hash)
    if not m_count or m_count[0] != minute:
        _MEM_MIN[key_hash] = (minute, 1)
        m_n = 1
    else:
        m_n = m_count[1] + 1
        _MEM_MIN[key_hash] = (minute, m_n)
    if m_n > minute_limit:
        raise HTTPException(429, f"触发频率限制({minute_limit} 次/分)")


# ── 鉴权 ────────────────────────────────────────────────────────────

def _get_api_key(x_api_key: str = Header(default="", alias="X-API-Key"), db: Session = Depends(get_db)) -> SkillApiKey:
    if not x_api_key or not x_api_key.startswith("sk_"):
        raise HTTPException(401, "缺少或无效的 X-API-Key(格式 sk_...)。请先 POST /api/keys 领取。")
    h = _hash_key(x_api_key)
    row = db.query(SkillApiKey).filter(SkillApiKey.key_hash == h).first()
    if not row:
        raise HTTPException(401, "无效的 API Key")
    if row.status == "disabled":
        raise HTTPException(403, "该 Key 已被禁用")
    if row.status == "frozen":
        raise HTTPException(403, f"该 Key 已被冻结: {row.frozen_reason or '异常用量'}")
    # trial 到期自动降 free
    if row.tier == "trial" and row.expires_at:
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            row.tier = "free"
            row.daily_limit = TIER_DAILY_LIMIT["free"]
            db.commit()
    return row


def _check_skill_tier(name: str, row: SkillApiKey) -> None:
    meta = OPEN_SKILLS.get(name)
    if not meta:
        if name in BLOCKED_SKILLS:
            raise HTTPException(403, f"该 skill 不对外开放: {name}")
        raise HTTPException(404, f"未知 skill: {name}")
    need = meta["tier_min"]
    if TIER_RANK[row.tier] < TIER_RANK[need]:
        raise HTTPException(403, f"skill {name} 需要 {need} 档位(当前 {row.tier})")


# ── 请求/响应模型 ────────────────────────────────────────────────────

class KeyRegisterRequest(BaseModel):
    owner_label: str = Field(default="", max_length=64, description="手机号或微信标识(可选)")
    trial: bool = Field(default=True, description="是否领取 10 天试用(500次/天)")


class SkillRunRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


class SkillRunResponse(BaseModel):
    skill: str
    result: str
    caliber: str
    risk: str
    duration_ms: int


# ── 路由 ────────────────────────────────────────────────────────────

@router.get("/skills")
def list_skills(row: SkillApiKey = Depends(_get_api_key)) -> dict:
    """列出当前 key 可调用的 skill(含 schema)。"""
    out = []
    for name, meta in OPEN_SKILLS.items():
        tool = CHAT_TOOL_REGISTRY.get(name)
        allowed = TIER_RANK[row.tier] >= TIER_RANK[meta["tier_min"]]
        out.append({
            "name": name,
            "allowed": allowed,
            "tier_min": meta["tier_min"],
            "slow": meta["slow"],
            "caliber": tool.caliber if tool else "",
            "schema": (tool.schema or {}).get("function", {}) if tool and tool.schema else None,
        })
    return {
        "tier": row.tier,
        "daily_limit": row.daily_limit,
        "skills": out,
        "blocked": sorted(BLOCKED_SKILLS),
    }


@router.post("/skills/{name}/run", response_model=SkillRunResponse)
async def run_skill(
    name: str,
    body: SkillRunRequest,
    row: SkillApiKey = Depends(_get_api_key),
    db: Session = Depends(get_db),
) -> SkillRunResponse:
    """执行开放 skill。只返回 handler 输出字符串 + 口径 + 风险提示。"""
    _check_skill_tier(name, row)
    _check_rate_limit(row.key_hash, row.daily_limit, minute_limit=20)

    tool = CHAT_TOOL_REGISTRY.get(name)
    if tool is None:
        raise HTTPException(404, f"未知 skill: {name}")

    t0 = time.monotonic()
    status_code = 200
    try:
        # 开放 skill 均为行情/市场数据类, 不传 user(个人数据类已被 BLOCKED)
        result = await tool.handler(db, body.args or {}, None)
        if not isinstance(result, str):
            result = str(result)
        duration_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        status_code = 500
        duration_ms = int((time.monotonic() - t0) * 1000)
        _log_usage(db, row.id, name, status_code, duration_ms)
        raise HTTPException(500, f"skill 执行失败: {e!r}")

    _log_usage(db, row.id, name, status_code, duration_ms)
    row.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return SkillRunResponse(
        skill=name,
        result=result,
        caliber=tool.caliber,
        risk=RISK_DISCLAIMER,
        duration_ms=duration_ms,
    )


def _log_usage(db: Session, key_id: int, skill: str, code: int, duration_ms: int) -> None:
    try:
        db.add(SkillUsage(
            api_key_id=key_id, skill_name=skill, status_code=code, duration_ms=duration_ms,
        ))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("skill_usage 落库失败: %r", e)


# ── Key 注册 / 用量 ─────────────────────────────────────────────────

@router.post("/keys")
def register_key(
    body: KeyRegisterRequest,
    db: Session = Depends(get_db),
) -> dict:
    """领取 AppKey。明文 key 只在本次响应返回一次, 服务端只存 hash。"""
    raw = _gen_key()
    h = _hash_key(raw)
    tier = "trial" if body.trial else "free"
    daily = TIER_DAILY_LIMIT[tier]
    expires = None
    if tier == "trial":
        expires = datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
    row = SkillApiKey(
        key_hash=h,
        key_prefix=raw[:11],
        owner_label=(body.owner_label or "").strip()[:64],
        tier=tier,
        status="active",
        daily_limit=daily,
        expires_at=expires,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "api_key": raw,  # 仅此一次
        "key_prefix": row.key_prefix,
        "tier": row.tier,
        "daily_limit": row.daily_limit,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "note": "请妥善保存 API Key, 服务端不会再次展示明文。",
        "risk": RISK_DISCLAIMER,
    }


@router.get("/usage")
def get_usage(
    row: SkillApiKey = Depends(_get_api_key),
    db: Session = Depends(get_db),
) -> dict:
    """自查当日用量与剩余配额。"""
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today = db.query(SkillUsage).filter(
        SkillUsage.api_key_id == row.id,
        SkillUsage.created_at >= day_start,
    ).count()
    remaining = max(0, row.daily_limit - today)
    return {
        "tier": row.tier,
        "status": row.status,
        "daily_limit": row.daily_limit,
        "used_today": today,
        "remaining_today": remaining,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


class ProApplyRequest(BaseModel):
    contact: str = Field(..., max_length=64, description="联系方式(手机/微信)")
    reason: str = Field(default="", max_length=200)


@router.post("/pro/apply")
def apply_pro(
    body: ProApplyRequest,
    row: SkillApiKey = Depends(_get_api_key),
    db: Session = Depends(get_db),
) -> dict:
    """Pro 申请(Phase 2: 第一批人工审核, 不自动升级)。"""
    logger.info(
        "Pro 申请 key=%s contact=%s reason=%s",
        row.key_prefix, body.contact[:20], body.reason[:50],
    )
    return {
        "status": "pending_review",
        "message": "申请已提交, 请等待人工审核(1-2 个工作日)。审核通过后额度自动升至 5000 次/天。",
    }
