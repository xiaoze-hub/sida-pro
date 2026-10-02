"""TQ 条件选股公式 —— **按需通用批量执行引擎**(2026-10-02)。

用途
----
给定 **公式集 × 全市场(或指定标的池)**, 按需一次性批量执行条件选股, 返回每个公式的
命中标的清单。与定时采集互补:

  · 定时采集(`src/collectors/tq_formula_signals.py` + `tq_formula_signal_scheduler`)
    只用**少量固定公式**每交易日扫一遍并落 `tq_formula_signal_daily`, 出「当日触发家数 +
    近 N 日基线」;
  · 本引擎 = **按需**(接口/用户给定公式集与池)扫一遍, 直接返回命中清单, **不落库、不写表**。

两者数据源同一个通达信客户端 TQ 网关方法 `formula_process_mul_xg`(条件选股):
免费、无限频、零外部依赖 —— **不依赖问财限频**, 也没有东财/同花顺免费层「某技术条件当日
多少只触发」的口径。

硬约束(与定时采集**同口径**, 不得放宽)
--------------------------------
1. **分片**: 单次请求标的数受 TQ 客户端上限约束(把全市场单发会把客户端压成假死),
   按 `TQ_SCAN_CHUNK`(实测安全值 500)分片, 小池可用 `chunk` 覆盖。
2. **完整性**: 任一分片失败**绝不**当作「0 命中」——该公式置 `complete=False`, 并把
   **缺哪些片**(索引/起始下标/片内标的数/失败原因)记进 `missing_chunks`, 消费方必须显式标注。
3. **日期归一紧凑格式** `YYYYMMDD`: 传 `2026-09-24`(ISO) 会被网关判为「该日无数据行」而
   **静默返回 0 命中**, 看起来像「今天没信号」——必须在校验前归一。
4. **非交易日不扫**: 请求日不在 A 股交易日历(含法定节假日/调休)内 → 直接跳过并**显式提示**,
   不打网关(与 `tq_formula_signal_scheduler._is_market_day` 同一红线)。

不写路径: 本模块只读(调 TQ 取数), **不落库、不改定时采集口径、不接写路径**。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from marketdata.vendors import tq as tqmod

logger = logging.getLogger(__name__)

#: 扫描时间窗(自然日)。与 `marketdata.vendors.tq._SCAN_LOOKBACK_DAYS` 同值:
#: 必须给**窄窗口**, 不带日期时网关按全部历史算, 每片响应过重会从 24s 拖到分钟级。
BATCH_LOOKBACK_DAYS = 20

#: 单次批量执行的公式数上限(防误触发全市场 × N 公式的长扫描把客户端压住)。
#: 定时采集固定 24 个公式是**盘后**跑; 按需接口面向交互, 上限收紧。
MAX_BATCH_FORMULAS = 20

#: 「公式不存在」类错误的文案指纹。命中则不把整轮退化成「全部片失败」, 而是给出显式错误态。
_FORMULA_MISSING_MARKERS = (
    "公式不存在", "获取公式失败", "formula not found", "no such formula", "formula_not_exist",
)


def _norm_day(d: str) -> str:
    """把交易日期归一成客户端要求的**紧凑格式** `YYYYMMDD`(同定时采集口径)。

    `2026-09-24` / `2026/09/24` / `2026.09.24` → `20260924`; 空串原样返回
    (调用方再决定默认值)。不在这里替调用方猜一个日期。
    """
    return (d or "").strip().replace("-", "").replace("/", "").replace(".", "")


def _normalize_formulas(raw) -> list[dict]:
    """公式集归一成 `[{code, name, arg}]`(去重、保序)。

    接受三种写法, 便于 API/工具层直接传:
      · `"MACD买入"`                          → code=name=MACD买入, arg=""
      · `{"code": "UPN", "name": "连涨3天", "arg": "3"}`  (也认 `formula`/`formula_arg` 键)
      · `("UPN", "连涨3天", "3")`             → 元组 (code, name, arg)
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw or []:
        if isinstance(item, str):
            code, name, arg = item.strip(), item.strip(), ""
        elif isinstance(item, dict):
            code = str(item.get("code") or item.get("formula") or "").strip()
            name = str(item.get("name") or code).strip()
            arg = str(item.get("arg") or item.get("formula_arg") or "").strip()
        elif isinstance(item, (list, tuple)):
            code = str(item[0] if item else "").strip()
            name = str(item[1] if len(item) > 1 else code).strip()
            arg = str(item[2] if len(item) > 2 else "").strip()
        else:
            continue
        if not code:
            continue
        key = (code, arg)
        if key in seen:
            continue  # 同一 (公式, 参数) 重复执行是纯浪费(全市场一遍 24s)
        seen.add(key)
        out.append({"code": code, "name": name or code, "arg": arg})
    return out


