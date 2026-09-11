"""大盘资金流快照采样(2026-09-11, 老板报"大盘资金流图没内容")。

背景: `market_flow_snapshots` 原先只在大盘资金流接口被**前端调用**时写入(30s 节流)
→ 页面不打开时曲线一整天只有零星几个点, 看起来"没内容"。
本采样器独立于前端: 交易时段内每分钟把网关聚合值落一条, 使日内曲线全天连续。

口径与 `/api/market-data/market-capital-flow` 完全一致(同一网关 + 同字段映射):
- total_main_flow 单位=亿(东财四档口径, 仅资金面参考)
- 失败/非交易时段静默跳过(job 永不抛异常); 重拉幂等(同 ts 秒级不同, 属正常时序数据)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

_GATEWAY = "http://115.190.177.213:8100/cn/market-overview"
_TIMEOUT_S = 6.0


def collect_once(now: datetime | None = None) -> dict:
    """采样一次: 非交易时段跳过; 取数失败静默返回(不抛)。"""
    from src.core.quote_snapshots import in_trading_window

    if not in_trading_window(now):
        return {"ok": True, "skipped": True, "reason": "非交易时段"}

    try:
        import requests as _req

        ov = _req.get(_GATEWAY, timeout=_TIMEOUT_S).json()
    except Exception as e:  # noqa: BLE001
        logger.warning("大盘资金快照采样失败(静默): %s", e)
        return {"ok": False, "skipped": False, "error": str(e)}

    if not isinstance(ov, dict) or ov.get("error"):
        return {"ok": False, "skipped": False, "error": str((ov or {}).get("error") or "空响应")}

    row = {
        "total_main_flow": ov.get("total_main_flow"),
        "up_count": ov.get("up_count"),
        "down_count": ov.get("down_count"),
        "flat_count": ov.get("flat_count"),
        "sh_flow": (ov.get("sh") or {}).get("main_flow"),
        "sz_flow": (ov.get("sz") or {}).get("main_flow"),
    }
    if row["total_main_flow"] is None:
        return {"ok": False, "skipped": False, "error": "缺 total_main_flow"}

    try:
        from src.db.session import engine

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO market_flow_snapshots
                        (ts, total_main_flow, up_count, down_count, flat_count, sh_flow, sz_flow)
                    VALUES
                        (:ts, :total_main_flow, :up_count, :down_count, :flat_count, :sh_flow, :sz_flow)
                    """
                ),
                {**row, "ts": datetime.now(timezone.utc).replace(tzinfo=None)},
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("大盘资金快照写库失败(静默): %s", e)
        return {"ok": False, "skipped": False, "error": str(e)}

    return {"ok": True, "skipped": False, **row}
