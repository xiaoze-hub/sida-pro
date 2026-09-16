"""股票外部链接生成工具：根据股票代码、市场和用户选择的平台生成行情页 URL。

全局设置 key: stock_link_platform (默认 xueqiu)
audit P2 2026-09-15: 删除死函数 get_platform / stock_url(无生产调用),
URL 生成收敛到 _xueqiu_url, 对外只暴露 stock_link_markdown。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 支持的平台 {code: 中文名}
PLATFORMS = {
    "xueqiu": "雪球",
}

DEFAULT_PLATFORM = "xueqiu"
SETTING_KEY = "stock_link_platform"


def stock_link_markdown(symbol: str, market: str, platform: str = "") -> str:
    """生成 Markdown 格式的股票链接: [002837.CN](https://xueqiu.com/S/SZ002837)

    platform 参数保留用于向后兼容(当前仅支持 xueqiu, 传入其他值也走雪球兜底)。
    """
    code = f"{symbol}.{market}"
    url = _xueqiu_url(symbol, market.upper())
    return f"[{code}]({url})"


# ---------------------------------------------------------------------------
# 各平台 URL 生成
# ---------------------------------------------------------------------------

def _xueqiu_url(symbol: str, market: str) -> str:
    if market == "US":
        return f"https://xueqiu.com/S/{symbol}"
    if market == "HK":
        return f"https://xueqiu.com/S/{symbol}"
    # CN A股
    from src.core.cn_symbol import get_cn_prefix
    prefix = get_cn_prefix(symbol, upper=True)
    return f"https://xueqiu.com/S/{prefix}{symbol}"
