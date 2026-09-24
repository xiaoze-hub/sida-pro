"""统一网关中间件 (2026-08-17 v0.2.65 — Phase 1)

提供:
- RequestLoggerMiddleware: 每个请求打日志(方法 + 路径 + 状态码 + 耗时)
- RateLimitMiddleware: 基于 IP+endpoint 的限流(Redis token bucket,降级内存)
- JWTDecodeMiddleware: 解码 JWT payload 存到 request.state(避免重复解码)
- SecurityHeadersMiddleware (tier2): CSP + 安全响应头
- CSRFProtectionMiddleware (tier2): 双提交 Cookie CSRF 防护

设计要点:
- 用 BaseHTTPMiddleware, FastAPI 0.104+ 标准
- 限流降级: Redis 不可用时用进程内 dict 仍然限流(只是不跨进程)
- 例外: /health /metrics /static 跳过限流(避免监控系统被自己限流)
"""

import hmac
import json
import logging
import secrets
import time
import os
import asyncio
from collections import defaultdict
from typing import Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# P2-3: 审计后台 task 持有集合, 防 GC 回收
_AUDIT_TASKS: set = set()


# ─── 全局开关 ───
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default

RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
# 2026-08-21 限流分级: 全局默认 60 → 300 req/min
# 原因: Dashboard 单次刷新并发 26 个 GET, 60/min 下多 tab/连续操作极易误伤(实测生产触发)
RATE_LIMIT_DEFAULT = _env_int("RATE_LIMIT_DEFAULT", 300)  # 默认 300 req / minute / IP
RATE_LIMIT_WINDOW = 60  # 60 秒滑动窗口
RATE_LIMIT_BURST = _env_int("RATE_LIMIT_BURST", 10)  # 突发容忍 10 个
# 敏感端点单独严格档(防爆破/防滥用): 登录/改密等写操作
RATE_LIMIT_SENSITIVE = _env_int("RATE_LIMIT_SENSITIVE", 20)  # 20 req / min
# P2(audit-20260915): webhook 免登录仅靠共享 secret, 单独更严限流(10/min)
RATE_LIMIT_WEBHOOK = _env_int("RATE_LIMIT_WEBHOOK", 10)  # 10 req / min
_SENSITIVE_PATHS = (
    "/api/auth/login",
    "/api/auth/change-password",
    "/api/auth/reset-password",
)
_WEBHOOK_PATHS = ("/api/webhooks/",)

# 例外路径 (跳过限流和日志)
# v0.4.9: 加 /api/quotes/ws — WebSocket 行情轮询被自家限流挡(429), 前端反复重连风暴
EXEMPT_PATHS = {
    "/health", "/metrics", "/api/health", "/api/metrics", "/favicon.ico",
    "/api/quotes/ws",
}


def _get_client_ip(request: Request) -> str:
    """获取客户端 IP。

    P1-9 (2026-08-23 审计): 不再无条件信任 X-Forwarded-For 首段(攻击者可伪造
    `X-Forwarded-For: 1.2.3.4` 绕过 IP 限流)。仅当直连 peer 是 loopback 或私网时
    才认 XFF(等价 uvicorn --forwarded-allow-ips=127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
    的语义), 直连 peer 是公网(裸跑 / 非可信反代)时直接用 peer.host。
    """
    import ipaddress
    peer = request.client.host if request.client else None
    if not peer:
        return "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
        # 直连 peer 是 loopback 或私网 → 可信反代, 取 XFF 首段
        trusted = (
            peer_ip.is_loopback
            or peer_ip.is_private
            or peer_ip.is_link_local
        )
    except ValueError:
        trusted = False
    if trusted:
        xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if xff:
            return xff
    return peer


def _is_exempt(path: str) -> bool:
    for p in EXEMPT_PATHS:
        if path == p or path.startswith(p + "/"):
            return True
    return False


