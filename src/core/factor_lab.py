"""因子工厂核心(B6.1): 因子注册表 + 横截面分层回测。

动机: `factor_eval` 只算 IC/IR, 缺"因子注册"与"分层(quantile)回测"两件基础设施,
新增因子要散落改代码, 也无法回答"因子单调性如何"。

本模块提供:
- `register_factor` / `FACTOR_REGISTRY`: 因子登记(代码/名称/方向/类别/计算函数);
- `cross_section_quantiles`: 按日横截面分 N 层, 返回各层平均收益与单调性;
- `factor_summary`: 组合分层结果, 输出 long_short(顶层次-底层次)与单调性判定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactorSpec:
    code: str
    name: str
    direction: int = 1          # 1 = 值越大越看多; -1 = 值越大越看空(如风险类)
    category: str = "generic"
    description: str = ""


@dataclass
class _Registry:
    specs: dict[str, FactorSpec] = field(default_factory=dict)
    compute: dict[str, Callable] = field(default_factory=dict)


_REGISTRY = _Registry()


def register_factor(spec: FactorSpec, fn: Callable | None = None) -> Callable:
    """登记因子。可作装饰器: `@register_factor(FactorSpec(...))`。"""
    if spec.code in _REGISTRY.specs:
        raise ValueError(f"因子已登记: {spec.code}")
    _REGISTRY.specs[spec.code] = spec
    if fn is not None:
        _REGISTRY.compute[spec.code] = fn
        return fn

    def _wrap(f: Callable) -> Callable:
        _REGISTRY.compute[spec.code] = f
        return f

    return _wrap


def factor_registry() -> dict[str, FactorSpec]:
    return dict(_REGISTRY.specs)


def factor_fn(code: str) -> Callable | None:
    return _REGISTRY.compute.get(code)


def cross_section_quantiles(
    values_by_symbol: dict[str, float],
    returns_by_symbol: dict[str, float],
    *,
    n_quantiles: int = 5,
) -> dict:
    """单日横截面分层: 按因子值分 N 层, 返回各层平均收益与单调性。

    只统计同时有因子值与收益的标的; 样本不足 N 层时按实际样本数分。
    """
    pairs = [
        (float(values_by_symbol[s]), float(returns_by_symbol[s]))
        for s in values_by_symbol
        if s in returns_by_symbol
        and values_by_symbol[s] is not None
        and returns_by_symbol[s] is not None
    ]
    if len(pairs) < 2:
        return {"n": len(pairs), "quantiles": [], "monotonic": None, "long_short": None}

    pairs.sort(key=lambda x: x[0])
    k = max(1, min(int(n_quantiles), len(pairs)))
    size = len(pairs) // k
    buckets: list[list[float]] = []
    start = 0
    for i in range(k):
        end = len(pairs) if i == k - 1 else start + size
        buckets.append([r for _v, r in pairs[start:end]])
        start = end

    quantiles = [
        {"bucket": i + 1, "n": len(b), "avg_return": round(sum(b) / len(b), 6) if b else None}
        for i, b in enumerate(buckets)
    ]
    avgs = [q["avg_return"] for q in quantiles if q["avg_return"] is not None]
    monotonic = None
    if len(avgs) >= 2:
        monotonic = all(avgs[i] <= avgs[i + 1] for i in range(len(avgs) - 1))
    long_short = round(avgs[-1] - avgs[0], 6) if len(avgs) >= 2 else None
    return {
        "n": len(pairs),
        "quantiles": quantiles,
        "monotonic": monotonic,
        "long_short": long_short,
    }


def factor_summary(
    daily_values: dict[str, dict[str, float]],
    daily_returns: dict[str, dict[str, float]],
    *,
    n_quantiles: int = 5,
) -> dict:
    """多日分层汇总: 每日分层后取各层平均收益的均值 + 单调天数占比。

    daily_values / daily_returns: {date: {symbol: value}}
    """
    per_day: list[dict] = []
    for d in sorted(set(daily_values) & set(daily_returns)):
        res = cross_section_quantiles(
            daily_values[d], daily_returns[d], n_quantiles=n_quantiles
        )
        if res["n"] >= 2:
            per_day.append({"date": d, **res})
    if not per_day:
        return {"days": 0, "quantiles": [], "monotonic_ratio": None, "avg_long_short": None}

    k = max((len(x["quantiles"]) for x in per_day), default=0)
    bucket_avgs: list[list[float]] = [[] for _ in range(k)]
    for x in per_day:
        for q in x["quantiles"]:
            if q["avg_return"] is not None and q["bucket"] - 1 < k:
                bucket_avgs[q["bucket"] - 1].append(q["avg_return"])
    quantiles = [
        {
            "bucket": i + 1,
            "days": len(vals),
            "avg_return": round(sum(vals) / len(vals), 6) if vals else None,
        }
        for i, vals in enumerate(bucket_avgs)
    ]
    mono_days = sum(1 for x in per_day if x["monotonic"])
    ls = [x["long_short"] for x in per_day if x["long_short"] is not None]
    return {
        "days": len(per_day),
        "quantiles": quantiles,
        "monotonic_ratio": round(mono_days / len(per_day), 4),
        "avg_long_short": round(sum(ls) / len(ls), 6) if ls else None,
    }
