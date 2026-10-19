"""src/web/api/copilot.py

AI 数智参谋（Copilot）API 路由。
提供 /api/copilot/brief/{symbol} 与 /api/copilot/presets。
契约源: docs/research/AI数智参谋全链路落地设计方案.md §4
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, Query

from src.core.copilot_brief import build_copilot_brief
from src.core.trade_phase import phase_meta
from src.web.api.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot"])

_TTL = {"intraday": 20, "closed": 300, "non_trading": 1800}

# 简易进程内缓存与单飞锁
_cache: dict[str, tuple[float, int, dict]] = {}
_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()


def _get_flight_lock(key: str) -> threading.Lock:
    with _global_lock:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


@router.get("/brief/{symbol}")
def get_copilot_brief(
    symbol: str,
    market: str = Query("CN", pattern="^(CN|HK|US)$"),
    refresh: bool = Query(False, description="true=跳过进程缓存"),
    user: Any = Depends(get_current_user),
) -> dict:
    meta = phase_meta()
    phase = meta.get("trade_phase", "closed")
    is_live = bool(meta.get("is_live", False))

    ttl = _TTL["intraday"] if is_live else (_TTL["closed"] if phase == "closed" else _TTL["non_trading"])
    cache_key = f"{symbol}:{market}:{phase}"

    if not refresh and cache_key in _cache:
        ts, hit_ttl, payload = _cache[cache_key]
        if time.time() - ts < hit_ttl:
            res = dict(payload)
            res["cache"] = {"hit": True, "ttl_s": hit_ttl, "key": cache_key}
            return res

    lock = _get_flight_lock(cache_key)
    with lock:
        # 双重检查
        if not refresh and cache_key in _cache:
            ts, hit_ttl, payload = _cache[cache_key]
            if time.time() - ts < hit_ttl:
                res = dict(payload)
                res["cache"] = {"hit": True, "ttl_s": hit_ttl, "key": cache_key}
                return res

        brief = build_copilot_brief(symbol, market=market)
        # 若不是全降级，写缓存
        evidence_used = brief.get("verdict", {}).get("evidence_used", [])
        if len(evidence_used) >= 1:
            _cache[cache_key] = (time.time(), ttl, brief)

        res = dict(brief)
        res["cache"] = {"hit": False, "ttl_s": ttl, "key": cache_key}
        return res


@router.get("/presets")
def get_copilot_presets(
    user: Any = Depends(get_current_user),
) -> list[dict]:
    return [
        {
            "id": "full_dossier",
            "icon": "🧬",
            "label": "全景数智深度诊断(多维Harness)",
            "prompt": "请调用标的全景档案工具，对我当前查看的股票进行全面深度分析：整合实时量价、K线形态、腾讯逐笔主力意图、120日底仓筹码沉淀、数智决策GS多空色带、东方财富机构研报、近期公司公告与利好/利空消息面，给出明确结论与实战作战预案。",
            "group": "dossier",
            "requires_perm": None,
        },
        {
            "id": "auction_strong",
            "icon": "🔥",
            "label": "扫描今日 9:25 爆量抢筹",
            "prompt": "扫描今天集合竞价里爆量抢筹(竞价金额>2000万且量比>2.5)的股票，按竞价金额降序给出前 10 只，并标注每只的五态判定与竞价撤单率。",
            "group": "auction",
            "requires_perm": "view_auction",
        },
        {
            "id": "chip_cost",
            "icon": "🎯",
            "label": "诊断持仓股底仓成本与获利盘",
            "prompt": "用我的持仓列表，逐只给出底仓成本(COST50)、70%筹码区间与当前获利盘比例，标出已跌破 70% 筹码下沿的票。",
            "group": "chip",
            "requires_perm": None,
        },
        {
            "id": "resonance_first",
            "icon": "🛡️",
            "label": "筛三指标首次共振股",
            "prompt": "筛出今天三指标首次共振(🔥首次共振·机会挖掘)的股票，并按机构活跃度降序排列，说明每只的 GS 区间与 L2 主力净额。",
            "group": "resonance",
            "requires_perm": "view_forecast",
        },
        {
            "id": "demon_mainline",
            "icon": "👑",
            "label": "检索主线中的妖股候选",
            "prompt": "找出当前主线 Top5 板块里、同时进入妖股因子库 Top50 的个股，给出因子分、分级与连板身位。",
            "group": "demon",
            "requires_perm": "view_opportunities",
        },
        {
            "id": "defense_check",
            "icon": "🧯",
            "label": "检查持仓的防守位是否破位",
            "prompt": "对每只持仓，给出第一防守位与核心生命线(70%筹码下沿)，并明确哪些已经破位、哪些还在上方。",
            "group": "defense",
            "requires_perm": None,
        },
    ]
