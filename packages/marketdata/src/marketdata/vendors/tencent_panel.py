"""腾讯证券盘口面板 vendor(2026-08-11 接入): 盘口大单占比 + 大单分档统计。

端点(网页端 gu.qq.com/sz002361/gp/dadan 等页面同源):
- s_pk   : qt.gtimg.cn/q=s_pk{code}      → 买盘大单/买盘小单/卖盘大单/卖盘小单占比
- dadan  : stock.gtimg.cn/data/index.php?appn=dadan&c={code}&p=1
             → 大单分档统计(档位1-13: 单数/股数/金额/买/卖)
- price  : stock.gtimg.cn/data/index.php?appn=price&c={code}&p=1
             → 分价表(各价位成交量/占比)

用途: 盘中监测资金面补充 —— 判断"主力大单扫货 vs 散户接盘"、"大单净买入档位分布"。
"""
from __future__ import annotations

import logging
import re
import urllib.request

from marketdata.symbol import Symbol

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}


def _tencent_code(symbol: Symbol) -> str | None:
    code = (symbol.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return None
    if code[0] in ("6", "9") or code.startswith("688"):
        return f"sh{code}"
    if code[0] in ("0", "2", "3"):
        return f"sz{code}"
    return None


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("gbk", "replace")


def fetch_pan_analysis(symbol: Symbol) -> dict | None:
    """盘口大单占比: {buy_big, buy_small, sell_big, sell_small} 百分比。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    try:
        body = _get(f"https://qt.gtimg.cn/q=s_pk{code}")
        m = re.search(r'"([\d.~]+)"', body)
        if not m:
            return None
        parts = m.group(1).split("~")
        if len(parts) < 4:
            return None
        return {
            "buy_big": round(float(parts[0]) * 100, 1),    # 买盘大单占比 %
            "buy_small": round(float(parts[1]) * 100, 1),  # 买盘小单占比 %
            "sell_big": round(float(parts[2]) * 100, 1),   # 卖盘大单占比 %
            "sell_small": round(float(parts[3]) * 100, 1), # 卖盘小单占比 %
        }
    except Exception as e:
        logger.warning(f"[tencent_panel] s_pk {code} 失败: {e}")
        return None


def fetch_big_order_stats(symbol: Symbol) -> list[dict] | None:
    """大单分档统计: [{tier, count, shares, amount, buy, sell}] (档位1-13)。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    try:
        body = _get(f"https://stock.gtimg.cn/data/index.php?appn=dadan&c={code}&p=1")
        m = re.search(r"=\[(\d+),\"(\d+)\",\"(\d+)\",(\[.*\])\]", body, re.S)
        if not m:
            return None
        rows = []
        raw_list = m.group(4)
        # 解析 [[1, 2323, 1350574, '158674.75', 598353, 628981, 123240], ...]
        items = re.findall(r"\[(\d+),\s*(\d+),\s*(\d+),\s*'([\d.]+)',\s*(\d+),\s*(\d+),\s*(\d+)\]", raw_list)
        for it in items:
            tier, count, shares, amount, buy, sell, _unk = it
            rows.append({
                "tier": int(tier),
                "count": int(count),
                "shares": int(shares),
                "amount_wan": float(amount),     # 金额(万)
                "buy": int(buy),                  # 买入(股)
                "sell": int(sell),                # 卖出(股)
            })
        return rows or None
    except Exception as e:
        logger.warning(f"[tencent_panel] dadan {code} 失败: {e}")
        return None


def fetch_price_distribution(symbol: Symbol, limit: int = 10) -> list[dict] | None:
    """分价表: 各价位成交量/主动买量/竞买率, 按量降序取前 limit 个。

    真实字段(2026-08-11 截图破解):
      价~主动买量(手)~总成交量(手)~委托买量~委托卖量
      竞买率 = 主动买量/总成交量 (11.84: 56652/101403=55.87% ✓ 截图)
    """
    code = _tencent_code(symbol)
    if not code:
        return None
    try:
        body = _get(f"https://stock.gtimg.cn/data/index.php?appn=price&c={code}&p=1")
        m = re.search(r'\[(\d+),(\d+),(\d+),"(.+)"\]', body)
        if not m:
            return None
        data = m.group(4)
        rows = []
        for part in data.split("^"):
            p = part.split("~")
            if len(p) >= 5 and p[0] and p[2]:
                try:
                    total_vol = float(p[2])     # 总成交量(手)
                    buy_vol = float(p[1])       # 主动买量(手)
                    rows.append({
                        "price": float(p[0]),
                        "volume": total_vol,          # 总成交量(手)
                        "buy_volume": buy_vol,        # 主动买量(手)
                        "buy_ratio": round(buy_vol / total_vol, 4) if total_vol else None,  # 竞买率
                        "bid_vol": float(p[3]) if p[3] else 0,   # 委托买量
                        "ask_vol": float(p[4]) if p[4] else 0,   # 委托卖量
                    })
                except (ValueError, IndexError, TypeError):
                    continue
        rows.sort(key=lambda r: -r["volume"])
        return rows[:limit] or None
    except Exception as e:
        logger.warning(f"[tencent_panel] price {code} 失败: {e}")
        return None
