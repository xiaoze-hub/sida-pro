# -*- coding: utf-8 -*-
"""I-3 委托行为画像: 撤单率历史基线 + 动态 Z-Score 坐标系.

为什么需要这一层
----------------
`cancel_order_intent` 原来的判定是**固定阈值**（撤单率 > 40% = 诱多）。但撤单率是
一只票的**结构属性**: 小盘活跃票 40% 是常态, 大盘蓝筹 15% 就已异常。同一个阈值压在
5000 只票上, 必然一半误报。本模块把"撤单率"从一个绝对值变成一个**相对它自己的坐标**:

    base_i = median(BCancel / BOrder)  over 近 N 个交易日   # 该票的常态撤单率
    std_i  = stdev(同上)                                    # 常态波动
    z_i    = (today_rate - base_i) / std_i                  # 今天偏离几个标准差
    pct_i  = ECDF(today_rate | 近 N 日)                      # 今日所处分位 (P0~P100)

判定（相对口径, 不是绝对口径）:
    z > 2 且 涨幅 > 3%   → 相对自身异常诱多（真实异常, 不是"数值高"）
    z < -1 且 跌幅 > 2%  → 抛压真实/恐慌确认（撤单萎缩 + 价格破位）
    |z| < 1              → 常态（不再误报）
    其余                 → 轻度偏离, 只陈述事实不给方向

数据口径（TQ 独有, 免费东财/腾讯层拿不到）
------------------------------------------
`get_exday_data(stock_code, count=N)` 的日线统计: BCancel/SCancel 撤单量(手),
BOrder/SOrder 委托量(手), 支持的 `count` 可回溯多日 ⇒ 每只票都能算出自己的历史分布。

纯函数 + 注入式取数: `build_cancel_profile` 不碰网络, `fetch_cancel_profile` 的
`exday_fn` 可替换, 单测因此可以逐值锁死算法而不依赖 TQ 客户端。
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 默认基线窗口（交易日）。研究文档口径: 近 60 日。
DEFAULT_WINDOW = 60

#: 基线最少样本。低于此值只给"今日值", 不给相对判定 —— 3 天算出来的 std 是噪声,
#: 拿它做 z 分等于把随机波动包装成结论。
MIN_BASELINE_DAYS = 20

#: 判定阈值（与研究文档 §4.3 一致）。
Z_FAKE_BIDDING = 2.0
Z_REAL_SELLING = -1.0
Z_NORMAL_BAND = 1.0
CHG_FAKE_BIDDING = 3.0
CHG_REAL_SELLING = -2.0

CALIBER = (
    "TQ get_exday_data 日线统计: 撤单率 = BCancel/BOrder, 单位手; "
    "基线=近 N 个交易日(不含今日)的中位数±标准差"
)

#: TQ 原始键 → 内部键。兼容已归一化的小写键(exday_latest 的产出)。
_ALIASES = {
    "b_cancel": ("BCancel", "b_cancel", "bcancel"),
    "s_cancel": ("SCancel", "s_cancel", "scancel"),
    "b_order": ("BOrder", "b_order", "border", "TotalBVol", "total_b_vol"),
    "s_order": ("SOrder", "s_order", "sorder", "TotalSVol", "total_s_vol"),
    "trades": ("CJBS", "trades", "cjbs"),
    "date": ("Date", "date"),
}


def _num(v: Any) -> Optional[float]:
    """任意值 → 有限 float；None / 空串 / NaN / inf / 非数字 一律 None（不补 0）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    try:
        f = float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pick(row: Dict[str, Any], key: str) -> Optional[float]:
    for name in _ALIASES.get(key, (key,)):
        if name in row:
            val = _num(row.get(name))
            if val is not None:
                return val
    return None


def norm_date(raw: Any) -> str:
    """TQ 日期(20261017) → '2026-10-17'；已带横线原样返回。"""
    s = str(raw or "").strip()[:10].replace("/", "-")
    if "-" in s:
        return s
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def cancel_rate(cancel: Optional[float], order: Optional[float]) -> Optional[float]:
    """撤单率 = 撤单量 / 委托量, 规范到 [0, 1]。

    口径说明（两种 BOrder 语义都要能算, 且必须单调有界）:
      · BOrder 是"累计委托量(含已撤)"  → rate = BCancel / BOrder        (主力口径, 常态 < 1)
      · BOrder 是"当前剩余委托量"      → 总申报 = BOrder + BCancel       (回退口径)
    判别方式: cancel <= order 时按前者; 否则按后者。两者都不会 > 1, 也不臆造数据 ——
    任一侧缺值直接 None（0 是真实值, 不能顶替缺失）。
    """
    cancel = _num(cancel)
    order = _num(order)
    if cancel is None or order is None:
        return None
    if cancel < 0 or order < 0:
        return None
    if order <= 0:
        return None
    if cancel <= order:
        return min(1.0, cancel / order)
    return min(1.0, cancel / (cancel + order))


