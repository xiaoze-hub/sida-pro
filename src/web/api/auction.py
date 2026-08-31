# -*- coding: utf-8 -*-
"""竞价快照轻接口(竞价快览卡 AuctionSnapshotCard 渲染用, 2026-08-19)。

端点:
    GET /api/auction-snapshot?symbol=USZA002361   (thsdk 代码格式)
    GET /api/auction-snapshot?symbol=002361       (6位A股代码, 自动归一化)

响应(经 app.py ResponseWrapperMiddleware 包装为 {code, success, data, message}):
    data = {
        "available": bool,                # 是否拿到真实竞价数据
        "direction": str,                 # 高开 / 低开 / 平开 / 无数据
        "gap_pct": float|None,            # 相对昨收涨跌幅 %
        "withdraw_rate_full": float|None, # 全程撤单率近似(0~1, 基于虚拟匹配量衰减)
        "auction_price": float|None,      # 竞价现价(= 开盘价, 09:25 撮合价)
        "prev_close": float|None,         # 昨收基准
        "trade_volume": float|None,       # 09:25 真实撮合量(股)
        "note": str,                      # 口径/失败原因说明
    }

实现: 复用 src.core.thsdk_alert 的竞价逻辑(auction_snapshot 算法 +
_fetch_tick_super_level1 / _fetch_prev_close 内部函数), 不复制算法。
thsdk 缺失 / 数据为空时 available=false 且 note 如实说明原因, 严禁伪造。
历史已收盘时若数据源仍能取当日竞价快照则照常返回真实值。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query


logger = logging.getLogger(__name__)

router = APIRouter()


def _normalize_symbol(raw: str) -> str:
    """6位A股代码 -> thsdk 代码(USZA/USHA/USTM); 已是 thsdk 格式原样返回。"""
    code = (raw or "").strip().upper()
    if code.startswith(("USZA", "USHA", "USTM")):
        return code
    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"非法股票代码: {raw!r}(需要6位A股代码或 USZA/USHA/USTM 格式)")
    if code.startswith(("60", "68")):
        return f"USHA{code}"
    if code.startswith(("00", "30")):
        return f"USZA{code}"
    if code.startswith(("8", "4")):
        return f"USTM{code}"
    raise ValueError(f"无法识别市场: {raw!r}")


def _empty(prev_close: Optional[float], note: str) -> dict:
    """统一空结构(available=false, 不伪造数据)。"""
    return {
        "available": False,
        "direction": "无数据",
        "gap_pct": None,
        "withdraw_rate_full": None,
        "auction_price": None,
        "prev_close": prev_close,
        "trade_volume": None,
        "note": note,
    }


def get_auction_snapshot(
    symbol: str,
    date: Optional[str] = None,
    prev_close: Optional[float] = None,
) -> dict:
    """竞价快照(无 FastAPI 依赖的纯函数, 供 endpoint 与测试直接调用)。

    复用 thsdk_alert 内部竞价逻辑: THS 上下文拉 tick_super_level1(支持历史
    date=YYYYMMDD)与昨收, 再跑 auction_snapshot 分析。
    thsdk 缺失 / 数据为空 -> available=false + 真实 note, 不伪造。
    """
    try:
        tsym = _normalize_symbol(symbol)
    except ValueError as exc:
        return _empty(None, f"参数错误: {exc}")

    try:
        from src.core import thsdk_alert
    except ImportError:
        return _empty(None, "thsdk 不可用(L2 竞价数据源未接入), 无法获取竞价快照")

    try:
        with thsdk_alert.THS() as ths:
            tick_rows = thsdk_alert._fetch_tick_super_level1(ths, tsym, date)
            if prev_close is None:
                prev_close = thsdk_alert._fetch_prev_close(ths, tsym)
    except Exception as exc:  # noqa: BLE001 - thsdk 上下文异常统一降级
        logger.warning("[auction] thsdk 上下文异常 %s: %r", tsym, exc)
        return _empty(prev_close, f"thsdk 调用异常: {exc!r}(竞价快照不可用, 未伪造)")

    snap = thsdk_alert.auction_snapshot(tick_rows, prev_close)

    if not tick_rows:
        return _empty(
            snap.get("prev_close"),
            (snap.get("note") or "无 tick_super_level1 数据(非交易时段或查询失败)")
            + "; thsdk 未返回数据, 未伪造",
        )
    if not snap.get("auction_price"):
        return _empty(
            snap.get("prev_close"),
            (snap.get("note") or "竞价时段(09:15-09:25)无有效快照行") + "; 未伪造",
        )

    return {
        "available": True,
        "direction": snap.get("direction") or "无数据",
        "gap_pct": snap.get("gap_pct"),
        "withdraw_rate_full": snap.get("withdraw_rate_full"),
        "auction_price": snap.get("auction_price"),
        "prev_close": snap.get("prev_close"),
        "trade_volume": snap.get("trade_volume"),
        "note": snap.get("note", ""),
    }


@router.get("")
def auction_snapshot_endpoint(
    symbol: str = Query(..., description="thsdk 代码(USZA002361)或6位A股代码(002361)"),
):
    """竞价快照(轻接口, 分时卡片渲染用)。"""
    return get_auction_snapshot(symbol)
