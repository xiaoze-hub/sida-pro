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

    评分闸门(2026-11-13, fail-closed): 计分键唯一性只由 m250 的四条部分唯一索引保证。
    评估路径**只检查、不 DDL**(业务角色没有建/删索引权限, 也不该在评估时反复锁热表);
    索引缺失/定义不符时直接返回 `scoring_blocked=True` 的统计骨架, **一条都不回填** ——
    宁可显示"评分阻断", 也不输出可能双计的命中率。

    批量性能(2026-11-13): 整个批次复用**同一个会话**、闸门只在批次边界**检查一次**,
    不按行新建连接(避免连接池耗尽)。批次读取完成后先结束只读事务再抓 K 线, 不在网络
    IO 期间占着池连接。不跨批次缓存闸门结论。
    """
    from src.core import context_store as _cs

    db = _cs.SessionLocal()
    try:
        return _evaluate_pending_with_session(
            db, max_horizon_days=max_horizon_days, limit=limit, user_id=user_id
        )
    finally:
        db.close()


def _evaluate_pending_with_session(db, *, max_horizon_days: int, limit: int, user_id: str | None) -> dict:
    from src.core.scoring_guard import check_scoring_health

    # 只读检查: 运行期任何情况下都不在这里建/删索引。
    health = check_scoring_health(bind=db.connection(), allow_repair=False)
    if health.blocked:
        logger.error(
            "预测后验评估被评分闸门阻断(%s): %s", health.reason_code, health.detail
        )
        # 检查失败可能把只读事务打进 aborted 状态; 先结束它再读欠评估条数。
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 — 回滚失败不影响"阻断"结论
            pass
        # 只读地报出"欠着多少条"(不评估、不改写), 免得把阻断误读成"没有待评估数据"
        try:
            backlog = len(
                list_pending_prediction_outcomes(
                    max_horizon_days=max_horizon_days,
                    limit=limit,
                    user_id=user_id,
                    db=db,
                )
            )
        except Exception as exc:  # noqa: BLE001 — 连读取都失败时如实报 -1, 不假装是 0
            logger.warning("评分阻断下读取待评估条数失败: %s", type(exc).__name__)
            backlog = -1
        return {
            "scoring_blocked": True,
            "scoring_repaired": list(health.repaired),
            "scoring_status": health.as_payload(),
            "total_pending": backlog,
            "eligible": 0,
            "evaluated": 0,
            "skipped_not_due": 0,
            "skipped_invalid_date": 0,
            "skipped_no_price": 0,
            "skipped_unowned_legacy": 0,
            "skipped_system_bucket": 0,
            "skipped_ghost_user": 0,
            "scope": "blocked",
            "user_scope": (
                str(user_id).strip() if user_id is not None else "all_user_scoped_rows"
            ),
            "note": (
                "评分阻断: " + health.detail + " —— 本次不评估、不回填、不计分; "
                "运行期只检查、不做任何 DDL, 数据一行未改"
            ),
        }

    try:
        pending = list_pending_prediction_outcomes(
            max_horizon_days=max_horizon_days,
            limit=limit,
            user_id=user_id,
            db=db,
        )
        # 无归属的历史待评估行: 不评估(改不了"属于谁"), 但必须如实报出被排除的量,
        # 不能让人以为"待评估为 0 = 都评估完了"。
        unowned = count_unowned_pending_prediction_outcomes(
            max_horizon_days=max_horizon_days, db=db
        )
        system_pending = count_system_pending_prediction_outcomes(
            max_horizon_days=max_horizon_days, db=db
        )
        ghost = count_ghost_user_prediction_outcomes(
            max_horizon_days=max_horizon_days, db=db
        )
        # 把本批次要用的行字段拷成纯数据: 随后结束只读事务(见下), 不依赖 ORM 刷新。
        pending_rows = [
            {
                "id": rec.id,
                "prediction_date": rec.prediction_date,
                "horizon_days": rec.horizon_days,
                "stock_symbol": rec.stock_symbol,
                "stock_market": rec.stock_market,
                "trigger_price": rec.trigger_price,
            }
            for rec in pending
        ]
    finally:
        # 结束只读事务再进入网络 IO(K 线): 不要把池连接整批握着, 否则并发评估会耗尽连接池。
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 — 已拿到本批次数据, 回滚失败不改变评估结论
            pass

    uid = str(user_id).strip() if user_id is not None else ""
    stats = {
        "scoring_blocked": False,
        # 本批次检查时是否恢复过索引(运行期不 DDL, 正常恒为空)。保留字段形状给旧调用方。
        "scoring_repaired": list(health.repaired),
        "total_pending": len(pending_rows),
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
    if not pending_rows:
        return stats

    today = date.today()
    kline_cache: dict[tuple[str, str], list] = {}

    for rec in pending_rows:
        pred_day = _parse_day(rec["prediction_date"])
        if pred_day is None:
            stats["skipped_invalid_date"] += 1
            continue

        horizon = max(1, int(rec["horizon_days"] or 1))
        # 交易日口径: 从 pred_day 起算 horizon 个工作日(与回测引擎一致)
        target_day = _add_trading_days(pred_day, horizon)
        if target_day > today:
            stats["skipped_not_due"] += 1
            continue

        stats["eligible"] += 1
        market = _to_market(rec["stock_market"])
        cache_key = (rec["stock_symbol"], market.value)
        if cache_key not in kline_cache:
            lookback_days = max(120, (today - pred_day).days + 30)
            try:
                kline_cache[cache_key] = KlineCollector(market).get_klines(
                    rec["stock_symbol"],
                    days=min(lookback_days, 600),
                )
            except Exception as e:
                logger.warning(
                    "评估建议获取K线失败: %s %s - %s",
                    rec["stock_symbol"],
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
        if rec["trigger_price"] is not None and rec["trigger_price"] > 0:
            try:
                base_price = float(rec["trigger_price"])
            except Exception:
                base_price = None
        if base_price is None:
            base_price = _pick_close_on_or_before(klines, pred_day)

        if base_price is None or base_price <= 0:
            ok = mark_agent_prediction_outcome(
                record_id=rec["id"],
                outcome_price=outcome_price,
                outcome_return_pct=None,
                status="no_base_price",
                db=db,
                health=health,
            )
        else:
            outcome_ret = (outcome_price - base_price) / base_price * 100
            ok = mark_agent_prediction_outcome(
                record_id=rec["id"],
                outcome_price=outcome_price,
                outcome_return_pct=outcome_ret,
                status="evaluated",
                db=db,
                health=health,
            )
        if ok:
            stats["evaluated"] += 1

    return stats
