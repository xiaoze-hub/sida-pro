"""妖股因子存档与增量管线(批次B 存档化, 2026-09-06 28号, 老板要求)。

设计:
- 存档层: limit_up_events(原始涨停事件, 不可变追加) → 因子层: demon_factors
  (每股最新六维快照, unique(symbol))。因子可随时从事件全量重算, 不丢口径。
- 回填双通道:
  * backfill_direct_tq: TQ 直连批量(本机网关 ~30ms/股, 全市场 5-10 分钟),
    存档重建/首次全量用;
  * backfill_incremental: 每日增量——只拉近 N 日 K 线, 只补库里没有的新日期,
    只对当日有新事件的股票重算因子(老板要求: 后面日期作增量, 快很多)。
  Engine 多源链是盘中实时兜底用的, 批量回填陪它熬长尾失败链没有意义
  (实测周日长尾每股 10-15s 全源失败等待)。
- 管线: update_pipeline() = 增量回填 → 新事件股票因子重算 → 落库。15:30 cron。
"""
from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_EVENTS = "limit_up_events"
_FACTORS = "demon_factors"
_WINDOW_DAYS = 260
_LOOKBACK_BARS = 15  # 增量回取 K 线根数(覆盖长假期停牌)
_TQ_WORKERS = 4


def _now_cst_iso() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def _existing_max_dates() -> dict[str, str]:
    """全表每股最大事件日(一次查询, 增量判断用)。无事件的股票不在结果里。"""
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text(f"SELECT symbol, MAX(trade_date) AS d FROM {_EVENTS} GROUP BY symbol")
        ).fetchall()
    finally:
        db.close()
    return {r[0]: r[1] for r in rows}


def _tq_bars_direct(symbol: str, days: int) -> list[dict]:
    """TQ 直连日 K(绕过 Engine 多源链)。失败返回空。"""
    pkgs = str(Path(__file__).parent.parent / "packages" / "marketdata" / "src")
    if pkgs not in sys.path:
        sys.path.insert(0, pkgs)
    try:
        from marketdata.symbol import Symbol as _Sym
        from marketdata.vendors.tq import TqKlineVendor

        bars = TqKlineVendor().fetch([_Sym.parse(symbol, "CN")], {"days": days})
        return [
            {
                "date": str(b.date).replace("-", ""),
                "open": b.open,
                "close": b.close,
                "high": b.high,
                "low": b.low,
                "volume": b.volume,
            }
            for b in bars
        ]
    except Exception as e:  # noqa: BLE001
        logger.debug("TQ 直连日K %s 失败: %s", symbol, e)
        return []


def backfill_direct_tq(
    symbols: list[str],
    names: dict[str, str] | None = None,
    days: int = _WINDOW_DAYS,
    workers: int = _TQ_WORKERS,
) -> dict:
    """TQ 直连全量回填(存档重建用)。多线程, 返回统计。"""
    started = _now_cst_iso()
    names = names or {}
    saved = 0
    done = 0

    def _one(sym: str) -> int:
        try:
            from src.core.limit_up_backfill import _extract_events_from_bars, _upsert_rows

            bars = _tq_bars_direct(sym, days)
            if not bars or len(bars) < 2:
                return 0
            return _upsert_rows(_extract_events_from_bars(sym, names.get(sym, ""), bars))
        except Exception as e:  # noqa: BLE001
            logger.warning("TQ直连回填 %s 失败: %s", sym, e)
            return 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, s): s for s in symbols}
        for f in as_completed(futs):
            saved += f.result()
            done += 1
            if done % 500 == 0:
                logger.info("TQ直连回填进度: %s/%s, 累计事件 %s", done, len(symbols), saved)
    return {
        "mode": "direct_tq",
        "started_at": started,
        "finished_at": _now_cst_iso(),
        "stocks": len(symbols),
        "events_saved": saved,
    }


