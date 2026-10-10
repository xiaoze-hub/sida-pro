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

logger = logging.getLogger(__name__)

#: 回填作业在作业框架里的 kind(单飞复用与作业面板都按它归类)
BACKFILL_JOB_KIND = "decision_backfill"

_CST = ZoneInfo("Asia/Shanghai")

#: 命中率最小样本量 —— 低于它不给数字(避免小样本误导)
MIN_SAMPLE = 30

#: 回填要求的未来交易日数(与 stats 的 horizon 对齐)
HORIZONS = (1, 3, 5)


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


def backfill_outcomes(
    engine: Engine,
    *,
    limit: int = 500,
    series_provider: Any = None,
) -> dict[str, int]:
    """回填 T+1/3/5 收益与命中。

    **只填"未来那根 K 线已经存在"的部分**: 不足 T+n 的只填得出来的那几档,
    一档都填不出来就原样留着(下次再填) —— 不推算、不假设。
    命中定义: 收益 > 0 记 1, ≤0 记 0(**含 0 为未命中**, 不把平盘算成赚)。
    """
    filled = 0
    scanned = 0
    with engine.begin() as conn:
        pend = conn.execute(
            text(
                "SELECT id, symbol, trade_date, price_at_signal FROM decision_log "
                "WHERE ret_t5 IS NULL OR ret_t3 IS NULL OR ret_t1 IS NULL "
                "ORDER BY trade_date ASC LIMIT :lim"
            ),
            {"lim": int(limit)},
        ).fetchall()

    for pid, symbol, day, base in pend:
        scanned += 1
        if base is None:
            # 当时就没价 → 这里也补不出"当时的价", 只能等(不拿今天的价冒充)
            continue
        provider = series_provider or _close_series_pg
        series = provider(engine, symbol, str(day))
        # series[0] 必须是信号日当根; 否则说明该日无 K 线(停牌/非交易日) → 不硬填
        # (比较走 _norm_day 归一: 账本 trade_date 紧凑格式 vs 系列 ISO 格式, 直接比永远不等)
        if not series or series[0][0] != _norm_day(day):
            continue
        sets: dict[str, Any] = {}
        for n in HORIZONS:
            if len(series) > n:
                c = series[n][1]
                ret = (c - float(base)) / float(base)
                sets[f"ret_t{n}"] = round(ret, 6)
                sets[f"hit_t{n}"] = 1 if ret > 0 else 0
        if not sets:
            continue
        assign = ", ".join(f"{k} = :{k}" for k in sets)
        with engine.begin() as conn:
            conn.execute(
                text(f"UPDATE decision_log SET {assign}, filled_at = CURRENT_TIMESTAMP WHERE id = :pid"),
                {**sets, "pid": pid},
            )
        filled += 1

    return {"scanned": scanned, "filled": filled}


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
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
SELECT signal_kind, COALESCE(NULLIF(regime, ''), 'unknown') AS reg,
       COUNT(*) AS n,
       COUNT(hit_t1) AS n_t1, COUNT(hit_t3) AS n_t3, COUNT(hit_t5) AS n_t5,
       SUM(CASE WHEN hit_t1 = 1 THEN 1 ELSE 0 END) AS w_t1,
       SUM(CASE WHEN hit_t3 = 1 THEN 1 ELSE 0 END) AS w_t3,
       SUM(CASE WHEN hit_t5 = 1 THEN 1 ELSE 0 END) AS w_t5
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


