"""klines 数据访问仓库(B4.5/KI-039)。

把"回测取数直连 DB"的逻辑从 `src/core/backtest/data_adapter.py` 下沉到数据层,
让 core 不再 import src.web。仓储层是唯一允许碰 ORM/引擎构造的地方。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _norm_market(market) -> str:
    mc = market.value if hasattr(market, "value") else str(market).upper()
    return "CN" if mc in ("SH", "SZ", "BJ") else mc


def load_qfq_bars(symbol: str, market, days: int = 250) -> list[dict]:
    """按 (symbol, market) 取前复权日 K(升序)。

    返回 [{date, open, high, low, close, volume}] —— 与 core 的 PriceBar 字段对齐,
    由调用方自行映射, 避免数据层依赖 core 的类型。

    查库失败返回 [] (调用方决定是否联网兜底), 不抛异常。
    """
    from sqlalchemy import text

    try:
        mc_str = _norm_market(market)
        # P0(2026-09-18): 复用主引擎单例(src/db/session.py, 与主引擎同一连接池/参数),
        # 禁止 per-call create_engine+dispose。
        from src.db.session import engine
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    # 2026-10-21: 原来写死 source='tencent' —— TQ 采集的票（用户铁律优先源）
                    # 一行都查不到，多周期走势直接空白。改为同时接受 tq/tencent，
                    # 同一交易日优先取 tq，Python 侧按日期去重。
                    "SELECT ts, open, high, low, close, volume, coalesce(source,'') AS src "
                    "FROM klines "
                    "WHERE symbol=:s AND market=:m AND period='1d' AND adjust='qfq' "
                    "  AND ts >= :c AND coalesce(source,'') IN ('tq','tencent') "
                    "ORDER BY ts ASC, CASE WHEN coalesce(source,'')='tq' THEN 0 ELSE 1 END"
                ),
                {"s": symbol, "m": mc_str, "c": cutoff},
            ).fetchall()
        out, seen = [], set()
        for r in rows:
            day = str(r[0])[:10]
            if day in seen:      # 同一天 tq 已取过，跳过 tencent 副本
                continue
            seen.add(day)
            out.append({
                "date": day,
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5] or 0),
            })
        return out
    except Exception as e:
        logger.warning(f"[klines_repo] 查 PG klines 失败 {symbol}: {e}")
        return []


def load_qfq_bars_cached(symbol: str, market, days: int = 250) -> list[dict]:
    """`load_qfq_bars` 的"本轮可复用"版本（2026-10-23，单轮取数缓存）。

    **只在管线作用域内命中缓存**（`src.harness.bar_cache.begin_bar_scope()` 已开）：
    同一轮里 `get_trend_profile` / `get_chip_accumulation` / `get_scenario_matrix` /
    `get_stock_dossier` 取同一只票的日线，从 4 次 PG 查询降到 1 次。

    没有作用域时与直接调 `load_qfq_bars` 完全等价（直通 loader）—— 回填 / 因子 /
    回测 / 定时任务这些要**新鲜**数据的调用方请继续用 `load_qfq_bars`，不要换成这个。
    缓存模块不可用时同样退回 `load_qfq_bars`：少一层优化可以，少一份报价不行。
    """
    try:
        from src.harness.bar_cache import cached_load_bars
    except Exception as exc:  # noqa: BLE001 - 缓存模块缺失不该让取数失败
        logger.debug(f"[klines_repo] bar_cache 不可用，直接读库: {exc}")
        return load_qfq_bars(symbol, market, days)
    return cached_load_bars(symbol, market, days, loader=load_qfq_bars)
