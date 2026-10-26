# -*- coding: utf-8 -*-
"""市场情绪序列(market_sentiment_daily)数据新鲜度自检 + 自动补数(2026-10-11 断链修复)。

背景(破案结论)
------------
`market_sentiment_daily`(原 TQ `get_scjy_value` SC01-35 全量落库)在生产**停在
2026-09-22** 无人知, 而决策账本信号到 09-25 之后还在产 —— 情绪温度计/regime/胜率
分桶验证都取不到当日温度。属「断链无人知」类:

- 采集器**接线在**(`TqSentimentScheduler`, 每交易日 15:35, 见 `startup.py`),
  不是"没调度";
- 但每日同步一旦失败(`sync_sentiment_series` 把 TQ 异常吞成 `{"error"}` 只写一条
  日志、调度器只 `logger.warning`)就**不显眼**, 且**没有表新鲜度自检**;
- 启动回补又只在表近空(`< 30 行`)时才触发 → 中途断一次就永久停在断点。

2026-10-14 改口径(用户决策: **不购买通达信云数据 Key**)
---------------------------------------------------
SC/GP **云**函数永久放弃(生产断在 2026-09-23)。本表的产者换成
`src.core.sentiment_selfcalc`(**客户端本地接口自算**: get_zdt_data/get_pricevol +
`limit_up_events` 聚合, 每交易日 15:45). 于是:

- ``check()`` 的**自检目标就是自算序列**(同一张 `market_sentiment_daily`, 兼容列);
- 自愈走 **本地自算**(快照型接口只能算"最近一个已收盘交易日", 故历史缺口**补不回来** ——
  显式列进 ``historic_gaps``, 不假装补好);
- `get_scjy_value` 降级为**可选增强源**: 默认**关闭**
  (`SIDA_SENTIMENT_SC_ENHANCEMENT=1` 才启用), 无数据**不告警**、不再空转重试。

本模块(纯核心层, 不引 src.web —— B4.1)
------------------------------------
- ``check()``            : 比对 `market_sentiment_daily` 最新交易日 vs 期望最新交易日,
                          缺则 ``stale=True`` 并列出缺失交易日;
- ``backfill_missing()`` : **可选增强源**专用 —— 云 SC 直连批量补全(幂等 upsert);
                          默认链路不再调用它(见上);
- ``guard()``            : stale → **本地自算** → 复检, 返回诚实作业态;
- ``run_sentiment_freshness_job()``: cron 入口, 走作业框架(``ok=False → failed``),
                          落后即写通知(断链显式告警)。

口径(与全仓一致, 缺数据显式不编造)
--------------------------------
- 「真实交易日」复用 `ladder_freshness` 的判定(交易日历 **且非周末**, A 股周末恒
  不开市), 与 `market_sentiment_daily` 行集口径一致;
- 空表**不**以"无缺口"冒充, 显式 ``ok=False``(不下结论)。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

_TABLE = "market_sentiment_daily"
_MARKET = "CN"

JOB_KIND = "sentiment_freshness"
JOB_LABEL = "市场情绪序列数据自检"

#: 补数时回看的自然日窗口(要覆盖最早缺失日; SC 序列一次调用即返回整段, 略宽无害)。
BACKFILL_LOOKBACK_DAYS = 60
#: TQ 失败重试次数(总尝试 = retries + 1)。TQ 链路突发 reset 常见(~0.7s 后恢复)。
DEFAULT_RETRIES = 3
#: 重试退避(秒)。测试注入 ``sleep_fn`` 为 no-op 即不真等。
RETRY_BACKOFF_S = 1.5


def sc_enhancement_enabled() -> bool:
    """云 SC(`get_scjy_value`)是否作为**可选增强源**启用。默认关闭。

    用户决策(2026-10-14): 不购通达信云数据 Key ⇒ 生产断在 2026-09-23。默认关闭后
    本模块**不再对云 SC 空转重试/告警**; 若日后重新拿到云数据, 置
    ``SIDA_SENTIMENT_SC_ENHANCEMENT=1`` 即恢复"有数据就并入"的旧路径(真值优先)。
    """
    return str(os.environ.get("SIDA_SENTIMENT_SC_ENHANCEMENT", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


# ── 交易日历口径(复用 ladder_freshness 的唯一事实源, 避免第二套日历) ──────────

def _expected_latest(now: datetime | None = None) -> str:
    """期望的 `market_sentiment_daily` 最新交易日(YYYYMMDD)。

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

