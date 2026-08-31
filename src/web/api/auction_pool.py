"""竞价异动池 API(v0.3.2, 2026-08-24 字段口径二次修正)。

GET  /api/auction/anomaly?market=CN              → 最新竞价异动池(fetch_auction_anomaly)
GET  /api/auction/anomaly/{symbol}/history?days=5 → 某股近 N 天竞价异动历史(DB)
POST /api/auction/sync                            → 触发热拉 + 落库(内部用, cron 也用)

⚠️ 模块名用 auction_pool 而非 auction: src/web/api/auction.py 已被并行子任务占用
(竞价快照 /api/auction-snapshot), 避免撞名。本模块路由注册在 /api/auction 前缀下。

⚠️ 字段口径(2026-08-24 v0.3.2 二次修正 — 推翻 v0.3.1 错误假设):
- "价格" 列**不是价格**: 是异动幅度小数比例 / 撤单率 / 占位 1.0。
- "总金额" 列恒为 2147483648 (int32 上限占位垃圾), 已 skip 不入 record。
- gap_pct / withdraw_rate 由 src.core.auction_pool._to_records 基于
  异动类型 + 价格列直接推导(无需 klines 昨收):
  * 急速涨跌 / 大幅高低开 → gap_pct = 价格 × 100
  * 涨停/跌停试盘       → gap_pct = None(价格=1.0 占位无信息)
  * 涨停/跌停撤单       → withdraw_rate = 价格 × 100, gap_pct = None
  * 其他类型             → |价格|<0.21 才按涨跌幅处理, 否则 None
- volume_ratio 数据源不提供, 响应 missing_fields 仅声明该字段(2026-08-24 v0.3.2 收紧,
  withdraw_rate 已部分填充不再 always-missing)。
- 前端 AuctionAnomalyTab.tsx 对 None 字段显 "—", 无需大改。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from src.core.auction_pool import (
    MISSING_FIELDS,
    MISSING_NOTE,
    fetch_auction_anomaly,
    get_anomaly_history,
    sync_auction_to_db,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _validate_symbol(raw: str) -> str:
    code = (raw or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, f"非法股票代码: {raw!r}(需要6位A股代码)")
    return code


@router.get("/anomaly")
def anomaly(market: str = Query("CN", description="CN/SH/SZ/BJ/ALL 或 thsdk 代码(USHA等)")):
    """最新竞价异动池。数据源不可用时 available=false 且如实说明, 不伪造。

    2026-08-24 v0.3.2: 响应 fixed 字段 missing_fields + note, 告知前端
    数据源不提供量比(撤单率已部分填充)。前端表格对应列显 "—"。
    """
    records = fetch_auction_anomaly(market)
    if not records:
        return {
            "available": False,
            "count": 0,
            "records": [],
            "missing_fields": list(MISSING_FIELDS),
            "note": MISSING_NOTE
            + " | thsdk 竞价异动数据暂不可用(数据源未接入/非交易时段/拉取失败)",
        }
    return {
        "available": True,
        "count": len(records),
        "records": records,
        "missing_fields": list(MISSING_FIELDS),
        "note": MISSING_NOTE,
    }


@router.get("/anomaly/{symbol}/history")
def history(
    symbol: str,
    days: int = Query(5, ge=1, le=90, description="查询近 N 天(默认5)"),
):
    """某股近 N 天竞价异动历史(DB 落库追踪)。"""
    code = _validate_symbol(symbol)
    rows = get_anomaly_history(code, days=days)
    return {
        "symbol": code,
        "days": days,
        "count": len(rows),
        "records": rows,
        "missing_fields": list(MISSING_FIELDS),
        "note": MISSING_NOTE,
    }


@router.post("/sync")
def sync(market: str = Query("CN", description="同步的市场(默认 CN 沪深沪A)")):
    """触发热拉竞价异动并落库(内部用, 由工作日 09:25 cron 与手动运维触发)。"""
    try:
        records = fetch_auction_anomaly(market)
        n = sync_auction_to_db(records)
        return {"synced": n, "fetched": len(records), "market": market}
    except Exception as e:  # noqa: BLE001
        logger.error("[auction-sync] 竞价异动同步失败: %r", e)
        raise HTTPException(502, f"竞价异动同步失败: {e!r}")
