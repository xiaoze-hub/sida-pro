"""题材演化观察层(A 通道): 只读 API + SIDA 内调度注册。

## 口径(先读这段, 页面文案必须与它一致)

- 数据只来自 `theme_obs_*` **追加表**: 每行都带 `run_id` / `captured_at` / 来源,
  同一天多次采集是多个 attempt, 不覆盖。
- **没有"当日已定稿"的供应商字段**。`upstream_ready` 只证明"TQ 日线已覆盖目标日",
  不等于盘后定稿; 批次因此多是 `partial` —— 接口原样返回, 不修饰。
- 题材归属(`sector` / `concepts`)是**采集时刻**源对同一行的富化(`tq:get_relation`),
  `block_as_of` 记录反查日期。它**不是**该证券在目标日的历史题材归属, 也不证明
  某题材当时"属于主线"。
- 板块成分的 `data_day` / `date_basis` 常为 `null`(unknown): 空值就是空值, 不补。

## 为什么读写都用写库

观察层的数据是当天刚追加的, 走读副本会读到"今天没数据"而实际已写入。量极小,
统一用 `write_engine`, 避免把副本延迟显示成"没跑"。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Iterable

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, func, select

from src.core import theme_evolution_adapters as adapters
from src.core import theme_evolution_capture as cap
from src.core import theme_evolution_scheduler as sched
from src.core import theme_evolution_store as store
from src.web.database import write_engine

router = APIRouter()
logger = logging.getLogger(__name__)

#: 触发后多久算"上游已覆盖当日"。与 scheduler 的 cron 一致, 早于它一律未就绪。
READY_HOUR = sched.RUN_HOUR
READY_MINUTE = sched.RUN_MINUTE

_DAY_LEN = 8
_DEFAULT_DAYS = 60
_MAX_DAYS = 250


# ── 工具 ────────────────────────────────────────────────────────────────
def _require_day(day: str) -> str:
    text = (day or "").strip()
    if len(text) != _DAY_LEN or not text.isdigit():
        raise HTTPException(status_code=400, detail="day 必须是 YYYYMMDD")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="day 不是真实日期") from exc
    return text


def _clamp_days(value: int) -> int:
    if value < 1:
        raise HTTPException(status_code=400, detail="days 必须 >= 1")
    return min(value, _MAX_DAYS)


def _run_payload(row: Any) -> dict[str, Any]:
    captured = row.get("captured_at")
    return {
        "run_id": row.get("id"),
        "day": row.get("day"),
        "attempt_seq": row.get("attempt_seq"),
        "status": row.get("status"),
        "input_partial": bool(row.get("input_partial")),
        "source": row.get("source"),
        "rule_version": row.get("rule_version"),
        "captured_at": captured.isoformat(timespec="seconds") if captured else None,
        "captured_at_is_vendor_publish_time": False,
        "source_fresh": row.get("source_fresh"),
        "source_trade_date": row.get("source_trade_date"),
        "boards_total": row.get("boards_total"),
        "boards_ok": row.get("boards_ok"),
        "boards_empty_ok": row.get("boards_empty_ok"),
        "boards_fetch_failed": row.get("boards_fetch_failed"),
        "pool_state": row.get("pool_state"),
        "pool_data_day": row.get("pool_data_day"),
        "pool_date_basis": row.get("pool_date_basis"),
        "limit_up_count": row.get("limit_up_count"),
        "error": row.get("error"),
        "refused_reason": row.get("refused_reason"),
        "capture_notes": json.loads(row.get("capture_notes") or "[]"),
    }


def _label_payload(row: Any) -> dict[str, Any]:
    raw = row.get("concepts_json")
    concepts = json.loads(raw) if raw else None
    return {
        "stock_code": row.get("stock_code"),
        "stock_name": row.get("stock_name"),
        "sector": row.get("sector"),
        "concepts": concepts,
        # 归属反查日期: 采集当日。不是该股在 day 的历史题材归属。
        "block_as_of": row.get("block_as_of"),
        "block_source": row.get("block_source"),
        "labels_known": bool(row.get("sector") or concepts is not None),
    }


def _themes(limit_up: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """按 (行业 | 概念) 聚合当日涨停, 同一只票在一个标签下只算一次。

    返回 (按涨停家数降序的题材列表, 没有任何归属标签的证券数)。
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    unknown = 0
    for row in limit_up:
        if not (row.get("sector") or row.get("concepts_json")):
            unknown += 1
        pairs: list[tuple[str, str]] = []
        if row.get("sector"):
            pairs.append(("industry", str(row["sector"])))
        if row.get("concepts_json"):
            for name in json.loads(row["concepts_json"]):
                pairs.append(("concept", str(name)))
        for kind, name in pairs:
            slot = buckets.setdefault((kind, name), {
                "kind": kind,
                "name": name,
                "stocks": [],
                "block_as_of": set(),
            })
            slot["stocks"].append({
                "stock_code": row.get("stock_code"),
                "stock_name": row.get("stock_name"),
            })
            if row.get("block_as_of"):
                slot["block_as_of"].add(row["block_as_of"])

    themes = []
    for (kind, name), slot in buckets.items():
        themes.append({
            "kind": kind,
            "name": name,
            "limit_up_count": len(slot["stocks"]),
            "stocks": sorted(slot["stocks"], key=lambda s: str(s["stock_code"])),
            "block_as_of": sorted(slot["block_as_of"]),
        })
    themes.sort(key=lambda t: (-t["limit_up_count"], t["kind"], t["name"]))
    return themes, unknown


