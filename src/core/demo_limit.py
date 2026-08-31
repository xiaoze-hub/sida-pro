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
# demo GET 限流(2026-08-15): 每小时 5 次 API 请求 — 防爬虫用公开 demo
# 账号高频刷 GET 烧数据源配额(wudao 200次/天 / zhitu / tdx 等)。
# 固定窗口(自然小时) + 内存计数; 仅作用于 demo 账号, 其他用户零影响。
# ════════════════════════════════════════════════════════════════════
_DEMO_GET_HOURLY_LIMIT = 20
# {user_id: {"hour": "2026-08-15T17", "count": int}}
_get_state: dict[str, dict] = {}


def allow_api_get(user_id: str) -> bool:
    """demo 账号每小时 API 请求配额: 返回 True=放行(计数+1); False=超限。"""
    hour = time.strftime("%Y-%m-%dT%H")
    with _lock:
        st = _get_state.get(user_id)
        if not st or st["hour"] != hour:
            _get_state[user_id] = {"hour": hour, "count": 1}
            return True
        if st["count"] >= _DEMO_GET_HOURLY_LIMIT:
            return False
        st["count"] += 1
        return True