def backfill_incremental(symbols: list[str], names: dict[str, str] | None = None) -> dict:
    """每日增量回填: 只拉近 _LOOKBACK_BARS 根 K 线, 只补库里没有的新日期。

    返回 {symbols_with_new_events: [...], events_saved} —— 新事件股票进因子重算。
    """
    names = names or {}
    known = _existing_max_dates()
    touched: list[str] = []
    saved = 0
    for sym in symbols:
        try:
            from src.core.limit_up_backfill import _extract_events_from_bars, _upsert_rows

            bars = _tq_bars_direct(sym, _LOOKBACK_BARS)
            if not bars or len(bars) < 2:
                continue
            max_db = known.get(sym)
            new_bars = [b for b in bars if max_db is None or str(b["date"]) > max_db]
            if not new_bars:
                continue
            prev = [b for b in bars if max_db is None or str(b["date"]) <= max_db]
            # 新日期的事件需要前一根作 prev_close → 从包含前一根的完整序列提取再过滤
            seq = prev[-1:] + new_bars if prev else new_bars
            rows = [
                r
                for r in _extract_events_from_bars(sym, names.get(sym, ""), seq)
                if max_db is None or str(r["trade_date"]) > max_db
            ]
            n = _upsert_rows(rows)
            if n:
                saved += n
                touched.append(sym)
        except Exception as e:  # noqa: BLE001
            logger.warning("增量回填 %s 失败: %s", sym, e)
    return {"mode": "incremental", "stocks": len(symbols), "events_saved": saved, "touched": touched}


def recompute_factors(symbols: list[str] | None = None) -> dict:
    """因子重算并落库(存档)。symbols=None → 全市场有事件的股票。

    单次批量拉取近一年事件分组重算, 每股一条 upsert(最新快照口径)。
    2026-09-10: 接入龙虎榜(dragon_tiger_events) → lhb 维(近一年上榜天数)
    与 circ_mv(流通市值加分)不再恒缺数据; 榜单未覆盖的股票 n_lhb=0(真实 0)。
    """
    from src.core.demon_score import demon_score_from_events
    from src.core.lhb_backfill import lhb_stats
    from src.db.session import SessionLocal

    cutoff = _year_ago()
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                f"SELECT trade_date, symbol, name, limit_price, open_price, high_price, low_price,"
                " close_price, touched, is_sealed_close, one_way, open_count, first_time,"
                f" max_seal_amount, limit_days, circ_mv FROM {_EVENTS}"
                " WHERE trade_date >= :cutoff ORDER BY symbol, trade_date"
            ),
            {"cutoff": cutoff},
        ).fetchall()
    finally:
        db.close()
    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r._mapping)
        by_symbol.setdefault(d["symbol"], []).append(d)
    if symbols is not None:
        targets = {s: by_symbol.get(s, []) for s in symbols}
    else:
        targets = by_symbol
    try:
        lhb = lhb_stats(symbols=None if symbols is None else list(targets.keys()))
    except Exception as e:  # noqa: BLE001
        logger.warning("龙虎榜统计失败, lhb 维退回缺数据口径: %s", e)
        lhb = {}

    updated = 0
    factor_date = datetime.now(_CST).strftime("%Y%m%d")
    db = SessionLocal()
    try:
        for sym, events in targets.items():
            if not events:
                continue
            name = next((e.get("name") for e in events if e.get("name")), None)
            st = lhb.get(sym) or {}
            # 表有数据才启用"未上榜=真实 0"口径; 统计失败/表空时传 None 保持缺数据语义
            n_lhb = st.get("n_lhb", 0) if lhb else None
            score = demon_score_from_events(
                events, circ_mv=st.get("circ_mv"), n_lhb=n_lhb
            )
            payload = {
                "factor_date": factor_date,
                "symbol": sym,
                "name": name,
                "total": score.get("total"),
                "grade": score.get("grade"),
                "n_events": score.get("n_events"),
                "n_sealed": score.get("n_sealed"),
                "max_streak": score.get("max_streak"),
                "participation": score.get("participation"),
                "dims": json.dumps(score.get("dims") or {}, ensure_ascii=False),
                "flags": json.dumps(score.get("flags") or [], ensure_ascii=False),
                "updated_at": _now_cst_iso(),
            }
            try:
                exists = db.execute(
                    text(f"SELECT 1 FROM {_FACTORS} WHERE symbol = :s"),
                    {"s": sym},
                ).first()
                if exists:
                    sets = ",".join(f"{k} = :{k}" for k in payload if k != "symbol")
                    db.execute(text(f"UPDATE {_FACTORS} SET {sets} WHERE symbol = :symbol"), payload)
                else:
                    cols = ",".join(payload.keys())
                    binds = ",".join(f":{k}" for k in payload)
                    db.execute(text(f"INSERT INTO {_FACTORS} ({cols}) VALUES ({binds})"), payload)
                updated += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("因子写库 %s 失败: %s", sym, e)
                db.rollback()
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("因子落库整体失败: %s", e)
        db.rollback()
    finally:
        db.close()
    return {"factor_date": factor_date, "updated": updated}


