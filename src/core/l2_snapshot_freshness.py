# -*- coding: utf-8 -*-
"""L2 成品资金收盘快照(l2_fund_snapshots)新鲜度自检 + 自动补采(2026-10-13 快照源空修复)。

背景(破案结论)
------------
主力净流入盘中显示『无数据』有两层因: ①TdxW 隔夜死 + 重拉后卡在无窗口登录态
(人工修); ②`l2_fund_snapshots` 快照表 **0 行** —— 昨天 15:05 快照 cron
(`run_l2_snapshot_job`) 撞上 TQ 僵尸期失败后**无自愈重试**, 于是非交易时段
web 层 `_apply_l2_fallback` 回退到本表时**也没东西可兜**(v0.13.62 的回退设计
是对的, 但快照源空了)。

根因同 `market_sentiment_daily` 断链: 采集器**接线在**(15:05 cron), 但
- 单次采样失败只写一条日志、**无表新鲜度自检**, 无人知;
- 调度器只 `logger.warning`, 不显式判 failed;

本模块(纯核心层, 不引 src.web —— B4.1)
------------------------------------
仿 `src.core.sentiment_freshness` 成熟模式(check → backfill → 复检 → 落后告警):
- ``check()``            : 比对 `l2_fund_snapshots` 最新交易日 vs 期望最新交易日,
                          缺则 ``stale=True`` 并列出缺失交易日;
- ``backfill_missing()`` : **复用** `l2_fund_snapshot.capture_universe` 重采(落库
                          口径与现有实现一致, **禁自创第二套**); TQ 不可用显式
                          ``ok=False`` + 原因(不静默), 留给下次调度再试;
- ``guard()``            : stale → 重采 → 复检, 返回诚实作业态;
- ``run_l2_snapshot_freshness_job()``: cron 入口, 走作业框架(``ok=False → failed``),
                          落后即写通知(断链显式告警)。

口径(与全仓一致, 缺数据显式不编造)
--------------------------------
- 「真实交易日」复用 `ladder_freshness` 的判定(交易日历 **且非周末**, 单一事实源);
- 空表**不**以"无缺口"冒充, 显式 ``ok=False``(不下结论);
- **只重采当前会话语义**: TQ `get_more_info` 只给实时会话值 —— 历史某日的 L2 收盘
  快照**跨日无法回补**(会话已结束); 故自愈恢复的是**当日**快照(常见缺口: 15:05
  cron 失败后, 15:35 自检在会话仍有效时重采补齐)。更早的历史缺口**如实告警**、
  不编造:
  空行写不出。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE = "l2_fund_snapshots"

JOB_KIND = "l2_snapshot_freshness"
JOB_LABEL = "L2 成品资金快照数据自检"


# ── 交易日历口径(复用 ladder_freshness 的唯一事实源, 避免第二套日历) ──────────

def _expected_latest(now: datetime | None = None) -> str:
    """期望的 `l2_fund_snapshots` 最新交易日(YYYYMMDD)。

    语义: 最近一个**真实交易日**(<= now 当日); 周末/节假日回退到上一交易日。
    与 `ladder_freshness.expected_latest_trading_date` 同一口径(单一事实源)。
    """
    from src.core.ladder_freshness import expected_latest_trading_date

    return expected_latest_trading_date(now)


def _missing_days(latest: str | None, expected: str | None) -> list[str]:
    """(latest, expected] 区间内的真实交易日(升序)。"""
    from src.core.ladder_freshness import missing_trading_days

    return missing_trading_days(latest, expected)


# ── 数据读 ──────────────────────────────────────────────────────────────────

def latest_snapshot_date(db=None) -> str | None:
    """`l2_fund_snapshots` 全表最新交易日(YYYYMMDD); 表空/异常 → None(不猜)。"""
    from sqlalchemy import text

    owns = db is None
    if owns:
        from src.db.session import SessionLocal

        db = SessionLocal()
    try:
        row = db.execute(text(f"SELECT MAX(trade_date) FROM {_TABLE}")).fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except Exception as e:  # noqa: BLE001 —— 表未建/方言异常都算"读不到"(显式)
        logger.warning("l2-snapshot-freshness: 读取 %s 最新日失败: %s", _TABLE, e)
        return None
    finally:
        if owns:
            db.close()


def check(now: datetime | None = None, *, latest_fn=latest_snapshot_date) -> dict:
    """新鲜度自检。返回 {ok, latest, expected, missing, stale, reason}。"""
    expected = _expected_latest(now)
    latest = latest_fn()
    if latest is None:
        return {
            "ok": False,
            "latest": None,
            "expected": expected,
            "missing": [],
            "stale": True,
            "reason": f"{_TABLE} 无任何行(空表不下结论, 需重采补齐)",
        }
    missing = _missing_days(latest, expected)
    ok = not missing and str(latest) >= expected
    return {
        "ok": ok,
        "latest": str(latest),
        "expected": expected,
        "missing": missing,
        "stale": not ok,
        "reason": None if ok else f"快照表停在 {latest} 日(期望 {expected})",
    }


# ── 补采(复用现有采样口径, TQ 不可用显式失败) ─────────────────────────────────

def backfill_missing(
    days: list[str] | None = None,
    *,
    capture_fn=None,
    now: datetime | None = None,
    symbols: list[str] | None = None,
) -> dict:
    """**复用** `l2_fund_snapshot.capture_universe` 重采当前会话快照(幂等 upsert)。

    - 落库口径与 `src/core/l2_fund_snapshot.py` 现有实现**完全一致**(不写第二套 SQL):
      TQ `get_more_info` L2 字段(Zjl_HB/Zjl/TotalBVol/TotalSVol/BCancel/SCancel/
      L2TicNum/L2OrderNum), 全 0 不落(0 绝不冒充真实值);
    - 采样 time 用调用时 `now`(缺省=当下), trade_date 落**当日**(与 15:05 cron 同口径);
    - **降级诚实**: TQ 不可用 / 采样零产出 → ``ok=False`` + 显式 ``error``(绝不静默当
      成功); 由每日调度下次再试。

    Returns: {ok, days, captured, error}
    """
    days = sorted({str(d) for d in (days or []) if d})
    if not days:
        return {"ok": True, "days": [], "captured": 0, "error": None}

    if capture_fn is None:
        from src.core.l2_fund_snapshot import capture_universe

        capture_fn = capture_universe

    try:
        out = capture_fn(symbols=symbols, now=now)
    except Exception as e:  # noqa: BLE001 —— 采样异常显式降级, 不外抛
        logger.warning("l2-snapshot-freshness 重采异常: %s", e)
        return {"ok": False, "days": days, "captured": 0, "error": str(e)[:200]}

    captured = int((out or {}).get("captured") or 0)
    if not isinstance(out, dict) or not out.get("ok"):
        err = (out or {}).get("reason") or (out or {}).get("error") or "采样未产出任何真实值"
        logger.warning("l2-snapshot-freshness 重采未成功: %s", err)
        return {"ok": False, "days": days, "captured": captured, "error": str(err)[:200]}

    return {"ok": True, "days": days, "captured": captured, "error": None}


# ── 自检 + 自愈 ─────────────────────────────────────────────────────────────

def guard(
    now: datetime | None = None,
    *,
    latest_fn=latest_snapshot_date,
    backfill_fn=None,
) -> dict:
    """自检 → 缺则重采 → 复检。返回诚实作业态(``ok`` False = 仍缺, 判 failed)。

    幂等: 无缺口时不触发任何重采(纯读 MAX(trade_date))。
    """
    chk = check(now, latest_fn=latest_fn)
    if chk["ok"]:
        return {
            **chk,
            "latest_before": chk["latest"],
            "backfilled": 0,
            "backfill_error": None,
            "recovered": True,
        }

    bf = backfill_fn or backfill_missing
    # 触发条件: 有缺口, **或**空表(latest=None → 无法枚举缺口, 但空表=全缺, 仍须重采)。
    if chk["missing"]:
        target_days = chk["missing"]
    elif chk["latest"] is None:
        target_days = [chk["expected"]]
    else:
        target_days = []
    try:
        res = bf(target_days, now=now) if target_days else {}
    except Exception as e:  # noqa: BLE001
        logger.exception("l2-snapshot-freshness 重采异常: %s", e)
        res = {"ok": False, "captured": 0, "error": str(e)}

    chk2 = check(now, latest_fn=latest_fn)
    reason = None
    if not chk2["ok"]:
        if chk2["latest"] is None:
            reason = (
                f"L2 成品资金快照表仍空无行(期望 {chk2['expected']} 日), "
                f"重采 {'成功' if res.get('ok') else '未完成'}"
            )
        else:
            reason = (
                f"L2 成品资金快照仍缺 {len(chk2['missing'])} 个交易日 "
                f"(最新 {chk2['latest']} < 期望 {chk2['expected']}), "
                f"重采 {'成功' if res.get('ok') else '未完成'}"
            )
        if res.get("error"):
            reason += f": {res['error']}"
    return {
        **chk2,
        "latest_before": chk["latest"],
        "backfilled": int(res.get("captured") or 0),
        "backfill_error": res.get("error"),
        "recovered": chk2["ok"],
        "reason": reason,
    }


# ── 告警(断链显式) ──────────────────────────────────────────────────────────

_SOURCE = "l2_snapshot_freshness"


def _notify_stale(info: dict, now: datetime) -> None:
    """快照落后 → 写一条系统级通知(推 owner), 走 notify_center 统一入口。

    失败只记日志(告警通道问题不能反过来拖垮自检)。
    """
    try:
        from src.core.notify_center import push_notification
        from src.db.models import User
        from src.db.session import SessionLocal

        title = f"L2 成品资金快照落后: 停在 {info.get('latest') or '无'} 日"
        body = (
            f"l2_fund_snapshots 最新 {info.get('latest')} 日, "
            f"期望 {info.get('expected')} 日, 缺 {len(info.get('missing') or [])} 个交易日。"
            f"重采{'未完成' if not info.get('recovered') else '已完成'}"
        )
        if info.get("backfill_error"):
            body += f"(TQ: {info['backfill_error']})"
        owners: list[str] = []
        try:
            db = SessionLocal()
            try:
                owners = [
                    u.id
                    for u in db.query(User)
                    .filter(User.role == "owner", User.is_active.is_(True))
                    .all()
                ]
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("[l2-snapshot-freshness] 查询 owner 失败, 回退全局推送: %s", e)
        if not owners:
            push_notification(title=title, body=body, category="system",
                              level="warning", source=_SOURCE)
            return
        for uid in owners:
            try:
                push_notification(title=title, body=body, category="system",
                                  level="warning", source=_SOURCE, user_id=uid)
            except Exception as e:  # noqa: BLE001 —— 单用户失败不阻断
                logger.warning("[l2-snapshot-freshness] 通知 owner %s 失败: %s", uid, e)
    except Exception as e:  # noqa: BLE001 —— 告警失败只在日志留痕
        logger.warning("[l2-snapshot-freshness] 写告警通知失败: %s", e)


def run_l2_snapshot_freshness_job(now: datetime | None = None) -> dict:
    """cron 入口(每日盘后): 自检 + 自愈, 走作业框架诚实性(``ok=False → failed``)。

    非交易日也会跑 —— 这正是"周末发现断档"的路径(期望日 = 上一交易日)。
    仍落后 → 写通知(断链显式告警, 不再无声)。
    """
    from src.core.jobs import jobs

    job_id, is_new = jobs.create(JOB_KIND, JOB_LABEL)
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id,
                "note": "上一轮自检仍在跑, 跳过本轮"}
    try:
        out = guard(now)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("L2 快照自检作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="L2快照自检: ")
    if not out.get("ok"):
        _notify_stale(out, now or datetime.now(_CST))
    return out
