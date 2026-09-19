"""因子 IC 每日快照(时序落库) —— B9 数据资产 E1 收口(2026-09-19)。

## 为什么需要
`factor_eval.evaluate_factor_ic()` 一直是**即时算**: 每次用同一段窗口重算一遍, 结果不留。
于是能回答"这段时间哪些因子有效", 但回答不了**"某个因子的 IC 随时间怎么变、什么时候失效"** ——
后者才是调权、判断因子衰减、以及给归因报告提供原料的前提。

## 三条口径
1. **样本不足也落行**: `ic` 等为 NULL 且记下 `ic_periods` —— 留下"这天算不出来(样本不够)"的痕迹。
   留空洞分不清"没跑"和"没法算", 表里少一行会让人以为漏采。
2. **幂等**: 唯一键 `(factor_code, market, trade_date, horizon)` —— 当天重跑只更新不重复插,
   所以补跑/重跑安全。
3. **不补 0**: 任何指标取不到就 NULL; `0` 是"真的没相关性", 与"没数据"是两件事。

## 调度
由 `register_daily_job` 注册(默认交易日 17:25, 收盘后 —— outcome 需要当日 K 线已落库)。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from src.db.dialect import upsert_sql

logger = logging.getLogger(__name__)

TABLE = "factor_ic_snapshots"

#: 落库的指标列(与 factor_eval 输出一一对应; 缺的键一律 NULL, 不补 0)
_METRIC_COLS = (
    "ic", "ic_t", "ic_std", "ir", "ic_holdout", "ic_pooled",
    "sample_size", "ic_periods", "holdout_periods",
)

_INSERT_COLS = ("factor_code", "market", "trade_date", "horizon", *_METRIC_COLS)
_CONFLICT_COLS = ("factor_code", "market", "trade_date", "horizon")
_UPDATE_COLS = _METRIC_COLS


def snapshot_ic(
    *,
    market: str = "CN",
    horizon: int = 5,
    days: int = 90,
    holdout_ratio: float = 0.3,
    today: date | None = None,
    db=None,
) -> dict[str, Any]:
    """算一次 IC 并把**每个因子**落成当天一行(幂等)。

    `today` 可注入(默认取 UTC 日期): 测试与补跑都要能指定"这是哪一天" ——
    锚 `date.today()` 的代码在 CI(UTC)与本地(CST)会落到不同日期, 这是踩过的坑。
    """
    from src.core.factor_eval import evaluate_factor_ic

    trade_date = (today or datetime.now(timezone.utc).date()).strftime("%Y-%m-%d")
    resp = evaluate_factor_ic(days=days, horizon=horizon, market=market or None,
                              holdout_ratio=holdout_ratio, db=db)
    factors: dict[str, dict] = resp.get("factors") or {}
    if resp.get("error"):
        # 计算失败: **一行都不写** —— 落一堆 NULL 会被误读成"这天样本不足"
        return {"trade_date": trade_date, "market": market, "horizon": horizon,
                "written": 0, "factors": 0, "error": str(resp["error"])}
    if not factors:
        return {"trade_date": trade_date, "market": market, "horizon": horizon,
                "written": 0, "factors": 0, "note": "该区间没有可评估的因子快照"}

    own = db is None
    if own:
        from src.db.session import SessionLocal

        db = SessionLocal()
    try:
        sql = text(upsert_sql(TABLE, _INSERT_COLS, _CONFLICT_COLS, _UPDATE_COLS))
        insufficient = 0
        for code, m in factors.items():
            params: dict[str, Any] = {
                "factor_code": str(code), "market": market, "trade_date": trade_date,
                "horizon": int(horizon),
            }
            for col in _METRIC_COLS:
                params[col] = m.get(col)      # 取不到 → None(NULL), 绝不填 0
            if m.get("ic") is None:
                insufficient += 1
            db.execute(sql, params)
        db.commit()
        return {
            "trade_date": trade_date, "market": market, "horizon": horizon,
            "written": len(factors), "factors": len(factors),
            "insufficient": insufficient,   # 其中"样本不足、IC 为 NULL"的因子数
            "days": days,
        }
    finally:
        if own:
            db.close()


def history(
    *,
    market: str = "CN",
    horizon: int = 5,
    days: int = 30,
    factor_code: str | None = None,
    db=None,
) -> list[dict[str, Any]]:
    """取 IC 时序(按日期升序; 一次可取多个因子, 便于前端画每行小趋势)。"""
    own = db is None
    if own:
        from src.db.session import SessionLocal

        db = SessionLocal()
    try:
        q = (
            "SELECT factor_code, trade_date, ic, ic_holdout, ir, sample_size, ic_periods "
            f"FROM {TABLE} WHERE market = :market AND horizon = :horizon"
        )
        params: dict[str, Any] = {"market": market, "horizon": int(horizon)}
        if factor_code:
            q += " AND factor_code = :factor_code"
            params["factor_code"] = factor_code
        q += " ORDER BY trade_date DESC LIMIT :limit"
        params["limit"] = max(1, int(days)) * (50 if not factor_code else 5)
        rows = db.execute(text(q), params).mappings().all()
    finally:
        if own:
            db.close()
    out = [dict(r) for r in rows]
    out.reverse()   # 升序给前端用(时间从左到右)
    return out


def register_daily_job(scheduler, hour: int = 17, minute: int = 25) -> None:
    """注册每日 IC 快照 job(收盘后: outcome 依赖当日 K 线已落库)。"""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        _daily_run,
        CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
        id="factor_ic_snapshot",
        name="因子 IC 每日快照",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )
    logger.info("[factor-ic] 已注册每日 IC 快照 job(%02d:%02d, 周一至周五)", hour, minute)


def _daily_run() -> None:
    """job 包一层: 任何异常都不能掀翻调度器(只告警)。"""
    try:
        for horizon in (1, 5, 10):
            r = snapshot_ic(horizon=horizon)
            logger.info("[factor-ic] 快照完成 %s", r)
    except Exception as e:  # noqa: BLE001
        logger.warning("[factor-ic] 每日快照失败: %s", e)
