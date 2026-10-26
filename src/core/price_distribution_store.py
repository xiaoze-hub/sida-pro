# -*- coding: utf-8 -*-
"""I-8 分价矩阵持久化与读穿透缓存(2026-10-18)。

用户硬约束: **所有新功能产生的数据必须全部落库** ——
  ① 历史回测量化研究 / 主力轨迹复盘需要分价时序;
  ② 首屏不应每次跨网打腾讯(读库一行 JSON 是毫秒级)。

因此本模块是 API 与 analyzer 之间的**唯一数据通道**:

    读: 先查 `price_distribution_snapshots` 最新一行
         ├─ 新鲜(age ≤ TTL)              → 直出 DB payload(`cache.hit=True, source="db"`)
         └─ 过期 / 缺行                   → 回源取数 + 落库, 再出(`source="live"`)
    写: 盘中每次成功取数都 upsert 一行(同 (票, 交易日, 快照时刻) 原地覆盖);
        收盘/手动归档写 `price_distribution_daily`(每票每日一份定稿, 永久保留)。

为什么 TTL 分时段: 连续竞价 15s 内的分价表几乎不变(腾讯按分钟聚合), 而**收盘后数据
是冻结的**, 再用 15s 去回源纯属自残。故交易时段 TTL=15s, 非交易时段 TTL=600s。

诚实边界:
  · 落库失败**不影响**本次响应(返回本次现算结果 + `cache.persist_error`), 但要留日志;
  · 回源失败时若有旧行, 返回旧行并标 `cache.stale=True` + `cache.reason`, 绝不把旧
    数据当新数据(前端会显示"显示上次成功值"及 `trade_date`);
  · 历史日永不回源(腾讯分价表只给当日), `load_daily_history` 只读库。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.core.price_distribution_analyzer import DEFAULT_LIMIT, fetch_analysis
from src.db.models import PriceDistributionDaily, PriceDistributionSnapshot
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

#: 连续竞价期的缓存 TTL(秒)。腾讯分价表分钟级更新, 15s 足够"跟手"又不打爆上游。
TTL_LIVE_S = 15
#: 非交易时段(收盘后/开盘前/周末)TTL: 数据冻结, 读库即可。
TTL_CLOSED_S = 600
#: 盘中快照的时间桶(分钟): 桶内多次采样原地覆盖, 只留最后一次。
SNAPSHOT_BUCKET_MIN = 5
#: 盘中快照保留交易日数(日终归档已在 daily 表永久保留)。
SNAPSHOT_KEEP_DAYS = 5


def _now() -> float:
    return time.time()


def _default_ttl() -> int:
    """按交易阶段取 TTL; 阶段判定异常时保守用非交易 TTL(少打上游)。"""
    try:
        from src.core.trade_phase import phase_meta

        return TTL_LIVE_S if phase_meta().get("is_live_session") else TTL_CLOSED_S
    except Exception:  # noqa: BLE001 - 阶段模块异常不影响取数
        return TTL_CLOSED_S


# --------------------------------------------------------------------------- #
# 行 → dict
# --------------------------------------------------------------------------- #
def _row_payload(row: PriceDistributionSnapshot | PriceDistributionDaily) -> Dict[str, Any]:
    payload = row.payload
    if isinstance(payload, str):  # 兼容裸 TEXT 列(历史库/手写 INSERT)
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    return payload if isinstance(payload, dict) else {}


def snapshot_to_dict(row: PriceDistributionSnapshot) -> Dict[str, Any]:
    return {
        "trade_date": row.trade_date,
        "bucket_ts": getattr(row, "bucket_ts", None),
        "market": row.market,
        "symbol": row.symbol,
        "snapshot_ts": row.snapshot_ts,
        "captured_at": row.captured_at,
        "payload": _row_payload(row),
    }


# --------------------------------------------------------------------------- #
# 写: 盘中快照 / 日终归档
# --------------------------------------------------------------------------- #
def bucket_of(snapshot_ts: Optional[str], minutes: int = SNAPSHOT_BUCKET_MIN) -> str:
    """`HH:MM:SS` → `HH:MM` 的 N 分钟桶标签(向下取整)。解析失败返回原串前 5 位或空串。

    5 分钟桶不是"丢数据": 桶内每次采样都会覆盖写, 桶里留下的是**桶内最新**那一份,
    日内轨迹(48 点/天)完整, 只是不保留每 3 秒的重复帧。
    """
    s = str(snapshot_ts or "").strip()
    if len(s) < 5 or s[2] != ":":
        return s[:5]
    try:
        hh, mm = int(s[:2]), int(s[3:5])
    except ValueError:
        return s[:5]
    span = max(1, int(minutes))
    mm = (mm // span) * span
    return f"{hh:02d}:{mm:02d}"


def _levels_price(levels: Optional[List[Dict[str, Any]]]) -> Optional[float]:
    if not levels:
        return None
    try:
        return float(levels[0].get("price"))
    except (TypeError, ValueError, AttributeError):
        return None


def upsert_snapshot(
    session: Session,
    analysis: Dict[str, Any],
    *,
    captured_at: Optional[float] = None,
) -> bool:
    """写入/覆盖一条盘中快照。返回是否落库成功(异常向上抛, 由调用方决定降级)。

    只接受**可用**结果: `available=False` 的降级响应不落库 —— 否则一次网络抖动会
    在回测数据集里留下一条"当天无分价"的假事实。
    """
    if not analysis or not analysis.get("available"):
        return False
    trade_date = analysis.get("trade_date")
    snapshot_ts = analysis.get("snapshot_ts") or ""
    symbol = str(analysis.get("symbol") or "")
    market = (analysis.get("market") or "CN").upper()
    if not trade_date or not symbol:
        return False

    bucket = bucket_of(snapshot_ts)
    day = analysis.get("day") or {}
    quote = analysis.get("quote") or {}
    values = dict(
        snapshot_ts=snapshot_ts,        # 桶内覆盖时也要跟着更新成最新一次
        captured_at=float(captured_at if captured_at is not None else _now()),
        close_price=quote.get("close"),
        total_vol=day.get("total_vol"),
        buy_vol=day.get("buy_vol"),
        sell_vol=day.get("sell_vol"),
        net_amount=day.get("net_amount"),
        buy_ratio=day.get("buy_ratio"),
        big_ratio=day.get("big_ratio"),
        price_bins_json=analysis.get("bins") or [],
        support_levels=analysis.get("support_levels") or [],
        resistance_levels=analysis.get("resistance_levels") or [],
        iceberg_signals=analysis.get("iceberg_signals") or [],
        payload=analysis,
        source=analysis.get("source") or "tencent_price",
        quality_json=analysis.get("quality") or {},
    )

    row = session.scalar(
        select(PriceDistributionSnapshot).where(
            PriceDistributionSnapshot.trade_date == trade_date,
            PriceDistributionSnapshot.market == market,
            PriceDistributionSnapshot.symbol == symbol,
            PriceDistributionSnapshot.bucket_ts == bucket,
        )
    )
    if row is None:
        session.add(
            PriceDistributionSnapshot(
                trade_date=trade_date,
                market=market,
                symbol=symbol,
                bucket_ts=bucket,
                **values,
            )
        )
    else:
        for k, v in values.items():
            setattr(row, k, v)
    session.commit()
    return True


def archive_daily(
    session: Session,
    analysis: Dict[str, Any],
    *,
    settled_at: Optional[float] = None,
) -> bool:
    """把一次分析结果归档为**当日定稿**(唯一键 (trade_date, market, symbol) 原地覆盖)。

    收盘后跑即当日定稿; 盘中跑则是最新一次采样的快照(可被后续覆盖)。
    """
    if not analysis or not analysis.get("available"):
        return False
    trade_date = analysis.get("trade_date")
    symbol = str(analysis.get("symbol") or "")
    market = (analysis.get("market") or "CN").upper()
    if not trade_date or not symbol:
        return False

    day = analysis.get("day") or {}
    quote = analysis.get("quote") or {}
    support = analysis.get("support_levels") or []
    resistance = analysis.get("resistance_levels") or []
    iceberg = analysis.get("iceberg_signals") or []
    values = dict(
        snapshot_ts=analysis.get("snapshot_ts") or "",
        settled_at=float(settled_at if settled_at is not None else _now()),
        close_price=quote.get("close"),
        total_vol=day.get("total_vol"),
        buy_vol=day.get("buy_vol"),
        sell_vol=day.get("sell_vol"),
        net_amount=day.get("net_amount"),
        buy_ratio=day.get("buy_ratio"),
        big_ratio=day.get("big_ratio"),
        support_price=_levels_price(support),
        resistance_price=_levels_price(resistance),
        iceberg_count=len(iceberg),
        price_bins_json=analysis.get("bins") or [],
        support_levels=support,
        resistance_levels=resistance,
        iceberg_signals=iceberg,
        payload=analysis,
        source=analysis.get("source") or "tencent_price",
        quality_json=analysis.get("quality") or {},
    )

    row = session.scalar(
        select(PriceDistributionDaily).where(
            PriceDistributionDaily.trade_date == trade_date,
            PriceDistributionDaily.market == market,
            PriceDistributionDaily.symbol == symbol,
        )
    )
    if row is None:
        session.add(
            PriceDistributionDaily(
                trade_date=trade_date, market=market, symbol=symbol, **values
            )
        )
    else:
        for k, v in values.items():
            setattr(row, k, v)
    session.commit()
    return True


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def load_latest_snapshot(
    session: Session,
    symbol: str,
    market: str = "CN",
    *,
    max_age_s: Optional[float] = None,
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """取该标的最新一条盘中快照; 超过 `max_age_s` 视为过期返回 None。"""
    row = session.scalar(
        select(PriceDistributionSnapshot)
        .where(
            PriceDistributionSnapshot.symbol == symbol,
            PriceDistributionSnapshot.market == (market or "CN").upper(),
        )
        .order_by(PriceDistributionSnapshot.captured_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    age = (now_ts if now_ts is not None else _now()) - float(row.captured_at or 0)
    if max_age_s is not None and age > max_age_s:
        return None
    out = snapshot_to_dict(row)
    out["age_s"] = round(max(0.0, age), 1)
    return out


def load_daily_history(
    session: Session,
    symbol: str,
    market: str = "CN",
    *,
    days: int = 60,
) -> List[Dict[str, Any]]:
    """读日终归档序列(升序), 供回测/轨迹复盘。**不**回源, 库里有几天给几天。"""
    rows = session.scalars(
        select(PriceDistributionDaily)
        .where(
            PriceDistributionDaily.symbol == symbol,
            PriceDistributionDaily.market == (market or "CN").upper(),
        )
        .order_by(PriceDistributionDaily.trade_date.desc())
        .limit(max(1, int(days)))
    ).all()
    out = []
    for row in reversed(list(rows)):
        out.append({
            "trade_date": row.trade_date,
            "snapshot_ts": row.snapshot_ts,
            "close_price": row.close_price,
            "net_amount": row.net_amount,
            "buy_ratio": row.buy_ratio,
            "big_ratio": row.big_ratio,
            "support_price": row.support_price,
            "resistance_price": row.resistance_price,
            "iceberg_count": row.iceberg_count or 0,
            "payload": _row_payload(row),
        })
    return out


def purge_old_snapshots(
    session: Session,
    *,
    keep_days: int = SNAPSHOT_KEEP_DAYS,
    today: Optional[str] = None,
) -> int:
    """清理超过保留期的盘中快照(日终归档在 daily 表, 不受影响)。返回删除行数。"""
    from datetime import date, timedelta

    if today:
        base = date.fromisoformat(today)
    else:
        base = date.today()
    cutoff = (base - timedelta(days=max(1, int(keep_days)))).isoformat()
    res = session.execute(
        delete(PriceDistributionSnapshot).where(PriceDistributionSnapshot.trade_date < cutoff)
    )
    session.commit()
    return int(res.rowcount or 0)


# --------------------------------------------------------------------------- #
# 读穿透
# --------------------------------------------------------------------------- #
def _cache_meta(
    *, hit: bool, source: str, age_s: Optional[float] = None,
    ttl_s: Optional[int] = None, stale: bool = False,
    reason: Optional[str] = None, persist_error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "hit": hit,
        "source": source,       # db | live | db_stale | none
        "age_s": age_s,
        "ttl_s": ttl_s,
        "stale": stale,
        "reason": reason,
        "persist_error": persist_error,
    }


def get_or_refresh(
    symbol: str,
    market: str = "CN",
    *,
    limit: int = DEFAULT_LIMIT,
    ttl_s: Optional[int] = None,
    refresh: bool = False,
    persist: bool = True,
    fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    session_factory: Callable[[], Session] = SessionLocal,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """读库优先 → 过期回源 → 落库 → 出参。API 层唯一入口。

    `refresh=True` 跳过缓存直连上游(前端"强制刷新"), 但**仍会落库**。
    `persist=False` 只读不写(测试/只读副本场景)。
    `fetcher` 注入供单测(默认 `analyzer.fetch_analysis`)。
    """
    sym = str(symbol or "").strip()
    market_u = (market or "CN").upper()
    ttl = int(ttl_s if ttl_s is not None else _default_ttl())
    ts = now_ts if now_ts is not None else _now()

    cached = None
    try:
        with session_factory() as session:
            cached = load_latest_snapshot(session, sym, market_u, now_ts=ts)
    except Exception as e:  # noqa: BLE001 - 库不可用不能连累取数
        logger.warning(f"分价快照读库失败[{sym}]: {e}")

    if cached and not refresh and (cached.get("age_s") or 0) <= ttl:
        payload = dict(cached["payload"])
        payload["cache"] = _cache_meta(
            hit=True, source="db", age_s=cached.get("age_s"), ttl_s=ttl
        )
        return payload

    fetch = fetcher or fetch_analysis
    try:
        analysis = fetch(sym, market=market_u, limit=limit)
    except Exception as e:  # noqa: BLE001 - 上游边界
        logger.warning(f"分价矩阵回源失败[{sym}]: {e}")
        analysis = {
            "available": False,
            "reason": f"分价矩阵回源失败: {e}",
            "symbol": sym,
            "market": market_u,
        }

    if not analysis.get("available") and cached:
        # 上游失败/无数据但库里有旧行: 出旧数据 + 显式 stale, 绝不静默当新数据
        payload = dict(cached["payload"])
        payload["cache"] = _cache_meta(
            hit=True, source="db_stale", age_s=cached.get("age_s"), ttl_s=ttl,
            stale=True, reason=analysis.get("reason"),
        )
        return payload

    persist_error = None
    if persist and analysis.get("available"):
        try:
            with session_factory() as session:
                upsert_snapshot(session, analysis, captured_at=ts)
        except Exception as e:  # noqa: BLE001 - 落库失败不阻断响应
            persist_error = str(e) or e.__class__.__name__
            logger.warning(f"分价快照落库失败[{sym}]: {e}")

    out = dict(analysis)
    available = bool(analysis.get("available"))
    out["cache"] = _cache_meta(
        hit=False,
        # 取数失败时不能说 source="live"(那是"实时好数据"的意思) —— 明确 none + reason
        source="live" if available else "none",
        age_s=0.0 if available else None,
        ttl_s=ttl,
        reason=None if available else analysis.get("reason"),
        persist_error=persist_error,
    )
    return out
