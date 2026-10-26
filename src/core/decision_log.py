"""决策日志(B6, 2026-09-18) —— 把"信号 → 结果"变成可统计的样本。

## 为什么
共振(三指标)/GS 买卖点/竞价池这些信号一直能"看", 但**没有账**: 哪个信号在什么市场状态下真管用?
没有账就只能靠感觉调参数, 也永远无法证伪自己。这张表把每个信号**连同当时的证据与价格**留痕,
事后用真实的后续 K 线回填 T+1/T+3/T+5 收益与命中。

## 三条纪律(与全仓诚实口径一致)
1. **取不到就 NULL**: `price_at_signal` / 收益 / 命中缺失一律 NULL, **绝不补 0**
   (0 是真实收益, 不能冒充"没有数据")。
2. **回填必须等未来那根 K 线真的存在**: 不许用之后的数据推算, 也不许"假设持有"。
3. **样本不足就不给结论**: 命中率在 `n < MIN_SAMPLE` 时返回 `insufficient=True` 且**不给数值**,
   页面得显示"样本不足", 而不是拿 3 个样本算出 67% 这种数字去误导决策。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.core.jobs import jobs
from src.core.stock_names import names_for

logger = logging.getLogger(__name__)

#: 回填作业在作业框架里的 kind(单飞复用与作业面板都按它归类)
BACKFILL_JOB_KIND = "decision_backfill"

_CST = ZoneInfo("Asia/Shanghai")

#: 命中率最小样本量 —— 低于它不给数字(避免小样本误导)
MIN_SAMPLE = 30

#: 回填要求的未来交易日数(与 stats 的 horizon 对齐)
HORIZONS = (1, 3, 5)

#: 回填候选窗口的"查看"上限(与"填多少"解耦, 见 `backfill_outcomes`; 缺省取 `max(2*limit, 本值)`)。
#: 生产实证(2026-10-14): `limit=500` 单跑只看 500 条候选, 而积压里 20260925(中秋休市,
#: 信号日无 K 线, T+n 永远算不出来)的 597 行恰好排在 waiters 最前 → 整跑 0 填。
DEFAULT_SCAN_CAP = 5000

#: 定时回填单跑的目标填充行数(交易日 18:35)。积压清空靠它: 2026-10-14 实测积压 3270 行
#: (其中 ~2350 行 K 线早已就绪), 旧的 500/天 口径要 6 天以上才能追上, 期间新信号继续堆。
BACKFILL_DAILY_LIMIT = 5000

#: 回填终态: 信号日无 K 线锚点(休市/停牌) → T+n 无从起算, 显式标注、收益列保持 NULL。
#: NULL = 尚未定论(未到期/部分已填), 仍会进候选窗口。
OUTCOME_NO_ANCHOR = "no_anchor"

#: 回填终态: 停牌跨期样本(2026-10-15) —— T+n 取「信号日起第 n 根可用 K 线」, 遇长期停牌会把
#: 一个月后的收盘当 T+1。`t_n_span > SUSPENDED_GAP_MAX_SPAN` 即标此态, **覆盖原状态**,
#: 收益列**保持原值不动**(用户裁决: 不重算存量数值, 只标注); 胜率/分桶统计据此排除该行。
OUTCOME_SUSPENDED_GAP = "suspended_gap"

#: 停牌跨期判据(交易日): 信号日→T+1 取值日跨度 > 本值 → `suspended_gap`。
#: 正常票 T+1 取值日=次一交易日(span=1); 一周(5 交易日)内的小休整不算跨期, 超过即标。
SUSPENDED_GAP_MAX_SPAN = 5


def _today_cst() -> str:
    return datetime.now(_CST).date().isoformat()


def record_signal(
    engine: Engine,
    *,
    signal_kind: str,
    symbol: str,
    trade_date: str | None = None,
    price: float | None = None,
    context: dict[str, Any] | None = None,
    source: str = "",
    regime: str | None = None,
) -> dict[str, Any]:
    """记录一个信号(幂等: 同 kind+symbol+trade_date 走 UPDATE, 不堆行)。

    `price=None` 表示**当时取不到价** —— 如实留空, 不用 0 或"之后的价格"顶上。

    `regime`(可选, 2026-10-10 A 决策提胜率): 信号产生时的情绪周期态(规范 key,
    见 `src.core.market_regime`)。落库供**按 regime 分桶统计命中率**; 缺省 None →
    分桶归入 'unknown'。二次写入未给 regime 时保留原值(不抹成 NULL)。
    """
    day = trade_date or _today_cst()
    sym = str(symbol or "").strip()
    kind = str(signal_kind or "").strip()
    if not kind or not sym:
        raise ValueError("signal_kind 与 symbol 都必填")
    ctx = json.dumps(context or {}, ensure_ascii=False, separators=(",", ":"))[:4000]
    reg = _norm_regime(regime)
    # 兼容老库(未跑 v184): 表无 regime 列时**不写 regime**(不炸), 待迁移后自然生效。
    if reg is not None and _has_regime_column(engine):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
INSERT INTO decision_log (signal_kind, symbol, trade_date, price_at_signal, context_json, source, regime)
VALUES (:kind, :symbol, :day, :price, :ctx, :source, :regime)
ON CONFLICT (signal_kind, symbol, trade_date) DO UPDATE SET
  price_at_signal = COALESCE(excluded.price_at_signal, decision_log.price_at_signal),
  context_json = excluded.context_json,
  source = excluded.source,
  regime = COALESCE(excluded.regime, decision_log.regime)
"""
                ),
                {"kind": kind, "symbol": sym, "day": day, "price": price, "ctx": ctx,
                 "source": source, "regime": reg},
            )
    else:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
INSERT INTO decision_log (signal_kind, symbol, trade_date, price_at_signal, context_json, source)
VALUES (:kind, :symbol, :day, :price, :ctx, :source)
ON CONFLICT (signal_kind, symbol, trade_date) DO UPDATE SET
  price_at_signal = COALESCE(excluded.price_at_signal, decision_log.price_at_signal),
  context_json = excluded.context_json,
  source = excluded.source
