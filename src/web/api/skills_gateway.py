"""Skill Gateway — 对外开放 skill 的 HTTP API(2026-09-15)。

设计(任务清单 Phase 1 + 统一身份 2026-09-16):
- `POST /api/keys` 手机号/微信标识领 AppKey(PG 落库, key_hash + 额度 + 状态)
- `POST /api/skills/{name}/run` 三通道鉴权后执行 skill, **只返回结果字段, skill 原文绝不外泄**
  - API Key(`X-API-Key`): channel='api'
  - JWT(`Authorization: Bearer`, 无 API Key 时): channel='web', 与该用户 key 共享配额
  - 游客(无 key 无 JWT): channel='guest', IP 24h 滑动窗口 10 次, 仅 free 档 skill
- Redis 限流: `skill_quota:{key}:{day}` 免费 100/天, `skill_burst:{key}:{minute}` 20/分
- 计量: `skill_usage` 表(channel + user_id)
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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.agents.chat.registry import CHAT_TOOL_REGISTRY
from src.web.database import get_db
from src.web.models import SkillApiKey, SkillUsage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["skill-gateway"])

# 盐: 环境变量优先; 未配置时生成随机盐(仅本进程有效)并告警。
# P2(audit-20260915): 禁止回落硬编码常量 —— 硬编码盐等于 key_hash 可被离线彩虹表碰撞。
_KEY_SALT = os.getenv("SKILL_KEY_SALT", "")
if not _KEY_SALT:
    _KEY_SALT = secrets.token_hex(16)
    logger.warning(
        "SKILL_KEY_SALT 未配置, 已生成随机盐(仅本进程有效)。"
        "生产环境必须设置 SKILL_KEY_SALT, 否则进程重启后已签发 key 校验会失效。"
    )

# ── 注册防刷(P1, audit-20260915): IP 级限流, 每小时最多 5 次 ─────────
_KEY_REG_MAX_PER_HOUR = 5
_KEY_REG_WINDOW_SEC = 3600
# {ip: [ts, ...]} 进程内滑动窗口; 单实例部署足够, 多实例需换 Redis
_key_reg_hits: dict[str, list[float]] = {}
_key_reg_lock = threading.Lock()


def _check_key_register_rate(ip: str) -> None:
    """超过每小时 5 次则 429。内存滑动窗口, 重启清零可接受。"""
    now = time.monotonic()
    with _key_reg_lock:
        hits = [t for t in _key_reg_hits.get(ip, []) if now - t < _KEY_REG_WINDOW_SEC]
        if len(hits) >= _KEY_REG_MAX_PER_HOUR:
            _key_reg_hits[ip] = hits
            retry_in = int(_KEY_REG_WINDOW_SEC - (now - hits[0])) + 1
            raise HTTPException(429, f"注册过于频繁, 请 {retry_in} 秒后重试")
        hits.append(now)
        _key_reg_hits[ip] = hits


# ── 游客试用(统一身份 2026-09-16): IP 级限流, 每天最多 10 次 ──────────
_guest_ip_counts: dict[str, list[float]] = {}
_guest_lock = threading.Lock()
GUEST_DAILY_LIMIT = 10
_GUEST_WINDOW_SEC = 86400  # 24h 滑动窗口


def _check_guest_rate(ip: str) -> None:
    """游客 IP 24h 滑动窗口, 每 IP 最多 GUEST_DAILY_LIMIT 次。超限 429。

    复用 _check_key_register_rate 的内存滑动窗口模式; 线程安全; 重启清零可接受。
    """
    now = time.monotonic()
    with _guest_lock:
        hits = [t for t in _guest_ip_counts.get(ip, []) if now - t < _GUEST_WINDOW_SEC]
        if len(hits) >= GUEST_DAILY_LIMIT:
            _guest_ip_counts[ip] = hits
            raise HTTPException(
                429,
                "请注册获取 API Key",
                headers={"Retry-After": str(_GUEST_WINDOW_SEC)},
            )
        hits.append(now)
        _guest_ip_counts[ip] = hits

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

# 投顾红线词(Phase 3.1): 输出含这些词时强制追加免责 + 记录
# 只做"追加提示+审计", 不改写业务结论(避免误伤技术指标描述)
_ADVISORY_RED_FLAGS = (
    "买入", "卖出", "建议买", "建议卖", "必涨", "必跌", "稳赚",
    "保证收益", "稳赚不赔", "翻倍", "翻几倍", "无风险",
    "满仓", "清仓", "抄底", "逃顶", "跟单",
)
_RED_LINE_NOTE = (
    "【合规提示】结果含可能被理解为投资建议的表述, 已按投顾红线处理: "
    "本内容仅为数据/算法输出, 不构成买卖建议。"
)


def _compliance_wrap(result: str) -> tuple[str, list[str]]:
    """扫描红线词, 命中则追加合规提示; 返回 (结果, 命中词列表)。"""
    hits = [w for w in _ADVISORY_RED_FLAGS if w in (result or "")]
    if not hits:
        return result, []
    wrapped = f"{result}\n\n{_RED_LINE_NOTE}"
    return wrapped, hits


# 异常冻结(Phase 2.3): 单 key 突增超阈值自动冻结
_FREEZE_WINDOW_S = 300  # 5 分钟窗口
_FREEZE_MULTIPLIER = 10
_RECENT_CALLS: dict[str, list[float]] = {}  # key_hash -> [timestamps]


def _detect_spike_and_freeze(db: Session, row: SkillApiKey) -> None:
    """5 分钟内调用次数突增超 10× 基线 → 冻结 + 告警。"""
    now = time.time()
    hist = _RECENT_CALLS.setdefault(row.key_hash, [])
    hist.append(now)
    # 保留窗口内
    cutoff = now - _FREEZE_WINDOW_S
    hist[:] = [t for t in hist if t >= cutoff]
    if len(hist) < 20:  # 样本太少不判
        return
    # 基线: 前一窗口(5分钟前~10分钟前)调用数
    prev_cutoff = now - 2 * _FREEZE_WINDOW_S
    prev = [t for t in hist if prev_cutoff <= t < cutoff]
    prev_n = max(len(prev), 1)
    if len(hist) >= prev_n * _FREEZE_MULTIPLIER and len(hist) >= 30:
        row.status = "frozen"
        row.frozen_reason = f"调用突增({len(hist)}/5min, 基线 {prev_n})"
        db.commit()
        logger.warning(
            "skill key %s 自动冻结: %s", row.key_prefix, row.frozen_reason,
        )
        # 告警: 通知中心站内(若有 admin 用户)
        try:
            from src.core.notify_center import notify_task_done

            notify_task_done(
                "skill_gateway_freeze",
                f"Skill Key {row.key_prefix} 因调用突增自动冻结: {row.frozen_reason}",
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("冻结告警发送失败: %r", e)


# ── Key 工具 ────────────────────────────────────────────────────────

def _hash_key(raw_key: str) -> str:
    # P2(audit-20260915): _KEY_SALT 已保证非空(无 env 时随机生成), 不再回落硬编码
    return hashlib.sha256(f"{_KEY_SALT}:{raw_key}".encode("utf-8")).hexdigest()


def _gen_key() -> str:
    """生成 sk_ 前缀的 AppKey(明文只在创建响应里出现一次)。"""
    return "sk_" + secrets.token_urlsafe(32)


# ── 限流(令牌桶, 2026-09-15 并发优化) ──────────────────────────────
# 旧版: 分钟 20 次硬顶, 压测 50 并发 30 个 429 误伤正常 burst。
# 新版: 令牌桶 — burst 桶容量 + 匀速补充, 日配额单独计。

_TOKEN_BUCKET: dict[str, tuple[float, float]] = {}  # key_hash -> (tokens, last_ts)
_MEM_DAY: dict[str, tuple[str, int]] = {}  # (day, count)

# 档位 burst / 匀速(次/分) — 配置化, 不硬编码在逻辑里
TIER_BURST = {"trial": 50, "free": 30, "pro": 100}
TIER_REFILL_PER_MIN = {"trial": 20, "free": 15, "pro": 60}


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


def _check_rate_limit(
    key_hash: str,
    daily_limit: int,
    *,
    tier: str = "free",
    minute_limit: int | None = None,
) -> None:
    """令牌桶限流 + 日配额。超限抛 HTTPException 429, 响应头带 Retry-After。"""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    now = time.time()
    burst = TIER_BURST.get(tier, 30)
    refill_per_min = TIER_REFILL_PER_MIN.get(tier, 15)
    # 允许调用方覆盖(测试/特殊场景)
    if minute_limit is not None:
        refill_per_min = minute_limit

    # 1) 日配额(Redis 优先)
    day_key = f"skill_quota:{key_hash[:16]}:{day}"
    rc = _redis_client()
    if rc is not None:
        try:
            d = int(rc.incr(day_key))
            if d == 1:
                rc.expire(day_key, 86400)
            if d > daily_limit:
                raise HTTPException(
                    status_code=429,
                    detail=f"日配额已用尽({daily_limit} 次/天), 请升级 Pro 或明日再试",
                    headers={"Retry-After": "3600"},
                )
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug("redis 日配额失败, 回退内存: %r", e)
            # fall through to memory
            d_n = None
        else:
            d_n = d
    else:
        d_n = None

    if d_n is None:
        # 内存日配额兜底
        d_count = _MEM_DAY.get(key_hash)
        if not d_count or d_count[0] != day:
            _MEM_DAY[key_hash] = (day, 1)
            d_n = 1
        else:
            d_n = d_count[1] + 1
            _MEM_DAY[key_hash] = (day, d_n)
        if d_n > daily_limit:
            raise HTTPException(
                status_code=429,
                detail=f"日配额已用尽({daily_limit} 次/天)",
                headers={"Retry-After": "3600"},
            )

    # 2) 令牌桶(进程内存; 多 worker 时每进程各有一份, 实际总量≈workers×burst,
    #    日配额仍是全局上限, 可接受)
    with _TB_LOCK:
        tokens, last = _TOKEN_BUCKET.get(key_hash, (float(burst), now))
        # 匀速补充
        elapsed = max(0.0, now - last)
        refill = elapsed * (refill_per_min / 60.0)
        tokens = min(float(burst), tokens + refill)
        if tokens < 1.0:
            # 需等待的秒数(约到 1 个 token)
            wait_s = max(1, int((1.0 - tokens) / (refill_per_min / 60.0)) + 1)
            raise HTTPException(
                status_code=429,
                detail=f"触发频率限制(burst {burst}, 匀速 {refill_per_min}/分), {wait_s}s 后重试",
                headers={"Retry-After": str(wait_s)},
            )
        _TOKEN_BUCKET[key_hash] = (tokens - 1.0, now)


_TB_LOCK = __import__("threading").Lock()

# ── 热点缓存(并发优化任务2, 2026-09-15) ──────────────────────────────
# quote 类读多写少, 3-5s 短 TTL; 同 symbol 并发只打一次上游。
# 多 worker 时每进程各一份 L1, 只影响命中率不影响正确性(可接受)。

_HOT_CACHE_TTL_S = 4.0
_HOT_CACHE: dict[str, tuple[float, Any]] = {}
_HOT_LOCK = __import__("threading").Lock()


def _hot_cache_get(key: str) -> Any | None:
    with _HOT_LOCK:
        ent = _HOT_CACHE.get(key)
        if not ent:
            return None
        ts, val = ent
        if time.time() - ts > _HOT_CACHE_TTL_S:
            _HOT_CACHE.pop(key, None)
            return None
        return val


def _hot_cache_set(key: str, val: Any) -> None:
    with _HOT_LOCK:
        # 简单淘汰: 超 2000 条清最旧
        if len(_HOT_CACHE) > 2000:
            now = time.time()
            for k in list(_HOT_CACHE.keys())[:200]:
                ent = _HOT_CACHE.get(k)
                if ent and now - ent[0] > _HOT_CACHE_TTL_S:
                    _HOT_CACHE.pop(k, None)
        _HOT_CACHE[key] = (time.time(), val)


# ── 鉴权 ────────────────────────────────────────────────────────────

def _validate_api_key_row(x_api_key: str, db: Session) -> SkillApiKey:
    """校验 X-API-Key 并返回行; 无效抛 401, 禁用/冻结抛 403。"""
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


def _get_api_key(x_api_key: str = Header(default="", alias="X-API-Key"), db: Session = Depends(get_db)) -> SkillApiKey:
    return _validate_api_key_row(x_api_key, db)


def _header_str(request: Request, name: str) -> str:
    """安全读 header(Mock/异常一律回落空串)。"""
    try:
        v = request.headers.get(name) or ""
        return v if isinstance(v, str) else ""
    except Exception:  # noqa: BLE001
        return ""


def _optional_jwt_user(authorization: str, db: Session) -> _User | None:
    """从 Authorization: Bearer 解 JWT; 缺失/无效/禁用返回 None(降级 guest)。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    try:
        from src.core.auth_tokens import decode_token as _decode

        payload = _decode(token)
        if not payload:
            return None
        user = db.query(_User).filter(_User.id == payload.get("sub", "")).first()
        if not user or not user.is_active:
            return None
        if user.token_version != int(payload.get("ver", 0)):
            return None
        return user
    except Exception as e:  # noqa: BLE001
        logger.debug("skill gateway JWT 解析失败, 降级 guest: %r", e)
        return None


