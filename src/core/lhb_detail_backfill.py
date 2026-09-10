"""龙虎榜机构/营业部明细回填(东财, 全自动) — 2026-09-10 老板"切换到东财全自动方案"。

数据源(东财 datacenter, 实测 2026-09-10 可达; 与 lhb_backfill.py 的"榜单"互补):
- `RPT_ORGANIZATION_TRADE_DETAILS` 机构买卖统计 → `lhb_institution_daily`
  (机构买/卖次数与金额、成交额占比、换手、流通市值 + 上榜后 1/2/3/5/10 日涨幅)
- `RPT_BILLBOARD_DAILYDETAILSBUY` / `...SELL` 营业部买卖明细 → `lhb_seat_details`
  (营业部名/席位买卖额/净额、3 日上涨概率、占买卖总额比)

与通达信页面缓存路径(v0.5.54, 手动)的关系: 东财为**全自动定时入库**(cron 17:50),
通达信侧保留"席位胜率"等其独有字段; 两者按 (trade_date, symbol) 可交叉对账。

幂等: 机构表 `(trade_date, symbol, reason)` 唯一; 席位表 `row_uid`(内容哈希)主键 ——
同一营业部同日同股可在不同上榜原因下多行, 粗去重会丢行。
失败: 单日/单报告失败只记账不抛(cron 入口永不抛异常), 重拉幂等补齐。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE_INST = "lhb_institution_daily"
_TABLE_SEAT = "lhb_seat_details"
_PAGE_SLEEP = 0.3  # 东财礼貌间隔

_INST_COLS = (
    "trade_date", "symbol", "name", "reason", "close", "change_pct", "buy_times",
    "sell_times", "buy_amt", "sell_amt", "net_amt", "accum_amount", "ratio",
    "turnover_pct", "free_cap", "d1_pct", "d2_pct", "d3_pct", "d5_pct", "d10_pct",
    "source", "created_at",
)
_SEAT_COLS = (
    "row_uid", "trade_date", "side", "symbol", "name", "reason",
    "operate_dept_code", "operate_dept_name", "close", "change_pct",
    "accum_amount", "accum_volume", "buy_amt", "sell_amt", "net_amt",
    "rise_probability_3d", "change_type", "trade_id", "total_buy_ratio",
    "total_sell_ratio", "source", "created_at",
)


def _engine():
    from src.db.session import engine

    return engine


def _norm_date(v: object) -> str:
    return str(v or "")[:10].replace("-", "")


def _to_float(v: object) -> float | None:
    try:
        if v is None or str(v).strip() in ("", "-", "None", "null"):
            return None
        return float(str(v).strip())
    except Exception:  # noqa: BLE001
        return None


def _to_int(v: object) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _now() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def _norm_institutions(rows: list[dict]) -> list[dict]:
    """东财机构买卖统计 → 落库行(纯函数): 非 6 位代码丢弃, 日期/原因归一。"""
    now = _now()
    out: list[dict] = []
    for r in rows or []:
        sym = str((r or {}).get("SECURITY_CODE") or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        out.append(
            {
                "trade_date": _norm_date(r.get("TRADE_DATE")),
                "symbol": sym,
                "name": str(r.get("SECURITY_NAME_ABBR") or ""),
                "reason": str(r.get("EXPLANATION") or ""),
                "close": _to_float(r.get("CLOSE_PRICE")),
                "change_pct": _to_float(r.get("CHANGE_RATE")),
                "buy_times": _to_int(r.get("BUY_TIMES")),
                "sell_times": _to_int(r.get("SELL_TIMES")),
                "buy_amt": _to_float(r.get("BUY_AMT")),
                "sell_amt": _to_float(r.get("SELL_AMT")),
                "net_amt": _to_float(r.get("NET_BUY_AMT")),
                "accum_amount": _to_float(r.get("ACCUM_AMOUNT")),
                "ratio": _to_float(r.get("RATIO")),
                "turnover_pct": _to_float(r.get("TURNOVERRATE")),
                "free_cap": _to_float(r.get("FREECAP")),
                "d1_pct": _to_float(r.get("D1_CLOSE_ADJCHRATE")),
                "d2_pct": _to_float(r.get("D2_CLOSE_ADJCHRATE")),
                "d3_pct": _to_float(r.get("D3_CLOSE_ADJCHRATE")),
                "d5_pct": _to_float(r.get("D5_CLOSE_ADJCHRATE")),
                "d10_pct": _to_float(r.get("D10_CLOSE_ADJCHRATE")),
                "source": "eastmoney",
                "created_at": now,
            }
        )
    return out


def _seat_uid(row: dict) -> str:
    """内容哈希(幂等键): 含上榜原因与买卖额 —— 同部同日同股不同原因/金额各自成行。"""
    key = "|".join(
        str(row.get(k) or "")
        for k in (
            "trade_date", "side", "symbol", "reason", "operate_dept_code",
            "operate_dept_name", "buy_amt", "sell_amt", "trade_id",
        )
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _norm_seats(rows: list[dict], side: str) -> list[dict]:
    """东财营业部明细(单侧) → 落库行(纯函数)。"""
    now = _now()
    out: list[dict] = []
    for r in rows or []:
        sym = str((r or {}).get("SECURITY_CODE") or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        row = {
            "trade_date": _norm_date(r.get("TRADE_DATE")),
            "side": side,
            "symbol": sym,
            "name": "",  # 席位报告不含名称(与榜单 join 时可补)
            "reason": str(r.get("EXPLANATION") or ""),
            "operate_dept_code": str(r.get("OPERATEDEPT_CODE") or ""),
            "operate_dept_name": str(r.get("OPERATEDEPT_NAME") or ""),
            "close": _to_float(r.get("CLOSE_PRICE")),
            "change_pct": _to_float(r.get("CHANGE_RATE")),
            "accum_amount": _to_float(r.get("ACCUM_AMOUNT")),
            "accum_volume": _to_float(r.get("ACCUM_VOLUME")),
            "buy_amt": _to_float(r.get("BUY")),
            "sell_amt": _to_float(r.get("SELL")),
            "net_amt": _to_float(r.get("NET")),
            "rise_probability_3d": _to_float(r.get("RISE_PROBABILITY_3DAY")),
            "change_type": str(r.get("CHANGE_TYPE") or ""),
            "trade_id": str(r.get("TRADE_ID") or ""),
            "total_buy_ratio": _to_float(r.get("TOTAL_BUYRIO")),
            "total_sell_ratio": _to_float(r.get("TOTAL_SELLRIO")),
            "source": "eastmoney",
            "created_at": now,
        }
        row["row_uid"] = _seat_uid(row)
        out.append(row)
    return out


# ── 取数(单缝, 测试打桩点) ────────────────────────────────────────────────

def _fetch_institutions(date: str) -> list[dict]:
    from marketdata.vendors.market_flow import fetch_lhb_institutions

    return fetch_lhb_institutions(date)


def _fetch_seats(date: str, side: str) -> list[dict]:
    from marketdata.vendors.market_flow import fetch_lhb_seat_details

    return fetch_lhb_seat_details(date, side)


# ── 落库 ──────────────────────────────────────────────────────────────────

def _upsert(table: str, cols: tuple[str, ...], rows: list[dict]) -> int:
    """幂等 upsert: 冲突键取 (trade_date,symbol,reason) 或 row_uid(席位表)。"""
    if not rows:
        return 0
    conflict = "row_uid" if table == _TABLE_SEAT else "trade_date, symbol, reason"
    placeholders = ", ".join(f":{c}" for c in cols)
    collist = ", ".join(cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in conflict.split(", "))
    sql = (
        f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
    )
    with _engine().begin() as conn:
        conn.execute(text(sql), rows)
    return len(rows)


def sync_dates(dates: list[str]) -> dict:
    """逐日拉取机构+买卖席位明细并幂等落库(单报告失败只记账)。"""
    inst_rows = seat_rows = 0
    errors = 0
    for d in dates:
        day = _norm_date(d)
        if len(day) != 8 or not day.isdigit():
            continue
        try:
            inst_rows += _upsert(_TABLE_INST, _INST_COLS, _norm_institutions(_fetch_institutions(day)))
        except Exception as e:  # noqa: BLE001
            errors += 1
            logger.warning("机构明细拉取/落库失败 %s: %s", day, e)
        for side in ("buy", "sell"):
            try:
                seat_rows += _upsert(_TABLE_SEAT, _SEAT_COLS, _norm_seats(_fetch_seats(day, side), side))
            except Exception as e:  # noqa: BLE001
                errors += 1
                logger.warning("席位明细拉取/落库失败 %s/%s: %s", day, side, e)
    return {"institutions": inst_rows, "seats": seat_rows, "errors": errors}


def _trade_dates(days: int) -> list[str]:
    """近 N 自然日的日期串(东财对非交易日返回空, 天然容错; 与 lhb_backfill 同口径)。"""
    today = datetime.now(_CST).date()
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(max(1, int(days)))]


def backfill_history(days: int = 180) -> dict:
    """一次性历史回填(默认 180 天)。"""
    out = sync_dates(_trade_dates(days))
    logger.info("龙虎榜明细回填(days=%s): %s", days, out)
    return out


def daily_job(days: int = 3) -> dict:
    """cron 入口(交易日 17:50, 与榜单 17:45 错峰): 近 N 日增量, 永不抛异常。"""
    try:
        out = sync_dates(_trade_dates(days))
        out["ok"] = out.get("errors", 0) == 0
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("龙虎榜明细每日任务异常: %s", e)
        return {"ok": False, "errors": 1, "institutions": 0, "seats": 0}
