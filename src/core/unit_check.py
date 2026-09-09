"""量价金额恒等式出口校验(风险整改 3.5/B5)。

红线(AGENTS.md 单位约束): vol(股) × price ≈ amt(元)。对不上先怀疑单位换算
(手↔股 ×100、万元↔元 ×10⁴、百分点↔小数 ×100 —— P1-13 资金流占比 ×100 bug 即同类)。

**只在不复权数据上校验**: qfq/hfq 柱的 close 按除权因子调整而 amount/volume
恒为原始值, 恒等式天然破缺 —— 实测(2026-09-09, 东财 push2his 30 股×20 日):
  不复权 fqt=0: dev max=1.28% / p95=1.02% / p50=0.29%
  前复权 fqt=1: 000651 除息前柱 dev≈5.6%(除权因子所致, 非单位错误)
tol 由此实测校准(方案 B5 明确禁止拍脑帒 2%): 5.0% ≈ 4× 实测 max, 同时 20×
低于最小单位错误(×2 换算 → dev≈100%)的量级。证据存档 docs/_frozen/data.md。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# docs/_frozen/data.md 实测校准; SIDA_UNIT_TOL_PCT 可覆盖(0.01-100 合理域)
DEFAULT_TOL_PCT = 5.0


class UnitInconsistencyError(ValueError):
    """量价金额恒等式超阈(疑似单位换算错误, B5)。"""

    def __init__(self, symbol: str, dev_pct: float) -> None:
        self.symbol = symbol
        self.dev_pct = dev_pct
        super().__init__(
            f"单位口径异常 {symbol}: amount/volume 与 close 偏差 {dev_pct:.2f}% "
            f"(超 tol, 疑似手↔股/万元↔元类单位错误)"
        )


def unit_deviation(
    *, volume: float | None, close: float | None, amount: float | None
) -> float | None:
    """dev = |amount/volume − close| / close(百分数)。缺失字段返回 None
    (缺失走 1.1 的 None 语义, 不当 0 —— 0 会把停牌/缺数据误判成单位错误)。"""
    if not volume or not amount or not close:
        return None
    implied = amount / volume
    if close <= 0:
        return None
    return abs(implied - close) / close * 100.0


def assert_unit_consistency(
    *,
    symbol: str,
    trade_date: str = "",
    volume: float | None,
    close: float | None,
    amount: float | None,
    source: str = "",
    tol_pct: float | None = None,
) -> float | None:
    """恒等式校验(方案 B5 出口规范): 超阈 → error 日志 + 数据源失败计数
    (复用 0.2 record_datasource_failure, kind=parse, 经 vendor 失败桥或直调);
    SIDA_STRICT_UNITS=1 时抛 UnitInconsistencyError(fail-closed, 供入库门禁)。

    返回 dev 百分数; 字段缺失返回 None(跳过, 不校验)。
    """
    dev = unit_deviation(volume=volume, close=close, amount=amount)
    if dev is None:
        return None
    tol = tol_pct if tol_pct is not None else _tol_from_env()
    if dev <= tol:
        return dev

    logger.error(
        "单位口径异常 %s %s: amount/volume=%.4f vs close=%.4f (dev=%.2f%% > tol %.2f%%, source=%s)",
        symbol, trade_date, (amount or 0) / (volume or 1), close, dev, tol, source or "-",
    )
    _record_failure(source)
    if os.environ.get("SIDA_STRICT_UNITS") == "1":
        raise UnitInconsistencyError(symbol, dev)
    return dev


def _tol_from_env() -> float:
    raw = os.environ.get("SIDA_UNIT_TOL_PCT")
    if raw:
        try:
            v = float(raw)
            if 0.01 <= v <= 100:
                return v
        except ValueError:
            pass
    return DEFAULT_TOL_PCT


def _record_failure(source: str) -> None:
    """落一条数据源解析失败明细(中立层, KI-039 第二阶段: 不再依赖 web 层)。

    明细与 Prometheus 计数由 web 侧 health 模块各自桥接; 这里只写可查明细
    (datasource_failures 表, 内部 60s 限流)。
    """
    try:
        from src.core.datasource_failures import record as _record

        _record(source or "unknown", kind="parse")
    except Exception:  # noqa: BLE001
        pass