@dataclass
class _CallIdentity:
    """run_skill 三通道鉴权结果(统一身份 2026-09-16)。

    channel: api | web | guest
    api_key_id: 游客 / 未关联 key 的 web 为 0
    rate_key: 限流标识 —— API/web 关联 key 共用 key_hash 实现同用户共享配额
    """

    channel: str
    api_key_id: int
    user_id: str | None
    rate_key: str
    tier: str
    daily_limit: int
    key_row: SkillApiKey | None


def _best_active_key_for_user(db: Session, user_id: str) -> SkillApiKey | None:
    """该用户名下最优 active key(档位优先, 同档看额度)。用于 web/API 共享配额。"""
    keys = (
        db.query(SkillApiKey)
        .filter(
            SkillApiKey.user_id == user_id,
            SkillApiKey.status == "active",
        )
        .all()
    )
    best: SkillApiKey | None = None
    for k in keys:
        k_rank = TIER_RANK.get(k.tier or "free", 0)
        b_rank = TIER_RANK.get(best.tier or "free", 0) if best else -1
        if best is None or k_rank > b_rank:
            best = k
        elif k_rank == b_rank and best is not None and (k.daily_limit or 0) > (best.daily_limit or 0):
            best = k
    return best


def _web_call_identity(user: _User, db: Session) -> _CallIdentity:
    """JWT 登录用户: channel=web; 有关联 key 则共享其配额, 否则 free 档按 user 限流。"""
    uid = str(user.id)
    best = _best_active_key_for_user(db, uid)
    if best is not None:
        # trial 到期降 free(与 _validate_api_key_row 同口径)
        if best.tier == "trial" and best.expires_at:
            exp = best.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                best.tier = "free"
                best.daily_limit = TIER_DAILY_LIMIT["free"]
                db.commit()
        return _CallIdentity(
            channel="web",
            api_key_id=best.id,
            user_id=uid,
            rate_key=best.key_hash,  # 与该 key 的 API 调用共享配额
            tier=best.tier,
            daily_limit=best.daily_limit,
            key_row=best,
        )
    return _CallIdentity(
        channel="web",
        api_key_id=0,
        user_id=uid,
        rate_key=f"web_user:{uid}",
        tier="free",
        daily_limit=TIER_DAILY_LIMIT["free"],
        key_row=None,
    )


