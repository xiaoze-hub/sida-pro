"""深度健康检查 + Prometheus /metrics (2026-08-17 v0.2.65 — Phase 1)

/health 端点 (deep health):
- 返回 PG 连接池 / Redis ping / 调度器存活 / 限流状态 / 服务版本
- 子项目异常时单独标记 unhealthy,不整体 500(便于 K8s / LB 区分)
- 返回 200 但 body 标 status:"degraded" 表示有组件故障(可配)

/metrics 端点 (Prometheus):
- 请求计数 (按 path+method+status)
- 请求耗时直方图
- 自定义业务 metric (预测命中率 / AI 调用次数)
"""

import time
import asyncio
import hmac
import logging
import platform
from typing import Any

from fastapi import APIRouter, Request, Response
try:
    from prometheus_client import (
        Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    # 定义占位, /metrics 端点返回 503
    def generate_latest():
        return b""
    CONTENT_TYPE_LATEST = "text/plain"
    Counter = Histogram = Gauge = None  # type: ignore

logger = logging.getLogger(__name__)

router = APIRouter()

# ─── Prometheus metrics (lazy init — 避免 prometheus_client 未装时崩) ───
class _Metrics:
    REQUEST_COUNT: Any = None
    REQUEST_DURATION: Any = None
    IN_FLIGHT: Any = None
    AI_CALLS: Any = None
    PREDICT_REQUESTS: Any = None
    NOTIFICATIONS_SENT: Any = None
    DATASOURCE_FAILURES: Any = None
    COMPONENT_STATUS: Any = None  # P4: 组件健康(1=ok, 0=down), 供 Prometheus 告警用

_metrics = _Metrics()

def _init_metrics():
    if not _PROMETHEUS_AVAILABLE or _metrics.REQUEST_COUNT is not None:
        return
    assert Counter is not None and Histogram is not None and Gauge is not None
    _metrics.REQUEST_COUNT = Counter(
        "sida_http_requests_total",
        "Total HTTP requests",
        ["method", "path", "status"],
    )
    _metrics.REQUEST_DURATION = Histogram(
        "sida_http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["method", "path"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )
    _metrics.IN_FLIGHT = Gauge(
        "sida_http_requests_in_flight",
        "Number of HTTP requests currently in flight",
    )
    _metrics.AI_CALLS = Counter(
        "sida_ai_calls_total",
        "AI/LLM calls by model and result",
        ["model", "scene", "result"],
    )
    _metrics.PREDICT_REQUESTS = Counter(
        "sida_predict_requests_total",
        "Forecast requests by symbol",
        ["symbol"],
    )
    _metrics.NOTIFICATIONS_SENT = Counter(
        "sida_notifications_sent_total",
        "Notifications sent by channel and result",
        ["channel", "result"],
    )
    # 数据源失败计数: marketdata vendor 层失败自动上报(见本模块底部桥接), 包外自建源手工调用 record_datasource_failure
    _metrics.DATASOURCE_FAILURES = Counter(
        "sida_datasource_failures_total",
        "Datasource failures by provider and kind",
        ["provider", "kind"],
    )
    # P4 (2026-09-07): 组件健康 gauge, /health 每次检查刷新。
    # 注意: deploy/prometheus-rules.yml 里三条数据告警曾引用不存在的指标名
    # (request_count_total/datasource_failures_total/sida_health_redis_status),
    # 一条都没响过。此 gauge 名与规则文件双向锁定, 改名必须同步改规则 + 跑
    # tests/test_p4_alerts.py。
    _metrics.COMPONENT_STATUS = Gauge(
        "sida_health_component_status",
        "Component health from /health checks (1=ok, 0=down)",
        ["component"],
    )


def record_component_status(component: str, ok: bool) -> None:
    """刷新组件健康(供 /health 检查路径调用)。失败静默, 绝不影响业务。"""
    try:
        if not _PROMETHEUS_AVAILABLE:
            return
        _init_metrics()
        if _metrics.COMPONENT_STATUS is None:
            return
        _metrics.COMPONENT_STATUS.labels(component=component).set(1 if ok else 0)
    except Exception:  # noqa: BLE001
        pass


_FORECAST_PROBE_CACHE: dict = {"ts": 0.0, "detail": {"status": "unknown"}}


def _forecast_engine_probe() -> dict:
    """探测 8010 预测引擎可达性(30s 缓存 + 1.5s 超时, D5/3.6)。

    8010 停机时 /api/health components.forecast_engine=down 明确反映(验收项);
    8010 是可选加速引擎, 不翻转 overall。结果喂 record_component_status。
    """
    import time as _t

    now = _t.time()
    if now - _FORECAST_PROBE_CACHE["ts"] < 30.0:
        return _FORECAST_PROBE_CACHE["detail"]
    detail: dict
    try:
        from src.web.api.forecast import FORECAST_ENGINE_URL as url
    except Exception:
        url = "unknown"
    try:
        import httpx as _httpx

        r = _httpx.get(f"{url}/health", timeout=1.5)
        if r.status_code == 200:
            detail = {"status": "ok", "url": url}
        else:
            detail = {"status": "down", "url": url, "error": f"HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001
        detail = {"status": "down", "url": url, "error": str(e)[:100]}
    record_component_status("forecast_engine", detail["status"] == "ok")
    _FORECAST_PROBE_CACHE["ts"] = now
    _FORECAST_PROBE_CACHE["detail"] = detail
    return detail


def record_request_metrics(method: str, path: str, status: int, duration_ms: float) -> None:
    """HTTP 指标埋点(供 RequestLoggerMiddleware 调用)。

    path 归一化(避免高基数 label):
    - 数字段 / {id} (老路径)
    - 末段字母数字混合(形如 600519.SH / CN / BOARD_HS300) → {sym} (P1-10 2026-08-23 审计)
    - 截断到 80 字符防爆炸

    失败静默不影响请求。
    """
    try:
        if not _PROMETHEUS_AVAILABLE:
            return
        import re as _re

        # P1-10: 把形如 /api/quotes/600519.SH /api/board-capital-flow/CN 这种
        # 字母数字混合的叶子段也归一, 否则每个 symbol 一条时序爆 Prometheus 内存。
        # 规则: 第一段是 /api/*, 把所有"末尾叶子段(无 /)"按 [A-Za-z0-9._-] 长度 >=2 归一成 {sym}
        norm = path[:80]
        if norm.startswith("/api/"):
            # P1-10 (2026-08-23 审计): 先归一末段 (高基数嫌疑: 含 "." 形如 600519.SH,
            # 或 2..4 位全大写市场/板块代码 CN/SH/SZ/HK), 再归一中间数字段。
            # 顺序很关键: 如果先归一数字, 会把 600519 → {id} 后, "{id}.SH" 含
            # { 字符无法匹配 [A-Za-z0-9._-]+ 而漏归一 → 退化为每个 symbol 一条时序。
            parts = norm.split("/")
            if len(parts) >= 3:
                leaf = parts[-1]
                looks_like_symbol = (
                    "." in leaf and _re.fullmatch(r"[A-Za-z0-9._-]+", leaf)
                    or (2 <= len(leaf) <= 4 and leaf.isupper() and leaf.isalpha())
                )
                if looks_like_symbol:
                    parts[-1] = "{sym}"
                    norm = "/".join(parts)
            # 数字段 (路径中段或末段纯数字)
            norm = _re.sub(r"/\d+", "/{id}", norm)
        _init_metrics()
        if _metrics.REQUEST_COUNT is None:
            return
        _metrics.REQUEST_COUNT.labels(method=method, path=norm, status=str(status)).inc()
        _metrics.REQUEST_DURATION.labels(method=method, path=norm).observe(duration_ms / 1000.0)
    except Exception:  # noqa: BLE001 - 指标绝不影响业务
        pass


_DATASOURCE_KINDS = ("fetch", "parse", "timeout", "auth")


def record_datasource_failure(provider: str, kind: str = "fetch") -> None:
    """数据源失败计数(供 Prometheus SidaDatasourceFailures 告警)。

    调用点: marketdata vendors/base 的 fetch 包装与 Engine 的 timeout/auth
    分支经 on_vendor_failure 桥接自动上报; 包外自建源可手工调用。
    kind 固定枚举(未知值归一为 fetch, 防 label 基数膨胀);
    禁止把 symbol 等高基数值放进 provider。

    B1.6(2026-09-10): 除 Prometheus 计数外, 同步落一条**可查明细**
    (datasource_failures 表, 内部按 (provider, kind) 60s 限流)。
    """
    try:
        if kind not in _DATASOURCE_KINDS:
            kind = "fetch"
        try:
            from src.core.datasource_failures import record as _record_failure

            _record_failure(provider, kind)
        except Exception:  # noqa: BLE001 - 明细落库绝不影响计数
            pass
        if not _PROMETHEUS_AVAILABLE:
            return
        _init_metrics()
        _metrics.DATASOURCE_FAILURES.labels(provider=provider, kind=kind).inc()
    except Exception:  # noqa: BLE001
        pass


def _on_vendor_failure(provider: str, kind: str = "fetch") -> None:
    """marketdata vendor 层失败 → 本模块计数的桥。"""
    record_datasource_failure(provider, kind=kind)


try:
    from marketdata.vendors.base import on_vendor_failure as _md_on_vendor_failure

    _md_on_vendor_failure(_on_vendor_failure)
except Exception:  # noqa: BLE001 - 桥接失败不影响业务
    pass


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus 抓取端点。

    2026-09-08 T9: 该端点泄露路径/状态码/AI 调用/数据源故障等运营细节,
    收紧为仅容器内网/回环来源或持服务 token 者可读; 外部匿名请求 403。
    (Prometheus 与本服务同 compose 网络, 抓取来源为内网 IP, 无需改抓取配置。)
    """
    if not _PROMETHEUS_AVAILABLE:
        return Response(content=b"# prometheus_client not installed\n", media_type="text/plain", status_code=503)

    if not _request_from_internal(request):
        # 服务 token 兜底(与 auth.py 的 SIDA_SERVICE_TOKEN 双轨同源)
        supplied = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        try:
            from src.web.api.auth import get_service_token

            if not (supplied and hmac.compare_digest(supplied, get_service_token())):
                return Response(content=b"forbidden", media_type="text/plain", status_code=403)
        except Exception:
            return Response(content=b"forbidden", media_type="text/plain", status_code=403)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _request_from_internal(req: Request) -> bool:
    """来源是回环/私网(容器网络)即视为内部抓取方。"""
    import ipaddress as _ip

    client = getattr(getattr(req, "client", None), "host", "") or ""
    if not client:
        return False
    try:
        addr = _ip.ip_address(client)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


@router.get("/health")
async def health() -> dict[str, Any]:
    """深度健康检查 — 返回各组件状态

    返回结构:
    {
      "status": "ok" | "degraded" | "down",
      "version": "v0.2.65",
      "uptime_seconds": 1234,
      "components": {
        "database": {"status": "ok", "dialect": "postgresql", "pool_size": 5},
        "redis": {"status": "ok", "url": "redis://..."},
        "scheduler": {"status": "ok", "schedulers": ["agent", "price_alert", "paper_trading"]},
        "rate_limit": {"enabled": true, "buckets": 12},
      },
      "service": {"name": "SIDA", "python": "3.11.4", "platform": "linux"}
    }
    """

    HEALTH_TIMEOUT = 5.0  # 防 thsdk 等外部故障拖垮 health 端点(Docker healthcheck 10s)

    async def _check() -> dict[str, Any]:
        components = {}
        overall_ok = True

        # ─── DB 检查 ───
        try:
            from src.web.database import engine
            from sqlalchemy import text
            start = time.perf_counter()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            latency_ms = int((time.perf_counter() - start) * 1000)
            components["database"] = {
                "status": "ok",
                "latency_ms": latency_ms,
                # 0.4② (2026-09-08): 方言标签, 一眼看出连的是哪个库(sqlite/postgresql)
                "dialect": engine.url.get_backend_name(),
                "url": str(engine.url).split("@")[-1] if "@" in str(engine.url) else "sqlite",
            }
            record_component_status("database", True)  # P4: 喂 Prometheus 告警
        except Exception as e:
            try:
                from src.db.dialect import declared_backend

                _declared = declared_backend()
            except Exception:
                _declared = "unknown"
            components["database"] = {
                "status": "down",
                "dialect": _declared,
                "error": str(e)[:100],
            }
            record_component_status("database", False)  # P4: 喂 Prometheus 告警
            overall_ok = False

        # ─── Redis 检查 ───
        try:
            from src.web.cache.redis_client import redis_client
            if not redis_client._enabled:
                components["redis"] = {"status": "disabled"}
                record_component_status("redis", True)  # P4: disabled 是预期降级, 不告警
            elif await redis_client.ping():
                from src.web.cache.redis_client import REDIS_URL
                from src.web.cache.biz_cache import _mask_url
                # 2026-09-08 T9: /health 未鉴权, 只回显 host 段, 不回完整连接串(可能带密码)
                components["redis"] = {"status": "ok", "url": _mask_url(REDIS_URL)}
                record_component_status("redis", True)  # P4
            else:
                components["redis"] = {"status": "down", "url": "n/a"}
                record_component_status("redis", False)  # P4
                # Redis 降级 OK — 不影响 overall
        except Exception as e:
            components["redis"] = {"status": "down", "error": str(e)[:100]}
            record_component_status("redis", False)  # P4

        # ─── 业务缓存层(biz_cache: L1 内存 + L2 Redis)检查 ───
        try:
            from src.web.cache.biz_cache import biz_cache
            components["biz_cache"] = biz_cache.stats()
        except Exception as e:
            components["biz_cache"] = {"status": "down", "error": str(e)[:100]}

        # ─── 调度器存活检查 ───
        try:
            import server as srv_mod
            schedulers = []
            schedulers_status = {"running": 0, "shutdown": 0}
            for attr_name in ("scheduler", "price_alert_scheduler", "paper_trading_scheduler",
                              "context_maintenance_scheduler", "kline_backfill_scheduler"):
                sched = getattr(srv_mod, attr_name, None)
                if sched is not None:
                    name = attr_name.replace("_scheduler", "")
                    # 存活判定优先用内部 APScheduler 实例的 .running(真·运行状态)。
                    # 注意: 封装类的 _running 是 job 重入锁(扫描开始置 True、结束置 False,
                    # 平时恒为 False),不能作为调度器存活依据。
                    inner = getattr(sched, "scheduler", None)
                    if inner is not None and hasattr(inner, "running"):
                        running = bool(inner.running)
                    elif hasattr(sched, "running"):
                        running = bool(sched.running)
                    elif hasattr(sched, "_running"):
                        running = bool(sched._running)
                    else:
                        running = False
                    schedulers.append(name)
                    if running:
                        schedulers_status["running"] += 1
                    else:
                        schedulers_status["shutdown"] += 1
            components["scheduler"] = {
                "status": "ok" if schedulers_status["running"] >= 2 else "degraded",
                "schedulers": schedulers,
                **schedulers_status,
            }
            if schedulers_status["running"] >= 2:
                record_component_status("scheduler_leader", True)  # A3
            if schedulers_status["running"] < 2:
                # 2026-08-23 Q1; 2026-09-08 A3: leader_state() 三态区分
                # "合法让位(standby)"与"fail-closed 没敢当(failed)"。
                from src.core.scheduler_leader import leader_state
                state = leader_state()
                if schedulers_status["running"] == 0 and state == "standby":
                    components["scheduler"] = {
                        "status": "ok",
                        "schedulers": [],
                        "running": 0,
                        "shutdown": 0,
                        "leader_state": state,
                        "note": "non-leader worker(调度器由 leader 进程运行)",
                    }
                else:
                    overall_ok = False
                    # failed = 选主失败/Redis 不可用, 无任何实例在跑调度 → 告警。
                    # (standby 不记 0: 合法让位不该响 SidaSchedulerLeaderDown)
                    record_component_status("scheduler_leader", False)  # A3
                    components["scheduler"]["leader_state"] = state
                    components["scheduler"]["note"] = (
                        "选主失败(fail-closed: Redis 不可用, 无实例在跑调度)"
                        if state == "failed" else f"leader 调度器不足 2 个(state={state})"
                    )
        except Exception as e:
            components["scheduler"] = {"status": "down", "error": str(e)[:100]}
            overall_ok = False

        # ─── 限流状态 ───
        try:
            from src.web.middleware import get_rate_limit_stats
            components["rate_limit"] = get_rate_limit_stats()
        except Exception:
            components["rate_limit"] = {"enabled": False, "error": "not loaded"}

        # ─── 预测引擎(8010)可达性(D5/3.6): 停机时明确反映, 不翻转 overall ───
        components["forecast_engine"] = _forecast_engine_probe()

        # ─── service info ───
        import os
        try:
            with open("/app/VERSION") as f:
                version = f.read().strip()
        except Exception:
            version = os.getenv("APP_VERSION", "unknown")

        # ─── 整体状态 ───
        if overall_ok and components["redis"]["status"] in ("ok", "disabled"):
            overall = "ok"
        elif overall_ok:
            overall = "degraded"  # Redis 挂了但其他 OK
        else:
            overall = "down"

        return {
            "status": overall,
            "version": version,
            "components": components,
            "service": {
                "name": "SIDA",
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
        }
    try:
        return await asyncio.wait_for(_check(), timeout=HEALTH_TIMEOUT)
    except asyncio.TimeoutError:
        logging.getLogger(__name__).warning(
            "health check 超时(>%.1fs), 触发节流返回 down 防止 healthcheck 进程堆积", HEALTH_TIMEOUT)
        return {"status": "down", "version": "unknown",
                "components": {"timeout": True},
                "error": "health_check_timeout"}
    except Exception as e:
        logging.getLogger(__name__).exception("health check 异常: %s", e)
        return {"status": "down", "version": "unknown",
                "components": {"exception": True},
                "error": str(e)[:120]}
