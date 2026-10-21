"""标的画像判定器(2026-10-21)。

为什么需要它: 62 个 chat 工具里只有 27 个进了配方, 妖股因子/威科夫等从未进入分析
路径 —— 一只五连板妖股也被"趋势+估值"的尺子去量, 输出"跌破支撑位"这种废话。
本模块先回答"这是什么票", 上层再按画像挑配方与必答维度。

口径(全部确定性规则, 不调 LLM, 不联网):
1. demon  情绪妖股 —— demon_factors 六维总分/grade/连板身位 (grade S 或 A, 或总分 ≥60)
2. left   左侧超跌 —— 距近 250 日最高价回撤 ≥35% 且 现价在 MA60 下方
3. leader 题材龙头 —— 该股是主线快照(mainline_daily_snapshot)前 N 名题材的龙头/成分
4. trend  趋势白马 —— 站上 MA60 且 MA60 近 5 日斜率 >0
5. normal 常规个股 —— 以上都不中

铁律: 缺数据不编造 —— 取不到就跳过该判据(并在 reasons 写明原因), 绝不填 0、绝不猜。
单次 detect() 只读 3 次单行/单表查询(妖股因子 1 行 + 该股 250 根日 K + 主线快照 1 行),
不做全市场扫描, 目标 <1.5s。

数据源说明:
- 妖股因子: `demon_factors` 表(unique(symbol), 15:30 增量管线落档)。仓库里的
  `demon_factors.load_factor_pool()` 是**全市场 TopN** 口径(签名 `(topn, min_total)`,
  不收 db、不按 symbol 过滤, 且只返回前 200 名), 用它判单只股票会漏判,
  故这里按 symbol 直查单行。
- 日 K: `src.db.klines_repo.load_qfq_bars`(PG TimescaleDB hypertable, 时间列 ts
  TIMESTAMPTZ, **没有** trade_date 字段; 该函数已按 (symbol, market, '1d', 'qfq')
  取数并按日期去重 tq/tencent)。
- 题材排名: `mainline_daily_snapshot.payload`(收盘 cron 预渲染的 aggregate_mainline
  结果, ranked_groups 顺序即排名, 内含 leader.code/题材分)。不调用 TDX 板块反查
  RPC(get_relation 单次超时 6s), 那会打穿 1.5s 预算; 拿不到就跳过该判据。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

KINDS = ("demon", "left", "leader", "trend", "normal")

LABELS = {
    "demon": "情绪妖股",
    "left": "左侧超跌",
    "leader": "题材龙头",
    "trend": "趋势白马",
    "normal": "常规个股",
}

RECIPES = {
    "demon": "demon_diagnosis",
    "left": "left_diagnosis",
    "leader": "theme_leader",
    "trend": "deep_diagnosis",
    "normal": "deep_diagnosis",
}

# 判据阈值(经验值, 与 demon_score 的分档口径对齐)
DEMON_MIN_TOTAL = 60.0          # 六维总分 ≥60 即按妖股处理
DEMON_STRONG_GRADES = ("S", "A")  # 妖股等级强档
LEFT_MIN_DRAWDOWN = 35.0        # 距 250 日高点回撤 ≥35%
TREND_MA_WINDOW = 60            # 均线窗口
TREND_SLOPE_BARS = 5            # MA60 斜率回看根数
LEADER_TOP_N = 5                # 主线题材排名前 N 视为"近期排名靠前"
BARS_WINDOW = 250
BUDGET_S = 1.5                  # 单次 detect 目标耗时上限

# demon_score 用中文分档(极妖/妖/活跃/普通), 对外统一成 S/A/B/C
_GRADE_ALIAS = {
    "极妖": "S",
    "妖": "A",
    "活跃": "B",
    "普通": "C",
    "无数据": None,
}


@dataclass(frozen=True)
class Persona:
    kind: str                 # KINDS 之一
    label: str                # 中文画像名
    score: float | None       # 妖股六维总分(0~100); None = 无数据
    grade: str | None         # S/A/B/C; None = 无数据
    reasons: list[str]        # 逐条判定依据(必须带真实数值)
    dims: dict                # 原始分项(原样保留)
    recipe: str               # 建议配方 id


# ── 数据读取(全部可注入, 单测 monkeypatch 这三个函数即可)────────────────────

def _session(db):
    """有传 session 就用传入的; 否则临时开一个(用完关闭)。"""
    if db is not None:
        return db, False
    from src.db.session import SessionLocal

    return SessionLocal(), True


def _row_to_dict(row) -> dict:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    if isinstance(row, dict):
        return dict(row)
    return {}


def _load_demon(db, symbol: str) -> dict | None:
    """妖股六维因子单行(该股无因子/表缺失 → None)。不编造、不填 0。"""
    sess, own = _session(db)
    try:
        from sqlalchemy import text

        row = sess.execute(
            text(
                "SELECT factor_date, total, grade, n_events, n_sealed, max_streak,"
                " participation, dims, flags"
                " FROM demon_factors WHERE symbol = :s"
                " ORDER BY factor_date DESC LIMIT 1"
            ),
            {"s": symbol},
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 —— 表缺失/库断只在 reasons 留痕
        logger.warning("[persona] 妖股因子读取失败 %s: %s", symbol, exc)
        return None
    finally:
        if own:
            sess.close()
    if row is None:
        return None
    d = _row_to_dict(row)
    if not d:
        return None
    for key, default in (("dims", {}), ("flags", [])):
        raw = d.get(key)
        if isinstance(raw, str):
            try:
                d[key] = json.loads(raw or "null") or default
            except (TypeError, ValueError):
                d[key] = default
    total = d.get("total")
    try:
        d["total"] = float(total) if total is not None else None
    except (TypeError, ValueError):
        d["total"] = None
    d["grade_raw"] = d.get("grade")
    d["grade"] = _norm_grade(d.get("grade"))
    return d


def _norm_grade(grade) -> str | None:
    """中文分档 → S/A/B/C; 已是 S/A/B/C 的原样返回; 认不出 → None(不猜)。"""
    if grade is None:
        return None
    g = str(grade).strip().upper()
    if g in ("S", "A", "B", "C"):
        return g
    return _GRADE_ALIAS.get(str(grade).strip(), None)


def _load_bars(db, symbol: str, n: int = BARS_WINDOW, market: str = "CN") -> list[dict]:
    """该股前复权日 K(升序, 最多 n 根)。取不到返回 [](调用方跳过判据, 不猜)。

    注意口径差: `load_qfq_bars(days=)` 是**自然日**窗口, 而 n 是**交易日**根数
    (一年 ≈ 250 个交易日 ≈ 365 自然日)。直接传 days=n 只拿到约 n×0.7 根,
    "近 250 日最高价"会算短。故按 n×1.55 折算自然日再取最后 n 根。
    """
    try:
        from src.db.klines_repo import load_qfq_bars

        window_days = int(n * 1.55) + 10
        bars = load_qfq_bars(symbol, market, days=window_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[persona] 日 K 读取失败 %s: %s", symbol, exc)
        return []
    out: list[dict] = []
    for b in bars or []:
        try:
            out.append(
                {
                    "date": str(b.get("date") or ""),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out[-n:] if n else out


def _code_key(code) -> str:
    """代码归一(只留数字, 取后 6 位), 用于跨源比对; 空 → ""。"""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _load_theme_rank(db, symbol: str, top_n: int = LEADER_TOP_N) -> dict | None:
    """该股在最新主线快照里的题材身位(前 top_n 名内)。

    返回 {group, rank, score, is_leader, days} 或 None(无快照/不在前列)。
    只看库内预渲染快照, 不现算、不调 RPC。
    """
    sess, own = _session(db)
    try:
        from sqlalchemy import text

        row = sess.execute(
            text("SELECT payload FROM mainline_daily_snapshot ORDER BY date DESC LIMIT 1")
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[persona] 主线快照读取失败: %s", exc)
        return None
    finally:
        if own:
            sess.close()
    if row is None:
        return None
    payload = row[0] if not hasattr(row, "_mapping") else _row_to_dict(row).get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "null")
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    groups = payload.get("ranked_groups")
    if not isinstance(groups, list):
        return None
    key = _code_key(symbol)
    if not key:
        return None
    for idx, g in enumerate(groups[: max(int(top_n), 1)], start=1):
        if not isinstance(g, dict):
            continue
        leader = g.get("leader") or {}
        if _code_key(leader.get("code")) == key:
            return {
                "group": str(g.get("name") or ""),
                "rank": idx,
                "score": g.get("score"),
                "is_leader": True,
                "days": leader.get("days"),
            }
        for c in g.get("constituents") or []:
            if isinstance(c, dict) and _code_key(c.get("code")) == key:
                return {
                    "group": str(g.get("name") or ""),
                    "rank": idx,
                    "score": g.get("score"),
                    "is_leader": False,
                    "days": c.get("days"),
                }
    return None


# ── 主判定 ──────────────────────────────────────────────────────────────

def _fmt(v: float | None, nd: int = 2) -> str:
    return "--" if v is None else f"{v:.{nd}f}"


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def _make(kind: str, reasons: list[str], dims: dict, *, score=None, grade=None) -> Persona:
    return Persona(
        kind=kind,
        label=LABELS[kind],
        score=score,
        grade=grade,
        reasons=reasons,
        dims=dims,
        recipe=RECIPES[kind],
    )


def detect(db, symbol: str, market: str = "CN") -> Persona:
    """按画像规则判定单只标的(短路, 第一条命中即返回)。永不抛异常。"""
    t0 = time.monotonic()
    symbol = (symbol or "").strip()
    reasons: list[str] = []
    dims: dict[str, Any] = {"symbol": symbol, "market": market}
    evaluated = 0  # 真正拿到数据并比过阈值的判据数(0 → 画像数据不足)

    if not symbol:
        return _make("normal", ["标的代码为空, 无法判定 —— 画像数据不足"], dims)

    # ① demon 情绪妖股 ───────────────────────────────────────────────
    demon_score: float | None = None
    demon_grade: str | None = None
    dif = _load_demon(db, symbol)
    if dif is None:
        reasons.append("妖股六维因子(demon_factors)取不到, 跳过妖股判据")
    else:
        dims["demon"] = dif
        evaluated += 1
        demon_score = dif.get("total")
        grade_raw = dif.get("grade_raw") or dif.get("grade") or "无数据"
        # 归一口径统一放在消费侧: 无论数据层给的是中文档还是 S/A/B/C 都能判
        demon_grade = _norm_grade(grade_raw)
        streak = dif.get("max_streak")
        n_events = dif.get("n_events")
        hit = (demon_grade in DEMON_STRONG_GRADES) or (
            demon_score is not None and demon_score >= DEMON_MIN_TOTAL
        )
        if hit:
            reasons.append(
                f"妖股六维评分 {_fmt(demon_score)} 分（grade={demon_grade or '--'}/原档 {grade_raw}, "
                f"最高连板 {streak if streak is not None else '--'} 板, "
                f"近一年涨停 {n_events if n_events is not None else '--'} 次）——"
                f"满足 grade∈{{S,A}} 或 总分≥{DEMON_MIN_TOTAL:.0f}"
            )
            if dif.get("factor_date"):
                reasons.append(f"因子存档日 {dif['factor_date']}（15:30 增量管线口径）")
            return _make("demon", reasons, dims, score=demon_score, grade=demon_grade)
        reasons.append(
            f"妖股六维评分 {_fmt(demon_score)} 分（grade={demon_grade or '--'}/原档 {grade_raw}）"
            f"未达妖股门槛（需 grade∈{{S,A}} 或 总分≥{DEMON_MIN_TOTAL:.0f}）"
        )

    # 日 K 只取这一只, 左侧/趋势共用一份
    bars = _load_bars(db, symbol, BARS_WINDOW, market)
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars] if bars else []
    if not bars:
        reasons.append("日 K 取不到(klines 无该股前复权日线), 跳过左侧/趋势判据")
        ma60 = None
        last = None
        dd = None
    else:
        last = closes[-1]
        high_n = max(highs) if highs else None
        dd = round((high_n - last) / high_n * 100, 2) if high_n and high_n > 0 else None
        ma60 = _sma(closes, TREND_MA_WINDOW)
        dims["bars"] = {
            "count": len(bars),
            "last_close": round(last, 2),
            "high_n": round(high_n, 2) if high_n else None,
            "drawdown_pct": dd,
            "ma60": round(ma60, 2) if ma60 is not None else None,
        }
        if dd is None:
            reasons.append(f"近 {len(bars)} 根日 K 无最高价, 回撤算不出, 跳左侧判据")
        if ma60 is None:
            reasons.append(
                f"日 K 仅 {len(bars)} 根, 不足 {TREND_MA_WINDOW} 根算 MA60, 跳过左侧/趋势判据"
            )

    # ② left 左侧超跌 ────────────────────────────────────────────────
    if dd is not None and ma60 is not None and last is not None:
        evaluated += 1
        if dd >= LEFT_MIN_DRAWDOWN and last < ma60:
            reasons.append(
                f"距近 {len(bars)} 日最高价 {_fmt(dims['bars']['high_n'])} 回撤 {_fmt(dd)}%"
                f"（≥{LEFT_MIN_DRAWDOWN:.0f}%）且现价 {_fmt(last)} 在 MA60 {_fmt(ma60)} 下方"
            )
            return _make("left", reasons, dims, score=demon_score, grade=demon_grade)
        reasons.append(
            f"左侧判据未触发：回撤 {_fmt(dd)}%（需≥{LEFT_MIN_DRAWDOWN:.0f}%）, "
            f"现价 {_fmt(last)} / MA60 {_fmt(ma60)}"
        )
    elif dd is not None and last is not None:
        reasons.append(f"左侧判据未触发：回撤 {_fmt(dd)}%, MA60 缺样本")

    # ③ leader 题材龙头 ──────────────────────────────────────────────
    # 只剩这一次额外查询; 已超预算就跳过它(但不中断后续 trend: 日 K 已在手, 零额外 IO)
    elapsed = time.monotonic() - t0
    if elapsed > BUDGET_S * 0.8:
        reasons.append(
            f"已耗时 {elapsed:.2f}s, 跳过题材龙头判据(保 {BUDGET_S:.1f}s 预算, 未查不等于不是龙头)"
        )
    else:
        theme = _load_theme_rank(db, symbol, LEADER_TOP_N)
        if theme is None:
            reasons.append(
                f"主线快照(mainline_daily_snapshot)未见该股在前 {LEADER_TOP_N} 名题材内或快照缺失, "
                "跳过题材龙头判据"
            )
        else:
            dims["theme"] = theme
            role = "龙头股" if theme.get("is_leader") else "成分股"
            reasons.append(
                f"所属题材「{theme.get('group')}」今日主线排名第 {theme.get('rank')}"
                f"（主线分 {_fmt(theme.get('score'))}）, 该股为其{role}"
                f"（{theme.get('days') if theme.get('days') is not None else '--'} 板）"
            )
            return _make("leader", reasons, dims, score=demon_score, grade=demon_grade)

    # ④ trend 趋势白马 ───────────────────────────────────────────────
    if ma60 is not None and last is not None:
        evaluated += 1
        slope = None
        if len(closes) >= TREND_MA_WINDOW + TREND_SLOPE_BARS:
            prev = _sma(closes[:-TREND_SLOPE_BARS], TREND_MA_WINDOW)
            slope = round(ma60 - prev, 4) if prev is not None else None
        dims["ma60_slope_5d"] = slope
        if last > ma60 and slope is not None and slope > 0:
            reasons.append(
                f"现价 {_fmt(last)} 站上 MA60 {_fmt(ma60)}"
                f"（{_fmt((last - ma60) / ma60 * 100)}%）, MA60 近 {TREND_SLOPE_BARS} 日斜率 "
                f"{slope:+.4f}（上行）, 回撤仅 {_fmt(dd)}%"
            )
            return _make("trend", reasons, dims, score=demon_score, grade=demon_grade)
        slope_txt = "--" if slope is None else f"{slope:+.4f}"
        reasons.append(
            f"趋势判据未触发：现价 {_fmt(last)} / MA60 {_fmt(ma60)}, MA60 近 {TREND_SLOPE_BARS} 日斜率 "
            f"{slope_txt}（需站上 MA60 且斜率 >0）"
        )
    elif bars:
        reasons.append("均线缺样本, 跳过趋势判据(不猜上行方向)")

    # ⑤ normal ──────────────────────────────────────────────────────
    if evaluated == 0:
        reasons.append("画像数据不足：妖股因子/日 K/题材快照均未取到, 按常规个股处理")
    else:
        reasons.append(f"未触发妖股/左侧/题材龙头/趋势任一判据, 按常规个股处理（耗时 {time.monotonic() - t0:.2f}s）")
    return _make("normal", reasons, dims, score=demon_score, grade=demon_grade)


def render(p: Persona) -> str:
    """一段可读文本: 第一行结论, 之后逐条依据。"""
    head = f"[标的画像] {p.label}（kind={p.kind}｜建议配方 {p.recipe}）"
    if p.score is not None or p.grade:
        head += f"｜妖股六维 {_fmt(p.score)} 分 grade={p.grade or '--'}"
    lines = [head]
    lines.extend(f"· {r}" for r in (p.reasons or ["无判定依据"]))
    return "\n".join(lines)