def normalize_exday_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """一行 TQ 日线统计 → 内部结构（含算好的撤买率/撤卖率）。"""
    b_cancel = _pick(raw, "b_cancel")
    s_cancel = _pick(raw, "s_cancel")
    b_order = _pick(raw, "b_order")
    s_order = _pick(raw, "s_order")
    return {
        "date": norm_date(_raw_of(raw, "date")),
        "b_cancel": b_cancel,
        "s_cancel": s_cancel,
        "b_order": b_order,
        "s_order": s_order,
        "trades": _pick(raw, "trades"),
        "b_rate": cancel_rate(b_cancel, b_order),
        "s_rate": cancel_rate(s_cancel, s_order),
    }


def _raw_of(row: Dict[str, Any], key: str) -> Any:
    for name in _ALIASES.get(key, (key,)):
        if row.get(name) not in (None, ""):
            return row.get(name)
    return None


def _values(rows: List[Dict[str, Any]], field: str) -> List[float]:
    out = []
    for r in rows:
        v = r.get(field)
        if isinstance(v, (int, float)) and math.isfinite(v):
            out.append(float(v))
    return out


def describe(values: List[float]) -> Dict[str, Any]:
    """一组撤单率的分布描述。样本量不足时 std/z 相关字段为 None（不硬算）。"""
    n = len(values)
    if n == 0:
        return {"n": 0, "median": None, "mean": None, "std": None,
                "p10": None, "p90": None, "min": None, "max": None}
    med = statistics.median(values)
    mean = statistics.fmean(values)
    std = statistics.stdev(values) if n >= 2 else None
    ordered = sorted(values)
    return {
        "n": n,
        "median": med,
        "mean": mean,
        "std": std,
        "p10": _quantile(ordered, 0.10),
        "p90": _quantile(ordered, 0.90),
        "min": ordered[0],
        "max": ordered[-1],
    }


def _quantile(ordered: List[float], q: float) -> Optional[float]:
    """线性插值分位数（ordered 已升序）。"""
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def percentile_rank(value: Optional[float], values: List[float]) -> Optional[float]:
    """value 在 values 中的分位 (0~100, ECDF 口径: <= value 的占比)。"""
    if value is None or not values:
        return None
    le = sum(1 for v in values if v <= value)
    return round(100.0 * le / len(values), 1)


def zscore(value: Optional[float], median: Optional[float],
           std: Optional[float]) -> Optional[float]:
    """(value - median) / std。std 无意义(0/None) 时返回 None —— 不给伪 z。"""
    if value is None or median is None or std is None:
        return None
    if not math.isfinite(std) or std <= 1e-9:
        return None
    z = (value - median) / std
    return z if math.isfinite(z) else None


def classify(z: Optional[float], pct_chg: Optional[float]) -> Dict[str, Any]:
    """把 z (+涨跌幅) 翻成一句话结论。阈值是相对口径, 不是绝对撤单率。"""
    chg = pct_chg if pct_chg is not None else None
    if z is None:
        return {
            "code": "insufficient",
            "label": "基线不足",
            "level": "unknown",
            "abnormal": None,
            "desc": "历史基线样本不足或波动为零, 暂不给相对判定",
        }
    if z >= Z_FAKE_BIDDING and chg is not None and chg > CHG_FAKE_BIDDING:
        return {
            "code": "fake_bidding",
            "label": "⚠️ 相对异常诱多",
            "level": "warning",
            "abnormal": True,
            "desc": "撤单率显著高于该股自身常态且股价强势, 托单撤单与拉升同步, 谨防诱多",
        }
    if z <= Z_REAL_SELLING and chg is not None and chg < CHG_REAL_SELLING:
        return {
            "code": "real_selling",
            "label": "抛压真实确认",
            "level": "caution",
            "abnormal": True,
            "desc": "撤单率显著低于常态(挂单被真实成交)且股价破位, 抛压不是虚挂",
        }
    if abs(z) < Z_NORMAL_BAND:
        return {
            "code": "normal",
            "label": "常态",
            "level": "safe",
            "abnormal": False,
            "desc": "相对该股自身近端历史处于常态区间, 不构成撤单异常",
        }
    if z >= Z_FAKE_BIDDING:
        return {
            "code": "elevated_no_rally",
            "label": "撤单抬升·涨幅未配合",
            "level": "caution",
            "abnormal": True,
            "desc": "撤单率显著高于自身常态, 但涨幅未达诱多确认条件, 仅作观察",
        }
    if z <= Z_REAL_SELLING:
        return {
            "code": "quiet",
            "label": "撤单显著偏低",
            "level": "safe",
            "abnormal": False,
            "desc": "撤单率显著低于自身常态, 挂单意愿稳定(或挂单被成交消化)",
        }
    return {
        "code": "mild",
        "label": "轻度偏离",
        "level": "safe",
        "abnormal": False,
        "desc": "偏离常态但未达异常阈值",
    }


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "--"