# ─── 限流存储(降级内存) ───
class _InMemoryBucket:
    """进程内 token bucket(降级 — Redis 不可用时使用)"""

    def __init__(self):
        # key: (ip, endpoint) → [token_count, last_refill_ts]
        self._buckets: dict = defaultdict(lambda: [RATE_LIMIT_DEFAULT, time.time()])
        self._lock = None  # 进程内单线程够用(uvicorn 单 worker)

    def allow(self, key: Tuple[str, str], limit: int = RATE_LIMIT_DEFAULT) -> bool:
        now = time.time()
        tokens, last = self._buckets[key]
        # 补充 token: 每秒补 limit/60 个
        elapsed = now - last
        refill = elapsed * (limit / RATE_LIMIT_WINDOW)
        tokens = min(limit, tokens + refill)
        if tokens >= 1:
            tokens -= 1
            self._buckets[key] = [tokens, now]
            return True
        self._buckets[key] = [tokens, now]
        return False

    def stats(self) -> dict:
        return {"buckets": len(self._buckets), "limit_default": RATE_LIMIT_DEFAULT}


_in_memory_bucket = _InMemoryBucket()


# ─── JWT 解码中间件 ───
class JWTDecodeMiddleware(BaseHTTPMiddleware):
    """解码 JWT payload, 把 user_id/role/username 放到 request.state.user

    不强制鉴权 — 鉴权由各路由的 Depends(get_current_user) 决定
    这里是性能优化: 让依赖能直接读 request.state.user 避免重复解码

    2026-09-18: token 来源裁决统一走 `auth.token_from_request`
    (**Authorization Bearer 优先, 无 header 时回退 Cookie**), 本中间件不再自己写一份优先级。
    """
    async def dispatch(self, request: Request, call_next):
        request.state.user = None
        try:
            from src.web.api.auth import (
                decode_token as _decode_token,
                principal_from_payload,
                token_from_request,
            )
            raw = token_from_request(request) or ""
            if raw:
                payload = _decode_token(raw)
                if payload:
                    request.state.user = principal_from_payload(payload)
        except Exception:
            pass  # 鉴权失败路由自己返回 401
        return await call_next(request)


