# -*- coding: utf-8 -*-
"""L2 成品资金收盘快照 + 非交易时段回退源(2026-10-11 P1 数据断档修复)。

问题
----
工作台 L2 标签「L2 成品资金」与暗盘卡片的「主力净流入(L2·TQ)」读的是 TQ
`get_more_info` **实时会话值**(`Zjl_HB/Zjl/TotalBVol/TotalSVol/BCancel/SCancel/
L2TicNum/L2OrderNum`)。非交易时段 TQ 一律回 `0`(盘口会话已结束), 而 UI 没回退也
没标注 → 周末显示"0万 平衡 / 逐笔0笔·委托0笔", 把 0 冒充成真实值, 误导。

修复(本模块负责"落库 + 读取", web 层负责"回退 + 标注" —— B4.1)
--------------------------------------------------------------
- 交易日收盘后(15:05 cron)对全市场采样, 落 `l2_fund_snapshots`(**只落有真实值的**,
  全 0 不落 —— 0 绝不冒充);
- 读取 `latest_snapshot()`: 优先本表最近交易日行; 无则回退**现有落库**
  `seal_quality_samples` 的末行(同字段口径, 显式带实际日期 as_of)。

缺数据显式(不编造)
----------------
- 采样全 0 → 不落库;
- 读取无任何可用行 → 返回 None(由 web 层显式 available:false)。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE = "l2_fund_snapshots"
_SEAL = "seal_quality_samples"

# 面板「L2 成品资金」实际消费的字段(TQ get_more_info 明盘口径)。
# 金额类为**万元**(Zjl_HB/Zjl), 量类为股(TotalBVol/TotalSVol/BCancel/SCancel)。
FIELDS: tuple[str, ...] = (
    "zjl_hb", "zjl", "total_buy_vol", "total_sell_vol",
    "cancel_buy", "cancel_sell", "l2_tick_num", "l2_order_num",
)
# seal_quality_samples 覆盖的同口径字段(缺 zjl_hb/zjl)。
_SEAL_MAP = {
    "total_buy_vol": "total_buy_vol",
    "total_sell_vol": "total_sell_vol",
    "cancel_buy": "cancel_buy",
    "cancel_sell": "cancel_sell",
    "l2_tick_num": "l2_tick_num",
    "l2_order_num": "l2_order_num",
}


def _as_num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def has_live_values(vals: dict) -> bool:
    """任一字段为非 0 的有限数值 → True(会话确实产出了数据, 非"已收盘全 0")。"""
    for v in (vals or {}).values():
        n = _as_num(v)
        if n is not None and n != 0.0:
            return True
    return False


def _trade_date(now: datetime | None = None) -> str:
    dt = now or datetime.now(_CST)
    return dt.strftime("%Y%m%d")


# ── 写入 ────────────────────────────────────────────────────────────────────

def _upsert(symbol: str, market: str, trade_date: str, vals: dict, now=None) -> None:
    from sqlalchemy import text

    from src.db.dialect import upsert_sql
    from src.db.session import SessionLocal

    cols = ["symbol", "market", "trade_date", *FIELDS, "captured_at"]
    updates = [*FIELDS, "captured_at"]
    stmt = text(upsert_sql(_TABLE, cols, ["symbol", "market", "trade_date"], updates))
    payload = {"symbol": symbol, "market": market, "trade_date": trade_date,
               "captured_at": datetime.now(_CST).replace(tzinfo=None)}
    for f in FIELDS:
        payload[f] = _as_num(vals.get(f))
    db = SessionLocal()
    try:
        db.execute(stmt, payload)
        db.commit()
    finally:
        db.close()


def capture_symbol(symbol: str, market: str = "CN", now: datetime | None = None) -> dict:
    """采样单只并落库(会话值全 0 / 无数据 → 不落, 显式说明)。"""
    from src.core.marketdata_client import md_more_info

    try:
        rows = md_more_info([symbol], market)
    except Exception as e:  # noqa: BLE001
        return {"symbol": symbol, "captured": False, "reason": f"取数失败: {e}"}
    if not rows:
        return {"symbol": symbol, "captured": False, "reason": "无数据"}
    row = rows[0] or {}
    vals = {f: _as_num(row.get(f)) for f in FIELDS}
    if not has_live_values(vals):
        return {"symbol": symbol, "captured": False, "reason": "会话值全 0(不落, 不冒充真实值)"}
    try:
        _upsert(symbol, market, _trade_date(now), vals, now=now)
    except Exception as e:  # noqa: BLE001
        return {"symbol": symbol, "captured": False, "reason": f"落库失败: {e}"}
    return {"symbol": symbol, "captured": True}


def capture_universe(symbols: list[str] | None = None, market: str = "CN",
                     max_stocks: int = 0, now: datetime | None = None) -> dict:
    """全市场(或指定)收盘快照采样。返回统计; 走作业诚实性(ok=False → failed)。"""
    if symbols is None:
        try:
            from src.collectors.stock_list import get_stock_list

            symbols = [
                s["symbol"] for s in get_stock_list()
                if s.get("symbol") and len(str(s["symbol"])) == 6
            ]
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"股票名单加载失败: {e}", "captured": 0}
    if max_stocks > 0:
        symbols = symbols[:max_stocks]

    captured = skipped = failed = 0
    for sym in symbols:
        r = capture_symbol(sym, market, now)
        if r.get("captured"):
            captured += 1
        elif "取数失败" in str(r.get("reason") or "") or "落库失败" in str(r.get("reason") or ""):
            failed += 1
        else:
            skipped += 1
    # 全部"全 0/无数据"且一只没落 → 大概率是非交易时段误触发, 显式 ok=False 不假装成功
    ok = captured > 0
    return {"ok": ok, "trade_date": _trade_date(now), "stocks": len(symbols),
            "captured": captured, "skipped": skipped, "failed": failed,
            "reason": None if ok else "采样未产出任何真实值(非交易时段或数据源不可用)"}


def run_l2_snapshot_job(now: datetime | None = None, symbols: list[str] | None = None) -> dict:
    """cron 入口(交易日收盘后): 采样 + 落库, 走作业框架诚实性(ok=False → failed)。"""
    from src.core.jobs import jobs

    job_id, is_new = jobs.create("l2_fund_snapshot", "L2 成品资金收盘快照")
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id}
    try:
        out = capture_universe(symbols=symbols, now=now)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("L2 收盘快照作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="L2收盘快照: ")
    return out


# ── 读取(回退源) ────────────────────────────────────────────────────────────

def _snap_from_row(row: dict, origin: str, trade_date: str) -> dict:
    out: dict = {f: _as_num(row.get(f)) for f in FIELDS}
    out["trade_date"] = trade_date
    out["origin"] = origin
    return out


def latest_snapshot(symbol: str, market: str = "CN") -> dict | None:
    """最近可用收盘快照。

    优先 `l2_fund_snapshots` 最新交易日行; 无则回退现有落库 `seal_quality_samples`
    末行(同字段口径)。两者皆无 → None(调用方显式 available:false, 绝不返 0 冒充)。

    两个源各开**独立 session**: PG 里一条语句报错(如表未迁移)会让整个事务进入
    aborted 态, 同 session 上后续查询全废(`InFailedSqlTransaction`) —— 独立 session
    天然隔离, 不依赖 rollback。
    """
    from sqlalchemy import text

    from src.db.session import SessionLocal

    # ① 专用收盘快照表
    db = SessionLocal()
    try:
        r = db.execute(
            text(
                f"SELECT trade_date, {', '.join(FIELDS)} FROM {_TABLE} "
                "WHERE symbol = :s AND market = :m ORDER BY trade_date DESC LIMIT 1"
            ),
            {"s": symbol, "m": market},
        ).mappings().first()
    except Exception as e:  # noqa: BLE001 表未迁移 → 退到 seal 源
        logger.warning("l2_fund_snapshots 查询失败, 回退 seal 源: %s", e)
        r = None
    finally:
        db.close()
    if r:
        return _snap_from_row(dict(r), "snapshot", str(r.get("trade_date")))

    # ② 现有落库 seal_quality_samples(独立 session)
    #    取**最近一行有真实值**的样本: 最新一行可能恰为全 0(采样器该次没拿到 L2),
    #    取它等于"有落库却当无数据"; 取更早的非 0 行仍按实际 ts 标注日期(不冒充当日)。
    db = SessionLocal()
    try:
        cols = ", ".join(_SEAL_MAP.values())
        nonzero = " + ".join(f"COALESCE({c}, 0)" for c in _SEAL_MAP.values())
        s = db.execute(
            text(
                f"SELECT ts, {cols} FROM {_SEAL} "
                "WHERE symbol = :s AND market = :m AND (" + nonzero + ") > 0 "
                "ORDER BY ts DESC LIMIT 1"
            ),
            {"s": symbol, "m": market},
        ).mappings().first()
    except Exception as e:  # noqa: BLE001
        logger.warning("seal_quality_samples 查询失败: %s", e)
        s = None
    finally:
        db.close()
    if s and has_live_values({k: s.get(v) for k, v in _SEAL_MAP.items()}):
        d = dict(s)
        return _snap_from_row(d, "seal_sample", str(d.get("ts") or "")[:10].replace("-", ""))
    return None
