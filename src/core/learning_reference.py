"""历史无归属经验参考 + system 桶独立对账(2026-11-13)。

## 三条互不相通的数据路

1. **个人桶**(`owner_scope='user'`, 有 `user_id`): 只出现在 `/api/profile/*` 的个人命中率里,
   按登录用户过滤。本模块**不读也不写**个人桶。
2. **历史无归属**(`user_id IS NULL AND owner_scope IS NULL`): 加 `user_id` 之前的存量
   (生产 4.4 万+ 条已评估)。**不能计入任何人的个人命中率**, 也不清洗/不回填。它只有
   "整体系统经验"的参考价值 —— 因此只提供**全局聚合**, 不输出个股、用户标识或任何行级明细。
3. **system 桶**(`user_id IS NULL AND owner_scope='system'`): 无会话的系统调度运行留档。
   独立统计, 与个人结果互不合并; 成熟时只做**只读行情对账预览**(零写入、不改权重、不下单)。

## 为什么旧数据永远不能"训练/调权/回测"

这些行的来源时点无法证明: 没有 `run_trace_id`、没有当时的版本约束, 无法排除"事后写入"
或同键重复。任何据此得出的命中率都可能把"看到结果之后才写下的判断"算成预测能力。
所以返回里硬编码 `provenance_verified=False` / `trainable=False` / `auto_weighting=False` /
`backtest_claim_allowed=False`, 并给出真实分母(方向性动作)、重复键风险与去重口径。

## 为什么在评分闸门后面

后验计分唯一索引(schema)不可信时, 连"这一行是不是计分行"都无法证明。虽然旧行不落在
那四条部分索引里, 但 schema 与代码假设不一致时我们宁可不给数字(fail-closed), 避免
"看起来还行"的聚合被当成可信经验。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import func

from src.core.scoring_guard import STATUS_BLOCKED, ScoringHealth, check_scoring_health
from src.db.models import AgentPredictionOutcome

logger = logging.getLogger(__name__)

SCOPE_LEGACY = "legacy_unowned"
SCOPE_SYSTEM = "system"

# 方向性动作 → 命中方向; 与个人命中榜同一口径(中性动作不计入真实分母)。
_BULLISH_ACTIONS = {"buy", "add"}
_BEARISH_ACTIONS = {"reduce", "sell", "avoid"}

LEGACY_LABELS = ("历史无归属", "未验证", "仅经验参考", "非个人成绩")

_PROVENANCE_NOTE = (
    "旧记录没有 run_trace_id / 版本约束, 来源时点不可证明: 无法排除事后写入或同键重复, "
    "不得据此训练、自动调权, 也不得宣称回测结论"
)


def _blocked_health(reason_code: str, detail: str) -> ScoringHealth:
    return ScoringHealth(status=STATUS_BLOCKED, reason_code=reason_code, detail=detail)


def _health_payload(db) -> dict:
    """schema 体检(只读、不 DDL) —— 拿不到绑定/检查抛错都翻成 blocked。"""
    bind = None
    try:
        getter = getattr(db, "get_bind", None)
        bind = getter() if callable(getter) else getattr(db, "bind", None)
    except Exception as exc:  # noqa: BLE001 — 只报类型, 不外抛
        logger.error("经验参考取数据库绑定失败: %s", type(exc).__name__)
        return _blocked_health(
            "unavailable", f"无法确定数据库绑定({type(exc).__name__}) → 经验参考阻断"
        ).as_payload()
    if bind is None:
        return _blocked_health(
            "unavailable", "无法确定数据库绑定 → 经验参考阻断"
        ).as_payload()
    try:
        return check_scoring_health(bind=bind, allow_repair=False).as_payload()
    except Exception as exc:  # noqa: BLE001 — 闸门承诺不抛, 真抛也按阻断处理
        logger.error("经验参考 schema 体检异常: %s", type(exc).__name__)
        return _blocked_health(
            "check_failed", f"schema 体检失败({type(exc).__name__}) → 经验参考阻断"
        ).as_payload()


def _since_from_days(days: int | None) -> str | None:
    if days is None:
        return None
    return (date.today() - timedelta(days=max(1, int(days)))).isoformat()


def _scope_filter(q, scope: str):
    if scope == SCOPE_LEGACY:
        return q.filter(
            AgentPredictionOutcome.user_id.is_(None),
            AgentPredictionOutcome.owner_scope.is_(None),
        )
    if scope == SCOPE_SYSTEM:
        return q.filter(
            AgentPredictionOutcome.user_id.is_(None),
            AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
        )
    raise ValueError(f"未知 scope: {scope!r}")


def _evaluated_rows(db, *, scope: str, since: str | None) -> list[tuple]:
    """只取聚合所需的列 —— 不把行级对象/明细带出本函数。"""
    q = db.query(
        AgentPredictionOutcome.id,
        AgentPredictionOutcome.agent_name,
        AgentPredictionOutcome.action,
        AgentPredictionOutcome.outcome_return_pct,
        AgentPredictionOutcome.stock_symbol,
        AgentPredictionOutcome.stock_market,
        AgentPredictionOutcome.prediction_date,
        AgentPredictionOutcome.horizon_days,
    ).filter(
        AgentPredictionOutcome.outcome_status == "evaluated",
        AgentPredictionOutcome.outcome_return_pct.isnot(None),
    )
    q = _scope_filter(q, scope)
    if since:
        q = q.filter(AgentPredictionOutcome.prediction_date >= since)
    return q.order_by(AgentPredictionOutcome.id.asc()).all()


def _count_by_status(db, *, scope: str, since: str | None) -> dict[str, int]:
    q = db.query(
        AgentPredictionOutcome.outcome_status,
        func.count(AgentPredictionOutcome.id),
    )
    q = _scope_filter(q, scope)
    if since:
        q = q.filter(AgentPredictionOutcome.prediction_date >= since)
    q = q.group_by(AgentPredictionOutcome.outcome_status)
    return {str(status or "unknown"): int(n or 0) for status, n in q.all()}


def _excluded_counts(db, *, since: str | None) -> dict[str, int]:
    """另两类"归属不可证明"的行 —— 既不是历史无归属, 也不是 system, 不得混进任何全局汇总。

    - detached: 账号注销后 `user_id` 被置空但 `owner_scope='user'`(删除语义的留档);
    - ghost: `user_id` 指向已不存在的用户(迁移库缺外键 + 账号被物理清除)。
    """
    from src.db.models import User

    def _base(q):
        q = q.filter(
            AgentPredictionOutcome.outcome_status == "evaluated",
            AgentPredictionOutcome.outcome_return_pct.isnot(None),
        )
        if since:
            q = q.filter(AgentPredictionOutcome.prediction_date >= since)
        return q

    detached = int(_base(
        db.query(func.count(AgentPredictionOutcome.id)).filter(
            AgentPredictionOutcome.user_id.is_(None),
            AgentPredictionOutcome.owner_scope == "user",
        )
    ).scalar() or 0)
    ghost = int(_base(
        db.query(func.count(AgentPredictionOutcome.id)).filter(
            AgentPredictionOutcome.user_id.isnot(None),
            AgentPredictionOutcome.user_id.notin_(db.query(User.id)),
        )
    ).scalar() or 0)
    return {"detached_account_rows": detached, "ghost_user_rows": ghost}


def _hit_for(action: str, ret: float) -> bool | None:
    action = (action or "").lower()
    if action in _BULLISH_ACTIONS:
        return ret > 0
    if action in _BEARISH_ACTIONS:
        return ret < 0
    return None


def _rows_window(rows) -> tuple[str | None, str | None]:
    days = [str(r[6])[:10] for r in rows if r[6]]
    if not days:
        return None, None
    return min(days), max(days)


def _aggregate(rows, *, scope: str, min_n: int, status_counts: dict[str, int]) -> dict:
    """全局聚合: 每键取最早一行(去重), 方向性动作构成真实分母。"""
    raw_directional = 0
    neutral = 0
    invalid = 0
    key_first: dict[tuple, tuple] = {}
    key_rows: dict[tuple, int] = {}

    for row in rows:
        (_rid, agent, action, ret, symbol, market, pdate, horizon) = row
        try:
            ret = float(ret)
        except Exception:  # noqa: BLE001 — 坏数值单独计数, 不混进"中性动作"
            invalid += 1
            continue
        if _hit_for(action, ret) is None:
            neutral += 1
            continue
        raw_directional += 1
        key = (agent or "unknown", symbol, market, pdate, horizon)
        key_rows[key] = key_rows.get(key, 0) + 1
        # rows 已按 id 升序: 首次出现即"最早写入"
        key_first.setdefault(key, row)

    deduped = list(key_first.values())
    dup_groups = [k for k, n in key_rows.items() if n > 1]
    rows_in_dup = sum(key_rows[k] for k in dup_groups)

    per: dict[str, dict] = {}
    ohit = otot = 0
    oret_sum = 0.0
    for (_rid, agent, action, ret, _symbol, _market, _pdate, _horizon) in deduped:
        ret = float(ret)
        good = bool(_hit_for(action, ret))
        ohit += int(good)
        otot += 1
        oret_sum += ret
        cell = per.setdefault(agent or "unknown", {"hit": 0, "total": 0, "ret_sum": 0.0})
        cell["total"] += 1
        cell["ret_sum"] += ret
        if good:
            cell["hit"] += 1

    agents = []
    for agent, cell in per.items():
        n = cell["total"]
        qualified = n >= int(min_n)
        agents.append({
            "agent": agent,
            "hit": cell["hit"],
            "total": n,
            "hit_rate": round(cell["hit"] / n * 100, 1) if (n and qualified) else None,
            "avg_return_pct": round(cell["ret_sum"] / n, 2) if (n and qualified) else None,
            "qualified": qualified,
            "small_sample": not qualified,
        })
    agents.sort(key=lambda a: (not a["qualified"], -(a["hit_rate"] or -1.0), a["agent"]))

    status_counts = status_counts or {}
    first_day, last_day = _rows_window(rows)
    return {
        "sample": {
            "evaluated_rows": len(rows),
            "directional_sample": raw_directional,
            "deduped_directional_sample": otot,
            "excluded_neutral_rows": neutral,
            "invalid_return_rows": invalid,
            "pending_rows": int(status_counts.get("pending", 0)),
            "other_status_rows": sum(
                n for s, n in status_counts.items() if s not in ("evaluated", "pending")
            ),
            "distinct_agents": len(per),
            "distinct_prediction_dates": len({str(r[6])[:10] for r in rows if r[6]}),
            "first_prediction_date": first_day,
            "last_prediction_date": last_day,
            "min_n": int(min_n),
        },
        "duplicate_risk": {
            "key_groups_with_duplicates": len(dup_groups),
            "rows_in_duplicate_groups": rows_in_dup,
            "deduped_sample": otot,
            "note": (
                "旧表在加 user_id 前没有部分唯一索引: 同键可能存在重复行。聚合按最早一行去重, "
                "但无法证明哪一版是「当时」的判断, 重复本身也不可视为已修复"
                if scope == SCOPE_LEGACY else
                "system 桶由部分唯一索引保证同键至多一条计分行; 这里仍按最早一行聚合"
            ),
        },
        "overall": {
            "hit_count": ohit,
            "total": otot,
            "raw_total": raw_directional,
            "hit_rate": round(ohit / otot * 100, 1) if otot else None,
            "avg_return_pct": round(oret_sum / otot, 2) if otot else None,
            "basis": "deduped_first_row_per_key",
            "scope": scope,
        },
        "agents": agents,
    }


def _summary(db, *, scope: str, since: str | None, min_n: int) -> dict:
    health = _health_payload(db)
    labels = LEGACY_LABELS if scope == SCOPE_LEGACY else ("系统运行留档", "未验证", "仅经验参考", "非个人成绩")
    base = {
        "scope": scope,
        "scope_label": "历史无归属" if scope == SCOPE_LEGACY else "system 桶(无会话运行留档)",
        "labels": list(labels),
        "global_only": True,
        "attributable_to_user": False,
        "personal_scores_affected": False,
        "independent_of_personal": True,
        "merged_into_personal": False,
        "provenance_verified": False,
        "trainable": False,
        "auto_weighting": False,
        "backtest_claim_allowed": False,
        "usage": "仅经验参考, 非个人成绩",
        "since": since,
    }
    if health.get("scoring_blocked"):
        base.update({
            "scoring_blocked": True,
            "scoring_status": health,
            "block_reason_code": health.get("reason_code") or "unknown",
            "block_reason": health.get("reason") or "schema 体检未通过",
            "sample": {
                "evaluated_rows": None,
                "directional_sample": None,
                "deduped_directional_sample": None,
                "excluded_neutral_rows": None,
                "invalid_return_rows": None,
                "pending_rows": None,
                "other_status_rows": None,
                "distinct_agents": None,
                "distinct_prediction_dates": None,
                "first_prediction_date": None,
                "last_prediction_date": None,
                "min_n": int(min_n),
            },
            "duplicate_risk": {
                "key_groups_with_duplicates": None,
                "rows_in_duplicate_groups": None,
                "deduped_sample": None,
                "note": "评分阻断: 不读取任何聚合数字",
            },
            "overall": {
                "hit_count": None,
                "total": None,
                "raw_total": None,
                "hit_rate": None,
                "avg_return_pct": None,
                "basis": None,
                "scope": scope,
            },
            "agents": [],
            "excluded": {
                "detached_account_rows": None,
                "ghost_user_rows": None,
                "note": "评分阻断: 不读取任何计数",
            },
            "note": (
                "经验参考已阻断(fail-closed): " + str(health.get("reason") or "")
                + " —— 本接口不给任何聚合数字、不训练、不调权、不写回。仅只读检查, "
                "不做任何 DDL; 恢复后重新查询即可"
            ),
            "provenance_note": _PROVENANCE_NOTE,
        })
        return base

    rows = _evaluated_rows(db, scope=scope, since=since)
    status_counts = _count_by_status(db, scope=scope, since=since)
    agg = _aggregate(rows, scope=scope, min_n=min_n, status_counts=status_counts)

    base.update({
        "scoring_blocked": False,
        "scoring_status": health,
        "provenance_note": _PROVENANCE_NOTE,
        "sample_conditions": {
            "window": f"prediction_date >= {since}" if since else "全部历史(不限窗口)",
            "denominator": "方向性动作(buy/add 或 reduce/sell/avoid)的已评估行; 中性动作不计入",
            "dedupe": "同 (agent, symbol, market, date, horizon) 保留最早一行",
            "min_n": int(min_n),
            "source": "加 user_id 之前的存量行" if scope == SCOPE_LEGACY else "无会话系统调度运行",
        },
    })
    base.update(agg)
    base["excluded"] = {
        **_excluded_counts(db, since=since),
        "note": (
            "账号注销留档(detached)与悬空 user_id(ghost)的行既不是历史无归属、也不是 system, "
            "不进入本汇总, 也不计入任何人的成绩"
        ),
    }
    if scope == SCOPE_LEGACY:
        base["note"] = (
            "历史无归属记录(加列前存量): 归属不可证明, 不纳入任何人的命中率, 不回填、不清洗。"
            "这里只做全局汇总, 供整体经验参考; 来源时点不可证, 不训练、不自动调权、不宣称回测。"
            f"真实分母为方向性动作 {agg['overall']['total']} 条(去重后), "
            f"重复键组 {agg['duplicate_risk']['key_groups_with_duplicates']} 个"
        )
    else:
        base["note"] = (
            "system 桶(无会话系统调度运行): 与个人成绩独立, 绝不并入任何账号。"
            "只做全局聚合; 对账预览为只读, 不写回、不改权重、不下单。"
        )
    return base


def legacy_reference_summary(db, *, days: int | None = None, min_n: int = 30) -> dict:
    """历史无归属已评估行的**全局**经验参考(不输出个股/用户/明细)。"""
    return _summary(db, scope=SCOPE_LEGACY, since=_since_from_days(days), min_n=min_n)


def system_reference_summary(db, *, days: int | None = None, min_n: int = 20) -> dict:
    """system 桶的**全局**聚合(独立于个人; 不并入任何账号)。"""
    return _summary(db, scope=SCOPE_SYSTEM, since=_since_from_days(days), min_n=min_n)


def _default_kline_fetcher(symbol: str, market: str):
    from src.collectors.kline_collector import KlineCollector
    from src.core.prediction_outcome import _to_market

    return KlineCollector(_to_market(market)).get_klines(symbol, days=600)


def system_reconcile_preview(
    db,
    *,
    max_horizon_days: int = 10,
    limit: int = 50,
    kline_fetcher=None,
    today: date | None = None,
) -> dict:
    """system 桶的**只读**行情对账预览 —— 算出"本来会评估多少", 一行不写。

    - 只读 system 桶的 pending 行; 个人桶/历史无归属行完全不在查询范围内;
    - 只取行情、只做计数; 不调用 mark/save、不改 outcome 字段、不改权重、不下单;
    - 行情故障逐行降级(`kline_error_rows`), 不外抛;
    - 返回恒定 `writes=0 / executed=False / dry_run=True`。
    """
    from src.core.prediction_outcome import (
        _add_trading_days,
        _parse_day,
        _pick_close_on_or_before,
    )

    health = _health_payload(db)
    skeleton = {
        "scope": SCOPE_SYSTEM,
        "dry_run": True,
        "read_only": True,
        "executed": False,
        "writes": 0,
        "auto_weighting": False,
        "weights_changed": False,
        "orders_placed": 0,
        "max_horizon_days": int(max_horizon_days),
        "limit": int(limit),
        "total_pending": None,
        "eligible": None,
        "would_evaluate": None,
        "skipped_not_due": None,
        "skipped_invalid_date": None,
        "skipped_no_price": None,
        "kline_error_rows": None,
        "personal_rows_touched": 0,
        "note": "",
    }
    if health.get("scoring_blocked"):
        skeleton.update({
            "scoring_blocked": True,
            "scoring_status": health,
            "note": (
                "评分阻断: " + str(health.get("reason") or "")
                + " —— 不做行情对账、不写回、不改权重; 恢复后重试"
            ),
        })
        return skeleton

    pending = (
        db.query(
            AgentPredictionOutcome.id,
            AgentPredictionOutcome.prediction_date,
            AgentPredictionOutcome.horizon_days,
            AgentPredictionOutcome.stock_symbol,
            AgentPredictionOutcome.stock_market,
        )
        .filter(
            AgentPredictionOutcome.user_id.is_(None),
            AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
            AgentPredictionOutcome.outcome_status == "pending",
            AgentPredictionOutcome.horizon_days <= max(1, int(max_horizon_days)),
        )
        .order_by(AgentPredictionOutcome.id.asc())
        .limit(max(1, int(limit)))
        .all()
    )
    rows = [
        {
            "prediction_date": r[1],
            "horizon_days": r[2],
            "stock_symbol": r[3],
            "stock_market": r[4],
        }
        for r in pending
    ]
    # 先结束只读事务再进网络 IO: 不在抓行情期间占着池连接。
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 — 已拿到本批次数据, 回滚失败不改变只读结论
        pass

    stats = dict(skeleton)
    stats.update({
        "scoring_blocked": False,
        "scoring_status": health,
        "total_pending": len(rows),
        "eligible": 0,
        "would_evaluate": 0,
        "skipped_not_due": 0,
        "skipped_invalid_date": 0,
        "skipped_no_price": 0,
        "kline_error_rows": 0,
    })
    fetcher = kline_fetcher if kline_fetcher is not None else _default_kline_fetcher
    ref_day = today or date.today()
    cache: dict[tuple[str, str], list] = {}

    for row in rows:
        pred_day = _parse_day(row["prediction_date"])
        if pred_day is None:
            stats["skipped_invalid_date"] += 1
            continue
        horizon = max(1, int(row["horizon_days"] or 1))
        target_day = _add_trading_days(pred_day, horizon)
        if target_day > ref_day:
            stats["skipped_not_due"] += 1
            continue
        stats["eligible"] += 1
        key = (str(row["stock_symbol"]), str(row["stock_market"] or "CN"))
        if key not in cache:
            try:
                cache[key] = fetcher(row["stock_symbol"], row["stock_market"])
            except Exception as exc:  # noqa: BLE001 — 行情故障逐行降级, 只报类型
                logger.warning("system 对账预览取行情失败: %s", type(exc).__name__)
                cache[key] = []
                stats["kline_error_rows"] += 1
        klines = cache[key]
        outcome_price = _pick_close_on_or_before(klines, target_day)
        if outcome_price is None:
            stats["skipped_no_price"] += 1
            continue
        stats["would_evaluate"] += 1

    stats["note"] = (
        "只读行情对账预览: 只统计 system 桶到期未评估行, 返回「本来会评估多少」; "
        "一行未写、未改权重、未下单。个人桶与历史无归属行不在查询范围内; "
        "system 结论不得反推/附会到任何个人成绩"
    )
    return stats
