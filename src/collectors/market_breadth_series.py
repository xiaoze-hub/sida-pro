"""市场广度日序列 —— 从 PG klines 自算并落库（零网关配额）。

为什么自算而不是"每天抓快照攒"：
    1. 本库 `klines` 已有**全市场深历史**（实测 2026-09-21 及以前每日约 5480 只标的），
       一次 SQL 即可回溯任意长度；而 TQ `pricevol`（`tq.breadth` 的数据源）只有
       **当日快照**，攒不出历史基线。
    2. 广度指标（ADL/ADR/ARMS/BTI/MCL/STIX）与"情绪温度"依赖历史分位，
       样本 < 20 不给分位（见 `src/core/market_breadth.py`）。

指标定义与口径纪律见 `src/core/market_breadth.py`（**自研口径**，非通达信客户端公式，
数值可能与客户端同名指标有差异；对外展示须保留 `caliber` 标注）。

单位：家数=只；成交量=股（与 `klines.volume` 同源，**不做换算**，避免引入误差）。
多源去重：同一 (symbol, 交易日) 多源并存时取优先级 `tq > tencent > sina > 其它`，
防止同一天同一票被两只源重复计数（库口径铁律）。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text

from src.core.market_breadth import (
    BreadthBar,
    compute_series_with_percentile,
    latest_summary,
)

logger = logging.getLogger(__name__)

TABLE = "market_breadth_daily"
DEFAULT_DAYS = 260
#: 标的数少于此的交易日视为"当日数据未收全"，**不落库**（避免污染分位基线）。
MIN_FULL_DAY_SYMBOLS = 1000

_INSERT_COLS: tuple[str, ...] = (
    "trade_date", "up_count", "down_count", "flat_count", "up_volume", "down_volume",
    "adl", "adr", "arms", "bti", "bti_thrust", "mcl", "mcl_summation", "stix",
    "up_ratio", "sentiment_score", "symbols", "source",
)
_UPDATE_COLS: tuple[str, ...] = tuple(c for c in _INSERT_COLS if c != "trade_date")


def _is_pg() -> bool:
    from src.db.dialect import is_postgres

    return bool(is_postgres())


def _day_expr(col: str = "ts") -> str:
    """双方言"取日期"表达式。"""
    return f"({col})::date" if _is_pg() else f"date({col})"


def load_bars_from_pg(db, *, days: int = DEFAULT_DAYS) -> list[BreadthBar]:
    """取近 `days` 个交易日的全市场涨跌家数与涨/跌成交量。

    起始日期在 Python 侧算好再传参（双方言日期算术写法不同，避免拼接出错）。
    """
    d = _day_expr()
    # 交易日→自然日按 1.7 倍估(含长假), 再多给 30 天缓冲; 窗口越小查询越轻(实测过宽的
    # 窗口会触发 PG statement timeout)。
    lookback_calendar_days = int(int(days) * 1.7) + 30
    start = date.today() - timedelta(days=lookback_calendar_days)
    sql = text(
        f"""
WITH dedup AS (
  SELECT symbol, {d} AS td, close, volume,
         row_number() OVER (
             PARTITION BY symbol, {d}
             ORDER BY CASE source
                        WHEN 'tq' THEN 1 WHEN 'tencent' THEN 2 WHEN 'sina' THEN 3
                        ELSE 9 END
         ) AS rn
  FROM klines
  WHERE period = '1d' AND market = 'CN' AND {d} >= :start_date
),
clean AS (SELECT symbol, td, close, volume FROM dedup WHERE rn = 1),
seq AS (
  SELECT symbol, td, close, volume,
         lag(close) OVER (PARTITION BY symbol ORDER BY td) AS prev_close
  FROM clean
)
SELECT CAST(td AS TEXT) AS trade_date,
       SUM(CASE WHEN close > prev_close THEN 1 ELSE 0 END) AS up_count,
       SUM(CASE WHEN close < prev_close THEN 1 ELSE 0 END) AS down_count,
       SUM(CASE WHEN close = prev_close THEN 1 ELSE 0 END) AS flat_count,
       COALESCE(SUM(CASE WHEN close > prev_close THEN volume ELSE 0 END), 0) AS up_volume,
       COALESCE(SUM(CASE WHEN close < prev_close THEN volume ELSE 0 END), 0) AS down_volume
