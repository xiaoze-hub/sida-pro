"""同花顺账号 API(2026-09-05 精简: 扫码登录整条线已下掉, 只留账号摘要)。

端点:
    GET /api/ths/account  SDK 模式(正式/游客) + 已验证能力一览

背景: SDK 只认账号密码(THS_USERNAME/PASSWORD), 扫码通行证 session
无任何数据链路消费, 前后端引用已清零(src/core/ths_auth.py 已删)。
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ths"])


@router.get("/account")
async def ths_account():
    """同花顺账号维护摘要: SDK 模式(正式/游客) + 已验证能力。

    mode 判定与 THSDKL2 一致: THS_USERNAME/PASSWORD 有无。
    capabilities 为 2026-09-05 生产实测结论(非实时探测, 实时探测走 /datasources)。
    """
    try:
        from data_source.thsdk_l2 import resolve_ths_creds

        _u, _p, src = resolve_ths_creds()
        formal = bool(_u and _p)
    except Exception:
        src = "env"
        formal = bool(os.environ.get("THS_USERNAME") and os.environ.get("THS_PASSWORD"))
    return {
        "mode": "formal" if formal else "guest",
        "mode_label": "正式账户" if formal else "游客模式",
        "source": src,
        "capabilities": [
            {"key": "dde_official", "label": "DDE官方分档(query_data)", "ok": True},
            {"key": "ext1", "label": "扩展1主力净流入", "ok": formal},
            {"key": "orderbook20", "label": "20档盘口", "ok": True},
            {"key": "l2ticks", "label": "L2逐笔", "ok": True},
            {"key": "auction", "label": "竞价异动", "ok": True},
            {"key": "bigorders", "label": "大单流向", "ok": formal},
            {"key": "hk_us", "label": "港美行情", "ok": formal},
        ],
        "note": "游客模式下扩展1/港美/大单返0行, 需正式账户解锁",
    }
