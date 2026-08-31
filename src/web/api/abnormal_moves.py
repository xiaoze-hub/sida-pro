"""异动接近度监控 API(任务 C, 2026-08-24)。

GET /api/abnormal-moves?min_proximity=0.5  -> 扫描自选股 + 当日候选池,

  返回 {
    "available": True/False,
    "min_proximity": 0.5,
    "count": N,
    "items": [
      {symbol, name, board, board_name, benchmark, available,
       worst, windows, status, proximity}
    ],
    "note": "...",
  }

数据源:
  - 自选股: src.web.models.Stock (user 自己 + 全局)
  - 当日候选池: src.web.models.AuctionAnomalyRecord (工作日 09:25 cron + manual sync 落库)

进程内缓存: 60s (biz_cache 两级缓存; key 前缀 biz:am:)

不动 src/core/market_phase.py / src/web/api/market_phase.py /
   src/core/market_mainline.py / src/web/api/market_mainline.py 及对应前端组件。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.core.abnormal_moves import analyze_for_symbols
from src.web.api.auth import get_current_user
from src.web.cache.biz_cache import biz_cache
from src.web.database import get_db
from src.web.models import AuctionAnomalyRecord, Stock, User

logger = logging.getLogger(__name__)

router = APIRouter()

CACHE_TTL_S = 60
CACHE_KEY_FMT = "am:all:{min_proximity:.2f}"

# 单股分析次数上限(防被异常数据拖爆); 实际生产由 K 线负缓存保护.
_ANALYZE_LIMIT = 100


def _gather_candidate_symbols(db: Session) -> dict[str, dict]:
    """合并 watchlist + 当日竞价异动池, 返回 {symbol: {'name': ..., 'source': ...}}.

    - 去重: 同 symbol 多个来源时, 'source' 用逗号连接.
    - watchlist 全部走 db.query, 不依赖业务层外部 service.
    - 当日候选: AuctionAnomalyRecord.created_at >= today 00:00 (本机 tz).
    """
    out: dict[str, dict] = {}

    # 1) 自选股
    try:
        for row in db.query(Stock).all():
            code = (row.symbol or "").strip()
            if not code or not code.isdigit() or len(code) != 6:
                continue
            tag = "watchlist"
            cur = out.get(code)
            if cur:
                sources = [s for s in cur.get("source", "").split(",") if s]
                if tag not in sources:
                    sources.append(tag)
                    cur["source"] = ",".join(sources)
                if not cur.get("name") and row.name:
                    cur["name"] = row.name
            else:
                out[code] = {
                    "name": row.name or "",
                    "source": tag,
                    "market": row.market or "CN",
                }
    except Exception as e:
        logger.debug("[abnormal_moves] watchlist 读取失败: %r", e)

    # 2) 当日竞价异动池(本机时区今日 00:00 之后)
    try:
        today_midnight = datetime.combine(
            datetime.now().date(), datetime.min.time()
        )
        rows = (
            db.query(AuctionAnomalyRecord)
            .filter(AuctionAnomalyRecord.created_at >= today_midnight)
            .order_by(AuctionAnomalyRecord.created_at.desc())
            .all()
        )
        seen_today: dict[str, str] = {}
        for r in rows:
            code = (r.symbol or "").strip()
            if not code or not code.isdigit() or len(code) != 6:
                continue
            # 同 symbol 当日可能多条; 取最近一条 name.
            if code in seen_today:
                continue
            seen_today[code] = (r.name or "").strip()
            tag = "auction_pool"
            cur = out.get(code)
            if cur:
                sources = [s for s in cur.get("source", "").split(",") if s]
                if tag not in sources:
                    sources.append(tag)
                    cur["source"] = ",".join(sources)
                if not cur.get("name"):
                    cur["name"] = (r.name or "").strip()
            else:
                out[code] = {
                    "name": (r.name or "").strip(),
                    "source": tag,
                    "market": "CN",
                }
    except Exception as e:
        logger.warning("[abnormal_moves] 当日候选池读取失败: %r", e)

    return out


def _attach_name_and_source(items: list[dict], meta: dict[str, dict]) -> list[dict]:
    """analyze_abnormal_moves 输出的 item 不含 name; 这里补上.

    来源兼容: source 字符串 (例 'watchlist' / 'auction_pool' / 'watchlist,auction_pool').
    """
    for it in items:
        sym = (it.get("symbol") or "").strip()
        m = meta.get(sym) or {}
        # 仅在缺失时填, 避免覆盖实际返回的 name
        if not it.get("name"):
            it["name"] = m.get("name") or ""
        it["source"] = m.get("source") or "watchlist"
    return items


def _scan_impl(db: Session, min_proximity: float) -> dict[str, Any]:
    """缓存穿透封装: 命中-> 直返; miss-> 走 DB+分析后回填."""
    meta = _gather_candidate_symbols(db)
    if not meta:
        return {
            "available": False,
            "min_proximity": min_proximity,
            "count": 0,
            "items": [],
            "note": "自选股+当日候选池为空, 无标的扫描",
        }

    # 限量保护: 大池子先按 None proximity 预警
    symbols = list(meta.keys())
    if len(symbols) > _ANALYZE_LIMIT:
        logger.info(
            "[abnormal_moves] 候选池 %d > 限制 %d, 仅扫描前 %d",
            len(symbols), _ANALYZE_LIMIT, _ANALYZE_LIMIT,
        )
        symbols = symbols[:_ANALYZE_LIMIT]

    items = analyze_for_symbols(symbols, min_proximity=min_proximity)
    items = _attach_name_and_source(items, meta)

    return {
        "available": True,
        "min_proximity": min_proximity,
        "count": len(items),
        "items": items,
        "note": (
            f"按 {min_proximity:.2f} proximity 阈值过滤; 触发/边缘/观察按交易所异动规则计算; "
            f"数据不足的窗口如实保留 None, 不补默认"
        ),
    }


def clear_cache() -> dict:
    """清空 biz_cache 异动相关 key (运维/手动刷新用)."""
    # 直接删前缀; biz_cache.clear 只清前缀"biz:"的 L2, 不会 flushdb 误伤限流
    biz_cache.clear()
    return {"cleared": True}


@router.get("")
def list_abnormal_moves(
    min_proximity: float = Query(0.5, ge=0.0, le=2.0, description="接近度阈值; 过滤 < 该值"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """扫描自选股 + 当日竞价异动池, proximity 倒序返回异动接近度面板.

    60s 进程内缓存(biz_cache L1+L2), 避免高频扫描拖慢 K 线源.
    """
    cache_key = CACHE_KEY_FMT.format(min_proximity=min_proximity)

    def _fetch():
        return _scan_impl(db, min_proximity)

    cached = biz_cache.get_or_fetch(cache_key, ttl=CACHE_TTL_S, fetch=_fetch)
    return cached


@router.post("/cache/clear")
def cache_clear(_: User = Depends(get_current_user)):
    """清空异动监控 60s 进程缓存."""
    return clear_cache()
