"""市场情绪日序列 API (2026-09-23)。

数据源: 通达信客户端 TQ 网关 get_scjy_value(SCJYVALUE 编号空间) → market_sentiment_daily。
端点(挂 /api/market/sentiment, 与 /api/market 同前缀不冲突):
    GET  /api/market/sentiment/baseline?days=20  → 最新一日情绪 + 近 N 交易日均值/分位
    GET  /api/market/sentiment/series?days=60    → 最近 N 个交易日宽表序列
    GET  /api/market/sentiment/health            → 表内样本量与覆盖区间(免登录自检)
    POST /api/market/sentiment/sync?backfill=0   → 手动触发拉取(默认回看 30 天)

为什么单独做: 东财/腾讯类免费源只有**当日快照**, 判断情绪周期需要
"今日 vs 近 20 日均值/分位"这种历史基线。TQ 序列自带完整历史, 落库一次永久可查。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

logger = logging.getLogger(__name__)

router = APIRouter()

# 序列返回的列(剔除内部字段), 顺序即列顺序
_SERIES_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "limit_up_count",
    "limit_up_open_count",
    "limit_down_count",
    "limit_down_open_count",
    "streak_count",
    "streak_count_ex",
    "hard_limit_up_count",
    "hard_limit_down_count",
    "seal_success_money",
    "seal_fail_money",
    "margin_fin_balance",
    "margin_sec_balance",
    "lhb_buy",
    "lhb_sell",
    "lhb_inst_buy",
    "lhb_inst_sell",
    "pboc_net_injection",
)


@router.get("/health")
def sentiment_health():
    """自检: 表内样本量/覆盖区间/最近同步时间。免登录, 方便探活。"""
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        row = db.execute(
            text(
                "SELECT COUNT(*) AS n, MIN(trade_date) AS first, MAX(trade_date) AS last, "
                "MAX(updated_at) AS updated FROM market_sentiment_daily WHERE market='CN'"
            )
        ).mappings().first()
        source = db.execute(
            text(
                "SELECT source FROM market_sentiment_daily WHERE market='CN' "
                "ORDER BY trade_date DESC LIMIT 1"
            )
        ).scalar()
    except Exception as e:  # noqa: BLE001 — 表未建/DB 异常都按不可用返回
        raise HTTPException(503, f"market_sentiment_daily 不可读: {e}")
    finally:
        db.close()

    n = int((row or {}).get("n") or 0)
    return {
        "available": n > 0,
        "rows": n,
        "first": (row or {}).get("first"),
        "last": (row or {}).get("last"),
        "updated_at": str((row or {}).get("updated") or ""),
        "source": source or "",
    }


@router.get("/baseline")
def sentiment_baseline(days: int = Query(20, ge=5, le=250)):
    """最新一日情绪 + 近 days 个交易日的均值/极值/分位。

    分位口径: pct_rank = 历史中低于当前值的比例(0=最低, 100=最高)。
    样本不足 5 天时 baseline 为空 dict —— 不假装有基线。
    """
    from src.web.database import SessionLocal
    from src.collectors.tq_sentiment_series import latest_with_baseline

    db = SessionLocal()
    try:
        out = latest_with_baseline(db, days=days)
    finally:
        db.close()
    if not out.get("latest"):
        raise HTTPException(404, "无情绪序列数据(先 POST /sync 或等收盘后定时任务)")
    return out


@router.get("/series")
def sentiment_series(days: int = Query(60, ge=1, le=500)):
    """最近 days 个交易日(按日期升序)。"""
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT * FROM market_sentiment_daily WHERE market='CN' "
                "ORDER BY trade_date DESC LIMIT :n"
            ),
            {"n": int(days)},
        ).mappings().all()
    finally:
        db.close()
    items = []
    for r in reversed(rows):  # 升序
        d = {c: r.get(c) for c in _SERIES_COLUMNS}
        d["trade_date_iso"] = (
            f"{d['trade_date'][:4]}-{d['trade_date'][4:6]}-{d['trade_date'][6:]}"
            if d.get("trade_date")
            else ""
        )
        items.append(d)
    return {"days": int(days), "count": len(items), "items": items}


@router.post("/sync")
def sentiment_sync(backfill: int = Query(0, ge=0, le=1)):
    """手动触发拉取。backfill=1 回补 420 天, 否则回看 30 天。"""
    from src.web.database import SessionLocal
    from src.collectors.tq_sentiment_series import sync_sentiment_series

    db = SessionLocal()
    try:
        out = sync_sentiment_series(db, days=420 if backfill else 30)
    finally:
        db.close()
    if "error" in out:
        raise HTTPException(502, out["error"])
    return out
