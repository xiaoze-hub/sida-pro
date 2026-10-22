"""历史相似分形对标（2026-10-23）。

为什么需要它：用户问「这位置危险吗」，模型只会复述技术指标（MACD 死叉 / RSI 超买 / 空头排列）。
真正可核对的是**历史频率**：本股过去一年里出现过多少次同样的形态，之后 T+1 / T+5 实际怎么走。
这是赔率（frequency），不是预测（prediction）——所以每条出口都带样本量与免责口径。

纪律（硬，全部有测试兜底）：
- 邻样本 n < min_samples（默认 10）就写「样本不足」，**绝不**拿 3 个样本算上涨概率；
- 参照窗口排除最近 21 根，保证邻样本的前瞻窗口不会碰到当前这根（否则是拿今天当历史）；
- 特征缺失就点名缺哪个特征，不填补、不插值、不外推；
- 距离按候选集内的标准差归一：各维度量纲差两个数量级（量比 vs 涨跌%），
  不归一的话 20 日量比会独占全部权重；
- 不足 k 个邻居就用实际有的，报真实 n（不复制样本凑数）。
"""
from __future__ import annotations

from typing import Any

# 参照样本至少要比当前 bar 早这么多根：T+5 的前瞻窗口才不会压到当前这根上。
EXCLUDE_TAIL = 21
# 前瞻窗口（交易日）。
FWD_WINDOWS = (1, 5)
# 120 日区间位置需要的历史长度。
_MIN_HISTORY = 120

_FEATURE_LABELS = {
    "ret5": "5日涨跌",
    "ret20": "20日涨跌",
    "vol_ratio20": "20日量比",
    "range_pos120": "120日区间位置",
    "ma20_dist_pct": "20日均线偏离",
    "candle_up": "当日K线方向",
}
_FEATURES = tuple(_FEATURE_LABELS)

DISCLAIMER = "历史频率不是预测：只说明这些样本之后实际发生了什么，不代表本次会重复。"


