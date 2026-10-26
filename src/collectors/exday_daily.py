"""TQ `get_exday_data` 四档资金结构 全市场日采集器（2026-10-23）。

## 为什么必须落库

`get_exday_data` **只返当日 1 行**：count=1/5/30/250、start_time/end_time、date=
实测全部只给今天。所以四档结构的历史基线（分位、z、IC）**无法回填** ——
唯一的办法就是每天收盘后落一次库，让 `exday_daily` 自己长成历史。

## 成本

单票实测 **24~26ms**（三个标的、真网关）：

- 全市场 5,580 只 ≈ **150s**（分片+限速后 ~3 分钟）→ 只能进采集器，绝不进请求路径；
- 自选/持仓 50 只 ≈ 1.5s → 允许在工具调用时**写穿**（`write_through`）落一条。

## 纪律

- 只用交易日历判交易日（`is_trading_day`），**不用 `weekday()<5`**（节假日会把空数据写进库）；
- 非交易日取到空 → 整批 `{"ok": False, "reason": ...}`，不写半场数据；
- 单票失败只记 `failed`，不中断整批；全员失败时 `ok=False`（作业框架判 failed）；
- 幂等：`(trade_date, symbol, market)` 唯一，重跑覆盖不新增。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: 每片写库的标的数（与其它采集器一致，避免一次 flush 太大）
CHUNK = 200
#: 单票失败重试次数（TQ 单线程偶发 reset）
RETRY = 2
#: 片间最小间隔（秒）：17709 是单线程，给客户端喘息
PACE_S = 0.0


def _today() -> str:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def is_market_day(d: date | None = None) -> bool:
    """交易日判定统一走 trading_calendar（禁用 weekday 近似）。"""
    try:
        from src.core.trading_calendar import is_trading_day

        return bool(is_trading_day(d or date.today()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("exday 采集: 交易日判定异常，按非交易日处理: %r", exc)
        return False


def targets(db: Session, limit: int | None = None) -> list[str]:
    """全市场股票池：PG 日线覆盖的 CN 代码（与因子快照同一口径），取不到退回 TQ 列表。"""
    syms: list[str] = []
    try:
        from sqlalchemy import text

        rows = db.execute(
            text(
                "SELECT DISTINCT symbol FROM klines WHERE period='1d' AND market='CN' "
                "AND ts >= now() - interval '90 days'"
            )
        ).fetchall()
        syms = [str(r[0]).strip() for r in rows if r[0]]
    except Exception as exc:  # noqa: BLE001
        logger.debug("exday 采集: PG 股票池失败(%s)，退回 TQ", exc)
    if not syms:
        try:
            from marketdata.vendors.tq import tq_rpc

            rows = tq_rpc("get_stock_list", {"market": "5", "list_type": 1}, timeout=20) or []
            for r in rows:
                code = str((r.get("Code") if isinstance(r, dict) else r) or "").strip()
                if code and "." in code:
                    code = code.split(".")[0]
                if code.isdigit() and len(code) == 6:
                    syms.append(code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("exday 采集: TQ 股票列表也失败: %s", exc)
            return []
    syms = sorted(set(syms))
    return syms[:limit] if limit else syms


def fetch_one(symbol: str) -> dict[str, Any] | None:
    """单票取数 → 扁平行（含计算失败原因）。返回 None 表示应跳过。"""
    from src.core import exday_structure as ex

    last_err: Exception | None = None
    for attempt in range(RETRY):
        try:
            m = ex.fetch(symbol)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.3 * (attempt + 1))
    else:
        logger.debug("exday %s 取数失败: %s", symbol, last_err)
        return None
    if not m.get("available"):
        return None
    day = m.get("trade_date") or _today()
    return ex.to_row(m, symbol=str(symbol).split(".")[0], trade_date=day)


def upsert_rows(db: Session, rows: list[dict[str, Any]]) -> int:
    """按 (trade_date, symbol, market) 幂等写库；返回写入条数。"""
    if not rows:
        return 0
    from src.db.models import ExdayDaily

    days = {r["trade_date"] for r in rows}
    existing: dict[tuple[str, str, str], Any] = {}
    try:
        q = db.query(ExdayDaily).filter(ExdayDaily.trade_date.in_(tuple(days)))
        for obj in q.all():
            existing[(obj.trade_date, obj.symbol, obj.market)] = obj
    except Exception as exc:  # noqa: BLE001 - 表未建(迁移没跑)时整批跳过而不是炸
        logger.warning("exday_daily 读取失败(迁移未执行?): %s", exc)
        return 0

    written = 0
    for r in rows:
        key = (r["trade_date"], r["symbol"], r.get("market") or "CN")
        obj = existing.get(key)
        try:
            if obj is None:
                db.add(ExdayDaily(**r))
                existing[key] = r
            else:
                for k, v in r.items():
                    setattr(obj, k, v)
            written += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不毁整批
            logger.debug("exday 写入失败 %s: %s", r.get("symbol"), exc)
    db.commit()
    return written


def write_through(db: Session | None, symbol: str) -> dict[str, Any] | None:
    """工具路径写穿：取一次数 → 落一条 → 返回计算结果（db 为 None 时只算不落）。"""
    from src.core import exday_structure as ex

    if db is None:
        return None
    try:
        m = ex.fetch(symbol)
        if not m.get("available"):
            return m
        row = ex.to_row(m, symbol=str(symbol).split(".")[0], trade_date=m.get("trade_date") or _today())
        upsert_rows(db, [row])
        return m
    except Exception as exc:  # noqa: BLE001 - 写穿失败不影响工具返回
        logger.debug("exday 写穿失败 %s: %s", symbol, exc)
        return None


def collect_once(
    db: Session,
    *,
    symbols: list[str] | None = None,
    limit: int | None = None,
    trade_date: str | None = None,
) -> dict[str, Any]:
    """跑一轮采集。返回 `{ok, written, failed, scanned, started_at, finished_at}`。"""
    day = (trade_date or _today()).replace("-", "")[:8]
    if not symbols:
        symbols = targets(db, limit)
    if not symbols:
        return {"ok": False, "written": 0, "failed": 0, "scanned": 0,
                "reason": "股票池为空（PG 日线无 CN 数据且 TQ 列表不可用）"}

    started = datetime.now().isoformat(timespec="seconds")
    t0 = time.time()
    written = failed = 0
    buf: list[dict[str, Any]] = []
    for i, sym in enumerate(symbols, 1):
        row = fetch_one(sym)
        if row is None:
            failed += 1
        else:
            row["trade_date"] = day  # 以批次日为准，防止跨日跑批写到两个日期
            buf.append(row)
        if len(buf) >= CHUNK or i == len(symbols):
            written += upsert_rows(db, buf)
            buf = []
            if PACE_S:
                time.sleep(PACE_S)
        if i % 500 == 0:
            logger.info("exday 采集进度 %s/%s 写入=%s 跳过=%s 用时=%.0fs",
                        i, len(symbols), written, failed, time.time() - t0)

    elapsed = round(time.time() - t0, 1)
    ok = written > 0 and failed < len(symbols)
    if not ok:
        logger.error("exday 采集异常: 写入=%s 跳过=%s 共 %s 只（TQ 全断?）", written, failed, len(symbols))
    return {
        "ok": ok,
        "written": written,
        "failed": failed,
        "scanned": len(symbols),
        "trade_date": day,
        "elapsed_s": elapsed,
        "started_at": started,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }


def run_scheduled() -> dict[str, Any]:
    """调度入口（交易日 15:45）。自带 session，异常不抛给调度器。"""
    if not is_market_day():
        return {"ok": True, "skipped": True, "reason": "非交易日"}
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        # 顺序执行前先确认 TQ 可用：不可用就别扫 5600 只白等 3 分钟
        try:
            from marketdata.vendors.tq import tq_rpc

            probe = tq_rpc("get_exday_data", {"stock_code": "000001.SZ", "count": 1}, timeout=8)
            if not probe:
                return {"ok": False, "reason": "TQ 网关不可用（exday 探针空）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"TQ 网关不可用: {type(exc).__name__}"}
        res = collect_once(db)
        logger.info("exday 采集完成: %s", res)
        return res
    except Exception as exc:  # noqa: BLE001
        logger.error("exday 采集失败: %s", exc)
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        db.close()