# ── 只读接口 ────────────────────────────────────────────────────────────
@router.get("/days")
def list_days(limit: int = Query(default=_DEFAULT_DAYS, ge=1, le=_MAX_DAYS)) -> dict:
    """最近有批次的交易日(按日取最新 attempt), 含状态与覆盖计数。"""
    with write_engine.connect() as conn:
        latest = conn.execute(
            select(
                store.THEME_OBS_RUN.c.day,
                func.max(store.THEME_OBS_RUN.c.attempt_seq).label("attempt_seq"),
            ).group_by(store.THEME_OBS_RUN.c.day)
            .order_by(store.THEME_OBS_RUN.c.day.desc())
            .limit(limit)
        ).all()
        days = []
        for day, attempt_seq in latest:
            row = conn.execute(
                select(store.THEME_OBS_RUN).where(and_(
                    store.THEME_OBS_RUN.c.day == day,
                    store.THEME_OBS_RUN.c.attempt_seq == attempt_seq,
                ))
            ).mappings().first()
            if row is not None:
                days.append(_run_payload(row))
    return {
        "days": days,
        "count": len(days),
        "notes": [
            "每行是该日最后一次 attempt; 更早的批次仍在库里, 可用 run_id 精确查询(接口未开放)",
            "status=partial 表示覆盖不完整, 不是失败, 也不是完整快照",
        ],
    }


@router.get("/day/{day}")
def get_day(day: str) -> dict:
    """某日最新**可用**批次(complete/partial)的完整证据 + 按题材聚合。"""
    day = _require_day(day)
    with write_engine.connect() as conn:
        run_row = conn.execute(
            select(store.THEME_OBS_RUN).where(and_(
                store.THEME_OBS_RUN.c.day == day,
                store.THEME_OBS_RUN.c.status.in_((store.STATUS_COMPLETE, store.STATUS_PARTIAL)),
            ))
            .order_by(store.THEME_OBS_RUN.c.captured_at.desc(),
                      store.THEME_OBS_RUN.c.attempt_seq.desc())
            .limit(1)
        ).mappings().first()
        if run_row is None:
            refused = conn.execute(
                select(func.count()).select_from(store.THEME_OBS_RUN).where(
                    store.THEME_OBS_RUN.c.day == day)
            ).scalar_one()
            raise HTTPException(
                status_code=404,
                detail=("该日有批次但全部被拒绝" if refused else "该日没有采集批次"),
            )
        evidence = store.evidence_for_run(conn, int(run_row["id"])) or {
            "run": run_row, "boards": [], "limit_up": [],
        }

    limit_up = [_label_payload(row) for row in evidence["limit_up"]]
    themes, labels_unknown = _themes(evidence["limit_up"])
    run = _run_payload(evidence["run"])
    return {
        "day": day,
        "run": run,
        "boards": [
            {
                "board_code": row.get("board_code"),
                "board_name": row.get("board_name"),
                "board_type": row.get("board_type"),
                "state": row.get("state"),
                "member_count": row.get("member_count"),
                "data_day": row.get("data_day"),
                "date_basis": row.get("date_basis"),
                "membership_as_of_known": bool(row.get("data_day")),
            }
            for row in evidence["boards"]
        ],
        "limit_up": limit_up,
        "themes": themes,
        "labels": {
            "basis": "same_row_enrichment_at_capture_time",
            "block_as_of_values": sorted({r["block_as_of"] for r in limit_up if r["block_as_of"]}),
            "stocks_without_labels": labels_unknown,
            "is_historical_membership": False,
            "note": "题材归属是采集时刻的当前成员关系, 不是目标日的历史成分, 也不证明当时属于主线",
        },
        "coverage": {
            "status": run["status"],
            "limit_up_count": run["limit_up_count"],
            "boards_ok": run["boards_ok"],
            "boards_failed": run["boards_fetch_failed"],
            "pool_date_basis": run["pool_date_basis"],
        },
    }


