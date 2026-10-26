"""龙虎榜金额「次日 TQ 回填」（2026-10-20 用户要求）。

背景（实测）：
- 东财是**市场级枚举**的强源（TQ 没有"某日全部上榜"接口），但金额依赖外部网关，
  网关 502 的当天金额会整块缺失；
- TQ 的 GP02 金额与东财**分文不差**（2026-10-19 三只票 3/3 完全一致），
  但它**晚一个交易日**（当日 21:30 还没有当天数据，次日才有）。

⇒ 分工：当日用东财（TQ 没有）、**次日用 TQ 把金额覆写回去**。这样历史数据不再依赖
东财网关当天的可用性，坏掉的那天会在次日自动补齐。

铁律：
- TQ 没给该股该日数据 → **不动这一行**（绝不写 0、绝不拿别的日期顶替）；
- 只更新金额三列 + source 标注，名称/原因/席位一律不碰；
- 全程失败静默（记日志），不影响主流程。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

_MAX_SYMBOLS = 400


def _target_date(explicit: str | None = None) -> str:
    """回填目标日：默认「上一个交易日」（今天若是交易日，回填昨天那份榜单）。"""
    if explicit:
        return str(explicit).replace("-", "")[:8]
    from src.core.trading_calendar import prev_trading_day

    today = date.today()
    try:
        return prev_trading_day(today).strftime("%Y%m%d")
    except Exception:  # noqa: BLE001 - 日历不可用就用自然日昨天
        return (today - timedelta(days=1)).strftime("%Y%m%d")


def merge_rows(rows: list[dict], tq: dict[str, dict]) -> list[dict]:
    """纯函数：把 TQ 金额合并进库行，返回需要 UPDATE 的行（不碰读写）。

    只保留 TQ 有金额的票；返回 {symbol, buy_amt, sell_amt, net_buy, tq_buy, tq_sell}。
    """
    out: list[dict] = []
    for r in rows:
        sym = str(r.get("symbol") or "").split(".")[0]
        hit = tq.get(sym) or tq.get(f"{sym}")
        if not hit:
            continue
        buy, sell = hit.get("buy_amt"), hit.get("sell_amt")
        if buy is None and sell is None:
            continue
        out.append(
            {
                "symbol": sym,
                "buy_amt": buy,
                "sell_amt": sell,
                "net_buy": hit.get("net_buy"),
                "before_buy": r.get("buy_amt"),
                "before_sell": r.get("sell_amt"),
            }
        )
    return out


def _update_one(row: dict, trade_date: str, market: str) -> bool:
    from sqlalchemy import text

    from src.web.database import engine

    try:
        with engine.begin() as conn:
            res = conn.execute(
                text(
                    """
                    UPDATE dragon_tiger_daily
                       SET buy_amt = :b, sell_amt = :s, net_buy = :n,
                           source = 'em_ftshare+tq_amt'
                     WHERE trade_date = :d AND symbol = :sym AND market = :m
                    """
                ),
                {
                    "b": row["buy_amt"],
                    "s": row["sell_amt"],
                    "n": row["net_buy"],
                    "d": trade_date,
                    "sym": row["symbol"],
                    "m": market,
                },
            )
        return bool(res.rowcount)
    except Exception as e:  # noqa: BLE001
        logger.warning("[lhb-tq-backfill] UPDATE 失败 %s %s: %s", trade_date, row["symbol"], e)
        return False


def backfill(trade_date: str | None = None, market: str = "CN") -> dict:
    """对一个交易日做 TQ 金额回填。返回统计（供日志/自检）。"""
    d = _target_date(trade_date)
    try:
        from src.web.api.market_data import _lhb_amounts_from_tq, _lhb_read_date
    except Exception as e:  # noqa: BLE001
        logger.warning("[lhb-tq-backfill] 依赖导入失败: %s", e)
        return {"trade_date": d, "ok": False, "reason": f"import: {e}"}

    rows = _lhb_read_date(d, market)
    if not rows:
        logger.info("[lhb-tq-backfill] %s 库内无龙虎榜数据 → 跳过", d)
        return {"trade_date": d, "ok": True, "rows": 0, "updated": 0, "note": "库内无数据"}
    if len(rows) > _MAX_SYMBOLS:
        logger.info("[lhb-tq-backfill] %s 上榜 %d 只 > %d，截断", d, len(rows), _MAX_SYMBOLS)
        rows = rows[:_MAX_SYMBOLS]

    symbols = [str(r.get("symbol")) for r in rows if r.get("symbol")]
    tq = _lhb_amounts_from_tq(symbols, d, max_symbols=_MAX_SYMBOLS, budget_sec=60.0)
    merged = merge_rows(rows, tq)
    updated = sum(1 for r in merged if _update_one(r, d, market))

    # 回填后清掉该日响应缓存，避免前端继续读到旧金额
    try:
        from src.web.cache.biz_cache import biz_cache

        biz_cache.delete(f"mkt:dragon_tiger:{market}:{d}")
    except Exception:  # noqa: BLE001
        pass

    stats = {
        "trade_date": d,
        "ok": True,
        "rows": len(rows),
        "tq_hit": len(merged),
        "updated": updated,
        "miss": len(rows) - len(merged),
        "source": "tdx_tq_gp02",
    }
    logger.info("[lhb-tq-backfill] %s 回填完成: %s", d, stats)
    return stats


def run_scheduled() -> dict:
    """调度入口：交易日 08:30，回填上一个交易日。"""
    try:
        from src.core.trading_calendar import is_trading_day

        if not is_trading_day():
            return {"skipped": "非交易日"}
    except Exception:  # noqa: BLE001
        pass
    return backfill()
