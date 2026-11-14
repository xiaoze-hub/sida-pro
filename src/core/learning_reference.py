"""历史无归属经验参考 + system 桶独立对账(2026-11-13)。

## 三条互不相通的数据路

1. **个人桶**(`owner_scope='user'`, 有 `user_id`): 只出现在 `/api/profile/*` 的个人命中率里,
   按登录用户过滤。本模块**不读也不写**个人桶。
2. **历史无归属**(`user_id IS NULL AND owner_scope IS NULL`): 加 `user_id` 之前的存量
   (生产 4.4 万+ 条已评估)。**不能计入任何人的个人命中率**, 也不清洗/不回填。它只有
   "整体系统经验"的参考价值 —— 因此只提供**全局聚合**, 不输出个股、用户标识或任何行级明细。
3. **system 桶**(`user_id IS NULL AND owner_scope='system'`): 无会话的系统调度运行留档。
   独立统计, 与个人结果互不合并; 成熟时由**独立对账**(调度器每交易日收盘后 / 管理员
   手动)持久写回 system 行, 带数据日期/规则版本/采集时间/去重规则; 只读预览仍保留,
   但页面/汇总读取的是已持久化的结果。对账永不改权重、不下单、不触发交易。

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
import math
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func

from src.core.scoring_guard import STATUS_BLOCKED, ScoringHealth, check_scoring_health
from src.db.models import AgentPredictionOutcome

logger = logging.getLogger(__name__)

SCOPE_LEGACY = "legacy_unowned"
SCOPE_SYSTEM = "system"

#: system 桶成熟结果对账的规则版本 —— 写进每一条结果行的 meta, 事后可复核。
SYSTEM_RECONCILE_VERSION = "system-reconcile/2026-11-13.1"
SYSTEM_RECONCILE_RULE = (
    "target=add_trading_days(prediction_date, horizon); "
    "outcome=exact_close_on_target_trading_day; "
    "base=trigger_price_or_exact_close_on_prediction_date; "
    "missing/suspended/timeout => unknown (不写结果); "
    "write=conditional_update WHERE scope=system AND user_id IS NULL AND status=pending"
)
#: 自动对账任务(收盘后; 调度器时区 = 应用时区, 默认 Asia/Shanghai)。
SYSTEM_RECONCILE_JOB_ID = "context_system_reconcile"
SYSTEM_RECONCILE_CRON = "40 15 * * mon-fri"

_CN_TZ = ZoneInfo("Asia/Shanghai")
#: A 股 15:00 收盘, 留 5 分钟给行情源落当日收盘价; 之前当天不算成熟。
_MARKET_CLOSE_CUTOFF = time(15, 5)

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
    statuses = {str(s or "unknown"): int(n or 0) for s, n in status_counts.items()}
    superseded_rows = int(statuses.get("superseded", 0))
    unknown_status_rows = sum(
        n for s, n in statuses.items()
        if s not in ("evaluated", "pending", "superseded")
    )
    return {
        "sample": {
            "evaluated_rows": len(rows),
            "directional_sample": raw_directional,
            "deduped_directional_sample": otot,
            "excluded_neutral_rows": neutral,
            "invalid_return_rows": invalid,
            "pending_rows": int(statuses.get("pending", 0)),
            "other_status_rows": unknown_status_rows + superseded_rows,
            "unknown_status_rows": unknown_status_rows,
            "superseded_rows": superseded_rows,
            "status_breakdown": statuses,
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
                "system 桶由部分唯一索引保证同键至多一条计分行; 成熟对账只对 pending 行做"
                "条件更新(一次且仅一次), 这里仍按最早一行聚合"
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
    if scope == SCOPE_SYSTEM:
        base["automation"] = system_reconcile_automation()
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
                "unknown_status_rows": None,
                "superseded_rows": None,
                "status_breakdown": None,
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
            "unknown": {
                "status_rows": None,
                "superseded_rows": None,
                "invalid_return_rows": None,
                "excluded_neutral_rows": None,
                "pending_rows": None,
                "status_breakdown": None,
                "note": "评分阻断: 不读取任何未知项计数",
            },
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
    base["unknown"] = {
        "status_rows": int(agg["sample"]["unknown_status_rows"] or 0),
        "superseded_rows": int(agg["sample"].get("superseded_rows") or 0),
        "invalid_return_rows": int(agg["sample"]["invalid_return_rows"] or 0),
        "excluded_neutral_rows": int(agg["sample"]["excluded_neutral_rows"] or 0),
        "pending_rows": int(agg["sample"]["pending_rows"] or 0),
        "status_breakdown": agg["sample"]["status_breakdown"],
        "note": (
            "未知项单列, 不混入命中率: 尚未成熟/无行情/停牌/数据缺失的行保持 pending, "
            "不计入方向性分母; superseded 审计版本单独计数, 不参与计分; 中性动作与坏数值也分别计数"
        ),
    }
    base["excluded"] = {
        **_excluded_counts(db, since=since),
        "note": (
            "账号注销留档(detached)与悬空 user_id(ghost)的行既不是历史无归属、也不是 system, "
            "不进入本汇总, 也不计入任何人的成绩"
        ),
    }
    if scope == SCOPE_SYSTEM:
        base["result_provenance"] = {
            "source": "system_reconcile",
            "data_date_field": "meta.reconcile.data_date",
            "version_field": "meta.reconcile.version",
            "collected_at_field": "meta.reconcile.collected_at",
            "dedupe_rule": "conditional_update_where_status_pending; one_result_per_row",
            "rule_version": SYSTEM_RECONCILE_VERSION,
            "note": (
                "system 成熟结果的持久化字段随每行落库: 数据日期/规则版本/采集时间/去重规则; "
                "本轮汇总只读这些已写结果, 不在读取时补算"
            ),
        }
        base["automation"] = system_reconcile_automation()
    if scope == SCOPE_LEGACY:
        base["note"] = (
            "历史无归属记录(加列前存量): 归属不可证明, 不纳入任何人的命中率, 不回填、不清洗。"
            "这里只做全局汇总, 供整体经验参考; 来源时点不可证, 不训练、不自动调权、不宣称回测。"
            f"真实分母为方向性动作 {agg['overall']['total']} 条(去重后), "
            f"重复键组 {agg['duplicate_risk']['key_groups_with_duplicates']} 个; "
            f"未知/未成熟 {agg['sample']['unknown_status_rows']} 条单列"
        )
    else:
        base["note"] = (
            "system 桶(无会话系统调度运行): 与个人成绩独立, 绝不并入任何账号。"
            "成熟行由调度器(每交易日收盘后)或管理员手动触发持久对账; 结果写回 system 行并带"
            "数据日期/规则版本/采集时间。对账只碰 system 行, 不改权重、不下单、不触发交易。"
            f"未知/未成熟 {agg['sample']['unknown_status_rows']} 条单列, 待评估 "
            f"{agg['sample']['pending_rows']} 条保持 pending"
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


def system_reconcile_automation() -> dict:
    """自动对账的**运行期**状态(只读) —— 页面据此说明"是否会自动跑"。

    不 import runtime(会拉起整条启动链); 只读 scheduler_registry 里已注册的 context
    调度器。未启动的进程(enabled=False)依然有手动入口, 页面必须说清楚, 不能让人以为
    "没人跑"或"一定会跑"。
    """
    info = {
        "mode": "scheduler",
        "configured": True,
        "job_id": SYSTEM_RECONCILE_JOB_ID,
        "cron": SYSTEM_RECONCILE_CRON,
        "timezone": "Asia/Shanghai",
        "enabled": False,
        "running": False,
        "next_run_at": None,
        "manual_endpoint": "/api/learning/reference/system/reconcile/run",
        "manual_owner_only": True,
        "auto_trading": False,
        "auto_weighting": False,
        "note": (
            "每工作日 15:40(Asia/Shanghai) 自动对账成熟 system 行(法定休市日通常无到期行, "
            "空跑不写结果); 管理员也可手动触发。只写 system 桶的结果字段, 不下单、不调权、不触发交易"
        ),
    }
    try:
        from src.core.scheduler_registry import get_all

        sched = get_all().get("context")
        if sched is not None:
            info["running"] = bool(getattr(sched, "running", False))
            get_job = getattr(sched, "get_job", None)
            job = get_job(SYSTEM_RECONCILE_JOB_ID) if callable(get_job) else None
            if job is not None:
                info["enabled"] = True
                # cron 任务显式钉在 Asia/Shanghai; 以 trigger 的时区为准, 避免调度器
                # 自身时区(如 UTC)让页面把"15:40 北京时间"误显示成"15:40 UTC"。
                trigger_tz = getattr(getattr(job, "trigger", None), "timezone", None)
                if trigger_tz is not None:
                    info["timezone"] = str(trigger_tz)
                next_run = getattr(job, "next_run_time", None)
                if next_run is not None:
                    try:
                        info["next_run_at"] = next_run.isoformat()
                    except Exception:  # noqa: BLE001 — 只影响展示
                        info["next_run_at"] = None
    except Exception as exc:  # noqa: BLE001 — 状态查询失败不阻断页面
        logger.warning("读取 system 对账调度状态失败: %s", type(exc).__name__)
    if not info["enabled"]:
        info["note"] += "; 当前进程未注册自动任务(调度器未启动或版本较早), 手动入口仍可用"
    return info


def _now_cn(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(_CN_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=_CN_TZ)
    return now.astimezone(_CN_TZ)


def _fetch_with_timeout(fetcher, symbol: str, market: str, timeout_seconds: float):
    """给行情外呼加**硬超时**: 超时即当作 unknown, 不写任何结果。

    行情源自身也有 timeout, 但不同 vendor/重试链可能叠加; 这里再用 daemon 线程兜一层,
    保证一次对账不会因为某个标的卡死而无限挂住。超时线程不会阻止进程退出(daemon),
    也不会把未完成的抓取结果当成行情写进库。
    """
    box: dict = {}

    def _run():
        try:
            box["data"] = fetcher(symbol, market)
        except BaseException as exc:  # noqa: BLE001 — 原样带回给调用方分类
            box["error"] = exc

    t = threading.Thread(target=_run, name="system-reconcile-fetch", daemon=True)
    t.start()
    t.join(max(0.5, float(timeout_seconds)))
    if t.is_alive():
        raise TimeoutError(f"行情外呼超过 {timeout_seconds:.1f}s")
    if "error" in box:
        raise box["error"]
    return box.get("data") or []


def _empty_reconcile_stats(*, max_horizon_days: int, limit: int, max_symbols: int,
                           fetch_timeout_seconds: float, trigger: str, run_id: str) -> dict:
    return {
        "scope": SCOPE_SYSTEM,
        "owner_scope": SCOPE_SYSTEM,
        "scoring_blocked": False,
        "trigger": trigger,
        "run_id": run_id,
        "version": SYSTEM_RECONCILE_VERSION,
        "rule": SYSTEM_RECONCILE_RULE,
        "dry_run": False,
        "read_only": False,
        "executed": False,
        "writes": 0,
        "auto_weighting": False,
        "weights_changed": False,
        "orders_placed": 0,
        "trades_triggered": 0,
        "max_horizon_days": int(max_horizon_days),
        "limit": int(limit),
        "max_symbols": int(max_symbols),
        "fetch_timeout_seconds": float(fetch_timeout_seconds),
        "total_pending": None,
        "all_pending_rows": None,
        "excluded_horizon_pending_rows": None,
        "future_prediction_pending_rows": None,
        "batch_size": None,
        "has_more": None,
        "truncated": None,
        "batch_capped_by_limit": None,
        "eligible": 0,
        "attempted_evaluated": 0,
        "evaluated": 0,
        "skipped_not_due": 0,
        "skipped_not_mature": 0,
        "skipped_invalid_date": 0,
        "skipped_calendar_error": 0,
        "skipped_no_quote": 0,
        "skipped_no_base_price": 0,
        "skipped_invalid_price": 0,
        "skipped_symbol_cap": 0,
        "skipped_concurrent": 0,
        "kline_error_rows": 0,
        "fetch_timeout_rows": 0,
        "symbols_fetched": 0,
        "symbols_failed": 0,
        "symbol_cap_hit": False,
        "personal_rows_touched": 0,
        "legacy_rows_touched": 0,
        "db_committed": False,
        "write_failed": False,
        "error_kind": None,
        "data_date_min": None,
        "data_date_max": None,
        "started_at": None,
        "finished_at": None,
        "collected_at": None,
        "pending_after": None,
        "note": "",
    }


def reconcile_system_outcomes(
    db=None,
    *,
    max_horizon_days: int = 10,
    limit: int = 50,
    max_symbols: int = 40,
    fetch_timeout_seconds: float = 10.0,
    kline_fetcher=None,
    now: datetime | None = None,
    trigger: str = "manual",
) -> dict:
    """**持久化** system 桶成熟结果对账(只写 system 行, 幂等, 可审计)。

    与只读预览(`system_reconcile_preview`)的区别: 这里真的把成熟结果写回
    `agent_prediction_outcomes` 的 system 行, 页面/汇总因此能看到可积累的已评估样本。
    但写入边界极窄:

      * 只碰 `user_id IS NULL AND owner_scope='system'` 且 `outcome_status='pending'` 的行;
        个人行、账号注销留档(detached)、历史无归属(双 NULL)一行都不读不写;
      * 成熟判定用**真实交易日历**(`add_trading_days`), 且目标日必须是收盘后(15:05 CST);
        目标日晚于今天/当天未收盘 → unknown, 保持 pending;
      * 行情必须**恰好有目标交易日收盘价**: 停牌/缺失/源故障/超时都算 unknown, 保持
        pending, 绝不拿前一交易日收盘价冒充目标日结果;
      * 基准价 = 记录里的 trigger_price(>0), 否则**预测日当天**收盘价; 两者都没有就
        unknown, 不写 0/None 的假结果;
      * 每行用条件 UPDATE(`status='pending'` + system scope)写入, 一批一个事务; 并发时
        只有一个 runner 的 rowcount=1, 另一个记为 skipped_concurrent; 失败整体 rollback;
      * 每条结果写 `meta.reconcile`(数据日期/规则版本/采集时间/去重规则), 可事后审计;
      * 返回 `writes/evaluated` 是**真实提交成功**的行数, 失败时归零并标 `write_failed`;
      * 不调用任何下单/调权入口, 返回里 `orders_placed=0 / weights_changed=False`。

    计数口径: `total_pending` 是**未截断**的候选 system pending 总数, `batch_size` 才是
    本次读取的行数; `has_more/truncated/batch_capped_by_limit` 明确标出被 limit 截断。
    """
    from src.core.context_store import SessionLocal, mark_system_prediction_outcome
    from src.core.prediction_outcome import _add_trading_days, _gate_for_session, _parse_day
    from src.core.trading_calendar import TradingCalendarError

    cap_h = max(1, int(max_horizon_days))
    cap_rows = max(1, int(limit))
    cap_symbols = max(1, int(max_symbols))
    timeout_s = max(0.5, float(fetch_timeout_seconds))
    trigger = str(trigger or "manual").strip().lower() or "manual"
    run_id = uuid.uuid4().hex
    now_cn = _now_cn(now)
    ref_day = now_cn.date()
    ref_iso = ref_day.isoformat()
    started = datetime.now(timezone.utc)

    stats = _empty_reconcile_stats(
        max_horizon_days=cap_h, limit=cap_rows, max_symbols=cap_symbols,
        fetch_timeout_seconds=timeout_s, trigger=trigger, run_id=run_id,
    )
    stats["started_at"] = started.isoformat(timespec="seconds")
    stats["collected_at"] = started.isoformat(timespec="seconds")

    owned = db is None
    if owned:
        try:
            db = SessionLocal()
        except Exception as exc:  # noqa: BLE001 — 连会话都建不出来: 不假装跑过
            logger.error("system 对账无法建立数据库会话: %s", type(exc).__name__)
            stats.update({
                "write_failed": True,
                "error_kind": type(exc).__name__,
                "note": "数据库不可用(无法建立会话): 本轮没有读取、没有写入, 也不存在假结果",
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            return stats

    try:
        health = _gate_for_session(db)
        if health.blocked:
            logger.error(
                "system 对账被评分闸门阻断(%s): %s", health.reason_code, health.detail
            )
            stats.update({
                "scoring_blocked": True,
                "scoring_status": health.as_payload(),
                "note": (
                    "评分阻断: " + str(health.detail or "")
                    + " —— 不读取聚合数字、不写回、不改权重; total_pending 记为 None(不是 0), "
                    "恢复索引后重试"
                ),
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            return stats

        stats["scoring_blocked"] = False
        stats["scoring_status"] = health.as_payload()

        def _count(*extra) -> int:
            q = db.query(func.count(AgentPredictionOutcome.id)).filter(
                AgentPredictionOutcome.user_id.is_(None),
                AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
                AgentPredictionOutcome.outcome_status == "pending",
            )
            for clause in extra:
                q = q.filter(clause)
            return int(q.scalar() or 0)

        all_pending = _count()
        excluded_horizon = _count(AgentPredictionOutcome.horizon_days > cap_h)
        future_pred = _count(AgentPredictionOutcome.prediction_date > ref_iso)
        total_pending = _count(
            AgentPredictionOutcome.horizon_days <= cap_h,
            AgentPredictionOutcome.prediction_date <= ref_iso,
        )

        rows = (
            db.query(
                AgentPredictionOutcome.id,
                AgentPredictionOutcome.agent_name,
                AgentPredictionOutcome.action,
                AgentPredictionOutcome.prediction_date,
                AgentPredictionOutcome.horizon_days,
                AgentPredictionOutcome.stock_symbol,
                AgentPredictionOutcome.stock_market,
                AgentPredictionOutcome.trigger_price,
                AgentPredictionOutcome.run_trace_id,
                AgentPredictionOutcome.meta,
            )
            .filter(
                AgentPredictionOutcome.user_id.is_(None),
                AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
                AgentPredictionOutcome.outcome_status == "pending",
                AgentPredictionOutcome.horizon_days <= cap_h,
                AgentPredictionOutcome.prediction_date <= ref_iso,
            )
            .order_by(
                AgentPredictionOutcome.prediction_date.asc(),
                AgentPredictionOutcome.id.asc(),
            )
            .limit(cap_rows)
            .all()
        )
        # 先结束只读事务再进网络 IO: 不在抓行情期间占着池连接。
        db.rollback()

        batch_size = len(rows)
        has_more = total_pending > batch_size
        stats.update({
            "total_pending": total_pending,
            "all_pending_rows": all_pending,
            "excluded_horizon_pending_rows": excluded_horizon,
            "future_prediction_pending_rows": future_pred,
            "batch_size": batch_size,
            "has_more": has_more,
            "batch_capped_by_limit": has_more,
        })
        if not rows:
            stats.update({
                "executed": True,
                "db_committed": True,
                "pending_after": total_pending,
                "note": (
                    "没有待对账的 system 行(候选 pending=0)"
                    if all_pending == 0 else
                    "候选 pending=0: 其余 pending 行超出本次窗口/上限 "
                    f"(全量 pending={all_pending}, horizon>{cap_h} 或预测日在未来), 未处理"
                ),
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            return stats

        fetcher = kline_fetcher if kline_fetcher is not None else _default_kline_fetcher
        cache: dict[tuple[str, str], dict] = {}

        def _exact_closes(klines) -> dict[date, float]:
            out: dict[date, float] = {}
            for k in klines or []:
                d = _parse_day(getattr(k, "date", None))
                c = getattr(k, "close", None)
                if d is None or c is None:
                    continue
                try:
                    price = float(c)
                except Exception:  # noqa: BLE001 — 坏行情跳过, 不猜
                    continue
                if price > 0 and math.isfinite(price):
                    out[d] = price
            return out

        data_dates: list[date] = []
        try:
            for row in rows:
                (rid, _agent, _action, pred_raw, horizon_raw, symbol, market,
                 trigger_price, run_trace, existing_meta) = row
                pred_day = _parse_day(pred_raw)
                if pred_day is None:
                    stats["skipped_invalid_date"] += 1
                    continue
                try:
                    horizon = max(1, int(horizon_raw or 1))
                except Exception:  # noqa: BLE001
                    stats["skipped_invalid_date"] += 1
                    continue
                if horizon > cap_h:
                    stats["skipped_not_due"] += 1
                    continue
                try:
                    target_day = _add_trading_days(pred_day, horizon)
                except TradingCalendarError as exc:
                    logger.warning(
                        "system 对账跳过 %s/%s: 交易日历不可用(%s)",
                        symbol, pred_raw, type(exc).__name__,
                    )
                    stats["skipped_calendar_error"] += 1
                    continue
                if target_day > ref_day:
                    stats["skipped_not_due"] += 1
                    continue
                if target_day == ref_day and now_cn.time() < _MARKET_CLOSE_CUTOFF:
                    # 当日未收盘: 行情还没成熟, 不能拿盘中价当"目标交易日收盘价"
                    stats["skipped_not_mature"] += 1
                    continue

                stats["eligible"] += 1
                key = (str(symbol or ""), str(market or "CN").strip().upper() or "CN")
                if key not in cache:
                    if len(cache) >= cap_symbols:
                        stats["skipped_symbol_cap"] += 1
                        stats["symbol_cap_hit"] = True
                        continue
                    try:
                        klines = _fetch_with_timeout(
                            fetcher, key[0], key[1], timeout_s
                        )
                    except TimeoutError:
                        logger.warning(
                            "system 对账取行情超时: %s %s", key[0], key[1],
                        )
                        cache[key] = {"ok": False, "timeout": True, "klines": []}
                        stats["symbols_failed"] += 1
                    except Exception as exc:  # noqa: BLE001 — 行情故障逐行降级
                        logger.warning(
                            "system 对账取行情失败: %s %s - %s",
                            key[0], key[1], type(exc).__name__,
                        )
                        cache[key] = {"ok": False, "timeout": False, "klines": []}
                        stats["symbols_failed"] += 1
                    else:
                        cache[key] = {"ok": True, "timeout": False, "klines": klines}
                        stats["symbols_fetched"] += 1

                entry = cache[key]
                if not entry.get("ok"):
                    stats["kline_error_rows"] += 1
                    if entry.get("timeout"):
                        stats["fetch_timeout_rows"] += 1
                    continue

                closes = _exact_closes(entry.get("klines"))
                outcome_price = closes.get(target_day)
                if outcome_price is None:
                    # 停牌/缺当日 K 线/源只回旧数据: 严格 unknown, 不拿前一日收盘价冒充
                    stats["skipped_no_quote"] += 1
                    continue

                base_price = None
                base_source = ""
                if trigger_price is not None:
                    try:
                        candidate = float(trigger_price)
                    except Exception:  # noqa: BLE001
                        candidate = 0.0
                    if candidate > 0 and math.isfinite(candidate):
                        base_price = candidate
                        base_source = "trigger_price"
                if base_price is None:
                    base_price = closes.get(pred_day)
                    base_source = "exact_close_on_prediction_date"
                if base_price is None or base_price <= 0:
                    stats["skipped_no_base_price"] += 1
                    continue

                outcome_ret = (outcome_price - base_price) / base_price * 100.0
                if outcome_ret != outcome_ret:  # NaN 防御
                    stats["skipped_invalid_price"] += 1
                    continue

                audit_meta = {
                    **(existing_meta if isinstance(existing_meta, dict) else {}),
                    "reconcile": {
                        "run_id": run_id,
                        "version": SYSTEM_RECONCILE_VERSION,
                        "rule": SYSTEM_RECONCILE_RULE,
                        "trigger": trigger,
                        "data_date": target_day.isoformat(),
                        "prediction_date": pred_day.isoformat(),
                        "horizon_days": horizon,
                        "base_price_date": (
                            pred_day.isoformat()
                            if base_source == "exact_close_on_prediction_date"
                            else None
                        ),
                        "base_price_source": base_source,
                        "quote_source": "kline_daily_close",
                        "collected_at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        "dedupe_rule": (
                            "conditional_update_where_status_pending_and_scope_system"
                        ),
                    },
                }
                stats["attempted_evaluated"] += 1
                ok = mark_system_prediction_outcome(
                    record_id=rid,
                    outcome_price=outcome_price,
                    outcome_return_pct=outcome_ret,
                    audit_meta=audit_meta,
                    expected_run_trace_id=run_trace,
                    db=db,
                    health=health,
                )
                if ok:
                    stats["evaluated"] += 1
                    data_dates.append(target_day)
                else:
                    # 另一个 runner 已经写完(或这一行已不在 pending): 幂等跳过, 不覆盖
                    stats["skipped_concurrent"] += 1

            db.commit()
            stats["db_committed"] = True
        except Exception as exc:  # noqa: BLE001 — 整批回滚, 不让半批假结果落库
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.exception("system 对账事务失败, 已回滚: %s", type(exc).__name__)
            stats.update({
                "evaluated": 0,
                "writes": 0,
                "db_committed": False,
                "write_failed": True,
                "error_kind": type(exc).__name__,
                "note": (
                    "本批次写入失败并整体回滚: 没有任何半批结果落库"
                    f"(尝试 {stats['attempted_evaluated']} 条); 错误类型 {type(exc).__name__}"
                ),
            })
            stats["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return stats

        stats.update({
            "writes": stats["evaluated"],
            "executed": True,
            "data_date_min": min(data_dates).isoformat() if data_dates else None,
            "data_date_max": max(data_dates).isoformat() if data_dates else None,
            # 本批提交后的候选剩余量: 用批次口径(total_pending - evaluated)而不是再读一次
            # 读副本(读写分离下可能滞后); 并发写入的额外变化不在本批承诺内。
            "pending_after": max(0, int(total_pending or 0) - int(stats["evaluated"] or 0)),
        })

        truncated = bool(stats["has_more"] or stats["symbol_cap_hit"])
        stats["truncated"] = truncated
        if stats["evaluated"]:
            note = (
                f"已持久回填 {stats['evaluated']} 条 system 成熟结果(真实提交); "
                f"数据日 {stats['data_date_min']}~{stats['data_date_max']}, "
                f"规则版本 {SYSTEM_RECONCILE_VERSION}"
            )
        else:
            note = (
                "本轮没有写入任何结果: 到期行因行情缺失/停牌/超时/未成熟/并发跳过, "
                "全部保持 pending; 未记录假结果"
            )
        if truncated:
            note += (
                f"; 本批只处理 {stats['batch_size']}/{stats['total_pending']} 条"
                f"(limit={cap_rows}{', 标的数上限=' + str(cap_symbols) if stats['symbol_cap_hit'] else ''}), "
                "其余下轮继续, total_pending 未按批次截断"
            )
        note += "; 只写 system 桶结果字段, 不改权重、不下单、不触发交易"
        stats["note"] = note
        stats["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return stats
    finally:
        if owned:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


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
    from src.core.prediction_outcome import _add_trading_days, _parse_day

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
        "all_pending_rows": None,
        "future_prediction_pending_rows": None,
        "batch_size": None,
        "has_more": None,
        "truncated": None,
        "batch_capped_by_limit": None,
        "eligible": None,
        "would_evaluate": None,
        "skipped_not_due": None,
        "skipped_invalid_date": None,
        "skipped_no_price": None,
        "skipped_no_quote": None,
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

    cap_h = max(1, int(max_horizon_days))
    cap_rows = max(1, int(limit))
    ref_day = today or date.today()
    ref_iso = ref_day.isoformat()
    total_pending = int(
        db.query(func.count(AgentPredictionOutcome.id))
        .filter(
            AgentPredictionOutcome.user_id.is_(None),
            AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
            AgentPredictionOutcome.outcome_status == "pending",
            AgentPredictionOutcome.horizon_days <= cap_h,
            AgentPredictionOutcome.prediction_date <= ref_iso,
        )
        .scalar() or 0
    )
    all_pending = int(
        db.query(func.count(AgentPredictionOutcome.id))
        .filter(
            AgentPredictionOutcome.user_id.is_(None),
            AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
            AgentPredictionOutcome.outcome_status == "pending",
        )
        .scalar() or 0
    )
    future_pred = int(
        db.query(func.count(AgentPredictionOutcome.id))
        .filter(
            AgentPredictionOutcome.user_id.is_(None),
            AgentPredictionOutcome.owner_scope == SCOPE_SYSTEM,
            AgentPredictionOutcome.outcome_status == "pending",
            AgentPredictionOutcome.prediction_date > ref_iso,
        )
        .scalar() or 0
    )
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
            AgentPredictionOutcome.horizon_days <= cap_h,
            AgentPredictionOutcome.prediction_date <= ref_iso,
        )
        .order_by(AgentPredictionOutcome.id.asc())
        .limit(cap_rows)
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

    batch_size = len(rows)
    has_more = total_pending > batch_size
    stats = dict(skeleton)
    stats.update({
        "scoring_blocked": False,
        "scoring_status": health,
        # 未截断的候选总数: 绝不能把 limit 后的批次长度当总数
        "total_pending": total_pending,
        "all_pending_rows": all_pending,
        "future_prediction_pending_rows": future_pred,
        "batch_size": batch_size,
        "has_more": has_more,
        "truncated": has_more,
        "batch_capped_by_limit": has_more,
        "eligible": 0,
        "would_evaluate": 0,
        "skipped_not_due": 0,
        "skipped_invalid_date": 0,
        "skipped_no_price": 0,
        "skipped_no_quote": 0,
        "kline_error_rows": 0,
    })
    fetcher = kline_fetcher if kline_fetcher is not None else _default_kline_fetcher
    ref_day = today or _now_cn(None).date()
    cache: dict[tuple[str, str], dict] = {}

    def _exact_closes(klines) -> dict[date, float]:
        out: dict[date, float] = {}
        for k in klines or []:
            d = _parse_day(getattr(k, "date", None))
            c = getattr(k, "close", None)
            if d is None or c is None:
                continue
            try:
                price = float(c)
            except Exception:  # noqa: BLE001
                continue
            if price > 0 and math.isfinite(price):
                out[d] = price
        return out

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
        key = (str(row["stock_symbol"]), str(row["stock_market"] or "CN").strip().upper() or "CN")
        if key not in cache:
            try:
                cache[key] = {"ok": True, "klines": fetcher(key[0], key[1])}
            except Exception as exc:  # noqa: BLE001 — 行情故障逐行降级, 只报类型
                logger.warning("system 对账预览取行情失败: %s", type(exc).__name__)
                cache[key] = {"ok": False, "klines": []}
        entry = cache[key]
        if not entry.get("ok"):
            stats["kline_error_rows"] += 1
            continue
        # 只认**目标交易日当天**的收盘价: 停牌/缺失一律 unknown, 不用前一日收盘价冒充
        outcome_price = _exact_closes(entry.get("klines")).get(target_day)
        if outcome_price is None:
            stats["skipped_no_price"] += 1
            stats["skipped_no_quote"] += 1
            continue
        stats["would_evaluate"] += 1

    stats["note"] = (
        "只读行情对账预览: 只统计 system 桶到期未评估行, 返回「本来会评估多少」; "
        "一行未写、未改权重、未下单。个人桶与历史无归属行不在查询范围内; "
        "total_pending 是未截断的候选总数(不是 batch_size); "
        "目标交易日无当日收盘价(停牌/缺失)按 unknown 跳过, 不用前一日价冒充; "
        "system 结论不得反推/附会到任何个人成绩"
    )
    return stats
