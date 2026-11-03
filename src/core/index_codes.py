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


# 指数所属交易所。**不能按代码首位推断**：`000300`(沪深300)/`000852`(中证1000) 在**上交所**，
# 而个股的 `000xxx` 在深交所。`exday_structure._tq_code()` 按首位判市，对指数会把
# 000300 判成 `.SZ`（TQ 返回空）—— 指数必须走这张表拿后缀（2026-11-03 实测踩到）。
INDEX_EXCHANGE: dict[str, str] = {
    "000001": "SH",
    "000016": "SH",
    "000300": "SH",
    "000688": "SH",
    "000852": "SH",
    "000905": "SH",
    "399001": "SZ",
    "399006": "SZ",
}

# 盘面总览展示顺序：大盘 → 权重 → 中小盘 → 成长
BOARD_ORDER: list[str] = [
    "000001",
    "399001",
    "399006",
    "000300",
    "000016",
    "000905",
    "000852",
    "000688",
]


def tq_index_code(symbol: str) -> str | None:
    """裸指数码 → TQ 带后缀代码（`000300` → `000300.SH`）；非指数返回 None。"""
    s = str(symbol or "").strip().upper()
    if "." in s:
        head, _, tail = s.partition(".")
        if head in INDEX_CODES and tail in ("SH", "SZ"):
            return s
        return None
    ex = INDEX_EXCHANGE.get(s)
    return f"{s}.{ex}" if ex else None