def _f(v: Any) -> float | None:
    """数值化。非数/NaN/Inf 一律当「没取到」，不拿 0 冒充。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def _day(v: Any) -> str:
    """日期归一成 YYYY-MM-DD（只做格式归一，不推算日期）。"""
    s = str(v or "").strip().replace("/", "-")
    return s[:10]


def _features_at(closes: list[float | None], highs: list[float | None],
                 lows: list[float | None], opens: list[float | None],
                 vols: list[float | None], i: int) -> tuple[dict[str, float], list[str]]:
    """第 i 根 bar 的形态特征向量；缺哪个就在 missing 里点名。"""
    c = closes[i] if 0 <= i < len(closes) else None
    if c is None:
        return {}, list(_FEATURE_LABELS)
    feat: dict[str, float] = {}
    missing: list[str] = []

    base5 = closes[i - 5] if i >= 5 else None
    if base5:
        feat["ret5"] = (c / base5 - 1) * 100
    else:
        missing.append("ret5")

    base20 = closes[i - 20] if i >= 20 else None
    if base20:
        feat["ret20"] = (c / base20 - 1) * 100
    else:
        missing.append("ret20")

    vol_i = vols[i] if i >= 20 else None
    if vol_i:
        prev_v = [v for v in vols[i - 20:i] if v is not None]
        if len(prev_v) == 20:
            avg = sum(prev_v) / 20
            if avg > 0:
                feat["vol_ratio20"] = vol_i / avg
            else:
                missing.append("vol_ratio20")
        else:
            missing.append("vol_ratio20")
    else:
        missing.append("vol_ratio20")

    if i >= _MIN_HISTORY - 1:
        win_h = [h for h in highs[i - _MIN_HISTORY + 1:i + 1] if h is not None]
        win_l = [lo for lo in lows[i - _MIN_HISTORY + 1:i + 1] if lo is not None]
        if len(win_h) == _MIN_HISTORY and len(win_l) == _MIN_HISTORY:
            hi, lo = max(win_h), min(win_l)
            if hi > lo:
                feat["range_pos120"] = (c - lo) / (hi - lo) * 100
            else:
                missing.append("range_pos120")
        else:
            missing.append("range_pos120")
    else:
        missing.append("range_pos120")

    if i >= 19:
        win_c = closes[i - 19:i + 1]
        if all(v is not None for v in win_c):
            ma20 = sum(v for v in win_c if v is not None) / 20
            if ma20 > 0:
                feat["ma20_dist_pct"] = (c / ma20 - 1) * 100
            else:
                missing.append("ma20_dist_pct")
        else:
            missing.append("ma20_dist_pct")
    else:
        missing.append("ma20_dist_pct")

    o = opens[i]
    if o is None:
        missing.append("candle_up")
    else:
        # 只看方向（收 > 开 = 阳线），不给幅度——幅度已经在 ret5 里了。
        feat["candle_up"] = 1.0 if c > o else 0.0

    return feat, missing


def _unavailable(reason: str, *, query: dict | None = None, samples: int = 0,
                 window: dict | None = None) -> dict[str, Any]:
    """不可用形状：**不含**任何 t1/t5/up_rate 字段（不许从小样本里漏出赔率）。"""
    out: dict[str, Any] = {"available": False, "reason": reason, "samples": samples}
    if query is not None:
        out["query"] = query
    if window is not None:
        out["window"] = window
    return out


def find_analogs(bars: list[dict], *, lookback_days: int = 250, k: int = 5,
                 min_samples: int = 10, max_distance: float = 1.0) -> dict[str, Any]:
    """纯函数：给最后一根 bar 找历史近邻，算 T+1 / T+5 的实际分布。

    bars: [{date, open, high, low, close, volume}] 升序（与 load_qfq_bars 一致）。

    样本口径（两道闸，缺一不可）：
    - `max_distance`：只有归一化 L1 距离 ≤ 1.0（平均每个特征偏 1σ）才算「同形」。
      不设门槛只取最近 5 根，等于把「随便 5 根」当成同形样本——那是假统计；
    - `min_samples`：同形样本不足 10 个就整块不可用。k 只是取用/展示几个最近邻，
      所以实际取 `max(k, min_samples)` 个。
    """
    rows = [b for b in (bars or []) if isinstance(b, dict)]
    lookback_days = max(int(lookback_days or 0), 1)
    k = max(int(k or 0), 1)
    min_samples = max(int(min_samples or 0), 1)
    take = max(k, min_samples)

    if not rows:
        return _unavailable("无日线数据（0 根）")

    closes = [_f(b.get("close")) for b in rows]
    highs = [_f(b.get("high")) for b in rows]
    lows = [_f(b.get("low")) for b in rows]
    opens = [_f(b.get("open")) for b in rows]
    vols = [_f(b.get("volume")) for b in rows]

    last = len(rows) - 1
    q_feat, q_missing = _features_at(closes, highs, lows, opens, vols, last)
    if q_missing:
        names = "、".join(_FEATURE_LABELS[m] for m in q_missing)
        return _unavailable(f"缺失特征：{names}——无法定位形态",
                            query={kk: (round(vv, 4) if vv is not None else None)
                                   for kk, vv in q_feat.items()},
                            window={"lookback_days": lookback_days,
                                    "excluded_tail": EXCLUDE_TAIL})

    # 参照池：窗口内 + 特征齐 + 前瞻窗口不碰当前 bar（末 21 根整段排除）。
    lo = max(_MIN_HISTORY - 1, last - lookback_days + 1)
    hi = last - EXCLUDE_TAIL - 1
    cand: list[int] = []
    cand_feat: list[dict[str, float]] = []
    for j in range(lo, hi + 1):
        feat, miss = _features_at(closes, highs, lows, opens, vols, j)
        if miss:
            continue                     # 特征不全的样本直接不入池，不做插值
        cand.append(j)
        cand_feat.append(feat)

    window = {
        "lookback_days": lookback_days,
        "from_date": _day(rows[cand[0]]["date"]) if cand else "",
        "to_date": _day(rows[cand[-1]]["date"]) if cand else "",
        "candidate_bars": len(cand),
        "excluded_tail": EXCLUDE_TAIL,
        "max_distance": max_distance,
    }
    query = {kk: round(vv, 4) for kk, vv in q_feat.items()}

    if not cand:
        return _unavailable("参照窗口内无可用的历史样本（历史日线不足）",
                            query=query, window=window)

    # 各维度按候选集标准差归一（量纲不同的维度必须同权）。
    scales: dict[str, float] = {}
    for name in _FEATURES:
        vals = [f[name] for f in cand_feat]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        sd = var ** 0.5
        scales[name] = sd if sd > 1e-9 else 1.0

    ranked: list[tuple[float, str, int]] = []
    for j, feat in zip(cand, cand_feat):
        dist = sum(abs(q_feat[n] - feat[n]) / scales[n] for n in _FEATURES) / len(_FEATURES)
        ranked.append((dist, _day(rows[j]["date"]), j))
    # 距离相同按日期定序：同一份输入永远给同一份输出。
    ranked.sort(key=lambda t: (t[0], t[1], t[2]))

    # 距离门槛：没有门槛的「最近 k 根」不是同形，是同形样本的赝品。
    matched = [t for t in ranked if t[0] <= max_distance]
    if len(matched) < min_samples:
        return _unavailable(f"样本不足（n={len(matched)}，至少 {min_samples}）",
                            query=query, samples=len(matched), window=window)

    neighbors: list[dict[str, Any]] = []
    f1_raw: list[float] = []
    f5_raw: list[float] = []
    mdd_list: list[float] = []
    failed_n = 0
    for dist, day, j in matched[:take]:
        base = closes[j]
        c1 = closes[j + FWD_WINDOWS[0]]
        c5 = closes[j + FWD_WINDOWS[1]]
        if not base or c1 is None or c5 is None:
            continue                     # 缺价就不算这个样本，不插值
        f1 = (c1 - base) / base * 100
        f5 = (c5 - base) / base * 100
        prior_lows = [v for v in lows[max(j - 9, 0):j + 1] if v is not None]
        low10 = min(prior_lows) if prior_lows else None
        fwd_lows = [v for v in (lows[j + m] for m in FWD_WINDOWS) if v is not None]
        failed = bool(low10 is not None and any(v < low10 for v in fwd_lows))
        # 单样本前 5 日路径的收盘口径最大回撤（只允许先见峰再见谷）
        peak, mdd = base, 0.0
        for m in range(1, FWD_WINDOWS[1] + 1):
            cm = closes[j + m]
            if cm is None:
                continue
            peak = max(peak, cm)
            mdd = min(mdd, (cm - peak) / peak * 100)
        mdd_list.append(mdd)
        f1_raw.append(f1)
        f5_raw.append(f5)
        failed_n += 1 if failed else 0
        neighbors.append({
            "date": day,
            "distance": round(dist, 4),
            "fwd1_pct": round(f1, 2),
            "fwd5_pct": round(f5, 2),
            "failed": failed,
        })

    n = len(neighbors)
    if n < min_samples:
        # 3 个样本算出来的「上涨 67%」不是赔率，是噪声——宁可不给。
        return _unavailable(f"样本不足（n={n}，至少 {min_samples}）",
                            query=query, samples=n, window=window)

    return {
        "available": True,
        "query": query,
        "samples": n,
        "window": window,
        "t1": {
            "mean_pct": round(sum(f1_raw) / n, 2),
            "up_rate": round(sum(1 for v in f1_raw if v > 0) / n * 100, 2),
            "n": n,
        },
        "t5": {
            "mean_pct": round(sum(f5_raw) / n, 2),
            "up_rate": round(sum(1 for v in f5_raw if v > 0) / n * 100, 2),
            "max_drawdown_pct": round(min(mdd_list), 2) if mdd_list else None,
            "failed": failed_n,
            "n": n,
        },
        "neighbors": neighbors,
        "disclaimer": DISCLAIMER,
    }


def _q(m: dict, name: str, nd: int = 2, unit: str = "%") -> str:
    v = (m.get("query") or {}).get(name)
    if v is None:
        return "--"
    if name == "candle_up":
        return "阳线" if v >= 0.5 else "阴线"
    if name == "vol_ratio20":
        return f"{v:.2f}"
    if name == "range_pos120":       # 0~100 的百分位，不带正负号
        return f"{v:.{nd}f}{unit}"
    return f"{v:+.{nd}f}{unit}"


def render(analogs: dict) -> str:
    """紧凑证据串（≤15 行），给 system prompt / 工具结果用。"""
    if not analogs.get("available"):
        reason = str(analogs.get("reason") or "无数据").strip()
        for tail in ("——不做推断。", "不做推断。"):   # 别渲染成「…——不做推断。——不做推断。」
            if reason.endswith(tail):
                reason = reason[: -len(tail)].strip()
        return f"[相似形态] {reason}——不做推断。"

    w, t1, t5 = analogs["window"], analogs["t1"], analogs["t5"]
    n = analogs["samples"]
    lines = [
        f"[相似形态] 当前形态 × 近 {w['lookback_days']} 日历史"
        f"（参照 {w['from_date']} ~ {w['to_date']}，候选 {w['candidate_bars']} 根，"
        f"取最近 {n} 个）",
        "形态：5日 {}｜20日 {}｜20日量比 {}｜120日区间位置 {}｜MA20 偏离 {}｜{}".format(
            _q(analogs, "ret5"), _q(analogs, "ret20"), _q(analogs, "vol_ratio20"),
            _q(analogs, "range_pos120", nd=1), _q(analogs, "ma20_dist_pct"),
            _q(analogs, "candle_up")),
        f"最相近的 {min(5, n)} 个历史时刻（统计用 {n} 个样本）：",
    ]
    for nb in analogs["neighbors"][:5]:
        lines.append("  {} 距离 {:.4f}｜T+1 {:+.2f}%｜T+5 {:+.2f}%｜{}".format(
            nb["date"], nb["distance"], nb["fwd1_pct"], nb["fwd5_pct"],
            "破位" if nb["failed"] else "未破位"))
    lines.append(f"频率 T+1：均值 {t1['mean_pct']:+.2f}%｜上涨 {t1['up_rate']:.2f}%｜n={t1['n']}")
    lines.append("频率 T+5：均值 {:+.2f}%｜上涨 {:.2f}%｜最差路径回撤 {}｜破位 {}/{}｜n={}".format(
        t5["mean_pct"], t5["up_rate"],
        "--" if t5.get("max_drawdown_pct") is None else f"{t5['max_drawdown_pct']:.2f}%",
        t5.get("failed", 0), t5["n"], t5["n"]))
    lines.append(
        f"[数据源: PG 前复权日线 · 相似形态分形统计 · 窗口 {w['lookback_days']} 天 · 样本 {n}]")
    lines.append(f"这是历史频率，不是预测：{n} 个样本只能当参照，不构成赔率承诺。")
    return "\n".join(lines)


def build(symbol: str, market: str = "CN", lookback_days: int = 250) -> str:
    """工具入口：读库 → 找近邻 → 渲染。数据不足就直说，不猜。"""
    sym = str(symbol or "").strip()
    if not sym:
        return "[相似形态] 缺少标的——不做推断。"
    try:
        from src.db.klines_repo import load_qfq_bars_cached as load_qfq_bars

        # 比参照窗口多取一截：末 21 根要排除，且要留出 T+5 的前瞻空间。
        bars = load_qfq_bars(sym, market, days=400)
    except Exception as exc:  # noqa: BLE001 - 取数失败不编形态
        return f"[相似形态] 取数失败：{type(exc).__name__}——不做推断。"
    return render(find_analogs(bars, lookback_days=lookback_days))
