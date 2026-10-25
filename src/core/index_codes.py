"""指数代码表（单一事实源，2026-10-25）。

**为什么要独立成文件**：`000001` 双关 —— 既是上证指数，也是平安银行(深市)。
这个歧义已经造成两次真实事故：
  1. `stock.kline` 域传 000001 命中 klines 里的平安银行 → 参谋答「上证没有趋势下跌」（v0.58.0 修）
  2. `get_trend_profile` 同样命中平安银行 → 把「60日 +5.31%」当成上证报给用户（v0.60.0 修）

两份代码各写一份映射就会继续漏，所以收成一处：谁要判指数，就 import 这里。
"""

from __future__ import annotations

# 与 src/web/api/market.py::MARKET_INDICES、src/web/api/quotes.py::get_minute(is_index)
# 保持同源。新增指数请三处一起加。
INDEX_CODES: dict[str, str] = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000300": "沪深300",
    "000016": "上证50",
    "000905": "中证500",
    "000852": "中证1000",
    "000688": "科创50",
}


def is_index_code(symbol: str, market: str = "CN") -> bool:
    """是否 A 股指数代码。只对 CN 生效（港股/美股的 000001 是另一回事）。"""
    return (market or "CN").upper() == "CN" and str(symbol or "").strip() in INDEX_CODES


def index_name(symbol: str) -> str:
    return INDEX_CODES.get(str(symbol or "").strip(), "")