FROM seq
WHERE prev_close IS NOT NULL AND close IS NOT NULL AND close > 0
GROUP BY td
ORDER BY td
"""
    )
    try:
        # 这是**每日一次**的全市场重活(250 交易日 × 5000+ 标的), 显式放宽本会话超时;
        # 用完立刻复位, 避免污染连接池里的其他查询。
        _set_timeout(db, "600s")
        try:
            rows = db.execute(sql, {"start_date": start}).fetchall()
        finally:
            _set_timeout(db, "0")
    except Exception as exc:  # noqa: BLE001
        logger.warning("market_breadth: 读取 klines 失败: %s", exc)
        return []
    bars: list[BreadthBar] = []
    for r in rows:
        td = str(r[0])[:10]
        bars.append(
            BreadthBar(
                trade_date=td,
                up=int(r[1] or 0),
                down=int(r[2] or 0),
                flat=int(r[3] or 0),
                up_volume=float(r[4] or 0.0),
                down_volume=float(r[5] or 0.0),
            )
        )
    return bars


def _set_timeout(db, value: str) -> None:
    """双方言设置本会话 statement_timeout(失败不影响主流程)。"""
    try:
        if _is_pg():
            db.execute(text(f"SET statement_timeout = '{value}'"))
        elif value != "0":
            db.execute(text(f"PRAGMA busy_timeout = {int(float(value.rstrip('s')) * 1000)}"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("market_breadth: set timeout failed: %s", exc)


def _load_stored_bars(db, *, days: int = DEFAULT_DAYS) -> list[BreadthBar]:
    """从落库表读已有序列(毫秒级, 供 chat 工具/API 使用)。

    ⚠️ chat 工具**绝不能**走 load_bars_from_pg 那条重查询(会卡住对话), 一律走这里。
    """
    sql = text(
        "SELECT trade_date, up_count, down_count, flat_count, up_volume, down_volume "
        f"FROM {TABLE} ORDER BY trade_date DESC LIMIT :lim"
    )
    rows = db.execute(sql, {"lim": int(days)}).fetchall()
    bars = [
        BreadthBar(
            trade_date=str(r[0])[:10],
            up=int(r[1] or 0),
            down=int(r[2] or 0),
            flat=int(r[3] or 0),
            up_volume=float(r[4] or 0.0),
            down_volume=float(r[5] or 0.0),
        )
        for r in rows
    ]
    bars.reverse()
    return bars


def sync_breadth_series(db, *, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """自算并落库。返回 {ok, bars, written, skipped_thin, skipped_no_volume, last_date}。

    数据不足时如实返回 `ok=False`（**不伪造**）；未收全的交易日跳过并计数。
    """
    from src.db.dialect import acquire_write, upsert_sql

    bars = load_bars_from_pg(db, days=days)
    if not bars:
        return {"ok": False, "reason": "no_data", "bars": 0, "written": 0}
    rows = compute_series_with_percentile(bars)

    sql = text(upsert_sql(TABLE, _INSERT_COLS, ["trade_date"], list(_UPDATE_COLS)))
    lock = acquire_write()
    written = skipped_thin = 0
    last_date = ""
    try:
        for bar, row in zip(bars, rows):
            total = bar.up + bar.down + bar.flat
            if total < MIN_FULL_DAY_SYMBOLS:
                skipped_thin += 1
                continue
            params = {
                "trade_date": bar.trade_date,
                "up_count": bar.up,
                "down_count": bar.down,
                "flat_count": bar.flat,
                "up_volume": bar.up_volume,
                "down_volume": bar.down_volume,
                "adl": row["adl"],
                "adr": row["adr"],
                "arms": row["arms"],
                "bti": row["bti"],
                "bti_thrust": 1 if row["bti_thrust"] else 0,
                "mcl": row["mcl"],
                "mcl_summation": row["mcl_summation"],
                "stix": row["stix"],
                "up_ratio": row["up_ratio"],
                "sentiment_score": row.get("sentiment_score"),
                "symbols": total,
                "source": "pg_klines",
            }
            db.execute(sql, params)
            written += 1
            last_date = bar.trade_date
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        lock.release()
    return {
        "ok": True,
        "bars": len(bars),
        "written": written,
        "skipped_thin": skipped_thin,
        "last_date": last_date,
    }


def latest_with_percentile(db, *, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """最新一日广度 + 各指标历史分位（API / AI 工具用）。

    读**落库表**而非重算（毫秒级）：重算要在 5000+ 标的上跑 250 交易日窗口，
    会把 chat 对话卡住（实测过重会触发 PG statement timeout）。表由每日
    `sync_breadth_series` 维护，分位在内存里按表内序列算。
    """
    bars = _load_stored_bars(db, days=days)
    if not bars:
        return {"ok": False, "reason": "no_data", "hint": "尚未生成本表数据(需先跑 sync_breadth_series)"}
    out = latest_summary(bars)
    if not out.get("ok", True):
        return {"ok": False, "reason": out.get("reason", "no_data")}
    out["ok"] = True
    total = bars[-1].up + bars[-1].down + bars[-1].flat
    out["symbols"] = total
    out["complete"] = total >= MIN_FULL_DAY_SYMBOLS
    return out


# ── 文本渲染（chat 工具出口用；纯函数便于测试）──────────────────────────

_METRIC_LABELS: tuple[tuple[str, str, str], ...] = (
    ("adl", "ADL 腾落", ""),
    ("adr", "ADR 涨跌比率", ""),
    ("arms", "ARMS 阿姆氏(TRIN)", ""),
    ("bti", "BTI 广量冲力", ""),
    ("mcl", "MCL 麦克连", ""),
    ("stix", "STIX 指数平滑广量", ""),
)


def _fmt_metric(value: Any, pct: Any) -> str:
    if value is None:
        return "—"
    s = f"{float(value):,.2f}" if abs(float(value)) >= 1000 else f"{float(value):.2f}"
    return f"{s} (分位 {pct:.0f})" if pct is not None else f"{s} (分位不足)"


def render_text(summary: dict[str, Any]) -> str:
    """把 latest_with_percentile() 的结果渲染成 chat 文本。数据缺失一律显示"—"。"""
    if not summary or not summary.get("ok"):
        return ""
    up = int(summary.get("up") or 0)
    down = int(summary.get("down") or 0)
    flat = int(summary.get("flat") or 0)
    total = int(summary.get("symbols") or (up + down + flat))
    score = summary.get("sentiment_score")
    lines = [
        f"📊 市场广度·情绪温度（{summary.get('trade_date') or '?'}）",
        f"· 涨跌家数：上涨 {up} / 下跌 {down} / 平盘 {flat}（共 {total} 只）",
        f"· 情绪温度：{score if score is not None else '—'}/100（各指标历史分位均值）",
    ]
    for key, label, _unit in _METRIC_LABELS:
        val = summary.get(key)
        pct = summary.get(f"{key}_pct")
        if key == "mcl":
            summ = summary.get("mcl_summation")
            tail = f"，累计 {float(summ):,.2f}" if summ is not None else ""
            lines.append(f"· {label}：{_fmt_metric(val, pct)}{tail}")
        elif key == "bti":
            thrust = " ⚡thrust(广量冲力信号)" if summary.get("bti_thrust") else ""
            lines.append(f"· {label}：{_fmt_metric(val, pct)}{thrust}")
        else:
            lines.append(f"· {label}：{_fmt_metric(val, pct)}")
    lines.append(f"· 样本：近 {summary.get('bars_used', '?')} 个交易日")
    if not summary.get("complete", True):
        lines.append("· ⚠️ 该交易日标的数偏少（数据未收全），涨跌家数仅供参考")
    lines.append(
        "注：广度指标为**自研口径**（教科书定义），非通达信客户端同名公式，数值可能有差异；"
        "显示『—』= 当日不可算或历史样本不足 20，**不是 0**。"
    )
    return "\n".join(lines)