"""
                ),
                {"kind": kind, "symbol": sym, "day": day, "price": price, "ctx": ctx,
                 "source": source},
            )
    return {"signal_kind": kind, "symbol": sym, "trade_date": day, "price": price, "regime": reg}


# 老库/新库列存在性缓存(按引擎实例; 表结构在一次进程内不中途变)
_regime_col_cache: dict[Any, bool] = {}


def _has_regime_column(engine: Engine) -> bool:
    """decision_log 是否有 `regime` 列(兼容未跑 v184 的老库/老测试库)。永不抛。"""
    key = (id(engine), str(getattr(engine, "url", "")))
    hit = _regime_col_cache.get(key)
    if hit is not None:
        return hit
    ok = False
    try:
        from sqlalchemy import inspect as _inspect

        cols = {c["name"] for c in _inspect(engine).get_columns("decision_log")}
        ok = "regime" in cols
    except Exception:  # noqa: BLE001 —— 表不存在/方言异常: 视为无该列
        ok = False
    _regime_col_cache[key] = ok
    return ok


# outcome_state/outcome_note 列存在性缓存(与 regime 同理, 单独一份)
_state_col_cache: dict[Any, bool] = {}


def _has_outcome_state_column(engine: Engine) -> bool:
    """decision_log 是否有 `outcome_state` 列(兼容未跑 v197 的老库/老测试库)。永不抛。"""
    key = (id(engine), str(getattr(engine, "url", "")))
    hit = _state_col_cache.get(key)
    if hit is not None:
        return hit
    ok = False
    try:
        from sqlalchemy import inspect as _inspect

        cols = {c["name"] for c in _inspect(engine).get_columns("decision_log")}
        ok = "outcome_state" in cols
    except Exception:  # noqa: BLE001 —— 表不存在/方言异常: 视为无该列
        ok = False
    _state_col_cache[key] = ok
    return ok


# t_n_span 列存在性缓存(与 outcome_state 同理, 单独一份; v198 与 v197 是两条迁移)
_span_col_cache: dict[Any, bool] = {}


def _has_t_n_span_column(engine: Engine) -> bool:
    """decision_log 是否有 `t_n_span` 列(兼容未跑 v198 的老库/老测试库)。永不抛。"""
    key = (id(engine), str(getattr(engine, "url", "")))
    hit = _span_col_cache.get(key)
    if hit is not None:
        return hit
    ok = False
    try:
        from sqlalchemy import inspect as _inspect

        cols = {c["name"] for c in _inspect(engine).get_columns("decision_log")}
        ok = "t_n_span" in cols
    except Exception:  # noqa: BLE001 —— 表不存在/方言异常: 视为无该列
        ok = False
    _span_col_cache[key] = ok
    return ok


def _mark_no_anchor(engine: Engine, pid: int, note: str) -> None:
    """把"信号日无 K 线锚点"的行显式标注(**只写标注, 不碰收益列** —— 不填 0 冒充真值)。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE decision_log SET outcome_state = :st, outcome_note = :nt WHERE id = :pid"
            ),
            {"st": OUTCOME_NO_ANCHOR, "nt": note, "pid": pid},
        )


def _norm_regime(regime: str | None) -> str | None:
    """规范 regime key(缺省→None, 分桶时归 unknown); 未知值也归 unknown(不硬编造状态)。"""
    if regime is None:
        return None
    s = str(regime).strip()
    if not s:
        return None
    try:
        from src.core.market_regime import normalize_regime

        return normalize_regime(s)
    except Exception:  # noqa: BLE001
        return s


def record_many(engine: Engine, rows: list[dict[str, Any]], *, source: str = "") -> int:
    """批量记录(同一个 source); 单条失败只记 warning, 不拖垮整批。"""
    ok = 0
    for r in rows:
        try:
            record_signal(engine, source=source, **r)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("决策日志写入失败 %s: %r", r.get("symbol"), exc)
    return ok


def record_many_safe(rows: list[dict[str, Any]], *, source: str = "", engine: Engine | None = None) -> int:
    """emit 侧便捷入口: 把一批信号写决策账本, **绝不抛异常**。

    信号生成优先 —— 账本留痕是旁路: 取引擎失败/表不存在/写入报错一律只 warn,
    返回实际写入条数(0 表示没写进, 不阻断主流程)。engine 缺省走写库。
    """
    if not rows:
        return 0
    try:
        if engine is None:
            from src.db.session import get_write_engine

            engine = get_write_engine()
        return record_many(engine, rows, source=source)
    except Exception as exc:  # noqa: BLE001 —— 旁路失败只 warn
        logger.warning("决策日志留痕失败(不影响主流程): %r", exc)
        return 0