def latest_sentiment_date(db=None) -> str | None:
    """`market_sentiment_daily`(CN)最新交易日(YYYYMMDD); 表空/异常 → None(不猜)。"""
    from sqlalchemy import text

    owns = db is None
    if owns:
        from src.db.session import SessionLocal

        db = SessionLocal()
    try:
        row = db.execute(
            text(f"SELECT MAX(trade_date) FROM {_TABLE} WHERE market = :m"),
            {"m": _MARKET},
        ).fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except Exception as e:  # noqa: BLE001 —— 表未建/方言异常都算"读不到"(显式)
        logger.warning("sentiment-freshness: 读取 %s 最新日失败: %s", _TABLE, e)
        return None
    finally:
        if owns:
            db.close()


def check(now: datetime | None = None, *, latest_fn=latest_sentiment_date) -> dict:
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
            "reason": f"{_TABLE} 无任何 CN 行(空表不下结论, 需全量回填)",
        }
    missing = _missing_days(latest, expected)
    ok = not missing and str(latest) >= expected
    return {
        "ok": ok,
        "latest": str(latest),
        "expected": expected,
        "missing": missing,
        "stale": not ok,
        "reason": None if ok else f"数据停在 {latest} 日(期望 {expected})",
    }


# ── 补数(批量 + 重试/降级诚实) ───────────────────────────────────────────────

def backfill_missing(
    days: list[str] | None = None,
    *,
    sync_fn=None,
    db=None,
    retries: int = DEFAULT_RETRIES,
    sleep_fn=None,
) -> dict:
    """云 SC(``get_scjy_value``)**批量补全缺失交易日**(幂等; 重跑安全)。

    ⚠️ 2026-10-14 起这是**可选增强源**专用路径(默认不启用, 见 ``sc_enhancement_enabled``):
    日常自愈走本地自算。保留本函数供"日后重新拿到云数据"时并入真值, 以及手动运维补历史。

    SC 是市场级序列, 一次 `get_scjy_value` 调用即返回整段窗口 → 无需逐日循环。
    **重试/降级诚实**: 失败按 ``retries`` 次重试; 仍失败 → ``ok=False`` + 显式
    ``error``(绝不静默当成功); 空返回同样是失败(不拿"无数据"冒充"补好了")。

    Returns: {ok, days, rows, attempts, error}
    """
    days = sorted({str(d) for d in (days or []) if d})
    if not days:
        return {"ok": True, "days": [], "rows": 0, "attempts": 0, "error": None}

    from src.collectors.tq_sentiment_series import sync_sentiment_series

    sync_fn = sync_fn or sync_sentiment_series
    _sleep = sleep_fn if sleep_fn is not None else _default_sleep

    # 起点: 最早缺失日往前回看 BACKFILL_LOOKBACK_DAYS(自然日), 保证窗口覆盖整段缺口
    from datetime import timedelta

    try:
        first = datetime.strptime(days[0], "%Y%m%d").date()
    except (TypeError, ValueError):
        first = datetime.now().date() - timedelta(days=BACKFILL_LOOKBACK_DAYS + 10)
    start_time = (first - timedelta(days=BACKFILL_LOOKBACK_DAYS)).strftime("%Y%m%d")

    owns = db is None
    if owns:
        from src.db.session import SessionLocal

        db = SessionLocal()

    attempts = 0
    last_err: str | None = None
    try:
        total = max(1, int(retries) + 1)
        for i in range(total):
            attempts = i + 1
            try:
                out = sync_fn(db, start_time=start_time)
            except Exception as e:  # noqa: BLE001 —— 单次失败进重试, 不外抛
                last_err = str(e)[:200]
                logger.warning(
                    "sentiment-freshness 补数第 %d/%d 次异常: %s", attempts, total, e
                )
                out = {"error": last_err}
            if isinstance(out, dict) and out.get("error"):
                last_err = str(out["error"])[:200]
                logger.warning(
                    "sentiment-freshness 补数第 %d/%d 次未成功: %s", attempts, total, last_err
                )
            else:
                rows = int((out or {}).get("rows") or 0)
                return {
                    "ok": True,
                    "days": days,
                    "rows": rows,
                    "attempts": attempts,
                    "error": None,
                }
            if i < total - 1:
                _sleep(RETRY_BACKOFF_S)
        return {
            "ok": False,
            "days": days,
            "rows": 0,
            "attempts": attempts,
            "error": last_err or "TQ 无返回(客户端未开 / 窗口无数据)",
        }
    finally:
        if owns:
            db.close()


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


