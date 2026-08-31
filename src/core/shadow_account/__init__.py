"""Shadow Account — 从用户自己的交割单提炼交易影子。

公共 API(供 API 端点/测试/外部调用):
    extract_shadow_profile: 交割单 → ShadowProfile(规则画像)
    run_shadow_attribution: 影子 vs 真实 PnL 差值归因
    render_shadow_report:   HTML/PDF 报告
    compute_profile / compute_behavior: 行为画像
"""

from src.core.shadow_account.backtester import (
    run_shadow_attribution,
    summarize_result,
)
from src.core.shadow_account.extractor import extract_shadow_profile
from src.core.shadow_account.journal import (
    compute_behavior,
    compute_profile,
    pair_trades_fifo,
)
from src.core.shadow_account.models import (
    AttributionBreakdown,
    ShadowBacktestResult,
    ShadowProfile,
    ShadowRule,
)
from src.core.shadow_account.parsers import (
    TradeRecord,
    detect_format,
    load_dataframe,
    parse_file,
    records_to_dataframe,
)
from src.core.shadow_account.reporter import render_shadow_report

__all__ = [
    "AttributionBreakdown",
    "ShadowBacktestResult",
    "ShadowProfile",
    "ShadowRule",
    "TradeRecord",
    "compute_behavior",
    "compute_profile",
    "detect_format",
    "extract_shadow_profile",
    "load_dataframe",
    "pair_trades_fifo",
    "parse_file",
    "records_to_dataframe",
    "render_shadow_report",
    "run_shadow_attribution",
    "summarize_result",
]