@router.get("/timeline")
def get_timeline(days: int = Query(default=20, ge=1, le=_MAX_DAYS)) -> dict:
    """近 N 个**有批次**的交易日 × 题材的涨停家数矩阵(用于演化条)。

    只统计概念与行业标签的**榜内同现**: 某标签下当日有几只涨停股被源标了它。
    没有任何跨日"持续/接力/主升"的推断 —— 那需要另一层验证, 这里不给。
    """
    days = _clamp_days(days)
    with write_engine.connect() as conn:
        day_rows = conn.execute(
            select(store.THEME_OBS_RUN.c.day).distinct()
            .order_by(store.THEME_OBS_RUN.c.day.desc()).limit(days)
        ).scalars().all()
        ordered = sorted(day_rows)
        series: dict[tuple[str, str], dict[str, int]] = {}
        for day in ordered:
            evidence = store.latest_evidence(conn, day)
            if not evidence:
                continue
            themes, _ = _themes(evidence["limit_up"])
            for theme in themes:
                key = (theme["kind"], theme["name"])
                series.setdefault(key, {})[day] = theme["limit_up_count"]

    out = []
    for (kind, name), by_day in series.items():
        counts = [by_day.get(day, 0) for day in ordered]
        out.append({
            "kind": kind,
            "name": name,
            "counts": counts,
            "total": sum(counts),
            "max": max(counts) if counts else 0,
            "days_present": sum(1 for c in counts if c),
        })
    out.sort(key=lambda t: (-t["total"], t["kind"], t["name"]))
    return {
        "days": ordered,
        "themes": out,
        "notes": [
            "计数 = 当日涨停榜里被源标上该标签的证券数; 榜内计数, 不是全市场成分覆盖",
            "0 表示当日该标签没有涨停股, 或当日数据缺失 —— 两者本接口未区分, 请看 /days 的 status",
        ],
    }


# ── 调度接线(默认关闭) ─────────────────────────────────────────────────
def _capture_with_runtime_freshness(bind, *, now, day, slot,
                                    source_fresh=None, source_trade_date=None, **kw):
    """在**调用时刻**读新鲜度, 而不是注册时刻。

    静态写死 `source_fresh=True` 等于对每天都说"源是新的"; 这里失败即 None(未知),
    让批次自然落成 partial/unknown。
    """
    from src.core import tdx_boards

    try:
        fresh: bool | None = bool(tdx_boards.data_fresh())
    except Exception:  # noqa: BLE001 —— 探测失败是未知, 不是"新鲜"
        fresh = None
    try:
        trade = adapters.adapt_trade_date(tdx_boards.latest_trade_date())
    except Exception:  # noqa: BLE001
        trade = None
    return cap.capture_day(
        bind, now=now, day=day, slot=slot,
        source_fresh=fresh, source_trade_date=trade, **kw,
    )


def build_deps() -> dict[str, Any]:
    """组装真实取数依赖(不联网; 网络只在作业真正跑时发生)。"""
    from src.collectors.market_sentiment_collector import MarketSentimentCollector
    from src.core import tdx_boards

    collector = MarketSentimentCollector()
    return {
        "capture": _capture_with_runtime_freshness,
        "fetch_boards": adapters.board_catalog_fetcher(tdx_boards.sector_items),
        "fetch_members": adapters.member_fetcher(tdx_boards.constituents),
        "fetch_pool": adapters.limit_up_pool_fetcher(collector.get_limit_up_pool),
        "fetch_trade_date": adapters.trade_date_fetcher(tdx_boards.latest_trade_date),
    }


def upstream_ready(now: datetime) -> dict[str, Any]:
    """仅当"TQ 日线已覆盖当日 + 已过 16:10"才就绪。

    **这不是盘后定稿证明**: 它只说明我们要读的 K 线覆盖了目标日。任何"已定稿"
    的声明都没有供应商字段支撑, 页面按 partial 展示。
    """
    from src.core import tdx_boards

    day = now.strftime("%Y%m%d")
    if (now.hour, now.minute) < (READY_HOUR, READY_MINUTE):
        return {"ready": False, "day": day, "reason": "before_daily_close_window",
                "basis": "clock_only"}

    trade = adapters.adapt_trade_date(tdx_boards.latest_trade_date())
    if trade is None:
        return {"ready": False, "day": day, "reason": "source_trade_date_unknown",
                "basis": "tq_daily"}
    if trade != day:
        return {"ready": False, "day": trade, "reason": "source_daily_not_on_target_day",
                "basis": "tq_daily"}
    if not bool(tdx_boards.data_fresh()):
        return {"ready": False, "day": day, "reason": "source_daily_reported_stale",
                "basis": "tq_daily"}

    return {
        "ready": True,
        "day": day,
        "basis": "tq_daily_covers_target_day",
        "vendor_finalized_claim": False,
    }


def register_cron(scheduler) -> bool:
    """把观察作业挂到现有 APScheduler。开关 `SIDA_THEME_EVOLUTION_AGENT=1` 才注册。"""
    if scheduler is None or not hasattr(scheduler, "add_job"):
        return False
    adapter = sched.ThemeEvolutionScheduler(
        engine=write_engine,
        deps=build_deps(),
        upstream_ready=upstream_ready,
        scheduler=scheduler,
        timezone="Asia/Shanghai",
    )
    registration = adapter.register()
    if registration.registered:
        logger.info(
            "[theme-evo-agent] 已注册(工作日 %02d:%02d); 只读观察, 不产生交易指令",
            sched.RUN_HOUR, sched.RUN_MINUTE,
        )
    return bool(registration.registered)
