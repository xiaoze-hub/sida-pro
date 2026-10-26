# -*- coding: utf-8 -*-
"""I-8 分价矩阵**收盘归档**调度(2026-10-18)。

盘中快照(`price_distribution_snapshots`)只保留 5 个交易日, 是缓存; 真正给历史回测/
主力轨迹复盘用的是 `price_distribution_daily`(每票每日一份定稿)。

本模块把"归档"从手动接口变成**收盘自动动作**:
  · 工作日 15:05(收盘后 5 分钟, 收盘竞价已计入、当日分价表已定稿);
  · 标的池 = `get_default_symbols()`(自选 ∪ 持仓 ∪ 观察池, 与 K 线预缓存同源);
  · 每票: 回源一次(refresh=True) → `archive_daily` 覆盖写当日行;
  · 单票失败只记日志, 不中断整批(上游抖动不该让整天的归档归零)。

节流: 顺序执行 + 可配上限(`PRICE_DIST_ARCHIVE_LIMIT`, 默认 200 只), 避免收盘瞬间
对腾讯打出几百并发; 正常单票 ~0.3~1s, 200 只约 1~3 分钟, 远离次日开盘。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 单批归档上限(只), env 可配。
DEFAULT_LIMIT = 200
#: 相邻两票之间的间隔(秒): 对上游友好, 也给 DB 一点喘息。
THROTTLE_S = 0.15


def archive_symbols(
    symbols: Optional[List[Tuple[str, str]]] = None,
    *,
    limit: Optional[int] = None,
    refresh: bool = True,
    fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    session_factory=None,
    sleep_s: float = THROTTLE_S,
) -> Dict[str, Any]:
    """批量归档当日分价定稿。返回统计(成功/跳过/失败 + 失败原因样本)。

    `fetcher` / `session_factory` 注入供单测; 默认走读穿透 + 真实库。
    """
    from src.core.price_distribution_store import archive_daily, get_or_refresh
    from src.db.session import SessionLocal

    cap = int(limit if limit is not None else os.getenv("PRICE_DIST_ARCHIVE_LIMIT", DEFAULT_LIMIT))
    if symbols is None:
        from src.collectors.klines_ingestor import get_default_symbols

        symbols = get_default_symbols() or []
    targets = list(symbols)[: max(0, cap)]

    factory = session_factory or SessionLocal
    fetch = fetcher or get_or_refresh

    ok = skipped = 0
    errors: List[str] = []
    started = time.time()
    for sym, mkt in targets:
        try:
            analysis = fetch(sym, mkt or "CN", refresh=refresh, persist=True)
            if not analysis.get("available"):
                skipped += 1
                errors.append(f"{sym}: {analysis.get('reason') or '不可用'}")
                continue
            with factory() as session:
                if archive_daily(session, analysis):
                    ok += 1
                else:
                    skipped += 1
        except Exception as e:  # noqa: BLE001 - 单票失败不影响整批
            skipped += 1
            errors.append(f"{sym}: {e}")
            logger.warning(f"[price-dist-archive] {sym} 归档失败: {e}")
        if sleep_s:
            time.sleep(sleep_s)

    out = {
        "total": len(targets),
        "archived": ok,
        "skipped": skipped,
        "elapsed_s": round(time.time() - started, 1),
        "errors": errors[:20],   # 只留前 20 条, 免得日志/响应膨胀
    }
    logger.info(
        "[price-dist-archive] 完成: %(archived)s/%(total)s 归档, %(skipped)s 跳过, "
        "%(elapsed_s)s 秒",
        out,
    )
    return out


def register_archive_cron(scheduler) -> bool:
    """注册工作日 15:05 的收盘归档 job。调度器不可用返回 False(不崩启动)。"""
    if scheduler is None or not hasattr(scheduler, "add_job"):
        return False
    try:
        from apscheduler.triggers.cron import CronTrigger

        scheduler.add_job(
            archive_symbols,
            CronTrigger(day_of_week="mon-fri", hour=15, minute=5),
            id="price_distribution_archive",
            name="I-8 分价矩阵收盘归档",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )
        return True
    except Exception as e:  # noqa: BLE001 - 注册失败不该拖垮启动
        logger.error(f"分价矩阵归档 cron 注册失败: {e}")
        return False