def _close_series_pg(engine: Engine, symbol: str, start_day: str) -> list[tuple[str, float]]:
    """该标的从 start_day 起的日线收盘(升序, 每交易日一根)。

    `klines` 是 TimescaleDB hypertable, 时间列是 **`ts`**(不是 trade_date), 且同一 (symbol, period, ts)
    可能有多源 → 这里按 `ts, source` 排序后在 Python 侧**按日期去重**(每交易日取第一条, 结果确定)。
    取不到就返回空 —— 由调用方决定"这根还没到/这天没数据", 不做任何推算。
    """
    rows: list[tuple[str, float]] = []
    try:
        with engine.connect() as conn:
            rs = conn.execute(
                text(
                    "SELECT ts, close, source FROM klines "
                    "WHERE symbol = :s AND period = '1d' AND ts >= :d "
                    "ORDER BY ts ASC, source ASC LIMIT 200"
                ),
                {"s": symbol, "d": f"{start_day} 00:00:00+08"},
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 —— 表不存在(本地/早期库)也算取不到
        logger.debug("决策日志取K线失败 %s: %r", symbol, exc)
        return []
    seen: set[str] = set()
    for ts, close, _source in rs:
        if close is None:
            continue
        day = str(ts)[:10]
        if day in seen:
            continue
        seen.add(day)
        rows.append((day, float(close)))
    return rows


def _norm_day(day: Any) -> str:
    """日期归一到 ISO(YYYY-MM-DD) 仅供比较用。

    决策账本 `trade_date` 落库是紧凑格式(如 `20260925`), 而 `_close_series_pg`
    返回 ISO(`2026-09-25`) —— 两种字面量直接 `==` 永远不等(踩过: 回填 0 填)。
    比较一律走本函数归一; **不改写存储值**(字面量保真)。
    """
    s = str(day).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _trading_span(anchor_day: Any, value_day: Any) -> int | None:
    """信号日 → 取值日相隔的**交易日数**(含停牌; `1` = 次一个交易日)。取不到 → None(不猜)。

    这是「T+n = 信号日起第 n 根**可用** K 线」口径下, T+1 取值日(信号日之后第一根可用 K 线)
    离信号日有多远 —— 正常票 = 次一交易日(span=1); 长期停牌会把一个月后的收盘当 T+1(span 远大于 1)。
    判据见 `SUSPENDED_GAP_MAX_SPAN`。

    日期先经 `_norm_day` 归一(账本紧凑 `20260918` vs K 线 ISO `2026-09-18`, 直接比永远不等),
    再数严格开区间 `(anchor, value]` 内的交易日数(走 `src.core.trading_calendar` 交易日历,
    与 `market_phase.consecutive_boards` 的「按交易日历数交易日」同源)。日历未覆盖年份/归一失败
    → None(**不编造跨度**), 由调用方决定留空。
    """
    try:
        from src.core.trading_calendar import trading_days_between

        a = date.fromisoformat(_norm_day(anchor_day))
        v = date.fromisoformat(_norm_day(value_day))
    except Exception:  # noqa: BLE001 —— 归一失败/日历模块缺失: 不给跨度
        return None
    if v <= a:
        return None
    try:
        return int(trading_days_between(a, v, inclusive=True))
    except Exception as exc:  # noqa: BLE001 —— 日历未覆盖年份: 不猜(留空, 下次补表再算)
        logger.debug("决策账本交易日跨度不可得 %s→%s: %r", anchor_day, value_day, exc)
        return None


def backfill_outcomes(
    engine: Engine,
    *,
    limit: int = 500,
    scan_cap: int | None = None,
    series_provider: Any = None,
) -> dict[str, int]:
    """回填 T+1/3/5 收益与命中。

    **只填"未来那根 K 线已经存在"的部分**: 不足 T+n 的只填得出来的那几档,
    一档都填不出来就原样留着(下次再填) —— 不推算、不假设。
    命中定义: 收益 > 0 记 1, ≤0 记 0(**含 0 为未命中**, 不把平盘算成赚)。

    2026-10-14(回填覆盖修复) 改的是"看得见的行轮不到", **不是判定口径**(T+n 仍按信号日
    起第 n 根可用 K 线, 与既有已回填值同口径 —— 改口径会和存量混算):

    - **查看预算与填充预算解耦**(`scan_cap`, 缺省 `max(2*limit, DEFAULT_SCAN_CAP)`):
      一份候选里只要有永久不可填的行(信号日无 K 线: 休市/停牌), 老口径下它们每次都被重新
      选中并**占满 `LIMIT :lim` 窗口** → 后面的行永远轮不到(生产: 20260925 休市 597 行挡在
      最前, 2026-10-10 10:17 一跑 `scanned=500, filled=0`)。现在照 `scan_cap` 看、按
      `limit` 填, 不可填的行不再挤掉可填的行。
    - **不可填的行显式标注**: 信号日那根 K 线缺失(且其后确有 K 线) → 该行 `outcome_state`
      记 `no_anchor`(原因落 `outcome_note`), 收益列**保持 NULL(绝不填 0)**; 仍以**低优先**
      留在候选里(K 线后续补齐即自动重新评估并清空标注), 故不会永久掩盖真值。列缺失
      (未跑 v197)时降级: 不写标注, 但同样不填 0。

    幂等: 已填满的行不进候选(重跑不双计); 标注行重跑无副作用。

    2026-10-15(停牌跨期口径): 回填时**同步写 `t_n_span`**(信号日→T+1 取值日 = 信号日后第一根
    可用 K 线的交易日跨度, 走交易日历, 1=次一交易日)。跨度 > `SUSPENDED_GAP_MAX_SPAN` → 该行
    `outcome_state='suspended_gap'`(**覆盖原状态**, note 注明跨度), **收益列保持原值(绝不改写已填
    数值)** —— 只标注, 由胜率/分桶统计排除(见 `stats`/`query_log`)。存量已填行的跨度由
    `backfill_spans` 一次性幂等回填。列缺失(未跑 v198)时同样降级: 不写跨度/标注, 照常回填。

    Returns: `{scanned, filled, no_anchor, blocked, no_base}`
      - `scanned` 实际查看行数(不含已填满的); `filled` 实际回填行数;
      - `no_anchor` 本轮新标注"信号日无 K 线"的行数; `blocked` 有锚点但至今一档也算不出的行数;
      - `no_base` 当时没留价(`price_at_signal IS NULL`)的行数 —— 都拿不到就显式计数, 不猜。
    """
    filled = scanned = no_anchor = blocked = no_base = 0
    # 余量一倍: 候选头部若堆着不可填的行(空序列那种不下永久结论的), 也得留够额度够到可填的行。
    cap = int(scan_cap) if scan_cap else max(2 * int(limit), DEFAULT_SCAN_CAP)
    has_state = _has_outcome_state_column(engine)
    has_span = _has_t_n_span_column(engine)

    state_col = "outcome_state" if has_state else "NULL"
    # 已标注(永远算不出)的行排到最后: 仍会被看到, 但不许占用窗口头部、不许挤掉可填的行。
    order = (
        "CASE WHEN outcome_state IS NULL THEN 0 ELSE 1 END, trade_date ASC, id ASC"
        if has_state
        else "trade_date ASC, id ASC"
    )
    with engine.connect() as conn:
        pend = conn.execute(
            text(
                f"SELECT id, symbol, trade_date, price_at_signal, {state_col} "
                "FROM decision_log "
                "WHERE ret_t1 IS NULL OR ret_t3 IS NULL OR ret_t5 IS NULL "
                f"ORDER BY {order} LIMIT :cap"
            ),
            {"cap": cap},
        ).fetchall()

    for pid, symbol, day, base, state in pend:
        if filled >= int(limit):
            break
        scanned += 1
        if base is None:
            # 当时就没价 → 这里也补不出"当时的价", 只能等(不拿今天的价冒充)
            no_base += 1
            continue
        provider = series_provider or _close_series_pg
        series = provider(engine, symbol, str(day))
        # series[0] 必须是信号日当根; 否则说明该日无 K 线(停牌/非交易日) → 不硬填
        # (比较走 _norm_day 归一: 账本 trade_date 紧凑格式 vs 系列 ISO 格式, 直接比永远不等)
        anchor = _norm_day(day)
        if not series or series[0][0] != anchor:
            if not series:
                # 该标的从信号日起一根 K 线都没有 → 可能只是还没落库, 不下"永久不可填"的结论
                blocked += 1
            elif has_state and state is None:
                _mark_no_anchor(
                    engine, pid, f"信号日 {anchor} 无 K 线(休市/停牌), T+n 无法起算",
                )
                no_anchor += 1
            else:
                blocked += 1
            continue
        sets: dict[str, Any] = {}
        for n in HORIZONS:
            if len(series) > n:
                c = series[n][1]
                ret = (c - float(base)) / float(base)
                sets[f"ret_t{n}"] = round(ret, 6)
                sets[f"hit_t{n}"] = 1 if ret > 0 else 0
        if not sets:
            blocked += 1
            continue
        # T+1 取值日 = 信号日之后**第一根可用 K 线**(series[1]); 它与信号日的交易日跨度即
        # `t_n_span`。长期停牌会把一个月后的收盘当 T+1 → span 远超正常值 → 该行是停牌跨期样本。
        span: int | None = None
        if len(series) > 1:
            span = _trading_span(anchor, series[1][0])
        if has_span and span is not None:
            sets["t_n_span"] = span
        if has_state:
            # 锚点已就绪 → 清掉此前的缺数据标注(不留过期状态); 跨期样本则改标 suspended_gap。
            if span is not None and span > SUSPENDED_GAP_MAX_SPAN:
                sets["outcome_state"] = OUTCOME_SUSPENDED_GAP
                sets["outcome_note"] = (
                    f"信号日 {anchor} 后首个可用 K 线为 {_norm_day(series[1][0])}"
                    f"(相隔 {span} 个交易日 > {SUSPENDED_GAP_MAX_SPAN}), 疑长期停牌跨期; "
                    "收益列保留原值(不改写已填数值), 该行不计入胜率/分桶统计"
                )
            else:
                sets["outcome_state"] = None
                sets["outcome_note"] = None
        assign = ", ".join(f"{k} = :{k}" for k in sets)
        with engine.begin() as conn:
            conn.execute(
                text(f"UPDATE decision_log SET {assign}, filled_at = CURRENT_TIMESTAMP WHERE id = :pid"),
                {**sets, "pid": pid},
            )
        filled += 1

    return {
        "scanned": scanned,
        "filled": filled,
        "no_anchor": no_anchor,
        "blocked": blocked,
        "no_base": no_base,
    }


def backfill_spans(
    engine: Engine,
    *,
    limit: int = 20000,
    series_provider: Any = None,
) -> dict[str, int]:
    """存量已填行的 `t_n_span` **一次性幂等回填**(2026-10-15 停牌跨期口径标注)。

    背景: T+n 取「信号日起第 n 根**可用** K 线」, 长期停牌会把一个月后的收盘当 T+1(实证
    symbol=300082 信号日 20260918, 下一根可用 K 线是 2026-10-13)。v198 之前已填的行没有跨度
    记录 → 无从判断哪条是"停牌跨期样本"。用户裁决: **不重算存量数值, 只标注 + 排除统计** ——
    本函数**只补跨度列与标注, 绝不改写已填的收益/命中**:

    - 候选 = `t_n_span IS NULL` 且至少一档已填(`ret_t1/3/5` 有非空)的行;
    - 用与 `backfill_outcomes` **同一个 K 线序列口径**重取 `series[1]`(T+1 取值日), 算跨度写入;
    - 跨度 > `SUSPENDED_GAP_MAX_SPAN` → 标 `outcome_state='suspended_gap'`(覆盖原状态), note 注明跨度;
    - K 线取不到 / 信号日那根已不在 / 日历未覆盖 → 保持 `t_n_span` NULL(下次再扫, **不猜跨度**);
    - 列不存在(未跑 v198) → 空跑, 计数全 0, 不炸。

    幂等: 只碰 `t_n_span IS NULL` 的行(重跑对已填行无副作用)。**幂等 ⇒ 可一次性脚本跑, 也可
    挂日常作业顺带扫**; 本仓取独立脚本 `scripts/backfill_decision_spans.py`(见 CHANGELOG)。

    Returns: {scanned, filled, suspended_gap, skipped_no_column, no_series}
      - `no_series` = 重取 K 线失败 / 信号日那根已不在 / 日历未覆盖 → 留空待下次的行数。
    """
    if not _has_t_n_span_column(engine):
        return {"scanned": 0, "filled": 0, "suspended_gap": 0, "skipped_no_column": 1, "no_series": 0}
    has_state = _has_outcome_state_column(engine)
    with engine.connect() as conn:
        pend = conn.execute(
            text(
                "SELECT id, symbol, trade_date FROM decision_log "
                "WHERE t_n_span IS NULL "
                "AND (ret_t1 IS NOT NULL OR ret_t3 IS NOT NULL OR ret_t5 IS NOT NULL) "
                "ORDER BY trade_date ASC, id ASC LIMIT :lim"
            ),
            {"lim": int(limit)},
        ).fetchall()

    scanned = filled = suspended = no_series = 0
    for pid, symbol, day in pend:
        scanned += 1
        provider = series_provider or _close_series_pg
        series = provider(engine, symbol, str(day))
        anchor = _norm_day(day)
        # 信号日那根必须仍在(与 backfill_outcomes 同判定)且其后有取值日, 否则跨度不可靠 → 留空
        if not series or _norm_day(series[0][0]) != anchor or len(series) < 2:
            no_series += 1
            continue
        span = _trading_span(anchor, series[1][0])
        if span is None:
            no_series += 1
            continue
        sets: dict[str, Any] = {"t_n_span": span}
        if has_state and span > SUSPENDED_GAP_MAX_SPAN:
            sets["outcome_state"] = OUTCOME_SUSPENDED_GAP
            sets["outcome_note"] = (
                f"信号日 {anchor} 后首个可用 K 线为 {_norm_day(series[1][0])}"
                f"(相隔 {span} 个交易日 > {SUSPENDED_GAP_MAX_SPAN}), 疑长期停牌跨期; "
                "收益列保留原值(不改写已填数值), 该行不计入胜率/分桶统计"
            )
            suspended += 1
        assign = ", ".join(f"{k} = :{k}" for k in sets)
        with engine.begin() as conn:
            conn.execute(
                text(f"UPDATE decision_log SET {assign} WHERE id = :pid"),
                {**sets, "pid": pid},
            )
        filled += 1

    return {
        "scanned": scanned,
        "filled": filled,
        "suspended_gap": suspended,
        "skipped_no_column": 0,
        "no_series": no_series,
    }


#: 停牌跨期行(outcome_state='suspended_gap')在胜率聚合里的排除条件(2026-10-15)。
#: 排除加在**分子/分母**上而非 WHERE: `n_total` 仍报全量行数, 排除条数单列显式标注 ——
#: 读者既看得到全样本、也知道胜率剔了几条。`COALESCE` 兼容 NULL 状态(正常/未定论行)。
_SUSPENDED_GAP_EXCL = "COALESCE(outcome_state, '') <> 'suspended_gap'"


def _hit_aggs_sql(has_state: bool) -> str:
    """拼 n_t1/n_t3/n_t5(分母)与 w_t1/w_t3/w_t5(命中)六个聚合表达式。

    有 `outcome_state` 列 → 前缀排除条件, **停牌跨期行不计入命中率**;
    列缺失(未跑 v197 的老库)→ 与旧口径逐字一致(向后兼容零破坏)。
    """
    excl = f"{_SUSPENDED_GAP_EXCL} AND " if has_state else ""
    out = []
    for label, col in (("t1", "hit_t1"), ("t3", "hit_t3"), ("t5", "hit_t5")):
        out.append(f"SUM(CASE WHEN {excl}{col} IS NOT NULL THEN 1 ELSE 0 END) AS n_{label}")
    for label, col in (("t1", "hit_t1"), ("t3", "hit_t3"), ("t5", "hit_t5")):
        out.append(f"SUM(CASE WHEN {excl}{col} = 1 THEN 1 ELSE 0 END) AS w_{label}")
    return ",\n       ".join(out)


def _horizon_block(n: Any, n1: Any, n3: Any, n5: Any, w1: Any, w3: Any, w5: Any,
                   min_sample: int) -> dict[str, dict[str, Any]]:
    """把一行聚合计数 → {t1/t3/t5: {n, hit_rate, insufficient, note}}(样本不足不给数字)。"""
    out: dict[str, dict[str, Any]] = {}
    for label, nn, ww in (("t1", n1, w1), ("t3", n3, w3), ("t5", n5, w5)):
        have = int(nn or 0)
        if have == 0:
            out[label] = {
                "n": 0, "hit_rate": None, "insufficient": True,
                "note": "尚无已回填样本(需要未来的 K 线才算得出来)",
            }
        elif have < min_sample:
            out[label] = {
                "n": have, "hit_rate": None, "insufficient": True,
                "note": f"样本不足({have} < {min_sample}), 不给命中率",
            }
        else:
            out[label] = {
                "n": have, "hit_rate": round(int(ww or 0) / have, 4),
                "insufficient": False, "note": "",
            }
    return out


def _regime_rows(engine: Engine, since: str, min_sample: int) -> list[dict[str, Any]]:
    """按 (signal_kind, regime) 分桶统计命中率(2026-10-10 A)。列缺失/异常 → []。

    只加聚合维度: 不建回测 UI, 不换算命中定义。regime NULL 归入 'unknown'。
    停牌跨期行(outcome_state='suspended_gap')不计入命中率分子/分母(2026-10-15)。
    """
    has_state = _has_outcome_state_column(engine)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
SELECT signal_kind, COALESCE(NULLIF(regime, ''), 'unknown') AS reg,
       COUNT(*) AS n,
       {_hit_aggs_sql(has_state)}
FROM decision_log
WHERE (length(trade_date) = 8 AND trade_date >= :since_c)
   OR (length(trade_date) = 10 AND trade_date >= :since)
GROUP BY signal_kind, reg
ORDER BY signal_kind, n DESC
"""
                ),
                {"since": since, "since_c": since.replace("-", "")},
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 —— regime 列缺失(老库) → 无分桶
        logger.debug("decision stats regime 分桶不可用: %r", exc)
        return []
    out: list[dict[str, Any]] = []
    for kind, reg, n, n1, n3, n5, w1, w3, w5 in rows:
        out.append({
            "signal_kind": kind,
            "regime": reg,
            "n_total": int(n or 0),
            "horizons": _horizon_block(n, n1, n3, n5, w1, w3, w5, min_sample),
        })
    return out


def _outcome_state_counts(engine: Engine, since: str, state: str) -> dict[str, int]:
    """按 signal_kind 统计窗口内指定 `outcome_state` 的行数(`{}` = 列缺失/查询失败, 不假装有数)。

    通用口径: 缺 K 锚点(`no_anchor`)与停牌跨期(`suspended_gap`)都靠它报出「胜率到底剔了几条」。
    """
    if not _has_outcome_state_column(engine):
        return {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT signal_kind, COUNT(*) FROM decision_log "
                    "WHERE outcome_state = :st "
                    "AND ((length(trade_date) = 8 AND trade_date >= :since_c) "
                    "     OR (length(trade_date) = 10 AND trade_date >= :since)) "
                    "GROUP BY signal_kind"
                ),
                {"st": state, "since": since, "since_c": since.replace("-", "")},
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 —— 列缺失/方言异常 → 无此口径
        logger.debug("decision stats 状态计数不可用 %s: %r", state, exc)
        return {}
    return {str(k): int(v or 0) for k, v in rows}


def _no_anchor_counts(engine: Engine, since: str) -> dict[str, int]:
    """按 signal_kind 统计"信号日无 K 线 → T+n 算不出来"的行数(2026-10-14 显式标注)。

    这些行**不进命中分母**(hit_* 为 NULL), 但此前与"还没到 T+n"不可区分, 看板上只能写
    "无法证实也无法证伪"。现在单独报出来, 让读的人知道分母为什么小。
    列缺失(未跑 v197)/查询失败 → `{}`(不假装有数据)。
    """
    return _outcome_state_counts(engine, since, OUTCOME_NO_ANCHOR)


def _suspended_gap_counts(engine: Engine, since: str) -> dict[str, int]:
    """按 signal_kind 统计停牌跨期行(`outcome_state='suspended_gap'`, 2026-10-15)。

    这些行**已被排除在胜率/分桶统计之外**(span>5, 收益列是"一个月后的跌幅"而非真 T+1),
    单列报出以便看板写"含 N 条停牌跨期样本已排除"。列缺失/失败 → `{}`(不假装有数)。
    """
    return _outcome_state_counts(engine, since, OUTCOME_SUSPENDED_GAP)


def stats(engine: Engine, *, days: int = 180, min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    """按信号类型统计命中率。

    `n < min_sample` → 该档返回 `insufficient: True` 且**命中率为 None**(页面显示"样本不足"),
    不给数字, 免得 3 个样本算出 67% 去指导决策。

    2026-10-10 A: 额外返回 `regime_rows`(按 signal_kind × regime 分桶), 供验证
    "情绪周期条件化是否真提胜率"; 老库无 regime 列时 `regime_rows=[]`(向后兼容)。
    """
    since = (date.fromisoformat(_today_cst()) - timedelta(days=max(7, int(days)))).isoformat()
    has_state = _has_outcome_state_column(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
SELECT signal_kind,
       COUNT(*) AS n,
       {_hit_aggs_sql(has_state)}
FROM decision_log
WHERE (length(trade_date) = 8 AND trade_date >= :since_c)
   OR (length(trade_date) = 10 AND trade_date >= :since)
GROUP BY signal_kind
ORDER BY n DESC
"""
            ),
            {"since": since, "since_c": since.replace("-", "")},
        ).fetchall()

    out: list[dict[str, Any]] = []
    for kind, n, n1, n3, n5, w1, w3, w5 in rows:
        out.append({
            "signal_kind": kind,
            "n_total": int(n or 0),
            "horizons": _horizon_block(n, n1, n3, n5, w1, w3, w5, min_sample),
        })

    gap_by_kind = _suspended_gap_counts(engine, since)
    gap_total = int(sum(gap_by_kind.values()))
    return {
        "since": since,
        "min_sample": int(min_sample),
        "rows": out,
        "regime_rows": _regime_rows(engine, since, min_sample),
        # 缺失显式标注(2026-10-14): 信号日无 K 线的存量行单列出来, 命中分母为什么小有据可查。
        "no_data": {
            "state": OUTCOME_NO_ANCHOR,
            "rows_by_kind": _no_anchor_counts(engine, since),
            # 停牌跨期显式标注(2026-10-15): 收益列已填但跨期(span>5)的行, 已从命中率剔除。
            "suspended_gap": {
                "state": OUTCOME_SUSPENDED_GAP,
                "max_span": SUSPENDED_GAP_MAX_SPAN,
                "rows_by_kind": gap_by_kind,
                "total": gap_total,
                "note": (
                    f"含 {gap_total} 条停牌跨期样本已排除 —— outcome_state='{OUTCOME_SUSPENDED_GAP}'"
                    f"(T+1 取值日距信号日 > {SUSPENDED_GAP_MAX_SPAN} 个交易日, 疑长期停牌); "
                    "收益列保留原值, 但不计入胜率/分桶统计(不是删数据, 是不混算)。"
                ),
            },
            "note": (
                "outcome_state='no_anchor' = 信号日无 K 线(休市/停牌) → T+n 永远算不出来; "
                "显式标注、收益列留 NULL(不是 0% 命中), 故不进命中分母。"
            ),
        },
        "note": (
            "命中 = T+n 收益 > 0(平盘记未命中); 缺失/未回填一律不计入分母, 不用推算值填充。"
            " regime_rows 为按情绪周期分桶(regime NULL 归 unknown)。"
            f" 含 {gap_total} 条停牌跨期样本已排除(outcome_state='{OUTCOME_SUSPENDED_GAP}',"
            f" span>{SUSPENDED_GAP_MAX_SPAN})。"
        ),
    }


