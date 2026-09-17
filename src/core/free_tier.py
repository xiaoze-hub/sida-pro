"""免费档("免费级别")运行时可调配置 —— 2026-09-18 老板需求。

**背景**: 原先把"哪些功能 member 能试用、每天几次"硬编码在 `core/permissions.py`
(`TRIAL_FEATURES` / `TRIAL_DAILY_LIMIT`), 改一次要改代码 + 发版; 且数智决策三指标、集合竞价池
这类**高价值功能被放进免费试用**。现在改成**运行时配置**:

- 存 `app_settings` 表(KV, key = `free_tier_config`), 30s 缓存 + 写后立即失效, 改完新请求热生效;
- 由 owner 通过 `GET/PUT /api/admin/free-tier` 调整, 前端「系统设置」里有配套面板;
- 默认口径(2026-09-18 老板拍板): **数智决策三指标(机构活跃度+GS+L2主力净流入 TQ口径)与
  集合竞价池一律 pro 档**, 不在免费层级; member 只保留机会/L2资金/暗盘资金 3 次/天试用。

**分层**: 本模块是纯 core(不 import FastAPI / src.web), 供 permissions、skills_gateway、
chat 工具、admin 端点共用; 落库用 `src.db.models.AppSettings`(与 settings API 同一张 KV 表)。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

#: KV 键名(app_settings.key)
CONFIG_KEY = "free_tier_config"

#: 缓存 TTL(秒) —— 与 tier_configs 的 30s 口径一致: 改完最多 30s 内全 worker 生效
_CACHE_TTL = 30.0

# ── 默认口径 ────────────────────────────────────────────────────────
#: 功能权限点 → 中文名。**默认不在免费层级**的功能(pro 专属)见 permissions.PRO_ONLY_PERMS。
DEFAULT_TRIAL_FEATURES: dict[str, str] = {
    "view_opportunities": "机会",
    "view_l2": "L2资金",
    "view_dark": "暗盘资金",
}
#: member 试用日限
DEFAULT_TRIAL_DAILY_LIMIT = 3
#: 自选/预警上限(member)
DEFAULT_MEMBER_WATCHLIST_MAX = 10
DEFAULT_MEMBER_ALERT_MAX = 3
#: 外部 skill 的档位覆盖 {skill_name: "free"|"trial"|"pro"}; 空 = 用 OPEN_SKILLS 内置档位
DEFAULT_SKILL_TIER_OVERRIDES: dict[str, str] = {}

#: 档位取值白名单(与 skills_gateway.TIER_RANK 同名)
VALID_TIERS = ("free", "trial", "pro")

_LOCK = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_at: float = 0.0


def _defaults() -> dict[str, Any]:
    return {
        "trial_features": dict(DEFAULT_TRIAL_FEATURES),
        "trial_daily_limit": DEFAULT_TRIAL_DAILY_LIMIT,
        "member_watchlist_max": DEFAULT_MEMBER_WATCHLIST_MAX,
        "member_alert_max": DEFAULT_MEMBER_ALERT_MAX,
        "skill_tier_overrides": dict(DEFAULT_SKILL_TIER_OVERRIDES),
    }


def _coerce(raw: Any) -> dict[str, Any]:
    """把任意输入(JSON 字符串 / dict / 脏数据)收拾成**完整且合法**的配置。

    原则: 缺字段补默认、非法值丢弃回默认 —— 配置读坏不能让权限判定崩(那是全站 500)。
    """
    cfg = _defaults()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("free_tier_config 不是合法 JSON, 回落默认口径")
            raw = None
    if not isinstance(raw, dict):
        return cfg

    tf = raw.get("trial_features")
    if isinstance(tf, dict):
        clean = {
            str(k): str(v)
            for k, v in tf.items()
            if isinstance(k, str) and k and isinstance(v, (str, int, float))
        }
        cfg["trial_features"] = clean
    elif tf == {}:
        cfg["trial_features"] = {}

    for key, lo, hi in (
        ("trial_daily_limit", 0, 100),
        ("member_watchlist_max", 0, 1000),
        ("member_alert_max", 0, 1000),
    ):
        v = raw.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and lo <= int(v) <= hi:
            cfg[key] = int(v)

    sto = raw.get("skill_tier_overrides")
    if isinstance(sto, dict):
        cfg["skill_tier_overrides"] = {
            str(k): str(v)
            for k, v in sto.items()
            if isinstance(k, str) and k and str(v) in VALID_TIERS
        }
    return cfg


def invalidate_cache() -> None:
    """写配置后调用 —— 本进程立即失效; 其他 worker 最多 30s(TTL)后跟上。"""
    global _cache, _cache_at
    with _LOCK:
        _cache = None
        _cache_at = 0.0


def get_config(db=None, *, force: bool = False) -> dict[str, Any]:
    """取当前免费档配置(带 30s 缓存)。

    db 为 None 时: 只读缓存; 缓存为空则用默认(不落库、不报错) —— 让权限判定在无 db
    上下文(如纯单测)也能工作, 不因为拿不到配置就把人挡在门外或放进来。
    """
    global _cache, _cache_at
    now = time.monotonic()
    with _LOCK:
        if not force and _cache is not None and (now - _cache_at) < _CACHE_TTL:
            return json.loads(json.dumps(_cache))  # 深拷贝: 调用方改不脏缓存
    if db is None:
        with _LOCK:
            cached = _cache
        return json.loads(json.dumps(cached)) if cached else _defaults()

    cfg = _defaults()
    try:
        from src.db.models import AppSettings

        row = db.query(AppSettings).filter(AppSettings.key == CONFIG_KEY).first()
        if row is not None and row.value:
            cfg = _coerce(row.value)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 free_tier_config 失败, 用默认口径: %r", e)

    with _LOCK:
        _cache = cfg
        _cache_at = time.monotonic()
    return json.loads(json.dumps(cfg))


def save_config(db, patch: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    """写入配置(整体覆盖语义: 未给的字段沿用当前值), 落库 + 立即失效缓存。"""
    from src.db.models import AppSettings

    current = get_config(db, force=True)
    merged = {**current, **(patch or {})}
    cfg = _coerce(merged)

    row = db.query(AppSettings).filter(AppSettings.key == CONFIG_KEY).first()
    payload = json.dumps(cfg, ensure_ascii=False)
    if row is None:
        db.add(AppSettings(
            key=CONFIG_KEY,
            value=payload,
            description="免费档运行时可调配置(试用功能/日限/自选上限/skill 档位覆盖)",
        ))
    else:
        row.value = payload
    db.commit()
    invalidate_cache()
    logger.info("free_tier_config 已更新 by %s: %s", actor or "?", payload)
    return cfg


# ── 访问器(给 permissions / skills_gateway / chat 工具用) ─────────────
def trial_features(db=None) -> dict[str, str]:
    """当前允许 member 试用的功能 {权限点: 中文名}。"""
    return dict(get_config(db).get("trial_features") or {})


def trial_daily_limit(db=None) -> int:
    return int(get_config(db).get("trial_daily_limit", DEFAULT_TRIAL_DAILY_LIMIT))


def member_watchlist_max(db=None) -> int:
    return int(get_config(db).get("member_watchlist_max", DEFAULT_MEMBER_WATCHLIST_MAX))


def member_alert_max(db=None) -> int:
    return int(get_config(db).get("member_alert_max", DEFAULT_MEMBER_ALERT_MAX))


def skill_tier_min(name: str, builtin: str, db=None) -> str:
    """外部 skill 的最低档位: 配置有覆盖用覆盖, 否则用 OPEN_SKILLS 内置值。"""
    override = (get_config(db).get("skill_tier_overrides") or {}).get(name)
    return override if override in VALID_TIERS else builtin
