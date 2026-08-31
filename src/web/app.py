import os

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.web.api import (
    stocks,
    agents,
    presets,
    settings,
    logs,
    providers,
    channels,
    datasources,
    subscriptions,
    accounts,
    history,
    news,
    market,
    reports,
    strategies,
    auth,
    suggestions,
    quotes,
    klines,
    templates,
    feedback,
    discovery,
    price_alerts,
    context,
    recommendations,
    dashboard,
    paper_trading,
    chat,
    forecast,
    calendar,
    market_data,
    tdx,
    shadow,
    ths,
    darkflow,
    decision_pioneer,
    stock_pool,
    boards,
    main_flow,
    auction_pool,
    abnormal_moves,
    market_phase,
    chat_upload,
    my_ai_services,
    users,
    llm_usage,
    profile,
    export as export_data,
    audit,
    market_mainline,
)
from src.web.api import factors
from src.web.api import notifications
from src.web.api import health as health_router
from src.web.api import insights
from src.web.api import wechat_bind
from src.web.api import thsdk_snapshot, thsdk_alert as thsdk_alert_router
from src.web.api import thsdk_extended as thsdk_extended_router
from src.web.api import thsdk_ext as thsdk_ext_router
from src.web.api.auth import get_current_user
from src.web.api.settings import get_app_version
from src.web.response import ResponseWrapperMiddleware

