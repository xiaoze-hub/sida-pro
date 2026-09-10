"""分钟K线(1m)盘中滚动入库(2026-09-10, 数据落库设计 Matrix #2)。

- 每 60s(交易时段) 拉自选∪候选池 的腾讯 m1 分时(320 根窗口, 断档自愈)
- 直连 vendor 单源(腾讯, 未复权), 不走 klines_with_vendor 竞速链 —— 那是
  日K qfq 口径; 分钟柱单源无竞速意义, 且绝不能混入 qfq 分区
- 落 klines period='1m' adjust='', ON CONFLICT DO UPDATE 幂等(重跑覆盖自愈)
- 指数不入 klines: 裸码与个股键位冲突(sh000001 上证指 vs sz000001 平安银行
  同码不同物), 指数分时由 quote_snapshots 承担
- 非时段/异常静默跳过, 永不抛(镜像 quote_snapshots)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

PERIOD = "1m"
SOURCE = "tencent"
BARS_PER_CALL = 320  # 腾讯 mkline 单次窗口 ≈ 1.3 个交易日, 断档自动补齐

_UPSERT = text(
    """
INSERT INTO klines (ts, symbol, market, period, source, adjust,
                    open, high, low, close, volume, amount, quality_flag)
VALUES (:ts, :symbol, :market, :period, :source, :adjust,
        :open, :high, :low, :close, :volume, :amount, :quality_flag)
ON CONFLICT (symbol, market, period, ts, source, adjust)
DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
              close=EXCLUDED.close, volume=EXCLUDED.volume,
              amount=EXCLUDED.amount, quality_flag=EXCLUDED.quality_flag
"""
)

_CST = ZoneInfo("Asia/Shanghai")


def _engine():
    from src.db.session import engine

    return engine


def _symbols_in_worker() -> list[tuple[str, str]]:
    """自选 ∪ 候选池(仅 CN; 与 L2 cron / 日K回填同一标的口径)。"""
    try:
        from src.collectors.klines_ingestor import get_default_symbols

        pairs = {(s, (m or "CN").upper()) for s, m in (get_default_symbols() or []) if s}
        return sorted(p for p in pairs if p[1] == "CN")
    except Exception as e:  # noqa: BLE001
        logger.debug("minute klines 取标的列表失败: %s", e)
        return []


def _one_symbol(code: str, market: str) -> int:
    """拉一只股的 1m K线并 upsert 落库, 返回写入行数。"""
    from marketdata.symbol import Market, Symbol

    from marketdata.vendors.kline import fetch_tencent_minute_kline

    tcode = Symbol(market=Market(market), code=code).to_tencent()
    bars = fetch_tencent_minute_kline(tcode, BARS_PER_CALL, PERIOD)
    if not bars:
        return 0
    rows = []
    for b in bars:
        try:
            ts = datetime.fromisoformat(str(b.date)).replace(tzinfo=_CST)
            o, h, l, c = float(b.open), float(b.high), float(b.low), float(b.close)
        except Exception:  # noqa: BLE001
            continue
        if min(o, h, l, c) <= 0 or l > min(o, c) + 1e-9 or max(o, c) > h + 1e-9:
            continue
        rows.append(
            {
                "ts": ts,
                "symbol": code,
                "market": market,
                "period": PERIOD,
                "source": SOURCE,
                "adjust": "",
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": int(b.volume or 0),
                "amount": None,  # mkline 末字段语义不明, 诚实缺失
                "quality_flag": 1,
            }
        )
    if not rows:
        return 0
    total = 0
    with _engine().begin() as conn:
        for row in rows:
            total += conn.execute(_UPSERT, row).rowcount
    return total


def collect_minute_klines_once() -> dict:
    """盘中 1m K线滚动入库一轮(60s 调度; 时段守卫在函数内)。永不抛。"""
    try:
        from src.core.quote_snapshots import in_trading_window

        if not in_trading_window():
            return {"skipped": "non_trading"}
        pairs = _symbols_in_worker()
        if not pairs:
            return {"symbols": 0, "written": 0}
        start = time.time()
        written = 0
        ok = 0
        for sym, mkt in pairs:
            try:
                w = _one_symbol(sym, mkt)
                written += w
                ok += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("minute klines %s 失败: %s", sym, e)
        elapsed = time.time() - start
        logger.info(
            f"[klines 1m] 完成: symbols={ok}/{len(pairs)} written={written} / {elapsed:.1f}s"
        )
        return {"symbols": ok, "total": len(pairs), "written": written, "elapsed": elapsed}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[klines 1m] 一轮失败(不抛): {e}")
        return {"error": str(e)}
