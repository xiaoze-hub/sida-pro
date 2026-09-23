"""TQ 专业序列 → 市场情绪日序列落库(SCJYVALUE 编号空间)。

为什么单独建表: 东财/腾讯这类免费源只给**当日快照**, 而情绪周期判断需要
"今日 vs 近 20 日均值"这种历史基线。通达信客户端的 SCJYVALUE 序列自带完整
历史(实测 2025-01-02 起 420+ 交易日, 34/35 张表有值), 落库一次即可永久用于
分位/均值/极值对比, 不必每天抓快照攒。

单位口径(官方文档, 不做换算以免引入误差):
  家数=家; SC1/SC10/SC11/SC25=万元; SC2/SC8/SC12/SC13/SC14/SC15/SC16-19/SC27=亿元;
  SC5/SC6/SC7=手; SC9=户; SC21/SC22/SC26=%。

数据源降级: TQ 客户端未开/PANWATCH_ENABLE_TQ!=1 → 直接抛 RuntimeError,
由调用方(定时任务/API)标记为不可用, **不伪造数据**。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# SC 编号 -> (第1个值, 第2个值 或 None) 对应的表列名
SC_METRICS: dict[str, tuple[str, str | None]] = {
    "SC1": ("margin_fin_balance", "margin_sec_balance"),
    "SC3": ("limit_up_count", "limit_up_open_count"),
    "SC4": ("limit_down_count", "limit_down_open_count"),
    "SC5": ("ih_net_position", None),
    "SC6": ("ic_net_position", None),
    "SC7": ("im_net_position", None),
    "SC8": ("etf_scale", "etf_net_sub"),
    "SC9": ("new_investor_natural", "new_investor_inst"),
    "SC10": ("holder_increase", "holder_decrease"),
    "SC11": ("block_premium", "block_discount"),
    "SC12": ("unlock_plan", "unlock_actual"),
    "SC13": ("dividend_total", None),
    "SC14": ("fundraising_total", None),
    "SC15": ("seal_success_money", "seal_fail_money"),
    "SC16": ("lhb_buy", "lhb_sell"),
    "SC17": ("lhb_inst_buy", "lhb_inst_sell"),
    "SC18": ("lhb_yyb_buy", "lhb_yyb_sell"),
    "SC19": ("lhb_hsgt_buy", "lhb_hsgt_sell"),
    "SC23": ("streak_count", "streak_count_ex"),
    "SC24": ("hard_limit_up_count", "hard_limit_down_count"),
    "SC25": ("margin_buy_amount", "margin_sell_volume"),
    "SC26": ("pledge_ratio_total", None),
    "SC27": ("pboc_net_injection", None),
}

# 主链: 这些表决定 trade_date 全集(它们每个交易日都有值)
CORE_TABLES: tuple[str, ...] = tuple(SC_METRICS)

# 交易日骨架: SC3(沪深涨停股个数)每个 A 股交易日必有值, 且只在交易日发布。
# ⚠️ 不能用"所有表的日期并集": SC13(分红)/SC14(募资)/SC27(央行净投放) 按**自然日**
# 发布, 会把周末/节假日塞进行集 → 一系列 NULL 情绪列污染 20 日均值基线。
# (实测: 用并集会得到 377 行/窗口 420 自然日, 其中 95 行 limit_up_count 为 NULL)
SPINE_TABLE = "SC3"

# 通达信扩展序列, 官方文档未列; 先进 extra, 标定后再升列
EXTRA_TABLES: tuple[str, ...] = tuple(f"SC{i}" for i in (28, 29, 30, 31, 32, 33, 34, 35))

ALL_TABLES: tuple[str, ...] = CORE_TABLES + EXTRA_TABLES

# 情绪基线的默认回看窗口(交易日)
DEFAULT_BASELINE_DAYS = 20

_INT_COLUMNS = frozenset(
    {
        "limit_up_count",
        "limit_up_open_count",
        "limit_down_count",
        "limit_down_open_count",
        "streak_count",
        "streak_count_ex",
        "hard_limit_up_count",
        "hard_limit_down_count",
    }
)


def _to_num(v, *, as_int: bool = False):
    """'34.00' → 34 / 34.0; 空/异常 → None(不用 0 冒充缺失)。"""
    if v is None or v == "":
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return int(f) if as_int else f


def _series_to_row(table: str, rows: list) -> dict[str, dict]:
    """单表序列 → {trade_date: {col: value}}。Value 数组按位置映射到列名。"""
    if table not in SC_METRICS or not isinstance(rows, list):
        return {}
    c1, c2 = SC_METRICS[table]
    out: dict[str, dict] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        d = str(item.get("Date") or "").strip()
        if len(d) != 8 or not d.isdigit():
            continue
        vals = item.get("Value")
        if not isinstance(vals, list):
            continue
        cell: dict = {}
        if c1 and len(vals) > 0:
            cell[c1] = _to_num(vals[0], as_int=c1 in _INT_COLUMNS)
        if c2 and len(vals) > 1:
            cell[c2] = _to_num(vals[1], as_int=c2 in _INT_COLUMNS)
        if cell:
            out[d] = cell
    return out


def _extra_to_map(tables_raw: dict) -> dict[str, dict]:
    """SC28-35 → {trade_date: {SCxx: [v1, v2]}}。"""
    out: dict[str, dict] = {}
    for table in EXTRA_TABLES:
        rows = tables_raw.get(table)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            d = str(item.get("Date") or "").strip()
            vals = item.get("Value")
            if len(d) != 8 or not isinstance(vals, list):
                continue
            nums = [_to_num(x) for x in vals[:2]]
            out.setdefault(d, {})[table] = nums
    return out


def fetch_sentiment_series(start_time: str = "", end_time: str = "") -> list[dict]:
    """拉取市场级情绪序列 → [{trade_date, **列, extra:{...}}, ...] 按日期升序。

    行集 = SPINE_TABLE(SC3)的日期 —— 保证是**纯交易日**。其他表的值合并到这些日期上,
    非交易日发布的数据(SC13/SC14/SC27)按自然日落不到骨架上, 丢弃(它们不是情绪指标)。

    start_time 默认回看 400 天。TQ 不可用/客户端未开时抛 RuntimeError。
    """
    from marketdata.vendors.tq import sc_series

    if not start_time:
        start_time = (date.today() - timedelta(days=400)).strftime("%Y%m%d")

    raw = sc_series(list(ALL_TABLES), start_time=start_time, end_time=end_time or "")
    if not raw:
        return []

    spine_rows = raw.get(SPINE_TABLE)
    if not spine_rows:
        # 骨架表缺失 → 退化为核心表并集, 但记警告(基线可信度下降)
        logger.warning(
            "tq_sentiment: 骨架表 %s 无数据, 退化为核心表并集(行集可能含非交易日)",
            SPINE_TABLE,
        )
        spine_dates = {
            str(r.get("Date"))
            for t in CORE_TABLES
            for r in (raw.get(t) or [])
            if isinstance(r, dict) and str(r.get("Date") or "").isdigit()
        }
    else:
        spine_dates = {
            str(r.get("Date"))
            for r in spine_rows
            if isinstance(r, dict) and len(str(r.get("Date") or "")) == 8
        }

    merged: dict[str, dict] = {}
    for table, rows in raw.items():
        for d, cell in _series_to_row(table, rows).items():
            if d in spine_dates:
                merged.setdefault(d, {}).update(cell)

    extras = _extra_to_map(raw)
    out: list[dict] = []
    for d in sorted(spine_dates):
        row = {"trade_date": d, **merged.get(d, {})}
        ex = extras.get(d)
        if ex:
            row["extra"] = json.dumps(ex, ensure_ascii=False)
        out.append(row)
    return out


_UPSERT_COLUMNS: tuple[str, ...] = (
    tuple(dict.fromkeys(c for pair in SC_METRICS.values() for c in pair if c))
    + ("extra",)
)


def _upsert_rows(db, rows: list[dict], market: str = "CN") -> int:
    """按 (trade_date, market) 幂等 upsert。只写取到的列, 缺列不动(避免用 None 覆盖已有值)。"""
    from sqlalchemy import text

    written = 0
    for row in rows:
        cols = [c for c in _UPSERT_COLUMNS if row.get(c) is not None]
        if not cols:
            continue
        params = {"trade_date": row["trade_date"], "market": market}
        for c in cols:
            params[c] = row[c]
        placeholders = ", ".join(f":{c}" for c in cols)
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
        sql = (
            f"INSERT INTO market_sentiment_daily (trade_date, market, {', '.join(cols)}) "
            f"VALUES (:trade_date, :market, {placeholders}) "
            f"ON CONFLICT (trade_date, market) DO UPDATE SET "
            f"{set_clause}, updated_at = CURRENT_TIMESTAMP"
        )
        db.execute(text(sql), params)
        written += 1
    db.commit()
    return written


def sync_sentiment_series(db, *, start_time: str = "", days: int = 0) -> dict:
    """采集并落库。days>0 时按天数回看(自然日); 否则 start_time(YYYYMMDD)。

    返回 {rows, first, last, tables} 或 {error}。异常不抛给定时器, 记日志返回。
    """
    try:
        if days and days > 0:
            start_time = (date.today() - timedelta(days=int(days))).strftime("%Y%m%d")
        rows = fetch_sentiment_series(start_time=start_time)
        if not rows:
            return {"error": "TQ 无返回(客户端未开 / PANWATCH_ENABLE_TQ!=1 / 窗口无数据)"}
        written = _upsert_rows(db, rows)
        return {
            "rows": written,
            "first": rows[0]["trade_date"],
            "last": rows[-1]["trade_date"],
            "tables": len(ALL_TABLES),
        }
    except Exception as e:  # noqa: BLE001 — 定时任务不因单源失败炸掉
        logger.warning("sync_sentiment_series 失败: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"error": str(e)[:200]}


def latest_with_baseline(db, *, days: int = DEFAULT_BASELINE_DAYS) -> dict:
    """最新交易日情绪 + 各列近 N 个交易日的均值/极值/分位。

    **逐列独立**算基线: 每列取"该列自己最近的非空值"当 current, 并回带它属于哪一天。
    原因: 部分表比 SC3 晚一天发布(实测 SC1 两融 / SC15 打板资金 的末日是 T-1),
    若按"最新整行"取, 这两列会整个丢掉基线 —— 而它们恰恰是情绪判断的关键列。

    返回:
      asof          最新交易骨架日(SC3 有值的那天)
      latest        该日整行(某些列可能为 None, 是真实缺失)
      sample_days   实际参与计算的历史条数
      baseline      {列: {current, current_date, mean, min, max, pct_rank}}
                    样本不足 5 天时 baseline 为空 dict —— 不假装有基线。
    """
    from sqlalchemy import text

    # 多取一些行: 要保证每列都能凑够 days 个非空历史值(有列发布更稀疏)
    fetch_n = int(days) * 3 + 10
    rows = db.execute(
        text(
            "SELECT * FROM market_sentiment_daily WHERE market = 'CN' "
            "ORDER BY trade_date DESC LIMIT :n"
        ),
        {"n": fetch_n},
    ).mappings().all()
    if not rows:
        return {"asof": None, "latest": None, "baseline": {}, "sample_days": 0}

    ordered = [dict(r) for r in rows]
    latest = dict(ordered[0])
    latest.pop("id", None)
    asof = latest.get("trade_date")

    numeric = [c for c in _UPSERT_COLUMNS if c != "extra"]
    baseline: dict = {}
    used = 0
    for c in numeric:
        # 降序遍历, 首个非空 = 当前值; 其后 days 个非空 = 历史窗口
        seq = [(r.get("trade_date"), r.get(c)) for r in ordered if r.get(c) is not None]
        if not seq:
            continue
        cur_date, cur = seq[0]
        hist = [v for _, v in seq[1 : int(days) + 1]]
        if len(hist) < 5:
            continue
        srt = sorted(hist)
        below = sum(1 for v in hist if v < cur)
        baseline[c] = {
            "current": cur,
            "current_date": cur_date,
            "mean": round(sum(hist) / len(hist), 2),
            "min": srt[0],
            "max": srt[-1],
            "pct_rank": round(below / len(hist) * 100, 1),
        }
        used = max(used, len(hist))
    return {
        "asof": asof,
        "latest": latest,
        "baseline": baseline,
        "sample_days": used,
    }

