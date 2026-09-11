"""三指标共振 API(2026-09-11, 决策先锋升级)。

GET  /api/resonance/scan            全市场扫描结果(最近一次, only=all|resonance|near)
POST /api/resonance/scan/run        手动触发一次全市场扫描(后台线程; 盘后自动 15:40)
GET  /api/resonance/activity/{symbol}  机构活跃度历史序列(副图: 三线+共振级别)

判定口径与 /api/stock-pool 一致(共享 resonance_scan.resonance_level)。
"""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter()

_scan_lock = threading.Lock()
_scan_running = False


@router.get("/scan")
def get_scan(
    date: str | None = Query(None, description="YYYYMMDD, 默认最近一次"),
    only: str = Query("all", description="all / resonance / near"),
    limit: int = Query(200, ge=1, le=1000),
):
    """全市场共振扫描结果(读取落库, 不触发计算)。"""
    if only not in ("all", "resonance", "near"):
        raise HTTPException(400, "only 仅支持 all/resonance/near")
    from src.core import resonance_scan

    return resonance_scan.latest(date, only=only, limit=limit)


@router.post("/scan/run")
def run_scan(limit: int | None = Query(None, ge=1, le=6000)):
    """手动触发全市场扫描(后台执行, 完成后可从 GET /scan 读取)。"""
    global _scan_running
    with _scan_lock:
        if _scan_running:
            return {"started": False, "reason": "扫描进行中"}
        _scan_running = True

    def _runner() -> None:
        global _scan_running
        try:
            from src.core import resonance_scan

            out = resonance_scan.scan(limit=limit)
            logger.info("手动共振扫描完成: %s", out)
        except Exception as e:  # noqa: BLE001
            logger.warning("手动共振扫描失败: %s", e)
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=_runner, name="resonance-scan-manual", daemon=True).start()
    return {"started": True, "running": True}


@router.get("/scan/status")
def scan_status():
    with _scan_lock:
        return {"running": _scan_running}


@router.get("/activity/{symbol}")
def activity(symbol: str, days: int = Query(120, ge=30, le=250)):
    """机构活跃度历史序列(生命线 1.56 / 强势线 3 / 大牛线 6 + 共振级别)。"""
    code = (symbol or "").strip()
    if not code or not (code.isdigit() and len(code) == 6 or "." in code):
        raise HTTPException(400, f"非法代码: {symbol!r}")
    from src.core import resonance_scan

    return resonance_scan.activity_series(code, days=days)
