"""货币结算的 Decimal 原语(风险整改 3.5/B5)。

分界(AGENTS.md 单位红线): 结算/持仓/盈亏的**金额运算**一律走本模块 Decimal,
量化到分再落库; 行情展示/报价等非结算路径留 float(性能), 汇率见各出口注释。
DB 列维持 Float(数值迁移另行评估), 入库值 = Decimal 精确计算 → q2/q4 量化 →
float 转存(≤4 位小数经 str 往返在 float 64 位精度内无损)。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN

_CENTS = Decimal("0.01")
_P4 = Decimal("0.0001")
_P6 = Decimal("0.000001")


def to_dec(v: float | int | str | Decimal | None) -> Decimal:
    """float/str → Decimal(经 str, 避免 0.1 二进制漂移)。None 视为 0。"""
    if v is None:
        return Decimal(0)
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def q2(d: Decimal) -> Decimal:
    """量化到分(银行家舍入, 与原 round() 语义一致)。"""
    return d.quantize(_CENTS, rounding=ROUND_HALF_EVEN)


def q4(d: Decimal) -> Decimal:
    """量化到 0.0001(成本拆解项历史精度)。"""
    return d.quantize(_P4, rounding=ROUND_HALF_EVEN)


def q6(d: Decimal) -> Decimal:
    """量化到 0.000001(成交价含滑点历史精度)。"""
    return d.quantize(_P6, rounding=ROUND_HALF_EVEN)
