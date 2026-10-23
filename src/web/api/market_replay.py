"""盘后复盘三池序列 + 连板晋级树 API(创新 1, 2026-10-24)。

数据源: `market_replay_daily`(Migration 222, 交易日 15:50 东财三池同源落库),
只读已落库快照, **不在请求路径里重新抓东财**(抓取归 `src/core/market_replay.run_scheduled`)。

端点(挂 /api/market/replay, data_read 双轨: 用户 JWT 或服务 token —— 行情类纯读数据,
与 /api/quotes /api/klines 同一套收口, 不含任何用户数据):

    GET /api/market/replay/series?days=20            → 近 N 个交易日三池标量序列(升序)
    GET /api/market/replay/series?from=&to=          → 按日期区间(YYYYMMDD, 闭区间)
    GET /api/market/replay/tree?date=20261024        → 单日连板梯队晋级树(缺省最新一期)

口径红线(交接备注 §5 / specs REPTREE-*, 违反即打回):
- 涨停/炸板/跌停/晋级率全部来自东财三池(`eastmoney_zt`, 不含北交所)同源同日;
  TQ 全 A 自算(`tq_pool`)只作交叉校验, 同屏各自带标签, 禁止跨源混算
  (v0.48 实测东财 82 vs TQ 86, 差 4 家北交所)。
- 缺数据日期显式 `available=false`, **绝不补 0**(0 是有效观测值, REPLAY-002 同款红线)。
- 晋级率三件套: 样本数(昨日涨停家数)必带; **同池无条件基准表内未存 → 显式口径缺口**;
  **未剔除 T+1 一字涨停 → 显式标注**(当日一字板家数旁证给出)。
- 封单额/流通市值比表内未存 → 显式 null, 不做组件间换算编造(逐股封单额原值照实下发)。

环比口径说明: series 的环比 = **表内相邻交易日差分**(前一日快照与当日同源同池,
差分结果与 payload.prev_comparison 同义); 树端点直接下发 payload 内存的权威环比。
"""
from __future__ import annotations

import logging
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.core.db_safe import safe_rollback

logger = logging.getLogger(__name__)

router = APIRouter()

CALIBER_EM = "eastmoney_zt"
CALIBER_TQ = "tq_pool"
# 明细截断上限与 src/core/market_replay.MAX_ROWS 对齐(payload.rows 只存 Top40)
DETAIL_MAX_ROWS = 40
# from/to 区间模式下最多返回的行数(防 Deep-History 大查询)
RANGE_HARD_CAP = 250

# 三池基准缺口说明(表内未存同池无条件基准 —— 不编造, 显式标注)
_BASELINE_NOTE = (
    "表内未存同池无条件基准(口径缺口): market_replay_daily 只有整体晋级率, "
    "没有「昨日全部涨停股今日任意涨幅」的无条件对照列"
)
# T+1 一字涨停未剔除说明(口径缺口)
_ONE_WORD_NOTE = (
    "未剔除 T+1 一字涨停(口径缺口): 晋级率分子含一字续板样本, "
    "当日一字板家数见 one_word_count 旁证"
)


