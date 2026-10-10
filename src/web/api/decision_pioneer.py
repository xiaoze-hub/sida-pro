"""数智决策三指标 API(盘中实时)。

GET /api/decision-pioneer/002361?market=CN
→ {symbol, market, institution_activity, gs, l2, main_intent, data_time}

进程内 30s 缓存(盘中多用户/多轮询防重复重算)。
权限(2026-09-15): view_forecast — member 3次/天试用, pro/owner 全开。

跨市场诚实降级(2026-10-10 P2-7): 三指标里的 L2 主力净流入/主力意图是 **CN 专有源**
(腾讯逐笔 + TQ get_more_info), 非 CN(HK/US)拿不到 → 由原来的裸 `400` 改为**结构化降级**:
HTTP 200 + `l2_supported=false` + `degraded=true` + 显式 `note`『仅 CN 支持 L2 主力』;
趋势/活跃度(K线可得)仍如实返回。**不编造资金、不静默 None**。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.core.decision_pioneer import fetch_decision_pioneer
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

# 进程内缓存: {key: (ts, data)}, TTL 30s
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 30.0

# 非 CN 降级说明(唯一出处, 主/历史端点共用)
L2_NON_CN_NOTE = "仅 CN 支持 L2 主力; 非 CN 已降级(无 L2 主力/主力意图, 趋势/活跃度可用)"


def _valid_symbol(raw: str, market: str = "CN") -> str:
    """按市场校验股票代码: CN=6位数字 / HK=5位数字 / 其余(US 等)字母数字放宽。"""
    code = (raw or "").strip()
    mkt = (market or "CN").upper()
    if mkt == "CN":
        ok = code.isdigit() and len(code) == 6
    elif mkt == "HK":
        ok = code.isdigit() and len(code) == 5
    else:  # US 等: 允许字母/数字/`.`/`-`(如 BRK.B / AAPL)
        ok = bool(code) and all(ch.isalnum() or ch in ".-" for ch in code)
    if not ok:
        raise HTTPException(400, f"非法股票代码: {raw!r}(市场 {mkt})")
    return code


def _get(symbol: str, market: str) -> dict:
    key = f"{market}:{symbol}"
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    data = fetch_decision_pioneer(symbol, market)
    _CACHE[key] = (now, data)
    # 09-03: 新鲜快照落库(历史回查用; best-effort, 失败不影响读链路)
    try:
        from src.core.history_store import record_dp_snapshot

        record_dp_snapshot(symbol, market, data)
    except Exception:  # noqa: BLE001
        pass
    return data


@router.get("/{symbol}/history")
def get_decision_pioneer_history(
    symbol: str,
    market: str = "CN",
    days: int = 30,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """数智决策历史快照(09-03 落库回查; 空=该股尚无落库, 不编造)。"""
    from src.core.permissions import PERM_VIEW_FORECAST, enforce_perm

    enforce_perm(user, PERM_VIEW_FORECAST, db)
    mkt = market.upper()
    code = _valid_symbol(symbol, mkt)
    if mkt != "CN":
        # 非 CN 无 L2 主力 → 结构化降级(显式), 不裸 400
        return {
            "symbol": code, "market": mkt, "rows": [],
            "l2_supported": False, "degraded": True, "note": L2_NON_CN_NOTE,
        }
    from src.core.history_store import query_dp_history

    return {"symbol": code, "market": mkt, "rows": query_dp_history(code, mkt, days)}


@router.get("/{symbol}")
def get_decision_pioneer(
    symbol: str,
    market: str = "CN",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """数智决策三指标 + L2 主力净流入 + 主力意图(盘中实时)。
    调用日志(2026-09-15 C.1): 每次调用落 high_value_api_logs。"""
    from src.core.permissions import PERM_VIEW_FORECAST, enforce_perm

    enforce_perm(user, PERM_VIEW_FORECAST, db)
    from src.core.hv_api_log import log_high_value_call

    log_high_value_call(db, user, "forecast", symbol)
    mkt = market.upper()
    code = _valid_symbol(symbol, mkt)
    if mkt != "CN":
        # 非 CN: L2 主力/主力意图无源 → 结构化降级(显式标注可用性), 不裸 400
        data: dict
        try:
            data = dict(fetch_decision_pioneer(code, mkt))
        except Exception as e:  # noqa: BLE001
            logger.warning("decision-pioneer 非 CN 取数 %s failed: %s", code, e)
            data = {"symbol": code, "market": mkt}
        data.setdefault("symbol", code)
        data.setdefault("market", mkt)
        data["l2_supported"] = False
        data["degraded"] = True
        data["note"] = L2_NON_CN_NOTE
        return data
    try:
        return _get(code, mkt)
    except Exception as e:  # noqa: BLE001
        logger.warning("decision-pioneer API %s failed: %s", code, e)
        raise HTTPException(502, f"数智决策数据获取失败: {e}") from e
