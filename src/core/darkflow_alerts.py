"""主力意图异常告警(#5, 2026-09-04)。

suspect(熔断)/stale(停滞) 以前只进 diag, 没人看。本模块在主链路里顺手推
通知: 全局默认渠道(enabled + user_id IS NULL), 同股同类每日一次。
渠道没配/发送失败 → 静默记日志, 永不影响主链路。

注意: WeCom corp 应用权限(850003/853006)需用户在管理页授权+购买,
修好前走用户已配的其他渠道(Telegram/群机器人 webhook 等)。
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

_ALERT_DISK = None


def _alert_cache():
    global _ALERT_DISK
    if _ALERT_DISK is None:
        from src.core.disk_cache import DiskCache, register
        _ALERT_DISK = DiskCache("darkflow_alerts", ttl=86400.0)
        register(_ALERT_DISK)
    return _ALERT_DISK


def should_alert(code: str, day: str, kind: str, cache=None) -> bool:
    """节流判定(纯逻辑, cache 可注入单测): 同股同日同类只报一次。"""
    c = cache if cache is not None else _alert_cache()
    key = f"{code}:{day}:{kind}"
    if c.get(key):
        return False
    c.set(key, True)
    return True


def maybe_alert_anomaly(symbol6: str, tcode: str, day: str, kind: str, detail: str) -> bool:
    """发一次异常告警。返回 True=已发送。任何异常吞掉(False)。"""
    try:
        if not should_alert(tcode, day, kind):
            return False
        from src.web.database import SessionLocal
        from src.web.models import NotifyChannel
        from src.core.notifier import NotifierManager

        db = SessionLocal()
        try:
            channels = (
                db.query(NotifyChannel)
                .filter(NotifyChannel.enabled == True,  # noqa: E712
                        NotifyChannel.user_id.is_(None))
                .all()
            )
        finally:
            db.close()
        if not channels:
            logger.debug("[darkflow_alerts] 无全局通知渠道, 跳过")
            return False
        mgr = NotifierManager()
        for ch in channels:
            try:
                mgr.add_channel(ch.type, ch.config or {})
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[darkflow_alerts] 渠道注册失败 {ch.type}: {e}")
        title = f"【主力意图异常】{symbol6} {kind}"
        res = asyncio.run(mgr.notify_with_result(title, detail, bypass_quiet_hours=True))
        ok = bool(res.get("success"))
        logger.info(f"[darkflow_alerts] {symbol6} {kind} 发送{'成功' if ok else '失败'}")
        return ok
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[darkflow_alerts] 跳过 {symbol6} {kind}: {e}")
        return False
