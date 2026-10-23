"""四档资金结构 日序列 API（2026-10-24, exday_daily 消费 MVP）。

GET /api/market/fund-structure/{symbol}?market=CN&start=20261023&end=20261031&limit=30&refresh=0
→ {available, symbol, market, caliber, direction_semantics, source,
   tier_thresholds_hint, data_since, count, start, end, limit, items: [表行原样...]}

- 数据源 `exday_daily` 表（Migration 220）：通达信 TQ `get_exday_data` 每交易日
  15:45 全市场落库累积，**2026-10-23 起才有数据**，历史无法回填。
- 口径 `tdx_exday4`：按**单笔成交金额**分四档的客户端口径，与腾讯逐笔(tick)、
  东财四档(eastmoney4)均不可换算 —— 响应强制携带 caliber + direction_semantics，
  同屏其他口径资金数据必须各带标签，禁止跨源混算（AGENTS.md 口径红线）。
- 缺日期**不补 0**：items 只含实际落库行，前端据此决定画不画趋势线（单日不画）。
- 缓存走 biz_cache（L1+L2, key 前缀 biz:），日频数据 TTL 300s；refresh=1 绕过。
- 失败降级 available=false（读库失败前先 safe_rollback，见 src/core/db_safe）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from src.web.cache.biz_cache import biz_cache
from src.web.database import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()

#: 日频数据（15:45 落库后一天不变）, 300s 缓存足够; 写穿(refresh)后手动刷新立即可见
_CACHE_TTL = 300


def _cache_key(symbol: str, market: str, start: str | None, end: str | None, limit: int) -> str:
    return (
        f"fund_structure:{symbol}:{market}:"
        f"{start or ''}:{end or ''}:{limit}"
    )


@router.get("/fund-structure/{symbol}")
def fund_structure_series(
    symbol: str,
    market: str = "CN",
    start: str | None = None,
    end: str | None = None,
    limit: int = 30,
    refresh: int = 0,
):
    """该标的的 exday_daily 日期序列（默认近 30 个交易日, 可传 start/end）。

    稀疏数据契约：表 2026-10-23 起累积, `data_since` 是全表最早交易日,
    `count` 是该股实际落库行数 —— 前端必须显式标注「数据自 2026-10-23 起累积,
    N 个交易日」; count==1 只展示当日结构不画趋势线; 无数据显式「无数据」。
    """
    from src.core import exday_structure as ex

    sym = (symbol or "").strip().split(".")[0]
    mkt = (market or "CN").strip().upper() or "CN"
    if not sym:
        return {"available": False, "caliber": ex.CALIBER, "reason": "缺少标的代码", "items": []}

    key = _cache_key(sym, mkt, start, end, limit)
    if not refresh:
        cached = biz_cache.get_json(key)
        if cached is not None:
            return cached

    def _fetch():
        db = SessionLocal()
        try:
            return ex.query_series(db, sym, market=mkt, start=start, end=end, limit=limit)
        finally:
            db.close()

    try:
        res = _fetch()
    except Exception as exc:  # noqa: BLE001 - session 创建/连接失败 → 显式无数据
        logger.warning("fund-structure %s 取数失败: %s", sym, exc)
        return {
            "available": False, "caliber": ex.CALIBER,
            "reason": f"取数失败: {type(exc).__name__}", "items": [],
        }
    # 只缓存成功结果: 失败(缺表/库断)不缓存, 恢复后下一个请求立即可见
    if res.get("available"):
        biz_cache.set_json(key, res, ttl=_CACHE_TTL)
    return res