def update_pipeline(symbols: list[str] | None = None) -> dict:
    """15:30 盘后增量管线入口: 增量回填 → 新事件股票因子重算。永不抛异常。"""
    try:
        names = _stock_names()
        syms = symbols or list(names.keys())
        inc = backfill_incremental(syms, names)
        touched = inc.get("touched") or []
        rec = recompute_factors(touched if touched else None if symbols is None else symbols)
        out = {"incremental": inc, "factors": rec}
        logger.info("妖股因子增量管线完成: 事件+%s, 因子更新%s", inc.get("events_saved"), rec.get("updated"))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("妖股因子增量管线失败: %s", e)
        return {"error": str(e)}


def _stock_names() -> dict[str, str]:
    try:
        from src.collectors.stock_list import get_stock_list

        return {
            s["symbol"]: (s.get("name") or "")
            for s in get_stock_list()
            if s.get("symbol") and len(str(s["symbol"])) == 6
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("股票名单加载失败: %s", e)
        return {}


def _year_ago() -> str:
    from datetime import timedelta

    return (datetime.now(_CST) - timedelta(days=365)).strftime("%Y%m%d")


def load_factor_pool(topn: int = 50, min_total: float = 0) -> list[dict]:
    """妖股池直读因子表(总因子分降序)。空表返回 [](调用方可回退现算)。"""
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                f"SELECT factor_date, symbol, name, total, grade, n_events, n_sealed, max_streak,"
                f" participation, dims, flags, updated_at FROM {_FACTORS}"
                " WHERE total IS NOT NULL AND total >= :min_total"
                " ORDER BY total DESC LIMIT :lim"
            ),
            {"min_total": min_total, "lim": max(min(topn, 200), 1)},
        ).fetchall()
    finally:
        db.close()
    out = []
    for r in rows:
        d = dict(r._mapping)
        try:
            d["dims"] = json.loads(d.get("dims") or "{}")
        except (TypeError, ValueError):
            d["dims"] = {}
        try:
            d["flags"] = json.loads(d.get("flags") or "[]")
        except (TypeError, ValueError):
            d["flags"] = []
        out.append(d)
    return out


# 妖股等级 → 盘前埋伏传导维加成(批次C×B 集成, 2026-09-06)
DEMON_BOOST_BY_GRADE = {"极妖": 3, "妖": 2, "活跃": 1}


def top_demon_factors(n: int = 30, min_events: int = 5) -> dict[str, dict]:
    """先锋组因子映射: {symbol: factor}——盘前埋伏评分/简报直接消费。

    独立于 API 缓存, 失败返回空映射(不阻塞盘前主链路)。
    """
    try:
        pool = load_factor_pool(topn=max(n, 1), min_total=0)
        return {
            p["symbol"]: p
            for p in pool
            if p.get("symbol") and (p.get("n_events") or 0) >= min_events
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("先锋组因子查询失败: %s", e)
        return {}
