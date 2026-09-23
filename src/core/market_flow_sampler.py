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
from datetime import datetime

from sqlalchemy import text

logger = logging.getLogger(__name__)

_GATEWAY = "http://115.190.177.213:8100/cn/market-overview"
_TIMEOUT_S = 6.0


def _tq_breadth_safe() -> dict | None:
    """TQ 涨跌家数(取不到/异常 → None)。TQ 走本地客户端, 与 cn 网关是否可用无关。"""
    try:
        from src.core.tdx_boards import market_breadth

        return market_breadth()
    except Exception as e:  # noqa: BLE001
        logger.debug("涨跌家数(TQ)不可用: %s", e)
        return None


def _write_row(row: dict) -> bool:
    """落一条快照。失败静默返回 False(采样 job 不抛异常)。"""
    try:
        from src.db.session import engine

        with engine.begin() as conn:
            # ts 不显式传值: 与接口侧写入(_try_write_snapshot_async)一致由 DB 默认
            # CURRENT_TIMESTAMP 生成 —— 曾按 UTC 手写, 与既有本地时间序列错位。
            conn.execute(
                text(
                    """
                    INSERT INTO market_flow_snapshots
                        (total_main_flow, up_count, down_count, flat_count, sh_flow, sz_flow)
                    VALUES
                        (:total_main_flow, :up_count, :down_count, :flat_count, :sh_flow, :sz_flow)
                    """
                ),
                row,
            )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("大盘资金快照写库失败(静默): %s", e)
        return False


def collect_once(now: datetime | None = None) -> dict:
    """采样一次: 非交易时段跳过; 取数失败静默返回(不抛)。

    2026-09-23 清单切换: 涨跌家数改走 TQ(见下)。**网关故障不再等于整条不落库** ——
    网关挂时仍用 TQ 落一条"只有家数"的行(total_main_flow/sh/sz 留 NULL, 不编造),
    否则日内家数曲线会在网关故障期间整段缺失, 而 TQ 本可独立提供家数。
    """
    from src.core.quote_snapshots import in_trading_window

    if not in_trading_window(now):
        return {"ok": True, "skipped": True, "reason": "非交易时段"}

    ov: dict | None = None
    gate_err: str | None = None
    try:
        import requests as _req

        ov = _req.get(_GATEWAY, timeout=_TIMEOUT_S).json()
    except Exception as e:  # noqa: BLE001
        gate_err = str(e)
    if not isinstance(ov, dict):
        ov = None
    elif ov.get("error"):
        gate_err = str(ov["error"])
        ov = None

    # 涨跌家数走 TQ(2026-09-23 清单切换): 与网关是否可用无关, 零配额。
    br = _tq_breadth_safe()

    if ov is None:
        # 网关不可用: 有 TQ 家数就落一条家数行, 让曲线不断
        if not br:
            return {"ok": False, "skipped": False, "error": gate_err or "空响应"}
        row = {
            "total_main_flow": None, "sh_flow": None, "sz_flow": None,
            "up_count": br["up"], "down_count": br["down"], "flat_count": br["flat"],
        }
        if not _write_row(row):
            return {"ok": False, "skipped": False, "error": "写库失败"}
        logger.info("大盘资金快照(仅家数, 网关不可用): %s", gate_err)
        return {"ok": True, "skipped": False, "breadth_only": True, **row}

    row = {
        "total_main_flow": ov.get("total_main_flow"),
        "up_count": ov.get("up_count"),
        "down_count": ov.get("down_count"),
        "flat_count": ov.get("flat_count"),
        "sh_flow": (ov.get("sh") or {}).get("main_flow"),
        "sz_flow": (ov.get("sz") or {}).get("main_flow"),
    }
    # 拿得到 TQ 家数就覆盖网关值 —— 口径不变(涨/跌/平), 只是换源去依赖
    if br:
        row["up_count"] = br["up"]
        row["down_count"] = br["down"]
        row["flat_count"] = br["flat"]
    if row["total_main_flow"] is None:
        # 网关通但缺资金字段(部分报文) → 仍可落家数行
        if not br:
            return {"ok": False, "skipped": False, "error": "缺 total_main_flow"}
        row["total_main_flow"] = None
    if not _write_row(row):
        return {"ok": False, "skipped": False, "error": "写库失败"}
    return {"ok": True, "skipped": False, **row}
