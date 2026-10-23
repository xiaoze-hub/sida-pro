"""盘后复盘统一证据装配器（2026-10-23）。

## 为什么要有这个模块

旧链路里"涨停结构/炸板率/跌停"三块是断的：

1. `signal_summary._block_limit_up` 把 `get_sentiment_summary()` 的 `limit_up_count`
   读成 `total`、`max_streak` 读成 `max_days` → **KeyError 静默成 0**，
   于是每天摘要都写"0家涨停，最高0板"（2026-10-22 真实是 44 家、2026-10-23 真实 86 家）。
2. **跌停家数从未采集**：`market_sentiment_daily.limit_down_count` 长期 NULL，
   `market_sentiment_daily.limit_up_open_count`（炸板）也从没进过对话证据。
3. **炸板池完全没接**：只有已封板池，没有分母 → `market_phase.seal_rate` 一直是 None，
   "炸板率"只能写"暂无"。

## 口径红线（本模块强制标注，不可静默合并）

- `tq_pool`：通达信客户端全 A 日线自算连板（★主口径，含北交所）。
- `eastmoney_zt`：东财 `getTopicZTPool`（只含沪深主板/创业板/科创板，**不含北交所**）。
- 两者家数天然有差，本模块同时给出 `total` 与 `cross_check`，并写明差值原因，
  **绝不把两个口径平均或取大**。
- 炸板率 `= 炸板 / (涨停 + 炸板)`；封板率 `= 涨停 / (涨停 + 炸板)`，均为**东财同源同日**计算，
  不跨源混算。

## 落库

`market_replay_daily`（Migration 222）：每个交易日一行，幂等 upsert，
`payload` 存完整 JSON，标量列供 SQL 聚合与参谋秒读。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date as _date
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SOURCE_EM = "eastmoney_replay"
CALIBER_TQ = "tq_pool"
CALIBER_EM = "eastmoney_zt"
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
_EM_BASE = {
    "ut": EM_UT,
    "dpt": "wz.ztzt",
    "Pageindex": "0",
    "pagesize": "200",
}
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://quote.eastmoney.com/",
}
_POOL_URLS = {
    "zt": ("https://push2ex.eastmoney.com/getTopicZTPool", "fbt:asc"),
    "zb": ("https://push2ex.eastmoney.com/getTopicZBPool", "fbt:asc"),
    "dt": ("https://push2ex.eastmoney.com/getTopicDTPool", "fund:asc"),
}
MAX_ROWS = 40


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _hms(raw: Any) -> str:
    """东财 fbt/lbt 是 HHMMSS 整数（92500 → 09:25:00）。"""
    s = str(raw or "").strip()
    if not s.isdigit():
        return ""
    s = s.zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def _sym(code: str, mkt: Any) -> str:
    """东财 m=1 → 沪市(SH)，m=0 → 深市(SZ)；返回带后缀的统一代码。"""
    c = str(code or "").strip()
    if not c:
        return ""
    suffix = "SH" if str(mkt) == "1" else "SZ"
    return f"{c}.{suffix}"


def _norm_row(it: dict) -> dict:
    """把东财池记录归一成统一样本（价格字段 ×1000 还原为元）。

    连板天数的字段名三个池各不相同，必须按池优先级逐级取：
    `lbc`(涨停池) → `zttj.days`(炸板池) → `days`(跌停池) → 1。
    """
    zttj = it.get("zttj") or {}
    board_days = it.get("lbc")
    if board_days in (None, "", 0):
        board_days = zttj.get("days") if isinstance(zttj, dict) else None
    if board_days in (None, "", 0):
        board_days = it.get("days")
    return {
        "symbol": str(it.get("c") or ""),
        "ts_code": _sym(str(it.get("c") or ""), it.get("m")),
        "name": str(it.get("n") or ""),
        "price": round(_f(it.get("p")) / 1000.0, 2),
        "pct": round(_f(it.get("zdp")), 2),
        "amount_wan": round(_f(it.get("amount")) / 1e4, 0),
        "circ_mv_yi": round(_f(it.get("ltsz")) / 1e8, 2),
        "turnover_pct": round(_f(it.get("hs")), 2),
        "seal_amount_wan": round(_f(it.get("fund")) / 1e4, 0),
        "first_seal": _hms(it.get("fbt")),
        "last_seal": _hms(it.get("lbt")),
        "open_times": int(_f(it.get("zbc")) or _f(it.get("oc"))),
        "board_days": int(_f(board_days) or 1),
        "industry": str(it.get("hybk") or ""),
        "limit_price": round(_f(it.get("ztp")) / 1000.0, 2),
    }


def _fetch_pool(kind: str, date_str: str) -> tuple[list[dict], str | None, int]:
    """抓取东财涨停/炸板/跌停池。返回 (rows, error, total)。"""
    url, sort = _POOL_URLS[kind]
    try:
        from src.collectors.market_http import market_get

        resp = market_get(
            url,
            host_key="push2ex.eastmoney.com",
            params={**_EM_BASE, "sort": sort, "date": date_str},
            headers=_EM_HEADERS,
            timeout=12,
            parse="json",
        )
        data = (resp or {}).get("data") or {}
        pool = data.get("pool") or []
        rows = [_norm_row(it) for it in pool if isinstance(it, dict)]
        return rows, None, int(_f(data.get("tc"), len(rows)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[盘后复盘] %s 池取数失败: %s", kind, exc)
        return [], f"{kind}池取数失败:{type(exc).__name__}", 0


def _tq_crosscheck(date_str: str) -> dict:
    """TQ 涨停池交叉校验（主口径）。仅做对照，不参与东财口径的炸板率计算。"""
    try:
        from src.collectors.market_sentiment_collector import MarketSentimentCollector

        summary = MarketSentimentCollector().get_sentiment_summary()
        if not isinstance(summary, dict) or summary.get("error"):
            return {"available": False, "reason": str((summary or {}).get("error"))[:80]}
        return {
            "available": True,
            "caliber": CALIBER_TQ,
            "total": int(summary.get("limit_up_count") or 0),
            "max_streak": int(summary.get("max_streak") or 0),
            "ladder": {str(k): int(v) for k, v in (summary.get("ladder") or {}).items()},
            "top_stocks": list(summary.get("top_stocks") or [])[:6],
            "source_detail": summary.get("limit_up_sources") or {},
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("[盘后复盘] TQ 交叉校验失败: %s", exc)
        return {"available": False, "reason": f"{type(exc).__name__}"}


def _ladder_counts(rows: list[dict]) -> dict[str, int]:
    c = Counter(int(r.get("board_days") or 1) for r in rows)
    return {str(k): v for k, v in sorted(c.items(), reverse=True)}


def _promotion(date_str: str, today_rows: list[dict]) -> dict:
    """晋级率 = 昨日涨停股今日继续涨停的比例（同口径：东财池 → 东财池）。"""
    try:
        from src.core.trading_calendar import prev_trading_day

        d = datetime.strptime(date_str, "%Y%m%d").date()
        prev = prev_trading_day(d).strftime("%Y%m%d")
        prev_rows, err, _ = _fetch_pool("zt", prev)
        if err or not prev_rows:
            return {"available": False, "prev_date": prev, "reason": err or "昨日池为空"}
        prev_syms = {r["symbol"] for r in prev_rows if r.get("symbol")}
        today_syms = {r["symbol"] for r in today_rows if r.get("symbol")}
        promoted = len(prev_syms & today_syms)
        return {
            "available": True,
            "prev_date": prev,
            "prev_count": len(prev_syms),
            "promoted": promoted,
            "promo_rate_pct": round(promoted / len(prev_syms) * 100.0, 2) if prev_syms else None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("[盘后复盘] 晋级率计算失败: %s", exc)
        return {"available": False, "reason": f"{type(exc).__name__}"}


def _prev_comparison(date_str: str, cur: dict) -> dict:
    """与上一交易日对比：涨停/炸板/炸板率/跌停/最高板/2板+ 的真实环比差值。"""
    try:
        from src.core.trading_calendar import prev_trading_day

        d = datetime.strptime(date_str, "%Y%m%d").date()
        prev = prev_trading_day(d).strftime("%Y%m%d")
        zt_rows, zt_err, zt_total = _fetch_pool("zt", prev)
        zb_rows, zb_err, zb_total = _fetch_pool("zb", prev)
        dt_rows, dt_err, dt_total = _fetch_pool("dt", prev)
        if zt_err and zb_err and dt_err:
            return {"available": False, "prev_date": prev}
        p_zt = int(zt_total or len(zt_rows))
        p_zb = int(zb_total or len(zb_rows))
        p_dt = int(dt_total or len(dt_rows))
        p_denom = p_zt + p_zb
        p_br = round(p_zb / p_denom * 100.0, 2) if p_denom else None
        p_max = max((int(r.get("board_days") or 1) for r in zt_rows), default=0)
        p_ge2 = sum(1 for r in zt_rows if int(r.get("board_days") or 1) >= 2)
        cur_br = cur.get("break_rate_pct")
        return {
            "available": True,
            "prev_date": prev,
            "prev_limit_up": p_zt,
            "prev_broken": p_zb,
            "prev_break_rate_pct": p_br,
            "prev_limit_down": p_dt,
            "prev_max_streak": p_max,
            "prev_ge2": p_ge2,
            "delta_limit_up": cur["limit_up"] - p_zt,
            "delta_broken": cur["broken"] - p_zb,
            "delta_break_rate_pp": round(cur_br - p_br, 2) if (cur_br is not None and p_br is not None) else None,
            "delta_limit_down": cur["limit_down"] - p_dt,
            "delta_max_streak": cur["max_streak"] - p_max,
            "delta_ge2": cur["ge2"] - p_ge2,
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("[盘后复盘] 环比计算失败: %s", exc)
        return {"available": False, "reason": f"{type(exc).__name__}"}


def _indices_flow() -> dict:
    """核心指数涨跌幅 + 主力净流入（复用 signal_summary 已落库块，没有再抓一次）。"""
    try:
        from src.db.models import SignalSummaryDaily
        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            row = (
                db.query(SignalSummaryDaily)
                .filter(SignalSummaryDaily.stock_market == "CN")
                .order_by(SignalSummaryDaily.snapshot_date.desc())
                .first()
            )
            if row and isinstance(row.blocks, dict):
                blk = row.blocks.get("indices_flow") or {}
                if blk.get("indices"):
                    return {
                        "available": True,
                        "indices": list(blk.get("indices") or []),
                        "main_flow": blk.get("main_flow") or "",
                        "as_of": row.snapshot_date,
                    }
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        import asyncio
        from src.core.report_generator import _collect_indices, _collect_market_flow

        indices = asyncio.run(_collect_indices()) or []
        flow = asyncio.run(_collect_market_flow()) or {}
        idx_parts = [
            f"{it.get('name','?')}{it.get('change_pct'):+.2f}%"
            for it in indices[:4]
            if isinstance(it.get("change_pct"), (int, float))
        ]
        net = flow.get("total_main_flow") or flow.get("net_inflow")
        flow_s = f"主力净流入{net:+.0f}亿" if isinstance(net, (int, float)) else ""
        if idx_parts:
            return {"available": True, "indices": idx_parts, "main_flow": flow_s}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[盘后复盘] 指数块失败: %s", exc)
    return {"available": False}


def _mainline(zt_rows: list[dict], top_n: int = 6) -> dict:
    """主线题材：按涨停股行业聚合家数，并与连板高度交叉给出龙头。"""
    if not zt_rows:
        return {"top_sectors": [], "leaders": []}
    ind = Counter(r.get("industry") or "其他" for r in zt_rows)
    top = [{"name": k, "count": v} for k, v in ind.most_common(top_n)]
    leaders = []
    for sec in top[:4]:
        members = [r for r in zt_rows if (r.get("industry") or "其他") == sec["name"]]
        members.sort(key=lambda r: (-int(r.get("board_days") or 1), r.get("first_seal") or "99:99:99"))
        if members:
            m = members[0]
            leaders.append(
                {
                    "sector": sec["name"],
                    "symbol": m["symbol"],
                    "name": m["name"],
                    "board_days": int(m.get("board_days") or 1),
                    "first_seal": m.get("first_seal") or "",
                }
            )
    return {"top_sectors": top, "leaders": leaders}


def _emotion(date_str: str, metrics: dict) -> dict:
    """情绪周期定位：复用 market_phase 的 6 阶段判定（冰点/启动/主升/高潮/退潮/修复）。"""
    try:
        from src.core.market_phase import PHASE_LABELS, classify_phase_series_full

        rows = [{"trade_date": date_str, **metrics}]
        # 需要历史序列才有连续性判定；先尝试从库内取最近 30 日驱动量
        try:
            from src.core.market_phase import _read_events, metrics_rows_from_events

            events = _read_events()
            dates = sorted({str(e.get("trade_date")) for e in events if e.get("trade_date")})[-30:]
            if dates:
                rows = metrics_rows_from_events(dates, events)
                if rows and rows[-1].get("trade_date") != date_str:
                    rows.append({"trade_date": date_str, **metrics})
        except Exception:  # noqa: BLE001
            pass
        out = classify_phase_series_full(rows)
        last = out[-1] if out else {}
        phase = last.get("phase") or ""
        return {
            "available": bool(phase),
            "phase": phase,
            "phase_label": PHASE_LABELS.get(phase, phase or "未知"),
            "days_in_phase": last.get("days") or last.get("span") or None,
            "confidence": "ok" if len(rows) >= 5 else "low",
            "basis_days": len(rows),
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("[盘后复盘] 情绪周期失败: %s", exc)
        return {"available": False, "reason": f"{type(exc).__name__}"}


def build_replay(date_str: str | None = None) -> dict:
    """装配某一交易日的完整盘后复盘证据（纯取数，不落库、不做 LLM 归纳）。"""
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    date_str = str(date_str).replace("-", "").strip()[:8]

    zt_rows, zt_err, zt_total = _fetch_pool("zt", date_str)
    zb_rows, zb_err, zb_total = _fetch_pool("zb", date_str)
    dt_rows, dt_err, dt_total = _fetch_pool("dt", date_str)

    zt_n = int(zt_total or len(zt_rows))
    zb_n = int(zb_total or len(zb_rows))
    dt_n = int(dt_total or len(dt_rows))
    denom = zt_n + zb_n

    ladder = _ladder_counts(zt_rows)
    first_board = sum(1 for r in zt_rows if int(r.get("board_days") or 1) == 1)
    ge2 = sum(1 for r in zt_rows if int(r.get("board_days") or 1) >= 2)
    ge3 = sum(1 for r in zt_rows if int(r.get("board_days") or 1) >= 3)
    max_streak = max((int(r.get("board_days") or 1) for r in zt_rows), default=0)
    one_word = sum(
        1
        for r in zt_rows
        if r.get("first_seal") == "09:25:00" and int(r.get("open_times") or 0) == 0
    )

    tq = _tq_crosscheck(date_str)
    promo = _promotion(date_str, zt_rows)
    mainline = _mainline(zt_rows)
    yoy = _prev_comparison(date_str, {
        "limit_up": zt_n, "broken": zb_n, "break_rate_pct": round(zb_n / denom * 100.0, 2) if denom else None,
        "limit_down": dt_n, "max_streak": max_streak, "ge2": ge2,
    })
    indices = _indices_flow()

    metrics = {
        "first_board": first_board,
        "ge2_count": ge2,
        "ge3_count": ge3,
        "max_height": max_streak,
        "promo_rate": (promo.get("promo_rate_pct") or 0) / 100.0 if promo.get("available") else None,
        "seal_rate": (zt_n / denom) if denom else None,
    }
    emotion = _emotion(date_str, metrics)

    quality: dict[str, str] = {}
    quality["涨停池"] = "missing" if zt_err else "ok"
    quality["炸板池"] = "missing" if zb_err else "ok"
    quality["跌停池"] = "missing" if dt_err else "ok"
    quality["TQ交叉校验"] = "ok" if tq.get("available") else "missing"
    quality["晋级率"] = "ok" if promo.get("available") else "missing"
    quality["情绪周期"] = "ok" if emotion.get("available") else "missing"

    replay = {
        "as_of": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE_EM,
        "limit_up": {
            "caliber": CALIBER_EM,
            "total": zt_n,
            "returned": len(zt_rows),
            "max_streak": max_streak,
            "ladder": ladder,
            "first_board": first_board,
            "ge2": ge2,
            "ge3": ge3,
            "one_word": one_word,
            "leaders": sorted(
                zt_rows, key=lambda r: (-int(r.get("board_days") or 1), r.get("first_seal") or "99")
            )[:8],
            "rows": sorted(
                zt_rows, key=lambda r: (-int(r.get("board_days") or 1), r.get("first_seal") or "99")
            )[:MAX_ROWS],
        },
        "broken": {
            "caliber": CALIBER_EM,
            "total": zb_n,
            "returned": len(zb_rows),
            "break_rate_pct": round(zb_n / denom * 100.0, 2) if denom else None,
            "seal_rate_pct": round(zt_n / denom * 100.0, 2) if denom else None,
            "rows": sorted(zb_rows, key=lambda r: -abs(_f(r.get("pct"))))[:MAX_ROWS],
        },
        "limit_down": {
            "caliber": CALIBER_EM,
            "total": dt_n,
            "returned": len(dt_rows),
            "max_days": max((int(r.get("board_days") or 1) for r in dt_rows), default=0),
            "rows": sorted(dt_rows, key=lambda r: _f(r.get("pct")))[:MAX_ROWS],
        },
        "promotion": promo,
        "mainline": mainline,
        "emotion": emotion,
        "prev_comparison": yoy,
        "indices_flow": indices,
        "cross_check": {
            "eastmoney_zt": zt_n,
            "tq_pool": tq.get("total"),
            "tq_max_streak": tq.get("max_streak"),
            "diff": (int(tq.get("total") or 0) - zt_n) if tq.get("available") else None,
            "note": (
                "TQ 走本地客户端全 A 日线自算，含北交所；东财涨停池不含北交所 → 家数天然有差，"
                "两口径分别标注、禁止混算。"
            ),
            "tq_detail": tq,
        },
        "quality": quality,
    }
    return replay


# ─────────────────────────── 落库 ───────────────────────────


def to_row(replay: dict) -> dict:
    """把 replay 压成 `market_replay_daily` 的标量列 + payload。"""
    lu = replay.get("limit_up") or {}
    bk = replay.get("broken") or {}
    dt = replay.get("limit_down") or {}
    pr = replay.get("promotion") or {}
    em = replay.get("emotion") or {}
    ml = replay.get("mainline") or {}
    q = replay.get("quality") or {}
    missing = [k for k, v in q.items() if v != "ok"]
    return {
        "trade_date": replay["as_of"],
        "market": "CN",
        "limit_up_count": int(lu.get("total") or 0),
        "limit_up_max_streak": int(lu.get("max_streak") or 0),
        "first_board_count": int(lu.get("first_board") or 0),
        "ge2_count": int(lu.get("ge2") or 0),
        "ge3_count": int(lu.get("ge3") or 0),
        "one_word_count": int(lu.get("one_word") or 0),
        "broken_count": int(bk.get("total") or 0),
        "break_rate_pct": bk.get("break_rate_pct"),
        "seal_rate_pct": bk.get("seal_rate_pct"),
        "limit_down_count": int(dt.get("total") or 0),
        "limit_down_max_days": int(dt.get("max_days") or 0),
        "promo_rate_pct": pr.get("promo_rate_pct"),
        "phase": em.get("phase"),
        "phase_label": em.get("phase_label"),
        "mainline": ml,
        "quality": "ok" if not missing else ("partial" if len(missing) < len(q) else "missing"),
        "missing_dims": missing,
        "source": replay.get("source"),
        "payload": replay,
    }


def persist(db: Session, replay: dict) -> int:
    """幂等写入 `market_replay_daily`（按 trade_date+market 唯一）。"""
    from sqlalchemy import text

    from src.core.db_safe import safe_rollback
    from src.db.models import MarketReplayDaily

    row = to_row(replay)
    try:
        obj = (
            db.query(MarketReplayDaily)
            .filter(
                MarketReplayDaily.trade_date == row["trade_date"],
                MarketReplayDaily.market == row["market"],
            )
            .first()
        )
        if obj is None:
            obj = MarketReplayDaily(**row)
            db.add(obj)
        else:
            for k, v in row.items():
                setattr(obj, k, v)
        db.commit()
        return 1
    except Exception as exc:  # noqa: BLE001
        safe_rollback(db)
        logger.warning("[盘后复盘] 落库失败(表未迁移?): %s", exc)
        # 兜底：直接 SQL upsert（迁移已跑但 ORM 未同步的冷启动窗口）
        try:
            db.execute(
                text(
                    "INSERT INTO market_replay_daily (trade_date, market, limit_up_count, "
                    "limit_up_max_streak, first_board_count, ge2_count, ge3_count, one_word_count, "
                    "broken_count, break_rate_pct, seal_rate_pct, limit_down_count, "
                    "limit_down_max_days, promo_rate_pct, phase, phase_label, mainline, quality, "
                    "missing_dims, source, payload, created_at) VALUES (:trade_date, :market, "
                    ":limit_up_count, :limit_up_max_streak, :first_board_count, :ge2_count, "
                    ":ge3_count, :one_word_count, :broken_count, :break_rate_pct, :seal_rate_pct, "
                    ":limit_down_count, :limit_down_max_days, :promo_rate_pct, :phase, :phase_label, "
                    ":mainline, :quality, :missing_dims, :source, :payload, now()) "
                    "ON CONFLICT (trade_date, market) DO UPDATE SET limit_up_count=EXCLUDED.limit_up_count"
                ),
                {
                    **row,
                    "mainline": __import__("json").dumps(row["mainline"], ensure_ascii=False),
                    "missing_dims": __import__("json").dumps(row["missing_dims"], ensure_ascii=False),
                    "payload": __import__("json").dumps(row["payload"], ensure_ascii=False, default=str),
                },
            )
            db.commit()
            return 1
        except Exception as exc2:  # noqa: BLE001
            safe_rollback(db)
            logger.warning("[盘后复盘] SQL 兜底落库也失败: %s", exc2)
            return 0


def read_latest(db: Session, date_str: str | None = None) -> dict | None:
    """从库里读最近一条复盘快照（供参谋秒读，不重新抓外部接口）。"""
    from src.core.db_safe import safe_rollback
    from src.db.models import MarketReplayDaily

    try:
        q = db.query(MarketReplayDaily)
        if date_str:
            q = q.filter(MarketReplayDaily.trade_date == str(date_str).replace("-", ""))
        obj = q.order_by(MarketReplayDaily.trade_date.desc()).first()
        return obj.payload if obj and obj.payload else None
    except Exception as exc:  # noqa: BLE001
        safe_rollback(db)
        logger.debug("[盘后复盘] 读库失败: %s", exc)
        return None


# ─────────────────────────── 渲染 ───────────────────────────


def render(replay: dict) -> str:
    """渲染成**要点化**证据文本（数字前置、一行一条、供 LLM 直接抄，不用它做归纳）。"""
    if not replay:
        return "[盘后复盘] 无数据。"

    lu = replay.get("limit_up") or {}
    bk = replay.get("broken") or {}
    dt = replay.get("limit_down") or {}
    pr = replay.get("promotion") or {}
    ml = replay.get("mainline") or {}
    em = replay.get("emotion") or {}
    cc = replay.get("cross_check") or {}
    q = replay.get("quality") or {}

    lines: list[str] = [
        f"【盘后复盘证据】{replay.get('as_of')} · 口径 {lu.get('caliber')} / {cc.get('note', '')[:40]}"
    ]

    yoy = replay.get("prev_comparison") or {}
    idx = replay.get("indices_flow") or {}
    if idx.get("available"):
        lines.append(
            "■ 指数与主力：" + "、".join(idx.get("indices") or [])
            + (f" ｜ {idx.get('main_flow')}" if idx.get("main_flow") else "")
        )
    if yoy.get("available"):
        d_br = yoy.get("delta_break_rate_pp")
        br_s = f"{d_br:+.2f}pp" if d_br is not None else "—"
        lines.append(
            f"■ 较上一交易日({yoy.get('prev_date')})环比："
            f"涨停 {yoy.get('delta_limit_up'):+d}家（昨{yoy.get('prev_limit_up')}）｜ "
            f"炸板率 {br_s}（昨{yoy.get('prev_break_rate_pct')}%）｜ "
            f"跌停 {yoy.get('delta_limit_down'):+d}家（昨{yoy.get('prev_limit_down')}）｜ "
            f"最高板 {yoy.get('delta_max_streak'):+d}板（昨{yoy.get('prev_max_streak')}）｜ "
            f"2板+ {yoy.get('delta_ge2'):+d}家（昨{yoy.get('prev_ge2')}）"
        )

    # 1) 涨停结构
    ladder_parts = [f"{k}板{v}家" for k, v in (lu.get("ladder") or {}).items()]
    lines.append(
        f"■ 涨停 {lu.get('total')} 家 ｜ 最高 {lu.get('max_streak')} 板 ｜ "
        f"首板 {lu.get('first_board')} 家 ｜ 2板+ {lu.get('ge2')} 家 ｜ 3板+ {lu.get('ge3')} 家 ｜ "
        f"一字 {lu.get('one_word')} 家"
    )
    if ladder_parts:
        lines.append("■ 连板梯队：" + " / ".join(ladder_parts))
    for r in (lu.get("leaders") or [])[:6]:
        lines.append(
            f"  · {r.get('name')}({r.get('symbol')}) {r.get('board_days')}板 "
            f"首封{r.get('first_seal') or '—'} 封单{int(r.get('seal_amount_wan') or 0)}万 "
            f"炸板{r.get('open_times')}次 {r.get('industry')}"
        )

    # 2) 炸板 / 封板
    if bk.get("break_rate_pct") is not None:
        lines.append(
            f"■ 炸板 {bk.get('total')} 家 ｜ 炸板率 {bk.get('break_rate_pct')}% ｜ "
            f"封板率 {bk.get('seal_rate_pct')}%（分母=涨停+炸板，同源同日）"
        )
    else:
        lines.append(f"■ 炸板池 {q.get('炸板池', 'missing')}（炸板率暂无）")
    for r in (bk.get("rows") or [])[:4]:
        lines.append(
            f"  · 炸板 {r.get('name')}({r.get('symbol')}) 现{r.get('pct')}% "
            f"炸{r.get('open_times')}次 首封{r.get('first_seal') or '—'} {r.get('industry')}"
        )

    # 3) 跌停
    if dt.get("total"):
        lines.append(f"■ 跌停 {dt.get('total')} 家 ｜ 最长连续 {dt.get('max_days')} 天")
        for r in (dt.get("rows") or [])[:4]:
            lines.append(
                f"  · 跌停 {r.get('name')}({r.get('symbol')}) {r.get('pct')}% "
                f"续{r.get('board_days')}天 封单{int(r.get('seal_amount_wan') or 0)}万 {r.get('industry')}"
            )
    else:
        lines.append(f"■ 跌停 {dt.get('total', 0)} 家（{q.get('跌停池', 'missing')}）")

    # 4) 晋级率
    if pr.get("available"):
        lines.append(
            f"■ 晋级率 {pr.get('promo_rate_pct')}%（{pr.get('prev_date')} 涨停 {pr.get('prev_count')} 家 "
            f"→ 今日续板 {pr.get('promoted')} 家）"
        )
    else:
        lines.append(f"■ 晋级率 暂无（{pr.get('reason') or '缺昨日池'}）")

    # 5) 主线
    if ml.get("top_sectors"):
        sec = " / ".join(f"{s['name']}{s['count']}家" for s in ml["top_sectors"][:6])
        lines.append(f"■ 涨停行业分布：{sec}")
        for ld in (ml.get("leaders") or [])[:4]:
            lines.append(
                f"  · {ld['sector']}龙头 {ld['name']}({ld['symbol']}) {ld['board_days']}板 "
                f"首封{ld.get('first_seal') or '—'}"
            )

    # 6) 情绪周期
    if em.get("available"):
        lines.append(
            f"■ 情绪周期：{em.get('phase_label')}（{em.get('phase')}，"
            f"置信度{em.get('confidence')}，依据{em.get('basis_days')}日驱动量）"
        )
    else:
        lines.append("■ 情绪周期：暂无（驱动量不足）")

    # 7) 口径与缺口
    lines.append(
        f"■ 交叉校验：东财涨停 {cc.get('eastmoney_zt')} 家 vs TQ {cc.get('tq_pool')} 家 "
        f"(差 {cc.get('diff')}) —— {cc.get('note')}"
    )
    missing = [k for k, v in q.items() if v != "ok"]
    lines.append("■ 数据缺口：" + ("无" if not missing else "、".join(missing)))
    return "\n".join(lines)


def run_scheduled(date_str: str | None = None) -> dict:
    """定时任务入口（交易日 15:50）：抓池 → 落库。"""
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        replay = build_replay(date_str)
        n = persist(db, replay)
        lu = replay.get("limit_up") or {}
        bk = replay.get("broken") or {}
        dt = replay.get("limit_down") or {}
        return {
            "ok": n > 0,
            "written": n,
            "as_of": replay.get("as_of"),
            "limit_up": lu.get("total"),
            "max_streak": lu.get("max_streak"),
            "broken": bk.get("total"),
            "break_rate_pct": bk.get("break_rate_pct"),
            "limit_down": dt.get("total"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("[盘后复盘] 定时任务失败: %s", exc)
        return {"ok": False, "reason": str(exc)[:160]}
    finally:
        db.close()
