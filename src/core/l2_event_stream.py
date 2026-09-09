"""L2 事件流推送(批次F F2, 2026-09-06 28号)。

两类独家粒度事件(委托级拆单识别 + 封单成色, 同花顺收费产品都没有的粒度):
- dark_cluster:  暗盘净流入聚簇 ≥ 100 万元(逐笔拆单识别口径, dark_flow.split_order.net)
- seal_anomaly:  涨停封单成色异常(撤单率 z-score ≥ 2 或 成色 < 0.6 且仍封住)

节流: 复用 darkflow_alerts.should_alert(同股同日同类一次, DiskCache 24h)。
推送: 全局默认渠道(enabled + user_id IS NULL), WeCom/Telegram/webhook 等, 失败静默。
评估入口 eval_tick(symbols) 由 seal_sampler.sample_tick 顺带调用(盘中 60s)。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

DARK_CLUSTER_THRESHOLD = 1_000_000.0  # 元(红线口径: 金额=元)
_SEAL_Z_TRIGGER = 2.0
_SEAL_QUALITY_FLOOR = 0.6
_MAX_SYMBOLS_PER_TICK = 10

_EVENT_TITLES = {
    "dark_cluster": "【L2事件流】暗盘聚簇",
    "seal_anomaly": "【L2事件流】封单成色异常",
}


def evaluate_dark_cluster(split_order: dict | None) -> tuple[bool, str]:
    """暗盘聚簇判定(纯函数)。split_order 来自 dark_flow.compute_dark_flow。"""
    if not isinstance(split_order, dict):
        return False, ""
    net = split_order.get("net")
    if not isinstance(net, (int, float)):
        return False, ""
    if net >= DARK_CLUSTER_THRESHOLD:
        return True, f"暗盘净流入 {net / 1e4:.0f} 万(≥100万阈值, 拆单识别口径)"
    return False, ""


def evaluate_seal_anomaly(metrics: dict) -> tuple[bool, str]:
    """封单成色异常判定(纯函数)。metrics 来自 seal_quality.compute_from_series。"""
    if not metrics.get("available") or not metrics.get("is_sealed"):
        return False, ""
    z = metrics.get("cancel_zscore")
    q = metrics.get("seal_quality")
    if z is not None and z >= _SEAL_Z_TRIGGER:
        return True, f"撤单率异动 z={z} (封单成色 {q if q is not None else '--'})"
    if q is not None and q < _SEAL_QUALITY_FLOOR:
        return True, f"封单成色 {q:.0%} 偏低(5min 撤单率 {1 - q:.0%})"
    return False, ""


def push_event(kind: str, symbol: str, day: str, detail: str) -> bool:
    """推一次事件(节流+全局渠道)。永不抛异常。"""
    try:
        from src.core.darkflow_alerts import should_alert

        if not should_alert(symbol, day, kind):
            return False
        from src.core.notifier import NotifierManager
        from src.db.session import SessionLocal
        from src.db.models import NotifyChannel

        db = SessionLocal()
        try:
            channels = (
                db.query(NotifyChannel)
                .filter(NotifyChannel.enabled == True,  # noqa: E711
                        NotifyChannel.user_id.is_(None))
                .all()
            )
        finally:
            db.close()
        if not channels:
            logger.debug("[l2_event_stream] 无全局通知渠道, 跳过")
            return False
        mgr = NotifierManager()
        for ch in channels:
            try:
                mgr.add_channel(ch.type, ch.config or {})
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[l2_event_stream] 渠道注册失败 {ch.type}: {e}")
        title = f"{_EVENT_TITLES.get(kind, '【L2事件流】' + kind)} {symbol}"
        res = asyncio.run(mgr.notify_with_result(title, detail, bypass_quiet_hours=True))
        ok = bool(res.get("success"))
        logger.info("[l2_event_stream] %s %s 发送%s", kind, symbol, "成功" if ok else "失败")
        return ok
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[l2_event_stream] 跳过 {kind}/{symbol}: {e}")
        return False


def eval_tick(symbols: list[str]) -> dict:
    """盘中评估入口(seal_sampler 60s 顺带调用)。永不抛异常。"""
    stats = {"dark_cluster": 0, "seal_anomaly": 0, "checked": 0}
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from marketdata.symbol import Symbol as _Symbol

        from src.core.seal_quality import compute_from_series
        from src.core.seal_sampler import get_recent_samples

        day = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        for sym in symbols[:_MAX_SYMBOLS_PER_TICK]:
            stats["checked"] += 1
            # 1) 暗盘聚簇
            try:
                from src.core.dark_flow import compute_dark_flow

                dark = compute_dark_flow(_Symbol.parse(sym, "CN"))
                hit, detail = evaluate_dark_cluster((dark or {}).get("split_order"))
                if hit and push_event("dark_cluster", sym, day, detail):
                    stats["dark_cluster"] += 1
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[l2_event_stream] dark_flow {sym}: {e}")
            # 2) 封单成色异常(需要已有采样序列)
            try:
                samples = get_recent_samples(sym)
                if samples:
                    metrics = compute_from_series(samples)
                    hit, detail = evaluate_seal_anomaly(metrics)
                    if hit and push_event("seal_anomaly", sym, day, detail):
                        stats["seal_anomaly"] += 1
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[l2_event_stream] seal anomaly {sym}: {e}")
        return stats
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[l2_event_stream] eval_tick 失败: {e}")
        return stats
