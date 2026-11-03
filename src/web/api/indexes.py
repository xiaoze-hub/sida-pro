"""盘面指数总览 API（2026-11-03）。

两个端点：
- `GET /api/indexes/overview`：八个宽基指数的实时涨跌/成交额/四档资金/涨跌家数。
  优先 TQ 实时（`src.core.index_flow`），失败回落库内当日最后一行并标 `stale`。
- `GET /api/indexes/{symbol}/history?days=N`：日序列（资金流与涨跌幅的历史分位）。

口径：`caliber=tdx_index_agg`（成分股按单笔金额四档聚合）。**与东财/同花顺的
"主力净流入"不可换算**，只可同向比较；响应里带 caliber 供前端标注。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.core.index_codes import BOARD_ORDER, index_name
from src.web.api.auth import get_current_user
from src.web.cache.biz_cache import biz_cache
from src.web.database import get_db
from src.web.models import IndexFlowDaily, User

logger = logging.getLogger(__name__)

router = APIRouter()

CACHE_TTL_S = 15
HISTORY_MAX_DAYS = 500

_FIELDS = (
    "symbol", "name", "trade_date", "close", "pct_chg", "amount", "volume_lots",
    "open", "high", "low", "up_home", "down_home", "outside", "inside",
    "xl_net", "lg_net", "md_net", "sm_net", "main_net", "net_all",
    "active_buy", "active_sell", "caliber", "quality", "reason", "source",
)


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    out = {k: item.get(k) for k in _FIELDS}
    out["symbol"] = str(out["symbol"] or "")[:6]
    out.setdefault("name", index_name(out["symbol"]))
    for k in ("amount", "main_net", "net_all", "xl_net", "lg_net", "md_net", "sm_net",
              "active_buy", "active_sell"):
        v = out.get(k)
        out[k] = round(v, 2) if isinstance(v, (int, float)) else None
    return out


def _live() -> dict[str, Any]:
    """实时取数（TQ）。异常不外抛：整块失败也要让调用方拿到库内兜底。"""
    from src.core import index_flow

    try:
        items = index_flow.fetch_many(list(BOARD_ORDER))
    except Exception as exc:  # noqa: BLE001
        logger.warning("index overview 实时取数失败: %r", exc)
        return {"ok": False, "items": []}
    ok_items = [it for it in items if it.get("available")]
    return {"ok": bool(ok_items), "items": items}


def _from_db(db: Session, trade_date: str | None = None) -> list[dict[str, Any]]:
    """库内兜底：优先给定交易日，否则取每个指数最近一行。"""
    q = db.query(IndexFlowDaily)
    if trade_date:
        rows = q.filter(IndexFlowDaily.trade_date == trade_date).all()
    else:
        latest = (
            db.query(IndexFlowDaily.trade_date)
            .order_by(IndexFlowDaily.trade_date.desc())
            .limit(1)
            .scalar()
        )
        rows = q.filter(IndexFlowDaily.trade_date == latest).all() if latest else []
    order = {s: i for i, s in enumerate(BOARD_ORDER)}
    return sorted(
        (_normalize({k: getattr(r, k, None) for k in _FIELDS}) for r in rows),
        key=lambda x: order.get(x["symbol"], 99),
    )


@router.get("/overview")
def overview(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
    live: bool = Query(True, description="是否走 TQ 实时（false 只读库）"),
) -> dict[str, Any]:
    """八个宽基指数盘面总览；`stale=true` 表示实时取数失败、用的是库内数据。"""
    cache_key = f"indexes:overview:{int(live)}"
    if not live:
        cached = biz_cache.get_json(cache_key)
        if cached is not None:
            return cached

    payload: dict[str, Any] = {"items": [], "stale": True, "as_of": None, "caliber": "tdx_index_agg"}
    if live:
        res = biz_cache.get_or_fetch(cache_key, CACHE_TTL_S, _live)
        if isinstance(res, dict) and res.get("ok"):
            items = [_normalize({**it, "trade_date": it.get("trade_date")}) for it in res["items"]
                     if it.get("available")]
            payload["items"] = items
            payload["stale"] = False
            payload["as_of"] = items[0].get("trade_date") if items else None

    if not payload["items"]:
        rows = _from_db(db)
        payload["items"] = rows
        payload["stale"] = True
        payload["as_of"] = rows[0].get("trade_date") if rows else None
        if not rows:
            payload["reason"] = "无数据：TQ 不可达且库内当日无记录"

    for it in payload["items"]:
        it["name"] = it.get("name") or index_name(it["symbol"])
    return payload


@router.get("/{symbol}/history")
def history(
    symbol: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
    days: int = Query(90, ge=5, le=HISTORY_MAX_DAYS),
) -> dict[str, Any]:
    """单指数日序列（按日期升序）；无记录返回空数组而不是报错。"""
    sym = str(symbol or "").strip()[:6]
    if not sym.isdigit():
        raise HTTPException(status_code=400, detail="指数代码应为 6 位数字")
    rows = (
        db.query(IndexFlowDaily)
        .filter(IndexFlowDaily.symbol == sym)
        .order_by(IndexFlowDaily.trade_date.desc())
        .limit(days)
        .all()
    )
    items = [
        {
            "trade_date": r.trade_date,
            "close": r.close,
            "pct_chg": r.pct_chg,
            "amount": r.amount,
            "main_net": r.main_net,
            "net_all": r.net_all,
            "xl_net": r.xl_net,
            "lg_net": r.lg_net,
            "md_net": r.md_net,
            "sm_net": r.sm_net,
            "up_home": r.up_home,
            "down_home": r.down_home,
            "quality": r.quality,
        }
        for r in reversed(rows)
    ]
    return {"symbol": sym, "name": index_name(sym), "caliber": "tdx_index_agg", "items": items}