def _fmt_pp(v: Optional[float]) -> str:
    # 撤单率是比例, std 换算成百分点(pp) 更好读
    return f"{v * 100:.1f}pp" if v is not None else "--"


def conclusion_text(*, today: Optional[float], stats: Dict[str, Any],
                    z: Optional[float], pct: Optional[float],
                    verdict: Dict[str, Any], pct_chg: Optional[float]) -> str:
    """一句话结论（前端直接展示, 不在 UI 里重算口径）。

    形态与产品稿一致: `今日买撤 62.0%, 为近 60 日 P97（常态 41.0%±9.0pp, z=+2.31）—— 相对它自己异常`
    """
    if today is None:
        return "今日无有效撤单数据（不冒充 0）"
    if stats.get("n", 0) < MIN_BASELINE_DAYS or z is None:
        return (
            f"今日买撤 {_fmt_pct(today)}；历史基线仅 {stats.get('n', 0)} 日"
            f"（需 ≥{MIN_BASELINE_DAYS} 日且波动非零）—— 暂不给相对判定"
        )
    pct_txt = f"P{pct:.0f}" if pct is not None else "P--"
    chg_txt = f", 涨幅 {pct_chg:+.2f}%" if pct_chg is not None else ""
    return (
        f"今日买撤 {_fmt_pct(today)}，为近 {stats['n']} 日 {pct_txt}"
        f"（常态 {_fmt_pct(stats['median'])}±{_fmt_pp(stats['std'])}，z={z:+.2f}{chg_txt}）"
        f" —— {verdict['desc']}"
    )