# ─── 限流中间件 ───
class RateLimitMiddleware(BaseHTTPMiddleware):
    """基于 (IP, endpoint) 的 token bucket 限流

    例外: /health /metrics /静态资源
    Auth: 已登录用户按 user_id 限流(更宽松), 匿名按 IP
    Redis: 优先用 Redis incr+expire, 降级到内存
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if not RATE_LIMIT_ENABLED:
            return await call_next(request)

        path = request.url.path
        if _is_exempt(path):
            return await call_next(request)

        # 2026-08-21 分级限流: 敏感端点(login等)> 写操作 > GET
        # P2(audit-20260915): /api/webhooks/* 免登录, 独立更严限流(10/min)
        method = request.method
        if path.startswith(_WEBHOOK_PATHS):
            limit = RATE_LIMIT_WEBHOOK  # 10/min — 无登录态, 防 secret 爆破/滥用
        elif path.startswith(_SENSITIVE_PATHS):
            limit = RATE_LIMIT_SENSITIVE  # 登录/改密防爆破: 20/min
        elif method == "GET":
            limit = _env_int("RATE_LIMIT_GET", RATE_LIMIT_DEFAULT)  # 300 / min
        else:
            limit = _env_int("RATE_LIMIT_WRITE", max(RATE_LIMIT_DEFAULT // 2, 60))  # 150 / min

        # Key: 已登录按 user_id, 否则 IP
        user = getattr(request.state, "user", None)
        if user and user.get("user_id"):
            bucket_key = f"u:{user['user_id']}"
        else:
            ip = _get_client_ip(request)
            bucket_key = f"i:{ip}"

        endpoint = f"{method}:{path}"

        # Redis 优先, 降级内存
        from src.web.cache.redis_client import redis_client
        allowed = True
        if redis_client.enabled:
            redis_key = f"rl:{bucket_key}:{endpoint}"
            # 用滑动窗口近似: incr + 60s expire
            count = await redis_client.incr(redis_key, ttl_seconds=RATE_LIMIT_WINDOW)
            if count is not None and count > limit:
                allowed = False
        else:
            allowed = _in_memory_bucket.allow((bucket_key, endpoint), limit=limit)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁, 请稍后再试", "limit": limit, "window": RATE_LIMIT_WINDOW},
                headers={
                    "Retry-After": str(RATE_LIMIT_WINDOW),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)

        # 响应头加 X-RateLimit
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Window"] = str(RATE_LIMIT_WINDOW)
        return response


# ─── 请求日志中间件 ───
class RequestLoggerMiddleware(BaseHTTPMiddleware):
    """结构化请求日志 — 每条请求一行 JSON, 方便 Loki/Grafana 聚合

    包含字段: method, path, status, duration_ms, client_ip, user_agent, user_id
    """

    def __init__(self, app, sample_rate: float = 1.0):
        super().__init__(app)
        self._sample_rate = sample_rate

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        status: int = 0  # 默认值, finally 块用到
        # APM(P1 2026-09-18): 请求级 trace_id, 贯穿日志/APM/Loki
        trace_token = None
        try:
            from src.core.apm import new_trace_id, set_trace_id, clear_trace_id
            from src.core.log_context import bind_log_context

            incoming = request.headers.get("x-trace-id") or request.headers.get("x-request-id")
            tid = set_trace_id(incoming)
            bind_log_context(trace_id=tid)
            trace_token = tid
            request.state.trace_id = tid
        except Exception:
            pass
        try:
            response = await call_next(request)
            status = response.status_code
            if trace_token:
                try:
                    response.headers["X-Trace-Id"] = trace_token
                except Exception:
                    pass
            return response
        except Exception as e:
            status = 500
            logger.exception(f"请求异常 {request.method} {request.url.path}: {e}")
            raise
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            user = getattr(request.state, "user", None)
            user_id = user.get("user_id") if user else None
            # Prometheus 指标埋点(2026-08-21): 延迟/计数 → /api/metrics
            try:
                from src.web.api.health import record_request_metrics

                record_request_metrics(request.method, request.url.path, status, duration_ms)
            except Exception:
                pass
            # 告警(2026-09-18 tier1): 5xx 滑动窗口计数, 超阈值发 api_error_5xx
            # fail-soft: 告警失败绝不影响请求
            if status >= 500:
                try:
                    from src.core.alerting import record_http_status

                    record_http_status(status, request.url.path)
                except Exception:
                    pass
            # 结构化日志(INFO 级 — 让运维聚合)
            try:
                logger.info(json.dumps({
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": duration_ms,
                    "client_ip": _get_client_ip(request),
                    "user_id": user_id,
                    "trace_id": trace_token or "",
                    "ua": (request.headers.get("user-agent", "")[:60]),
                }, ensure_ascii=False))
            except Exception:
                pass
            # 清理 trace 上下文(避免 contextvar 泄漏到线程池)
            try:
                if trace_token:
                    from src.core.apm import clear_trace_id
                    clear_trace_id()
            except Exception:
                pass


# ─── 健康快照(供 /health 端点读取) ───
def get_rate_limit_stats() -> dict:
    """给 /health 端点"""
    return {
        "enabled": RATE_LIMIT_ENABLED,
        "default_limit": RATE_LIMIT_DEFAULT,
        "window_seconds": RATE_LIMIT_WINDOW,
        "in_memory": _in_memory_bucket.stats(),
    }


# ─── 操作审计中间件(2026-08-18 补齐) ───
# 所有 2xx 写操作(POST/PUT/PATCH/DELETE)自动落 audit_logs, 一次覆盖全部管理接口
# (渠道/服务商/数据源/设置/用户/自选/预警等), 不用每处业务代码手动调 log_audit。
# 例外(避免重复/噪音):
#   - auth 登录/注册/改密: auth.py 已自带埋点
#   - 匿名请求(无 user): 不记
#   - 非 2xx: 失败不记(只记成功操作)
#   - 静态/health/webhook: 跳过
_SKIP_AUDIT_PREFIXES = (
    "/api/auth/", "/static", "/assets", "/health", "/api/health",
    "/api/webhooks/", "/api/tradingview", "/favicon.ico", "/api/metrics",
)
_SKIP_AUDIT_PATHS = {"/", "/api/auth"}


class AuditMiddleware(BaseHTTPMiddleware):
    """写操作自动审计。内部自行 decode JWT(不依赖中间件顺序)。"""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        try:
            method = request.method
            path = request.url.path
            if method not in ("POST", "PUT", "PATCH", "DELETE"):
                return response
            if response.status_code < 200 or response.status_code >= 300:
                return response
            if path in _SKIP_AUDIT_PATHS or path.startswith(_SKIP_AUDIT_PREFIXES):
                return response

            # 自己解析 JWT(避免依赖 JWTDecodeMiddleware 的外层/内层顺序)
            # 2026-09-18: 与 HTTP 依赖同源裁决(Bearer 优先 → Cookie), 保证审计归属的用户
            # 就是请求真正以之执行的那个用户。
            user = None
            try:
                from src.web.api.auth import (
                    decode_token as _decode_token,
                    principal_from_payload,
                    token_from_request,
                )
                raw = token_from_request(request) or ""
                if raw:
                    payload = _decode_token(raw)
                    if payload:
                        user = principal_from_payload(payload)
            except Exception:
                pass
            if not user or not user.get("user_id"):
                return response

            # 异步落库: 独立 session, 失败静默(不阻塞业务)
            import asyncio
            from src.web.database import SessionLocal
            from src.web.models import AuditLog

            # action: 如 "POST /api/channels" → "POST /channels"; 保留可读性
            parts = path.split("/")
            resource = parts[2] if len(parts) > 2 and parts[2] else path
            action = f"{method} /{resource}"

            # P2-3 (2026-09-05 28号审计): DB 写放线程池 + task 持有防 GC
            async def _write():
                try:
                    await asyncio.to_thread(_write_sync)
                except Exception:
                    pass

            def _write_sync():
                try:
                    db = SessionLocal()
                    try:
                        db.add(AuditLog(
                            user_id=user.get("user_id"),
                            username=user.get("username") or "",
                            action=action,
                            detail=f"{method} {path}",
                            ip=_get_client_ip(request),
                        ))
                        db.commit()
                    finally:
                        db.close()
                except Exception as e:
                    logger.warning(f"审计写入失败: {e}")

            _task = asyncio.create_task(_write())
            _AUDIT_TASKS.add(_task)
            _task.add_done_callback(_AUDIT_TASKS.discard)
        except Exception:
            pass  # 审计失败绝不影响主请求
        return response


# ─── 安全响应头 + CSP (tier2-stability 2026-09-18) ───
# CSP 兼容 Vite/React: script/style 允许 unsafe-inline/eval(dev HMR + 生产 inline runtime);
# connect-src 'self' 覆盖同源 API + WS(Vite HMR 在 dev 经同源代理)。
# object/frame-ancestors/base-uri/form-action 收紧到最小面。
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# P0(2026-09-18): HSTS — 仅 HTTPS 时下发(见 _request_is_https)
HSTS_MAX_AGE = _env_int("HSTS_MAX_AGE", 31536000)  # 1 年
HSTS_VALUE = f"max-age={HSTS_MAX_AGE}; includeSubDomains"


def _request_is_https(request: Request) -> bool:
    """HTTPS 判定: 反代场景优先 X-Forwarded-Proto, 直连看 request.url.scheme。

    注意: 仅当反代正确回写 X-Forwarded-Proto 时 HSTS 才会下发 —
    错误配置下不会误标 HTTP 为 HTTPS(宁缺勿错, 避免本地 http 被 HSTS 锁死)。
    """
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    try:
        return (request.url.scheme or "").lower() == "https"
    except Exception:
        return False


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """给所有响应挂 CSP + 常见安全头。外层最外(最后 add), 保证 4xx/5xx 也带头。

    P0(2026-09-18): HTTPS 请求额外挂 Strict-Transport-Security。
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        # setdefault 不覆盖上游(如 CORS 自带)已有值? 安全头一律由本中间件定口径, 直接覆盖
        for key, value in SECURITY_HEADERS.items():
            response.headers[key] = value
        response.headers["Content-Security-Policy"] = CSP_POLICY
        if _request_is_https(request):
            response.headers["Strict-Transport-Security"] = HSTS_VALUE
        return response


