"""I-3 委托行为画像 API: 撤单率历史基线 + 动态 Z-Score 坐标系.

`GET /api/market/cancel-profile/{symbol}`（router 挂载前缀 `/api/market`）

为什么不复用 `/api/market-data/fundamentals-detail` 的 `exday` 字段:
那边只出**最新一日**的标量（`exday_latest`, count=1）, 而本接口要的是**多日序列** +
统计量（中位数/标准差/分位/z）+ 一句话结论。序列在服务端算完, 前端不重算口径 ——
否则同一份数据两处算法, 迟早分叉。

降级诚实: 网关不可用/无数据 → HTTP 200 + `available=false` + `reason` 原文, series 为空;
绝不返回估算值或 0 填充（0 是真实值, 不能顶替缺失）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

from src.core.cancel_order_intent import calc_cancel_order_intent
from src.core.cancel_profile import DEFAULT_WINDOW, MIN_BASELINE_DAYS, fetch_cancel_profile

logger = logging.getLogger(__name__)

router = APIRouter()


def _degraded(reason: str, symbol: str, days: int) -> Dict[str, Any]:
    """网关层失败时的完整降级响应（形状与正常响应一致, 前端只走一条渲染路径）。"""
    return {
        "available": False,
        "reason": reason,
        "symbol": symbol,
        "window": days,
        "baseline_days": 0,
        "min_baseline_days": MIN_BASELINE_DAYS,
        "series": [],
        "today": None,
        "buy": None,
        "sell": None,
        "verdict": {
            "code": "insufficient",
            "label": "无数据",
            "level": "unknown",
            "abnormal": None,
            "desc": reason,
        },
        "conclusion": reason,
        "intent": None,
        "as_of": None,
    }


@router.get("/cancel-profile/{symbol}")
async def get_cancel_profile(
    symbol: str,
    market: str = Query("CN", description="市场, 仅 A 股(TQ 日线统计)支持"),
    days: int = Query(DEFAULT_WINDOW, ge=MIN_BASELINE_DAYS, le=250,
                      description="基线窗口(交易日), 不含今日"),
    pct_chg: Optional[float] = Query(None, description="当日涨跌幅(%), 参与诱多双条件判定"),
) -> Dict[str, Any]:
    """个股委托行为画像: 60 日撤单率时序 + 基线中位数/标准差 + 今日 z / 分位 + 结论.

    TQ `get_exday_data` 是单标的 RPC(毫秒级), 本接口不落缓存: 今日撤单量在盘中持续累加,
    缓存会把「盘中还在变的值」冻结成一份看似新鲜的旧值。
    """
    sym = (symbol or "").strip()
    if not sym:
        return _degraded("缺少标的代码", sym, days)

    try:
        profile = await asyncio.to_thread(
            fetch_cancel_profile, sym, days=days, pct_chg=pct_chg, market=market
        )
    except Exception as e:  # noqa: BLE001 - 网关异常不外抛, 降级为可读原因
        logger.warning(f"委托行为画像[{sym}]取数失败: {e}")
        profile = _degraded(f"TQ 日线统计不可用: {e}", sym, days)

    # 用同一份今日日线统计跑一次微观意图判定 —— 有基线时走相对 z 口径,
    # 这正是 I-3 对 cancel_order_intent 的升级（固定 40% 阈值不再单独定性）。
    # 口径提醒: exday 的 BOrder/SOrder 映射到意图引擎的 TotalBVol/TotalSVol,
    # 与基线撤单率同源（日内 get_more_info 的会话累计值同定义, 收盘后两者一致）。
    today = profile.get("today") or {}
    buy = profile.get("buy") or {}
    intent = None
    if profile.get("available") and today.get("b_cancel") is not None:
        more_info = {
            "BCancel": today.get("b_cancel"),
            "SCancel": today.get("s_cancel"),
            "TotalBVol": today.get("b_order"),
            "TotalSVol": today.get("s_order"),
        }
        baseline = buy if buy.get("n") else None
        intent = calc_cancel_order_intent(more_info, pct_chg=pct_chg, baseline=baseline)

    out = dict(profile)
    out["symbol"] = sym
    out["intent"] = intent
    return out
