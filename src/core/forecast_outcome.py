"""预测历史「到期对照」应用侧 enrichment(2026-09-29)。

背景
----
前端 `frontend/src/pages/Forecast.tsx` 的历史表已实现「到期对照」列: 仅当
`/api/forecast/history` 返回 `outcome_return_pct` / `outcome_status` 任一字段时才出现
(`historyHasOutcome = history.some(...)`)。该端点此前**直接代理** :8010 预测引擎
(`FORECAST_ENGINE_URL`), outcome 只能靠引擎侧 `forecast_lib` 自己对照行情 —— 但
`forecast_lib/` 是 8010 镜像专用(镜像内没有 `src/`, 禁 import `src.*`), 且引擎侧
对照口径与 UI 行情不同源。故改在**应用侧**(8000)做 enrichment。

本模块职责
----------
拿引擎返回的历史预测列表, 对每条**已到期**的预测, 用应用自己的日K
(`KlineCollector.get_klines()`, PG hypertable 优先) 对照预测基准日收盘算实际涨跌幅,
对每条**新增**两个字段(引擎返回体与既有字段值原样保留):
  - `outcome_return_pct: float | None` —— 实际涨跌幅 %(相对预测基准日收盘)
  - `outcome_status: 'hit' | 'miss' | 'pending' | 'no_data'`

判定规则(SIDA 硬约束: 数据缺失一律显式 null/no_data, **禁编造、禁填 0**)
----------------------------------------------------------------------
  - `pending`  目标日未到, **或目标日就是今天**(T 日收盘价未落定; 实测 K 线源
               盘中会给出当日实时柱, 拿它当到期收盘就是编造) —— 一律不出对照结果
  - `no_data`  到期但取不到行情 / 行情不足以覆盖预测窗口 / 到期日无法确定 /
               方向字段缺失无法判定(_此时仍如实返回已算出的 outcome_return_pct_)
  - `hit`      实际涨跌方向与该条预测方向一致(up↔涨, down↔跌, flat↔持平)
  - `miss`     方向不一致; 涨幅恰好 0.00% 对 up/down 判 miss(方向未兑现),
               对 flat 判 hit
  - 基准价优先取**同源 K 线**里基准日(`last_date`)或之前最近的收盘 —— 与预测引擎
    "同源序列算相对涨跌"口径一致, 规避复权口径混用导致的失真; 同源 K 线覆盖不到
    基准日时才回落该条预测自带的 `last_close`。

口径与降级
----------
  - **不改引擎、不改现有字段语义**: 应用侧算得出结果时以本模块结果覆盖 `outcome_*`
    两字段(应用侧行情与 UI 同源, 更权威); 算不出(no_data)时保留引擎已给的值,
    引擎也没给才写 `no_data` —— 保证不会因为应用侧取数失败而"擦掉"引擎的评估结果。
  - 单条评估异常 / 单标的取K线失败只影响该条, 不抛给调用方; 整体异常由调用方
    (`src/web/api/forecast.py::_enrich_forecast_history`)捕获并降级返回引擎原始数据。
  - 每次请求最多评估 `MAX_SYMBOLS_PER_REQUEST` 个标的(按列表顺序取前 N, 引擎按
    id DESC 返回 → 优先评估最新预测), 超出部分的条目保持引擎原值, 不阻塞列表。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from src.collectors.kline_collector import KlineCollector
from src.core.prediction_outcome import _add_trading_days, _parse_day
from src.models.market import MarketCode

logger = logging.getLogger(__name__)

# 单次请求最多评估的标的数(超出的条目保持引擎原值/不新增字段, 不影响列表可用性)
MAX_SYMBOLS_PER_REQUEST = 50
# 取 K 线并发度(同一标的的并发在 KlineCollector 内部已合并, 这里只限制标的间并发)
_FETCH_CONCURRENCY = 4
# K 线回溯上限(与 src/core/prediction_outcome.py 评估窗口一致)
_MAX_LOOKBACK_DAYS = 600

STATUS_HIT = "hit"
STATUS_MISS = "miss"
STATUS_PENDING = "pending"
STATUS_NO_DATA = "no_data"


def _parse_bars(klines) -> list[tuple[date, float]]:
    """把 K 线对象列表规整成按日期升序的 (date, close), 脏数据跳过。"""
    bars: list[tuple[date, float]] = []
    for k in klines or []:
        day = _parse_day(getattr(k, "date", None))
        close = getattr(k, "close", None)
        if day is None or close is None:
            continue
        try:
            price = float(close)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        bars.append((day, price))
    bars.sort(key=lambda x: x[0])
    return bars


def _pick_bar(bars: list[tuple[date, float]], day: date) -> tuple[date, float] | None:
    """取 `day` 当天或之前最近的一根 K 线(升序列表, 无则 None)。"""
    picked: tuple[date, float] | None = None
    for bar in bars:
        if bar[0] > day:
            break
        picked = bar
    return picked


def _resolve_target_day(item: dict) -> date | None:
    """确定该条预测的到期日。

    引擎正常落 `target_date`(交易日, `pred_dates[-1]`)。老记录可能为空, 此时按
    `last_date` + `pred_days` 个交易日推算(与引擎 target_date 口径一致, 走
    src/core/trading_calendar 法定节假日表); 推算不出来返回 None(→ no_data)。
    """
    day = _parse_day(item.get("target_date"))
    if day is not None:
        return day
    last_day = _parse_day(item.get("last_date"))
    if last_day is None:
        return None
    raw = item.get("pred_days")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        horizon = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    if horizon <= 0:
        return None
    try:
        return _add_trading_days(last_day, horizon)
    except Exception as e:  # 交易日历未覆盖的年份等 → 显式无数据
        logger.warning("到期对照推算目标日失败: %s", e)
        return None


def _judge_status(direction, pct: float) -> str | None:
    """按方向与涨跌幅符号判 hit/miss; 方向不可识别返回 None(调用方给 no_data)。"""
    direction_text = str(direction or "").strip().lower()
    if direction_text == "up":
        return STATUS_HIT if pct > 0 else STATUS_MISS
    if direction_text == "down":
        return STATUS_HIT if pct < 0 else STATUS_MISS
    if direction_text == "flat":
        return STATUS_HIT if pct == 0 else STATUS_MISS
    return None


def evaluate_history_item(
    item: dict,
    bars: list[tuple[date, float]],
    *,
    today: date,
) -> dict:
    """算单条的到期对照结果。

    返回**只含能确定字段**的 dict:
      - 到期日未到(含目标日=今天: T 日收盘价未落定) → `{"outcome_status": "pending"}`
      - 算得出涨跌幅 → `{"outcome_return_pct": float, "outcome_status": ...}`
      - 到期但无行情/无法判定 → `{"outcome_status": "no_data"}`
        (方向字段缺失无法判定时也返回已算出的 `outcome_return_pct`, 不丢真实数据)
    """
    target_day = _resolve_target_day(item)
    if target_day is None:
        return {"outcome_status": STATUS_NO_DATA}
    if target_day >= today:
        # 目标日未到; 或目标日就是今天而 T 日收盘价还没落定(盘中取到的是未收官价,
        # 实测 K 线源会给出当日实时柱) → 一律不出对照结果, 免得把未定的数当成到期涨跌
        return {"outcome_status": STATUS_PENDING}
    if not bars:
        return {"outcome_status": STATUS_NO_DATA}

    outcome_bar = _pick_bar(bars, target_day)
    if outcome_bar is None:
        return {"outcome_status": STATUS_NO_DATA}

    last_day = _parse_day(item.get("last_date"))
    base_bar = _pick_bar(bars, last_day) if last_day is not None else None
    if last_day is not None and outcome_bar[0] < last_day:
        # 到期价早于预测基准日 → 行情不足以覆盖预测窗口, 不许倒着算
        return {"outcome_status": STATUS_NO_DATA}

    base_price = base_bar[1] if base_bar is not None else None
    if base_price is None or base_price <= 0:
        # 同源 K 线覆盖不到基准日 → 回落该条预测自带的基准收盘(引擎记录值)
        try:
            raw = item.get("last_close")
            fallback = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            fallback = None
        base_price = fallback if fallback is not None and fallback > 0 else None
    if base_price is None:
        return {"outcome_status": STATUS_NO_DATA}

    pct = round((outcome_bar[1] - base_price) / base_price * 100, 2)
    status = _judge_status(item.get("direction"), pct)
    if status is None:
        # 方向字段缺失/不可识别: 涨跌幅是真的, 但不做 hit/miss 判定
        return {"outcome_return_pct": pct, "outcome_status": STATUS_NO_DATA}
    return {"outcome_return_pct": pct, "outcome_status": status}


def merge_outcome(item: dict, computed: dict) -> dict:
    """把计算结果并进条目副本。

    规则: 应用侧算得出就覆盖; 应用侧 no_data(取不到行情/判不了方向)时**保留**引擎
    已给的评估值 —— 不因应用侧取数失败而"擦掉"引擎的结果; 两边都没有才写
    `outcome_return_pct=null` + `outcome_status='no_data'`(显式无数据, 禁填 0)。
    """
    row = dict(item)
    status = computed.get("outcome_status")
    if status is not None and not (status == STATUS_NO_DATA and row.get("outcome_status")):
        row["outcome_status"] = status
    if "outcome_return_pct" in computed:
        row["outcome_return_pct"] = computed["outcome_return_pct"]
    if "outcome_return_pct" not in row:
        row["outcome_return_pct"] = None
    if not row.get("outcome_status"):
        row["outcome_status"] = STATUS_NO_DATA
    return row


def _load_klines(symbol: str, days: int):
    """应用侧日K(红线: 读取走 PG hypertable 优先, 由 get_klines 内部保证)。"""
    return KlineCollector(MarketCode.CN).get_klines(symbol, days=days)


def _normalize_symbol(value) -> str:
    """引擎记录为裸 6 位码; 防御性去掉 `.SH`/`.SZ` 之类的市场后缀。"""
    return str(value or "").strip().split(".")[0]


def _lookback_days(item: dict, target_day: date, today: date) -> int:
    last_day = _parse_day(item.get("last_date")) or target_day
    return min(_MAX_LOOKBACK_DAYS, max(120, (today - last_day).days + 30))


async def enrich_history_outcomes(
    payload,
    *,
    today: date | None = None,
    max_symbols: int = MAX_SYMBOLS_PER_REQUEST,
    klines_loader=None,
):
    """对 `/forecast/history` 的引擎返回体做到期对照 enrichment(保持原结构)。

    - `payload` 支持 `{"items": [...]}`(引擎现状)或裸 list; 其他结构原样返回。
    - `klines_loader(symbol, days) -> list` 可注入(测试用), 默认走 `_load_klines`。
    - 返回结构与入参一致, 条目为**副本**, 引擎原有字段一律不改写。
    """
    wrapper: dict | None
    if isinstance(payload, dict):
        if not isinstance(payload.get("items"), list):
            return payload
        items = payload["items"]
        wrapper = payload
    elif isinstance(payload, list):
        items = payload
        wrapper = None
    else:
        return payload

    today = today or date.today()
    loader = klines_loader or _load_klines

    # 1) 先挑出"已到期需要取行情"的标的(去重, 有上限)
    needed: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        target_day = _resolve_target_day(item)
        if target_day is None or target_day >= today:
            continue
        symbol = _normalize_symbol(item.get("symbol"))
        if not symbol:
            continue
        days = _lookback_days(item, target_day, today)
        needed[symbol] = max(needed.get(symbol, 0), days)
    if max_symbols > 0:
        selected = dict(list(needed.items())[:max_symbols])
    else:
        selected = {}

    # 2) 取行情(sync 采集放线程池, 不阻塞事件循环; 单标的失败只影响该条)
    bars_by_symbol: dict[str, list[tuple[date, float]]] = {}
    if selected:
        semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def _fetch(symbol: str, days: int):
            async with semaphore:
                try:
                    klines = await asyncio.to_thread(loader, symbol, days)
                except Exception as e:
                    logger.warning("到期对照取K线失败: %s - %s", symbol, e)
                    return symbol, []
                return symbol, _parse_bars(klines)

        for symbol, bars in await asyncio.gather(*(_fetch(s, d) for s, d in selected.items())):
            bars_by_symbol[symbol] = bars

    # 3) 逐条评估(单条异常不影响其他条目)
    enriched: list = []
    for item in items:
        if not isinstance(item, dict):
            enriched.append(item)
            continue
        try:
            symbol = _normalize_symbol(item.get("symbol"))
            target_day = _resolve_target_day(item)
            if target_day is not None and target_day >= today:
                # 未到期(含目标日=今天, T 日收盘价未定): 不需要行情就能判定
                enriched.append(merge_outcome(item, {"outcome_status": STATUS_PENDING}))
            elif target_day is None:
                # 记录不全(到期日都推不出来) → 显式无数据
                enriched.append(merge_outcome(item, {"outcome_status": STATUS_NO_DATA}))
            elif symbol in selected:
                bars = bars_by_symbol.get(symbol, [])
                enriched.append(
                    merge_outcome(item, evaluate_history_item(item, bars, today=today))
                )
            else:
                # 到期但本次没取行情(超过评估上限/无 symbol): 不猜, 保持引擎原值
                enriched.append(dict(item))
        except Exception as e:
            logger.warning("到期对照评估失败(id=%s): %s", item.get("id"), e)
            enriched.append(dict(item))

    if wrapper is None:
        return enriched
    result = dict(wrapper)
    result["items"] = enriched
    return result