# ─── CSRF 双提交 Cookie 防护 (tier2-stability 2026-09-18) ───
# 模式: 登录/注册时下发 HttpOnly csrf_token Cookie; 客户端写请求需带
# X-CSRF-Token 头, 与 Cookie 值一致才放行。
# 与现有 JWT 兼容: Authorization Bearer 不会被浏览器自动附带, 天然免疫 CSRF,
# 故带 Bearer 的请求跳过校验(现前端全量走 Bearer, 零破坏)。
# Cookie 鉴权路径(或将来纯 Cookie 会话)才强制双提交匹配。
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 登录前无 token; webhook 服务端调用无浏览器 Cookie
# logout: 清 Cookie 的幂等端点, 无 Bearer 时也须放行(Cookie-only 客户端)
CSRF_EXEMPT_PREFIXES = (
    "/api/auth/login",          # 含 /api/auth/login-by-email(前缀命中)
    "/api/auth/register",
    "/api/auth/logout",
    # 2026-09-19 修: 邮箱验证码是**登录前**就要用的端点(注册/验证码登录第一步),
    # 此时浏览器**还没有** csrf_token cookie → 必被拦成 403「CSRF token 缺失」,
    # 表现就是"点了发送验证码没反应/报错", 邮箱注册整条流程走不通。
    # 安全性: 该端点自带发送冷却(check_send_cooldown)+ 全局限流, 与 login 同级对待。
    "/api/auth/send-code",
    "/api/webhooks/",
    # 2026-09-24 修: POST /api/keys 是「领 AppKey」的注册端点, 与 /api/auth/register 同理,
    # 是**无任何凭证前**就要调用的第一个端点(此时既无 JWT 也无 sk_ key 也无 csrf cookie)。
    # 若不放行 ⇒ 新用户永远领不到 key ⇒ 整个对外 Skills API 注册通道死循环不可用。
    # 防滥用保障: register_key 自带 IP 级限流(_check_key_register_rate, 每小时 5 次) +
    #           盐校验(_require_stable_salt), 与 login/register 同级, 无需担心裸放行被刷号。
    "/api/keys",
)


