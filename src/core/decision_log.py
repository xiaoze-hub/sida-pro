"""决策日志(B6, 2026-09-18) —— 把"信号 → 结果"变成可统计的样本。

## 为什么
共振(三指标)/GS 买卖点/竞价池这些信号一直能"看", 但**没有账**: 哪个信号在什么市场状态下真管用?
没有账就只能靠感觉调参数, 也永远无法证伪自己。这张表把每个信号**连同当时的证据与价格**留痕,
事后用真实的后续 K 线回填 T+1/T+3/T+5 收益与命中。

## 三条纪律(与全仓诚实口径一致)
1. **取不到就 NULL**: `price_at_signal` / 收益 / 命中缺失一律 NULL, **绝不补 0**
   (0 是真实收益, 不能冒充"没有数据")。
2. **回填必须等未来那根 K 线真的存在**: 不许用之后的数据推算, 也不许"假设持有"。
3. **样本不足就不给结论**: 命中率在 `n < MIN_SAMPLE` 时返回 `insufficient=True` 且**不给数值**,
   页面得显示"样本不足", 而不是拿 3 个样本算出 67% 这种数字去误导决策。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

#: 命中率最小样本量 —— 低于它不给数字(避免小样本误导)
MIN_SAMPLE = 30

#: 回填要求的未来交易日数(与 stats 的 horizon 对齐)
HORIZONS = (1, 3, 5)


def _today_cst() -> str:
    return datetime.now(_CST).date().isoformat()


def record_signal(
    engine: Engine,
    *,
    signal_kind: str,
    symbol: str,
    trade_date: str | None = None,
    price: float | None = None,
    context: dict[str, Any] | None = None,
    source: str = "",
) -> dict[str, Any]:
    """记录一个信号(幂等: 同 kind+symbol+trade_date 走 UPDATE, 不堆行)。

    `price=None` 表示**当时取不到价** —— 如实留空, 不用 0 或"之后的价格"顶上。
    """
    day = trade_date or _today_cst()
    sym = str(symbol or "").strip()
    kind = str(signal_kind or "").strip()
    if not kind or not sym:
        raise ValueError("signal_kind 与 symbol 都必填")
    ctx = json.dumps(context or {}, ensure_ascii=False, separators=(",", ":"))[:4000]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
INSERT INTO decision_log (signal_kind, symbol, trade_date, price_at_signal, context_json, source)
VALUES (:kind, :symbol, :day, :price, :ctx, :source)
ON CONFLICT (signal_kind, symbol, trade_date) DO UPDATE SET
  price_at_signal = COALESCE(excluded.price_at_signal, decision_log.price_at_signal),
  context_json = excluded.context_json,
  source = excluded.source
"""
            ),
            {"kind": kind, "symbol": sym, "day": day, "price": price, "ctx": ctx, "source": source},
        )
    return {"signal_kind": kind, "symbol": sym, "trade_date": day, "price": price}


def record_many(engine: Engine, rows: list[dict[str, Any]], *, source: str = "") -> int:
    """批量记录(同一个 source); 单条失败只记 warning, 不拖垮整批。"""
    ok = 0
    for r in rows:
        try:
            record_signal(engine, source=source, **r)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("决策日志写入失败 %s: %r", r.get("symbol"), exc)
    return ok


