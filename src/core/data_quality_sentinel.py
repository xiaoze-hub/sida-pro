"""数据质量哨兵(2026-08-21)。

背景: 线上近期暴露三类数据质量问题:
  ① 逐笔缓存重复计数 → 主力意图净额翻倍 47%(买卖总额超当日实际成交额)
  ② PG 多表 created_at 大面积 NULL → 界面按时间排序显示旧数据
  ③ 数据源失败无告警

本模块每小时跑一次 4 项数据质量检查, 发现问题写站内 Notification,
全 ok 不写(防噪音)。

检查项:
  a) tick_reconciliation      逐笔总额对账(仅盘后 >=15:10): 对自选股前5只,
                              compute_dark_flow 的 buy_amt+sell_amt vs 当日实际成交额,
                              >130% fail, 110-130% warn
  b) null_created_at          stock_suggestions / notifications / agent_runs 三表
                              created_at IS NULL 计数, >0 warn, >100 fail
  c) suggestion_drop          近24h 建议数 vs 前7天日均, <30% warn
  d) failure_notifications    notifications 近24h title/body 含"获取失败"或"失败"条数,
                              >10 warn

聚合: 任一 fail→fail; 否则任一 warn→warn; 否则 ok。
仅当 overall != ok 时写一条 Notification。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from marketdata import Symbol as MDSymbol
from marketdata.vendors.tencent import TencentQuoteVendor
from sqlalchemy import or_

from src.core.dark_flow import compute_dark_flow

logger = logging.getLogger(__name__)

# 检查参数(集中可调)
_TICK_FAIL_PCT = 130.0   # 逐笔总额 / 实际成交额 > 130% → fail
_TICK_WARN_PCT = 110.0   # > 110% → warn
_NULL_WARN = 0           # created_at NULL > 0 → warn
_NULL_FAIL = 100         # > 100 → fail
_SUGG_DROP_PCT = 30.0    # 近24h 建议数 < 前7天日均 30% → warn
_FAIL_NOTIFY_WARN = 10   # 近24h '失败' 通知 > 10 → warn
_TICK_HOUR = 15          # 盘后对账起始小时
_TICK_MINUTE = 10        # 盘后对账起始分钟(15:10)

_SOURCE = "data_quality_sentinel"


def _now() -> datetime:
    """当前北京(应用)时间, naive。作为可 patch 的时钟源(tests 控制)。"""
    from src.core.timezone import beijing_now

    return beijing_now().replace(tzinfo=None)


# ── 单项检查 ──────────────────────────────────────────────────────────────
def _check_tick_reconciliation(db, now: datetime) -> dict:
    """a) 逐笔总额对账(仅盘后 >=15:10)。"""
    base = {"check": "tick_reconciliation", "value": None}
    if (now.hour, now.minute) < (_TICK_HOUR, _TICK_MINUTE):
        base.update({
            "status": "ok",
            "detail": f"未到盘后对账时间(需 >={_TICK_HOUR:02d}:{_TICK_MINUTE:02d}), 跳过",
        })
        return base

    from src.web.models import Stock

    stocks = (
        db.query(Stock)
        .filter(Stock.market == "CN")
        .order_by(Stock.id)
        .limit(5)
        .all()
    )
    if not stocks:
        base.update({"status": "ok", "detail": "自选股为空, 跳过逐笔对账"})
        return base

    problems: list[dict] = []
    checked = 0
    worst = "ok"
    for s in stocks:
        try:
            sym = MDSymbol.parse(s.symbol, s.market or "CN")
            r = compute_dark_flow(sym)
            if not r:
                continue
            total = (r.get("buy_amt") or 0) + (r.get("sell_amt") or 0)
            q = TencentQuoteVendor().fetch([sym], {})[0]
            turnover = q.turnover or 0
            if turnover <= 0:
                continue
            ratio = total / turnover * 100
            checked += 1
            problems.append({"symbol": s.symbol, "ratio": round(ratio, 1)})
            if ratio > _TICK_FAIL_PCT:
                worst = "fail"
            elif ratio > _TICK_WARN_PCT and worst != "fail":
                worst = "warn"
        except Exception as e:  # 单只失败不影响整体
            logger.debug(f"[dq] {s.symbol} 逐笔对账异常: {e}")

    if checked == 0:
        base.update({"status": "ok", "detail": "无可对账数据(行情/逐笔缺失), 跳过"})
        return base

    base["value"] = problems
    n_fail = sum(1 for p in problems if p["ratio"] > _TICK_FAIL_PCT)
    n_warn = sum(1 for p in problems if _TICK_WARN_PCT < p["ratio"] <= _TICK_FAIL_PCT)
    if worst == "fail":
        base.update({
            "status": "fail",
            "detail": (
                f"检查 {checked} 只自选股, {n_fail} 只逐笔总额超成交额 "
                f"{_TICK_FAIL_PCT:.0f}%(重复计数嫌疑): "
                + ", ".join(f"{p['symbol']}={p['ratio']:.0f}%" for p in problems)
            ),
        })
    elif worst == "warn":
        base.update({
            "status": "warn",
            "detail": (
                f"{n_warn} 只自选股逐笔总额为成交额的 {_TICK_WARN_PCT:.0f}-"
                f"{_TICK_FAIL_PCT:.0f}%(偏高): "
                + ", ".join(f"{p['symbol']}={p['ratio']:.0f}%" for p in problems)
            ),
        })
    else:
        base.update({"status": "ok", "detail": f"检查 {checked} 只自选股逐笔总额对账正常"})
    return base


def _check_null_created_at(db, now: datetime) -> dict:
    """b) created_at NULL 计数: >0 warn, >100 fail。"""
    from src.web.models import AgentRun, Notification, StockSuggestion

    models = [
        ("stock_suggestions", StockSuggestion),
        ("notifications", Notification),
        ("agent_runs", AgentRun),
    ]
    counts: dict[str, int] = {}
    for name, model in models:
        try:
            counts[name] = db.query(model).filter(model.created_at.is_(None)).count()
        except Exception as e:
            logger.debug(f"[dq] null 计数 {name} 异常: {e}")
            counts[name] = 0

    total = sum(counts.values())
    status = "fail" if total > _NULL_FAIL else ("warn" if total > _NULL_WARN else "ok")
    detail = "、".join(f"{k}={v}" for k, v in counts.items() if v > 0) or "无"
    return {
        "check": "null_created_at",
        "status": status,
        "value": counts,
        "detail": f"created_at NULL: {detail} (> {_NULL_WARN} warn, > {_NULL_FAIL} fail)",
    }


def _check_suggestion_drop(db, now: datetime) -> dict:
    """c) 近24h 建议数 vs 前7天日均, <30% warn。"""
    from src.web.models import StockSuggestion

    cutoff_24h = now - timedelta(hours=24)
    cutoff_8d = now - timedelta(days=8)
    cutoff_1d = now - timedelta(days=1)

    recent = (
        db.query(StockSuggestion)
        .filter(StockSuggestion.created_at >= cutoff_24h)
        .count()
    )
    prev = (
        db.query(StockSuggestion)
        .filter(
            StockSuggestion.created_at >= cutoff_8d,
            StockSuggestion.created_at < cutoff_1d,
        )
        .count()
    )
    daily_avg = prev / 7.0
    value = {"recent_24h": recent, "prev_7d_daily_avg": round(daily_avg, 2)}
    if daily_avg <= 0:
        return {
            "check": "suggestion_drop",
            "status": "ok",
            "value": value,
            "detail": "近7天无建议基准, 无法判断",
        }
    ratio = recent / daily_avg * 100
    value["ratio_pct"] = round(ratio, 1)
    if ratio < _SUGG_DROP_PCT:
        return {
            "check": "suggestion_drop",
            "status": "warn",
            "value": value,
            "detail": (
                f"近24h建议 {recent} 条, 仅前7天日均 {daily_avg:.1f} 的 "
                f"{ratio:.1f}% (<{_SUGG_DROP_PCT:.0f}%)"
            ),
        }
    return {
        "check": "suggestion_drop",
        "status": "ok",
        "value": value,
        "detail": f"近24h建议 {recent} 条, 为前7天日均的 {ratio:.1f}%",
    }


def _check_failure_notifications(db, now: datetime) -> dict:
    """d) 近24h 含'失败'/'获取失败'的通知条数, >10 warn。"""
    from src.web.models import Notification

    cutoff = now - timedelta(hours=24)
    try:
        count = (
            db.query(Notification)
            .filter(
                Notification.created_at >= cutoff,
                or_(
                    Notification.title.like("%获取失败%"),
                    Notification.title.like("%失败%"),
                    Notification.body.like("%获取失败%"),
                    Notification.body.like("%失败%"),
                ),
            )
            .count()
        )
    except Exception as e:
        logger.debug(f"[dq] 失败通知计数异常: {e}")
        count = 0

    status = "warn" if count > _FAIL_NOTIFY_WARN else "ok"
    return {
        "check": "failure_notifications",
        "status": status,
        "value": count,
        "detail": f"近24h含'失败'的通知 {count} 条 (> {_FAIL_NOTIFY_WARN} warn)",
    }


# ── 聚合 / 写通知 / 入口 ──────────────────────────────────────────────────
def _aggregate(checks: list[dict]) -> str:
    if any(c.get("status") == "fail" for c in checks):
        return "fail"
    if any(c.get("status") == "warn" for c in checks):
        return "warn"
    return "ok"


# 检查项中文名(推送正文用, 让人不用猜英文标识)
_CHECK_LABELS = {
    "tick_reconciliation": "逐笔对账",
    "null_created_at": "时间戳缺失",
    "suggestion_drop": "建议数突降",
    "failure_notifications": "失败通知",
}
_STATUS_LABELS = {"ok": "正常", "warn": "警告", "fail": "异常"}
_OVERALL_LABELS = {
    "ok": "全部正常",
    "warn": "有警告",
    "fail": "发现异常",
}


def _human_body(checks: list[dict]) -> str:
    """把 checks 翻译成人话(推送正文): 每项一行「中文项名: 状态 — 细节」。

    detail 本身已是中文; ok 项只给状态不带细节, 减少噪音。
    """
    lines = []
    for c in checks:
        name = _CHECK_LABELS.get(c.get("check"), c.get("check"))
        status = _STATUS_LABELS.get(c.get("status"), c.get("status"))
        icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}.get(c.get("status"), "·")
        line = f"{icon} {name}: {status}"
        if c.get("status") != "ok" and c.get("detail"):
            line += f" — {c['detail']}"
        lines.append(line)
    return "\n".join(lines)


def _write_notification(overall: str, checks: list[dict], now: datetime) -> None:
    """写一条数据质量哨兵通知, 并触发外发推送(走 notify_center 统一入口)。

    之前直接 db.add 绕过了 push_notification, 导致: ①push_status 为空(前端显示
    "未送达") ②根本不外发。改为走 push_notification, 站内落库 + 外发 + 回写状态
    一步到位。

    推送目标(2026-08-22): 生产渠道全是用户级(user_id 非空), user_id=None 只推
    全局渠道会 skipped("未配置通知渠道")。学 scheduler.py 订阅推送模式:
    owner 必推, 其余活跃用户有订阅意向才推——哨兵是系统级告警, 推 owner 即可,
    避免打扰全部用户。
    """
    from src.core.notify_center import push_notification
    from src.web.database import SessionLocal
    from src.web.models import User

    level = "error" if overall == "fail" else "warning"
    overall_cn = _OVERALL_LABELS.get(overall, overall)
    title = f"数据质量哨兵: {overall_cn} {now.strftime('%Y-%m-%d %H:%M')}"
    body = _human_body(checks)

    target_user_ids: list[str] = []
    try:
        db = SessionLocal()
        try:
            owners = (
                db.query(User)
                .filter(User.role == "owner", User.is_active.is_(True))
                .all()
            )
            target_user_ids = [u.id for u in owners]
        finally:
            db.close()
    except Exception as e:
        logger.warning("[dq] 查询 owner 失败, 回退全局推送: %s", e)

    if not target_user_ids:
        # 无 owner(异常情况) → 走全局渠道兜底
        push_notification(
            title=title, body=body, category="system", level=level, source=_SOURCE
        )
        return

    for uid in target_user_ids:
        try:
            push_notification(
                title=title,
                body=body,
                category="system",
                level=level,
                source=_SOURCE,
                user_id=uid,
            )
        except Exception as e:
            logger.warning("[dq] 哨兵推送用户 %s 失败: %s", uid[:8], e)


def run_dq_checks(db) -> dict:
    """执行 4 项数据质量检查。

    Returns:
        {"ran_at": str, "overall": ok/warn/fail, "checks": [{check,status,detail,value}]}
    仅当 overall != ok 时写一条 Notification(全 ok 不写, 防噪音)。
    """
    now = _now()
    check_fns = [
        _check_tick_reconciliation,
        _check_null_created_at,
        _check_suggestion_drop,
        _check_failure_notifications,
    ]
    checks: list[dict] = []
    for fn in check_fns:
        try:
            checks.append(fn(db, now))
        except Exception as e:  # 单检查异常不中断整轮
            logger.warning(f"[dq] 检查 {fn.__name__} 异常: {e}", exc_info=True)
            checks.append({
                "check": fn.__name__.replace("_check_", ""),
                "status": "ok",
                "detail": f"检查异常: {e}",
                "value": None,
            })

    overall = _aggregate(checks)
    result = {"ran_at": now.isoformat(), "overall": overall, "checks": checks}

    if overall != "ok":
        try:
            _write_notification(overall, checks, now)
        except Exception as e:
            logger.warning(f"[dq] 写 Notification 失败: {e}", exc_info=True)
    return result


# ── 定时注册 ───────────────────────────────────────────────────────────────
def _app_timezone() -> str:
    import os

    return os.environ.get("TZ") or os.environ.get("APP_TIMEZONE") or "Asia/Shanghai"


def _hourly_run() -> None:
    """同步调度入口: 打开自己的 session 跑一轮检查。"""
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        run_dq_checks(db)
    finally:
        db.close()


def register_hourly_job(scheduler=None):
    """用 APScheduler 注册每小时执行的数据质量哨兵 job。

    Args:
        scheduler: 传入已有 APScheduler 实例(如项目主调度器)则复用;
                   否则新建 BackgroundScheduler(返回给调用方 start/shutdown)。
    返回配置好的 scheduler(未自动 start, 由调用方控制生命周期)。
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    tz = _app_timezone()
    sched = scheduler if scheduler is not None else BackgroundScheduler(timezone=tz)
    sched.add_job(
        _hourly_run,
        IntervalTrigger(hours=1, timezone=tz),
        id="data_quality_sentinel",
        name="数据质量哨兵",
        replace_existing=True,
    )
    logger.info("[dq] 已注册每小时数据质量哨兵 job")
    return sched