def _resolve_call_identity(
    x_api_key: str,
    authorization: str,
    request: Request,
    db: Session,
) -> _CallIdentity:
    """三通道: API Key 优先 → JWT → 游客。游客在此完成 IP 限流。"""
    # 1) API Key(原路径不破坏)
    if x_api_key and x_api_key.startswith("sk_"):
        row = _validate_api_key_row(x_api_key, db)
        return _CallIdentity(
            channel="api",
            api_key_id=row.id,
            user_id=(str(row.user_id) if row.user_id else None),
            rate_key=row.key_hash,
            tier=row.tier,
            daily_limit=row.daily_limit,
            key_row=row,
        )
    # 2) JWT 登录(web)
    user = _optional_jwt_user(authorization, db)
    if user is not None:
        return _web_call_identity(user, db)
    # 3) 游客: IP 限流 + 仅 free skill
    ip = request.client.host if request.client else "unknown"
    _check_guest_rate(ip)
    return _CallIdentity(
        channel="guest",
        api_key_id=0,
        user_id=None,
        rate_key=f"guest:{ip}",
        tier="free",
        daily_limit=GUEST_DAILY_LIMIT,
        key_row=None,
    )


def _check_skill_tier(name: str, row: SkillApiKey) -> None:
    meta = OPEN_SKILLS.get(name)
    if not meta:
        if name in BLOCKED_SKILLS:
            raise HTTPException(403, f"该 skill 不对外开放: {name}")
        raise HTTPException(404, f"未知 skill: {name}")
    need = meta["tier_min"]
    if TIER_RANK[row.tier] < TIER_RANK[need]:
        raise HTTPException(403, f"skill {name} 需要 {need} 档位(当前 {row.tier})")


