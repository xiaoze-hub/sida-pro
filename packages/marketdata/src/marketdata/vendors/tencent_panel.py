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


def fetch_price_bins(symbol: Symbol, limit: int = 70) -> dict | None:
    """分价表**原始五列** + 表头(交易日/快照时刻/笔数), 供分价博弈矩阵使用。

    为什么另起一个函数而不是改 `fetch_price_distribution`
    --------------------------------------------------
    老函数把第 4/5 列标成"委托买量/委托卖量", 但实测三列恒等式是
    `第2列 == 第1列 + 第4列 + 第5列`(sz002361/sh600519/sz300750 逐行成立),
    委托挂单量不可能与成交量逐行相加等于成交量 —— 该标注是错的。
    老函数还被 dark_flow 复用, 改字段名会波及既有链路, 故新增本函数只做**原始列透传**,
    语义解读收敛到 `src/core/price_distribution_analyzer.py` 一处(带口径披露)。

    真实结构(2026-10-18 实测):
        价 ~ 竞买量(手) ~ 总成交量(手) ~ 对手量(手) ~ 大单量(手)
        且 竞买量 + 对手量 + 大单量 == 总成交量(逐行, 误差 0~1 手为取整)
    返回::

        {
          "trade_date": "2026-10-16",       # 交易所快照日, **不是**调用日
          "snapshot_ts": "15:00:02",
          "ticks": 10839,                    # 表头第三列: 成交笔数
          "bins": [{"price": 8.84, "buy_vol": 660.0, "total_vol": 660.0,
                    "counter_vol": 0.0, "big_vol": 0.0}, ...],  # 按量降序, 最多 limit 档
          "identity_ok": 52, "identity_bad": 0,
        }
    失败/无数据 → None(不返回空壳, 让上层显式降级)。
    """
    code = _tencent_code(symbol)
    if not code:
        return None
    try:
        body = _get(f"https://stock.gtimg.cn/data/index.php?appn=price&c={code}&p=1")
        m = re.search(r'\[(\d{8}),(\d{6}),(\d+),"(.*)"\]', body, re.S)
        if not m:
            return None
        day, tm, ticks = m.group(1), m.group(2), int(m.group(3))
        bins: list[dict] = []
        ok = bad = 0
        for part in m.group(4).split("^"):
            p = part.split("~")
            if len(p) < 5:
                continue
            try:
                price = float(p[0])
                buy = float(p[1] or 0)
                total = float(p[2] or 0)
                counter = float(p[3] or 0)
                big = float(p[4] or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or total <= 0:
                continue
            # 逐行恒等式体检: 三列拆分必须能还原总成交量(容差 1 手, 覆盖取整)
            if abs((buy + counter + big) - total) <= 1.0:
                ok += 1
            else:
                bad += 1
            bins.append({
                "price": price,
                "buy_vol": buy,
                "total_vol": total,
                "counter_vol": counter,
                "big_vol": big,
            })
        if not bins:
            return None
        bins.sort(key=lambda r: (-r["total_vol"], -r["price"]))
        return {
            "trade_date": f"{day[:4]}-{day[4:6]}-{day[6:8]}",
            "snapshot_ts": f"{tm[:2]}:{tm[2:4]}:{tm[4:6]}",
            "ticks": ticks,
            "bins": bins[:limit],
            "identity_ok": ok,
            "identity_bad": bad,
        }
    except Exception as e:  # noqa: BLE001 - 网络/解析边界统一降级
        logger.warning(f"[tencent_panel] price bins {code} 失败: {e}")
        return None


def fetch_quote_brief(symbol: Symbol) -> dict | None:
    """行情快照摘要(收盘/昨收/涨跌幅/外盘/内盘/成交额) —— 分价分析的价格锚与交叉校验。

    外盘/内盘与分价表"竞买率"是**两套口径**(分价表按逐笔方向, 快照按价格跳动),
    仅用于交叉披露, 不参与任何计算(实测两者可差 5~10pp)。
    """
    code = _tencent_code(symbol)
    if not code:
        return None
    try:
        body = _get(f"https://qt.gtimg.cn/q={code}")
        m = re.search(r'"(.*)"', body, re.S)
        if not m:
            return None
        p = m.group(1).split("~")
        if len(p) < 39:
            return None

        def _f(idx: int) -> float | None:
            try:
                v = float(p[idx])
            except (TypeError, ValueError, IndexError):
                return None
            return v if v == v else None  # NaN 过滤

        total = _f(6)
        outer = _f(8)   # 外盘(主动买)
        inner = _f(7)   # 内盘(主动卖)
        return {
            "name": p[1] if len(p) > 1 else None,
            "close": _f(3),
            "prev_close": _f(4),
            "open": _f(5),
            "change_pct": _f(32),
            "high": _f(33),
            "low": _f(34),
            "total_vol": total,      # 手
            "outer_vol": outer,      # 手
            "inner_vol": inner,      # 手
            "amount_wan": _f(37),    # 成交额(万元)
            "quote_ts": p[30] if len(p) > 30 else None,
            "outer_ratio": round(outer / total, 4) if total and outer is not None else None,
            "inner_ratio": round(inner / total, 4) if total and inner is not None else None,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[tencent_panel] quote brief {code} 失败: {e}")
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
