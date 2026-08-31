"""Shadow Account 数据契约(frozen dataclasses)。

移植自 HKUDS/Vibe-Trading (MIT) agent/src/shadow_account/models.py,按数智分析
A 股聚焦裁剪(去掉 us/hk/crypto 市场枚举,保留 china_a)。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

#: 确定性价格上下文特征名(extractor 计算 / codegen 展平 / scanner 实时求值)。
PRICE_FEATURES: tuple[str, ...] = ("entry_rsi14", "prior_5d_return")


@dataclass(frozen=True)
class ShadowRule:
    """一条从盈利回合提炼的人话 if-then 规则。

    Attributes:
        rule_id: 稳定 ID 如 "R1"/"R2"。
        human_text: 中文自然语言描述(≤30 字)。
        entry_condition: 结构化入场条件 dict。键是特征名;值是标量或 (op, value) 元组。
        exit_condition: 结构化出场条件 dict。
        holding_days_range: (min, max) 持仓天数。
        support_count: 支持该规则的盈利回合数。
        coverage_rate: 覆盖盈利回合的比例。
        sample_trades: 代表样本 "<symbol>@<date>" 串。
        weight: 信号权重(默认 1.0)。
    """

    rule_id: str
    human_text: str
    entry_condition: dict[str, Any]
    exit_condition: dict[str, Any]
    holding_days_range: tuple[int, int]
    support_count: int
    coverage_rate: float
    sample_trades: tuple[str, ...]
    weight: float = 1.0


@dataclass(frozen=True)
class ShadowProfile:
    """用户交易行为影子画像。

    Attributes:
        shadow_id: "shadow_<8-hex>" 唯一 ID。
        created_at: ISO8601 UTC 时间戳。
        journal_hash: 源交割单内容 SHA1(幂等)。
        source_market: 主市场(当前恒为 "china_a")。
        profitable_roundtrips: 用于提取规则的盈利回合数。
        total_roundtrips: 交割单完整回合总数。
        date_range: (start_iso, end_iso)。
        profile_text: 一段式中文画像(Section 1)。
        rules: 3-5 条 ShadowRule。
        preferred_markets: 实际交易过的市场(频次排序)。
        typical_holding_days: (median, p75) 持仓天数。
    """

    shadow_id: str
    created_at: str
    journal_hash: str
    source_market: str
    profitable_roundtrips: int
    total_roundtrips: int
    date_range: tuple[str, str]
    profile_text: str
    rules: tuple[ShadowRule, ...]
    preferred_markets: tuple[str, ...]
    typical_holding_days: tuple[float, float]

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON-safe dict。"""
        return asdict(self)


@dataclass(frozen=True)
class AttributionBreakdown:
    """用户真实交易与影子之间 delta PnL 归因。

    所有 PnL 字段单位为交割单记账货币(人民币)。
    """

    missed_signals_pnl: float
    noise_trades_pnl: float
    early_exit_pnl: float
    late_exit_pnl: float
    overtrading_pnl: float
    counterfactual_trades: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ShadowBacktestResult:
    """影子回测 + 归因输出。"""

    shadow_id: str
    per_market: dict[str, dict[str, float]]
    combined: dict[str, float]
    equity_curves: dict[str, list[tuple[str, float]]]
    attribution: AttributionBreakdown
    shadow_total_pnl: float
    real_total_pnl: float
    delta_pnl: float

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON-safe dict。"""
        return asdict(self)