app = FastAPI(
    title="SIDA API",
    version="0.1.0",
    redirect_slashes=False,  # 避免重定向丢失 Authorization header
    # 安全: 生产关闭 API 文档(/docs /redoc /openapi.json), 防接口地图泄露(2026-08-15 公开 demo 后)
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# GZip 压缩(2026-08-10): 静态 JS 2.3MB 未压缩, 跨境弱网加载慢 → 压缩后 ~600KB
# ⚠️ 顺序关键: Starlette add_middleware 后加的在更外层(先执行)。
# 正确: ResponseWrapper 先 add(内层, 先拿到后端原始响应并包装),
#      GZip 后 add(外层, 最后压缩包装后的响应)。
# 之前顺序反了(GZip内层), wrapper 收到压缩字节流 → JSON 解析失败 → 返回裸数据(设置页"都没了")。
app.add_middleware(ResponseWrapperMiddleware)
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
# P1-8 (2026-08-23 审计): 解析 CORS 来源(给下方 CORSMiddleware 用)。
# 必须在 CORSMiddleware add 之前解析 env, 但 add 顺序按 Starlette 语义: Audit 先 add
# (最内), CORS 最后 add (最外)。所以解析放在这里, 真正 add 放到下面业务中间件组里。
_cors_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:8000").split(",") if origin.strip()]
# (CORSMiddleware 已上移到业务中间件之后 add, 见下方)
# 2026-08-17 v0.2.65 (Phase 1): 统一网关中间件
# - JWTDecodeMiddleware: 解析 JWT 放 request.state.user(给限流/日志用)
# - RateLimitMiddleware: 基于 IP/user_id 限流(Redis 优先, 内存降级)
# - RequestLoggerMiddleware: 结构化 JSON 日志(给 Loki 聚合)
# - AuditMiddleware (2026-08-18): 写操作自动审计(自己解 JWT, 不依赖中间件顺序)
#
# P1-8 (2026-08-23 审计): 修正 add_middleware 顺序, 真正匹配原注释意图。
# Starlette `add_middleware` 后加的先执行(越后越外层)。
# 目标实际执行顺序(从最外到最内): CORS → 日志 → 限流 → JWT → 审计 → 路由
# → add 顺序必须反着来: Audit(先add=最内) → JWT → 限流 → 日志 → CORS(后add=最外)
# 收益: CORS 最外 → 即使被限流/鉴权拒也会带 CORS 头; 限流先于 JWT → 匿名爆破
#      不付 JWT 解码 CPU; 日志在内, 限流外 → 能看到被限流的请求; 审计最内 →
#      拿到 request.state.user 并对 2xx 写操作落 audit_logs。
from src.web.middleware import (
    JWTDecodeMiddleware,
    RateLimitMiddleware,
    RequestLoggerMiddleware,
    AuditMiddleware,
)
app.add_middleware(AuditMiddleware)        # innermost: add first
app.add_middleware(JWTDecodeMiddleware)    # user state for downstream
app.add_middleware(RateLimitMiddleware)    # rejects before JWT decode cost on attacks
app.add_middleware(RequestLoggerMiddleware)  # sees rate-limited requests too
app.add_middleware(CORSMiddleware,         # outermost: CORS headers even on 429/401
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 轻量错误追踪(2026-08-21): 捕获未处理异常 → JSONL 落盘 + 高频聚合告警
# 尽量内层(最后 add)以贴近路由, 捕获路由/处理器抛出的未处理异常, 原样 re-raise 不吞。
from src.core.error_tracker import install_error_tracker

install_error_tracker(app)

# API 版本化别名(2026-08-21): /api/v1/* → /api/* 透明改写
# 现有前端零改动; 将来破坏性 v2 只需新增路由表 + 调整别名, 老路径平滑保留。
from src.web.api_version import ApiVersionAliasMiddleware

app.add_middleware(ApiVersionAliasMiddleware, prefix="/api/v1")


# ════════════════════════════════════════════════════════════════════
# 账号权限控制(2026-08-15 RBAC): 角色权限驱动, 替代 username==demo 硬编码
# 1) guest(demo) 隔离: 只读浏览 + 管理页 403 + GET 限流 + 自选增删例外(行为保持现状)
# 2) 管理区 RBAC: 管理区路径 → 对应 manage_* 权限点; owner 全过,
#    member 默认无 manage_* → 403(owner 可在 users.permissions 数组给 member
#    加白名单权限点, 向后兼容通道); /api/settings /api/providers 的 GET
#    允许浏览(敏感 key 已掩码)。
# 判定: JWT payload.username + payload.role → ROLE_PERMISSIONS。
# 登录/刷新在认证前(无 token), 不受影响。
# ════════════════════════════════════════════════════════════════════
_DEMO_ADMIN_PREFIXES = (
    "/api/datasources",
    "/api/settings",
    "/api/ai-services",
    "/api/agents",
    "/api/strategies",
    "/api/users",
    "/api/shadow",
    "/api/paper-trading",
    "/api/forecast/predict",
    "/api/upload",
    "/api/reports/generate",
    "/api/wechat",
)

# 管理区路径 → 所需权限点(2026-08-15 RBAC; /api/providers 原不在隔离列表,
# 现纳入管理区, GET 仍可浏览)
# 2026-08-16 调整: /api/agents GET 放行(member 个股 AI 分析页需要拉 Agent
# 列表/能力, 只读无风险); /api/reports/generate、/api/strategies 移出管理区 ——
# 前者无对应路由(死配置); 后者 v0.2.47 已把策略库并入机会页(member 可见),
# 且该前缀下 list/get/scan/apply 全部为只读或纯计算端点(无写操作,
# 策略写入在 /api/recommendations), member 机会页的策略筛选/扫描需要它们。
_ADMIN_PREFIX_PERMISSIONS = {
    "/api/datasources": "manage_datasources",
    "/api/settings": "manage_settings",
    "/api/ai-services": "manage_ai_services",
    "/api/providers": "manage_ai_services",
    "/api/agents": "manage_agents",
    "/api/users": "manage_users",
    "/api/shadow": "manage_shadow",
    "/api/paper-trading": "manage_paper_trading",
    "/api/forecast/predict": "run_prediction",
    "/api/upload": "upload_files",
}
# 管理区中允许 GET 浏览的路径(敏感 key 已掩码, 只读无风险)
_READABLE_ADMIN_PREFIXES = ("/api/settings", "/api/providers", "/api/agents")


def _resolve_user_auth(username: str) -> tuple[str | None, set[str]]:
    """查 DB 取用户 role + users.permissions 白名单权限点(失败返回 (None, 空集))。

    users.permissions 兼容两种形态:
      - list: ["manage_datasources", ...] 权限点字符串数组(预留格式, 白名单)
      - dict: {"permissions": [...], "model_access": {...}} 新版扩展格式
    """
    try:
        from src.web.database import SessionLocal
        from src.web.models import User

        db = SessionLocal()
        try:
            u = db.query(User).filter(User.username == username).first()
            if not u:
                return None, set()
            extra: set[str] = set()
            perms = u.permissions
            if isinstance(perms, list):
                extra = {p for p in perms if isinstance(p, str)}
            elif isinstance(perms, dict):
                extra = {p for p in perms.get("permissions", []) if isinstance(p, str)}
            role_val = u.role
            return (str(role_val) if role_val is not None else None), extra
        finally:
            db.close()
    except Exception:
        return None, set()


@app.middleware("http")
async def demo_isolation_middleware(request: Request, call_next):
    path = request.url.path
    method = request.method
    # 非 API 路径(静态资源)直接放行
    if not path.startswith("/api/"):
        return await call_next(request)

    username = None
    payload = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            from src.web.api.auth import decode_token
            payload = decode_token(auth[7:])
            if payload:
                username = payload.get("username")
        except Exception:
            pass

    # 未认证 / CORS 预检: 放行(各路由自行鉴权)
    if not username or method == "OPTIONS":
        return await call_next(request)

    from src.core.permissions import get_role_permissions

    # role 优先级: JWT payload.role → DB users.role → "member"(向后兼容老 token)
    db_role, extra_perms = _resolve_user_auth(username)
    role = (payload.get("role") if payload else None) or db_role or "member"

    # ── guest(demo) 隔离: 行为保持现状 ──────────────────────────────
    if username == "demo" or role == "guest":
        msg = "演示账号为只读浏览模式,不可修改数据或访问管理页面。请自行部署体验完整功能: https://github.com/xiaoze-hub/Stock-Intelligent-Data-Analytics"
        # 0) GET 限流: 每小时 20 次 API 请求(防爬虫刷数据源配额)
        if method in ("GET", "HEAD"):
            from src.core.demo_limit import allow_api_get
            if not allow_api_get(str(payload.get("sub", ""))):
                return JSONResponse(status_code=429, content={"code": 429, "success": False, "message": "演示账号请求过于频繁(每小时限 20 次)。请稍后再试,或自行部署体验完整功能: https://github.com/xiaoze-hub/Stock-Intelligent-Data-Analytics"})
        # demo 专属例外: 自选增删(自己的数据, user_id 隔离; 数量上限在接口层)
        is_own_watchlist_write = (
            (method == "POST" and path.rstrip("/") == "/api/stocks")
            or (method == "DELETE" and path.startswith("/api/stocks/"))
        )
        # 1) 写操作: 除自选增删外一律拒绝
        if method not in ("GET", "HEAD", "OPTIONS") and not is_own_watchlist_write:
            return JSONResponse(status_code=403, content={"code": 403, "success": False, "message": msg})
        # 2) 管理区页面隔离: 设置/服务商列表允许浏览(敏感 key 已掩码), 其余管理页仍不可见
        _DEMO_READABLE_PREFIXES = ("/api/settings", "/api/providers")
        if path.startswith(_DEMO_ADMIN_PREFIXES) and not path.startswith(_DEMO_READABLE_PREFIXES):
            return JSONResponse(status_code=403, content={"code": 403, "success": False, "message": msg})
        return await call_next(request)

    # ── 非 guest: 角色权限驱动 ──────────────────────────────────────
    perms = set(get_role_permissions(role))
    perms |= extra_perms  # owner 给 member 开的白名单权限点

    # 自查询例外(2026-08-16): /api/users/me/permissions 是登录用户查自己的
    # 模块权限(前端导航过滤用), 只读且不暴露他人数据 → 任何登录用户放行,
    # 不受 /api/users → manage_users 管理区限制。
    if path.startswith("/api/users/me/permissions") and method in ("GET", "HEAD"):
        return await call_next(request)

    for prefix, required in _ADMIN_PREFIX_PERMISSIONS.items():
        if path.startswith(prefix):
            if required in perms:
                break
            # 只读浏览例外: settings/providers GET(敏感 key 已掩码)
            if method in ("GET", "HEAD") and prefix in _READABLE_ADMIN_PREFIXES:
                break
            return JSONResponse(
                status_code=403,
                content={"code": 403, "success": False, "message": "无权限访问该管理功能, 请联系管理员"},
            )
    return await call_next(request)

# 认证路由（无需登录）
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
# 行情 WebSocket(2026-08-12): 独立 router, 无路由级 auth(WS 握手与 HTTPBearer 冲突)
from src.web.api.ws_quotes import router as ws_quotes_router

app.include_router(ws_quotes_router, prefix="/api", tags=["quotes-ws"])
# 市场指数（公共数据，无需登录）
app.include_router(market.router, prefix="/api/market", tags=["market"])
# TradingView Alert Webhook(2026-08-12): 免登录, secret 鉴权
from src.web.api import tradingview_webhook

app.include_router(tradingview_webhook.router, prefix="/api/webhooks", tags=["webhooks"])

# 需要登录的路由
protected = [Depends(get_current_user)]
# 市场主线识别(2026-08-24, v0.3.0): Top20 主线 + 成分股; 60s 进程内缓存; 需登录
app.include_router(
    market_mainline.router,
    prefix="/api/market",
    tags=["market-mainline"],
    dependencies=protected,
)
app.include_router(
    stocks.router, prefix="/api/stocks", tags=["stocks"], dependencies=protected
)
app.include_router(
    quotes.router, prefix="/api/quotes", tags=["quotes"], dependencies=protected
)
app.include_router(
    klines.router, prefix="/api/klines", tags=["klines"], dependencies=protected
)
app.include_router(
    insights.router, prefix="/api/insights", tags=["insights"], dependencies=protected
)
# v0.3.0 thsdk L2 综合能力落地:个股快照 + 三大算法输出
app.include_router(
    thsdk_snapshot.router, prefix="/api/thsdk/snapshot", tags=["thsdk-snapshot"], dependencies=protected
)
app.include_router(
    thsdk_alert_router.router, prefix="/api/thsdk/alert", tags=["thsdk-alert"], dependencies=protected
)
# v0.3.1 选项B: DDE 官方主力资金 + 代码补齐 + 市场代码表
app.include_router(
    thsdk_ext_router.router,
    prefix="/api/thsdk/ext",
    tags=["thsdk-ext"],
    dependencies=protected,
)
# v0.3.1 thsdk 高价值待接能力落地: news/corporate_action/dde/hs300/跨市场/wencai-增强
app.include_router(
    thsdk_extended_router.router,
    prefix="/api/thsdk",
    tags=["thsdk-extended"],
    dependencies=protected,
)
app.include_router(
    accounts.router, prefix="/api", tags=["accounts"], dependencies=protected
)
app.include_router(
    agents.router, prefix="/api/agents", tags=["agents"], dependencies=protected
)
app.include_router(
    presets.router, prefix="/api/agents/presets", tags=["presets"], dependencies=protected
)
app.include_router(
    providers.router,
    prefix="/api/providers",
    tags=["providers"],
    dependencies=protected,
)
app.include_router(
    channels.router, prefix="/api/channels", tags=["channels"], dependencies=protected
)
app.include_router(
    subscriptions.router, prefix="/api/subscriptions", tags=["subscriptions"], dependencies=protected
)
app.include_router(
    notifications.router,
    prefix="/api/notifications",
    tags=["notifications"],
    dependencies=protected,
)
app.include_router(
    wechat_bind.router,
    tags=["wechat-bind"],
    dependencies=protected,
)
app.include_router(
    datasources.router,
    prefix="/api/datasources",
    tags=["datasources"],
    dependencies=protected,
)
app.include_router(
    settings.router, prefix="/api/settings", tags=["settings"], dependencies=protected
)
app.include_router(
    logs.router, prefix="/api/logs", tags=["logs"], dependencies=protected
)
app.include_router(
    history.router, prefix="/api", tags=["history"], dependencies=protected
)
app.include_router(
    context.router, prefix="/api", tags=["context"], dependencies=protected
)
# 权限体系(2026-08-15): BYOK 用户自定义服务商 + 用户模型授权管理
app.include_router(
    my_ai_services.router,
    prefix="/api/my-ai-services",
    tags=["my-ai-services"],
    dependencies=protected,
)
app.include_router(
    users.router,
    prefix="/api/users",
    tags=["users"],
    dependencies=protected,
)
app.include_router(
    llm_usage.router,
    prefix="/api",
    tags=["llm-usage"],
    dependencies=protected,
)
app.include_router(
    profile.router,
    prefix="/api/profile",
    tags=["profile"],
    dependencies=protected,
)
app.include_router(
    export_data.router,
    prefix="/api",
    tags=["export"],
    dependencies=protected,
)
app.include_router(
    audit.router,
    prefix="/api/audit",
    tags=["audit"],
    dependencies=protected,
)
app.include_router(
    news.router, prefix="/api/news", tags=["news"], dependencies=protected
)
app.include_router(
    suggestions.router,
    prefix="/api/suggestions",
    tags=["suggestions"],
    dependencies=protected,
)
app.include_router(
    templates.router,
    prefix="/api/templates",
    tags=["templates"],
    dependencies=protected,
)
app.include_router(
    feedback.router,
    prefix="/api/feedback",
    tags=["feedback"],
    dependencies=protected,
)

app.include_router(
    discovery.router,
    prefix="/api/discovery",
    tags=["discovery"],
    dependencies=protected,
)
app.include_router(
    price_alerts.router,
    prefix="/api/price-alerts",
    tags=["price-alerts"],
    dependencies=protected,
)
app.include_router(
    recommendations.router,
    prefix="/api/recommendations",
    tags=["recommendations"],
    dependencies=protected,
)
app.include_router(
    dashboard.router,
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=protected,
)
app.include_router(
    factors.router,
    prefix="/api/factors",
    tags=["factors"],
    dependencies=protected,
)
app.include_router(
    health_router.router,
    prefix="/api/health",
    tags=["health"],
    dependencies=protected,
)
app.include_router(
    forecast.router,
    prefix="/api",
    tags=["forecast"],
    dependencies=protected,
)
app.include_router(
    paper_trading.router,
    prefix="/api/paper-trading",
    tags=["paper-trading"],
    dependencies=protected,
)
app.include_router(
    chat.router,
    prefix="/api/chat",
    tags=["chat"],
    dependencies=protected,
)
# 对话助手附件上传/解析(2026-08-14): 图片 OCR / Excel / PDF / txt,md
app.include_router(
    chat_upload.router,
    prefix="/api/chat",
    tags=["chat-upload"],
    dependencies=protected,
)
app.include_router(
    reports.router,
    prefix="/api/reports",
    tags=["reports"],
    dependencies=protected,
)
app.include_router(
    strategies.router,
    prefix="/api/strategies",
    tags=["strategies"],
    dependencies=protected,
)
app.include_router(
    calendar.router,
    prefix="/api/calendar",
    tags=["calendar"],
    dependencies=protected,
)
app.include_router(
    market_data.router,
    prefix="/api/market-data",
    tags=["market-data"],
    dependencies=protected,
)
app.include_router(
    tdx.router,
    prefix="/api/tdx",
    tags=["tdx"],
    dependencies=protected,
)
app.include_router(
    shadow.router,
    prefix="/api/shadow",
    tags=["shadow"],
    dependencies=protected,
)
app.include_router(
    ths.router,
    prefix="/api/ths",
    tags=["ths"],
    dependencies=protected,
)
# 内盘外盘口诀 + 主力意图(分时卡片轻接口, 2026-08-13)
app.include_router(
    darkflow.router,
    prefix="/api/dark-flow",
    tags=["dark-flow"],
    dependencies=protected,
)
# 决策先锋三指标(GS策略+暗盘资金+AI机构活跃度) + L2主力净流入(盘中实时, 2026-08-30)
app.include_router(
    decision_pioneer.router,
    prefix="/api/decision-pioneer",
    tags=["decision-pioneer"],
    dependencies=protected,
)
# 决策先锋选股池(三指标共振扫描, 盘中实时, 2026-08-30)
app.include_router(
    stock_pool.router,
    prefix="/api/stock-pool",
    tags=["stock-pool"],
    dependencies=protected,
)
# 板块数据(阶段2.1/2.2, 2026-08-20): 板块/概念列表 + 详情 + 成分股 + 轮动
app.include_router(
    boards.router,
    prefix="/api/boards",
    tags=["boards"],
    dependencies=protected,
)
# 主力意图双源对比(阶段1.1, 2026-08-20): 腾讯逐笔 vs thsdk L2
app.include_router(
    main_flow.router,
    prefix="/api/main-flow",
    tags=["main-flow"],
    dependencies=protected,
)
# 竞价异动池(阶段1.2, 2026-08-20): 异动池 + 历史 + 同步
# (模块名用 auction_pool, 规避 src/web/api/auction.py 竞价快照占用)
app.include_router(
    auction_pool.router,
    prefix="/api/auction",
    tags=["auction-pool"],
    dependencies=protected,
)
# 异动接近度监控(任务 C, 2026-08-24): 交易所异常波动规则 60s 扫描
app.include_router(
    abnormal_moves.router,
    prefix="/api/abnormal-moves",
    tags=["abnormal-moves"],
    dependencies=protected,
)
# 情绪周期 6 阶段(2026-08-24, 任务 A): 当前阶段 + 30 天序列 + 分布
# prefix /api/market 与现有 market.router(indices)共存, market_phase 用 /phase 子路径
app.include_router(
    market_phase.router,
    prefix="/api/market",
    tags=["market-phase"],
    dependencies=protected,
)

# ---- L2 轻接口(2026-08-20): OB 失衡条/竞价快照/问小达 容错注册 ----
# 三个模块由并行子任务创建; 模块未就绪(尚未创建)或依赖缺失时 import 抛 ImportError,
# 被 try/except 吸收后跳过注册 → 启动/import 永不因这三个路由而崩。
try:
    from src.web.api import orderbook

    app.include_router(
        orderbook.router,
        prefix="/api/orderbook-ob",
        tags=["orderbook-ob"],
        dependencies=protected,
    )
except ImportError:
    pass

try:
    from src.web.api import auction

    app.include_router(
        auction.router,
        prefix="/api/auction-snapshot",
        tags=["auction-snapshot"],
        dependencies=protected,
    )
except ImportError:
    pass

try:
    from src.web.api import wencai

    app.include_router(
        wencai.router,
        prefix="/api/wencai",
        tags=["wencai"],
        dependencies=protected,
    )
except ImportError:
    pass


@app.get("/api/version")
async def version():
    """获取应用版本号（公开接口）"""
    return {"version": get_app_version()}

# 2026-08-17 v0.2.65: 深度健康检查 + Prometheus 指标(挂 /api 前缀, 跟其他路由一致)
app.include_router(health_router.router, prefix="/api")
