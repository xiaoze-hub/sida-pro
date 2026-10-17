"""可转债 - 正股联动套利雷达 API(I-4 初版, 2026-10-18)。

GET /api/cb-radar/snapshot?limit=60&refresh=1
    → { available, data_date, trade_phase, is_live, as_of, caliber,
        universe, evaluated, rows[], scenario_counts, note }

GET /api/cb-radar/bond/{code}
    → 单只转债: 条款 + 债/股报价 + 全部指标 + 场景

设计要点(对应审计 §4.5 / A-08):
- **零新增数据源**: 全部来自通达信 TQ(`get_stock_list list_type=32` 转债码表 +
  `get_pricevol` 批量价量 + `get_kzz_info` 条款)。
- **条款原值直用**: 转股价 / 强赎触发价 / 纯债价值不做二次贴现或估算
  —— 一旦与客户端数字不同, 又回到 A-06 的信任问题。
- **冷启动不挂在请求上**: 条款带 12h 进程内缓存, 响应再经 `biz_cache` 跨进程缓存;
  Top-N(按|涨跌幅|)只算用户真会看的几十只, 避免 A-02 式 N+1。
- **数据日/交易阶段显式标注**(A-03 同一铁律): 非交易时段打开必须看到"数据日 + 快照",
  不许拿浏览器时钟冒充实时。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Query

from src.core.cb_radar import DEFAULT_LIMIT, MAX_LIMIT, build_one, build_radar
from src.web.cache.biz_cache import biz_cache

logger = logging.getLogger(__name__)

router = APIRouter()

_CACHE_KEY = "cb_radar:snapshot"
#: 盘中 60s(转债/正股都在动), 非盘中 600s(条款与收盘价都不变)。
_TTL_LIVE = 60
_TTL_IDLE = 600


def _ttl() -> int:
    from src.core.trade_phase import PHASE_AUCTION, PHASE_AUCTION_MATCH, PHASE_CONTINUOUS, phase_meta

    return (
        _TTL_LIVE
        if phase_meta()["trade_phase"] in (PHASE_AUCTION, PHASE_AUCTION_MATCH, PHASE_CONTINUOUS)
        else _TTL_IDLE
    )


@router.get("/snapshot")
def snapshot(
    limit: int = Query(DEFAULT_LIMIT, ge=5, le=MAX_LIMIT, description="参与计算的转债数(按|涨跌幅|取 Top-N)"),
    refresh: bool = Query(False, description="true=绕过缓存实时重算(运维/调试用)"),
):
    """转债联动雷达快照。数据源不可用时 `available=false` + `note`, 绝不伪造行。"""
    key = f"{_CACHE_KEY}:{limit}"
    if not refresh:
        cached = biz_cache.get_json(key)
        if cached and cached.get("available"):
            age = int(time.time() - float(cached.get("_cached_at") or 0))
            body = {k: v for k, v in cached.items() if not k.startswith("_")}
            return {**body, "cached": True, "age_s": max(0, age)}

    try:
        payload = build_radar(limit=limit)
    except Exception as e:  # noqa: BLE001 - 接口不许 500, 如实降级
        logger.error("[cb-radar] 快照计算失败: %r", e)
        return {
            "available": False,
            "rows": [],
            "scenario_counts": {},
            "note": f"转债雷达计算失败: {type(e).__name__}: {e}",
        }

    if payload.get("available") and payload.get("rows"):
        # 只缓存可用载荷: 单次抖动不得被钉住整个 TTL
        biz_cache.set_json(key, {**payload, "_cached_at": time.time()}, ttl=_ttl())
    return {**payload, "cached": False, "age_s": 0}


@router.get("/bond/{code}")
def bond_detail(code: str):
    """单只转债详情(条款 + 折溢价 + 强赎距离 + 场景)。"""
    try:
        return build_one(code)
    except Exception as e:  # noqa: BLE001
        logger.error("[cb-radar] 单券计算失败 %s: %r", code, e)
        return {
            "available": False,
            "code": code,
            "note": f"转债详情计算失败: {type(e).__name__}: {e}",
        }
