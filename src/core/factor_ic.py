"""因子 IC/IR（2026-10-21，本地计算版）。

原来 `get_factor_ic_report` 走 LLM 归因、且要 OpenAI key（实测 `Missing credentials` 后静默降级成
"样本不足"，等于废掉）。这里改成**纯本地方差计算**，不需要任何外部数据：

    IC(T) = Spearman( T 日横截面因子值 , T→T+N 日收益 )
    IR    = mean(IC) / std(IC)

未来收益由日线自算（close(T+N)/close(T)-1），**不需要用户的交割单或调仓记录**。
样本不足（<20 只票 或 <5 个交易日）直接说不足，不编数字。

2026-10-24 增补（创新 5「IC 联合选股」后端前置，交接备注 §10）：

    - winsorize_cross_section      横截面去极值（MAD 绝对中位差法）
    - orthogonalize_cross_section  截面正交化（OLS 残差，纯 Python 高斯消元）
    - quantile_backtest(_core)     Quantile 分层回测（T+1/3/5 + 同池无条件基准
                                   + 剔除 T+1 一字涨停，口径与 backtest_lab 同源）

延续同一设计哲学：**不依赖外部数据、样本不足就直说、不编数字**——
不引 numpy/scipy，回归与排序全部纯 Python 手写（同 `_ranks` 的风格）。
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

FACTOR_LABELS = {
    "momentum_20": "20 日动量",
    "momentum_60": "60 日动量",
    "vol_20": "20 日年化波动",
    "volume_z20": "量能 z",
    "position_120": "120 日区间位置",
    "concentration": "CYQ 集中度",
}

# MAD→σ 的一致性常数（正态分布下 MAD×1.4826 ≈ σ），让 n_mad 可读作"稳健版 n 倍标准差"。
MAD_TO_SIGMA = 1.4826


def _ranks(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 10:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if not vx or not vy:
        return None
    return cov / (vx * vy)


def compute(db: Session, horizon: int = 5, max_dates: int = 60) -> dict:
    from src.db.klines_repo import load_qfq_bars
    from src.db.models import StockFactorDaily
    from sqlalchemy import text

    rows = (
        db.query(StockFactorDaily)
        .order_by(StockFactorDaily.trade_date.desc())
        .limit(60000)
        .all()
    )
    if not rows:
        return {"available": False, "reason": "因子表为空（先跑回填或等待每日采集）"}

    by_date: dict[str, list] = defaultdict(list)
    for r in rows:
        by_date[r.trade_date].append(r)
    dates = sorted(by_date)[-max_dates:]

    # 每只票的收盘价序列：只取因子表里出现过的票
    symbols = {r.symbol for r in rows}
    close_map: dict[str, list[tuple[str, float]]] = {}
    for sym in symbols:
        try:
            bars = load_qfq_bars(sym, "CN", days=260)
        except Exception:  # noqa: BLE001
            continue
        close_map[sym] = [(str(b["date"]).replace("-", ""), float(b["close"])) for b in bars]

    def fwd_ret(sym: str, day: str) -> float | None:
        series = close_map.get(sym)
        if not series:
            return None
        idx = next((i for i, (d, _) in enumerate(series) if d == day), None)
        if idx is None or idx + horizon >= len(series):
            return None
        base = series[idx][1]
        return (series[idx + horizon][1] / base - 1) if base else None

    results = {}
    for factor, label in FACTOR_LABELS.items():
        ics = []
        for day in dates:
            xs, ys = [], []
            for r in by_date[day]:
                v = getattr(r, factor, None)
                if v is None:
                    continue
                y = fwd_ret(r.symbol, day)
                if y is None:
                    continue
                xs.append(float(v))
                ys.append(y)
            ic = _spearman(xs, ys)
            if ic is not None:
                ics.append(ic)
        if len(ics) < 5:
            results[factor] = {"label": label, "ic_mean": None, "days": len(ics)}
            continue
        mean = sum(ics) / len(ics)
        var = sum((i - mean) ** 2 for i in ics) / max(len(ics) - 1, 1)
        std = math.sqrt(var)
        results[factor] = {
            "label": label,
            "ic_mean": round(mean, 4),
            "ic_std": round(std, 4),
            "ir": round(mean / std, 2) if std else None,
            "win_rate": round(sum(1 for i in ics if i > 0) / len(ics) * 100, 1),
            "days": len(ics),
        }
    return {"available": True, "horizon": horizon, "dates": [dates[0], dates[-1]],
            "factors": results}


def render(m: dict) -> str:
    if not m.get("available"):
        return f"[因子 IC] {m.get('reason', '无数据')}——不做推断。"
    lines = [f"[数据源: 本地计算 · stock_factor_daily × 前复权日线] 预测期 {m['horizon']} 个交易日"
             f"｜样本区间 {m['dates'][0]}~{m['dates'][1]}"]
    for _k, v in m["factors"].items():
        if v.get("ic_mean") is None:
            lines.append(f"- {v['label']}：样本不足（{v['days']} 个有效交易日，至少需 5 个）")
            continue
        lines.append(
            f"- {v['label']}：IC 均值 {v['ic_mean']:+.4f}｜IR {v.get('ir')}｜"
            f"胜率 {v['win_rate']}%｜样本 {v['days']} 天"
        )
    lines.append("口径：Spearman 秩相关；未来收益由日线自算，未使用任何外部调仓数据。")
    return "\n".join(lines)


# ═══════════════════ 去极值（winsorize，2026-10-24） ═══════════════════

def winsorize_cross_section(
    values: Sequence[float | None],
    *,
    n_mad: float = 3.0,
    min_valid: int = 5,
) -> dict:
    """横截面因子值去极值（MAD 绝对中位差法，纯本地，无外部依赖）。

    为什么选 MAD 而不是 1%/99% 分位截断：
    - A 股横截面（动量/量能类因子）厚尾且偏斜，1%/99% 是"无脑按固定比例砍尾巴"，
      尾巴里哪些是真极值与分布形状无关；分布越偏，误伤/漏截越严重；
    - MAD 以中位数为锚、以绝对中位差为尺度，**对极值本身不敏感**（极值再大也推不动
      中位数与 MAD），截断边界随分布自适应；`MAD × 1.4826 ≈ σ`（正态分布下的
      一致性常数），因此 `median ± n_mad × 1.4826 × MAD` 可读作"稳健版 n 倍标准差"；
    - 极少数 MAD=0 的退化截面（过半样本同值）自动回退 1%/99% 分位截断，
      否则边界会塌缩成中位数、把整个截面夹平——回退在 `method` 里显式标注，不静默。

    缺值处理（显式）：None / NaN / inf 一律视为缺值——不参与中位数与 MAD 统计，
    输出列表同位置保持 None（与输入逐位对齐，方便调用方 zip 回 symbol）。

    Args:
        values: 横截面因子值（允许 None/NaN/inf）。
        n_mad: 截断宽度，默认 3.0（≈ 正态 3σ）。
        min_valid: 有效值下限，低于它显式报不足（默认 5）。

    Returns:
        dict，含 available / reason / values（对齐输入位置）/ n_input / n_valid /
        n_clipped / median / mad / lower / upper / method。
        available=False 时 values=None、reason 说明原因，绝不硬算。
    """
    xs: list[float] = []
    idx_map: list[int] = []
    for i, v in enumerate(values):
        f: float | None = None
        if v is not None:
            try:
                f = float(v)
            except (TypeError, ValueError):
                f = None
        if f is not None and math.isfinite(f):
            xs.append(f)
            idx_map.append(i)

    base = {"n_input": len(values), "n_valid": len(xs)}
    if len(xs) < min_valid:
        return {
            "available": False,
            "reason": f"样本不足：有效值 {len(xs)} 个 < {min_valid}，不做去极值（不硬算）",
            "values": None,
            **base,
            "n_clipped": None, "median": None, "mad": None,
            "lower": None, "upper": None, "method": None,
        }

    med = float(statistics.median(xs))
    mad = float(statistics.median([abs(x - med) for x in xs]))
    if mad > 0:
        half = n_mad * MAD_TO_SIGMA * mad
        lower, upper = med - half, med + half
        method = "mad"
    elif len(xs) >= 2:
        # 退化保护：过半样本同值 → MAD=0，按原边界会把整截面夹成中位数。
        qs = statistics.quantiles(xs, n=100, method="inclusive")
        lower, upper = float(qs[0]), float(qs[98])  # 1% / 99%
        method = "quantile_fallback(mad=0)"
    else:
        lower, upper = med, med
        method = "quantile_fallback(mad=0,单点)"

    out: list[float | None] = [None] * len(values)
    n_clipped = 0
    for x, i in zip(xs, idx_map):
        c = min(max(x, lower), upper)
        if c != x:
            n_clipped += 1
        out[i] = c
    return {
        "available": True,
        "reason": None,
        "values": out,
        **base,
        "n_clipped": n_clipped,
        "median": med, "mad": mad, "lower": lower, "upper": upper,
        "method": method,
    }


# ═══════════════════ 截面正交化（orthogonalize，2026-10-24） ═══════════════════

def _solve_linear_system(a: list[list[float]], b: list[float]) -> list[float] | None:
    """高斯消元（部分主元）解线性方程组；奇异返回 None，绝不硬算。

    只用于 k 很小的正规方程（对照因子通常 1~3 个），纯 Python 足够，
    奇异判据相对全矩阵尺度（1e-12），完全共线的对照因子会被显式拒绝。
    """
    n = len(b)
    m = [a[i][:] + [b[i]] for i in range(n)]
    scale = max((abs(x) for row in m for x in row), default=0.0)
    if scale <= 0:
        return None
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) <= 1e-12 * scale:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = m[i][n] - sum(m[i][j] * x[j] for j in range(i + 1, n))
        x[i] = s / m[i][i]
    return x


def orthogonalize_cross_section(
    target: Sequence[float | None],
    controls: Sequence[Sequence[float | None]],
    *,
    control_names: Sequence[str] | None = None,
    min_valid: int | None = None,
) -> dict:
    """截面正交化：目标因子对一组对照因子做横截面 OLS（含截距），返回残差。

    残差 = 目标因子中**不能被对照因子线性解释**的部分（如动量对波动正交后，
    剩下的就是"波动解释不掉的动量"）。数学性质（有测试守着，FIC-003）：
    残差与每个对照因子、与截距的内积 ≈ 0；r_squared = 对照因子对目标横截面
    方差的解释比例。

    缺值处理（显式，listwise）：目标或任一对照在该位置缺值（None/NaN/inf）→
    整行剔除；输出残差同位置为 None（与输入逐位对齐），n_dropped 记录剔除行数。

    样本不足（显式，不硬算）：
    - 有效行 < max(10, 2×(对照数+1))——统计下限 10 与 `_spearman` 对齐，
      2×(k+1) 是"每个自由度至少 2 个样本"的回归下限；
    - 对照因子完全共线 / 设计矩阵退化（正规方程奇异）→ available=False。

    结构错误（无对照因子 / 长度不一致 / 名字数量不符）属调用方 bug，抛 ValueError——
    与"数据条件不满足显式报不足"是两类事情。

    Args:
        target: 目标因子横截面。
        controls: 对照因子横截面列表（与 target 逐位对齐）。
        control_names: 对照因子名（缺省 c1..ck），用于 coefficients 键名。
        min_valid: 有效行下限；None 时用 max(10, 2×(k+1))。

    Returns:
        dict，含 available / reason / residuals（对齐输入位置，缺值位 None）/
        n_input / n_used / n_dropped / names / coefficients / r_squared。
    """
    k = len(controls)
    if k == 0:
        raise ValueError("orthogonalize_cross_section 需要至少一个对照因子")
    n = len(target)
    if any(len(c) != n for c in controls):
        raise ValueError("target 与 controls 长度不一致（横截面必须逐位对齐）")
    names = list(control_names) if control_names is not None else [f"c{j + 1}" for j in range(k)]
    if len(names) != k:
        raise ValueError("control_names 数量与 controls 不一致")

    rows: list[list[float]] = []  # 每行 [1, c1, ..., ck]
    ys: list[float] = []
    keep_idx: list[int] = []
    for i in range(n):
        tv = target[i]
        if tv is None:
            continue
        try:
            tf = float(tv)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(tf):
            continue
        crow: list[float] = []
        ok = True
        for c in controls:
            cv = c[i]
            if cv is None:
                ok = False
                break
            try:
                cf = float(cv)
            except (TypeError, ValueError):
                ok = False
                break
            if not math.isfinite(cf):
                ok = False
                break
            crow.append(cf)
        if not ok:
            continue
        rows.append([1.0, *crow])
        ys.append(tf)
        keep_idx.append(i)

    need = max(10, 2 * (k + 1)) if min_valid is None else min_valid
    base = {"n_input": n, "n_used": len(ys), "n_dropped": n - len(ys), "names": names}
    if len(ys) < need:
        return {
            "available": False,
            "reason": f"样本不足：有效行 {len(ys)} < {need}（对照 {k} 个），不做正交化（不硬算）",
            "residuals": None,
            **base,
            "coefficients": None, "r_squared": None,
        }

    p = k + 1
    xtx = [[sum(row[r1] * row[r2] for row in rows) for r2 in range(p)] for r1 in range(p)]
    xty = [sum(row[r] * y for row, y in zip(rows, ys)) for r in range(p)]
    beta = _solve_linear_system(xtx, xty)
    if beta is None:
        return {
            "available": False,
            "reason": "对照因子完全共线或退化，正规方程奇异，不做正交化（不编造残差）",
            "residuals": None,
            **base,
            "coefficients": None, "r_squared": None,
        }

    residuals: list[float] = []
    ss_res = 0.0
    for row, y in zip(rows, ys):
        fit = sum(b * x for b, x in zip(beta, row))
        r = y - fit
        residuals.append(r)
        ss_res += r * r
    my = sum(ys) / len(ys)
    ss_tot = sum((y - my) ** 2 for y in ys)
    if ss_tot <= 0:
        return {
            "available": False,
            "reason": "目标因子横截面无差异（恒定值），正交化无意义（不编造残差）",
            "residuals": None,
            **base,
            "coefficients": None, "r_squared": None,
        }

    out: list[float | None] = [None] * n
    for r, i in zip(residuals, keep_idx):
        out[i] = r
    return {
        "available": True,
        "reason": None,
        "residuals": out,
        **base,
        "coefficients": {"intercept": beta[0], **{nm: beta[j + 1] for j, nm in enumerate(names)}},
        "r_squared": 1.0 - ss_res / ss_tot,
    }


# ═══════════════════ Quantile 分层回测（2026-10-24） ═══════════════════

# 延续 factor_ic.compute 既有门槛：单日横截面 <20 只票 或 有效交易日 <5 个 → 显式报不足。
MIN_XSEC = 20
MIN_DATES = 5
# 分层胜率/均值的最小样本，与 backtest_lab.MIN_SAMPLE 同口径（<20 不给胜率，测试钉住同值）。
MIN_BUCKET_SAMPLE = 20


def _agg_rets(rets: list[float], min_sample: int) -> dict:
    """聚合一批前瞻收益（%）：与 backtest_lab 同纪律——样本 < min_sample 一律 None，不给伪统计。

    胜率口径与 backtest_lab 完全一致：收盘对收盘收益 > 0 占比。
    """
    n = len(rets)
    if n < min_sample:
        return {
            "sample_count": n,
            "win_rate_pct": None, "avg_return_pct": None, "median_return_pct": None,
        }
    wins = sum(1 for r in rets if r > 0)
    return {
        "sample_count": n,
        "win_rate_pct": round(wins / n * 100.0, 2),
        "avg_return_pct": round(sum(rets) / n, 4),
        "median_return_pct": round(float(statistics.median(rets)), 4),
    }


def _finite(v: object) -> bool:
    """显式的有限数值判定：None / 非数值 / NaN / inf 都算缺值，绝不补 0。"""
    if v is None:
        return False
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def quantile_backtest_core(
    samples: Sequence[Mapping[str, object]],
    *,
    quantiles: int = 5,
    horizons: Sequence[int] = (1, 3, 5),
    min_xsec: int = MIN_XSEC,
    min_dates: int = MIN_DATES,
    min_sample: int = MIN_BUCKET_SAMPLE,
) -> dict:
    """Quantile 分层回测核心（纯函数，不碰 DB，可脱离数据库单测）。

    Args:
        samples: 每项 `{"date": str, "symbol": str, "value": float,
            "fwd": {horizon: 前瞻收益%}, "untradable": bool | None}`。
            - value 缺值（None/NaN/inf）或 fwd 缺该 horizon 的样本**不进任何分母**
              （数据缺失显式少算，不补 0、不冒充）；
            - untradable=True（T+1 一字涨停）的样本按 backtest_lab 口径整体剔除并计数；
            - untradable=None 表示调用方数据不足以判定一字涨停 → 输出显式标注
              「口径缺口：未剔除」，绝不装作已剔除。
        quantiles: 分层数 N（2~10，默认 5）。
        horizons: 前瞻期（交易日），默认 T+1/T+3/T+5。
        min_xsec / min_dates / min_sample: 样本门槛（延续 factor_ic 既有口径）。

    Returns:
        dict：available / reason / quantiles / horizons / dates / n_dates /
        samples_used / untradable_skipped / exclude_limit_up / exclusion_note /
        by_horizon[h] = {buckets: [{q, sample_count, win_rate_pct, avg_return_pct,
        median_return_pct}...], baseline: {...}, top_minus_bottom: {...}}。
        Q1=Bottom（因子值最低），QN=Top（因子值最高）；方向解读交给读数的人
        （vol_20 低好、momentum 高好，本函数不做方向裁判）。

    分层规则：每个交易日截面按 (value, symbol) 升序排位，第 i 名进第 i×N//n 层。
    同分并列按 symbol 破并列保证确定性——这是**秩分层**，对因子值离群不敏感，
    因此不需要也不应该再叠出去极值。样本不足（有效交易日 < min_dates 或
    单日截面有效样本 < min_xsec）→ available=False 直说不硬算。
    """
    q = int(quantiles)
    if not (2 <= q <= 10):
        raise ValueError("quantiles 须在 2..10")
    hs = sorted({int(h) for h in horizons if int(h) >= 1})
    if not hs:
        raise ValueError("horizons 不能为空")

    by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for s in samples or []:
        by_date[str(s["date"])].append(s)

    usable: dict[str, list[Mapping[str, object]]] = {}
    short_dates = 0
    for d in sorted(by_date):
        ok = [s for s in by_date[d] if _finite(s.get("value"))]
        if len(ok) >= min_xsec:
            usable[d] = ok
        else:
            short_dates += 1

    if len(usable) < min_dates:
        return {
            "available": False,
            "reason": (
                f"样本不足：有效交易日 {len(usable)} 天 < {min_dates}"
                f"（另有 {short_dates} 天横截面有效样本 < {min_xsec} 只被剔除），不硬算"
            ),
            "quantiles": q, "horizons": hs, "dates": None,
            "n_dates": len(usable), "short_dates": short_dates,
            "samples_used": 0, "untradable_skipped": 0,
            "exclude_limit_up": None, "exclusion_note": None,
            "min_sample": min_sample, "by_horizon": None,
        }

    untradable_unknown = False
    untradable_skipped = 0
    samples_used = 0
    bucket_rets: dict[tuple[int, int], list[float]] = defaultdict(list)
    base_rets: dict[int, list[float]] = defaultdict(list)

    for d in sorted(usable):
        day = sorted(usable[d], key=lambda s: (float(s["value"]), str(s["symbol"])))
        n_day = len(day)
        for i, s in enumerate(day):
            qidx = i * q // n_day
            untrad = s.get("untradable")
            if untrad is None:
                untradable_unknown = True
            if untrad:
                untradable_skipped += 1
                continue
            samples_used += 1
            fwd = s.get("fwd") or {}
            for h in hs:
                r = fwd.get(h)
                if not _finite(r):
                    continue
                rf = float(r)  # type: ignore[arg-type]
                bucket_rets[(qidx, h)].append(rf)
                base_rets[h].append(rf)

    by_horizon: dict[int, dict] = {}
    for h in hs:
        buckets = [{"q": qi + 1, **_agg_rets(bucket_rets.get((qi, h), []), min_sample)} for qi in range(q)]
        top, bottom = buckets[-1], buckets[0]

        def _diff(a: float | None, b: float | None) -> float | None:
            return round(a - b, 4) if (a is not None and b is not None) else None

        spread = {
            "avg_spread_pct": _diff(top["avg_return_pct"], bottom["avg_return_pct"]),
            "median_spread_pct": _diff(top["median_return_pct"], bottom["median_return_pct"]),
            "win_rate_diff_pp": _diff(top["win_rate_pct"], bottom["win_rate_pct"]),
        }
        by_horizon[h] = {
            "buckets": buckets,
            "baseline": _agg_rets(base_rets.get(h, []), min_sample),
            "top_minus_bottom": spread,
        }

    if untradable_unknown:
        exclude_flag = False
        note = (
            "口径缺口：部分样本无法判定 T+1 一字涨停（untradable=None），未剔除——"
            "结果可能高估可买收益，禁止据此宣称已剔除一字板"
        )
    else:
        exclude_flag = True
        note = (
            "已剔除 T+1 一字涨停（open==high==low 且较前收涨幅≥4.8%，与 backtest_lab 同口径），"
            f"共剔除 {untradable_skipped} 个样本"
        )

    return {
        "available": True,
        "reason": None,
        "quantiles": q, "horizons": hs,
        "dates": [min(usable), max(usable)], "n_dates": len(usable),
        "short_dates": short_dates,
        "samples_used": samples_used,
        "untradable_skipped": untradable_skipped,
        "exclude_limit_up": exclude_flag,
        "exclusion_note": note,
        "min_sample": min_sample,
        "by_horizon": by_horizon,
    }


def quantile_backtest(
    db: Session,
    factor: str,
    *,
    quantiles: int = 5,
    horizons: Sequence[int] = (1, 3, 5),
    max_dates: int = 60,
    min_xsec: int = MIN_XSEC,
    min_dates: int = MIN_DATES,
    min_sample: int = MIN_BUCKET_SAMPLE,
) -> dict:
    """Quantile 分层回测（DB 入口）：复用 compute() 的取数路径，不新增网络请求模式。

    数据路径与 `compute()` 完全一致：`stock_factor_daily` 横截面 ×
    `load_qfq_bars` 前复权日线（未来收益 = close(T+h)/close(T) − 1，
    收盘对收盘，单位 %，与 backtest_lab 同口径）。

    一字涨停判定**直接复用** `backtest_lab._is_untradable_next_bar`
    （open==high==low 且较前收涨幅 ≥ 4.8%）——同一套纪律，不发明第二套口径。
    非正价格的无效 bar 剔除（同 backtest_lab._norm_bar 的精神）。

    Args:
        db: SQLAlchemy 会话。
        factor: 因子列名，必须是 `FACTOR_LABELS` 之一。
        其余参数同 `quantile_backtest_core`。

    Returns:
        `quantile_backtest_core` 的输出，另附 factor / label / data_source。
    """
    out_fail = {"available": False, "reason": "", "factor": factor,
                "label": FACTOR_LABELS.get(factor), "data_source": None}
    if factor not in FACTOR_LABELS:
        out_fail["reason"] = f"未知因子 {factor}，可用：{'、'.join(FACTOR_LABELS)}"
        return out_fail

    from src.core.backtest_lab import _is_untradable_next_bar
    from src.db.klines_repo import load_qfq_bars
    from src.db.models import StockFactorDaily

    rows = (
        db.query(StockFactorDaily)
        .order_by(StockFactorDaily.trade_date.desc())
        .limit(60000)
        .all()
    )
    if not rows:
        out_fail["reason"] = "因子表为空（先跑回填或等待每日采集）"
        return out_fail

    by_date: dict[str, list] = defaultdict(list)
    for r in rows:
        by_date[r.trade_date].append(r)
    dates = sorted(by_date)[-max_dates:]

    # 每只票的前复权日线（含 OHLC，供一字涨停判定）；只取因子表里出现过的票
    symbols = {r.symbol for r in rows}
    bars_map: dict[str, list[dict[str, float]]] = {}
    for sym in symbols:
        try:
            bars = load_qfq_bars(sym, "CN", days=260)
        except Exception:  # noqa: BLE001
            continue
        norm: list[dict[str, float]] = []
        for b in bars or []:
            try:
                o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                continue  # 无效 bar 剔除（同 backtest_lab._norm_bar 精神）
            norm.append({"date": str(b["date"]).replace("-", ""), "open": o, "high": h, "low": l, "close": c})
        if norm:
            bars_map[sym] = norm

    samples: list[dict[str, object]] = []
    for day in dates:
        for r in by_date[day]:
            v = getattr(r, factor, None)
            if not _finite(v):
                continue
            series = bars_map.get(r.symbol)
            if not series:
                continue
            idx = next((i for i, b in enumerate(series) if b["date"] == str(day)), None)
            if idx is None or idx + 1 >= len(series):
                continue  # 没有 T+1 就既没有未来收益也没有一字判定，显式少算
            c0 = series[idx]["close"]
            fwd: dict[int, float] = {}
            for h in horizons:
                ih = int(h)
                if ih < 1 or idx + ih >= len(series):
                    continue
                c_end = series[idx + ih]["close"]
                if c0 > 0 and c_end > 0:
                    fwd[ih] = (c_end / c0 - 1.0) * 100.0
            samples.append({
                "date": str(day),
                "symbol": r.symbol,
                "value": float(v),  # type: ignore[arg-type]
                "fwd": fwd,
                "untradable": _is_untradable_next_bar(c0, series[idx + 1]),
            })

    out = quantile_backtest_core(
        samples, quantiles=quantiles, horizons=horizons,
        min_xsec=min_xsec, min_dates=min_dates, min_sample=min_sample,
    )
    out["factor"] = factor
    out["label"] = FACTOR_LABELS[factor]
    out["data_source"] = "stock_factor_daily × 前复权日线（同 compute，本地自算，未使用任何外部调仓数据）"
    return out


def render_quantile_backtest(m: dict) -> str:
    """把 `quantile_backtest` 输出渲染成参谋可读文本（三件套齐全，缺失显式标注）。"""
    if not m.get("available"):
        return f"[因子分层回测] {m.get('reason', '无数据')}——不做推断。"
    hs = m["horizons"]
    lines = [
        f"[数据源: {m.get('data_source', '本地计算')}] {m.get('label', m.get('factor'))}"
        f"｜{m['quantiles']} 层分层（Q1=Bottom，Q{m['quantiles']}=Top）"
        f"｜预测期 T+{'/T+'.join(str(h) for h in hs)}"
        f"｜样本区间 {m['dates'][0]}~{m['dates'][1]}（{m['n_dates']} 个交易日）",
        m["exclusion_note"],
    ]
    for h in hs:
        blk = m["by_horizon"][h]
        base = blk["baseline"]
        if base["win_rate_pct"] is not None:
            lines.append(
                f"【T+{h}】同池无条件基准：胜率 {base['win_rate_pct']:.2f}%｜"
                f"均值 {base['avg_return_pct']:+.3f}%（n={base['sample_count']}）"
            )
        else:
            lines.append(f"【T+{h}】同池无条件基准：样本不足（n={base['sample_count']} < {m['min_sample']}，不给胜率）")
        for b in blk["buckets"]:
            if b["avg_return_pct"] is None:
                lines.append(f"- Q{b['q']}：样本不足（n={b['sample_count']} < {m['min_sample']}，不给胜率/均值）")
                continue
            lines.append(
                f"- Q{b['q']}：均值 {b['avg_return_pct']:+.3f}%（中位 {b['median_return_pct']:+.3f}%）"
                f"｜胜率 {b['win_rate_pct']:.2f}%（n={b['sample_count']}）"
            )
        sp = blk["top_minus_bottom"]
        if sp["avg_spread_pct"] is not None:
            lines.append(
                f"- 多空价差(Top−Bottom)：均值 {sp['avg_spread_pct']:+.3f}%"
                f"（中位 {sp['median_spread_pct']:+.3f}%）｜胜率差 {sp['win_rate_diff_pp']:+.2f}pp"
            )
        else:
            lines.append("- 多空价差(Top−Bottom)：Top/Bottom 层样本不足，不给价差")
    lines.append("口径：收盘对收盘收益（%）；历史分层统计不代表未来，仅作条件化证据。")
    return "\n".join(lines)
