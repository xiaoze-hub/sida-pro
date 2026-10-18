# -*- coding: utf-8 -*-
"""悟道竞价多维因子结构化落库(2026-10-18 P0-a, 方案 §7.2③)。

背景
----
悟道竞价的独家字段(`consistency` 竞价一致性 / `bidStrength` 买盘强度 /
弱转强 / 涨停反馈 / 题材强度)此前只以**格式化中文文本**存在于
`auction_collector.fetch_auction_overview()` 的 30s 进程内缓存里 ——
既没有结构, 也没有历史。方案原文: "落库前需先把 `wudao_mcp_client` 的原始 JSON
拿出来解析 —— 这是本表唯一的额外工作量"。

本模块就是那一步: 从 `fetch_auction_raw()` 的原始 dict 里提取"股票级"行,
归一化成 `auction_wudao_daily` 的列, 幂等 upsert。

诚实边界
--------
- 悟道不可用(限流 9:15-10:30 / 未配 token / 报错) → **不写行**, 返回
  `ok=False` + 可读原因; 绝不写 0 或 "无" 冒充数据;
- 原始 JSON 形状不稳定(悟道会改字段), 解析器按"多候选键名 + 多候选容器"提取,
  认不出的结构一律不猜;
- `raw` 保留该行的原始 dict, 口径漂移时可回查比对。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import AuctionWudaoDaily
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

TABLE = "auction_wudao_daily"

#: 代码/名称/各字段的候选键名。悟道不同工具返回的命名不一致(驼峰 vs 下划线),
#: 这里统一收口 —— 认不出就 None, 不猜。
_CODE_KEYS = ("code", "stockCode", "stock_code", "symbol", "secuCode", "tsCode")
_NAME_KEYS = ("name", "stockName", "stock_name", "secuName")
_CONSISTENCY_KEYS = ("consistency", "auctionConsistency", "consistencyRate")
_BID_STRENGTH_KEYS = ("bidStrength", "bid_strength", "bidAmountPercentile")
_WEAK_KEYS = ("weakToStrong", "weak_to_strong", "isWeakToStrong", "wts")
_FEEDBACK_KEYS = ("limitupFeedback", "limitUpFeedback", "feedback", "comment")
_THEME_KEYS = ("theme", "themeName", "theme_name", "concept", "plateName")
_THEME_STRENGTH_KEYS = ("themeStrength", "theme_strength", "strength")
_RANK_KEYS = ("rankInTheme", "rank_in_theme", "rank", "themeRank")
_OPEN_PCT_KEYS = ("openPct", "open_pct", "openChangePct", "gapPct", "openZAF")


def _f(v) -> float | None:
    if v is None or v == "" or v == "--":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _first(d: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _code_of(d: dict) -> str:
    raw = str(_first(d, _CODE_KEYS) or "").strip().upper()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "是", "弱转强"):
        return True
    if s in ("0", "false", "no", "n", "否"):
        return False
    # 悟道有时给弱转强评分/描述, 有分即视为命中
    f = _f(v)
    return True if f is not None and f > 0 else None


def _iter_candidate_dicts(node: Any, depth: int = 0) -> Iterable[dict]:
    """递归找出"像个股行"的 dict(含代码键)。深挖有上限, 防止畸形 JSON 递归爆栈。"""
    if depth > 4:
        return
    if isinstance(node, dict):
        if _code_of(node):
            yield node
            return
        for v in node.values():
            yield from _iter_candidate_dicts(v, depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _iter_candidate_dicts(v, depth + 1)


def normalize_row(d: dict, *, default_weak: bool | None = None) -> dict | None:
    """单个原始 dict → `auction_wudao_daily` 列字典; 无代码返回 None。"""
    code = _code_of(d)
    if not code:
        return None
    weak = _bool(_first(d, _WEAK_KEYS))
    if weak is None and default_weak is not None:
        weak = default_weak
    return {
        "symbol": code,
        "name": _first(d, _NAME_KEYS),
        "consistency": _f(_first(d, _CONSISTENCY_KEYS)),
        "bid_strength": _f(_first(d, _BID_STRENGTH_KEYS)),
        "weak_to_strong": weak,
        "limitup_feedback": (
            str(_first(d, _FEEDBACK_KEYS))[:2000]
            if _first(d, _FEEDBACK_KEYS) is not None else None
        ),
        "theme": _first(d, _THEME_KEYS),
        "theme_strength": _f(_first(d, _THEME_STRENGTH_KEYS)),
        "rank_in_theme": _i(_first(d, _RANK_KEYS)),
        "open_pct": _f(_first(d, _OPEN_PCT_KEYS)),
        "raw": d,
    }


def extract_rows(raw: dict) -> list[dict]:
    """`fetch_auction_raw()` 的返回 → 归一化行列表(按 symbol 去重, 后者补空字段)。

    注意: 同一只票可能同时出现在 opening_snapshot 与 weak_to_strong 里 ——
    合并时**只补空值**, 不覆盖已有值(避免弱转强段的稀疏字段把主段字段冲掉)。
    """
    if not isinstance(raw, dict):
        return []
    by_symbol: dict[str, dict] = {}
    for section in ("opening_snapshot", "market_scan", "weak_to_strong",
                    "limitup_feedback", "theme_strength"):
        node = raw.get(section)
        if not node:
            continue
        default_weak = True if section == "weak_to_strong" else None
        for cand in _iter_candidate_dicts(node):
            row = normalize_row(cand, default_weak=default_weak)
            if not row:
                continue
            sym = row["symbol"]
            if sym in by_symbol:
                cur = by_symbol[sym]
                for k, v in row.items():
                    if cur.get(k) is None and v is not None:
                        cur[k] = v
            else:
                by_symbol[sym] = row
    return list(by_symbol.values())


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def upsert_daily(
    rows: Iterable[dict],
    *,
    trade_date: str,
    session: Session | None = None,
    session_factory=SessionLocal,
    captured_at: float | None = None,
) -> int:
    """幂等写入(唯一键 (trade_date, symbol, market)), 返回写入行数。"""
    ts = float(captured_at if captured_at is not None else time.time())
    items = [r for r in (rows or []) if r and r.get("symbol")]
    if not items:
        return 0

    def _run(sess: Session) -> int:
        n = 0
        for r in items:
            sym = r["symbol"]
            values = {
                "trade_date": trade_date,
                "symbol": sym,
                "market": "CN",
                "name": r.get("name"),
                "consistency": r.get("consistency"),
                "bid_strength": r.get("bid_strength"),
                "weak_to_strong": r.get("weak_to_strong"),
                "limitup_feedback": r.get("limitup_feedback"),
                "theme": r.get("theme"),
                "theme_strength": r.get("theme_strength"),
                "rank_in_theme": r.get("rank_in_theme"),
                "open_pct": r.get("open_pct"),
                "source": "wudao",
                "captured_at": ts,
                "raw": r.get("raw") or {},
            }
            row = sess.scalar(
                select(AuctionWudaoDaily).where(
                    AuctionWudaoDaily.trade_date == trade_date,
                    AuctionWudaoDaily.symbol == sym,
                    AuctionWudaoDaily.market == "CN",
                )
            )
            if row is None:
                sess.add(AuctionWudaoDaily(**values))
            else:
                for k, v in values.items():
                    setattr(row, k, v)
            n += 1
        if n:
            sess.commit()
        return n

    if session is not None:
        return _run(session)
    with session_factory() as sess:
        return _run(sess)


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def load_daily(
    trade_date: str | None = None,
    *,
    limit: int = 200,
    session: Session | None = None,
    session_factory=SessionLocal,
) -> dict:
    """读某日(默认最近有行的那天)悟道竞价归档。无行 → available=False。"""

    def _run(sess: Session) -> dict:
        day = trade_date
        if not day:
            day = sess.scalar(
                select(AuctionWudaoDaily.trade_date)
                .order_by(AuctionWudaoDaily.trade_date.desc())
                .limit(1)
            )
        if not day:
            return {"available": False, "trade_date": None, "rows": [],
                    "note": "悟道竞价归档尚无任何行(未运行或悟道不可用日不写行)"}
        rows = sess.scalars(
            select(AuctionWudaoDaily)
            .where(AuctionWudaoDaily.trade_date == day)
            .order_by(AuctionWudaoDaily.bid_strength.desc(),
                      AuctionWudaoDaily.symbol.asc())
            .limit(max(1, int(limit)))
        ).all()
        if not rows:
            return {"available": False, "trade_date": day, "rows": [],
                    "note": f"{day} 无悟道竞价行(该日悟道不可用, 显式空)"}
        return {
            "available": True,
            "trade_date": day,
            "rows": [
                {
                    "symbol": r.symbol,
                    "name": r.name,
                    "consistency": r.consistency,
                    "bid_strength": r.bid_strength,
                    "weak_to_strong": r.weak_to_strong,
                    "limitup_feedback": r.limitup_feedback,
                    "theme": r.theme,
                    "theme_strength": r.theme_strength,
                    "rank_in_theme": r.rank_in_theme,
                    "open_pct": r.open_pct,
                    "source": r.source,
                }
                for r in rows
            ],
            "note": None,
        }

    if session is not None:
        return _run(session)
    with session_factory() as sess:
        return _run(sess)


# --------------------------------------------------------------------------- #
# 采集 + 作业
# --------------------------------------------------------------------------- #
def capture_daily(
    *,
    now: datetime | None = None,
    fetcher: Callable[[], dict] | None = None,
    session_factory=SessionLocal,
) -> dict:
    """取一次悟道竞价原始 JSON → 解析 → 落库。返回统计(不抛)。"""
    from src.core.trade_phase import cn_now

    if fetcher is None:
        from src.collectors.auction_collector import fetch_auction_raw as fetcher  # type: ignore[assignment]

    trade_date = cn_now(now).date().isoformat()
    try:
        raw = fetcher() or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "trade_date": trade_date, "rows": 0,
                "reason": f"悟道竞价取数失败: {e}"}

    rows = extract_rows(raw)
    if not rows:
        reason = (raw.get("error") if isinstance(raw, dict) else "") or (
            "悟道竞价无结构化行(限流窗口 9:15-10:30 / 未配 token / 返回仅文本)"
        )
        return {"ok": False, "trade_date": trade_date, "rows": 0, "reason": reason}

    try:
        n = upsert_daily(rows, trade_date=trade_date, session_factory=session_factory)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "trade_date": trade_date, "rows": 0,
                "reason": f"落库失败: {e}"}
    return {"ok": n > 0, "trade_date": trade_date, "rows": n,
            "reason": None if n else "解析出 0 行可落库标的"}


def run_auction_wudao_job(
    *, now: datetime | None = None, fetcher: Callable[[], dict] | None = None
) -> dict:
    """cron 入口(交易日 9:26): 悟道竞价独家字段日归档。"""
    from src.core.jobs import jobs
    from src.core.trade_phase import cn_now
    from src.core.trading_calendar import is_trading_day

    if not is_trading_day(cn_now(now).date()):
        return {"ok": True, "skipped": "non_trading_day", "rows": 0}

    job_id, is_new = jobs.create("auction_wudao_daily", "悟道竞价因子日归档")
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id}
    try:
        out = capture_daily(now=now, fetcher=fetcher)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("悟道竞价归档作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="悟道竞价归档: ")
    return out
