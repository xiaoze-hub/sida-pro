"""thsdk 深度扩展 API(v0.3.1 选项B):代码补齐 + 市场代码表。

    GET /api/thsdk/ext/code/{code}      证券代码补齐为标准 THS 代码(支持逗号分隔多只)
    GET /api/thsdk/ext/market/{market}  市场代码全量列表(代码 + 名称)

D5+D6(2026-09-09): 原 GET /api/thsdk/ext/dde/{symbol} 重复端点已删除,
DDE 一律走 /api/thsdk/dde/{symbol}(src/web/api/thsdk_extended.py, 扁平化
响应 + 30s TTL 缓存 + 口径文档)。

底层包装 data_source.thsdk_l2 的 complete_ths_code/market_block 能力,
复用其限频/重试/熔断,并在此层加进程内 TTL 缓存避免重复拉 thsdk。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from src.web.api.auth import get_current_user

logger = logging.getLogger(__name__)

# 模块层属性优先 + lazy import 兜底(测试 monkeypatch 可替换)
try:
    from data_source.thsdk_l2 import (  # noqa: F401
        complete_ths_code,
        get_market_codes,
    )
except Exception:  # noqa: BLE001
    complete_ths_code = None  # type: ignore[assignment]
    get_market_codes = None  # type: ignore[assignment]

router = APIRouter()

# 进程内 TTL 缓存,30s 过期
_TTL = 30.0
_CODE_CACHE: dict = {}
_MKT_CACHE: dict = {}

# 允许的市场前缀(避免任意字符串打到后端)
_VALID_MARKETS = {
    "USHA", "USZA", "USTM", "UNQQ", "UNQS", "UFXB", "USHI", "UHKG", "UEUA", "UFHB",
}


def _cached(cache: dict, key: str, fn) -> dict:
    """TTL 缓存包装。"""
    now = time.time()
    hit = cache.get(key)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]
    out = fn()
    cache[key] = (now, out)
    return out


def _resolve(name: str) -> Any:
    """返回模块层函数(测试 mock 用优先级高于 lazy import)。"""
    fn = globals().get(name)
    if fn is not None:
        return fn
    if name == "complete_ths_code":
        from data_source.thsdk_l2 import complete_ths_code as _f
    else:
        from data_source.thsdk_l2 import get_market_codes as _f
    return _f


# D5+D6(2026-09-09): 原 /dde/{symbol} 路由已删 —— DDE 唯一入口是
# /api/thsdk/dde/{symbol}(thsdk_extended.py), 见模块 docstring。


@router.get("/code/{code}")
def code_norm(code: str, user=Depends(get_current_user)) -> dict:
    """证券代码补齐为标准 THS 代码(支持逗号分隔多只)。"""
    codes = [c.strip() for c in code.split(",") if c.strip()]
    if not codes:
        raise HTTPException(400, "code 不能为空")

    def _fetch() -> list:
        return _resolve("complete_ths_code")(codes)

    try:
        ths_codes = _cached(_CODE_CACHE, ",".join(codes), _fetch)
        return {
            "input": codes,
            "ths_codes": ths_codes,
            "count": len(ths_codes),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"thsdk complete_ths_code 失败 {code}: {e}", exc_info=True)
        raise HTTPException(502, f"thsdk 代码补齐失败: {e}") from e


@router.get("/market/{market}")
def market_list(market: str, user=Depends(get_current_user)) -> dict:
    """市场代码全量列表(代码 + 名称)。"""
    market = market.strip().upper()
    if market not in _VALID_MARKETS:
        raise HTTPException(400, f"无效市场: {market}, 可选 {sorted(_VALID_MARKETS)}")

    def _fetch() -> dict:
        df = _resolve("get_market_codes")(market)
        return {
            "count": len(df),
            "rows": df.to_dict("records") if len(df) else [],
        }

    try:
        return _cached(_MKT_CACHE, market, _fetch)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"thsdk market_block 失败 {market}: {e}", exc_info=True)
        raise HTTPException(502, f"thsdk 市场列表失败: {e}") from e