def query_log(
    engine: Engine,
    *,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """信号明细分页查询(账本明细, 审计 P2-4)。

    纯只读透传: 缺价/未回填一律 NULL(不补 0), 不在 API 层推算。
    - `limit` 钳到 [1, 500]; `offset` 负数按 0 处理(越界只是返回空页, 不报错);
    - `start_date`/`end_date` 支持 ISO(`2026-09-25`)与紧凑(`20260925`), 比较前统一归一为
      紧凑格式(存储即紧凑), 比较用字面量、**不改写存储值**;
    - 返回 `total`(过滤后总行数)与 `has_more`, 供前端翻页; 越界 `offset` → items 空、has_more=False。
    """
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    where: list[str] = []
    params: dict[str, Any] = {"lim": lim, "off": off}

    if kind:
        where.append("signal_kind = :kind")
        params["kind"] = kind
    s_date = _norm_compact_day(start_date)
    e_date = _norm_compact_day(end_date)
    if s_date:
        where.append("trade_date >= :start")
        params["start"] = s_date
    if e_date:
        where.append("trade_date <= :end")
        params["end"] = e_date
    cond = ("WHERE " + " AND ".join(where) + " ") if where else ""

    with engine.connect() as conn:
        total = int(
            conn.execute(text(f"SELECT COUNT(*) FROM decision_log {cond}"), params).scalar() or 0
        )
        has_reg = _has_regime_column(engine)
        reg_col = "regime, " if has_reg else ""
        has_state = _has_outcome_state_column(engine)
        has_span = _has_t_n_span_column(engine)
        # 追加列按存在性动态拼接(v197 outcome_state/outcome_note, v198 t_n_span)
        extra_cols: list[str] = []
        if has_state:
            extra_cols += ["outcome_state", "outcome_note"]
        if has_span:
            extra_cols += ["t_n_span"]
        extra_sql = (", " + ", ".join(extra_cols)) if extra_cols else ""
        rows = conn.execute(
            text(
                "SELECT signal_kind, symbol, trade_date, price_at_signal, context_json, source, "
                f"{reg_col}"
                "ret_t1, hit_t1, ret_t3, hit_t3, ret_t5, hit_t5, filled_at"
                f"{extra_sql} "
                f"FROM decision_log {cond}ORDER BY trade_date DESC, id DESC LIMIT :lim OFFSET :off"
            ),
            params,
        ).fetchall()

        # 停牌跨期标注(2026-10-15): 明细**照常列出**(审计用, 收益列是已填原值, 只标注不改写);
        # 但报出本过滤集里有几条被排除在胜率/分桶统计之外, 读者知道数从哪来(N 从库里数, 不编造)。
        gap_excluded = 0
        if has_state:
            gap_excluded = int(
                conn.execute(
                    text(
                        f"SELECT COUNT(*) FROM decision_log {cond}"
                        + ("AND " if cond else "WHERE ")
                        + "outcome_state = :st"
                    ),
                    {**params, "st": OUTCOME_SUSPENDED_GAP},
                ).scalar()
                or 0
            )

    off_i = 1 if has_reg else 0
    # 追加列的物理下标(按上面拼接顺序推导)
    idx = 13 + off_i
    st_i = nt_i = span_i = None
    if has_state:
        st_i, idx = idx, idx + 1
        nt_i, idx = idx, idx + 1
    if has_span:
        span_i, idx = idx, idx + 1
    items = [
        {
            "signal_kind": r[0],
            "symbol": r[1],
            "trade_date": r[2],
            "price_at_signal": None if r[3] is None else float(r[3]),
            "context": r[4] or "",
            "source": r[5] or "",
            "regime": (r[6] if has_reg else None),
            "outcomes": {
                label: {
                    "ret": None if r[i + off_i] is None else float(r[i + off_i]),
                    "hit": None if r[i + 1 + off_i] is None else bool(r[i + 1 + off_i]),
                }
                for label, i in (("t1", 6), ("t3", 8), ("t5", 10))
            },
            "filled_at": None if r[12 + off_i] is None else str(r[12 + off_i]),
            # 缺失显式标注(2026-10-14): 'no_anchor' = 信号日无 K 线, T+n 算不出来(收益列留 NULL);
            # None = 未到期/部分已填/已回填 —— 前端据此把"没数据"与"还没到"分开显示。
            "outcome_state": (r[st_i] if st_i is not None else None),
            "outcome_note": (r[nt_i] if nt_i is not None else None),
            # 停牌跨期跨度(2026-10-15): 信号日→T+1 取值日相隔交易日数(1=次一交易日); NULL = 未算。
            "t_n_span": (r[span_i] if span_i is not None else None),
        }
        for r in rows
    ]
    # 代码 → 名字(2026-10-21): 一次批量取(不逐行查), 前端才有中文名可显示。
    # 取不到 → name=None(前端显示 --); **绝不拿 symbol 冒充名字、也不回空串** ——
    # 空串会被前端当成「名字是空字符串」, 而 None 才是「名字未知」。
    if items:
        try:
            _names = names_for([str(it["symbol"]) for it in items])
        except Exception as e:  # noqa: BLE001 —— 名字只是展示增强, 取不到不能连累账本查询
            logger.warning("账本明细名称补全失败(不影响明细查询): %s", e)
            _names = {}
        for it in items:
            it["name"] = _names.get(str(it["symbol"]))
    return {
        "count": len(items),
        "total": total,
        "offset": off,
        "limit": lim,
        "has_more": (off + len(items)) < total,
        "items": items,
        # 停牌跨期排除条数显式标注(N 从库里数, 不编造); 明细本身不删行(审计), 但胜率/分桶统计已剔除。
        "suspended_gap_excluded": gap_excluded,
        "note": (
            "未回填的档位为 null(需要未来的 K 线才算得出来), 不用推算值填充; 命中 = 收益 > 0"
            "(平盘算未命中)。outcome_state='no_anchor' 表示信号日无 K 线(休市/停牌), 该行 T+n "
            "永远算不出来 —— 显式标注, 不填 0。"
            f" outcome_state='{OUTCOME_SUSPENDED_GAP}' 表示 T+1 取值日距信号日 > {SUSPENDED_GAP_MAX_SPAN} "
            f"个交易日(疑长期停牌跨期): 收益列保留原值, 但该行**不计入胜率/分桶统计** —— "
            f"本过滤集含 {gap_excluded} 条停牌跨期样本已排除(t_n_span 为该行跨度)。"
        ),
    }


def _norm_compact_day(day: str | None) -> str:
    """ISO/紧凑日期 → 紧凑 `YYYYMMDD`(供与库中 trade_date 字面量比较)。空/非法 → 空串。"""
    s = str(day or "").strip()
    if not s:
        return ""
    digits = s.replace("-", "").replace("/", "")
    return digits if (len(digits) == 8 and digits.isdigit()) else ""


# ── 回填调度(P0-1 审计修复, 2026-10-10) ─────────────────────────────────────
# 背景: backfill_outcomes 此前全仓无生产调度(仅测试调用, 生产引用只剩一条注释) →
# DecisionLedger 的 ret_t1/hit_t1 列**从不被回填**, 命中率恒显『样本不足』, 反馈环整段死。
# 修法: 交易日 18:35 cron + POST /api/decisions/backfill 手动触发, 统一走作业框架
# (单飞复用 + 进度/结果落库; ok=False 显式判失败 —— v0.13.49 诚实性约定)。
# 纪律: 永不抛异常; 非交易日跳过; 幂等可重跑(backfill_outcomes 只填『未来 K 线已存在』的档)。


def backfill_runner(
    job_id: str,
    *,
    engine: Engine | None = None,
    limit: int = 500,
    series_provider: Any = None,
    scan_cap: int | None = None,
) -> None:
    """作业体: 跑一次回填并把终态落库。**永不抛异常**(后台线程里抛出只丢日志, 无意义)。

    失败显式: `backfill_outcomes` 抛异常 → `jobs.fail`; 返回体显式 `ok=False` →
    `jobs.finish` 按诚实性约定判 failed(原因落 message/error)。正常计数体(无 `ok` 键)按成功处理。
    作业表写失败(极早的库)也不能拖垮回填本身 —— 只记 warning。
    """
    from src.db.session import get_write_engine

    eng = engine if engine is not None else get_write_engine()
    try:
        try:
            jobs.start(job_id, "backfilling")
        except Exception as exc:  # noqa: BLE001
            logger.warning("决策账本回填: 作业启动落库失败(继续跑回填): %r", exc)
        out = backfill_outcomes(eng, limit=limit, series_provider=series_provider, scan_cap=scan_cap)
    except Exception as exc:  # noqa: BLE001 —— 显式失败, 不抛
        logger.warning("决策账本回填失败: %r", exc)
        try:
            jobs.fail(job_id, str(exc))
        except Exception as exc2:  # noqa: BLE001
            logger.warning("决策账本回填: 失败态落库失败: %r", exc2)
        return
    try:
        if not jobs.finish(job_id, out, context="决策账本回填: "):
            logger.warning("决策账本回填未成: %s", out)
    except Exception as exc:  # noqa: BLE001
        logger.warning("决策账本回填: 终态落库失败: %r", exc)


def spawn_backfill(
    *,
    limit: int = 500,
    series_provider: Any = None,
    reason: str = "manual",
    scan_cap: int | None = None,
) -> dict[str, Any]:
    """单飞起一次回填作业(后台线程执行, 立即返回 job_id)。

    作业框架保证: 同类活跃作业已存在时**复用其 job_id**(`started=False`) —— 定时与手动
    撞车、重复点击都不起第二个(并发回填会互踩同一批 pending 行)。
    """
    import threading

    job_id, is_new = jobs.create(BACKFILL_JOB_KIND, "决策账本回填")
    if not is_new:
        return {"started": False, "running": True, "reason": "回填进行中", "job_id": job_id}
    threading.Thread(
        target=backfill_runner,
        kwargs={"job_id": job_id, "limit": limit, "series_provider": series_provider,
                "scan_cap": scan_cap},
        name=f"decision-backfill-{reason}",
        daemon=True,
    ).start()
    return {"started": True, "running": True, "reason": None, "job_id": job_id}


def backfill_daily_job(
    *,
    limit: int = BACKFILL_DAILY_LIMIT,
    scan_cap: int | None = None,
) -> dict[str, Any]:
    """调度器入口(交易日 18:35): **非交易日跳过**, 否则单飞起回填作业。

    与 signal-nightly-review(18:30) 错开 5 分钟: 那时当日日线已落库, T+1/3/5 才有 K 线可对。

    2026-10-14: 单跑目标从 500 提到 `BACKFILL_DAILY_LIMIT`(5000) —— 500/天 追不平 3000+ 积压,
    且旧口径下不可填的行会占满窗口(见 `backfill_outcomes`)。
    """
    from datetime import date

    try:
        from src.core.trading_calendar import is_trading_day

        if not is_trading_day(date.today()):
            return {"ok": True, "skipped": True, "reason": "非交易日跳过"}
    except Exception as exc:  # noqa: BLE001 —— 日历缺失/未覆盖 → 显式失败, 禁止回落推测
        logger.warning("决策账本回填: 交易日历不可用, 跳过: %r", exc)
        return {"ok": False, "reason": f"交易日历不可用: {exc!r}"[:200]}
    return spawn_backfill(limit=limit, reason="scheduled", scan_cap=scan_cap)


#: regime 历史回填作业 kind(与 outcomes 回填分开单飞)
REGIME_BACKFILL_JOB_KIND = "decision_regime_backfill"


def backfill_regime(
    engine: Engine,
    *,
    limit: int = 20000,
    day_resolver: Any = None,
) -> dict[str, int]:
    """给 `regime` 仍为 NULL/'' 的存量行**按其信号日**回填情绪周期态(幂等可重跑)。

    历史回填(2026-10-11): 存量行早于 regime 落库改动 → 全 NULL → regime 分桶退化为单桶。
    本函数按每行 `trade_date`(信号日)从当日情绪周期快照推 regime(口径见
    `src.core.market_regime.resolve_regime_for_day` —— 与 current_regime 同源同映射, 不造第二套)。

    纪律:
    - **只填解析出明确态的行**; 解析为 unknown 的行**保持 NULL**(留给未来数据补齐, 不写
      'unknown' 冒充已归集) —— 故幂等: 重跑对已填行与不可解析行都不改；
    - 日期比较一律归一(账本紧凑 20260918 vs 快照 ISO 2026-09-18, 直接 == 永远不等); **不改写存储值**；
    - regime 列不存在(老库) → 空跑, 计数全 0, 不炸。

    Returns: {scanned, filled, unresolved, skipped_no_column}
    """
    if not _has_regime_column(engine):
        return {"scanned": 0, "filled": 0, "unresolved": 0, "skipped_no_column": 1}
    with engine.begin() as conn:
        pend = conn.execute(
            text(
                "SELECT id, trade_date FROM decision_log "
                "WHERE regime IS NULL OR regime = '' "
                "ORDER BY trade_date ASC LIMIT :lim"
            ),
            {"lim": int(limit)},
        ).fetchall()

    if day_resolver is None:
        from src.core.market_regime import regime_for_day_key

        def _default_resolver(_engine: Engine, _day: Any) -> str:
            return regime_for_day_key(_engine, _day)

        day_resolver = _default_resolver

    scanned = filled = unresolved = 0
    for pid, day in pend:
        scanned += 1
        try:
            reg = _norm_regime(day_resolver(engine, day))
        except Exception as exc:  # noqa: BLE001 —— 单行解析失败不拖垮整批
            logger.warning("regime 回填: 信号日 %s 解析失败(跳过): %r", day, exc)
            unresolved += 1
            continue
        if not reg or reg == "unknown":
            # 当日无快照 → 显式缺数据, 保持 NULL(不写 unknown 冒充), 下次数据补齐再填
            unresolved += 1
            continue
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE decision_log SET regime = :r WHERE id = :pid"),
                {"r": reg, "pid": pid},
            )
        filled += 1

    return {"scanned": scanned, "filled": filled, "unresolved": unresolved, "skipped_no_column": 0}


