"""每日单位对账 job(风险整改 3.5/B5)。

抽样 N 只股票拉**不复权**日K(东财 fqt=0, 带成交额 f57), 跑 vol×price≈amt
恒等式(src/core/unit_check.assert_unit_consistency, tol 实测校准 5%), 超阈
记 error 日志 + 数据源失败计数 + 写入当日对账报告 JSON。

调度: 挂在 KlineBackfillScheduler 同一 AsyncIOScheduler 上(18:35 收盘后,
交易日), 见 kline_backfill_scheduler._RECON_CRON。也可一次性手跑:
``python -c "from src.core.unit_recon import run_unit_reconciliation; print(run_unit_reconciliation(8))"``
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

RECON_DAYS = 20  # 每股抽最近 20 根不复权日K
_FALLBACK_POOL = [
    ("600519", "CN"), ("000858", "CN"), ("601318", "CN"), ("000333", "CN"),
    ("300750", "CN"), ("600030", "CN"), ("000651", "CN"), ("601012", "CN"),
]


def _sample_pool(n: int) -> list[tuple[str, str]]:
    """优先 watchlist+候选池实时抽样, 库不可用退固定流动池(对账不能因库挂而失效)。"""
    try:
        from src.collectors.klines_ingestor import get_default_symbols

        pool = [s for s in get_default_symbols() if s[1] == "CN"]
        if pool:
            return pool[:n]
    except Exception as e:  # noqa: BLE001
        logger.warning("[单位对账] 默认股票池不可用, 退固定池: %r", e)
    return _FALLBACK_POOL[:n]


def _fetch_unadjusted_rows(symbol: str, days: int) -> list[list[str]]:
    """东财不复权日K原始行(f51-f57: date,open,close,high,low,volume手,amount元)。"""
    from marketdata.http import market_get

    secid = ("1." if symbol.startswith(("6", "9")) else "0.") + symbol
    payload = market_get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        host_key="push2his.eastmoney.com", min_interval_s=0.2,
        params={"secid": secid, "klt": "101", "fqt": "0",
                "lmt": str(days), "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
                "ut": "fa5fd1943c7b386f172d6893dbfba10b"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        timeout=12, retries=1, parse="json", log_label="单位对账", symbol=symbol,
    )
    return (((payload or {}).get("data") or {}).get("klines") or []) if isinstance(payload, dict) else []


def run_unit_reconciliation(sample_n: int = 20) -> dict[str, Any]:
    """跑一次对账并落报告。返回摘要 dict(供调度日志/测试断言)。fail-soft:
    单股失败只记警告, 不影响整体报告产出。"""
    from src.core.unit_check import UnitInconsistencyError, assert_unit_consistency

    pool = _sample_pool(sample_n)
    checked = 0
    skipped = 0
    violations: list[dict[str, Any]] = []
    max_dev = 0.0
    max_dev_at: dict[str, Any] | None = None
    tol = _tol()
    t0 = time.time()

    for symbol, _market in pool:
        try:
            rows = _fetch_unadjusted_rows(symbol, RECON_DAYS)
        except Exception as e:  # noqa: BLE001
            logger.warning("[单位对账] %s 拉取失败: %r", symbol, e)
            skipped += 1
            continue
        for row in rows:
            p = str(row).split(",")
            if len(p) < 7:
                continue
            try:
                close, vol, amt = float(p[2]), float(p[5]) * 100.0, float(p[6])
            except ValueError:
                skipped += 1
                continue
            dev: float | None
            try:
                # strict 模式逐柱抛错(fail-closed); 对账捕获后继续跑完样本
                dev = assert_unit_consistency(
                    symbol=symbol, trade_date=p[0], volume=vol, close=close,
                    amount=amt, source="eastmoney_push2his_unadj",
                )
            except UnitInconsistencyError as e:
                dev = e.dev_pct
            if dev is None:
                skipped += 1
                continue
            checked += 1
            if dev > tol:
                violations.append({"symbol": symbol, "date": p[0], "dev_pct": round(dev, 4)})
            if dev > max_dev:
                max_dev = dev
                max_dev_at = {"symbol": symbol, "date": p[0], "dev_pct": round(dev, 4)}

    report = {
        "date": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_pool": [s for s, _ in pool],
        "sample_n": len(pool),
        "bars_checked": checked,
        "bars_skipped": skipped,
        "violations": violations,
        "max_dev_pct": round(max_dev, 4),
        "max_dev_at": max_dev_at,
        "tol_pct": _tol(),
        "elapsed_s": round(time.time() - t0, 1),
    }
    _write_report(report)
    if violations:
        logger.error("[单位对账] 发现 %d 根超阈柱, 详见 %s", len(violations), report.get("report_path"))
    logger.info(
        "[单位对账] 完成: %d 股 %d 柱, max dev %.2f%%, 违规 %d, 耗时 %.1fs",
        len(pool), checked, max_dev, len(violations), report["elapsed_s"],
    )
    return report


def _tol() -> float:
    from src.core.unit_check import _tol_from_env

    return _tol_from_env()


def _write_report(report: dict[str, Any]) -> None:
    """报告落 DATA_DIR/reports/unit_recon/YYYY-MM-DD.json(W2.2/E4: DATA_DIR 唯一口径)。"""
    data_dir = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
    out_dir = os.path.join(data_dir, "reports", "unit_recon")
    path = os.path.join(out_dir, f"{report['date']}.json")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        report["report_path"] = path
    except Exception as e:  # noqa: BLE001
        logger.warning("[单位对账] 报告写入失败 %s: %r", path, e)
