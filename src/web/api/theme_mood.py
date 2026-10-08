"""题材情绪分 API(2026-09-12): 榜单 / 题材详情 / 手动扫描 / 盘中刷新 / 收盘定型。

GET  /api/theme-mood/board?window=20&top=15   Top 题材榜 + 20 日矩阵(读落库)
GET  /api/theme-mood/detail/{block_code}      单题材逐日五维明细
POST /api/theme-mood/scan/run                 手动触发扫描(后台线程)

盘中实时 + 收盘定型契约(2026-10-08, GET /board 顶层新增字段):
  phase:        "pre" | "live" | "closed_pending" | "final"
  as_of:        ISO8601 数据快照时间(带 +08:00), 无数据为 null
  settled_at:   ISO8601 收盘定型时间, 未定型为 null
  trading_day:  bool 今日是否交易日
  note:         str 显式说明(盘前/非交易日/已定型/待定型/降级原因)
语义: 盘中(交易日 9:30-15:00)=live, 每 10 分钟后台 source='intraday' 刷新;
     收盘 15:05=final(完整定型 source='close'+settled_at, 幂等, 定型行不被盘中覆盖);
     15:00-15:05 或定型未跑完=closed_pending; 盘前/非交易日=pre(无数据显式标注)。
陈旧(>10 分钟)时后台触发刷新但**先返回现有值**(serve-stale-while-revalidate), 不阻塞页面。

口径唯一事实源: src/core/theme_mood.py(设计见 docs/research/题材情绪分_设计方案_20260912.md)。
响应信封由全局中间件(src/web/response.py)统一包装。
"""
from __future__ import annotations

import json
import logging
import threading
import time

from src.core.jobs import jobs
from src.core.theme_rotation import daily_top_sets, membership_flags, rotation_series

from src.web.cache.biz_cache import biz_cache
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter()
_WINDOWS = (10, 20, 30)
_TOPS = (6, 8, 10, 15)
# 轮动口径: 每日 Top-K 的 K。与榜单 top 参数解耦 —— 榜单看"今天谁强",
# 轮动看"窗口内谁进出过", 两者 K 不必相同。
ROTATION_TOP_K = 10


def _read(sql: str, params: dict) -> list[dict]:
    from sqlalchemy import text

    from src.db.session import engine

    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [dict(r._mapping) for r in rows]