def regime_backfill_runner(
    job_id: str,
    *,
    engine: Engine | None = None,
    limit: int = 20000,
    day_resolver: Any = None,
) -> None:
    """作业体: 跑一次 regime 回填并把终态落库。**永不抛异常**(与 backfill_runner 同风格)。"""
    from src.db.session import get_write_engine

    eng = engine if engine is not None else get_write_engine()
    try:
        try:
            jobs.start(job_id, "backfilling")
        except Exception as exc:  # noqa: BLE001
            logger.warning("regime 回填: 作业启动落库失败(继续跑回填): %r", exc)
        out = backfill_regime(eng, limit=limit, day_resolver=day_resolver)
    except Exception as exc:  # noqa: BLE001 —— 显式失败, 不抛
        logger.warning("regime 回填失败: %r", exc)
        try:
            jobs.fail(job_id, str(exc))
        except Exception as exc2:  # noqa: BLE001
            logger.warning("regime 回填: 失败态落库失败: %r", exc2)
        return
    try:
        if not jobs.finish(job_id, out, context="regime 历史回填: "):
            logger.warning("regime 回填未成: %s", out)
    except Exception as exc:  # noqa: BLE001
        logger.warning("regime 回填: 终态落库失败: %r", exc)


def spawn_regime_backfill(
    *,
    limit: int = 20000,
    day_resolver: Any = None,
    reason: str = "manual",
) -> dict[str, Any]:
    """单飞起一次 regime 回填作业(后台线程, 立即返回 job_id)。并发撞车复用同 job。"""
    import threading

    job_id, is_new = jobs.create(REGIME_BACKFILL_JOB_KIND, "regime 历史回填")
    if not is_new:
        return {"started": False, "running": True, "reason": "回填进行中", "job_id": job_id}
    threading.Thread(
        target=regime_backfill_runner,
        kwargs={"job_id": job_id, "limit": limit, "day_resolver": day_resolver},
        name=f"decision-regime-backfill-{reason}",
        daemon=True,
    ).start()
    return {"started": True, "running": True, "reason": None, "job_id": job_id}


