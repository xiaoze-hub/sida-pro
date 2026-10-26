"""demo 账号限流 — 防止公开演示账号烧共享模型 API key。

策略: 每日 AI 对话次数上限(默认 10 次)。内存计数 + 线程锁,
单进程足够(FastAPI 单 worker); 重启清零可接受(demo 场景,
恶意者无法触发服务重启)。仅作用于 username=demo 的账号,
其他用户(admin/成员)不受影响。

2026-08-15 创建: README 公开生产 demo 域名后, 任何访客可用 demo
账号登录, 若不限流会直接消耗全局 ai_services 配置的模型 key。
"""

import threading
import time
from datetime import date

_DEMO_DAILY_LIMIT = 10
# {user_id: (date_str, count)}
_counter: dict[str, tuple[str, int]] = {}
_lock = threading.Lock()


def allow(user_id: str) -> bool:
    """返回 True=允许本次调用(计数+1); False=当日额度已用完。"""
    today = date.today().isoformat()
    with _lock:
        d, c = _counter.get(user_id, (today, 0))
        if d != today:
            _counter[user_id] = (today, 1)
            return True
        if c >= _DEMO_DAILY_LIMIT:
            return False
        _counter[user_id] = (today, c + 1)
        return True


def remaining(user_id: str) -> int:
    """当日剩余次数(用于提示)。"""
    today = date.today().isoformat()
    with _lock:
        d, c = _counter.get(user_id, (today, 0))
        if d != today:
            return _DEMO_DAILY_LIMIT
        return max(0, _DEMO_DAILY_LIMIT - c)


# ════════════════════════════════════════════════════════════════════
# demo GET 限流(2026-08-15): 每小时 20 次 API 请求 — 防爬虫用公开 demo
# 账号高频刷 GET 烧数据源配额(wudao 200次/天 / zhitu / tdx 等)。
# 固定窗口(自然小时) + 内存计数; 仅作用于 demo 账号, 其他用户零影响。
#
# B3(2026-09-29): 上限由 `free_tier.guest_strategy(db)["get_hourly_limit"]` 读
# (owner 在「免费档」面板可调, 30s 缓存热生效); 未配置时回退常量 20 —— 默认行为不变。
# ════════════════════════════════════════════════════════════════════
_DEMO_GET_HOURLY_LIMIT = 20
# {user_id: {"hour": "2026-08-15T17", "count": int}}
_get_state: dict[str, dict] = {}


def _own_db():
    """无 db 上下文时自建 session(与 skills_gateway.refresh_tier_configs 同源); 失败返回 None。"""
    try:
        from src.db.session import SessionLocal

        return SessionLocal()
    except Exception:  # noqa: BLE001 —— 单测/无 DB 环境读设置不该炸
        return None


def get_hourly_limit(db=None) -> int:
    """当前游客每小时 GET 限额(配置优先; 非法/读不到 → 20)。

    db=None: **先读 L1 缓存**(命中即返回, 不打库); 缓存冷才自建只读 session
    (与 skills_gateway.refresh_tier_configs 同源), 拿不到就回落常量 20。
    """
    own = None
    try:
        from src.core import free_tier

        if db is None:
            cached = free_tier.cached_config()
            if cached is not None:  # 缓存命中的是整份配置 → 取 guest_strategy 子项
                cfg = cached.get("guest_strategy") or {}
            else:
                own = _own_db()
                cfg = free_tier.guest_strategy(own) if own is not None else {}
        else:
            cfg = free_tier.guest_strategy(db)
        v = (cfg or {}).get("get_hourly_limit", _DEMO_GET_HOURLY_LIMIT)
        iv = int(v)
        return iv if iv > 0 else _DEMO_GET_HOURLY_LIMIT
    except Exception:  # noqa: BLE001 —— 配置层故障不该让限流入口 500
        return _DEMO_GET_HOURLY_LIMIT
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:  # noqa: BLE001
                pass


def allow_api_get(user_id: str, db=None) -> bool:
    """demo 账号每小时 API 请求配额: 返回 True=放行(计数+1); False=超限。"""
    limit = get_hourly_limit(db)
    hour = time.strftime("%Y-%m-%dT%H")
    with _lock:
        st = _get_state.get(user_id)
        if not st or st["hour"] != hour:
            _get_state[user_id] = {"hour": hour, "count": 1}
            return True
        if st["count"] >= limit:
            return False
        st["count"] += 1
        return True
