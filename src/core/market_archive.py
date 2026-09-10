"""批次2(2026-09-10): 市场数据落库 —— 竞价快照 + 筹码日频。

与 `klines`/`l2_ticks` 同款: 表由**迁移**建(非 ORM, 见 migrations `_m154`/`_m155`),
本模块只做**幂等 upsert** 与读取。

- **公共数据**(表里无 user_id): 与账号无关 → 多账号共用一份, 天然不重复请求。
- **幂等**: `(trade_date, symbol, market)` 唯一 → `ON CONFLICT DO UPDATE`, 重跑覆盖(可自愈)。
- **不抛异常**: 落库/读取失败一律返回 0/False/[] —— 不拖垮主流程(与 notify 同哲学)。
- **零值不伪造**: 字段缺失写 NULL, 不用 0 冒充。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)


def _engine():
    from src.db.session import engine

    return engine


def _iso(d: Any) -> str:
    if isinstance(d, (date, datetime)):
        return d.isoformat()[:10]
    return str(d)[:10]


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── 竞价快照 ──────────────────────────────────────────────────────────────
# (库列名, 入参键名) —— 入参是 TQ/thsdk 合并后的结构, 键名与列名不完全一致。
_AUCTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("source", "source"),
    ("auction_price", "auction_price"),
    ("open_price", "open"),          # TQ 快照里叫 open
    ("open_pct", "open_pct"),
    ("open_amount", "open_amount"),
    ("open_limit_buy", "open_limit_buy"),
    ("direction", "direction"),
    ("gap_pct", "gap_pct"),
    ("auction_high", "auction_high"),
    ("auction_low", "auction_low"),
    ("withdraw_rate_pre0920", "withdraw_rate_pre0920"),
)
_AUCTION_COLS = tuple(c for c, _ in _AUCTION_FIELDS)


def persist_auction_snapshots(
    trade_date: Any, snaps: dict[str, dict], market: str = "CN"
) -> int:
    """逐票竞价快照幂等落库; 返回写入行数(失败 0, 不抛)。

    `snaps` 形如 `{symbol: {...}}`, 兼容 TQ(`open/open_pct/open_amount/...`)与
    thsdk(`direction/gap_pct/auction_high/low/withdraw_rate_pre0920/auction_price`)合并后的结构。
    """
    if not snaps:
        return 0
    from src.db.dialect import upsert_sql

    d = _iso(trade_date)
    now = _now_naive_utc()
    rows: list[dict] = []
    for sym, s in snaps.items():
        if not sym or not isinstance(s, dict):
            continue
        row = {
            "trade_date": d,
            "symbol": str(sym),
            "market": market,
            "updated_at": now,
        }
        for col, src in _AUCTION_FIELDS:
            v = s.get(src)
            if col == "source" and v:
                v = str(v)[:32]
            row[col] = v
        rows.append(row)
    if not rows:
        return 0
    insert_cols = ["trade_date", "symbol", "market", *_AUCTION_COLS, "updated_at"]
    stmt = upsert_sql(
        "auction_snapshots",
        insert_cols=insert_cols,
        conflict_cols=["trade_date", "symbol", "market"],
        update_cols=[*_AUCTION_COLS, "updated_at"],
    )
    try:
        with _engine().begin() as conn:
            conn.execute(text(stmt), rows)
        return len(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning("竞价快照落库失败(%d 行): %s", len(rows), e)
        return 0


def read_auction_snapshots(
    symbol: str, limit: int = 30, market: str = "CN"
) -> list[dict]:
    """按标的取近期竞价快照(新→旧); 失败返回 []。"""
    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT trade_date, source, auction_price, open_price, open_pct,
                           open_amount, open_limit_buy, direction, gap_pct,
                           auction_high, auction_low, withdraw_rate_pre0920
                    FROM auction_snapshots
                    WHERE symbol = :s AND market = :m
                    ORDER BY trade_date DESC
                    LIMIT :n
                    """
                ),
                {"s": str(symbol), "m": market, "n": int(limit)},
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning("读竞价快照失败 %s: %s", symbol, e)
        return []


# ── 筹码日频 ──────────────────────────────────────────────────────────────
_CHIP_FIELDS = (
    "cost_10",
    "cost_50",
    "cost_90",
    "profit_ratio",
    "peak_price",
    "cost_band_low",
    "cost_band_high",
)


def persist_chip_daily(
    trade_date: Any, symbol: str, market: str, chips: dict | None
) -> bool:
    """筹码分布日频幂等落库(入参为 `compute_chips`/`compute_chips_sina` 输出)。"""
    if not chips or not symbol:
        return False
    from src.db.dialect import upsert_sql

    band = chips.get("cost_band") if isinstance(chips.get("cost_band"), dict) else {}
    row = {
        "trade_date": _iso(trade_date),
        "symbol": str(symbol),
        "market": market,
        "cost_10": chips.get("cost_10"),
        "cost_50": chips.get("cost_50"),
        "cost_90": chips.get("cost_90"),
        "profit_ratio": chips.get("profit_ratio"),
        "peak_price": chips.get("peak_price"),
        "cost_band_low": (band or {}).get("low"),
        "cost_band_high": (band or {}).get("high"),
        "updated_at": _now_naive_utc(),
    }
    if row["cost_50"] is None and row["peak_price"] is None:
        return False  # 空壳不落库
    stmt = upsert_sql(
        "chip_daily",
        insert_cols=["trade_date", "symbol", "market", *_CHIP_FIELDS, "updated_at"],
        conflict_cols=["trade_date", "symbol", "market"],
        update_cols=[*_CHIP_FIELDS, "updated_at"],
    )
    try:
        with _engine().begin() as conn:
            conn.execute(text(stmt), [row])
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("筹码日频落库失败 %s: %s", symbol, e)
        return False


def read_chip_daily(symbol: str, limit: int = 30, market: str = "CN") -> list[dict]:
    """按标的取近期筹码日频(新→旧); 失败返回 []。"""
    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT trade_date, cost_10, cost_50, cost_90, profit_ratio,
                           peak_price, cost_band_low, cost_band_high
                    FROM chip_daily
                    WHERE symbol = :s AND market = :m
                    ORDER BY trade_date DESC
                    LIMIT :n
                    """
                ),
                {"s": str(symbol), "m": market, "n": int(limit)},
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning("读筹码日频失败 %s: %s", symbol, e)
        return []
