"""TQ 条件选股信号日序列采集器(2026-09-25)。

口径 / 来源
----------
数据源 = 通达信客户端 TQ 网关 `formula_process_mul_xg`(条件选股, formula_type=1)。
全市场(实测 5577 只)扫一遍 **24s/公式**(500 只/批 × 12 批, 实测), 无限频、零外部依赖、免费。
本模块默认 10 个公式 ≈ **4 分钟**(盘后跑, 客户端此时空闲)。
对照: 问财限频 250ms; 东财/同花顺免费层**没有**"某技术条件当日触发家数"的口径。

为什么落库
--------
"今天有 38 只票 MACD 买入信号"本身没有意义, 有意义的是**相对基线**
("38 家 vs 近 20 日均值 12 家" = 技术面集体转强)。免费源只给当日快照,
没有历史序列就算不出基线 —— 与 `market_sentiment_daily` 同一理由。

诚实性约束
--------
`formula_scan` 的 `complete=False` 表示扫描中有分片失败, 此时 `hit_count`
**不是**全市场命中数。该标记原样落库, 消费方必须显示为不完整,
**不得**当成"全市场家数"使用(那是编造)。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from marketdata.vendors.tq import formula_scan

logger = logging.getLogger(__name__)

#: 落库的公式集合: (acCode, 展示名, 参数)。
#: 取自 `formula_get_all(1)` 的 108 个条件选股公式, 挑**能横截面判读市场宽度**的三类:
#: 买入信号 / 卖出信号 / 连涨连跌。新增一行即可, 无需改迁移。
BASE_FORMULAS: tuple[tuple[str, str, str], ...] = (
    ("MACD买入", "MACD买入信号", ""),
    ("KDJ买入", "KDJ买入信号", ""),
    ("MA买入", "均线买入信号", ""),
    ("BOLL买入", "布林带买入信号", ""),
    ("BIAS买入", "乖离率买入信号", ""),
    ("W&R买入", "威廉指标买入信号", ""),
    ("MACD卖出", "MACD卖出信号", ""),
    ("KDJ卖出", "KDJ卖出信号", ""),
    ("UPN", "连涨3天", "3"),
    ("DOWNN", "连跌3天", "3"),
)

#: 「启动早期信号」组（2026-09-25 新增）：量价异动 + K线形态。
#: 与上面三类同属**市场宽度**视角（当日全市场触发家数 + 近 N 日基线），
#: 回答"今天有多少票刚出现放量/突破/见底形态"——它只说客观条件满足多少只，
#: **不是**"推荐买入"。acCode 取自客户端 `formula_all(1)` 实测（勿手写猜）。
STARTUP_FORMULAS: tuple[tuple[str, str, str], ...] = (
    ("C116", "放量上攻", ""),
    ("C117", "温和放量上攻", ""),
    ("C112", "阶段放量", ""),
    ("C113", "持续放量", ""),
    ("C118", "突然放量", ""),
    ("C131", "下跌多日再放量上涨", ""),
    ("C120", "小步碎阳", ""),
    ("C123", "突破长期盘整", ""),
    ("RED4", "四串阳", ""),
    ("C107", "连续N天收阳线", "3"),
    ("C134", "昨日底部十字星", ""),
    ("C135", "价量渐低后阳包阴", ""),
    ("MSTAR", "早晨之星", ""),
    ("SUNBY", "阳包阴", ""),
)


#: 实际参与扫描的完整清单 = 基础组 + 启动早期信号组。
#: ⚠️ 只定义 STARTUP_FORMULAS 而忘了并进来 = 这些公式**永远不会被扫**
#:   （2026-09-25 踩过：扫描"成功"但新公式一行都没有，看起来像没信号）。
#:   由 tests/test_startup_signals.py::test_startup_formulas_are_wired 守住。
FORMULA_SET: tuple[tuple[str, str, str], ...] = BASE_FORMULAS + STARTUP_FORMULAS

#: 命中清单落库上限(超出只存前 N 只并置 truncated=1)。
HITS_STORE_LIMIT = 500

#: 基线窗口(交易日近似值, 与 market_sentiment_daily 的基线口径一致)。
DEFAULT_BASELINE_DAYS = 20


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def _norm_day(d: str) -> str:
    """把交易日期归一成客户端要求的**紧凑格式** `YYYYMMDD`。

    客户端/`formula_scan` 只认 `20260924`；传 `2026-09-24`（ISO，最常见的写法）
    会被判定为"该日无数据行"→ 全部公式跳过 → **静默返回 0 行**，
    看起来像"今天没有信号"，实际是参数没归一（2026-09-25 实测踩到）。
    """
    return (d or "").strip().replace("-", "").replace("/", "").replace(".", "")


def fetch_formula_signals(trade_date: str = "") -> list[dict]:
    """扫 FORMULA_SET 的每个公式 → 行字典列表(不含 DB 写入)。

    单公式失败**不拖垮其余公式**: 该公式本日不出行(由下一次运行补齐),
    而不是写一行 0(0 会被误读成"今天没人触发")。
    """
    day = _norm_day(trade_date) or _today()
    rows: list[dict] = []
    for code, name, arg in FORMULA_SET:
        try:
            scan = formula_scan(code, formula_arg=arg, date=day)
        except Exception as e:  # noqa: BLE001 — 单公式失败不影响其余
            logger.warning("条件选股信号采集: %s 扫描失败(%s)", code, e)
            continue
        if scan.get("date_has_data") is False:
            # 该日**根本没有数据行** = 非交易日(或窗口未覆盖) → 跳过。
            # 落了 0 会把基线拉低(用假期稀释真实基线), 而"0 家"这种真实情况
            # 是有数据行且值全 0 —— 两者必须分开(见 tq._date_rows_seen)。
            logger.info("条件选股信号: %s 在 %s 无数据行(非交易日?), 跳过不落 0", code, day)
            continue
        hits = scan.get("hits") or []
        kept = hits[:HITS_STORE_LIMIT]
        rows.append({
            "trade_date": scan.get("date") or day,
            "formula_code": code,
            "formula_name": name,
            "formula_arg": arg,
            "hit_count": int(scan.get("hit_count") or 0),
            "scanned_count": int(scan.get("scanned") or 0),
            "chunks_failed": int(scan.get("chunks_failed") or 0),
            "complete": 1 if scan.get("complete") else 0,
            "truncated": 1 if len(hits) > HITS_STORE_LIMIT else 0,
            "hits_json": json.dumps([h.get("symbol") for h in kept], ensure_ascii=False),
        })
    return rows


def _upsert_rows(db, rows: list[dict]) -> int:
    """按 (trade_date, formula_code, formula_arg) 幂等 upsert。"""
    from sqlalchemy import text

    written = 0
    for row in rows:
        cols = [
            "trade_date", "formula_code", "formula_name", "formula_arg",
            "hit_count", "scanned_count", "chunks_failed", "complete",
            "truncated", "hits_json",
        ]
        params = {c: row.get(c) for c in cols}
        placeholders = ", ".join(f":{c}" for c in cols)
        set_clause = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in cols if c not in ("trade_date", "formula_code", "formula_arg")
        )
        sql = (
            f"INSERT INTO tq_formula_signal_daily ({', '.join(cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (trade_date, formula_code, formula_arg) DO UPDATE SET "
            f"{set_clause}, updated_at = CURRENT_TIMESTAMP"
        )
        db.execute(text(sql), params)
        written += 1
    db.commit()
    return written


def sync_formula_signals(db, *, trade_date: str = "") -> dict:
    """采集并落库。返回汇总或 {error}。异常不抛给定时器。"""
    try:
        rows = fetch_formula_signals(trade_date)
        if not rows:
            return {"error": "无任何公式返回数据(非交易日 / TQ 客户端未开 / 公式名不可用)"}
        written = _upsert_rows(db, rows)
        incomplete = [r["formula_code"] for r in rows if not r["complete"]]
        total_hits = {r["formula_code"]: r["hit_count"] for r in rows}
        return {
            "rows": written,
            "trade_date": rows[0]["trade_date"],
            "hits": total_hits,
            "incomplete": incomplete,
        }
    except Exception as e:  # noqa: BLE001 — 定时任务不因单源失败炸掉
        logger.warning("sync_formula_signals 失败: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"error": str(e)[:200]}


def latest_with_baseline(db, *, days: int = DEFAULT_BASELINE_DAYS) -> dict:
    """最新一日各公式信号家数 + 近 N 日均值/极值(供端点与 UI 判读)。

    返回 {trade_date, items: [{formula_code, formula_name, formula_arg, hit_count, scanned_count,
    complete, truncated, baseline_avg, baseline_max, baseline_min, samples}]}。
    没有历史时 baseline 全为 None —— **不填占位数字**(缺失就是缺失)。
    """
    from sqlalchemy import text

    q = text(
        """
        SELECT trade_date, formula_code, formula_name, formula_arg,
               hit_count, scanned_count, complete, truncated
          FROM tq_formula_signal_daily
         ORDER BY trade_date DESC
        """
    )
    rows = [dict(r._mapping) for r in db.execute(q).fetchall()]
    if not rows:
        return {"trade_date": "", "items": []}

    latest = rows[0]["trade_date"]
    by_formula: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_formula.setdefault((r["formula_code"], r["formula_arg"]), []).append(r)

    items = []
    for (code, arg), series in by_formula.items():
        head = series[0]
        hist = [r["hit_count"] for r in series[1: 1 + days]]
        items.append({
            "formula_code": code,
            "formula_name": head["formula_name"],
            "formula_arg": arg,
            "trade_date": head["trade_date"],
            "hit_count": head["hit_count"],
            "scanned_count": head["scanned_count"],
            "complete": bool(head["complete"]),
            "truncated": bool(head["truncated"]),
            "baseline_avg": round(sum(hist) / len(hist), 1) if hist else None,
            "baseline_max": max(hist) if hist else None,
            "baseline_min": min(hist) if hist else None,
            "samples": len(hist),
            "is_latest_date": head["trade_date"] == latest,
        })
    items.sort(key=lambda x: x["hit_count"], reverse=True)
    return {"trade_date": latest, "items": items}