def regime_backfill_daily_job(*, limit: int = 20000) -> dict[str, Any]:
    """调度器入口(交易日 18:36): **非交易日跳过**, 否则单飞起 regime 回填作业。

    与 outcomes 回填(18:35)错开 1 分钟: regime 是"按信号日"解析, 不依赖当日 K 线, 但避开
    同一分钟的资源竞争。非交易日命中在任务内再拦一道(工作日专 cron + 交易日守卫双保险)。
    """
    from datetime import date

    try:
        from src.core.trading_calendar import is_trading_day

        if not is_trading_day(date.today()):
            return {"ok": True, "skipped": True, "reason": "非交易日跳过"}
    except Exception as exc:  # noqa: BLE001 —— 日历缺失/未覆盖 → 显式失败, 禁止回落推测
        logger.warning("regime 回填: 交易日历不可用, 跳过: %r", exc)
        return {"ok": False, "reason": f"交易日历不可用: {exc!r}"[:200]}
    return spawn_regime_backfill(limit=limit, reason="scheduled")


def register_cron(scheduler) -> bool:
    """把决策账本回填 job 注册到传入的现有 APScheduler(**禁止新开 scheduler**)。

    注册两个同风格作业:
      - `decision-backfill-daily`(18:35): T+1/3/5 结果回填(需当日 K 线);
      - `decision-regime-backfill-daily`(18:36): 存量行 regime 历史回填(按信号日)。
    在 lifespan 的调度器选主分支里调用; 传入 None / 无 add_job → 返回 False, 不崩。
    非交易日命中在任务内再拦一道(工作日专 cron + 交易日守卫双保险)。
    """
    if scheduler is None or not hasattr(scheduler, "add_job"):
        return False
    ok = True
    try:
        scheduler.add_job(
            backfill_daily_job,
            "cron",
            day_of_week="mon-fri",
            hour=18,
            minute=35,
            id="decision-backfill-daily",
            name="决策账本回填",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        logger.info("决策账本回填 job 已注册: 交易日 18:35")
    except Exception as exc:  # noqa: BLE001
        logger.error("决策账本回填 job 注册失败: %r", exc)
        ok = False
    try:
        scheduler.add_job(
            regime_backfill_daily_job,
            "cron",
            day_of_week="mon-fri",
            hour=18,
            minute=36,
            id="decision-regime-backfill-daily",
            name="regime 历史回填",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        logger.info("regime 历史回填 job 已注册: 交易日 18:36")
    except Exception as exc:  # noqa: BLE001
        logger.error("regime 历史回填 job 注册失败: %r", exc)
        ok = False
    return ok