def _trading_day_state(day: str) -> tuple[bool, str]:
    """`(是否交易日, 备注)`。走全仓统一日历; 年份未覆盖按**非交易日**处理(红线: 禁止推测)。

    与 `tq_formula_signal_scheduler._is_market_day` 同一判据 —— 不用 `weekday()<5`,
    那会把中秋/国庆之类的**周中假日**当交易日。
    """
    if len(day) != 8 or not day.isdigit():
        return False, f"日期格式无法识别: {day!r}(应为 YYYYMMDD)"
    from src.core.trading_calendar import TradingCalendarError, is_trading_day

    iso = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    try:
        return bool(is_trading_day(iso)), ""
    except TradingCalendarError as e:
        return False, f"交易日历未覆盖: {str(e)[:80]}"
    except Exception as e:  # noqa: BLE001 — 判定失败按休市处理, 宁可漏扫
        return False, f"交易日判定失败: {str(e)[:80]}"


def _resolve_pool(codes, market: str, list_type: int) -> tuple[list[str], str]:
    """标的池: 显式给定则用给定的, 否则取全 A(`stock_list`)。返回 (pool, 来源)。"""
    if codes:
        return [str(c).strip() for c in codes if str(c).strip()], "custom"
    try:
        raw = tqmod.stock_list(market, list_type)
    except Exception as e:  # noqa: BLE001 — 取列表失败交给上层报 empty_pool
        logger.warning("公式批量执行: 取全市场列表失败(%s)", e)
        return [], "full_market"
    pool = [str(i.get("Code")) for i in (raw or []) if isinstance(i, dict) and i.get("Code")]
    return pool, "full_market"


def _is_formula_missing_error(msg: str) -> bool:
    """错误文案是否属于「公式不存在」类(网关 ErrorId=9 + `获取公式失败或公式不存在`)。"""
    low = (msg or "").lower()
    return any(m.lower() in low for m in _FORMULA_MISSING_MARKERS)


def _scan_one(formula: str, arg: str, pool: list[str], *, end_day: str,
              start_day: str, chunk: int) -> dict:
    """单个公式 × 全池扫描: 分片调 TQ, 分片失败**显式记账**而非静默当 0 命中。"""
    step = max(1, int(chunk))
    chunks_total = (len(pool) + step - 1) // step
    hits: list[dict] = []
    missing: list[dict] = []
    scanned = 0
    date_rows = 0
    resolved = end_day
    error: str | None = None

    for idx, start in enumerate(range(0, len(pool), step)):
        part = pool[start:start + step]
        try:
            res = tqmod.formula_xg_mul(
                formula, part, formula_arg=arg, start_time=start_day, end_time=end_day,
            )
        except Exception as e:  # noqa: BLE001 — 分片失败记账, 不拖垮其余片
            msg = str(e)[:200]
            if idx == 0 and _is_formula_missing_error(msg):
                # 第一片就报「公式不存在」: 整轮无意义(每片都会同样报错), 直接给显式错误态,
                # 不再空扫其余片(否则看起来是「扫描不完整」而非「公式不存在」)。
                error = "formula_not_found"
                missing.append({"index": 0, "start_index": start, "count": len(part), "error": msg})
                break
            logger.warning("公式批量执行 %s: 第 %d/%d 片失败(%s)", formula, idx + 1, chunks_total, msg)
            missing.append({"index": idx, "start_index": start, "count": len(part), "error": msg})
            continue
        scanned += len(part)
        date_rows += max(0, tqmod._date_rows_seen(res, end_day))
        part_hits, resolved = tqmod._formula_hits(res, end_day)
        hits.extend(part_hits)

    per_signal: dict[str, int] = {}
    for h in hits:
        per_signal[h["signal"]] = per_signal.get(h["signal"], 0) + 1
    return {
        "formula": formula,
        "formula_arg": arg,
        "complete": not missing,
        "error": error,
        "chunks_total": chunks_total,
        "chunks_failed": len(missing),
        "missing_chunks": missing,
        "scanned": scanned,
        "hit_count": len(hits),
        "hits": hits,
        "per_signal": per_signal,
        # ⚠️ 区分两种 0: date_rows==0 ⇒ 该日无数据行(非交易日/窗口没覆盖), hit_count=0
        # **不是**「当日无票触发」; 有行但值全 0 才是真的 0 家。
        "date_rows": date_rows,
        "date_has_data": (date_rows > 0) if end_day else None,
    }


