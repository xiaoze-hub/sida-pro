"""三指标共振判定 + 全市场扫描(2026-09-11, 老板"都做")。

三指标(全部用**我们自己的实现**, 非客户端复刻公式 —— 规避双源分叉):
  趋势 = GS策略(gs_strategy.trend_label(eval_gs(bars)))  → G区/G信号 视为趋势对
  强度 = AI机构活跃度(ai_activity.eval_activity)          → >= 强势线 3.0 视为强度对
  资金 = 主力净流入(通达信 SUPAMO 批量, 万元→元)          → > 0 视为资金对

共振 = 三项全对; 接近共振 = 恰好两项(供梯度观察)。
口径注: ACT>=3 只作**共振条件**使用, 不改 GS 自身信号语义(校准: 独立门控会把
27~37 个信号砍到 1~16, 见 scripts/gs_calibration.py)。

数据: 通达信客户端批量(日线 get_market_data / 资金 SUPAMO 公式), 全市场约 1~2 分钟;
落库(迁移 v161)供页面/回看; 单票失败跳过不编造。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE = "resonance_scan"
_STRONG_LINE = 3.0  # 与 ai_activity.STRONG_LINE 同源(强势线)
_BARS = 90          # 扫描用日线根数(BB0 需 28, 活跃度需 2; 90 富余)
_KLINE_CHUNK = 400  # 单次批量日线的代码数
_COLS = (
    "trade_date", "symbol", "name", "trend", "activity", "level",
    "fund_net", "hits", "resonance", "near", "close", "change_pct", "source", "created_at",
)


def _engine():
    from src.db.session import engine

    return engine


def _now() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def _tdx_code(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if "." in s:
        return s
    # 92 前缀优先判 BJ(920xxx 会先命中 "9"→SH, 同 to_tq_code 的历史坑)
    if s.startswith(("92", "4", "8")):
        return f"{s}.BJ"
    if s.startswith(("6", "9", "5")):
        return f"{s}.SH"
    return f"{s}.SZ"


def _num(v) -> float | None:
    if isinstance(v, (list, tuple)):
        v = v[-1] if v else None
    try:
        s = str(v).strip()
        return float(s) if s not in ("", "None", "null", "nan") else None
    except Exception:  # noqa: BLE001
        return None


# ── 共享判定(唯一真相源) ───────────────────────────────────────────────────
# stock_pool(按需 50 只) 与 resonance_scan(全市场) 必须同一套判定 —— 曾各自实现,
# 现统一到这里; 阈值改动只改这一处。
LEVEL_STRONG = "强"
LEVEL_WEAK = "弱"
LEVEL_NONE = "无"


def resonance_level(*, trend_zone_g: bool, activity: float | None, fund_net: float | None) -> tuple[str, int]:
    """共振级别(强/弱/无) + 得分(0-2): 趋势G区为门槛, 强度/资金各 1 分。

    与 stock_pool 口径一致: 强度=活跃度>=强势线3(或 level 大牛/强势), 资金=净流入>0;
    缺数据计 0 分(不猜)。
    """
    if not trend_zone_g:
        return LEVEL_NONE, 0
    score = 0
    if isinstance(activity, (int, float)) and activity >= _STRONG_LINE:
        score += 1
    if isinstance(fund_net, (int, float)) and fund_net > 0:
        score += 1
    if score >= 2:
        return LEVEL_STRONG, 2
    if score == 1:
        return LEVEL_WEAK, 1
    return LEVEL_NONE, 0


def evaluate_one(bars: list[dict], fund_net: float | None) -> dict:
    """单票共振判定(纯函数)。

    Returns {trend, activity, level, fund_net, hits:[趋势,强度,资金], resonance, near}
    数据不足/缺失 → 对应项 False 并在 note 标注(不猜)。
    """
    from src.core.ai_activity import eval_activity
    from src.core.gs_strategy import eval_gs, trend_label

    trend = trend_label(eval_gs(bars)) if bars else "无数据"
    act = eval_activity(bars) if bars else {}
    activity = act.get("activity") if isinstance(act, dict) else None
    level = act.get("level") if isinstance(act, dict) else None

    hit_trend = trend in ("G信号", "G区间")
    hit_strength = activity is not None and activity >= _STRONG_LINE
    hit_fund = fund_net is not None and fund_net > 0
    hits = [hit_trend, hit_strength, hit_fund]
    n = sum(1 for h in hits if h)
    lvl, _score = resonance_level(trend_zone_g=hit_trend, activity=activity, fund_net=fund_net)
    return {
        "trend": trend,
        "activity": activity,
        "level": level,
        "fund_net": fund_net,
        "hits": hits,
        "resonance": n == 3,
        "near": n == 2,
        "level3": lvl,
    }


def _stock_pool() -> list[tuple[str, str]]:
    """全 A 股池 [(TDX代码, 名称)](通达信本地, 无网络)."""
    from marketdata.vendors.tq import tq_rpc

    raw = tq_rpc("get_stock_list", {"market": "5", "list_type": 1}) or []
    out: list[tuple[str, str]] = []
    for x in raw:
        if not isinstance(x, dict):
            continue
        code = str(x.get("Code") or "").strip().upper()
        if code.endswith((".SH", ".SZ", ".BJ")):
            out.append((code, str(x.get("Name") or "")))
    return out


def _fetch_daily(codes: list[str]) -> dict[str, list[dict]]:
    """通达信批量日线(前复权) → {code: bars}。分片, 单片失败跳过。"""
    from marketdata.vendors.tq import tq_rpc

    out: dict[str, list[dict]] = {}
    for i in range(0, len(codes), _KLINE_CHUNK):
        part = codes[i : i + _KLINE_CHUNK]
        try:
            v = tq_rpc(
                "get_market_data",
                {"stock_list": part, "period": "1d", "count": _BARS, "dividend_type": "front"},
                timeout=120,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("共振扫描: 日线批量失败(%d 码): %s", len(part), e)
            continue
        for code, rows in (v or {}).items():
            if not isinstance(rows, dict):
                continue
            ds = rows.get("Date") or []
            op = rows.get("Open") or []
            cl = rows.get("Close") or []
            hi = rows.get("High") or []
            lo = rows.get("Low") or []
            vo = rows.get("Volume") or []
            n = min(len(ds), len(cl), len(op), len(hi), len(lo))
            bars = []
            for j in range(n):
                try:
                    bars.append(
                        {
                            "date": str(ds[j]),
                            "open": float(op[j]),
                            "close": float(cl[j]),
                            "high": float(hi[j]),
                            "low": float(lo[j]),
                            "volume": float(vo[j]) if j < len(vo) else 0.0,
                        }
                    )
                except Exception:  # noqa: BLE001
                    continue
            if bars:
                out[code] = bars
    return out


def _fetch_funds(codes: list[str]) -> dict[str, float | None]:
    """通达信 SUPAMO 批量主力资金(万元→元)。分片, 失败跳过。"""
    from marketdata.vendors.tq import tq_rpc

    out: dict[str, float | None] = {}
    for i in range(0, len(codes), 500):
        part = codes[i : i + 500]
        try:
            v = tq_rpc(
                "formula_process_mul_zb",
                {
                    "formula_name": "SUPAMO",
                    "formula_arg": "",
                    "stock_list": part,
                    "stock_period": "1d",
                    "periodstr": "1d",
                    "count": -1,
                    "return_count": 1,
                    "dividend_type": 0,
                    "xsflag": -1,
                },
                timeout=120,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("共振扫描: 资金批量失败(%d 码): %s", len(part), e)
            continue
        for code, rec in (v or {}).items():
            val = _num((rec or {}).get("主力资金")) if isinstance(rec, dict) else None
            out[code] = val * 1e4 if val is not None else None
    return out


def scan(limit: int | None = None, *, trade_date: str | None = None) -> dict:
    """全市场扫描并落库。返回汇总(永不抛异常由调用方 daily_job 兜)。"""
    from src.core import tdx_boards

    try:
        pairs = _stock_pool()
    except Exception as e:  # noqa: BLE001
        logger.warning("共振扫描: 股票池拉取失败: %s", e)
        return {"ok": False, "error": f"股票池不可用: {e}"}
    names = dict(pairs)
    pool = [c for c, _ in pairs]
    if limit:
        pool = pool[: int(limit)]

    bars_by = _fetch_daily(pool)
    funds = _fetch_funds([c for c in pool if c in bars_by])
    day = trade_date or datetime.now(_CST).strftime("%Y%m%d")
    rows = []
    for code in pool:
        bars = bars_by.get(code)
        if not bars:
            continue
        try:
            res = evaluate_one(bars, funds.get(code))
        except Exception as e:  # noqa: BLE001
            logger.debug("共振扫描: %s 计算失败 %s", code, e)
            continue
        last = bars[-1]
        prev = bars[-2] if len(bars) > 1 else None
        chg = None
        if prev and prev["close"]:
            chg = round((last["close"] / prev["close"] - 1) * 100, 2)
        rows.append(
            {
                "trade_date": day,
                "symbol": code.split(".")[0],
                "name": names.get(code, ""),
                "trend": res["trend"],
                "activity": res["activity"],
                "level": res["level"],
                "fund_net": res["fund_net"],
                "hits": int(sum(1 for h in res["hits"] if h)),
                "resonance": bool(res["resonance"]),
                "near": bool(res["near"]),
                "close": last["close"],
                "change_pct": chg,
                "source": "tdx",
                "created_at": _now(),
            }
        )
    inserted = _upsert(rows)
    n_res = sum(1 for r in rows if r["resonance"])
    n_near = sum(1 for r in rows if r["near"])
    logger.info("共振扫描完成: %s 只入池, 共振 %s / 接近 %s", len(rows), n_res, n_near)
    return {"ok": True, "trade_date": day, "scanned": len(rows), "resonance": n_res, "near": n_near, "inserted": inserted}


def _upsert(rows: list[dict]) -> int:
    if not rows:
        return 0
    placeholders = ", ".join(f":{c}" for c in _COLS)
    collist = ", ".join(_COLS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _COLS if c not in ("trade_date", "symbol"))
    sql = (
        f"INSERT INTO {_TABLE} ({collist}) VALUES ({placeholders}) "
        f"ON CONFLICT(trade_date, symbol) DO UPDATE SET {updates}"
    )
    with _engine().begin() as conn:
        conn.execute(text(sql), rows)
    return len(rows)


def latest(trade_date: str | None = None, *, only: str = "all", limit: int = 200) -> dict:
    """读取最近一次扫描结果(only=all|resonance|near)。"""
    params: dict = {"lim": max(1, min(int(limit), 1000))}
    where: list[str] = []
    if trade_date:
        where.append("trade_date = :d")
        params["d"] = trade_date.replace("-", "")
    if only == "resonance":
        where.append("resonance = TRUE")
    elif only == "near":
        where.append("near = TRUE")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with _engine().begin() as conn:
        if not trade_date:
            row = conn.execute(text(f"SELECT MAX(trade_date) FROM {_TABLE}")).scalar()
            params["d"] = row
            where_sql = " WHERE trade_date = :d" + (" AND resonance = TRUE" if only == "resonance" else " AND near = TRUE" if only == "near" else "")
        rows = conn.execute(
            text(
                f"SELECT trade_date, symbol, name, trend, activity, level, fund_net, hits, resonance, near, close, change_pct"
                f" FROM {_TABLE}{where_sql} ORDER BY hits DESC, activity DESC NULLS LAST LIMIT :lim"
            ),
            params,
        ).fetchall()
    items = [dict(r._mapping) for r in rows]
    return {"trade_date": params.get("d"), "count": len(items), "items": items}


def symbol_detail(symbol: str, days: int = 60) -> dict:
    """单票最近 N 次日线口径三指标 + 共振判定(供个股页/副图)。"""
    code = _tdx_code(symbol)
    bars_by = _fetch_daily([code])
    bars = bars_by.get(code) or []
    if not bars:
        return {"symbol": symbol, "available": False, "reason": "无日线数据"}
    funds = _fetch_funds([code])
    res = evaluate_one(bars, funds.get(code))
    return {"symbol": symbol, "available": True, "trade_date": bars[-1]["date"], **res, "bars": len(bars)}


def daily_job() -> dict:
    """cron 入口(交易日 15:40 盘后): 全市场扫描落库; 永不抛异常。"""
    from src.core.quote_snapshots import in_trading_window

    try:
        # 盘后跑: 用"今日是否有K线"判断即可, 不依赖时段; 周末/节假日扫描也幂等(数据不变)
        out = scan()
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("共振扫描每日任务异常: %s", e)
        return {"ok": False, "error": str(e)}


def activity_series(symbol: str, days: int = 120) -> dict:
    """机构活跃度历史序列(供副图): 逐日活跃度 + 三线 + 共振级别。

    从日线口径逐日滚动计算(数据不足的头部为 None, 不补零)。
    """
    from src.core.ai_activity import eval_activity, BULL_LINE, LIFE_LINE, STRONG_LINE
    from src.core.gs_strategy import eval_gs, trend_label

    code = _tdx_code(symbol)
    bars = (_fetch_daily([code]) or {}).get(code) or []
    if len(bars) < 30:
        return {"symbol": symbol, "available": False, "reason": f"日线不足({len(bars)} 根)", "items": []}
    funds = _fetch_funds([code])
    fund_net = (funds or {}).get(code)
    items: list[dict] = []
    start = max(1, len(bars) - int(days))
    for i in range(start, len(bars)):
        window = bars[: i + 1]
        act = eval_activity(window) or {}
        activity = act.get("activity")
        trend = trend_label(eval_gs(window))
        lvl, _ = resonance_level(trend_zone_g=trend in ("G信号", "G区间"), activity=activity, fund_net=fund_net if i == len(bars) - 1 else None)
        items.append(
            {
                "date": str(bars[i].get("date")),
                "close": bars[i].get("close"),
                "activity": activity,
                "level": act.get("level"),
                "above_strong": act.get("above_strong"),
                "trend": trend,
                "resonance_level": lvl,
            }
        )
    return {
        "symbol": symbol,
        "available": True,
        "lines": {"life": LIFE_LINE, "strong": STRONG_LINE, "bull": BULL_LINE},
        "fund_net": fund_net,
        "count": len(items),
        "items": items,
    }
