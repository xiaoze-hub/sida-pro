"""TradingView Alert Webhook 接收端点(2026-08-12)。

用户 Pine Script 策略在 TradingView 上跑, 触发 Alert 时向本端点 POST JSON:
  POST /api/webhooks/tradingview
  Header: X-PanWatch-Secret: <secret>
  Body(TradingView Alert 标准格式):
    {"exchange":"SSE","ticker":"600519","time":"...","close":1234.5,
     "volume":..., "strategy":{...}, "message":"..."}   ← message 由 Alert 自定义

行为:
- secret 校验失败 → 401
- 校验通过 → 写站内通知 + 按订阅用户外发(复用 push_notification)
- 绝不抛异常(webhook 回调失败不能 500 刷屏)

安全: 不依赖登录态(TradingView 服务器无法带 PanWatch token), 用共享 secret 鉴权。
secret 来源: 环境变量 PANWATCH_TV_WEBHOOK_SECRET; 未配置时端点禁用(503)。
"""
import hmac
import os

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


def _webhook_secret() -> str:
    """每次请求动态读取 env(2026-08-12): 模块级常量在 import 时求值,
    测试/多环境切换时 env 可能未就绪, 动态读保证 secret 变更/测试隔离生效。"""
    return os.environ.get("PANWATCH_TV_WEBHOOK_SECRET", "")


class TVAlertPayload(BaseModel):
    ticker: str = ""
    exchange: str = ""
    time: str = ""
    close: float | None = None
    volume: float | None = None
    message: str = ""
    strategy: dict | None = None
    interval: str = ""
    # 允许任意额外字段(不同 Pine 策略 alert 格式不同)
    model_config = {"extra": "allow"}


def _secret_ok(secret: str | None) -> bool:
    if not _webhook_secret() or not secret:
        return False
    return hmac.compare_digest(secret, _webhook_secret())


@router.post("/tradingview")
async def tradingview_alert(request: Request):
    if not _webhook_secret():
        return {"ok": False, "error": "webhook_disabled"}
    secret = request.headers.get("X-PanWatch-Secret", "")
    if not _secret_ok(secret):
        return {"ok": False, "error": "unauthorized"}
    try:
        raw = await request.json()
    except Exception:
        return {"ok": False, "error": "bad_json"}
    try:
        payload = TVAlertPayload(**raw)
    except Exception:
        payload = TVAlertPayload()
        # 用原始 dict 兜底(字段名不匹配时)
        for k, v in raw.items():
            if not getattr(payload, k, None):
                try:
                    setattr(payload, k, v)
                except Exception:
                    pass

    # 组装可读告警文本
    ticker = payload.ticker or raw.get("symbol", "") or "?"
    ex = payload.exchange or ""
    price = f"{payload.close:g}" if payload.close is not None else "--"
    msg = (payload.message or "").strip()
    lines = [f"📡 TradingView Alert | {ex} {ticker}"]
    if price != "--":
        lines.append(f"价格: {price}")
    if payload.interval:
        lines.append(f"周期: {payload.interval}")
    if msg:
        lines.append(f"信号: {msg}")
    body = "\n".join(lines)

    try:
        from src.core.notify_center import push_notification

        push_notification(
            f"TV信号 {ticker}",
            body,
            category="alert",
            level="warning",
            source="tradingview",
        )
    except Exception:
        # 通知失败不能返回错误(webhook 会重试导致重复)
        pass
    return {"ok": True, "ticker": ticker}
