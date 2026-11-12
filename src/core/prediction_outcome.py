from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from src.collectors.kline_collector import KlineCollector
from src.core.context_store import (
    count_ghost_user_prediction_outcomes,
    count_system_pending_prediction_outcomes,
    count_unowned_pending_prediction_outcomes,
    list_pending_prediction_outcomes,
    mark_agent_prediction_outcome,
)
from src.models.market import MarketCode

logger = logging.getLogger(__name__)


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            continue
    return None


def _to_market(value: str | None) -> MarketCode:
    try:
        return MarketCode((value or "CN").strip().upper())
    except Exception:
        return MarketCode.CN


def _pick_close_on_or_before(klines: list, target: date) -> float | None:
    if not klines:
        return None
    rows: list[tuple[date, float]] = []
    for k in klines:
        d = _parse_day(getattr(k, "date", None))
        c = getattr(k, "close", None)
        if d is None or c is None:
            continue
        try:
            rows.append((d, float(c)))
        except Exception:
            continue
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    for d, c in reversed(rows):
        if d <= target:
            return c
    return None


def _add_trading_days(start: date, n: int) -> date:
    """从 start 起算 n 个 A股交易日, 返回第 n 个交易日。

    S6(2026-08-23): 旧实现仅按 weekday()<5 计数, 忽略法定节假日和调休补班,
    导致回测命中率和预测评估窗口错位。改走 src.core.trading_calendar 工具,
    该工具内置 2025-2027 法定节假日 + 调休补班静态表, 与上交所公告口径对齐。

    n<=0 时返回 start 本身。
    """
    from src.core.trading_calendar import add_trading_days as _calendar_add

    if n <= 0:
        return start
    return _calendar_add(start, n)


def evaluate_pending_prediction_outcomes(
    *,
    max_horizon_days: int = 10,
    limit: int = 300,
    user_id: str | None = None,
) -> dict:
    """评估到期的个人预测后验 —— 只碰**归属可证明的计分行**（2026-11-12 二次修正）。

    - 历史行(user_id/owner_scope 均为 NULL): 不评估(改不了"属于谁"), 但如实报出被排除量;
    - ghost 行(user_id 指向已不存在的用户): 同样不评估 —— 那等于给一个不存在的账号计分;
      条数在 `skipped_ghost_user` 里如实报出(不清理、不改写);
    - system 桶: 独立统计口径, 这里不评估, 单独报数(绝不并入任何个人成绩);
    - superseded 审计版本: 不是计分行, 不会出现在待评估列表里(存储层已排除);
    - `user_id` 可选: 只评估该用户的行(供按用户范围触发的维护调用)。
    """
    pending = list_pending_prediction_outcomes(
        max_horizon_days=max_horizon_days,
        limit=limit,
        user_id=user_id,
    )
    # 无归属的历史待评估行: 不评估(改不了"属于谁"), 但必须如实报出被排除的量,
    # 不能让人以为"待评估为 0 = 都评估完了"。
    unowned = count_unowned_pending_prediction_outcomes(max_horizon_days=max_horizon_days)
    system_pending = count_system_pending_prediction_outcomes(
        max_horizon_days=max_horizon_days
    )
    ghost = count_ghost_user_prediction_outcomes(max_horizon_days=max_horizon_days)
    uid = str(user_id).strip() if user_id is not None else ""
    stats = {
        "total_pending": len(pending),
        "eligible": 0,
        "evaluated": 0,
        "skipped_not_due": 0,
        "skipped_invalid_date": 0,
        "skipped_no_price": 0,
        "skipped_unowned_legacy": unowned,
        "skipped_system_bucket": system_pending,
        "skipped_ghost_user": ghost,
        "scope": "owned_only",
        "user_scope": uid or "all_user_scoped_rows",
        "note": (
            "只评估有归属用户的计分行; 历史无归属行(user_id 为空)保持 pending 不作成绩; "
            "ghost 行(user_id 指向已不存在用户)不评估、不清理; "
            "system 桶单独计数、不并入任何个人成绩; superseded 审计版本不评估"
        ),
    }
    if unowned:
        logger.warning(
            "预测后验评估跳过 %s 条无归属历史行(user_id 为空): 无法证明属于哪个账户, "
            "不评估、不回填, 需人工核查数据来源",
            unowned,
        )
    if ghost:
        logger.error(
            "预测后验评估跳过 %s 条 ghost 行(user_id 指向已不存在的用户): 归属不可证明, "
            "不评估、不清理、不改写; 需人工核查这些行是怎么写进去的(旧库缺 user_id 外键)",
            ghost,
        )
    if system_pending:
        logger.info(
            "预测后验评估跳过 %s 条 system 桶待评估行: 系统运行无个人归属, 独立统计, "
            "不并入任何账户成绩",
            system_pending,
        )
    if not pending:
        return stats

    today = date.today()
    kline_cache: dict[tuple[str, str], list] = {}

    for rec in pending:
        pred_day = _parse_day(rec.prediction_date)
        if pred_day is None:
            stats["skipped_invalid_date"] += 1
            continue

        horizon = max(1, int(rec.horizon_days or 1))
        # 交易日口径: 从 pred_day 起算 horizon 个工作日(与回测引擎一致)
        target_day = _add_trading_days(pred_day, horizon)
        if target_day > today:
            stats["skipped_not_due"] += 1
            continue

        stats["eligible"] += 1
        market = _to_market(rec.stock_market)
        cache_key = (rec.stock_symbol, market.value)
        if cache_key not in kline_cache:
            lookback_days = max(120, (today - pred_day).days + 30)
            try:
                kline_cache[cache_key] = KlineCollector(market).get_klines(
                    rec.stock_symbol,
                    days=min(lookback_days, 600),
                )
            except Exception as e:
                logger.warning(
                    "评估建议获取K线失败: %s %s - %s",
                    rec.stock_symbol,
                    market.value,
                    e,
                )
                kline_cache[cache_key] = []

        klines = kline_cache[cache_key]
        outcome_price = _pick_close_on_or_before(klines, target_day)
        if outcome_price is None:
            stats["skipped_no_price"] += 1
            continue

        base_price = None
        if rec.trigger_price is not None and rec.trigger_price > 0:
            try:
                base_price = float(rec.trigger_price)
            except Exception:
                base_price = None
        if base_price is None:
            base_price = _pick_close_on_or_before(klines, pred_day)

        if base_price is None or base_price <= 0:
            ok = mark_agent_prediction_outcome(
                record_id=rec.id,
                outcome_price=outcome_price,
                outcome_return_pct=None,
                status="no_base_price",
            )
        else:
            outcome_ret = (outcome_price - base_price) / base_price * 100
            ok = mark_agent_prediction_outcome(
                record_id=rec.id,
                outcome_price=outcome_price,
                outcome_return_pct=outcome_ret,
                status="evaluated",
            )
        if ok:
            stats["evaluated"] += 1

    return stats
