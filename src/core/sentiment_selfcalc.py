# -*- coding: utf-8 -*-
"""情绪序列**本地自算**(2026-10-14, 替代已弃用的云 SC 序列)。

## 为什么(用户决策, 2026-10-14)

`market_sentiment_daily` 原由通达信 **云** 专业序列 `get_scjy_value`(SC01-35)填充。
用户决定**不购买云数据 Key**(费钱), SC/GP 云函数永久放弃 —— 生产已断在 2026-09-23。
情绪温度计/regime/胜率分桶都要"当日市场温度", 于是改为**只用客户端本地接口自算**:
`get_zdt_data`(涨跌停快照) + `get_pricevol`(批量价量) + `get_stock_list`(全A码表) +
已落库的 `limit_up_events` 聚合。

## 四个指标(每日盘后)

| 指标 | 口径 | 落库列(与 SC 兼容) |
|---|---|---|
| 涨停家数 | `get_zdt_data` 全市场真涨停候选家数(=SC3 v1 "涨停/曾涨停"口径) | `limit_up_count` |
| 炸板家数 | 候选里 `OpenTimesZT>0` **或** 曾涨停未封住 | `limit_up_open_count` |
| 连板家数 | `limit_up_events` 聚合(收盘封住且连续封板 ≥2 天) | `streak_count` |
| 封板成功/失败资金 | zdt 封单量 × 收盘价, 按封住/未封分侧求和(**近似**) | `seal_success_money` / `seal_fail_money` |

派生 `炸板比 = 炸板家数/涨停家数`、`封板成功率 = 成功资金/(成功+失败)` —— **不自己再写一份**,
一律调用 `src.core.sentiment_thermometer.derive_thermometer`(全仓单一口径)。

## 口径钉子(禁自创第二套)

1. **真涨停候选** = 复用 `src.core.limit_pool_zdt.parse_zdt_snapshot`(ZT 字段存在即候选),
   与涨停池 TQ 备源同一判定 —— 不新写一套。
2. **"封住"** = 收盘价 ≥ 涨停价(昨收) —— **与 `limit_up_events` 生成侧同一口径**
   (`limit_up_backfill._extract_events_from_bars` 的 `sealed = close >= limit_up_price - eps`)。
   这里只判收盘(快照拿不到盘中最高价, 故不判 `touched`), 涨停价一律走
   `src.core.limit_rules.limit_up_price`。
3. **`ZDTStatusNow` 不单独当判据**: 客户端 14 字段文档对该字段只注"当前状态", **枚举未标定**
   (AGENTS: 单位/口径禁猜)。故它只做**交叉核对**(原始值分布进 extra; 与封住判定冲突时计入
   `status_mismatch`), 不参与"封住"结论。
4. **单位**: 涨停/炸板/连板 = 家; 封板资金落库口径与 SC15 一致为 **亿元**
   (封单量 `FDVolMaxZT` 单位=手 → ×100=股 → ×收盘价=元 → /1e8=亿元)。原始元金额+口径
   写进 `sentiment_selfcalc_daily.extra`, 便于复核。
5. **封板资金是近似**(`FDVolMaxZT` 是当日**最大**封单量, 不是收盘瞬间封单): 表里
   `approximation=1` 显式标注, 并在 extra 写明近似的来源。
6. **缺数据显式**: 价格缺失的候选单独计数(`price_missing`), 不计入成功也不计入失败;
   快照连续 2 片失败即**中止**(事故铁律: 单次 TQ 请求 ≤50 只, 连续失败绝不压测式重试)。

## 已知边界(诚实声明)

- `get_zdt_data` 是**当日快照**, **无法回补历史** ⇒ 中断的历史交易日补不回来(显式记录, 不编造);
- `ZDTStatusNow` 枚举、SC3 v2(`limit_up_open_count`)的精确官方语义未标定 —— 本模块口径
  以本仓既有消费方(thermometer)为准并显式登记;
- `is_st` 依赖股票名称(名称缺失时按非 ST 判 —— 与 `limit_up_backfill` 一致)。

## 落库

- `market_sentiment_daily`(**同表兼容列**): 温度计/`/api/market/sentiment`/新鲜度自检零改动即可读到;
  写入时对已有非空列**不覆盖**(`COALESCE`), 故 SC 真值若日后并入仍优先, 且重复跑幂等;
- `sentiment_selfcalc_daily`(迁移 196): 自算明细/诊断/近似标注, 供审计与复核。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

MARKET = "CN"
SOURCE = "tdx_selfcalc"
TABLE = "sentiment_selfcalc_daily"
SENTIMENT_TABLE = "market_sentiment_daily"

JOB_KIND = "sentiment_selfcalc"
JOB_LABEL = "市场情绪序列本地自算"

#: 事故铁律(见 skill n150-tq-gateway 坑10): 单次 TQ 请求标的数必须 ≤50,
#: 超过会把客户端打进假死态(恢复只能整机重启)。**改这里前先读那段**。
_CHUNK = 50
#: 连续失败片数达到此值即中止(绝不压测式重试)。
_MAX_CHUNK_FAILS = 2
#: 连板数聚合的回看窗口(自然日)。覆盖最长连板 + 假期余量。
_EVENTS_LOOKBACK_DAYS = 45

#: 封板资金落库口径(与 SC15 一致, 亿元)。原始金额=元。
MONEY_UNIT = "亿元"
_YUAN_PER_YI = 1e8

#: "封住"判定口径标识(与 limit_up_events 生成侧同源)。
CALIBER_SEALED = "close_ge_limit_price"

#: 封单量字段的源单位(手) —— 见 limit_pool_zdt.py:"封单量单位=手, 股=手×100"。
_SHARES_PER_HAND = 100

#: `get_zdt_data.ZDTStatusNow` 的**唯一**语义登记点。客户端 14 字段文档只注"当前状态",
#: 未给枚举 ⇒ 未登记的值一律 None(不猜)。已知: 1 = 涨停(与 DYNAINFO(88) 封单额
#: ">0=涨停状态" 同向)。仅用于交叉核对, 不参与"封住"结论。
_ZDT_STATUS_KNOWN: dict[int, bool] = {1: True}

#: 涨停价容差(与 limit_up_backfill._EPS 同值): 涨停价按分位取整, 留 1e-6 容差。
_EPS = 1e-6


def _num(v) -> float | None:
    """任意输入 → float 或 None(空/非法/非有限都当缺数据, 不用 0 冒充)。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _to_int(v, default: int = 0) -> int:
    """TQ 值(int/float/数字串) → int; 取不到给 default(不猜)。"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v)
    return default


def _bare(code) -> str:
    """`002361.SZ` / `SZ.002361` → `002361`。"""
    return (str(code or "").strip().split(".")[0])


# ── ①/② 涨停·炸板·封板资金(纯函数) ─────────────────────────────────────────

def is_sealed_close(close, prev_close, symbol6: str, *, is_st=None) -> bool | None:
    """收盘**封住**判定 —— 与 `limit_up_events` 生成侧同一口径(禁自创第二套)。

    `limit_up_backfill._extract_events_from_bars` 里 `sealed = close >= 涨停价 - eps`,
    涨停价 = `limit_rules.limit_up_price(昨收)`, 这里逐字复用该口径(只是拿不到最高价,
    所以只判收盘, 不判 touched)。

    缺收盘/昨收(新股首日/停牌/取数失败)或涨停价算不出(非 A 股代码) → None(显式无数据)。
    """
    from src.core.limit_rules import limit_up_price

    lp = limit_up_price(symbol6, _num(prev_close), is_st)
    c = _num(close)
    if lp is None or c is None:
        return None
    return c >= lp - _EPS


def _zdt_status_limit_up(v) -> bool | None:
    """`ZDTStatusNow` → 是否涨停; 未登记的枚举值 → None(不猜)。"""
    iv = _to_int(v, default=-999)
    return _ZDT_STATUS_KNOWN.get(iv)


def zt_metrics(snap: dict, prices: dict, names: dict | None = None) -> dict:
    """zdt 快照 + 批量价量 → ①涨停/炸板家数 + ②封板成功/失败资金(纯函数, 无 I/O)。

    Args:
        snap:   `get_zdt_data` 原始响应 ``{TQ码: 记录}``(全市场)。
        prices: ``{裸码: {"close": 现价/收盘价, "prev_close": 昨收}}``。
        names:  ``{裸码: 名称}``(判 ST, 与 limit_up_backfill 同法)。

    Returns: dict(四指标中的 ①② + 诊断; 单位见模块 docstring)。
    """
    from src.core.limit_pool_zdt import parse_zdt_snapshot

    live = parse_zdt_snapshot(snap)
    names = names or {}

    limit_up_count = 0  # 真触及涨停计数(逐票 touched 过滤后累加, 不用 len(live))
    sealed_count = 0
    open_times_gt0 = 0
    blow_count = 0
    price_missing = 0
    status_mismatch = 0
    status_dist: dict[str, int] = {}
    success_yuan = 0.0
    fail_yuan = 0.0

    for tq_code, rec in live.items():
        if not isinstance(rec, dict):
            continue
        bare = _bare(tq_code)
        px = prices.get(bare) or {}
        close, prev_close = px.get("close"), px.get("prev_close")
        name = names.get(bare) or names.get(tq_code) or ""
        is_st = ("ST" in name.upper()) if name else None

        sealed = is_sealed_close(close, prev_close, bare, is_st=is_st)
        open_times = _to_int(rec.get("OpenTimesZT"), 0)
        fd_hands = _to_int(rec.get("FDVolMaxZT"), 0)
        first_t = str(rec.get("FirstTimeZT") or "").strip("0: ")
        # 真触及涨停(全市场口径): 封住 / 开过板 / 有过封单量 / 有过首封时间。
        # ⚠️ parse_zdt_snapshot 的「字段存在即候选」判据只适用于预过滤池;
        # 全市场 get_zdt_data 对每只票都回 ZT 字段(值 0/空), 必须按**值**过滤。
        touched = sealed is True or open_times > 0 or fd_hands > 0 or bool(first_t)
        if not touched:
            continue
        limit_up_count += 1
        if sealed is None:
            price_missing += 1

        if open_times > 0:
            open_times_gt0 += 1
        # 炸板 = 打开过(OpenTimesZT>0) **或** 曾涨停未封住。
        # (未封住必然打开过, 故并集 ≈ OpenTimesZT>0; 并集写法对脏数据更稳, 且 garant 炸板 ≤ 涨停)
        if open_times > 0 or sealed is False:
            blow_count += 1

        # 封单额(元, 近似) = 封单量(手) × 100 × 收盘价
        amt_yuan: float | None = None
        fd = rec.get("FDVolMaxZT")
        c = _num(close)
        if c is not None and fd is not None:
            hands = _to_int(fd, 0)
            if hands > 0:
                amt_yuan = hands * _SHARES_PER_HAND * c
        if sealed is True:
            sealed_count += 1
            if amt_yuan is not None:
                success_yuan += amt_yuan
        elif sealed is False and amt_yuan is not None:
            fail_yuan += amt_yuan

        st_now = rec.get("ZDTStatusNow")
        status_dist[str(st_now) if st_now is not None else "none"] = (
            status_dist.get(str(st_now) if st_now is not None else "none", 0) + 1
        )
        inferred = _zdt_status_limit_up(st_now)
        if inferred is not None and sealed is not None and inferred != sealed:
            status_mismatch += 1

    # 候选**全都**取不到价量 ⇒ 封板资金 = **未知**(不是 0): 显式 None, 不落 0 冒充。
    # (只缺一部分时, 有价那侧的合计是真实值; 缺价的票已由 price_missing 显式计数)
    no_prices = limit_up_count > 0 and price_missing == limit_up_count
    money_kwargs = (
        {"seal_success_money": None, "seal_fail_money": None}
        if no_prices
        else {
            "seal_success_money": round(success_yuan / _YUAN_PER_YI, 4),
            "seal_fail_money": round(fail_yuan / _YUAN_PER_YI, 4),
        }
    )

    return {
        "limit_up_count": limit_up_count,
        "limit_up_open_count": blow_count,
        "sealed_count": sealed_count,
        "open_times_gt0": open_times_gt0,
        "price_missing": price_missing,
        "prices_available": not no_prices,
        "seal_success_money_yuan": round(success_yuan, 2),
        "seal_fail_money_yuan": round(fail_yuan, 2),
        "approx": True,
        "caliber": CALIBER_SEALED,
        "money_unit": MONEY_UNIT,
        "zdt_status_dist": status_dist,
        "status_mismatch": status_mismatch,
        **money_kwargs,
    }


# ── ③ 连板家数(limit_up_events 聚合, 纯函数) ────────────────────────────────

def streak_count_from_events(
    events: list[dict],
    trade_date: str,
    trading_days: list[str] | None = None,
) -> int | None:
    """连板家数: 目标交易日**收盘封住**且连续封板 ≥2 天的股票数。

    - 封住口径 = `limit_up_events.is_sealed_close`(即 ①② 同一口径的来源);
    - 连板 run 一律走 `market_phase.consecutive_boards`(全仓单一事实源);
    - `trading_days` = 交易日序(升序)。**生产必须传日历**: 只拿 events 里出现过的日期
      当序列时, 中间没有涨停的交易日会被跳过 ⇒ 连板数被高估。
    - 当日**无任何** `limit_up_events` 行 → None(显式无数据, 不是 0)。
    """
    day_rows = [e for e in (events or []) if str(e.get("trade_date")) == str(trade_date)]
    if not day_rows:
        return None

    from src.core.market_phase import consecutive_boards

    sealed = {
        (str(e.get("trade_date")), str(e.get("symbol"))): bool(e.get("sealed"))
        for e in (events or [])
        if e.get("trade_date") and e.get("symbol")
    }
    if trading_days:
        dates = [str(d) for d in trading_days if str(d) <= str(trade_date)]
    else:
        dates = sorted({d for d, _s in sealed if d <= str(trade_date)})

    boards = consecutive_boards(dates, sealed)
    return sum(1 for (d, _s), n in boards.items() if d == str(trade_date) and n >= 2)


# ── ④ 派生(走 thermometer 单一口径) ────────────────────────────────────────

def derive_ratios(
    limit_up_count, limit_up_open_count, seal_success_money, seal_fail_money
) -> dict:
    """派生 炸板比 / 封板成功率 —— 委托 `sentiment_thermometer.derive_thermometer`。

    不在这里写第二份公式: 温度计将来改口径(如 sick 值处理、分母口径)本模块自动跟随。
    """
    from src.core import sentiment_thermometer as st

    t = st.derive_thermometer(
        {
            "trade_date": "",
            "limit_up_count": limit_up_count,
            "limit_up_open_count": limit_up_open_count,
            "seal_success_money": seal_success_money,
            "seal_fail_money": seal_fail_money,
        }
    )
    return {
        "blow_ratio": t["blow_ratio"],
        "seal_success_rate": t["seal_success_rate"],
        "suspicious": t["suspicious"],
        "derive_reason": t["reason"],
    }


# ── 交易日 / 口径辅助 ──────────────────────────────────────────────────────

def target_trade_date(now: datetime | None = None) -> str:
    """自算目标交易日(YYYYMMDD): 今日为交易日取今日, 否则取最近交易日。

    快照型接口只能给"最近一个已收盘交易日"的数据, 故非交易日(周末/节假日)也**可自算**
    上一交易日 —— 这正是"周末发现断档就地自愈"的路径。
    """
    day = (now or datetime.now(_CST)).date()
    try:
        from src.core.trading_calendar import TradingCalendarError, is_trading_day, prev_trading_day

        try:
            if not is_trading_day(day):
                day = prev_trading_day(day)
        except TradingCalendarError:
            logger.warning("交易日历未覆盖当前年份, 自算沿用自然日 %s", day)
    except Exception as e:  # noqa: BLE001 —— 日历异常不该阻断自算
        logger.warning("交易日历不可用, 自算沿用自然日: %s", e)
    return day.strftime("%Y%m%d")


def _trading_days_between(start: str, end: str) -> list[str]:
    """[start, end] 区间内的**交易日**序列(升序); 日历不可用 → [](调用方退回事件日期)。

    ⚠️ 不要用 `trading_calendar.trading_days_between` —— 那个返回的是**数量**(int),
    不是日期序列(实测签名: `-> int`)。这里走 `is_trading_day`/`prev_trading_day` 逐日回退,
    与全仓交易日历同一事实源。
    """
    try:
        from src.core.trading_calendar import (
            TradingCalendarError,
            is_trading_day,
            prev_trading_day,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("交易日历不可用(连板聚合退回事件日期): %s", e)
        return []

    def _d(s: str):
        return datetime.strptime(str(s), "%Y%m%d").date()

    try:
        cur = _d(end)
        floor = _d(start)
        if not is_trading_day(cur):
            cur = prev_trading_day(cur)
        out: list[str] = []
        # 回退收集(带步数上限, 防"日历覆盖不到"时死循环)
        for _ in range(400):
            if cur < floor:
                break
            out.append(cur.strftime("%Y%m%d"))
            cur = prev_trading_day(cur)
    except (TradingCalendarError, ValueError) as e:
        logger.warning("交易日序列解析失败(连板聚合退回事件日期): %s", e)
        return []
    out.reverse()
    return out


# ── 数据源(默认实现; 全部可注入以便离线单测) ────────────────────────────────

def fetch_universe() -> tuple[list[str], dict[str, str]]:
    """全A TQ 码 + 名称表。→ ([TQ码], {TQ码: 名称, 裸码: 名称})。

    `get_stock_list` 单次调用(0.1s 级, 与涨停池主源同源), 不会压垮客户端。
    """
    import marketdata.vendors.tq as tqmod

    v = tqmod.tq_rpc("get_stock_list", {"market": "5", "list_type": 1}, timeout=15.0)
    items = v if isinstance(v, list) else []
    codes: list[str] = []
    names: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        code = str(it.get("Code") or "").strip()
        if not code:
            continue
        nm = str(it.get("Name") or "")
        codes.append(code)
        names[code] = nm
        names[_bare(code)] = nm
    return codes, names


def fetch_snapshot(codes: list[str]) -> tuple[dict, dict]:
    """分片 `get_zdt_data`(≤50/次) → (原始快照, 统计 {chunks_total, chunks_failed, aborted})。

    连续 `_MAX_CHUNK_FAILS` 片失败即**中止**(事故铁律: 绝不压测式重试)。
    """
    import marketdata.vendors.tq as tqmod

    snap: dict = {}
    total = failed = 0
    for i in range(0, len(codes), _CHUNK):
        part = [str(c) for c in codes[i : i + _CHUNK]]
        total += 1
        try:
            s = tqmod.zdt_snapshot(part)
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning("自算情绪: 快照第 %d 批失败(%s)", total, e)
            if failed >= _MAX_CHUNK_FAILS:
                logger.error("自算情绪: 快照连续 %d 批失败 → 中止并显式降级", failed)
                return snap, {"chunks_total": total, "chunks_failed": failed, "aborted": True}
            continue
        if isinstance(s, dict):
            snap.update(s)
    return snap, {"chunks_total": total, "chunks_failed": failed, "aborted": False}


def fetch_prices(codes: list[str]) -> tuple[dict, dict]:
    """分片 `get_pricevol`(≤50/次) → ({裸码: {close, prev_close}}, 统计)。"""
    import marketdata.vendors.tq as tqmod

    out: dict[str, dict] = {}
    total = failed = 0
    for i in range(0, len(codes), _CHUNK):
        part = [str(c) for c in codes[i : i + _CHUNK]]
        total += 1
        try:
            got = tqmod.pricevol(part)
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning("自算情绪: 价量第 %d 批失败(%s)", total, e)
            continue
        for code, p in (got or {}).items():
            if not isinstance(p, dict):
                continue
            out[_bare(code)] = {
                "close": _num(p.get("Now")),
                "prev_close": _num(p.get("LastClose")),
            }
    return out, {"price_chunks_total": total, "price_chunks_failed": failed}


def fetch_events(trade_date: str, *, days: int = _EVENTS_LOOKBACK_DAYS, db=None) -> list[dict]:
    """读 `limit_up_events` 近 days 自然日 → [{trade_date, symbol, sealed}]。

    只读, 不走网络。表未建/异常 → [](显式空, 调用方把连板家数落 None 不落 0)。
    """
    from sqlalchemy import text

    try:
        start = (
            datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=int(days))
        ).strftime("%Y%m%d")
    except (TypeError, ValueError):
        return []

    owns = db is None
    if owns:
        from src.db.session import SessionLocal

        db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT trade_date, symbol, is_sealed_close AS sealed FROM limit_up_events "
                "WHERE trade_date >= :start AND trade_date <= :end"
            ),
            {"start": start, "end": trade_date},
        ).fetchall()
    except Exception as e:  # noqa: BLE001 —— 表未建/方言异常都算缺数据(显式)
        logger.warning("自算情绪: 读 limit_up_events 失败: %s", e)
        return []
    finally:
        if owns:
            db.close()
    return [
        {
            "trade_date": str(dict(r._mapping).get("trade_date")),
            "symbol": str(dict(r._mapping).get("symbol")),
            "sealed": bool(dict(r._mapping).get("sealed")),
        }
        for r in rows
    ]


# ── 编排: 自算一天 ─────────────────────────────────────────────────────────

def collect_and_compute(
    trade_date: str | None = None,
    *,
    now: datetime | None = None,
    db=None,
    universe_fn=None,
    snapshot_fn=None,
    price_fn=None,
    events_fn=None,
    calendar_fn=None,
) -> dict:
    """自算一日四指标(**不落库**, 纯编排)。所有数据源可注入 → 离线可测。

    Returns: dict —— ``ok`` 为 False 时**不得落库**(调用方须按显式失败处理)。
    """
    trade_date = str(trade_date or target_trade_date(now))
    errors: list[str] = []

    universe_fn = universe_fn or fetch_universe
    snapshot_fn = snapshot_fn or fetch_snapshot
    price_fn = price_fn or fetch_prices
    events_fn = events_fn or fetch_events
    calendar_fn = calendar_fn or _trading_days_between

    def _events() -> list[dict]:
        try:
            return events_fn(trade_date, db=db) if events_fn is fetch_events else events_fn(trade_date)
        except TypeError:
            return events_fn(trade_date)

    try:
        codes, names = universe_fn()
    except Exception as e:  # noqa: BLE001
        logger.warning("自算情绪: 取全A码表失败: %s", e)
        return _fail(trade_date, f"取全A码表失败: {e}")

    if not codes:
        return _fail(trade_date, "全A码表为空(get_stock_list 无返回)")

    try:
        snap, snap_stats = snapshot_fn(codes)
    except Exception as e:  # noqa: BLE001
        logger.warning("自算情绪: 取涨跌停快照失败: %s", e)
        return _fail(trade_date, f"取涨跌停快照失败: {e}", stats={"chunks_total": 0, "chunks_failed": 0})

    if snap_stats.get("aborted"):
        return _fail(
            trade_date,
            "TQ 涨跌停快照连续分片失败, 中止(事故铁律, 不压测式重试)",
            stats=snap_stats,
        )

    from src.core.limit_pool_zdt import parse_zdt_snapshot

    live = parse_zdt_snapshot(snap)
    if not live:
        return _fail(
            trade_date,
            "涨跌停快照无真涨停条目(盘前/非交易日/网关返回空) —— 不落 0 冒充",
            stats=snap_stats,
        )

    # 只对候选取价量(几十~两百只, 4 片内)
    try:
        prices, price_stats = price_fn(list(live.keys()))
    except Exception as e:  # noqa: BLE001
        logger.warning("自算情绪: 取批量价量失败: %s", e)
        prices, price_stats = {}, {"price_chunks_total": 0, "price_chunks_failed": 0}
        errors.append(f"价量取数失败(封住/资金判不了): {e}")

    m = zt_metrics(snap, prices, names)
    if not m.get("prices_available", True):
        errors.append("候选全部取不到价量, 封板成功/失败资金落 None(不落 0 冒充)")

    events = _events()
    if not events:
        errors.append("limit_up_events 当日无数据, 连板家数落 None(不落 0)")
    trading_days = calendar_fn(
        (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=_EVENTS_LOOKBACK_DAYS)).strftime("%Y%m%d"),
        trade_date,
    )
    streak = streak_count_from_events(events, trade_date, trading_days or None)

    ratios = derive_ratios(
        m["limit_up_count"], m["limit_up_open_count"], m["seal_success_money"], m["seal_fail_money"]
    )

    extra = {
        "commission": "自算替代云 SC(get_scjy_value 已弃用, 用户决策不购云 Key)",
        "approximation": {
            "seal_money": "封单量(FDVolMaxZT 手)×100×收盘价 → 元 → 亿元; FDVolMaxZT 为当日最大封单量, 非收盘瞬时封单",
            "limit_up_count": "zdt 真涨停候选家数(=SC3 v1 涨停/曾涨停口径)",
            "streak_count": "limit_up_events 聚合(收盘封住且连续封板 ≥2 天)",
        },
        "caliber": CALIBER_SEALED,
        "money_unit": MONEY_UNIT,
        "money_unit_raw": "元",
        "seal_success_money_yuan": m["seal_success_money_yuan"],
        "seal_fail_money_yuan": m["seal_fail_money_yuan"],
        "zdt_status_dist": m["zdt_status_dist"],
        "status_mismatch": m["status_mismatch"],
        "candidates": m["limit_up_count"],
        "sealed_count": m["sealed_count"],
        "open_times_gt0": m["open_times_gt0"],
        "price_missing": m["price_missing"],
        "prices_available": m.get("prices_available", True),
        "chunks": {**snap_stats, **price_stats},
        "events_rows": len(events),
        "errors": errors,
    }
    return {
        "ok": True,
        "trade_date": trade_date,
        "market": MARKET,
        "limit_up_count": m["limit_up_count"],
        "limit_up_open_count": m["limit_up_open_count"],
        "streak_count": streak,
        "seal_success_money": m["seal_success_money"],
        "seal_fail_money": m["seal_fail_money"],
        "seal_success_rate": ratios["seal_success_rate"],
        "blow_ratio": ratios["blow_ratio"],
        "sealed_count": m["sealed_count"],
        "open_times_gt0": m["open_times_gt0"],
        "price_missing": m["price_missing"],
        "chunks_total": snap_stats.get("chunks_total", 0),
        "chunks_failed": snap_stats.get("chunks_failed", 0),
        "approximation": True,
        "caliber": CALIBER_SEALED,
        "money_unit": MONEY_UNIT,
        "source": SOURCE,
        "extra": extra,
        "errors": errors,
    }


def _fail(trade_date: str, reason: str, *, stats: dict | None = None) -> dict:
    return {
        "ok": False,
        "trade_date": trade_date,
        "market": MARKET,
        "source": SOURCE,
        "reason": reason,
        "extra": {"errors": [reason], "chunks": stats or {}},
        "errors": [reason],
    }


# ── 落库(幂等) ────────────────────────────────────────────────────────────

_SENTIMENT_COLS = (
    "limit_up_count",
    "limit_up_open_count",
    "streak_count",
    "seal_success_money",
    "seal_fail_money",
)

_SELF_COLS = (
    "limit_up_count",
    "limit_up_open_count",
    "streak_count",
    "seal_success_money",
    "seal_fail_money",
    "seal_success_rate",
    "blow_ratio",
    "candidates",
    "sealed_count",
    "open_times_gt0",
    "price_missing",
    "chunks_total",
    "chunks_failed",
    "approximation",
    "caliber",
    "money_unit",
    "source",
    "extra",
)


def persist(result: dict, *, db=None) -> dict:
    """把自算结果落库(幂等)。``result["ok"] is False`` → 直接拒绝(不写半行)。

    - `market_sentiment_daily`: 只写 5 个兼容列; 已有非空值**不覆盖**(COALESCE) ⇒
      重复跑幂等, 且 SC 真值若有仍优先(可选增强源语义);
    - `sentiment_selfcalc_daily`: 自算明细/诊断(整行 upsert, 同日重跑原地更新)。
    """
    if not result.get("ok"):
        return {"ok": False, "written": 0, "reason": result.get("reason") or "自算未成功"}

    from sqlalchemy import text

    owns = db is None
    if owns:
        from src.db.session import SessionLocal

        db = SessionLocal()
    written = 0
    try:
        td = result["trade_date"]
        params = {
            "trade_date": td,
            "market": MARKET,
            "limit_up_count": result.get("limit_up_count"),
            "limit_up_open_count": result.get("limit_up_open_count"),
            "streak_count": result.get("streak_count"),
            "seal_success_money": result.get("seal_success_money"),
            "seal_fail_money": result.get("seal_fail_money"),
            "seal_success_rate": result.get("seal_success_rate"),
            "blow_ratio": result.get("blow_ratio"),
            "candidates": result.get("limit_up_count"),
            "sealed_count": result.get("sealed_count"),
            "open_times_gt0": result.get("open_times_gt0"),
            "price_missing": result.get("price_missing"),
            "chunks_total": result.get("chunks_total", 0),
            "chunks_failed": result.get("chunks_failed", 0),
            "approximation": 1 if result.get("approximation") else 0,
            "caliber": result.get("caliber") or CALIBER_SEALED,
            "money_unit": result.get("money_unit") or MONEY_UNIT,
            "source": result.get("source") or SOURCE,
            "extra": json.dumps(result.get("extra") or {}, ensure_ascii=False),
        }

        # ① 兼容列(温度计/API/新鲜度自检零改动即读) —— 已有非空不动
        cols = ", ".join(_SENTIMENT_COLS)
        sets = ", ".join(
            f"{c} = COALESCE({SENTIMENT_TABLE}.{c}, EXCLUDED.{c})" for c in _SENTIMENT_COLS
        )
        db.execute(
            text(
                f"INSERT INTO {SENTIMENT_TABLE} (trade_date, market, {cols}, source) "
                f"VALUES (:trade_date, :market, {', '.join(':' + c for c in _SENTIMENT_COLS)}, :source) "
                f"ON CONFLICT (trade_date, market) DO UPDATE SET {sets}, "
                f"updated_at = CURRENT_TIMESTAMP"
            ),
            params,
        )

        # ② 明细/诊断表(整行 upsert)
        from src.db.dialect import upsert_sql

        insert_cols = ("trade_date", "market") + _SELF_COLS
        sql = upsert_sql(
            TABLE,
            insert_cols=insert_cols,
            conflict_cols=("trade_date", "market"),
            update_cols=_SELF_COLS,
        )
        db.execute(text(sql), params)
        db.commit()
        written = 1
    except Exception as e:  # noqa: BLE001
        logger.warning("自算情绪: 落库失败: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "written": 0, "reason": f"落库失败: {e}"}
    finally:
        if owns:
            db.close()
    return {"ok": True, "written": written, "trade_date": result["trade_date"]}


def selfcalc_for_date(
    trade_date: str | None = None, *, db=None, now: datetime | None = None, **providers
) -> dict:
    """自算 + 落库一日(供 cron 入口与新鲜度自愈复用)。"""
    res = collect_and_compute(trade_date, now=now, db=db, **providers)
    if not res.get("ok"):
        return {**res, "written": 0}
    w = persist(res, db=db)
    return {**res, "written": w.get("written", 0), "persist_ok": w.get("ok"),
            "persist_reason": w.get("reason")}


# ── cron 入口 ─────────────────────────────────────────────────────────────

def run_selfcalc_job(now: datetime | None = None, *, db=None, **providers) -> dict:
    """cron 入口(每交易日盘后): 自算并落库; 走作业框架诚实性(ok=False → failed)。"""
    from src.core.jobs import jobs

    job_id, is_new = jobs.create(JOB_KIND, JOB_LABEL)
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id,
                "note": "上一轮自算仍在跑, 跳过本轮"}
    try:
        out = selfcalc_for_date(None, db=db, now=now, **providers)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("市场情绪自算作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="市场情绪自算: ")
    return out