def stats(engine: Engine, *, days: int = 180, min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    """按信号类型统计命中率。

    `n < min_sample` → 该档返回 `insufficient: True` 且**命中率为 None**(页面显示"样本不足"),
    不给数字, 免得 3 个样本算出 67% 去指导决策。

    2026-10-10 A: 额外返回 `regime_rows`(按 signal_kind × regime 分桶), 供验证
    "情绪周期条件化是否真提胜率"; 老库无 regime 列时 `regime_rows=[]`(向后兼容)。
    """
    since = (date.fromisoformat(_today_cst()) - timedelta(days=max(7, int(days)))).isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
SELECT signal_kind,
       COUNT(*) AS n,
       SUM(CASE WHEN hit_t1 IS NOT NULL THEN 1 ELSE 0 END) AS n_t1,
       SUM(CASE WHEN hit_t3 IS NOT NULL THEN 1 ELSE 0 END) AS n_t3,
       SUM(CASE WHEN hit_t5 IS NOT NULL THEN 1 ELSE 0 END) AS n_t5,
       SUM(CASE WHEN hit_t1 = 1 THEN 1 ELSE 0 END) AS w_t1,
       SUM(CASE WHEN hit_t3 = 1 THEN 1 ELSE 0 END) AS w_t3,
       SUM(CASE WHEN hit_t5 = 1 THEN 1 ELSE 0 END) AS w_t5
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

    return {
        "since": since,
        "min_sample": int(min_sample),
        "rows": out,
        "regime_rows": _regime_rows(engine, since, min_sample),
        "note": (
            "命中 = T+n 收益 > 0(平盘记未命中); 缺失/未回填一律不计入分母, 不用推算值填充。"
            " regime_rows 为按情绪周期分桶(regime NULL 归 unknown)。"
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
        rows = conn.execute(
            text(
                "SELECT signal_kind, symbol, trade_date, price_at_signal, context_json, source, "
                f"{reg_col}"
                "ret_t1, hit_t1, ret_t3, hit_t3, ret_t5, hit_t5, filled_at "
                f"FROM decision_log {cond}ORDER BY trade_date DESC, id DESC LIMIT :lim OFFSET :off"
            ),
            params,
        ).fetchall()

    off_i = 1 if has_reg else 0
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
        }
        for r in rows
    ]
    return {
        "count": len(items),
        "total": total,
        "offset": off,
        "limit": lim,
        "has_more": (off + len(items)) < total,
        "items": items,
        "note": "未回填的档位为 null(需要未来的 K 线才算得出来), 不用推算值填充; 命中 = 收益 > 0(平盘算未命中)。",
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
        out = backfill_outcomes(eng, limit=limit, series_provider=series_provider)
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
        kwargs={"job_id": job_id, "limit": limit, "series_provider": series_provider},
        name=f"decision-backfill-{reason}",
        daemon=True,
    ).start()
    return {"started": True, "running": True, "reason": None, "job_id": job_id}


def backfill_daily_job(*, limit: int = 500) -> dict[str, Any]:
    """调度器入口(交易日 18:35): **非交易日跳过**, 否则单飞起回填作业。

    与 signal-nightly-review(18:30) 错开 5 分钟: 那时当日日线已落库, T+1/3/5 才有 K 线可对。
    """
    from datetime import date

    try:
        from src.core.trading_calendar import is_trading_day

        if not is_trading_day(date.today()):
            return {"ok": True, "skipped": True, "reason": "非交易日跳过"}
    except Exception as exc:  # noqa: BLE001 —— 日历缺失/未覆盖 → 显式失败, 禁止回落推测
        logger.warning("决策账本回填: 交易日历不可用, 跳过: %r", exc)
        return {"ok": False, "reason": f"交易日历不可用: {exc!r}"[:200]}
    return spawn_backfill(limit=limit, reason="scheduled")


def register_cron(scheduler) -> bool:
    """把决策账本回填 job 注册到传入的现有 APScheduler(**禁止新开 scheduler**)。

    在 lifespan 的调度器选主分支里调用; 传入 None / 无 add_job → 返回 False, 不崩。
    非交易日命中在任务内再拦一道(工作日专 cron + 交易日守卫双保险)。
    """
    if scheduler is None or not hasattr(scheduler, "add_job"):
        return False
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
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("决策账本回填 job 注册失败: %r", exc)
        return False
