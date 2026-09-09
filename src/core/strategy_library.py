"""策略库核心(W4.2/KI-039): YAML 策略的求值与打分实现。

原实现在 `src/web/api/strategies.py`(API 层), 与 `src/core/strategy_engine.py` 形成两套
策略口径。本模块把实现下沉到 core, API 层只做参数校验与响应封装(import 后再导出,
保持既有导入路径可用)。

口径与实现逐字保留(搬迁, 非重写); 由 `tests/test_strategy_semantics.py` 与
`tests/test_strategies_scan.py` 锁定行为。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def _quote_to_dict(q) -> dict:
    """Quote 对象 → dict(与 apply 现有归一化输出同形)。"""
    if q is None:
        return {}
    if isinstance(q, dict):
        return q
    if hasattr(q, "__dict__"):
        return {k: v for k, v in vars(q).items() if not k.startswith("_")}
    return {"current_price": getattr(q, "current_price", None)}


def _evaluate_strategy(cfg: dict, q: dict, strategy_id: str, symbol: str, market: str) -> dict:
    """对单只股票的 dict 行情执行策略硬过滤 + 因子打分(apply/scan 共用)。

    纯函数, 不发起网络请求。字段缺失 → missing_fields 标注并跳过该过滤项。
    """
    filter_cfg = cfg.get("filter", {})
    ranking = cfg.get("ranking_factors", {})

    def getf(key, default=None):
        return q.get(key, default)

    # 字段名规范化: 腾讯行情 pe_ratio → pe_ttm 统一口径
    if q.get("pe_ttm") is None and q.get("pe_ratio") is not None:
        q = {**q, "pe_ttm": q["pe_ratio"]}

    current_price = getf("current_price")
    change_pct = getf("change_pct")
    volume_ratio = getf("volume_ratio")
    turnover_rate = getf("turnover_rate")
    # 2026-08-26 顺手修: 行情对象字段名是 turnover(成交额,元), 'amount' 恒为 None
    amount = getf("amount") or getf("turnover")  # 元
    open_p = getf("open")
    high = getf("high")
    low = getf("low")
    pe_ttm = getf("pe_ttm")
    if pe_ttm is None:
        pe_ttm = getf("pe_ratio")  # 腾讯行情字段名
    pb_ratio = getf("pb_ratio")
    market_cap = getf("total_market_value")  # 亿

    # 硬过滤 + 标注缺失项
    passed = True
    failed_filters = []
    missing_fields = []

    def check(name, actual, op, threshold):
        nonlocal passed
        if actual is None:
            # 2026-08-23 P3 修复: 字段缺失 = 无法验证 = 不通过(保守语义)。
            # 此前"跳过该条件"会让放量策略在无量能数据时裸筛全市场。
            missing_fields.append(name)
            passed = False
            failed_filters.append({"field": name, "actual": None, "required": op, "threshold": threshold})
            return
        ok = (
            (op == "min" and actual >= threshold)
            or (op == "max" and actual <= threshold)
        )
        if not ok:
            passed = False
            failed_filters.append({"field": name, "actual": actual, "required": op, "threshold": threshold})

    # 实时字段(2026-08-23 P3: 字段缺失时也进 check → missing 标注 + 不通过,
    # 此前的 "is not None" 守卫会让缺数据的票静默通过全部过滤)
    if "price_min" in filter_cfg:
        check("current_price", current_price, "min", filter_cfg["price_min"])
    if "price_max" in filter_cfg:
        check("current_price", current_price, "max", filter_cfg["price_max"])
    if "change_pct_min" in filter_cfg:
        check("change_pct", change_pct, "min", filter_cfg["change_pct_min"])
    if "change_pct_max" in filter_cfg:
        check("change_pct", change_pct, "max", filter_cfg["change_pct_max"])
    if "volume_ratio_min" in filter_cfg:
        check("volume_ratio", volume_ratio, "min", filter_cfg["volume_ratio_min"])
    if "volume_ratio_max" in filter_cfg:
        check("volume_ratio", volume_ratio, "max", filter_cfg["volume_ratio_max"])
    if "turnover_rate_min" in filter_cfg:
        check("turnover_rate", turnover_rate, "min", filter_cfg["turnover_rate_min"])
    if "turnover_rate_max" in filter_cfg:
        check("turnover_rate", turnover_rate, "max", filter_cfg["turnover_rate_max"])
    # 盘后字段(pe_ttm/pb/market_cap) — 既可能在 filter 里也可能在 cfg 顶层
    for prefix in ("pe_ttm", "pb", "market_cap"):
        for suffix in ("_min", "_max"):
            key = f"{prefix}{suffix}"
            threshold = filter_cfg.get(key)
            if threshold is None:
                threshold = cfg.get(key)  # 兜底从 cfg 顶层取(dual_low 写法)
            if threshold is None:
                continue
            actual_field = {"pe_ttm": "pe_ttm", "pb": "pb_ratio", "market_cap": "total_market_value"}[prefix]
            actual = getf(actual_field)
            if actual is None:
                missing_fields.append(actual_field)
                passed = False  # 2026-08-23 P3: 缺失 = 无法验证 = 不通过
                failed_filters.append({"field": actual_field, "actual": None, "required": suffix.lstrip("_"), "threshold": threshold})
            elif suffix == "_min" and actual < threshold:
                passed = False
                failed_filters.append({"field": actual_field, "actual": actual, "required": "min", "threshold": threshold})
            elif suffix == "_max" and actual > threshold:
                passed = False
                failed_filters.append({"field": actual_field, "actual": actual, "required": "max", "threshold": threshold})

    # 因子打分(简化版: 归一化到 0-100)
    score = 50.0
    score_breakdown = []
    if "low_pe" in ranking and pe_ttm is not None and pe_ttm > 0:
        # PE 越低分越高(PE=0 得 100, PE=30 得 0)
        s = max(0, min(100, 100 - pe_ttm * 3.3))
        # 2026-08-23 P3 修复: PE<3 多为一次性收益/ST 脱帽等异常, 封顶防价值陷阱排第一
        if pe_ttm < 3:
            s = min(s, 55.0)
        score += (s - 50) * ranking["low_pe"]
        score_breakdown.append({"factor": "low_pe", "raw": pe_ttm, "score": round(s, 1), "weight": ranking["low_pe"]})
    if "low_pb" in ranking and pb_ratio is not None and pb_ratio > 0:
        s = max(0, min(100, 100 - pb_ratio * 25))
        score += (s - 50) * ranking["low_pb"]
        score_breakdown.append({"factor": "low_pb", "raw": pb_ratio, "score": round(s, 1), "weight": ranking["low_pb"]})
    if "volume_ratio" in ranking and volume_ratio is not None:
        # 量比 1.0 = 50分, 2.0+ = 100, 0.5 = 0
        s = max(0, min(100, volume_ratio * 50))
        score += (s - 50) * ranking["volume_ratio"]
        score_breakdown.append({"factor": "volume_ratio", "raw": volume_ratio, "score": round(s, 1), "weight": rounding_safe(ranking["volume_ratio"])})
    if "change_pct" in ranking and change_pct is not None:
        # 涨跌幅 -5~+5% 映射到 0~100
        s = max(0, min(100, 50 + change_pct * 10))
        score += (s - 50) * ranking["change_pct"]
        score_breakdown.append({"factor": "change_pct", "raw": change_pct, "score": round(s, 1), "weight": rounding_safe(ranking["change_pct"])})
    if "turnover_rate" in ranking and turnover_rate is not None:
        # 换手率 0~8% 映射到 0~100
        s = max(0, min(100, turnover_rate * 12.5))
        score += (s - 50) * ranking["turnover_rate"]
        score_breakdown.append({"factor": "turnover_rate", "raw": turnover_rate, "score": round(s, 1), "weight": rounding_safe(ranking["turnover_rate"])})
    if "stable_amount" in ranking and amount is not None and amount > 0:
        # 成交额 1亿=50, 5亿+=100, 0.1亿=0
        s = max(0, min(100, 25 + 15 * (amount ** 0.3)))
        score += (s - 50) * ranking["stable_amount"]
        score_breakdown.append({"factor": "stable_amount", "raw": amount, "score": round(s, 1), "weight": rounding_safe(ranking["stable_amount"])})
    if "stable" in ranking and turnover_rate is not None and volume_ratio is not None:
        # 稳定 = 换手率中等 + 量比稳定(1附近)
        stability = 100 - abs(turnover_rate - 2.0) * 20 - abs(volume_ratio - 1.0) * 15
        s = max(0, min(100, stability))
        score += (s - 50) * ranking["stable"]
        score_breakdown.append({"factor": "stable", "raw": f"turnover={turnover_rate}, vol_ratio={volume_ratio}", "score": round(s, 1), "weight": rounding_safe(ranking["stable"])})
    if "oversold" in ranking and change_pct is not None:
        # 跌越多(超卖)分越高, change_pct=-5 → 100, +5 → 0
        s = max(0, min(100, 50 - change_pct * 10))
        score += (s - 50) * ranking["oversold"]
        score_breakdown.append({"factor": "oversold", "raw": change_pct, "score": round(s, 1), "weight": rounding_safe(ranking["oversold"])})
    if "reversal" in ranking and change_pct is not None:
        # 反转信号: 企稳度打分 — 涨/平(企稳)=满分, 继续跌按跌幅衰减(-5% → 50, -10% → 0)
        # 2026-08-23 P3 修复: 此前方向写反(涨 0 分跌加分, 与"企稳"语义相反)
        s = 100 if change_pct >= 0 else max(0, 100 + change_pct * 10)
        score += (s - 50) * ranking["reversal"]
        score_breakdown.append({"factor": "reversal", "raw": change_pct, "score": round(s, 1), "weight": rounding_safe(ranking["reversal"])})

    score = max(0, min(100, round(score, 1)))

    return {
        "strategy_id": strategy_id,
        "symbol": symbol,
        "market": market,
        "passed": passed,
        "score": score,
        "score_breakdown": score_breakdown,
        "failed_filters": failed_filters,
        "missing_fields": missing_fields,
        "current_data": {
            "current_price": current_price,
            "change_pct": change_pct,
            "volume_ratio": volume_ratio,
            "turnover_rate": turnover_rate,
            "amount": amount,
            "pe_ttm": pe_ttm,
            "pb_ratio": pb_ratio,
            "market_cap": market_cap,
        },
    }


def rounding_safe(v):
    if v is None:
        return 0
    try:
        return round(float(v), 2)
    except Exception:
        return 0