def _read_codes(sql: str, params: dict, codes: tuple[str, ...]) -> list[dict]:
    """带 `IN :codes` 的查询(text 参数不能展开元组, 必须 expanding bindparam)。"""
    from sqlalchemy import bindparam, text

    from src.db.session import engine

    with engine.begin() as conn:
        rows = conn.execute(
            text(sql).bindparams(bindparam("codes", expanding=True)),
            {**params, "codes": codes},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def _read_ohlc(dates: list[str], symbols: list[str]) -> dict:
    """klines 日线 OHLC(qfq, 唯一常驻复权维度) → {(compact_date, symbol): {o,h,l,c}}。

    klines.ts 是 timestamptz('2026-09-11 00:00:00+08:00'), 而入参 dates 是紧凑 'yyyymmdd',
    直接 IN 会因时区/格式不匹配而全空(v0.5.85 生产实测 with_candle=0) → 这里把入参转 ISO、
    用 CAST(ts AS date) 比较, 结果键再归一回紧凑格式。任一入参为空短路返回 {}。
    OHLC 任一为 None 的行丢弃(调用方落 candle=None, 不编)。

    2026-09-18 性能: 梯队 20 日窗口可聚集 **1100+ 只**涨停股, 一次
    `IN (1160 codes) × IN (20 dates)` 会撞 PG statement_timeout(实测 8s)→ 页面 500。
    改为 ①日期改**范围** BETWEEN(参数从 20 降到 2) ②symbol **分批 200** 查询。
    """
    if not dates or not symbols:
        return {}
    from sqlalchemy import bindparam, text

    from src.db.session import engine

    iso = sorted(f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in dates)
    stmt = text(
        "SELECT ts, symbol, open, high, low, close, amount FROM klines "
        "WHERE period = '1d' AND adjust = 'qfq' "
        "AND CAST(ts AS date) BETWEEN :d0 AND :d1 AND symbol IN :codes"
    ).bindparams(bindparam("codes", expanding=True))
    out: dict = {}
    codes_all = sorted(set(str(s) for s in symbols))
    batch = 200
    with engine.begin() as conn:
        for i in range(0, len(codes_all), batch):
            chunk = tuple(codes_all[i:i + batch])
            raw = conn.execute(
                stmt, {"d0": iso[0], "d1": iso[-1], "codes": chunk}
            ).fetchall()
            for r in raw:
                m = dict(r._mapping)
                if None in (m["open"], m["high"], m["low"], m["close"]):
                    continue
                out[(str(m["ts"])[:10].replace("-", ""), str(m["symbol"]))] = {
                    "o": m["open"], "h": m["high"], "l": m["low"], "c": m["close"],
                    "amount": m["amount"]}
    return out


def _ladder_stats(ladder: list, live_day) -> dict:
    """头部统计(借鉴 quicktiny): 昨日候选/今日首板/晋级/炸板/断板/冲板。

    live 时用 live_day(盘中), 否则用最新定型日。空 → 全 0(不猜)。
    """
    def counts(day):
        rows = (day or {}).get("rows") or []
        first = sum(len(r.get("codes") or []) for r in rows if r.get("boards") == 1)
        promoted = sum(len(r.get("codes") or []) for r in rows if (r.get("boards") or 0) >= 2)
        return first, promoted, len((day or {}).get("blown") or []), \
            len((day or {}).get("broken") or []), len((day or {}).get("charging") or [])

    if live_day:
        first, promoted, blown, broken, charging = counts(live_day)
        prev_total = sum(len(r.get("codes") or []) for r in (ladder[-1]["rows"] if ladder else [])) \
            if ladder else 0
    else:
        if len(ladder) >= 2:
            prev_total = sum(len(r.get("codes") or []) for r in ladder[-2]["rows"])
            first, promoted, blown, broken, charging = counts(ladder[-1])
        elif ladder:
            prev_total = 0
            first, promoted, blown, broken, charging = counts(ladder[-1])
        else:
            return {"prev_candidates": 0, "first": 0, "promoted": 0,
                    "blown": 0, "broken": 0, "charging": 0}
    return {"prev_candidates": prev_total, "first": first, "promoted": promoted,
            "blown": blown, "broken": broken, "charging": charging}


def _live_snapshot():
    """盘中态快照(v0.5.87): 读 scan_tick 预渲染的 live_day + meta。

    返回 None 的情况(调用方降级 finalized): Redis 不可用 / 当日无 live_day / 非交易时段。
    closing=True 表示 15:00~15:05 收盘撮合窗口(仍给 live 但提示稍后定型)。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.core import ladder_live_state as rst
    from src.core.limit_ladder_live import _is_intraday

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    cli = rst.client()
    if cli is None:
        return None
    if not _is_intraday(now):
        return None
    date = now.strftime("%Y%m%d")
    day = rst.load_day(cli, date)
    if not day:
        return None
    meta = rst.load_meta(cli, date)
    mins = now.hour * 60 + now.minute
    return {"live_day": day, "meta": meta, "date": date,
            "closing": (15 * 60 <= mins <= 15 * 60 + 5)}


def _loads(v):
    if not v:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _latest_date() -> str | None:
    rows = _read("SELECT MAX(trade_date) AS d FROM theme_mood_daily", {})
    return rows[0]["d"] if rows else None


@router.get("/ladder")
def get_ladder(window: int = Query(20, ge=5, le=60), mode: str = Query("auto")):
    """连板梯队(带两级缓存, 2026-09-26)。

    实测该接口 6.5s(20 天全市场涨停梯队 + 全部个股 OHLC 批量查询), 每进一次页面都重算。
    现按交易时段缓存: 盘中 30s / 收盘后 30min。`as_of` 每次新鲜生成, 不随缓存变旧。
    """
    if mode not in ("auto", "finalized", "live"):
        raise HTTPException(400, "mode 仅支持 auto/finalized/live")
    ttl = _cache_ttl()
    payload, cached = _cached(
        f"biz:tm:ladder:{window}:{mode}", ttl,
        lambda: _ladder_payload(window, mode),
        worth=lambda p: bool(p.get("ladder")),
    )
    from datetime import datetime, timezone

    return {**payload,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "cached": cached, "cache_ttl_s": ttl}


def _board_data(window: int, top: int) -> dict:
    """Top 榜 + 每题材按共享日期轴对齐的 matrix cells + 强势情绪走势; 无数据 → 空结构。"""
    from src.core.theme_mood import align_cells, market_series, rank_items

    days = _read("SELECT DISTINCT trade_date FROM theme_mood_daily ORDER BY trade_date DESC LIMIT :n",
                 {"n": int(window)})
    dates = sorted([d["trade_date"] for d in days])
    if not dates:
        return {"dates": [], "items": [], "market": [], "rotation": [], "rotation_top_k": ROTATION_TOP_K}
    latest = dates[-1]
    rows = _read("SELECT * FROM theme_mood_daily WHERE trade_date = :d", {"d": latest})
    if not rows:
        return {"dates": [], "items": [], "market": [], "rotation": [], "rotation_top_k": ROTATION_TOP_K}
    prev = dates[-2] if len(dates) > 1 else None
    prev_map: dict[str, float] = {}
    if prev:
        prev_map = {r["block_code"]: r["score"] for r in _read(
            "SELECT block_code, score FROM theme_mood_daily WHERE trade_date = :d", {"d": prev})}
    hist = _read(
        "SELECT block_code, trade_date, score, limit_up_cnt FROM theme_mood_daily WHERE trade_date >= :a"
        " ORDER BY trade_date", {"a": dates[0]}
    )
    cells: dict[str, list[dict]] = {}
    hist_scores: dict[str, list[float]] = {}
    for h in hist:
        cells.setdefault(h["block_code"], []).append(
            {"date": h["trade_date"], "score": h["score"], "limit_up_cnt": h["limit_up_cnt"]})
        if h["score"] is not None:
            hist_scores.setdefault(h["block_code"], []).append(float(h["score"]))
    items = []
    latest_codes = {r["block_code"] for r in rows}
    # 轮动(v0.5.81): 行集合 = 最新一天 ∪ 窗口内进过每日 Top-K 的题材。
    # 只取最新一天的话,"昨天还热今天掉榜"的题材整行消失, 轮动就看不见。
    top_sets = daily_top_sets(hist, top_k=ROTATION_TOP_K)
    flags = membership_flags(dates, top_sets)
    rotation = rotation_series(dates, top_sets)
    union_codes = latest_codes | set(flags)
    meta: dict[str, dict] = {r["block_code"]: r for r in rows}
    if union_codes - latest_codes:
        # PG 严格 GROUP BY: 非聚合列必须全部进 GROUP BY(SQLite 宽松, PG 会 GroupingError
        # → 题材情绪页 500 整页空白, 2026-09-18 生产实测)。同名多行时在 Python 取最新。
        extra = _read_codes(
            "SELECT block_code, block_name, block_type, MAX(trade_date) AS last_d "
            "FROM theme_mood_daily WHERE trade_date >= :a AND block_code IN :codes "
            "GROUP BY block_code, block_name, block_type",
            {"a": dates[0]}, tuple(sorted(union_codes - latest_codes)),
        )
        for e in extra:
            prev = meta.get(e["block_code"])
            if prev and str(prev.get("last_d") or "") >= str(e.get("last_d") or ""):
                continue
            meta[e["block_code"]] = {**e, "score": None, "confidence": None, "core": 0,
                                     "s1": None, "s2": None, "s3": None, "s4": None, "s5": None,
                                     "limit_up_cnt": None, "max_boards": None, "core_stocks": None}
    for code in union_codes:
        r = meta.get(code)
        if r is None:
            continue
        recent = hist_scores.get(code, [])[-3:]
        fl = flags.get(code, {"top_days": 0, "first_top_date": None,
                              "last_top_date": None, "in_top_today": False})
        items.append({
            "block_code": code, "block_name": r["block_name"], "block_type": r["block_type"],
            "score": r["score"],
            "delta": (round((r["score"] or 0) - prev_map[code], 1)
                      if code in prev_map and r["score"] is not None else None),
            "confidence": r["confidence"], "core": bool(r["core"]),
            "s1": r["s1"], "s2": r["s2"], "s3": r["s3"], "s4": r["s4"], "s5": r["s5"],
            "limit_up_cnt": r["limit_up_cnt"], "max_boards": r["max_boards"],
            "core_stocks": _loads(r["core_stocks"]) or [],
            "score3_avg": (round(sum(recent) / len(recent), 1) if recent else None),
            "cells": cells.get(code, []),
            "top_days": fl["top_days"], "first_top_date": fl["first_top_date"],
            "last_top_date": fl["last_top_date"], "in_top_today": fl["in_top_today"],
        })
    ranked = rank_items(items)
    for it in ranked:
        it["cells"] = align_cells(it["cells"], dates)
    return {"dates": dates, "items": ranked, "market": market_series(hist, dates),
            "rotation": rotation, "rotation_top_k": ROTATION_TOP_K}


def _detail_rows(block_code: str, days: int) -> list[dict]:
    rows = _read(
        "SELECT * FROM theme_mood_daily WHERE block_code = :c ORDER BY trade_date DESC LIMIT :n",
        {"c": block_code, "n": int(days)},
    )
    out = []
    for r in reversed(rows):
        out.append({
            "trade_date": r["trade_date"], "score": r["score"], "confidence": r["confidence"],
            "core": bool(r["core"]), "s1": r["s1"], "s2": r["s2"], "s3": r["s3"], "s4": r["s4"], "s5": r["s5"],
            "limit_up_cnt": r["limit_up_cnt"], "touched_cnt": r["touched_cnt"],
            "max_boards": r["max_boards"], "ge2_cnt": r["ge2_cnt"],
            "core_stocks": _loads(r["core_stocks"]) or [],
            "detail": _loads(r["detail"]) or {}, "breadth": _loads(r["breadth"]) or {},
        })
    return out


def _spawn_scan() -> dict:
    """起一次扫描作业。

    2026-09-12: 模块级 `_scan_lock/_scan_running` 换成统一作业框架 —— 单飞复用
    (重复点击拿回同一个 job_id) + 进度落库, /api/jobs 与作业面板里看得见。
    """
    job_id, is_new = jobs.create("theme_mood_scan", "题材情绪分扫描")
    if not is_new:
        return {"started": False, "reason": "扫描进行中", "job_id": job_id}

    def _runner() -> None:
        try:
            from src.core import theme_mood

            jobs.start(job_id, "scanning")
            out = theme_mood.scan(write_days=1, on_progress=jobs.progress_reporter(job_id))
            if jobs.finish(job_id, out, context="题材情绪扫描: "):
                logger.info("手动题材情绪扫描完成: %s", out)
            else:
                logger.warning("手动题材情绪扫描未成: %s", out)
        except Exception as e:  # noqa: BLE001
            jobs.fail(job_id, str(e))
            logger.warning("手动题材情绪扫描失败: %s", e)

    threading.Thread(target=_runner, name="theme-mood-scan", daemon=True).start()
    return {"started": True, "reason": None, "job_id": job_id}


# ── 盘中刷新 / 收盘定型(2026-10-08)─────────────────────────────────────────
# 契约(见模块顶层): 盘中每 10 分钟 source='intraday' 刷新; 15:05 source='close' 定型。
_REVALIDATE_COOLDOWN_S = 60.0   # 进程内 serve-stale 后台刷新的最小间隔(防请求风暴重复起)
_revalidate_lock = threading.Lock()
_last_revalidate = 0.0


def _spawn_intraday_refresh(reason: str = "scheduled") -> dict:
    """单飞起一次盘中刷新(后台线程, 立即返回)。沿用作业框架, 作业面板可见。"""
    job_id, is_new = jobs.create("theme_mood_intraday", "题材情绪盘中刷新")
    if not is_new:
        return {"started": False, "reason": "刷新进行中", "job_id": job_id}

    def _runner() -> None:
        try:
            from src.core import theme_mood

            jobs.start(job_id, "intraday")
            out = theme_mood.intraday_job()
            if jobs.finish(job_id, out, context="题材情绪盘中刷新: "):
                logger.info("题材情绪盘中刷新完成(%s): %s", reason, out)
            else:
                logger.warning("题材情绪盘中刷新未成(%s): %s", reason, out)
        except Exception as e:  # noqa: BLE001
            jobs.fail(job_id, str(e))
            logger.warning("题材情绪盘中刷新失败: %s", e)

    threading.Thread(target=_runner, name="theme-mood-intraday", daemon=True).start()
    return {"started": True, "reason": None, "job_id": job_id}


def spawn_settle(reason: str = "scheduled") -> dict:
    """单飞起一次收盘定型(后台线程, 立即返回)。source='close' + settled_at, 幂等。"""
    job_id, is_new = jobs.create("theme_mood_settle", "题材情绪收盘定型")
    if not is_new:
        return {"started": False, "reason": "定型进行中", "job_id": job_id}

    def _runner() -> None:
        try:
            from src.core import theme_mood

            jobs.start(job_id, "settling")
            out = theme_mood.settle_job()
            if jobs.finish(job_id, out, context="题材情绪收盘定型: "):
                logger.info("题材情绪收盘定型完成(%s): %s", reason, out)
            else:
                logger.warning("题材情绪收盘定型未成(%s): %s", reason, out)
        except Exception as e:  # noqa: BLE001
            jobs.fail(job_id, str(e))
            logger.warning("题材情绪收盘定型失败: %s", e)

    threading.Thread(target=_runner, name="theme-mood-settle", daemon=True).start()
    return {"started": True, "reason": None, "job_id": job_id}


def intraday_refresh_job() -> dict:
    """调度器入口(交易日 9:30-15:00 每 10 分钟): 非盘中/非交易日**跳过**(不起扫描)。"""
    from src.core import theme_mood

    try:
        if not theme_mood.is_live_window():
            return {"skipped": True, "reason": "非盘中时段"}
    except Exception as e:  # noqa: BLE001 — 日历不可用 → 保守跳过
        return {"skipped": True, "reason": f"交易日历不可用: {e!r}"[:120]}
    return _spawn_intraday_refresh(reason="scheduled")


def settle_refresh_job() -> dict:
    """调度器入口(交易日 15:05): 收盘定型; 非交易日/未到点**跳过**。"""
    from src.core import theme_mood

    try:
        if not theme_mood.is_settle_due_now():
            return {"skipped": True, "reason": "未到定型点或非交易日"}
    except Exception as e:  # noqa: BLE001
        return {"skipped": True, "reason": f"交易日历不可用: {e!r}"[:120]}
    return spawn_settle(reason="scheduled")


def _phase_payload() -> dict:
    """新鲜计算契约字段; core 读取异常时降级为 pre + 显式 note, 绝不打断页面。"""
    from src.core import theme_mood

    try:
        return theme_mood.market_phase()
    except Exception as e:  # noqa: BLE001
        logger.warning("[theme-mood] 时段判定降级: %r", e)
        return {"phase": "pre", "as_of": None, "settled_at": None, "trading_day": False,
                "note": f"时段判定降级: {e!r}"[:120]}


def _maybe_revalidate(phase: dict) -> dict:
    """盘中: 行陈旧(>10 分钟)则后台触发刷新, **先返回现有值**(serve-stale-while-revalidate)。

    绝不阻塞(后台线程 + 进程内冷却 60s + 作业单飞双节流)。非盘中直接 no-op。
    """
    from src.core import theme_mood

    if phase.get("phase") != "live":
        return {"triggered": False, "reason": "非盘中"}
    try:
        stale = theme_mood.intraday_is_stale(phase.get("as_of"))
    except Exception as e:  # noqa: BLE001
        logger.debug("[theme-mood] 陈旧判定失败, 保守不触发: %r", e)
        return {"triggered": False, "reason": "判定失败"}
    if not stale:
        return {"triggered": False, "reason": "fresh"}
    global _last_revalidate
    now = time.monotonic()
    with _revalidate_lock:
        if now - _last_revalidate < _REVALIDATE_COOLDOWN_S:
            return {"triggered": False, "reason": "cooldown"}
        _last_revalidate = now
    try:
        out = _spawn_intraday_refresh(reason="stale")
    except Exception as e:  # noqa: BLE001 — 触发失败也必须先返回现有值, 不打断页面
        logger.warning("[theme-mood] 后台刷新触发失败(仍返回现有值): %r", e)
        return {"triggered": False, "reason": "触发失败"}
    return {"triggered": bool(out.get("started")), "reason": out.get("reason"),
            "job_id": out.get("job_id")}


# ── 两级缓存(用户口径 2026-09-26)─────────────────────────────────────────
# 背景: /ladder 实测 6.5s(20 天全市场涨停梯队 + 全部个股 OHLC)、/board 2.4~5.8s/807KB,
# 每进一次页面都重算, 用户反馈"连板梯队要等一会才能加载出来"。
# 策略: 收盘后的历史日数据不可变 -> 长 TTL; 盘中有 live 日 -> 短 TTL。
_TTL_INTRADAY = 30        # 盘中(有 live 日): 30s, 保证实时性
_TTL_SETTLING = 300       # 收盘后 40 分钟内: 5min(当日数据还在定型)
_TTL_CLOSED = 1800        # 收盘后: 30min(历史不可变)


def _cache_ttl() -> int:
    """按交易时段选 TTL。盘中短、收盘后长。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.core.limit_ladder_live import _is_intraday

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    try:
        if _is_intraday(now):
            return _TTL_INTRADAY
    except Exception:  # noqa: BLE001 — 日历不可用时保守走短 TTL
        return _TTL_INTRADAY
    if now.hour * 60 + now.minute < 15 * 60 + 40:
        return _TTL_SETTLING
    return _TTL_CLOSED


def _cached(key: str, ttl: int, produce, worth) -> tuple[dict, bool]:
    """两级缓存取数。返回 (payload, 是否命中)。

    `worth(payload) is False` 时**不写缓存** —— 空/降级结果不能被钉 30 分钟
    (否则接口一旦抽风, 用户要等 TTL 过期才能恢复)。
    """
    hit = biz_cache.get_json(key)
    if isinstance(hit, dict):
        return hit, True
    payload = produce()
    if isinstance(payload, dict) and worth(payload):
        biz_cache.set_json(key, payload, ttl=ttl)
    return payload, False


def warm_caches() -> dict:
    """收盘后预热(供 18:40 调度器调用, 见 market_breadth_scheduler)。

    用前端默认参数算一次并写长 TTL, 让用户晚上/次日打开就命中缓存。
    """
    out: dict = {}
    for w, top in ((20, 15),):
        try:
            data = _board_data(w, top)
            if data.get("items"):
                biz_cache.set_json(f"biz:tm:board:{w}:{top}", data, ttl=_TTL_CLOSED)
                out[f"board:{w}:{top}"] = {"items": len(data["items"]), "cached": True}
        except Exception as e:  # noqa: BLE001 — 预热失败不影响主流程
            out[f"board:{w}:{top}"] = {"error": repr(e)[:120]}
        for mode in ("auto",):
            try:
                payload = _ladder_payload(w, mode)
                if payload.get("ladder"):
                    biz_cache.set_json(f"biz:tm:ladder:{w}:{mode}", payload, ttl=_TTL_CLOSED)
                    out[f"ladder:{w}:{mode}"] = {"days": len(payload.get("ladder") or []), "cached": True}
            except Exception as e:  # noqa: BLE001
                out[f"ladder:{w}:{mode}"] = {"error": repr(e)[:120]}
    return out

def _ladder_payload(window: int, mode: str) -> dict:
    """连板梯队(v0.5.81) + 定型炸板/断板/当日K(v0.5.85) + 盘中实时(v0.5.86, 见 live 分支)。

    通达信式天梯: 每日按连板高度分组列个股。连板数**不信任 limit_days 列**(从未落库),
    从事件表自己推: 沿表内日期序列数连续收盘封板日。只算收盘封板, touch 未封不算。
    """
    from src.core.limit_ladder import attach_candles, finalize_marks, ladder_window

    snap = _live_snapshot() if mode in ("auto", "live") else None
    days = _read(
        "SELECT DISTINCT trade_date FROM limit_up_events ORDER BY trade_date DESC LIMIT :n",
        {"n": int(window)},
    )
    dates = sorted(d["trade_date"] for d in days)
    if not dates:
        return {"dates": [], "ladder": [], "note": "limit_up_events 无数据",
                "mode": "finalized", "as_of": None, "stale": False,
                "degraded": None, "live_day": None}
    events = _read(
        "SELECT trade_date, symbol, name, is_sealed_close FROM limit_up_events "
        "WHERE trade_date >= :a ORDER BY trade_date", {"a": dates[0]},
    )
    names = {e["symbol"]: e["name"] for e in events if e.get("name")}
    ladder = ladder_window(dates, events, names)
    marks = finalize_marks(dates, events)
    symbols = sorted({str(e["symbol"]) for e in events})
    try:
        ohlc = _read_ohlc(dates, symbols)
    except Exception as e:  # noqa: BLE001
        # OHLC 只服务日K蜡烛标注, 超时/失败不得把整页打成 500
        logger.warning("[theme-mood] OHLC 批量查询失败, 梯队继续无日K: %r", e)
        ohlc = {}
    for day in ladder:
        for r in day["rows"]:
            r["stocks"] = attach_candles(
                [{"symbol": c, "name": n} for c, n in zip(r["codes"], r["names"])],
                day["date"], ohlc,
            )
            r["tag"] = "首板" if r["boards"] == 1 else None
        day["blown"] = marks[day["date"]]["blown"]
        day["broken"] = marks[day["date"]]["broken"]
    eff_mode = "live" if snap else "finalized"
    live_day = snap["live_day"] if snap else None
    stale = bool(snap["meta"].get("stale")) if snap else False
    note_closing = "收盘撮合中, 稍后定型" if (snap and snap["closing"]) else None
    return {
        "dates": dates, "ladder": ladder,
        "note": "连板数=沿事件表日期序列的连续收盘封板日; 只算收盘封板",
        "mode": eff_mode,
        "live_day": live_day,
        "stale": stale,
        "degraded": None,
        "note_closing": note_closing,
        "stats": _ladder_stats(ladder, live_day),
        # as_of 由路由层新鲜生成(不随缓存变旧), 故不进 payload/缓存 blob
    }


@router.get("/board")
def get_board(window: int = Query(20), top: int = Query(15)):
    """题材情绪榜 + 矩阵(带两级缓存, 2026-09-26)+ 盘中/定型契约字段(2026-10-08)。

    实测 2.4~5.8s / 807KB, 每进页面重算。缓存口径同 /ladder; trade_date 每次新鲜读,
    保证界面上的"数据日"不会因缓存而滞后。`phase/as_of/settled_at/trading_day/note`
    每次**新鲜**计算(不随缓存变旧); 盘中陈旧行由 `_maybe_revalidate` 后台刷新, 不阻塞。
    """
    if window not in _WINDOWS or top not in _TOPS:
        raise HTTPException(400, f"window 仅支持 {_WINDOWS}, top 仅支持 {_TOPS}")
    ttl = _cache_ttl()
    data, cached = _cached(
        f"biz:tm:board:{window}:{top}", ttl,
        lambda: _board_data(window, top),
        worth=lambda d: bool(d.get("items")),
    )
    phase = _phase_payload()
    revalidate = _maybe_revalidate(phase)
    return {**phase,
            "trade_date": _latest_date(), "window": window, "count": len(data["items"]),
            "dates": data["dates"], "items": data["items"], "market": data["market"],
            "rotation": data["rotation"], "rotation_top_k": data["rotation_top_k"],
            "cached": cached, "cache_ttl_s": ttl, "revalidate": revalidate}


@router.get("/detail/{block_code}")
def get_detail(block_code: str, days: int = Query(20, ge=1, le=60)):
    code = (block_code or "").strip().upper()
    if not code:
        raise HTTPException(400, "block_code 不能为空")
    items = _detail_rows(code, days)
    if not items:
        raise HTTPException(404, f"无该题材数据: {code}")
    return {"block_code": code, "count": len(items), "items": items}


@router.post("/scan/run")
def run_scan():
    return _spawn_scan()
