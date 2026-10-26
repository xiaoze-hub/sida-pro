"""个股筹码/主力体检 —— 用通达信客户端**现成公式**（不是我方拟合）。

数据源: TQ 客户端指标公式（调用前须先用 `formula_set_data_info` 喂 K 线，见
`marketdata.vendors.tq.formula_zb_many` 的说明）。

采用的公式（都是客户端自带、实测有真实输出）:
    SSRP 筹码峰(成本族 MA1/MA2/SSRP) / MCST 市场成本 / CYC 成本均线(CYC1/2/3/∞)
    AMV 成本价均线 / PAV·PAVE 筹码引力 / CYW 主力控盘 / ZJTJ 庄家抬轿

**不采用** `SCR` 筹码集中度: 实测(60 根与 250 根)恒返回 `0.00`，本口径下不可用 ——
按"缺失不得当数据展示"的纪律，直接不放进体检结果，而不是显示成 0。

口径纪律: 公式返回值原样转述并标注来源; "现价 vs 成本线"的差值是**我方计算**，
单独放 `vs_close` 并标注，不做买卖结论。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 体检用公式（顺序即展示顺序）
CHIP_FORMULAS: tuple[str, ...] = ("SSRP", "MCST", "CYC", "AMV", "PAV", "PAVE", "CYW", "ZJTJ")

_ERR_KEY = "_error"


def normalize_code(raw: str) -> str:
    """裸 6 位代码补市场后缀（6→SH / 0,3,2→SZ / 4,8→BJ / 9→SH）。已带后缀则原样返回。"""
    s = (raw or "").strip().upper()
    if not s or "." in s:
        return s
    if not (s.isdigit() and len(s) == 6):
        return s
    if s.startswith("920"):        # 北交所新代码段(92xxxx)
        return f"{s}.BJ"
    head = s[0]
    if head == "6" or head == "9":  # 6xxxxx 沪A / 900xxx 沪B
        return f"{s}.SH"
    if head in ("0", "3", "2"):     # 000/002/300 深A / 200xxx 深B
        return f"{s}.SZ"
    if head in ("4", "8"):          # 43/83/87 北交所
        return f"{s}.BJ"
    return s


def _to_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _last_of(out: dict, key: str) -> float | None:
    """取某公式某个输出键的最后一个值（公式返回的是序列）。"""
    arr = out.get(key) if isinstance(out, dict) else None
    if isinstance(arr, list) and arr:
        return _to_float(arr[-1])
    return None


def build_chips(code: str, *, count: int = 250, quote: dict | None = None) -> dict[str, Any]:
    """跑公式并组装体检结果。`quote` 可传 {close, trade_date} 用于算现价与成本线差值。

    公式取不到时如实返回 available=False（**不编造**）。
    """
    from marketdata.vendors import tq as T

    try:
        raw = T.formula_zb_many(list(CHIP_FORMULAS), code, count=count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("chips: 公式引擎不可用 %s", exc)
        return {"ok": False, "reason": "tq_unavailable", "detail": str(exc)[:200], "code": code}

    feed = raw.get("_feed") or {}
    out: dict[str, Any] = {
        "ok": True,
        "code": code,
        "count": count,
        "bars": feed.get("bars"),
        "trade_date": feed.get("last_date") or "",
        "formulas": {},
    }
    failed: list[str] = []
    for name in CHIP_FORMULAS:
        v = raw.get(name) or {}
        if v.get(_ERR_KEY):
            failed.append(f"{name}: {str(v[_ERR_KEY])[:60]}")
            continue
        out["formulas"][name] = {k: _last_of(v, k) for k in v if isinstance(v.get(k), list)}

    ssrp = out["formulas"].get("SSRP", {})
    cyc = out["formulas"].get("CYC", {})
    amv = out["formulas"].get("AMV", {})
    pav = out["formulas"].get("PAV", {})
    cyw = out["formulas"].get("CYW", {})
    zjtj = out["formulas"].get("ZJTJ", {})

    out["cost"] = {
        "chip_peak_cost": ssrp.get("SSRP"),        # 筹码峰成本
        "ssrp_ma1": ssrp.get("MA1"),
        "ssrp_ma2": ssrp.get("MA2"),
        "market_cost": (out["formulas"].get("MCST") or {}).get("MCST"),
        "cyc": {k: v for k, v in cyc.items() if v is not None},
        "amv": {k: v for k, v in amv.items() if v is not None},
    }
    out["chips"] = {
        "gravity_cv": pav.get("CV"),               # 筹码引力
        "gravity_diff": pav.get("DIFF"),
        "gravity_gv": pav.get("GV"),
    }
    out["main_force"] = {
        "cyw": cyw.get("CYW"),                     # 主力控盘
        "zjtj": {k: v for k, v in zjtj.items() if v is not None},
    }
    if quote:
        close = _to_float(quote.get("close"))
        if close:
            diffs: dict[str, Any] = {}
            for label, val in (
                ("chip_peak_cost", out["cost"].get("chip_peak_cost")),
                ("market_cost", out["cost"].get("market_cost")),
            ):
                if val:
                    diffs[label] = {"cost": val, "pct": round((close - val) / val * 100, 2)}
            out["vs_close"] = {"close": close, "diff_pct": diffs}
    out["failed_formulas"] = failed
    out["excluded"] = {"SCR": "客户端公式恒返回 0.00（60/250 根实测），本口径不可用，故不展示"}
    out["caliber"] = (
        "数据源=通达信客户端指标公式(SSRP/MCST/CYC/AMV/PAV/PAVE/CYW/ZJTJ)；"
        "「现价 vs 成本线」的百分比为**我方计算**，仅陈述差值，不构成买卖建议"
    )
    return out


def render_text(c: dict[str, Any]) -> str:
    """chat 出口文本。任何取不到的项显示"—"，不显示 0。"""
    if not c or not c.get("ok"):
        return ""
    f = c.get("formulas", {})
    cost = c.get("cost", {})
    ch = c.get("chips", {})
    mf = c.get("main_force", {})

    def num(v, unit=""):
        return f"{v:,.2f}{unit}" if isinstance(v, (int, float)) else "—"

    lines = [f"🎯 筹码/主力体检（{c.get('code')}，客户端公式口径，{c.get('count')} 根日线）"]
    if c.get("trade_date"):
        lines.append(f"· 现价 {num((c.get('vs_close') or {}).get('close'))}（{c['trade_date']}）")
    lines.append("【成本/筹码峰】")
    lines.append(f"· 筹码峰成本 SSRP：{num(cost.get('chip_peak_cost'))}"
                 f"（MA1 {num(cost.get('ssrp_ma1'))} / MA2 {num(cost.get('ssrp_ma2'))}）")
    lines.append(f"· 市场成本 MCST：{num(cost.get('market_cost'))}")
    cyc = cost.get("cyc") or {}
    if cyc:
        lines.append("· 成本均线 CYC：" + " / ".join(f"{k} {num(v)}" for k, v in cyc.items()))
    amv = cost.get("amv") or {}
    if amv:
        lines.append("· 成本价均线 AMV：" + " / ".join(f"{k} {num(v)}" for k, v in amv.items()))
    lines.append("【筹码引力】")
    lines.append(f"· CV {num(ch.get('gravity_cv'))} / DIFF {num(ch.get('gravity_diff'))} / GV {num(ch.get('gravity_gv'))}")
    lines.append("【主力行为】")
    lines.append(f"· 主力控盘 CYW：{num(mf.get('cyw'))}")
    zj = mf.get("zjtj") or {}
    if zj:
        lines.append("· 庄家抬轿 ZJTJ：" + " / ".join(f"{k}={num(v)}" for k, v in zj.items()))
    diff = (c.get("vs_close") or {}).get("diff_pct") or {}
    if diff:
        parts = [f"{k} {v['pct']:+.2f}%" for k, v in diff.items()]
        lines.append("【现价 vs 成本线（我方计算）】" + " / ".join(parts))
    if c.get("failed_formulas"):
        lines.append("· ⚠️ 取不到：" + "；".join(c["failed_formulas"][:3]))
    lines.append("显示『—』= 该公式本次没给出值（**不是 0**）。")
    lines.append("注：" + str(c.get("caliber", "")))
    return "\n".join(lines)