# ── 自检 + 自愈 ─────────────────────────────────────────────────────────────

def _default_selfcalc(trade_date: str) -> dict:
    """默认自愈路径: 用本地接口自算指定交易日并落库(2026-10-14)。

    快照型接口(`get_zdt_data`)只能给"最近一个已收盘交易日" ⇒ 这里只可能把
    **最新期望交易日**补上; 更早的缺口补不回来(由 ``guard`` 显式列进 ``historic_gaps``)。
    """
    from src.core import sentiment_selfcalc as sc

    out = sc.selfcalc_for_date(trade_date)
    return {
        "ok": bool(out.get("ok")),
        "trade_date": out.get("trade_date"),
        "written": int(out.get("written") or 0),
        "error": None if out.get("ok") else (out.get("reason") or "本地自算未成功"),
    }


def guard(
    now: datetime | None = None,
    *,
    latest_fn=latest_sentiment_date,
    selfcalc_fn=None,
    backfill_fn=None,
) -> dict:
    """自检 → 缺则**本地自算** → 复检。返回诚实作业态(``ok`` False = 仍缺, 判 failed)。

    幂等: 无缺口时不触发任何补数(纯读)。

    2026-10-14 改: 自愈主路径从"云 SC 批量补数"换成"本地自算"
    (``selfcalc_fn``, 缺省 ``_default_selfcalc``); 云 SC 降级为**可选增强源**
    (``backfill_fn``, 仅 ``SIDA_SENTIMENT_SC_ENHANCEMENT=1`` 时调用, 失败**不告警**)。
    """
    chk = check(now, latest_fn=latest_fn)
    if chk["ok"]:
        return {
            **chk,
            "latest_before": chk["latest"],
            "backfilled": 0,
            "backfill_attempts": 0,
            "backfill_error": None,
            "selfcalc_ok": False,
            "sc_enhancement": None,
            "historic_gaps": [],
            "recovered": True,
        }

    expected = chk["expected"]

    sc_fn = selfcalc_fn or _default_selfcalc
    try:
        res = sc_fn(expected) or {}
    except Exception as e:  # noqa: BLE001
        logger.exception("sentiment-freshness 本地自算异常: %s", e)
        res = {"ok": False, "written": 0, "error": str(e)}

    # 可选增强源: 云 SC —— 默认关闭(用户决策不购云 Key), 关闭时**完全不碰**
    enh = None
    if sc_enhancement_enabled():
        bf = backfill_fn or backfill_missing
        try:
            enh = bf(chk["missing"])
        except Exception as e:  # noqa: BLE001 —— 增强源失败不该拖垮主判据
            logger.warning("sentiment-freshness SC 增强源补数异常: %s", e)
            enh = {"ok": False, "rows": 0, "error": str(e)}

    chk2 = check(now, latest_fn=latest_fn)
    latest2 = chk2["latest"]
    ok = latest2 is not None and str(latest2) >= expected

    # 自算只能补最新交易日 ⇒ 上次断点到新边缘之间的交易日**补不回来**, 显式列出(不假装)
    historic: list[str] = []
    if latest2 is not None and chk["latest"] is not None:
        historic = [d for d in _missing_days(chk["latest"], str(latest2)) if d != str(latest2)]

    reason = None
    if not ok:
        why = res.get("error") or res.get("reason") or "无返回"
        reason = (
            f"市场情绪序列仍缺(最新 {latest2} < 期望 {expected}); "
            f"本地自算{'成功' if res.get('ok') else '未完成'}: {why}"
        )
        if enh is not None and not enh.get("ok"):
            reason += f"; SC 增强源: {enh.get('error')}"
    elif historic:
        reason = (
            f"最新交易日已补上({latest2}), 但 {len(historic)} 个历史交易日"
            f"({historic[0]}…{historic[-1]}) 快照型接口无法回补 —— 显式记录, 不假装补好"
        )
    return {
        **chk2,
        "ok": ok,
        "stale": not ok,
        "reason": reason,
        "latest_before": chk["latest"],
        "backfilled": int(res.get("written") or 0),
        "backfill_attempts": 1 if res else 0,
        "backfill_error": res.get("error"),
        "selfcalc_ok": bool(res.get("ok")),
        "sc_enhancement": enh,
        "historic_gaps": historic,
        "recovered": ok,
    }