def run_formula_batch(formulas, *, trade_date: str = "", codes=None,
                      chunk: int | None = None, market: str = "5",
                      list_type: int = 1) -> dict:
    """按需批量执行: 公式集 × (全市场 | 指定池) → 每公式命中清单。

    参数
    ----
    formulas   : 公式集, 接受 `_normalize_formulas` 的三种写法(代码串/字典/元组)。
    trade_date : 交易日(可 ISO); 缺省=今天。**非交易日直接跳过并显式提示**。
    codes      : 可选标的池; 缺省取全 A(`stock_list`)。
    chunk      : 分片大小; 缺省 `TQ_SCAN_CHUNK`(500)。

    返回见模块 docstring 与 `_scan_one`; 顶层带 `complete` / `incomplete_formulas` /
    `errored_formulas`, 完整性由**逐公式逐片**汇聚而来。
    """
    specs = _normalize_formulas(formulas)
    if not specs:
        return {"ok": False, "error": "no_formulas", "formulas": [], "complete": False,
                "note": "未给定任何公式"}
    if len(specs) > MAX_BATCH_FORMULAS:
        return {"ok": False, "error": "too_many_formulas", "formulas": [], "complete": False,
                "note": f"公式数 {len(specs)} 超过上限 {MAX_BATCH_FORMULAS}"}

    day = _norm_day(trade_date) or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    trading, cal_note = _trading_day_state(day)
    if not trading:
        return {
            "ok": False,
            "skipped": True,
            "trade_date": day,
            "requested_date": trade_date or day,
            "is_trading_day": False,
            "formulas": [],
            "complete": False,
            "formulas_requested": [s["code"] for s in specs],
            "note": f"{day} 不是 A 股交易日({cal_note or '周末/法定节假日'}), 按硬约束不执行扫描",
        }

    pool, source = _resolve_pool(codes, market, list_type)
    if not pool:
        return {"ok": False, "error": "empty_pool", "trade_date": day, "formulas": [],
                "complete": False,
                "note": "标的池为空(取股票列表失败或未给定 codes)"}

    try:
        start_day = (datetime.strptime(day, "%Y%m%d")
                     - timedelta(days=BATCH_LOOKBACK_DAYS)).strftime("%Y%m%d")
    except ValueError:
        start_day = ""

    step = max(1, int(chunk or tqmod.TQ_SCAN_CHUNK))
    results: list[dict] = []
    for s in specs:
        r = _scan_one(s["code"], s["arg"], pool, end_day=day, start_day=start_day, chunk=step)
        r["formula_name"] = s["name"]
        results.append(r)

    incomplete = [r["formula"] for r in results if not r["complete"]]
    errored = [r["formula"] for r in results if r["error"]]
    return {
        "ok": True,
        "trade_date": day,
        "requested_date": trade_date or "",
        "is_trading_day": True,
        "pool_source": source,
        "pool_size": len(pool),
        "chunk": step,
        "chunks_total": (len(pool) + step - 1) // step,
        "formulas": results,
        "total_hits": sum(r["hit_count"] for r in results),
        # 顶层完整 = 每个公式的每一片都成功; 任一缺片即 False(消费方不得当全市场口径用)。
        "complete": not incomplete,
        "incomplete_formulas": incomplete,
        "errored_formulas": errored,
        "note": (None if not incomplete
                 else "存在分片失败: 命中数不是全市场口径, 见各公式 missing_chunks"),
    }