def _close_series_pg(engine: Engine, symbol: str, start_day: str) -> list[tuple[str, float]]:
    """该标的从 start_day 起的日线收盘(升序, 每交易日一根)。

    `klines` 是 TimescaleDB hypertable, 时间列是 **`ts`**(不是 trade_date), 且同一 (symbol, period, ts)
    可能有多源 → 这里按 `ts, source` 排序后在 Python 侧**按日期去重**(每交易日取第一条, 结果确定)。
    取不到就返回空 —— 由调用方决定"这根还没到/这天没数据", 不做任何推算。
    """
    rows: list[tuple[str, float]] = []
    try:
        with engine.connect() as conn:
            rs = conn.execute(
                text(
                    "SELECT ts, close, source FROM klines "
                    "WHERE symbol = :s AND period = '1d' AND ts >= :d "
                    "ORDER BY ts ASC, source ASC LIMIT 200"
                ),
                {"s": symbol, "d": f"{start_day} 00:00:00+08"},
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 —— 表不存在(本地/早期库)也算取不到
        logger.debug("决策日志取K线失败 %s: %r", symbol, exc)
        return []
    seen: set[str] = set()
    for ts, close, _source in rs:
        if close is None:
            continue
        day = str(ts)[:10]
        if day in seen:
            continue
        seen.add(day)
        rows.append((day, float(close)))
    return rows


def backfill_outcomes(
    engine: Engine,
    *,
    limit: int = 500,
    series_provider: Any = None,
) -> dict[str, int]:
    """回填 T+1/3/5 收益与命中。

    **只填"未来那根 K 线已经存在"的部分**: 不足 T+n 的只填得出来的那几档,
    一档都填不出来就原样留着(下次再填) —— 不推算、不假设。
    命中定义: 收益 > 0 记 1, ≤0 记 0(**含 0 为未命中**, 不把平盘算成赚)。
    """
    filled = 0
    scanned = 0
    with engine.begin() as conn:
        pend = conn.execute(
            text(
                "SELECT id, symbol, trade_date, price_at_signal FROM decision_log "
                "WHERE ret_t5 IS NULL OR ret_t3 IS NULL OR ret_t1 IS NULL "
                "ORDER BY trade_date ASC LIMIT :lim"
            ),
            {"lim": int(limit)},
        ).fetchall()

    for pid, symbol, day, base in pend:
        scanned += 1
        if base is None:
            # 当时就没价 → 这里也补不出"当时的价", 只能等(不拿今天的价冒充)
            continue
        provider = series_provider or _close_series_pg
        series = provider(engine, symbol, str(day))
        # series[0] 必须是信号日当根; 否则说明该日无 K 线(停牌/非交易日) → 不硬填
        if not series or series[0][0] != str(day):
            continue
        sets: dict[str, Any] = {}
        for n in HORIZONS:
            if len(series) > n:
                c = series[n][1]
                ret = (c - float(base)) / float(base)
                sets[f"ret_t{n}"] = round(ret, 6)
                sets[f"hit_t{n}"] = 1 if ret > 0 else 0
        if not sets:
            continue
        assign = ", ".join(f"{k} = :{k}" for k in sets)
        with engine.begin() as conn:
            conn.execute(
                text(f"UPDATE decision_log SET {assign}, filled_at = CURRENT_TIMESTAMP WHERE id = :pid"),
                {**sets, "pid": pid},
            )
        filled += 1

    return {"scanned": scanned, "filled": filled}


def stats(engine: Engine, *, days: int = 180, min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    """按信号类型统计命中率。

    `n < min_sample` → 该档返回 `insufficient: True` 且**命中率为 None**(页面显示"样本不足"),
    不给数字, 免得 3 个样本算出 67% 去指导决策。
    """
    since = (date.fromisoformat(_today_cst()) - timedelta(days=max(7, int(days)))).isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
SELECT signal_kind,
       COUNT(*) AS n,
       SUM(CASE WHEN hit_t1 IS NOT NULL THEN 1 ELSE 0 END) AS n_t1,
       SUM(CASE WHEN hit_t3 IS NOT NULL THEN 1 ELSE 0 END) AS n_t3,
       SUM(CASE WHEN hit_t5 IS NOT NULL THEN 1 ELSE 0 END) AS n_t5,
       SUM(CASE WHEN hit_t1 = 1 THEN 1 ELSE 0 END) AS w_t1,
       SUM(CASE WHEN hit_t3 = 1 THEN 1 ELSE 0 END) AS w_t3,
       SUM(CASE WHEN hit_t5 = 1 THEN 1 ELSE 0 END) AS w_t5
FROM decision_log
WHERE trade_date >= :since
GROUP BY signal_kind
ORDER BY n DESC
"""
            ),
            {"since": since},
        ).fetchall()

    out: list[dict[str, Any]] = []
    for kind, n, n1, n3, n5, w1, w3, w5 in rows:
        item: dict[str, Any] = {"signal_kind": kind, "n_total": int(n or 0), "horizons": {}}
        for label, nn, ww in (("t1", n1, w1), ("t3", n3, w3), ("t5", n5, w5)):
            have = int(nn or 0)
            if have == 0:
                item["horizons"][label] = {
                    "n": 0,
                    "hit_rate": None,
                    "insufficient": True,
                    "note": "尚无已回填样本(需要未来的 K 线才算得出来)",
                }
            elif have < min_sample:
                item["horizons"][label] = {
                    "n": have,
                    "hit_rate": None,
                    "insufficient": True,
                    "note": f"样本不足({have} < {min_sample}), 不给命中率",
                }
            else:
                item["horizons"][label] = {
                    "n": have,
                    "hit_rate": round(int(ww or 0) / have, 4),
                    "insufficient": False,
                    "note": "",
                }
        out.append(item)

    return {
        "since": since,
        "min_sample": int(min_sample),
        "rows": out,
        "note": "命中 = T+n 收益 > 0(平盘记未命中); 缺失/未回填一律不计入分母, 不用推算值填充。",
    }
