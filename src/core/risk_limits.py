"""组合级风控闸门(W3/B3.1-B3.2, KI-038)。

模拟盘开仓前必须过闸: **账户回撤熔断 / 最大持仓只数 / 单票上限 / 总敞口上限**。
阈值走环境变量(可配置, 默认保守); 判定为纯函数, 便于单测与审计。

环境变量:
  SIDA_RISK_HALT_DRAWDOWN  账户回撤熔断线(默认 0.20 = 20%)
  SIDA_RISK_MAX_SINGLE     单票市值占净值上限(默认 0.20)
  SIDA_RISK_MAX_EXPOSURE   总持仓市值占净值上限(默认 0.90)
  SIDA_RISK_MAX_POSITIONS  最大持仓只数(默认 20)
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def env_float(name: str, default: float) -> float:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RiskLimits:
    halt_drawdown: float = 0.20
    max_single: float = 0.20
    max_exposure: float = 0.90
    max_positions: int = 20


def load_risk_limits() -> RiskLimits:
    return RiskLimits(
        halt_drawdown=env_float("SIDA_RISK_HALT_DRAWDOWN", 0.20),
        max_single=env_float("SIDA_RISK_MAX_SINGLE", 0.20),
        max_exposure=env_float("SIDA_RISK_MAX_EXPOSURE", 0.90),
        max_positions=int(env_float("SIDA_RISK_MAX_POSITIONS", 20)),
    )


def check_entry(
    *,
    limits: RiskLimits,
    account_drawdown_pct: float | None,
    equity: float,
    position_value: float,
    new_position_value: float,
    position_count: int,
) -> str | None:
    """开仓前置校验。返回 None = 放行, 否则返回拒绝原因码。

    原因码: no_equity / drawdown_halt / max_positions / max_exposure / max_single
    """
    if equity <= 0:
        return "no_equity"
    if float(account_drawdown_pct or 0.0) >= float(limits.halt_drawdown):
        return "drawdown_halt"
    if int(position_count) >= int(limits.max_positions):
        return "max_positions"
    if (float(position_value) + float(new_position_value)) / equity > float(limits.max_exposure) + 1e-9:
        return "max_exposure"
    if float(new_position_value) / equity > float(limits.max_single) + 1e-9:
        return "max_single"
    return None
