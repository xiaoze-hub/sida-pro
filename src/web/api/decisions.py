"""决策日志 API(B6, 2026-09-18) —— "信号 → 结果"的账, 只读。

两个端点:
- `GET /api/decisions/stats` —— 按信号类型统计 T+1/3/5 命中率;
  **样本不足 (`n < min_sample`) 时不给命中率**, 返回 `insufficient=true`(页面显示"样本不足")。
- `GET /api/decisions/log`    —— 最近的信号明细(含当时价格/上下文快照与回填状态)。

口径: 缺价格 / 未回填一律 NULL(不补 0), 平盘记未命中 —— 本端点只透传, 不在 API 层做任何推算。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from src.web.api.auth import get_current_user
from src.web.models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["decisions"])


@router.get("/stats")
def decisions_stats(
    days: int = Query(180, ge=7, le=730, description="回看天数(按信号产生日)"),
    min_sample: int = Query(30, ge=1, le=500, description="低于此样本量不给命中率"),
    _: User = Depends(get_current_user),
):
    """按信号类型统计命中率(样本不足则如实说"样本不足", 不拿小样本算百分比)。"""
    from src.core.decision_log import stats
    from src.db.session import get_read_engine

    return stats(get_read_engine(), days=days, min_sample=min_sample)


@router.get("/log")
def decisions_log(
    kind: str | None = Query(None, description="按信号类型过滤, 如 resonance3"),
    limit: int = Query(100, ge=1, le=500),
    _: User = Depends(get_current_user),
):
    """最近的信号明细: 当时价格 + 上下文快照 + T+1/3/5 回填状态(未回填=None)。"""
    from src.db.session import get_read_engine

    sql = (
        "SELECT signal_kind, symbol, trade_date, price_at_signal, context_json, source, "
        "ret_t1, hit_t1, ret_t3, hit_t3, ret_t5, hit_t5, filled_at "
        "FROM decision_log "
        + ("WHERE signal_kind = :kind " if kind else "")
        + "ORDER BY trade_date DESC, id DESC LIMIT :lim"
    )
    params: dict[str, object] = {"lim": int(limit)}
    if kind:
        params["kind"] = kind

    with get_read_engine().connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    items = [
        {
            "signal_kind": r[0],
            "symbol": r[1],
            "trade_date": r[2],
            "price_at_signal": None if r[3] is None else float(r[3]),
            "context": r[4] or "",
            "source": r[5] or "",
            "outcomes": {
                label: {
                    "ret": None if r[i] is None else float(r[i]),
                    "hit": None if r[i + 1] is None else bool(r[i + 1]),
                }
                for label, i in (("t1", 6), ("t3", 8), ("t5", 10))
            },
            "filled_at": None if r[12] is None else str(r[12]),
        }
        for r in rows
    ]
    return {
        "count": len(items),
        "items": items,
        "note": "未回填的档位为 null(需要未来的 K 线才算得出来), 不用推算值填充; 命中 = 收益 > 0(平盘算未命中)。",
    }