# ── 告警(断链显式) ──────────────────────────────────────────────────────────

_SOURCE = "sentiment_freshness"


def _notify_stale(info: dict, now: datetime) -> None:
    """数据落后 → 写一条系统级通知(推 owner), 走 notify_center 统一入口。

    失败只记日志(告警通道问题不能反过来拖垮自检)。
    """
    try:
        from src.core.notify_center import push_notification
        from src.db.session import SessionLocal
        from src.db.models import User

        title = f"市场情绪序列数据落后: 停在 {info.get('latest') or '无'} 日"
        body = (
            f"market_sentiment_daily 最新 {info.get('latest')} 日, "
            f"期望 {info.get('expected')} 日, 缺 {len(info.get('missing') or [])} 个交易日。"
            f"本地自算{'' if info.get('selfcalc_ok') else '未完成'}"
        )
        if info.get("backfill_error"):
            body += f"(自算: {info['backfill_error']})"
        if info.get("historic_gaps"):
            body += f"; 历史 {len(info['historic_gaps'])} 个交易日快照型接口无法回补"
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
            logger.warning("[sentiment-freshness] 查询 owner 失败, 回退全局推送: %s", e)
        if not owners:
            push_notification(title=title, body=body, category="system",
                              level="warning", source=_SOURCE)
            return
        for uid in owners:
            try:
                push_notification(title=title, body=body, category="system",
                                  level="warning", source=_SOURCE, user_id=uid)
            except Exception as e:  # noqa: BLE001 —— 单用户失败不阻断
                logger.warning("[sentiment-freshness] 通知 owner %s 失败: %s", uid, e)
    except Exception as e:  # noqa: BLE001 —— 告警失败只在日志留痕
        logger.warning("[sentiment-freshness] 写告警通知失败: %s", e)


def run_sentiment_freshness_job(now: datetime | None = None) -> dict:
    """cron 入口(每日): 自检 + 自愈, 走作业框架诚实性(``ok=False → failed``)。

    非交易日也会跑 —— 这正是"周末发现断档立即自愈"的路径(期望日 = 上一交易日)。
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
        logger.exception("市场情绪序列自检作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="市场情绪序列自检: ")
    if not out.get("ok"):
        _notify_stale(out, now or datetime.now())
    return out
