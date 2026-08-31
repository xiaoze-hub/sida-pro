"""主力意图双源对比 API(腾讯逐笔 vs thsdk L2, v0.5.0)。

GET /api/main-flow/compare/{symbol}  → 双源主力净额 + 一致性(0-100) + delta_pct
  + notes(各路可用性)
GET /api/main-flow/cache/clear       → 清空进程内缓存(运维/手动刷新)

prefix /api/main-flow 已确认不与现有路由冲突。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from src.core.main_flow_compare import clear_cache, compare_main_flow

logger = logging.getLogger(__name__)

router = APIRouter()


def _validate_symbol(raw: str) -> str:
    code = (raw or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, f"非法股票代码: {raw!r}(需要6位A股代码)")
    return code


@router.get("/compare/{symbol}")
def compare(symbol: str) -> dict:
    """主力意图双源对比(腾讯逐笔 vs thsdk L2)。symbol 为 6 位 A 股代码。

    响应结构:
      {
        "symbol", "tencent", "thsdk",
        "consistency", "delta_pct",
        "note", "notes"
      }
    任一源失败 -> 该源 None + notes 说明, 至少一路成功就返回。
    """
    code = _validate_symbol(symbol)
    return compare_main_flow(code)


@router.post("/cache/clear")
def clear() -> dict:
    """清空 main_flow_compare 进程内缓存(运维用)。"""
    clear_cache()
    return {"cleared": True, "sources": ["tencent", "thsdk"]}
