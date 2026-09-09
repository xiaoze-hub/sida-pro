"""盘后扫描 cron 入口(KI-039 第二阶段, 2026-09-09)。

从 `src/web/api/market_scan.py` 下沉: 这两个函数是 **scheduler 入口**(report_scheduler
调用), 不是 HTTP 端点 —— core 不应反向依赖 web。落库走中立层 `src.db`。

- `run_market_scan_job`: 全市场三榜扫描 + 落 market_scan_ranks
- `run_dark_fund_top_job`: 暗盘资金 TOP 扫描 + 落 dark_fund_top_snapshots

两者失败都不抛(返回 {"ok": False, "error": ...}), 不打断调度器。
"""

from __future__ import annotations

import logging
from datetime import datetime

from src.db.models import DarkFundTopSnapshot, MarketScanRank
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)


def run_market_scan_job() -> dict:
    """盘后 cron 入口(report_scheduler 调用): 扫描 + 落库, 失败不抛。

    与 POST /refresh 同逻辑, 但不用 FastAPI 依赖, 内部自开 DB session。
    """
    from src.core.market_scan import scan
    from src.db.session import SessionLocal

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


def run_dark_fund_top_job() -> dict:
    """盘后 cron 入口: 扫描 + 落库(内部自开 session, 失败不抛)。"""
    from src.core.dark_fund_scan import scan_dark_fund_top
    from src.db.session import SessionLocal

    try:
        result = scan_dark_fund_top()
    except Exception as e:  # noqa: BLE001
        logger.exception("盘后暗盘资金 TOP 扫描失败: %s", e)
        return {"ok": False, "error": str(e)}

    snap = datetime.now().strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        row = (
            db.query(DarkFundTopSnapshot)
            .filter(
                DarkFundTopSnapshot.snapshot_date == snap,
                DarkFundTopSnapshot.stock_market == "CN",
            )
            .first()
        )
        if row:
            row.payload = result
        else:
            db.add(DarkFundTopSnapshot(snapshot_date=snap, stock_market="CN", payload=result))
        db.commit()
        return {
            "ok": True,
            "universe": result.get("universe"),
            "computed": result.get("computed"),
            "top": len(result.get("top") or []),
        }
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("暗盘资金 TOP 快照落库失败: %s", e)
