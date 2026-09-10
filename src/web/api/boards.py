"""板块数据 API(阶段2.2, v0.3.0): 板块/概念列表 + 板块详情 + 成分股 + 轮动排序。

端点(全部需登录, 挂载前缀 /api/boards):
    GET /api/boards?type=industry|concept   板块/概念列表(DB, 每日 cron 同步)
    GET /api/boards/rotation?days=5         板块轮动排序(compute_rotation)
    GET /api/boards/{block_code}            板块详情(含今日 change_pct/fund_net)
    GET /api/boards/{block_code}/constituents  成分股(thsdk 实时, 1 小时缓存)

路由顺序注意: /rotation 静态路径必须先于 /{block_code} 声明,
否则 FastAPI 会把 "rotation" 当作 block_code 匹配。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from src.core.quote_snapshots import in_trading_window
from src.core import tdx_boards
from src.core.thsdk_board import (
    _extract_block_metrics,
    compute_rotation,
    fetch_block_constituents,
    fetch_block_detail,
    fetch_block_snapshots,
)
from src.web.database import get_db
from src.web.models import Board, BoardDaily

logger = logging.getLogger(__name__)

router = APIRouter(tags=["boards"])

# 允许的板块类型
_VALID_TYPES = ("industry", "concept")

# ── 实时模式(2026-09-10 盘中实时化): 交易时段内热力图 60s 自动刷新 ─────────────
# 仅 "扩展" 档(涨幅/主力净流入/量比/涨速), 面积量能仍用日线成交额(日内相对大小稳定)。
# 60s 服务端缓存 + 单飞(拉取 ~10s, 单 worker 内并发请求只拉一次); 失败静默回落日线。
_LIVE_TTL_S = 60.0
_LIVE_CACHE: dict[str, tuple[float, dict]] = {}
_LIVE_LOCK = threading.Lock()


def _clear_live_cache() -> None:
    """清空实时快照缓存(测试隔离/手动刷新用)。"""
    with _LIVE_LOCK:
        _LIVE_CACHE.clear()
        _TDX_ITEMS_CACHE.clear()


# 通达信全量批量 ~3.5-4.5s(pricevol+AMO/SUPAMO 公式), 60s 内多请求只打一轮
_TDX_ITEMS_TTL_S = 60.0
_TDX_ITEMS_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _live_snapshot(btype: str, codes: list[str]) -> dict | None:
    """60s 缓存的实时快照; 失败返回 None(调用方回落日线, 不报错)。"""
    hit = _LIVE_CACHE.get(btype)
    if hit is not None and time.time() - hit[0] < _LIVE_TTL_S:
        return hit[1]
    with _LIVE_LOCK:  # 单飞: 并发只拉一次, 后到者等锁后取缓存
        hit = _LIVE_CACHE.get(btype)
        if hit is not None and time.time() - hit[0] < _LIVE_TTL_S:
            return hit[1]
        try:
            snaps = fetch_block_snapshots(codes, modes=("扩展",))
        except Exception as e:  # noqa: BLE001 - 实时失败静默回落
            logger.warning("板块实时快照拉取失败(%s): %s", btype, e)
            return None
        if not snaps:
            return None
        _LIVE_CACHE[btype] = (time.time(), snaps)
        return snaps


def _merge_live(items: list[dict], snaps: dict) -> int:
    """实时字段就地覆盖(缺失字段保留日线值); 返回命中数量。"""
    merged = 0
    for it in items:
        s = snaps.get(it.get("block_code"))
        if not s:
            continue
        if s.get("change_pct") is not None:
            it["change_pct"] = s["change_pct"]
        if s.get("fund_net") is not None:
            it["fund_net"] = s["fund_net"]
        it["volume_ratio"] = s.get("volume_ratio")
        it["speed"] = s.get("speed")
        it["live"] = True
        merged += 1
    return merged


# ── 方案B(2026-09-10 老板拍板): 板块数据源 = 通达信客户端原生目录 ─────────────
# 目录 881xxx 二级行业 / 880xxx 概念(与同花顺代码撞号不同义, 不做代码映射);
# 实时 = get_pricevol + AMO(成交额)/SUPAMO(主力资金) 公式批量(全量 ~0.5s, 本地无配额);
# 涨速由自身轮询价差自算(compute_speed), 量比 = 今日额/(前5日均额×时段进度) 代理;
# 数据陈旧/客户端不可用 → 回落 thsdk 老链路(source 字段标注实际来源)。
_SOURCES = ("auto", "tdx", "ths")


def _tdx_items(btype: str, *, live_on: bool) -> list[dict] | None:
    """通达信板块 items; 不可用/数据陈旧/异常 → None(调用方回落 ths)。"""
    if not tdx_boards.data_fresh():
        logger.info("TDX 数据不新鲜, 热力图回落 thsdk")
        return None
    sectors = [s for s in tdx_boards.sector_items() if s["board_type"] == btype]
    if not sectors:
        return None
    codes = [s["code"] for s in sectors]
    quotes = tdx_boards.board_quotes(codes)
    if not any((q or {}).get("price") is not None for q in quotes.values()):
        logger.info("TDX 快照全空, 热力图回落 thsdk")
        return None
    tdx_boards.record_prices(quotes)
    progress = tdx_boards.intraday_progress()
    baseline = tdx_boards.amount_baseline(codes) if live_on else {}
    trade_date = tdx_boards.latest_trade_date()
    items: list[dict] = []
    for s in sectors:
        code = s["code"]
        q = quotes.get(code) or {}
        amount = q.get("amount")
        items.append(
            {
                "block_code": code,
                "name": s["name"] or code,
                "board_type": s["board_type"],
                "change_pct": q.get("change_pct"),
                "fund_net": q.get("fund_net"),
                "volume": amount,
                "date": trade_date,
                "has_daily": q.get("change_pct") is not None,
                "volume_ratio": tdx_boards.volume_ratio_proxy(amount, baseline.get(code), progress)
                if live_on
                else None,
                "speed": tdx_boards.speed_of(code) if live_on else None,
                "live": bool(live_on and q.get("price") is not None),
            }
        )
    return items


def _tdx_items_cached(btype: str, *, live_on: bool) -> list[dict] | None:
    """60s TTL + 单飞: 全量批量 ~4s, 窗口内并发请求只打一轮客户端。"""
    key = f"{btype}:{int(live_on)}"
    hit = _TDX_ITEMS_CACHE.get(key)
    if hit is not None and time.time() - hit[0] < _TDX_ITEMS_TTL_S:
        return list(hit[1])
    with _LIVE_LOCK:
        hit = _TDX_ITEMS_CACHE.get(key)
        if hit is not None and time.time() - hit[0] < _TDX_ITEMS_TTL_S:
            return list(hit[1])
        items = _tdx_items(btype, live_on=live_on)
        if items is None:
            return None
        _TDX_ITEMS_CACHE[key] = (time.time(), list(items))
        return list(items)


@router.get("")
def list_boards(
    type: str = Query("industry", description="industry / concept"),
    db: Session = Depends(get_db),
):
    """板块/概念列表(来自 Boards 表, cron 每日同步)。"""
    btype = (type or "industry").lower()
    if btype not in _VALID_TYPES:
        raise HTTPException(400, f"type 仅支持 {'/'.join(_VALID_TYPES)}")

    try:
        rows = (
            db.query(Board)
            .filter(Board.board_type == btype)
            .order_by(Board.name)
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("查询板块列表失败: %s", e)
        raise HTTPException(502, f"查询板块列表失败: {e}")

    return [
        {
            "block_code": b.block_code,
            "name": b.name or "",
            "board_type": b.board_type or "",
            "last_synced_at": b.last_synced_at.isoformat() if b.last_synced_at else None,
        }
        for b in rows
    ]


@router.get("/rotation")
def board_rotation(
    days: int = Query(5, ge=1, le=30, description="统计窗口天数"),
):
    """板块轮动排序(强度分 0-100 降序)。"""
    try:
        rows = compute_rotation(days=days)
        return {"days": max(1, int(days)), "items": rows}
    except Exception as e:  # noqa: BLE001
        logger.warning("板块轮动接口异常: %s", e)
        raise HTTPException(502, f"板块轮动计算失败: {e}")


@router.get("/heatmap")
def board_heatmap(
    type: str = Query("industry", description="industry / concept"),
    live: str = Query("auto", description="auto=交易时段内自动实时 / 1=强制实时 / 0=仅日线"),
    source: str = Query("auto", description="auto=通达信优先(不可用回落同花顺) / tdx / ths"),
    db: Session = Depends(get_db),
):
    """全板块热力图数据源。

    方案B(2026-09-10 老板拍板): 默认走**通达信客户端原生板块目录**(881xxx 二级行业 /
    880xxx 概念, source=tdx; 全量批量 ~0.5s, 本地无配额), 不可用/数据陈旧 → 回落
    thsdk 老链路(source=ths, 见下)。响应带 source 标注实际来源。

    live 模式: auto 时交易时段内叠加实时(通达信=T+0 快照即实时; thsdk=扩展档批量覆盖);
    非时段或取数失败回落最新收盘(date 字段为准), 不报错。
    """
    btype = (type or "industry").lower()
    if btype not in _VALID_TYPES:
        raise HTTPException(400, f"type 仅支持 {'/'.join(_VALID_TYPES)}")
    live_mode = (live or "auto").lower()
    if live_mode not in ("auto", "1", "0"):
        raise HTTPException(400, "live 仅支持 auto/1/0")
    src = (source or "auto").lower()
    if src not in _SOURCES:
        raise HTTPException(400, f"source 仅支持 {'/'.join(_SOURCES)}")

    use_live = live_mode == "1" or (live_mode == "auto" and in_trading_window())

    # ── 通达信优先(方案B) ──────────────────────────────────────────────────
    if src in ("auto", "tdx"):
        try:
            tdx_items = _tdx_items_cached(btype, live_on=use_live)
        except Exception as e:  # noqa: BLE001
            logger.warning("TDX 板块热力图失败(%s), 回落 thsdk: %s", btype, e)
            tdx_items = None
        if tdx_items:
            tdx_items.sort(key=lambda x: (x["change_pct"] is None, -(x["change_pct"] or 0.0), x["name"]))
            live_cnt = sum(1 for i in tdx_items if i["live"])
            return {
                "type": btype,
                "trade_date": tdx_boards.latest_trade_date(),
                "count": len(tdx_items),
                "live": live_cnt > 0,
                "live_count": live_cnt,
                "as_of": datetime.now(timezone.utc).isoformat() if live_cnt > 0 else None,
                "source": "tdx",
                "items": tdx_items,
            }
        if src == "tdx":
            raise HTTPException(502, "通达信板块数据不可用(客户端未开/数据陈旧)")
    # ── thsdk 老链路(原逻辑, source=ths) ──────────────────────────────────
    try:
        boards = db.query(Board).filter(Board.board_type == btype).all()
        latest = (
            db.query(
                BoardDaily.block_code,
                func.max(BoardDaily.date).label("max_date"),
            )
            .group_by(BoardDaily.block_code)
            .subquery()
        )
        daily_rows = (
            db.query(BoardDaily)
            .join(
                latest,
                and_(
                    BoardDaily.block_code == latest.c.block_code,
                    BoardDaily.date == latest.c.max_date,
                ),
            )
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("查询板块热力图数据失败: %s", e)
        raise HTTPException(502, f"查询板块热力图数据失败: {e}")

    daily_by_code = {r.block_code: r for r in daily_rows}
    items: list[dict[str, Any]] = []
    for b in boards:
        d = daily_by_code.get(b.block_code)
        items.append(
            {
                "block_code": b.block_code,
                "name": b.name or "",
                "board_type": b.board_type or "",
                "change_pct": d.change_pct if d is not None else None,
                "fund_net": d.fund_net if d is not None else None,
                "volume": d.volume if d is not None else None,
                "date": d.date.strftime("%Y-%m-%d") if d is not None and d.date else None,
                "has_daily": d is not None,
                # 实时字段(仅 live 命中的项有值; 前端据此做异动高亮)
                "volume_ratio": None,
                "speed": None,
                "live": False,
            }
        )

    # 实时覆盖: auto=交易时段内, "1"=强制; "0"=不实时。失败回落日线(静默, 不报错)
    use_live = live_mode == "1" or (live_mode == "auto" and in_trading_window())
    live_merged = 0
    as_of: str | None = None
    if use_live:
        snaps = _live_snapshot(btype, [i["block_code"] for i in items])
        if snaps:
            live_merged = _merge_live(items, snaps)
            if live_merged > 0:
                as_of = datetime.now(timezone.utc).isoformat()

    # 排序: 涨跌幅降序, 无数据排最后(Python 侧排, 不依赖方言 NULLS LAST 支持)
    items.sort(key=lambda x: (x["change_pct"] is None, -(x["change_pct"] or 0.0), x["name"]))

    dates = [i["date"] for i in items if i["date"]]
    return {
        "type": btype,
        "trade_date": max(dates) if dates else None,
        "count": len(items),
        "live": live_merged > 0,
        "live_count": live_merged,
        "as_of": as_of,
        "source": "ths",
        "items": items,
    }


@router.get("/{block_code}")
def board_detail(
    block_code: str,
    db: Session = Depends(get_db),
):
    """板块详情: 通达信板块(88xxxx.SH)走 TDX 实时; 其余走 Board 元信息 + 最新日线。

    通达信分支(方案B): 名称取板块目录, 今日指标取 pricevol+AMO/SUPAMO 批量,
    date = 板块日线最新日期; 数据不可用 404(不返回空壳)。
    """
    result: dict[str, Any] = {}
    code = block_code.strip()
    if not code:
        raise HTTPException(400, "block_code 不能为空")

    # ── 通达信原生板块 ─────────────────────────────────────────────────────
    if tdx_boards.is_tdx_block_code(code):
        try:
            sector = next((s for s in tdx_boards.sector_items() if s["code"] == code), None)
            quote = tdx_boards.board_quotes([code]).get(code) if sector else None
        except Exception as e:  # noqa: BLE001
            logger.warning("TDX 板块详情失败 %s: %s", code, e)
            sector, quote = None, None
        if not sector or not quote or quote.get("price") is None:
            raise HTTPException(404, f"板块不存在或数据不可用: {code}")
        return {
            "block_code": code,
            "name": sector["name"],
            "board_type": sector["board_type"],
            "source": "tdx",
            "live": True,
            "has_daily": True,
            "today": {
                "date": tdx_boards.latest_trade_date(),
                "change_pct": quote.get("change_pct"),
                "fund_net": quote.get("fund_net"),
                "volume": quote.get("amount"),
                "price": quote.get("price"),
            },
        }

    board = None
    daily = None
    try:
        board = db.query(Board).filter(Board.block_code == code).first()
        daily = (
            db.query(BoardDaily)
            .filter(BoardDaily.block_code == code)
            .order_by(BoardDaily.date.desc())
            .first()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("查询板块详情失败 %s: %s", code, e)
        board = None
        daily = None

    result["block_code"] = code
    name: str = ""
    btype: str = ""
    if board is not None:
        name = str(board.name or "")
        btype = str(board.board_type or "")
    result["name"] = name
    result["board_type"] = btype

    if daily is not None:
        result["today"] = {
            "date": daily.date.strftime("%Y-%m-%d"),
            "change_pct": daily.change_pct,
            "fund_net": daily.fund_net,
            "volume": daily.volume,
        }
        result["has_daily"] = True
        result["live"] = False
    else:
        result["today"] = None
        result["has_daily"] = False
        # DB 无日线 → 尝试实时拉取(1 小时缓存)
        detail = fetch_block_detail(code)
        if not detail:
            raise HTTPException(404, f"板块不存在或数据不可用: {code}")
        metrics = _extract_block_metrics(detail)
        result["today"] = {
            "date": None,
            "change_pct": metrics["change_pct"],
            "fund_net": metrics["fund_net"],
            "volume": metrics["volume"],
        }
        result["live"] = True
    return result


@router.get("/{block_code}/constituents")
def board_constituents(block_code: str):
    """板块成分股。

    通达信板块(88xxxx.SH, 方案B): get_stock_list_in_sector + pricevol 批量实时,
    返回**每只涨幅**(老板点名: 点进板块要看个股涨幅): {symbol,name,price,change_pct,amount}。
    其余板块: thsdk 实时(1 小时缓存), 原始列透传(前端模糊取列)。失败 404。
    """
    code = block_code.strip()
    if not code:
        raise HTTPException(400, "block_code 不能为空")

    if tdx_boards.is_tdx_block_code(code):
        try:
            codes = tdx_boards.constituents(code)
        except Exception as e:  # noqa: BLE001
            logger.warning("TDX 成分股失败 %s: %s", code, e)
            codes = None
        if codes is None:
            raise HTTPException(404, f"板块成分股拉取失败: {code}")
        quotes = tdx_boards.board_quotes(codes, with_fund=False)
        names = tdx_boards.name_map()
        items = []
        for c in codes:
            symbol = tdx_boards.tdx_code_to_symbol(c)
            if symbol is None:
                continue
            q = quotes.get(c) or {}
            items.append(
                {
                    "symbol": symbol,
                    "name": names.get(c, ""),
                    "price": q.get("price"),
                    "change_pct": q.get("change_pct"),
                    "amount": q.get("amount"),
                    "volume": q.get("volume"),
                }
            )
        # 涨幅降序, 无快照排最后(不猜)
        items.sort(key=lambda x: (x["change_pct"] is None, -(x["change_pct"] or 0.0), x["symbol"]))
        return {"block_code": code, "source": "tdx", "count": len(items), "items": items}

    rows = fetch_block_constituents(code)
    if rows is None:
        raise HTTPException(404, f"板块成分股拉取失败: {code}")
    # 成分股可能很多, 抽样返回前 300 条避免响应过大(完整实时数据已在缓存)
    return {"block_code": code, "source": "ths", "count": len(rows), "items": rows[:300]}