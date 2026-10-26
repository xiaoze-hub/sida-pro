"""TQ 情绪序列定时同步 —— **SC 云序列已弃用, 15:35 改为静默跳过(expected_gap)**。

历史: 每交易日 15:35 拉一次市场情绪日序列落 market_sentiment_daily。

2026-10-14 用户决策: **不购买通达信云数据 Key** ⇒ SC/GP 云函数(`get_scjy_value` /
`get_gpjy_value`)**永久放弃**(生产已断在 2026-09-23)。情绪序列改由
`src.core.sentiment_selfcalc` 用**客户端本地接口**自算(交易日 15:45, 见 startup.py)。

于是本调度器**不再打 TQ、不再记 ERROR、不再进入告警链** —— 每轮只返回
``{"skipped": "expected_gap"}`` 标记(explicit expected gap: 这是**预期内**的缺席,
不是故障)。保留调度器/registry 接线是为了兼容既有运维面板与"系统自检看调度器在不在跑"。

为什么"静默跳过"而不是删掉调度器
--------------------------------
- `GET /api/system/health` 一类的自检按 registry 判"调度器是否在跑"; 直接删会让
  `tq_sentiment` 从注册表消失 → 自检报"调度器缺失"这种**假故障**;
- 日后若重新拿到云数据, 只要把 ``SC_RETIRED`` 关掉(或把 SC 接成"可选增强源",
  见 `src.core.sentiment_freshness.sc_enhancement_enabled`)即可原地复活。

数据链现在是谁:
- 产者 = `src.core.sentiment_selfcalc`(本地自算, 15:45);
- 自检 = `src.core.sentiment_freshness`(16:20, 落后 → 本地自算自愈 + 显式告警)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

# 收盘后 15:35 — 留 5 分钟给交易所/客户端定稿
RUN_HOUR = 15
RUN_MINUTE = 35

# 首次启动(表为空/样本太少)时的回补天数(自然日)
BACKFILL_DAYS = 420
# 样本少于这个数就认为需要回补
MIN_SAMPLE_ROWS = 30

#: SC 云序列是否已退役(用户决策: 不购云 Key)。True = 本轮只打 expected_gap 标记。
SC_RETIRED = True

#: 静默跳过时返回的标记值(运维/作业面板据此区分"预期缺席"与"故障")。
EXPECTED_GAP = "expected_gap"

#: expected_gap 的人话原因(写日志/返回体的同一句话, 便于排查对齐)。
RETIRED_REASON = (
    "SC 云序列已弃用(用户决策: 不购通达信云数据 Key, 生产断在 2026-09-23); "
    "情绪序列改由本地自算提供(src.core.sentiment_selfcalc)"
)

_global_scheduler: "TqSentimentScheduler | None" = None


def _is_market_day() -> bool:
    """是否 A 股交易日(统一走 src.core.trading_calendar, 含法定节假日/调休)。

    ⚠️ 不要退回 `weekday() < 5`: 周中假日会被误判成交易日。
    """
    from src.core.trading_calendar import TradingCalendarError, is_trading_day

    try:
        return is_trading_day(datetime.now(ZoneInfo("Asia/Shanghai")).date())
    except TradingCalendarError:
        logger.warning("交易日历未覆盖当前年份, 本次按非交易日处理(请补表)")
        return False


def _needs_backfill(db) -> bool:
    from sqlalchemy import text

    try:
        n = db.execute(text("SELECT COUNT(*) FROM market_sentiment_daily")).scalar() or 0
    except Exception:  # noqa: BLE001 — 表还没建好(迁移未跑)时不回补
        return False
    return int(n) < MIN_SAMPLE_ROWS


def _sync_in_job(*, backfill: bool = False) -> dict:
    """SC 同步入口 —— 已退役: 显式 ``expected_gap`` 静默跳过(零网络, 零告警)。

    刻意**不**打 TQ: 旧行为每天打一次必然失败的云接口并记 ERROR, 属"空转告警",
    而这是**已知且已决策**的缺席, 不是故障。见模块 docstring。
    """
    if not SC_RETIRED:
        # 逃生口: 云数据恢复时把 SC_RETIRED 置 False, 走回原同步实现。
        from src.collectors.tq_sentiment_series import sync_sentiment_series
        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            out = sync_sentiment_series(
                db, days=BACKFILL_DAYS if backfill else 30, retries=2
            )
            if isinstance(out, dict) and out.get("error"):
                logger.error("[tq sentiment] 同步失败%s: %s", "(回补)" if backfill else "", out)
            else:
                logger.info("[tq sentiment] 同步完成%s: %s", "(回补)" if backfill else "", out)
            return out
        finally:
            db.close()

    logger.info(
        "[tq sentiment] %s%s: %s",
        EXPECTED_GAP,
        "(启动回补)" if backfill else "(每日 15:35)",
        RETIRED_REASON,
    )
    return {
        "skipped": EXPECTED_GAP,
        "backfill": bool(backfill),
        "reason": RETIRED_REASON,
        "source": "src.core.sentiment_selfcalc",
    }


class TqSentimentScheduler:
    """TQ 市场情绪日序列调度器。"""

    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self._running = False

    async def _job(self):
        if self._running:
            logger.warning("[tq sentiment] 上轮还在跑, 跳过本轮")
            return
        if not _is_market_day():
            return
        self._running = True
        try:
            import asyncio

            await asyncio.to_thread(_sync_in_job)
        except Exception as e:  # noqa: BLE001
            logger.error("[tq sentiment] 定时同步失败: %s", e)
        finally:
            self._running = False

    async def _startup_backfill(self):
        """启动时的历史回补 —— SC 已退役 ⇒ 直接 expected_gap 跳过(零网络)。

        即便表为空也不再回补: 云接口拿不到数据, 回补只会制造"启动即告警"。
        空表由 `sentiment_selfcalc`(15:45) 与 `sentiment_freshness`(16:20) 负责填。
        """
        if SC_RETIRED:
            logger.info("[tq sentiment] %s(启动回补): %s", EXPECTED_GAP, RETIRED_REASON)
            return
        try:
            import asyncio

            from src.db.session import SessionLocal

            def _check():
                db = SessionLocal()
                try:
                    return _needs_backfill(db)
                finally:
                    db.close()

            if await asyncio.to_thread(_check):
                logger.info("[tq sentiment] 样本不足, 启动回补 %s 天", BACKFILL_DAYS)
                await asyncio.to_thread(_sync_in_job, backfill=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("[tq sentiment] 启动回补跳过: %s", e)

    def start(self):
        global _global_scheduler
        _global_scheduler = self
        self.scheduler.add_job(
            self._job,
            "cron",
            day_of_week="mon-fri",
            hour=RUN_HOUR,
            minute=RUN_MINUTE,
            id="tq_sentiment_daily",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._startup_backfill,
            "date",
            run_date=datetime.now(),
            id="tq_sentiment_boot_backfill",
            replace_existing=True,
        )
        self.scheduler.start()
        try:
            from src.core.scheduler_registry import register

            register("tq_sentiment", self.scheduler)
        except Exception:  # noqa: BLE001 — 注册表缺失不阻断
            pass
        logger.info(
            "[tq sentiment] 调度器已启动: 每交易日 %02d:%02d", RUN_HOUR, RUN_MINUTE
        )

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass

    def trigger_now(self, *, backfill: bool = False) -> dict:
        """手动触发(管理界面/测试用)。"""
        return _sync_in_job(backfill=backfill)
