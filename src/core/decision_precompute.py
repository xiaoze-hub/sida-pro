"""决策合成批算调度(2026-10-10 冗余设计): 自选/持仓标的**盘后批算落库**。

## 为什么需要
`/api/decision/{symbol}` 是实时计算端点(拉 120 天 K 线 + 明暗盘资金现算, 慢)。工作台
「研究」标签首屏就发这条请求。盘后对**被持有/被自选**的标的批算一次落库, 让盘后与次日
盘前的浏览直接命中缓存(免重算); 未被预热的标的仍走端上短 TTL(见 decision_cache)。

## 口径
- 预热的标的集 = **全库**自选(Stock)+ 持仓(经 Stock 关联)的并集 —— 全局基底与用户无关,
  故取并集(不按 user 隔离; 这是"为所有用户预热公共基底", 不泄露任何用户维度)。
- 写入 source='precompute', TTL = 到**下一个工作日开盘**(见 decision_cache.precompute_ttl_s)
  ⇒ 开盘后自动过期, 端上重算, 不用隔夜基底冒充盘中结论。
- 单标的失败**不拖垮整批**(记录到 errors, 继续下一只); 缺库表/异常一律吞掉不抛。

## 调度
`DecisionPrecomputeScheduler` 每工作日 15:45(收盘后, 与 K 线入库/TQ 类错开)跑一次。
"""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

RUN_HOUR = 15
RUN_MINUTE = 45

_global_scheduler: "DecisionPrecomputeScheduler | None" = None


def collect_targets(db) -> list[tuple[str, str]]:
    """预热标的集(全库自选 ∪ 持仓): 去重的 (symbol, market) 列表。读失败 → 空。"""
    try:
        from src.db.models import Stock

        rows = db.query(Stock.symbol, Stock.market).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("[decision-precompute] 目标集读取失败: %s", e)
        return []
    seen: set[tuple[str, str]] = set()
    for symbol, market in rows:
        code = str(symbol or "").strip()
        if not code:
            continue
        seen.add((code, (market or "CN").upper()))
    return sorted(seen)


def precompute(
    *,
    db=None,
    symbols: list[tuple[str, str]] | None = None,
    days: int = 120,
    limit: int | None = None,
) -> dict:
    """批算并落库决策基底。返回统计 {total, computed, failed, skipped, errors, ttl_s}。

    symbols 显式给出则不查库(测试/运维用); 否则从 Stock 表取全库自选∪持仓。
    单标的失败只记录不抛; limit 截断(防一次性过多)。
    """
    from src.core.decision_cache import (
        SOURCE_PRECOMPUTE,
        compute_base,
        precompute_ttl_s,
        put_cached_decision,
    )

    owns_db = False
    if symbols is None:
        if db is None:
            from src.db.session import SessionLocal

            db = SessionLocal()
            owns_db = True
        try:
            symbols = collect_targets(db)
        finally:
            if owns_db:
                db.close()

    targets = list(symbols or [])
    if limit is not None and limit >= 0:
        targets = targets[:limit]

    ttl = precompute_ttl_s()
    out = {"total": len(targets), "computed": 0, "failed": 0, "skipped": 0,
           "errors": [], "ttl_s": ttl}
    for symbol, market in targets:
        try:
            base = compute_base(symbol, market, days)
            ok = put_cached_decision(
                symbol, market, base, ttl_s=ttl, source=SOURCE_PRECOMPUTE
            )
            if ok:
                out["computed"] += 1
            else:
                out["skipped"] += 1
        except Exception as e:  # noqa: BLE001 — 单标的失败不拖垮整批
            out["failed"] += 1
            if len(out["errors"]) < 20:
                out["errors"].append(f"{symbol}:{market}:{type(e).__name__}")
            logger.debug("[decision-precompute] %s.%s 失败: %s", symbol, market, e)
    logger.info("[decision-precompute] 完成: %s", {k: out[k] for k in ("total", "computed", "failed", "skipped")})
    return out


class DecisionPrecomputeScheduler:
    """每工作日 15:45 盘后批算决策基底(后台线程调度)。"""

    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.scheduler = BackgroundScheduler(timezone=timezone)

    def _job(self) -> None:
        try:
            from src.db.session import SessionLocal

            db = SessionLocal()
            try:
                precompute(db=db)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[decision-precompute] 批算任务失败: %s", exc)

    def start(self) -> None:
        global _global_scheduler
        _global_scheduler = self
        self.scheduler.add_job(
            self._job, "cron", day_of_week="mon-fri",
            hour=RUN_HOUR, minute=RUN_MINUTE,
            id="decision_precompute", replace_existing=True,
            coalesce=True, max_instances=1,
        )
        try:
            from src.core.scheduler_registry import register

            register("decision_precompute", self.scheduler)
        except Exception:  # noqa: BLE001 — CLI 无调度场景优雅跳过
            pass
        self.scheduler.start()
        logger.info("决策预落库调度器已启动(工作日 %02d:%02d)", RUN_HOUR, RUN_MINUTE)


    def shutdown(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


# 供 startup 直接 add_job 的薄包装(与 demon_factors 同法, 无需 scheduler 实例)
def run_precompute_job() -> None:
    """一次性批算入口(供 app 调度 add_job 调用)。失败静默(只记日志)。"""
    try:
        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            precompute(db=db)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[decision-precompute] 手动触发批算失败: %s", exc)