def _check_call_tier(name: str, ident: _CallIdentity) -> None:
    """按调用身份校验 skill 档位; 游客只放行 tier_min=free。"""
    meta = OPEN_SKILLS.get(name)
    if not meta:
        if name in BLOCKED_SKILLS:
            raise HTTPException(403, f"该 skill 不对外开放: {name}")
        raise HTTPException(404, f"未知 skill: {name}")
    if ident.channel == "guest":
        if meta["tier_min"] != "free":
            raise HTTPException(403, f"游客仅可调用免费 skill, 请注册获取 API Key: {name}")
        return
    need = meta["tier_min"]
    if TIER_RANK.get(ident.tier, 0) < TIER_RANK[need]:
        raise HTTPException(403, f"skill {name} 需要 {need} 档位(当前 {ident.tier})")


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
    request: Request,
    db: Session = Depends(get_db),
) -> SkillRunResponse:
    """执行开放 skill。三通道: API Key / JWT(web) / 游客。

    只返回 handler 输出字符串 + 口径 + 风险提示。
    """
    x_api_key = _header_str(request, "X-API-Key")
    authorization = _header_str(request, "Authorization")
    ident = _resolve_call_identity(x_api_key, authorization, request, db)

    _check_call_tier(name, ident)
    # 游客 IP 限流已在 _resolve_call_identity 完成; api/web 走令牌桶+日配额
    if ident.channel != "guest":
        _check_rate_limit(ident.rate_key, ident.daily_limit, tier=ident.tier)

    tool = CHAT_TOOL_REGISTRY.get(name)
    if tool is None:
        raise HTTPException(404, f"未知 skill: {name}")

    # 热点缓存(并发优化任务2): quote/技术类短 TTL, 同 symbol 并发只打一次上游
    cache_key = f"skill_cache:{name}:{hash(frozenset((body.args or {}).items()))}"
    cached = _hot_cache_get(cache_key)
    if cached is not None:
        _log_usage(
            db, ident.api_key_id, name, 200, 0,
            channel=ident.channel, user_id=ident.user_id,
        )
        if ident.key_row is not None:
            ident.key_row.last_used_at = datetime.now(timezone.utc)
            db.commit()
        return cached

    t0 = time.monotonic()
    status_code = 200
    try:
        # 开放 skill 均为行情/市场数据类, 不传 user(个人数据类已被 BLOCKED)
        result = await tool.handler(db, body.args or {}, None)
        if not isinstance(result, str):
            result = str(result)
        # Phase 3.1: 投顾红线词扫描
        result, red_hits = _compliance_wrap(result)
        duration_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        status_code = 500
        duration_ms = int((time.monotonic() - t0) * 1000)
        _log_usage(
            db, ident.api_key_id, name, status_code, duration_ms,
            channel=ident.channel, user_id=ident.user_id,
        )
        raise HTTPException(500, f"skill 执行失败: {e!r}")

    _log_usage(
        db, ident.api_key_id, name, status_code, duration_ms,
        channel=ident.channel, user_id=ident.user_id,
    )
    if ident.key_row is not None:
        ident.key_row.last_used_at = datetime.now(timezone.utc)
        db.commit()
        # Phase 2.3: 突增检测(仅 API Key 路径)
        _detect_spike_and_freeze(db, ident.key_row)

    risk = RISK_DISCLAIMER
    if red_hits:
        risk = f"{RISK_DISCLAIMER} | 合规: 命中红线词 {red_hits}"

    resp = SkillRunResponse(
        skill=name,
        result=result,
        caliber=tool.caliber,
        risk=risk,
        duration_ms=duration_ms,
    )
    # 热点缓存: 3-5s TTL, key 含 skill+args
    _hot_cache_set(cache_key, resp)
    return resp