def build_cancel_profile(
    rows: List[Dict[str, Any]] | None,
    *,
    window: int = DEFAULT_WINDOW,
    pct_chg: Optional[float] = None,
    min_days: int = MIN_BASELINE_DAYS,
) -> Dict[str, Any]:
    """日线统计序列 → 委托行为画像（基线 / 分位 / z / 一句话结论）。

    参数:
        rows: `get_exday_data` 的原始行(升序或乱序均可), 每行至少 BCancel/BOrder。
        window: 基线窗口(交易日), 不含今日。
        pct_chg: 当日涨跌幅(%), 参与"真实异常"的双条件判定；None 只做单条件。

    今日 = 序列**最后一行**（收盘后即当日; 非交易日即最近交易日 —— 用 date 显式标注,
    不把"最后一行"当"今天"）。
    """
    series: List[Dict[str, Any]] = []
    if rows:
        seen: Dict[str, Dict[str, Any]] = {}
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            row = normalize_exday_row(raw)
            if not row["date"]:
                continue
            # 同日重复(补采/多源) 以最后一次为准, 不叠加
            seen[row["date"]] = row
        series = [seen[k] for k in sorted(seen)]

    window = max(1, int(window or DEFAULT_WINDOW))
    # 今日 + 之前 window 日
    series = series[-(window + 1):]
    today = series[-1] if series else None
    baseline = series[:-1] if len(series) > 1 else []

    b_vals = _values(baseline, "b_rate")
    s_vals = _values(baseline, "s_rate")
    b_stats = describe(b_vals)
    s_stats = describe(s_vals)
    enough = b_stats["n"] >= min_days

    today_rate = today.get("b_rate") if today else None
    # 样本不足 → z 直接 None: 5 天算出来的 std 是噪声, 拿它做 z 等于把随机波动包装成结论
    b_z = zscore(today_rate, b_stats["median"], b_stats["std"]) if enough else None
    b_pct = percentile_rank(today_rate, b_vals)
    verdict = classify(b_z, pct_chg)

    s_today = today.get("s_rate") if today else None
    s_z = zscore(s_today, s_stats["median"], s_stats["std"]) if s_stats["n"] >= min_days else None

    reason = None
    available = True
    if not series:
        available, reason = False, "无日线统计返回（TQ 未返回撤单数据）"
    elif today_rate is None:
        available, reason = False, "当日撤单量/委托量缺失, 无法计算撤单率"
    elif not enough:
        available, reason = True, (
            f"历史基线仅 {b_stats['n']} 个交易日（<{min_days}）, 相对判定不可用"
        )

    return {
        "available": available,
        "reason": reason,
        "caliber": CALIBER,
        "window": window,
        "baseline_days": b_stats["n"],
        "min_baseline_days": min_days,
        "series": series,
        "today": today,
        "buy": {
            "rate": today_rate,
            "cancel": today.get("b_cancel") if today else None,
            "order": today.get("b_order") if today else None,
            "median": b_stats["median"],
            "std": b_stats["std"],
            "mean": b_stats["mean"],
            "p10": b_stats["p10"],
            "p90": b_stats["p90"],
            "min": b_stats["min"],
            "max": b_stats["max"],
            "n": b_stats["n"],
            "z": b_z,
            "percentile": b_pct,
        },
        "sell": {
            "rate": s_today,
            "median": s_stats["median"],
            "std": s_stats["std"],
            "p90": s_stats["p90"],
            "n": s_stats["n"],
            "z": s_z,
            "percentile": percentile_rank(s_today, s_vals),
        },
        "verdict": verdict,
        "conclusion": conclusion_text(
            today=today_rate, stats=b_stats, z=b_z, pct=b_pct,
            verdict=verdict, pct_chg=pct_chg,
        ),
        "as_of": (today or {}).get("date") or None,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _base_payload(days: int, pct_chg: Optional[float], reason: str = "") -> Dict[str, Any]:
    """降级响应骨架（与正常响应同形状, 前端只走一条渲染路径）。"""
    verdict = classify(None, pct_chg)
    if reason:
        verdict = {**verdict, "desc": reason}
    return {
        "available": False,
        "reason": reason or None,
        "caliber": CALIBER,
        "window": days,
        "baseline_days": 0,
        "min_baseline_days": MIN_BASELINE_DAYS,
        "series": [],
        "today": None,
        "buy": None,
        "sell": None,
        "verdict": verdict,
        "conclusion": reason or "无数据",
        "as_of": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def fetch_cancel_profile(
    symbol: str,
    *,
    days: int = DEFAULT_WINDOW,
    pct_chg: Optional[float] = None,
    market: str = "CN",
    exday_fn: Optional[Callable[..., List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """取 TQ 日线统计并产出画像。`exday_fn` 注入供单测(默认 TQ 网关)。

    降级诚实: 网关异常/无数据 → `available=False` + `reason` 原文, series 为空,
    **绝不**用估算值或 0 填充。
    """
    out_sym = str(symbol or "").strip()
    if (market or "CN").upper() != "CN":
        return _base_payload(days, pct_chg, "委托行为画像依赖通达信日线统计, 仅支持 A 股(CN)")
    if not out_sym:
        return _base_payload(days, pct_chg, "缺少标的代码")

    fn = exday_fn
    try:
        if fn is None:
            # 惰性 import: 无 TQ 环境的单测/CI 仍可 import 本模块
            from marketdata.symbol import Symbol
            from marketdata.vendors.tq import exday_data, to_tq_code

            code = to_tq_code(Symbol.parse(out_sym)) or out_sym
            rows = exday_data(code, count=int(days) + 1)
        else:
            rows = fn(out_sym, int(days) + 1)
    except Exception as e:  # noqa: BLE001 - I/O 边界: 网关异常降级为可读原因
        logger.warning(f"委托行为画像[{out_sym}]取数失败: {e}")
        msg = str(e) or e.__class__.__name__
        # 网关自身文案已含 "TQ" 时不再套前缀, 避免 "TQ ...: TQ 网关不可用: ..." 的双重前缀
        out = _base_payload(days, pct_chg, msg if "TQ" in msg else f"TQ 日线统计不可用: {msg}")
        out["symbol"] = out_sym
        return out

    profile = build_cancel_profile(rows or [], window=days, pct_chg=pct_chg)
    profile["symbol"] = out_sym
    return profile
