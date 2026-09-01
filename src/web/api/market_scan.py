# -*- coding: utf-8 -*-
"""全市场三榜扫描 API(设计稿 §6.1, 2026-09-01 接线)。

把 `src/core/market_scan.scan()`(孤儿代码) 接进生产:
  - GET  /api/market-scan/ranks   读最新三榜快照(盘后 cron 落库)
  - POST /api/market-scan/refresh 手动触发扫描(同步, 全市场约 60s)

## ⚠️ 为什么拆成「读快照」和「触发扫描」两个端点

scan() 全市场 5000 只逐只算 GS+活跃度+暗盘, 实测约 60s(设计稿 §6.1),
不能做成同步 GET(会拖垮反代)。因此:
  - 盘后 15:30 cron(report_scheduler) 跑 scan() 落库到 market_scan_ranks
  - 前端只读快照(GET ranks), 需要新鲜数据才手动 POST refresh

## 诚实口径

- 暗盘 TOP 榜是 OHLC 分摊**对照项**, 每条带 approximation=True(scan() 已硬标记),
  这里原样透传, 不冒充真实暗盘意图(真实暗盘 = .tck 委托号级, 见 postmarket_review)
- 无快照 → 返回 {available: false, note}, 不编造榜单
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.web.database import get_db
from src.web.models import MarketScanRank

logger = logging.getLogger(__name__)

router = APIRouter()

# 存量库兜底建表(生产启动会 create_all, 但老库升级/测试环境可能没有该表)。
# 用 ORM 的 __table__.create(checkfirst=True) 跨 SQLite/PG 通用, 无需手写 DDL。
_rank_table_ready = False


def _ensure_rank_table() -> None:
    global _rank_table_ready
    if _rank_table_ready:
        return
    try:
        from src.web.database import engine

        MarketScanRank.__table__.create(bind=engine, checkfirst=True)
        _rank_table_ready = True
    except Exception as e:  # noqa: BLE001
        logger.debug("market_scan_ranks 兜底建表失败(可能已由 create_all 建): %s", e)


_ensure_rank_table()


class RefreshRequest(BaseModel):
    symbols: list[str] | None = None   # 限池扫描(逗号或列表); None=全市场
    top_n: int = 20
    bars_days: int = 60
    dark_days: int = 1
    with_zljc: bool = True


def _latest_rank(db: Session, market: str = "CN"):
    return (
        db.query(MarketScanRank)
        .filter(MarketScanRank.stock_market == market)
        .order_by(MarketScanRank.snapshot_date.desc(), MarketScanRank.id.desc())
        .first()
    )


@router.get("/ranks")
def get_market_scan_ranks(market: str = "CN", db: Session = Depends(get_db)):
    """读最新三榜快照(盘后 cron 落库; 无快照 → available=false)。"""
    row = _latest_rank(db, market)
    if not row:
        return {
            "available": False,
            "note": "暂无三榜快照(盘后 15:30 cron 落库, 或 POST /refresh 手动触发)",
        }
    payload = row.payload if isinstance(row.payload, dict) else {}
    return {
        "available": True,
        "snapshot_date": row.snapshot_date,
        "market": row.stock_market,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        **payload,
    }


@router.post("/refresh")
def refresh_market_scan(req: RefreshRequest, db: Session = Depends(get_db)):
    """手动触发三榜扫描(同步, 全市场约 60s)。

    限池: 传 symbols(6 位代码列表)可快速测试/局部刷新。
    落库: 按 (snapshot_date, market) upsert 到 market_scan_ranks。
    """
    from src.core.market_scan import scan

    try:
        result = scan(
            symbols=req.symbols,
            top_n=req.top_n,
            bars_days=req.bars_days,
            dark_days=req.dark_days,
            with_zljc=req.with_zljc,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("market-scan refresh 失败: %s", e)
        raise HTTPException(status_code=500, detail=f"扫描失败: {e}") from e

    # 落库快照(upsert by snapshot_date + market)
    snap = datetime.now().strftime("%Y-%m-%d")
    try:
        row = (
            db.query(MarketScanRank)
            .filter(
                MarketScanRank.snapshot_date == snap,
                MarketScanRank.stock_market == "CN",
            )
            .first()
        )
        if row:
            row.payload = result
        else:
            db.add(MarketScanRank(snapshot_date=snap, stock_market="CN", payload=result))
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning("三榜快照落库失败: %s", e)

    result["snapshot_date"] = snap
    result["available"] = True
    return result


def run_market_scan_job() -> dict:
    """盘后 cron 入口(report_scheduler 调用): 扫描 + 落库, 失败不抛。

    与 POST /refresh 同逻辑, 但不用 FastAPI 依赖, 内部自开 DB session。
    """
    from src.core.market_scan import scan
    from src.web.database import SessionLocal

    try:
        result = scan()
    except Exception as e:  # noqa: BLE001
        logger.exception("盘后三榜扫描失败: %s", e)
        return {"ok": False, "error": str(e)}

    snap = datetime.now().strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        row = (
            db.query(MarketScanRank)
            .filter(
                MarketScanRank.snapshot_date == snap,
                MarketScanRank.stock_market == "CN",
            )
            .first()
        )
        if row:
            row.payload = result
        else:
            db.add(MarketScanRank(snapshot_date=snap, stock_market="CN", payload=result))
        db.commit()
        return {
            "ok": True,
            "universe": result.get("universe"),
            "computed": result.get("computed"),
            "skipped": result.get("skipped"),
            "new_g": len(result.get("new_g_points") or []),
            "dark_top": len(result.get("dark_top") or []),
            "activity_top": len(result.get("activity_top") or []),
        }
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("三榜快照落库失败: %s", e)
        return {"ok": False, "error": f"落库失败: {e}"}
    finally:
        db.close()
