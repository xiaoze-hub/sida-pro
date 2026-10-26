# -*- coding: utf-8 -*-
"""首封时间×封成比 强度因子 **影子运行** 落库 + 读取(2026-10-11 胜率创新 P0-1)。

影子运行(不干扰生产)
--------------------
每日盘后对**当日涨停股**打强度分层标签(S/A/B/C, 口径见
`src.core.limit_up_strength`), 只写 `limit_strength_shadow` 一张**只读用**新表 ——
**不改任何现有信号/决策/落库**。表建在 `src/web/migrations.py::_m188`。

独立入口(不碰 src/collectors/)
--------------------------------
本模块自成一个 core 模块 + 独立 cron 调度(见 `src/bootstrap/startup.py` 注册),
**不修改** `src/collectors/` 任何采集器(另一支在改 sentiment 同步, 避让)。

数据来源(TQ 独家)
------------------
- 首封时间 / 炸板次数: `get_zdt_data`(FirstTimeZT / OpenTimesZT) —— 分片 ≤50 只/次
  (事故铁律, 见 skill n150-tq-gateway: 单次请求 >50 只会把客户端打进假死态);
- 封单额 / 流通市值: `get_more_info`(limit_up_amount(元) / circulating_market_value) —— 同样分片;
  封成比 = 封单额 ÷ 流通市值(本模块现算, 也可由 more-info 的 limit_up_ratio 直接复用)。

缺数据显式(不编造)
------------------
行内 status='no_data' 且 label 为空; 缺失分量不猜。整批无可用行 → job 显式 ok=False。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE = "limit_strength_shadow"
_CHUNK = 50  # 事故铁律: 单次 TQ 请求 ≤50 只

#: 落库列(除主键 trade_date/symbol 外), 与 _m188 表结构一一对应。
FIELDS: tuple[str, ...] = (
    "name", "first_time", "first_seal_bucket", "seal_amount",
    "circulating_mktcap", "seal_ratio", "seal_ratio_grade", "open_times",
    "strength_label", "status", "source",
)


def _as_num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _trade_date(now: datetime | None = None) -> str:
    return (now or datetime.now(_CST)).strftime("%Y%m%d")


# ── 纯逻辑: 原始记录 → 影子行 ────────────────────────────────────────────────

def build_rows(records: list[dict]) -> list[dict]:
    """原始记录 → 影子行(含强度标签)。纯函数, 无 I/O。

    每条 record: ``{symbol, name?, first_time?, seal_amount?, circulating_mktcap?,
    open_times?, ratio?, source?}``。symbol 为空的行剔除(无法作主键)。
    标签口径全部委托 `limit_up_strength.strength_label`, 缺数据 → status='no_data'。
    """
    from src.core.limit_up_strength import strength_label

    out: list[dict] = []
    for rec in records or []:
        symbol = str(rec.get("symbol") or "").strip()
        if not symbol:
            continue
        res = strength_label(
            first_time=rec.get("first_time"),
            seal_amount=rec.get("seal_amount"),
            circulating_mktcap=rec.get("circulating_mktcap"),
            open_times=rec.get("open_times"),
            ratio=rec.get("ratio"),
        )
        out.append(
            {
                "symbol": symbol,
                "name": rec.get("name") or "",
                "first_time": str(rec.get("first_time") or "") or None,
                "first_seal_bucket": res["first_seal_bucket"],
                "seal_amount": _as_num(rec.get("seal_amount")),
                "circulating_mktcap": _as_num(rec.get("circulating_mktcap")),
                "seal_ratio": res["seal_ratio"],
                "seal_ratio_grade": res["seal_ratio_grade"],
                "open_times": res["open_times"],
                "strength_label": res["label"],
                "status": res["status"],
                "source": rec.get("source") or "tq_zdt",
            }
        )
    return out


# ── 写入(幂等 upsert) ───────────────────────────────────────────────────────

def _upsert(trade_date: str, row: dict, now: datetime | None = None) -> None:
    from sqlalchemy import text

    from src.db.dialect import upsert_sql
    from src.db.session import SessionLocal

    cols = ["trade_date", "symbol", *FIELDS, "captured_at"]
    updates = [*FIELDS, "captured_at"]
    stmt = text(upsert_sql(_TABLE, cols, ["trade_date", "symbol"], updates))
    payload = {"trade_date": trade_date, "symbol": row["symbol"],
               "captured_at": (now or datetime.now(_CST)).replace(tzinfo=None)}
    for f in FIELDS:
        payload[f] = row.get(f)
    db = SessionLocal()
    try:
        db.execute(stmt, payload)
        db.commit()
    finally:
        db.close()


def mark_rows(trade_date: str, rows: list[dict], *, write: bool = True,
              now: datetime | None = None) -> dict:
    """把影子行按 (trade_date, symbol) 幂等落库。返回统计。"""
    written = 0
    for row in rows:
        if write:
            _upsert(trade_date, row, now=now)
        written += 1
    labeled = sum(1 for r in rows if r.get("status") == "ok")
    return {
        "trade_date": trade_date,
        "rows": len(rows),
        "written": written,
        "labeled": labeled,
        "no_data": len(rows) - labeled,
    }


# ── 默认数据源(TQ 独家; 分片 ≤50) ──────────────────────────────────────────

def _default_codes(trade_date: str) -> list[str]:
    """当日涨停股代码: 优先已落库 `limit_up_events`, 回退现有涨停池采集(只读)。"""
    codes: list[str] = []
    try:
        from sqlalchemy import text

        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            rows = db.execute(
                text("SELECT DISTINCT symbol FROM limit_up_events WHERE trade_date = :d"),
                {"d": _ymd(trade_date)},
            ).fetchall()
            codes = [str(r[0]) for r in rows if r[0]]
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("强度影子: 读 limit_up_events 失败: %s", e)
    if codes:
        return codes
    # 回退: 现有涨停池采集器(只读调用, 不改其代码)
    try:
        from src.collectors.market_sentiment_collector import MarketSentimentCollector

        pool = MarketSentimentCollector().get_limit_up_pool(_ymd(trade_date)) or []
        return [str(p.get("code")) for p in pool if p.get("code")]
    except Exception as e:  # noqa: BLE001
        logger.warning("强度影子: 涨停池回退取码失败: %s", e)
        return []


def _default_names(trade_date: str) -> dict[str, str]:
    try:
        from sqlalchemy import text

        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    "SELECT symbol, name FROM limit_up_events WHERE trade_date = :d"
                ),
                {"d": _ymd(trade_date)},
            ).fetchall()
            return {str(r[0]): str(r[1] or "") for r in rows if r[0]}
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return {}


def _default_snapshot(codes: list[str]) -> dict[str, dict]:
    """TQ get_zdt_data(分片 ≤50) → {裸码: {first_time, open_times}}。"""
    import marketdata.vendors.tq as tqmod

    from src.core.limit_pool_zdt import fmt_zdt_time, parse_zdt_snapshot
    from src.core.tq_chips import normalize_code

    out: dict[str, dict] = {}
    tq_codes = [normalize_code(str(c)) for c in codes]
    fails = 0
    for i in range(0, len(tq_codes), _CHUNK):
        part = tq_codes[i : i + _CHUNK]
        try:
            snap = tqmod.zdt_snapshot(part)
        except Exception as e:  # noqa: BLE001
            fails += 1
            logger.warning("强度影子: 快照第 %d 批失败(%s)", i // _CHUNK + 1, e)
            if fails >= 2:
                break
            continue
        for code, rec in parse_zdt_snapshot(snap).items():
            bare = code.split(".", 1)[0]
            out[bare] = {
                "first_time": fmt_zdt_time(rec.get("FirstTimeZT")),
                "open_times": rec.get("OpenTimesZT"),
            }
    return out


def _default_more(codes: list[str]) -> dict[str, dict]:
    """TQ get_more_info(分片 ≤50) → {裸码: {seal_amount(元), circulating_mktcap(元)}}。"""
    from src.core.marketdata_client import md_more_info

    out: dict[str, dict] = {}
    for i in range(0, len(codes), _CHUNK):
        part = [str(c) for c in codes[i : i + _CHUNK]]
        try:
            rows = md_more_info(part, market="CN")
        except Exception as e:  # noqa: BLE001
            logger.warning("强度影子: more-info 第 %d 批失败(%s)", i // _CHUNK + 1, e)
            continue
        for m in rows or []:
            sym = str(m.get("symbol") or "").split(".", 1)[0]
            if not sym:
                continue
            out[sym] = {
                "seal_amount": m.get("limit_up_amount"),
                "circulating_mktcap": m.get("circulating_market_value"),
                "ratio": m.get("limit_up_ratio"),
            }
    return out


def _ymd(trade_date: str | None) -> str:
    """任意日期 → `YYYYMMDD`(limit_up_events / 影子表主键口径)。"""
    return str(trade_date or "").strip().replace("-", "")


# ── 编排 ────────────────────────────────────────────────────────────────────

def collect_and_mark(
    trade_date: str | None = None,
    *,
    codes_provider=None,
    snapshot_provider=None,
    more_provider=None,
    names_provider=None,
    write: bool = True,
    now: datetime | None = None,
) -> dict:
    """当日涨停股 → 强度标签 → 影子落库。provider 可注入(测试禁真网络)。

    返回统计 dict; 无涨停股/取数全失败 → ``ok=False`` 并显式说明。
    """
    td = trade_date or _trade_date(now)
    codes_provider = codes_provider or _default_codes
    snapshot_provider = snapshot_provider or _default_snapshot
    more_provider = more_provider or _default_more
    names_provider = names_provider or _default_names

    codes = codes_provider(td) or []
    if not codes:
        return {"ok": False, "trade_date": td, "reason": "当日无涨停股(或取码失败)", "rows": 0}

    snap = snapshot_provider(codes) or {}
    more = more_provider(codes) or {}
    try:
        names = names_provider(td) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("强度影子: 取名称失败(继续): %s", e)
        names = {}

    records: list[dict] = []
    for c in codes:
        bare = str(c).split(".", 1)[0].strip()
        if not bare:
            continue
        s = snap.get(bare) or {}
        m = more.get(bare) or {}
        records.append(
            {
                "symbol": bare,
                "name": names.get(bare, ""),
                "first_time": s.get("first_time"),
                "open_times": s.get("open_times"),
                "seal_amount": m.get("seal_amount"),
                "circulating_mktcap": m.get("circulating_mktcap"),
                "ratio": m.get("ratio"),
                "source": "tq_zdt",
            }
        )

    rows = build_rows(records)
    stats = mark_rows(td, rows, write=write, now=now)
    stats["ok"] = stats["labeled"] > 0
    if not stats["ok"]:
        stats["reason"] = "标的全为 no_data(首封时间/封成比均缺, 不编造)"
    return stats


def run_strength_shadow_job(now: datetime | None = None, trade_date: str | None = None) -> dict:
    """cron 入口(交易日收盘后): 打标 + 落库, 走作业框架诚实性(ok=False → failed)。"""
    from src.core.jobs import jobs

    job_id, is_new = jobs.create("limit_strength_shadow", "首封时间×封成比 强度影子打标")
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id}
    try:
        out = collect_and_mark(trade_date=trade_date, now=now)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("强度影子打标作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="强度影子打标: ")
    return out


# ── 只读读取(供 API/展示) ──────────────────────────────────────────────────

def _cluster_rows(rows: list[dict]) -> dict[str, int]:
    """{标签或 no_data: 计数} —— 分桶统计用。"""
    from collections import Counter

    c = Counter((r.get("strength_label") or "no_data") for r in rows)
    return dict(c)


def labels_map(trade_dates: list[str]) -> dict[str, dict[str, str]]:
    """一批交易日 → ``{YYYYMMDD: {symbol: strength_label}}``(只含 ok 标签)。

    供只读展示(梯队/池子徽章)一次批量取标签, 避免逐日查。键统一为 `YYYYMMDD`,
    与 `limit_up_events.trade_date` / 梯队 `day["date"]` 口径一致。无数据 → 空 dict。
    """
    from sqlalchemy import text

    from src.db.session import SessionLocal

    raw_dates = sorted({_ymd(d) for d in (trade_dates or []) if _ymd(d)})
    if not raw_dates:
        return {}
    out: dict[str, dict[str, str]] = {}
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                f"SELECT trade_date, symbol, strength_label FROM {_TABLE} "
                f"WHERE trade_date IN ({','.join(':' + str(i) for i in range(len(raw_dates)))})"
            ),
            {str(i): d for i, d in enumerate(raw_dates)},
        ).fetchall()
    except Exception as e:  # noqa: BLE001 表未迁移 → 空
        logger.warning("limit_strength_shadow 批量查询失败(视为无标签): %s", e)
        return {}
    finally:
        db.close()
    for td, sym, label in rows:
        if not label:
            continue
        out.setdefault(_ymd(td), {})[str(sym)] = str(label)
    return out


def latest_date() -> str | None:
    """影子表最近交易日(YYYYMMDD); 无数据 → None。"""
    from sqlalchemy import text

    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        r = db.execute(text(f"SELECT MAX(trade_date) AS d FROM {_TABLE}")).first()
        return str(r[0]) if r and r[0] else None
    except Exception as e:  # noqa: BLE001 表未迁移 → 当作无数据
        logger.warning("limit_strength_shadow 查询失败(视为无数据): %s", e)
        return None
    finally:
        db.close()


def labels_for_date(trade_date: str | None = None) -> dict:
    """某日强度标签。

    返回 ``{available, trade_date, labels: {symbol: {...}}, counts: {...}, note}``。
    trade_date 缺省取最近一日。无数据 → available=False + note(绝不编造)。
    """
    td = trade_date or latest_date()
    if not td:
        return {"available": False, "trade_date": None, "labels": {}, "counts": {},
                "note": "尚无影子打标数据(可能未到首个盘后 cron, 或表未迁移)"}
    # 归一: 入参可能 ISO, 表里存 YYYYMMDD
    td = _ymd(td)
    from sqlalchemy import text

    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                f"SELECT symbol, name, first_time, first_seal_bucket, seal_amount, "
                f"circulating_mktcap, seal_ratio, seal_ratio_grade, open_times, "
                f"strength_label, status FROM {_TABLE} WHERE trade_date = :d"
            ),
            {"d": td},
        ).mappings().all()
    except Exception as e:  # noqa: BLE001
        logger.warning("limit_strength_shadow 查询失败: %s", e)
        return {"available": False, "trade_date": td, "labels": {}, "counts": {},
                "note": f"影子表查询失败: {e}"}
    finally:
        db.close()

    items = [dict(r) for r in rows]
    labels = {str(r["symbol"]): r for r in items}
    return {
        "available": bool(items),
        "trade_date": td,
        "labels": labels,
        "counts": _cluster_rows(items),
        "note": None if items else f"{td} 无影子打标行",
    }