def _log_usage(
    db: Session,
    key_id: int,
    skill: str,
    code: int,
    duration_ms: int,
    *,
    channel: str = "api",
    user_id: str | None = None,
) -> None:
    try:
        db.add(SkillUsage(
            api_key_id=key_id,
            skill_name=skill,
            status_code=code,
            duration_ms=duration_ms,
            channel=channel or "api",
            user_id=user_id,
        ))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("skill_usage 落库失败: %r", e)


# ── Key 注册 / 用量 ─────────────────────────────────────────────────

@router.post("/keys")
def register_key(
    body: KeyRegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """领取 AppKey。明文 key 只在本次响应返回一次, 服务端只存 hash。

    P1(audit-20260915): 此前完全免鉴权易被刷号, 加 IP 级限流(每小时 5 次)。
    统一身份(2026-09-16): 若带 JWT, 绑定 user_id, 便于 web/API 共享配额。
    """
    ip = request.client.host if request.client else "unknown"
    _check_key_register_rate(ip)
    linked_user_id = None
    jwt_user = _optional_jwt_user(_header_str(request, "Authorization"), db)
    if jwt_user is not None:
        linked_user_id = str(jwt_user.id)
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
        user_id=linked_user_id,
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


def _usage_by_channel(db: Session, base_filters: list, day_start: datetime) -> dict[str, int]:
    """当日用量按 channel 聚合(旧数据 channel 可能为空, 归入 api)。"""
    rows = (
        db.query(SkillUsage.channel, SkillUsage.id)
        .filter(*base_filters, SkillUsage.created_at >= day_start)
        .all()
    )
    out: dict[str, int] = {}
    for ch, _rid in rows:
        key = (ch or "api").strip() or "api"
        out[key] = out.get(key, 0) + 1
    return out


@router.get("/usage")
def get_usage(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """自查当日用量与剩余配额。

    - 带 X-API-Key: 按 key 查(兼容旧路径)
    - 仅 JWT: 按 user_id 查该用户全部 channel 用量(不分 channel 汇总)
    - 响应含 by_channel 统计
    """
    x_api_key = _header_str(request, "X-API-Key")
    authorization = _header_str(request, "Authorization")
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    if x_api_key and x_api_key.startswith("sk_"):
        row = _validate_api_key_row(x_api_key, db)
        today = db.query(SkillUsage).filter(
            SkillUsage.api_key_id == row.id,
            SkillUsage.created_at >= day_start,
        ).count()
        by_channel = _usage_by_channel(db, [SkillUsage.api_key_id == row.id], day_start)
        remaining = max(0, row.daily_limit - today)
        return {
            "tier": row.tier,
            "status": row.status,
            "daily_limit": row.daily_limit,
            "used_today": today,
            "remaining_today": remaining,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "by_channel": by_channel,
        }

    user = _optional_jwt_user(authorization, db)
    if user is None:
        raise HTTPException(
            401,
            "缺少 API Key 或登录凭证。请先登录或 POST /api/keys 领取。",
        )
    uid = str(user.id)
    # 按 user_id 查该用户全部 channel 用量(不分 channel)
    today = db.query(SkillUsage).filter(
        SkillUsage.user_id == uid,
        SkillUsage.created_at >= day_start,
    ).count()
    by_channel = _usage_by_channel(db, [SkillUsage.user_id == uid], day_start)
    best = _best_active_key_for_user(db, uid)
    daily_limit = best.daily_limit if best is not None else TIER_DAILY_LIMIT["free"]
    tier = best.tier if best is not None else "free"
    return {
        "tier": tier,
        "status": "active",
        "daily_limit": daily_limit,
        "used_today": today,
        "remaining_today": max(0, daily_limit - today),
        "expires_at": best.expires_at.isoformat() if best is not None and best.expires_at else None,
        "by_channel": by_channel,
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


# ── 后台管理(Phase 3.3): 仅 owner(JWT)可操作 ────────────────────────

from fastapi import Depends as _Dep  # noqa: E402  (避免文件头过长)
from src.web.api.auth import get_current_user as _get_current_user  # noqa: E402
from src.db.models import User as _User  # noqa: E402


async def _require_owner_admin(user: _User = _Dep(_get_current_user)) -> _User:
    if user.role != "owner":
        raise HTTPException(403, "仅 owner 可管理 Skill Key")
    return user


@router.get("/admin/skills/keys")
def admin_list_keys(
    db: Session = Depends(get_db),
    user: _User = Depends(_require_owner_admin),
) -> dict:
    """Key 列表(不含明文, 只有 prefix/hash 前 8 位)。"""
    rows = db.query(SkillApiKey).order_by(SkillApiKey.created_at.desc()).limit(200).all()
    return {
        "keys": [
            {
                "id": r.id,
                "key_prefix": r.key_prefix,
                "owner_label": r.owner_label,
                "tier": r.tier,
                "status": r.status,
                "daily_limit": r.daily_limit,
                "frozen_reason": r.frozen_reason or "",
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            }
            for r in rows
        ]
    }


class AdminKeyAction(BaseModel):
    key_id: int
    action: str = Field(..., description="freeze | unfreeze | disable | set_tier | set_limit")
    tier: str = Field(default="", description="set_tier 时: free/trial/pro")
    daily_limit: int = Field(default=0, description="set_limit 时: 新额度")
    reason: str = Field(default="", max_length=200)


@router.post("/admin/skills/keys/action")
def admin_key_action(
    body: AdminKeyAction,
    db: Session = Depends(get_db),
    user: _User = Depends(_require_owner_admin),
) -> dict:
    """冻结/解冻/禁用/调档/调额度。"""
    row = db.query(SkillApiKey).filter(SkillApiKey.id == body.key_id).first()
    if not row:
        raise HTTPException(404, "Key 不存在")
    act = body.action.strip().lower()
    if act == "freeze":
        row.status = "frozen"
        row.frozen_reason = body.reason or "人工冻结"
    elif act == "unfreeze":
        row.status = "active"
        row.frozen_reason = ""
    elif act == "disable":
        row.status = "disabled"
        row.frozen_reason = body.reason or "人工禁用"
    elif act == "set_tier":
        t = body.tier.strip().lower()
        if t not in TIER_DAILY_LIMIT:
            raise HTTPException(400, "tier 必须是 free/trial/pro")
        row.tier = t
        row.daily_limit = TIER_DAILY_LIMIT[t]
        if t == "trial" and not row.expires_at:
            row.expires_at = datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
    elif act == "set_limit":
        if body.daily_limit <= 0 or body.daily_limit > 100000:
            raise HTTPException(400, "daily_limit 需在 1..100000")
        row.daily_limit = int(body.daily_limit)
    else:
        raise HTTPException(400, f"未知 action: {body.action}")
    db.commit()
    logger.info("admin key action %s id=%s by %s", act, row.id, getattr(user, "username", "?"))
    return {
        "id": row.id,
        "status": row.status,
        "tier": row.tier,
        "daily_limit": row.daily_limit,
        "frozen_reason": row.frozen_reason or "",
    }


@router.get("/admin/skills/usage")
def admin_usage_report(
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    user: _User = Depends(_require_owner_admin),
) -> dict:
    """用量报表: 按 key 聚合近 N 天调用次数。"""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        db.query(
            SkillUsage.api_key_id,
            SkillUsage.skill_name,
            SkillUsage.status_code,
        )
        .filter(SkillUsage.created_at >= since)
        .all()
    )
    by_key: dict[int, dict] = {}
    for r in rows:
        rec = by_key.setdefault(r.api_key_id, {"total": 0, "skills": {}, "errors": 0})
        rec["total"] += 1
        rec["skills"][r.skill_name] = rec["skills"].get(r.skill_name, 0) + 1
        if r.status_code >= 400:
            rec["errors"] += 1
    keys = {k.id: k for k in db.query(SkillApiKey).filter(SkillApiKey.id.in_(list(by_key))).all()} if by_key else {}
    return {
        "days": days,
        "report": [
            {
                "key_prefix": keys[kid].key_prefix if kid in keys else str(kid),
                "tier": keys[kid].tier if kid in keys else "",
                "total": rec["total"],
                "errors": rec["errors"],
                "top_skills": sorted(rec["skills"].items(), key=lambda x: -x[1])[:5],
            }
            for kid, rec in sorted(by_key.items(), key=lambda x: -x[1]["total"])
        ],
    }
