"""高开分档实证 API(v0.5.80 A7)。

GET /api/gap-study?days=250  ->  每个高开档位的实际后果(开盘买入 → 当日/次日)。

给盘前榜单贴徽标用: **只有样本够且前后半段同向的档位才带 verdict**, 其余一律
`insufficient` / `unstable`, 前端据此不贴徽标 —— 宁可不说, 不说错。

统计口径与限制见 `src/core/gap_study.py` docstring(尤其: 宇宙=库里已有日线的标的,
不是全 A 股; 开盘即涨停的样本已剔除, 因为那种交易买不进去)。

缓存 1 天(纯读库聚合, 一天算一次足够; key 不含用户 —— 结果与用户无关)。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from src.core.gap_study import run_study
from src.web.cache.biz_cache import biz_cache

router = APIRouter()
logger = logging.getLogger(__name__)

CACHE_TTL_S = 24 * 3600


@router.get("")
def get_gap_study(days: int = Query(250, ge=30, le=1000, description="回看自然日数(×2 折交易日窗口)")):
    """高开幅度 → 后续收益的分档统计。"""
    return biz_cache.get_or_fetch(
        f"gap_study:{days}", ttl=CACHE_TTL_S, fetch=lambda: run_study(days=days)
    )