def _iso(d: Any) -> str:
    """YYYYMMDD → YYYY-MM-DD(无法解析时原样返回, 不抛)。"""
    s = str(d or "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _norm_date(raw: str | None) -> str | None:
    """接受 YYYYMMDD / YYYY-MM-DD, 归一成 YYYYMMDD; 非法返回 None。"""
    if not raw:
        return None
    s = str(raw).replace("-", "").strip()
    if len(s) == 8 and s.isdigit():
        return s
    return None


def _parse_d(s: str) -> _date | None:
    try:
        return datetime.strptime(str(s), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _row_to_item(row: Any, prev_row: Any) -> dict:
    """一行快照 → 序列项。prev_row 是表内前一日行(可能 None), 且需日历确认相邻才差分。"""
    lu = int(row.limit_up_count or 0)
    zb = int(row.broken_count or 0)
    dt = int(row.limit_down_count or 0)
    br = row.break_rate_pct

    # 环比: 只有前一行**恰好是上一交易日**才差分; 中间缺期 → 显式 null, 不硬算
    deltas: dict[str, Any] = {
        "delta_limit_up": None,
        "delta_broken": None,
        "delta_break_rate_pp": None,
        "delta_limit_down": None,
        "delta_max_streak": None,
        "delta_ge2": None,
        "prev_date": None,
    }
    promo_prev_count: int | None = None
    if prev_row is not None and _prev_adjacent(prev_row.trade_date, row.trade_date):
        p_br = prev_row.break_rate_pct
        deltas = {
            "delta_limit_up": lu - int(prev_row.limit_up_count or 0),
            "delta_broken": zb - int(prev_row.broken_count or 0),
            "delta_break_rate_pp": (
                round(br - p_br, 2) if (br is not None and p_br is not None) else None
            ),
            "delta_limit_down": dt - int(prev_row.limit_down_count or 0),
            "delta_max_streak": int(row.limit_up_max_streak or 0)
            - int(prev_row.limit_up_max_streak or 0),
            "delta_ge2": int(row.ge2_count or 0) - int(prev_row.ge2_count or 0),
            "prev_date": prev_row.trade_date,
        }
        # 晋级率样本数 = 昨日涨停家数(同口径: 东财池当日涨停总数)
        promo_prev_count = int(prev_row.limit_up_count or 0)

    return {
        "trade_date": row.trade_date,
        "trade_date_iso": _iso(row.trade_date),
        "available": True,
        "limit_up_count": lu,
        "broken_count": zb,
        "limit_down_count": dt,
        "break_rate_pct": br,
        "seal_rate_pct": row.seal_rate_pct,
        "promo_rate_pct": row.promo_rate_pct,
        "promo_prev_count": promo_prev_count,
        "max_streak": int(row.limit_up_max_streak or 0),
        "first_board_count": int(row.first_board_count or 0),
        "ge2_count": int(row.ge2_count or 0),
        "ge3_count": int(row.ge3_count or 0),
        "one_word_count": int(row.one_word_count or 0),
        "phase": row.phase,
        "phase_label": row.phase_label,
        "quality": row.quality,
        "source": row.source,
        "caliber": CALIBER_EM,
        **deltas,
    }


def _missing_item(trade_date: str) -> dict:
    """缺数据交易日: 显式 available=false, 数值字段一律 null(绝不补 0)。"""
    return {
        "trade_date": trade_date,
        "trade_date_iso": _iso(trade_date),
        "available": False,
        "limit_up_count": None,
        "broken_count": None,
        "limit_down_count": None,
        "break_rate_pct": None,
        "seal_rate_pct": None,
        "promo_rate_pct": None,
        "promo_prev_count": None,
        "max_streak": None,
        "first_board_count": None,
        "ge2_count": None,
        "ge3_count": None,
        "one_word_count": None,
        "phase": None,
        "phase_label": None,
        "quality": None,
        "source": None,
        "caliber": None,
        "delta_limit_up": None,
        "delta_broken": None,
        "delta_break_rate_pp": None,
        "delta_limit_down": None,
        "delta_max_streak": None,
        "delta_ge2": None,
        "prev_date": None,
    }


def _prev_trading_day_safe(d: _date) -> _date | None:
    """交易日历 prev_trading_day 的安全包装; 静态表未覆盖年份返回 None(不猜)。"""
    try:
        from src.core.trading_calendar import prev_trading_day

        return prev_trading_day(d)
    except Exception as exc:  # noqa: BLE001 — TradingCalendarError 等, 显式降级
        logger.debug("[复盘序列] 交易日历不可用: %s", exc)
        return None


def _is_trading_day_safe(d: _date) -> bool | None:
    """交易日判定的安全包装; 静态表未覆盖年份返回 None(不猜)。"""
    try:
        from src.core.trading_calendar import is_trading_day

        return is_trading_day(d)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[复盘序列] 交易日历不可用: %s", exc)
        return None


def _prev_adjacent(prev_date: Any, cur_date: Any) -> bool:
    """prev_date 是否恰好是 cur_date 的上一交易日(决定能否差分)。"""
    d_cur = _parse_d(str(cur_date))
    d_prev = _parse_d(str(prev_date))
    if d_cur is None or d_prev is None:
        return False
    p = _prev_trading_day_safe(d_cur)
    return p is not None and p == d_prev


def _trading_days_in_range(d_from: str, d_to: str) -> list[str] | None:
    """from/to 区间内的交易日列表(闭区间, 末尾截到 RANGE_HARD_CAP); 日历不可用 → None。"""
    start = _parse_d(d_from)
    end = _parse_d(d_to)
    if start is None or end is None:
        return None
    out: list[str] = []
    cur = start
    while cur <= end:
        td = _is_trading_day_safe(cur)
        if td is None:
            return None
        if td:
            out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out[-RANGE_HARD_CAP:]


def _expected_days_ending_latest(rows_asc: list, n: int) -> list[str] | None:
    """以最新落库日为终点, 往回数 n 个交易日; 同时必须覆盖最早一条已落库行
    (否则那行会被占位重建静默丢掉)。日历不可用 → None(不做占位, 只返回已落库行)。
    """
    if not rows_asc:
        return None
    latest = _parse_d(str(rows_asc[-1].trade_date))
    earliest = _parse_d(str(rows_asc[0].trade_date))
    if latest is None or earliest is None:
        return None
    # 降序收集: [latest, ...] 直到(凑满 n 个 且 覆盖最早落库行)
    days_desc: list[_date] = [latest]
    cur = latest
    while len(days_desc) < max(n, 1) or cur > earliest:
        p = _prev_trading_day_safe(cur)
        if p is None:
            return None
        days_desc.append(p)
        cur = p
        if len(days_desc) >= max(n, 1) and cur <= earliest:
            break
    out = [d.strftime("%Y%m%d") for d in reversed(days_desc)]
    return out[-RANGE_HARD_CAP:]


@router.get("/series")
def replay_series(
    days: int = Query(20, ge=1, le=250),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
):
    """近 N 个交易日(或 from/to 闭区间)的三池复盘标量序列, 按日期升序。

    - 缺数据交易日补 `available=false` 占位(仅当交易日历可判定时), 数值全 null;
    - 环比 = 表内相邻交易日差分, 前一日行缺失/不连续 → null(不硬算);
    - 梯队标量(首板/2板+/3板+)直接来自表列; 完整梯队 dict 走 /tree 端点(payload)。
    """
    from src.web.database import SessionLocal

    d_from = _norm_date(date_from)
    d_to = _norm_date(date_to)
    if (date_from and not d_from) or (date_to and not d_to):
        raise HTTPException(400, "日期格式非法: 只接受 YYYYMMDD 或 YYYY-MM-DD")
    if d_from and d_to and d_from > d_to:
        raise HTTPException(400, "from 晚于 to")

    ranged = bool(d_from or d_to)
    limit_rows = RANGE_HARD_CAP if ranged else days

    db = SessionLocal()
    try:
        from src.db.models import MarketReplayDaily

        q = db.query(MarketReplayDaily).filter(MarketReplayDaily.market == "CN")
        if d_from:
            q = q.filter(MarketReplayDaily.trade_date >= d_from)
        if d_to:
            q = q.filter(MarketReplayDaily.trade_date <= d_to)
        # days 模式多取 1 行做首行环比锚点; 区间模式另取窗口前 1 行, 主查询不加 1
        fetch_n = limit_rows if ranged else limit_rows + 1
        rows_desc = q.order_by(MarketReplayDaily.trade_date.desc()).limit(fetch_n).all()
        anchor = None
        if not ranged and len(rows_desc) > days:
            anchor = rows_desc[-1]
            rows_desc = rows_desc[:-1]
        if ranged:
            aq = (
                db.query(MarketReplayDaily)
                .filter(
                    MarketReplayDaily.market == "CN",
                    MarketReplayDaily.trade_date < d_from,
                )
                .order_by(MarketReplayDaily.trade_date.desc())
                .first()
            )
            anchor = aq
    finally:
        db.close()

    rows_asc = list(reversed(rows_desc))
    items: list[dict] = []
    for i, r in enumerate(rows_asc):
        prev = rows_asc[i - 1] if i > 0 else anchor
        items.append(_row_to_item(r, prev))

    # 缺数据交易日补占位(交易日历可判定时); 占位窗口必须覆盖全部已落库行
    note = None
    if d_from and d_to:
        expected = _trading_days_in_range(d_from, d_to)
        if expected is None:
            note = "交易日历未覆盖该区间, 无法标记缺失交易日(仅返回已落库日期)"
    else:
        expected = _expected_days_ending_latest(rows_asc, days)
        if expected is None and rows_asc:
            note = "交易日历未覆盖, 无法标记缺失交易日(仅返回已落库日期)"
    if expected:
        by_date = {it["trade_date"]: it for it in items}
        items = [by_date.get(d) or _missing_item(d) for d in expected]

    return {
        "caliber": CALIBER_EM,
        "caliber_note": "东财三池同源同日(不含北交所); 与 TQ 全A自算口径并存不混算",
        "days": days,
        "count": len(items),
        "items": items,
        "note": note,
    }


@router.get("/tree")
def replay_tree(date: str | None = Query(None)):
    """单日连板梯队晋级树(缺省最新一期)。

    - 梯队层级/成员明细来自 `payload`(采集时截断 Top40, 按 连板高度↓ + 首封时间↑);
      payload 缺失时层级不可得 → `ladder_available=false`, 不用标量反推编造;
    - 晋级率三件套: 样本数 / 同池基准(缺口) / 一字板剔除(缺口) 全部显式;
    - 分层晋级率(如 首板→2板 晋级率)表内未存 → 不计算, 显式口径缺口。
    """
    from src.web.database import SessionLocal

    d = _norm_date(date)

    db = SessionLocal()
    try:
        from src.db.models import MarketReplayDaily

        q = db.query(MarketReplayDaily).filter(MarketReplayDaily.market == "CN")
        if d:
            q = q.filter(MarketReplayDaily.trade_date == d)
        row = q.order_by(MarketReplayDaily.trade_date.desc()).first()
        latest = (
            db.query(MarketReplayDaily.trade_date)
            .filter(MarketReplayDaily.market == "CN")
            .order_by(MarketReplayDaily.trade_date.desc())
            .first()
        )
    except Exception as exc:  # noqa: BLE001 — 表未迁移等, 显式 503 而非 500 堆栈
        safe_rollback(db)
        raise HTTPException(503, f"market_replay_daily 不可读: {exc}") from exc
    finally:
        db.close()

    latest_date = latest[0] if latest else None
    if row is None:
        return {
            "available": False,
            "note": (
                f"该日期无复盘快照({date})" if d else "暂无盘后复盘快照(交易日 15:50 落库)"
            ),
            "latest_date": latest_date,
            "latest_date_iso": _iso(latest_date or ""),
            "caliber": CALIBER_EM,
        }

    payload = row.payload if isinstance(row.payload, dict) else {}
    lu = payload.get("limit_up") or {}
    ladder_raw = lu.get("ladder") or {}
    detail_rows = lu.get("rows") or []
    ladder_available = bool(ladder_raw)

    # 层级: 高板在前; 成员从 payload 明细里按 board_days 过滤(受 Top40 截断 → partial 标注)
    levels: list[dict] = []
    try:
        board_keys = sorted({int(k) for k in ladder_raw}, reverse=True)
    except (TypeError, ValueError):
        board_keys = []
    detail_returned = int(lu.get("returned") or len(detail_rows) or 0)
    total_up = int(row.limit_up_count or 0)
    detail_partial = detail_returned < total_up
    for b in board_keys:
        cnt = int(ladder_raw.get(str(b)) or ladder_raw.get(b) or 0)
        members = [
            {
                "symbol": r.get("symbol"),
                "name": r.get("name"),
                "board_days": int(r.get("board_days") or 1),
                "pct": r.get("pct"),
                "price": r.get("price"),
                "seal_amount_wan": r.get("seal_amount_wan"),
                "first_seal": r.get("first_seal"),
                "open_times": r.get("open_times"),
                "industry": r.get("industry"),
                "circ_mv_yi": r.get("circ_mv_yi"),
            }
            for r in detail_rows
            if int(r.get("board_days") or 1) == b
        ]
        levels.append(
            {
                "board": b,
                "count": cnt,
                "members": members,
                "members_truncated": detail_partial and len(members) < cnt,
            }
        )

    # 晋级率三件套(晋级率取 payload 证据值, 标量列兜底 —— to_row 落库时两者同源)
    promo_src = payload.get("promotion") or {}
    promo_rate = row.promo_rate_pct
    if promo_src.get("available") and promo_src.get("promo_rate_pct") is not None:
        promo_rate = promo_src.get("promo_rate_pct")
    promo_prev_count = promo_src.get("prev_count") if promo_src.get("available") else None
    promoted = promo_src.get("promoted") if promo_src.get("available") else None
    prev_date = promo_src.get("prev_date") if promo_src.get("available") else None
    promotion = {
        "available": promo_rate is not None,
        "promo_rate_pct": promo_rate,
        "prev_date": prev_date,
        "prev_count": promo_prev_count,
        "promoted": promoted,
        "sample_count": promo_prev_count,
        "sample_note": "样本 = 上一交易日东财涨停池家数(同口径同池)",
        "baseline_available": False,
        "baseline_note": _BASELINE_NOTE,
        "excludes_one_word": False,
        "one_word_note": _ONE_WORD_NOTE,
        "one_word_count": int(row.one_word_count or 0),
        "per_level_available": False,
        "per_level_note": (
            "分层晋级率(首板→2板→…)表内未存逐层晋级明细, 不做前端推算 —— "
            "payload 明细受 Top40 截断, 用它算分层晋级率会静默偏小"
        ),
    }

    prev_cmp = payload.get("prev_comparison") or {}
    if not prev_cmp.get("available"):
        prev_cmp = {"available": False}
    emotion = payload.get("emotion") or {}
    cc = payload.get("cross_check") or {}
    tq_detail = cc.get("tq_detail") or {}

    return {
        "available": True,
        "trade_date": row.trade_date,
        "trade_date_iso": _iso(row.trade_date),
        "market": row.market,
        "source": row.source,
        "caliber": CALIBER_EM,
        "quality": row.quality,
        "missing_dims": row.missing_dims if isinstance(row.missing_dims, list) else [],
        # 三池概览(标量列)
        "limit_up_count": total_up,
        "broken_count": int(row.broken_count or 0),
        "break_rate_pct": row.break_rate_pct,
        "seal_rate_pct": row.seal_rate_pct,
        "limit_down_count": int(row.limit_down_count or 0),
        "limit_down_max_days": int(row.limit_down_max_days or 0),
        "max_streak": int(row.limit_up_max_streak or 0),
        "first_board_count": int(row.first_board_count or 0),
        "ge2_count": int(row.ge2_count or 0),
        "ge3_count": int(row.ge3_count or 0),
        # 梯队(payload)
        "ladder_available": ladder_available,
        "levels": levels,
        "detail_returned": detail_returned,
        "detail_partial": detail_partial,
        "detail_note": (
            f"明细为采集时 Top{DETAIL_MAX_ROWS}(连板高度↓+首封时间↑), "
            "首板家数多时低位层级明细不全; 家数以标量列/梯队计数为准"
            if detail_partial
            else None
        ),
        # 晋级率三件套
        "promotion": promotion,
        # 环比(payload 权威值)
        "prev_comparison": prev_cmp,
        # 情绪周期
        "phase": row.phase,
        "phase_label": row.phase_label,
        "emotion_available": bool(row.phase),
        "emotion_basis_days": emotion.get("basis_days"),
        # 交叉校验(同屏双口径, 各自带标签, 禁止混算)
        "cross_check": {
            "eastmoney_zt": cc.get("eastmoney_zt"),
            "tq_pool": cc.get("tq_pool"),
            "tq_max_streak": cc.get("tq_max_streak"),
            "diff": cc.get("diff"),
            "note": cc.get("note"),
            "tq_available": bool(tq_detail.get("available")),
        },
        # 封单额/流通市值比: 表内未存 → 显式无数据(逐股封单额原值在 members 里)
        "seal_mv_ratio": None,
        "seal_mv_ratio_note": "封单额/流通市值比表内未存, 不做换算编造; 逐股封单额见成员明细",
        "updated_at": row.created_at.isoformat() if row.created_at else None,
    }