def issue_csrf_token(response: Response, max_age_seconds: int = 12 * 3600) -> str:
    """生成 32 字节 hex CSRF token, 写入 HttpOnly Cookie, 返回明文(供响应体下发)。

    SameSite=Strict + Path=/; 不设 Secure(本地 http 开发可用, 生产反代可再加)。
    响应体同时返回, 便于前端把值放进 X-CSRF-Token(HttpOnly 时 JS 读不到 Cookie)。
    """
    token = secrets.token_hex(32)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        max_age=max_age_seconds,
        path="/",
        httponly=True,
        samesite="strict",
    )
    return token


class CSRFProtectionMiddleware(BaseHTTPMiddleware):
    """写操作 CSRF 校验: Cookie csrf_token == Header X-CSRF-Token。

    跳过: 安全方法 / 豁免路径 / Bearer JWT / X-Service-Token。
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        method = request.method.upper()
        if method in CSRF_SAFE_METHODS:
            return await call_next(request)

        path = request.url.path
        if path.startswith(CSRF_EXEMPT_PREFIXES):
            return await call_next(request)

        # JWT Bearer: 浏览器跨站不会自动带 Authorization 头 → 免 CSRF(与现前端兼容)
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return await call_next(request)

        # API Key 调用(2026-09-20 修): 携带 sk_... 的机器请求没有浏览器 cookie 上下文 ——
        # CSRF 的前提是"浏览器会自动带上 cookie 凭证", 机器调用不存在这个前提, 与 Bearer 同理豁免。
        # 此前 POST /api/skills/{name}/run 带 X-API-Key 被拦成 403「CSRF token 缺失」⇒
        # Skills API 的**核心调用面**(跑 skill)对外完全不可用, 而 GET 列表却正常(所以容易被忽略)。
        if (request.headers.get("x-api-key") or "").strip().startswith("sk_"):
            return await call_next(request)

        # 服务间调用: X-Service-Token, 无浏览器上下文
        if (request.headers.get("x-service-token") or "").strip():
            return await call_next(request)

        # 游客匿名调用(2026-09-24 修): POST /api/skills/{name}/run 是设计为"无凭证可调"的端点,
        # 下游 skills_gateway._resolve_call_identity 会对无 key 无 JWT 的请求走游客分支,
        # 自带 IP 级限流(_check_guest_rate, 每 IP 24h 10 次) + 只放行 free 档 + OPEN_SKILLS 白名单。
        # 若此处不放行, 游客会在 CSRF 层就被 403 拦死, 永远走不到下游限流(游客试用通道形同虚设)。
        # 仅放行 run 端点(有游客限流兜底); 其它 /api/skills 写操作(如 key 管理/admin)仍需凭证。
        if path.startswith("/api/skills/") and path.endswith("/run"):
            return await call_next(request)

        cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "") or ""
        header_token = request.headers.get(CSRF_HEADER_NAME, "") or ""
        if not cookie_token or not header_token:
            return JSONResponse(
                status_code=403,
                content={
                    "code": 403,
                    "success": False,
                    "message": "CSRF token 缺失, 请重新登录",
                },
            )
        if not hmac.compare_digest(cookie_token, header_token):
            return JSONResponse(
                status_code=403,
                content={
                    "code": 403,
                    "success": False,
                    "message": "CSRF token 校验失败",
                },
            )
        return await call_next(request)