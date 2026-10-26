# -*- coding: utf-8 -*-
"""I-8 盘中微观分价主力博弈矩阵与拆单检测(2026-10-18)。

为什么需要这一层
----------------
通达信 TQ 没有「分价(价位×主动买卖)」维度: 逐笔给方向, 却不给**价位聚合**; 十档只给
挂单不给成交。于是"主力在哪个价位真金白银承接/在哪个价位密集出货/哪一段是算法拆单
伪装"这三个实战问题, 现有链路全部答不了。腾讯分价表(`appn=price`)是免费源里唯一
按价位拆主动买卖的口径 —— 本模块把它做成**博弈矩阵**: 每个价位的多空力量、净买入额、
大单占比, 并从矩阵里判三类高确定性形态。

数据口径(实测校准, 与老 vendor 注释不同)
------------------------------------------
腾讯分价表每档 5 列, 逐行满足恒等式::

    价 ~ 竞买量(手) ~ 总成交量(手) ~ 对手量(手) ~ 大单量(手)
    竞买量 + 对手量 + 大单量 == 总成交量        # 容差 1 手(取整)

* **竞买率** = 竞买量 / 总成交量 —— 腾讯分价表原页面口径(2026-08-11 截图核对 55.87%)。
* **主动卖量** = 总成交量 − 竞买量(对手盘)。不直接用第 4 列: 第 4 列与大单量存在
  并列关系, 三列相加才等于总量, 单列当"委托卖量"会重复计数。
* **大单量** = 第 5 列, 方向不分(只用于"大单参与度", 不参与多空方向判定)。
* 行情快照的**外盘/内盘**与分价表竞买率是两套口径(实测可差 5~10pp), 只做交叉披露,
  **绝不**用来互相校准 —— 两套口径混算会造出一个谁也不认的数。

三类形态(全部可解释、可复算, 无黑箱)
------------------------------------
1. **强托举支撑位** `support`: 成交量前 3 且竞买率 ≥ 0.65 —— 放量且主动买压倒性,
   是真金承接, 不是挂单托盘(挂单在十档, 成交在这里)。
2. **密集抛压阻力位** `resistance`: 成交量前 3 且竞买率 ≤ 0.35 —— 放量且主动卖压倒性,
   是真实换手出货, 反弹到该价位会遇到套牢盘 + 当日卖压叠加。
3. **算法拆单伪装** `iceberg`: 成交量 ≥ max(3×中位量, 2% 全日量) 且大单占比 ≤ 10%
   且方向明确(|竞买率−50%| ≥ 10pp)。含义: 这个价位吃了巨量, 却没有一笔大单 ——
   只能由成百上千笔小单连续成交堆出来, 典型的 TWAP/冰山单拆单特征。方向由竞买率定:
   高 → 拆单吸筹; 低 → 拆单派发。

纯函数 + 注入式取数
-------------------
`build_matrix` 不碰网络/DB; `fetch_analysis` 的 `bins_fn` / `quote_fn` 可替换 ⇒ 单测
逐值锁死算法, 也不依赖腾讯可达性(海外 CI 常态超时)。
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: 至少要有这么多有效价位才出矩阵(低于此值多半是停牌/新股/解析异常)。
MIN_BINS = 5

#: 托举/抛压: 只看成交量前 N 档, 且方向占比达到阈值。
TOP_N = 3
SUPPORT_BUY_RATIO = 0.65
RESISTANCE_BUY_RATIO = 0.35

#: 拆单: 巨量 + 大单缺席 + 方向明确。
ICEBERG_BIG_RATIO_MAX = 0.10
ICEBERG_BIG_REL_MAX = 0.50       # 相对全日大单占比: 需低于其一半(scale-free, 抗源档位口径差异)
ICEBERG_VOL_MULT = 3.0          # 相对中位档成交量
ICEBERG_DAY_SHARE_MIN = 0.02    # 相对全日成交量
ICEBERG_DIRECTION_MIN = 0.10    # |竞买率 - 0.5|
#: 拆单档的最小成交额(元): 纯"3×中位量"在小票/高价股上会把几千手的小档也算巨量,
#: 加一道经济规模闸(2000 万元)—— 拆单吸筹/派发要有资金体量才有实战含义。
ICEBERG_MIN_AMOUNT = 2.0e7

#: 大单参与度分档(展示用)。
BIG_HEAVY_RATIO = 0.35

#: 默认取多少档(腾讯单次最多给全部档位, 热门股可达数百档; 70 档覆盖 95% 成交量)。
DEFAULT_LIMIT = 70

CALIBER = (
    "腾讯分价表 appn=price(逐行恒等式 竞买量+对手量+大单量=总成交量); "
    "竞买率=竞买量/总成交量(原页面口径); 主动卖量=总量−竞买量; "
    "大单量=第5列(方向不分, 仅作参与度); 单位: 手"
)

#: 1 手 = 100 股(A 股), 净额换算用。
SHARES_PER_LOT = 100


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _num(v: Any) -> Optional[float]:
    """任意值 → 有限 float; None/空串/NaN/inf 一律 None(**不补 0**)。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    try:
        f = float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _ratio(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    if part is None or whole is None or whole <= 0:
        return None
    return _clamp(part / whole, 0.0, 1.0)


def _pct(v: Optional[float], digits: int = 1) -> str:
    """比例 → 百分数文本; 缺值 '--'(后端统一格式化, 前端不再算口径)。"""
    return f"{v * 100:.{digits}f}%" if v is not None else "--"


def _price_txt(v: Optional[float]) -> str:
    return f"{v:.2f}" if v is not None else "--"


def _amount_txt(v: Optional[float]) -> str:
    """元 → 万/亿(带符号), 缺值 '--'。"""
    if v is None:
        return "--"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e8:
        return f"{sign}{a / 1e8:.2f}亿"
    if a >= 1e4:
        return f"{sign}{a / 1e4:.0f}万"
    return f"{sign}{a:.0f}"


# --------------------------------------------------------------------------- #
# 单档归一化
# --------------------------------------------------------------------------- #
def normalize_bin(
    raw: Dict[str, Any],
    day_vol: Optional[float] = None,
    *,
    open_price: Optional[float] = None,
    close_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """一档原始行 → 博弈矩阵行(含双向量/占比/净额/标签)。

    容错: 价格或总量缺失/非正 → None(该档直接丢, 不补 0 占位)。
    竞买量超过总量(个别脏数据) → 夹到总量并记 `clamped=True`, 不静默改数。

    **竞价污染档(undirected)**: 开盘价/收盘价这两档在腾讯分价表里混入了集合竞价
    (call auction 无 aggressor, 主动方向在数据上不可分离): 纯竞价档表现为 `竞买量=0`
    或 `竞买量=总量`, 而"竞价+连续"混合档则会被竞价量带偏方向。实测踩坑:
    600519@1329.00 / 601318@51.88 被读成"主动卖占比 100% 的密集抛压 + 拆单派发",
    300750@277.00(收盘价, 有双向)也被读成 93% 抛压 —— 全是竞价污染的假信号。

    处理: 开盘价/收盘价上成交量 ≥ 0.5% 全日量的档标 `undirected=True`, **不参与**
    托举/抛压/拆单判定, 也不计入全日竞买率(方向不可归因); 但仍保留在矩阵里
    (成交量是真的), 前端显示"竞价"徽标。宁可少一个信号, 不给一个假信号。
    """
    price = _num(raw.get("price"))
    total = _num(raw.get("total_vol"))
    if price is None or price <= 0 or total is None or total <= 0:
        return None
    buy = _num(raw.get("buy_vol"))
    big = _num(raw.get("big_vol"))
    clamped = False
    if buy is None:
        buy = 0.0
        clamped = True
    if buy > total:
        buy = total
        clamped = True
    if buy < 0:
        buy = 0.0
        clamped = True
    sell = total - buy
    if big is None or big < 0:
        big = 0.0
    if big > total:
        big = total

    buy_ratio = _ratio(buy, total)
    big_ratio = _ratio(big, total)
    net_vol = buy - sell
    net_amount = net_vol * price * SHARES_PER_LOT

    single_side = buy <= 0 or sell <= 0
    at_auction_price = False
    for ref in (open_price, close_price):
        r = _num(ref)
        if r is not None and abs(price - r) <= 0.0051:  # 半分钱以内视为同一价(取整)
            at_auction_price = True
            break
    # 门槛 0.5% 全日量: 极小档即便在开盘/收盘价上也只是噪声, 不必标; 竞价档通常 0.5%~10%
    undirected = bool(at_auction_price and day_vol and total >= day_vol * 0.005)

    return {
        "price": round(price, 3),
        "volume": round(total, 2),
        "buy_vol": round(buy, 2),
        "sell_vol": round(sell, 2),
        "big_vol": round(big, 2),
        "buy_ratio": round(buy_ratio, 4) if buy_ratio is not None else None,
        "sell_ratio": round(1 - buy_ratio, 4) if buy_ratio is not None else None,
        "big_ratio": round(big_ratio, 4) if big_ratio is not None else None,
        "net_vol": round(net_vol, 2),
        "net_amount": round(net_amount, 2),
        "turnover": round(total * price * SHARES_PER_LOT, 2),
        "day_share": round(_ratio(total, day_vol), 4) if day_vol else None,
        "counter_vol": round(_num(raw.get("counter_vol")) or 0.0, 2),
        "single_side": bool(single_side),
        "undirected": undirected,
        "clamped": clamped,
        "tags": [],
    }


# --------------------------------------------------------------------------- #
# 三类形态识别
# --------------------------------------------------------------------------- #
def _top_volume(bins: List[Dict[str, Any]], n: int = TOP_N) -> List[Dict[str, Any]]:
    return sorted(bins, key=lambda b: -(b.get("volume") or 0))[:n]


def detect_support_levels(bins: Iterable[Dict[str, Any]], top_n: int = TOP_N) -> List[Dict[str, Any]]:
    """【强托举支撑位】成交量前 N 且竞买率 ≥ 0.65。

    只认"放量 + 主动买压倒性": 缩量高价位的 90% 竞买率是噪声(几手成交就能刷出来),
    不构成支撑。返回值按 (竞买率, 成交量) 降序。
    """
    rows = [b for b in bins if isinstance(b, dict)]
    if not rows:
        return []
    out = []
    for b in _top_volume(rows, top_n):
        r = _num(b.get("buy_ratio"))
        if r is None or r < SUPPORT_BUY_RATIO:
            continue
        out.append({
            "kind": "support",
            "price": b.get("price"),
            "buy_ratio": r,
            "volume": b.get("volume"),
            "net_amount": b.get("net_amount"),
            "big_ratio": b.get("big_ratio"),
            "day_share": b.get("day_share"),
            "score": round(r * 0.7 + (_num(b.get("day_share")) or 0.0) * 0.3, 4),
        })
    out.sort(key=lambda x: (-x["buy_ratio"], -(x["volume"] or 0)))
    return out


def detect_resistance_levels(bins: Iterable[Dict[str, Any]], top_n: int = TOP_N) -> List[Dict[str, Any]]:
    """【密集抛压阻力位】成交量前 N 且竞买率 ≤ 0.35(主动卖 ≥ 65%)。"""
    rows = [b for b in bins if isinstance(b, dict)]
    if not rows:
        return []
    out = []
    for b in _top_volume(rows, top_n):
        r = _num(b.get("buy_ratio"))
        if r is None or r > RESISTANCE_BUY_RATIO:
            continue
        out.append({
            "kind": "resistance",
            "price": b.get("price"),
            "buy_ratio": r,
            "sell_ratio": round(1 - r, 4),
            "volume": b.get("volume"),
            "net_amount": b.get("net_amount"),
            "big_ratio": b.get("big_ratio"),
            "day_share": b.get("day_share"),
            "score": round((1 - r) * 0.7 + (_num(b.get("day_share")) or 0.0) * 0.3, 4),
        })
    out.sort(key=lambda x: (x["buy_ratio"], -(x["volume"] or 0)))
    return out


def detect_iceberg_signals(
    bins: Iterable[Dict[str, Any]],
    *,
    day_vol: Optional[float],
    big_data_present: bool,
    day_big_ratio: Optional[float] = None,
    max_results: int = 3,
) -> List[Dict[str, Any]]:
    """【算法拆单伪装】巨量价位但大单缺席。

    判据(全部需同时成立):
      · 该档成交量 ≥ max(3 × 中位档量, 2% 全日量)  —— "巨量"
      · 该档大单占比 ≤ 10% **且** ≤ 全日大单占比的一半 —— "大单缺席"
        (相对条件很关键: 第 5 列的绝对水平随个股/源档位口径漂移, 用绝对阈值会把
         全票普片低大单的票全部误报; 用"显著低于本票当日水平"才是可比口径)
      · |竞买率 − 50%| ≥ 10pp                       —— 方向明确(否则只是多空拉锯)

    `big_data_present=False`(源未给大单列)时**一律返回空**: 大单量为 0 与"源没这列"
    在数值上不可区分, 此时报"拆单"就是凭空捏造。
    """
    rows = [b for b in bins if isinstance(b, dict)]
    if not rows or not big_data_present:
        return []
    vols = [v for v in (_num(b.get("volume")) for b in rows) if v and v > 0]
    if not vols:
        return []
    med = statistics.median(vols)
    threshold = max(med * ICEBERG_VOL_MULT, (day_vol or 0.0) * ICEBERG_DAY_SHARE_MIN)
    if threshold <= 0:
        return []

    # 大单缺席的绝对上限: 有全日占比时取 min(10%, 全日的一半); 无则只用 10%
    big_cap = ICEBERG_BIG_RATIO_MAX
    if day_big_ratio is not None and day_big_ratio > 0:
        big_cap = min(ICEBERG_BIG_RATIO_MAX, day_big_ratio * ICEBERG_BIG_REL_MAX)

    out = []
    for b in rows:
        vol = _num(b.get("volume")) or 0.0
        if vol < threshold:
            continue
        if (_num(b.get("turnover")) or 0.0) < ICEBERG_MIN_AMOUNT:
            continue
        br = _num(b.get("big_ratio"))
        r = _num(b.get("buy_ratio"))
        if br is None or br > big_cap or r is None:
            continue
        if abs(r - 0.5) < ICEBERG_DIRECTION_MIN:
            continue
        side = "buy" if r > 0.5 else "sell"
        strength = abs(r - 0.5) * 2  # 0~1
        out.append({
            "kind": "iceberg",
            "side": side,
            "price": b.get("price"),
            "buy_ratio": r,
            "volume": vol,
            "turnover": b.get("turnover"),
            "big_ratio": br,
            "big_vol": b.get("big_vol"),
            "day_share": b.get("day_share"),
            "net_amount": b.get("net_amount"),
            "score": round((1 - br) * 0.6 + strength * 0.4, 4),
            "label": "算法拆单买入(吸筹)" if side == "buy" else "算法拆单卖出(派发)",
        })
    out.sort(key=lambda x: -x["score"])
    return out[:max_results]


def tag_bins(
    bins: List[Dict[str, Any]],
    support: List[Dict[str, Any]],
    resistance: List[Dict[str, Any]],
    iceberg: List[Dict[str, Any]],
) -> None:
    """把三类形态回写到对应档位的 `tags`(前端渲染徽标用, 不在前端重判)。"""
    by_price = {round(b["price"], 3): b for b in bins}
    for item, tag in (
        *((s, "strong_support") for s in support),
        *((r, "strong_resistance") for r in resistance),
        *((i, "iceberg_buy" if i["side"] == "buy" else "iceberg_sell") for i in iceberg),
    ):
        b = by_price.get(round(float(item["price"]), 3))
        if b is None:
            continue
        if tag not in b["tags"]:
            b["tags"].append(tag)
    for b in bins:
        if b.get("undirected"):
            b["tags"].append("undirected")
        elif b.get("single_side") and (_num(b.get("day_share")) or 0.0) >= 0.01:
            # 连续竞价里的单边档(该价位只有主动买 或 只有主动卖): 方向真实但样本单薄,
            # 只对 ≥1% 全日量的档打提示, 免得几十个零碎档全挂满徽标
            b["tags"].append("single_side")
        br = _num(b.get("big_ratio"))
        if br is not None and br >= BIG_HEAVY_RATIO and "big_heavy" not in b["tags"]:
            b["tags"].append("big_heavy")


# --------------------------------------------------------------------------- #
# 结论文案
# --------------------------------------------------------------------------- #
def build_summary(
    support: List[Dict[str, Any]],
    resistance: List[Dict[str, Any]],
    iceberg: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """三条实战结论(顺序固定: 托举 → 抛压 → 拆单)。

    前端直接渲染 `text`; 数字同时以结构化字段给出(前端格式走 safeFixed, 不重算口径)。
    没有命中时 `available=False` + 中性说明 —— 不硬凑一个价位。
    """
    s = support[0] if support else None
    r = resistance[0] if resistance else None
    i = iceberg[0] if iceberg else None
    return [
        {
            "key": "support",
            "icon": "🛡️",
            "title": "主力强托举位",
            "available": s is not None,
            "price": s["price"] if s else None,
            "ratio": s["buy_ratio"] if s else None,
            "volume": s["volume"] if s else None,
            "text": (
                f"{_price_txt(s['price'])} (主动买占比 {_pct(s['buy_ratio'])})"
                if s else "未检测到放量主动买托举位(前 3 大量档竞买率均 < 65%)"
            ),
        },
        {
            "key": "resistance",
            "icon": "⚡",
            "title": "密集抛压带",
            "available": r is not None,
            "price": r["price"] if r else None,
            "ratio": r["sell_ratio"] if r else None,
            "volume": r["volume"] if r else None,
            "text": (
                f"{_price_txt(r['price'])} (主动卖占比 {_pct(r['sell_ratio'])})"
                if r else "未检测到放量主动卖抛压带(前 3 大量档竞买率均 > 35%)"
            ),
        },
        {
            "key": "iceberg",
            "icon": "🕵️",
            "title": "算法拆单提示",
            "available": i is not None,
            "price": i["price"] if i else None,
            "side": i["side"] if i else None,
            "ratio": i["buy_ratio"] if i else None,
            "volume": i["volume"] if i else None,
            "text": (
                f"在 {_price_txt(i['price'])} 附近检测到"
                f"{'小单高频拆单吸筹' if i['side'] == 'buy' else '小单高频拆单派发'}"
                f"(该档 {i['volume']:.0f} 手 / {_amount_txt(_num(i.get('turnover')))}, "
                f"大单占比仅 {_pct(i['big_ratio'])})"
                if i else "未检测到拆单特征(巨量档均伴随大单成交, 或源未提供大单列)"
            ),
        },
    ]


def build_bias(day: Dict[str, Any]) -> Dict[str, Any]:
    """全日多空定性(只用竞买率与净额, 不做预测)。"""
    ratio = _num(day.get("buy_ratio"))
    net = _num(day.get("net_amount"))
    if ratio is None:
        return {"code": "unknown", "label": "数据不足", "net_amount": net}
    if ratio >= 0.55:
        return {"code": "net_buy", "label": "主动买占优", "net_amount": net}
    if ratio <= 0.45:
        return {"code": "net_sell", "label": "主动卖占优", "net_amount": net}
    return {"code": "balanced", "label": "多空拉锯", "net_amount": net}


# --------------------------------------------------------------------------- #
# 矩阵构建(纯函数)
# --------------------------------------------------------------------------- #
def _unavailable(reason: str, symbol: str = "", market: str = "CN") -> Dict[str, Any]:
    """降级骨架(形状与正常响应一致, 前端只走一条渲染路径)。"""
    return {
        "available": False,
        "reason": reason,
        "symbol": symbol,
        "market": market,
        "trade_date": None,
        "snapshot_ts": None,
        "caliber": CALIBER,
        "quote": None,
        "day": None,
        "bins": [],
        "support_levels": [],
        "resistance_levels": [],
        "iceberg_signals": [],
        "summary": build_summary([], [], []),
        "bias": {"code": "unknown", "label": "数据不足", "net_amount": None},
        "quality": {"bins": 0, "identity_ok_ratio": None, "degraded": True},
        "as_of": None,
        "source": "tencent_price",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_matrix(
    payload: Optional[Dict[str, Any]],
    *,
    symbol: str = "",
    market: str = "CN",
    quote: Optional[Dict[str, Any]] = None,
    limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """分价原始表 + 行情快照 → 微观博弈矩阵(纯函数, 无 I/O)。

    参数:
        payload: `tencent_panel.fetch_price_bins()` 的返回(bins 至少含 price/total_vol)。
        quote:   `fetch_quote_brief()` 的返回(收盘/涨跌幅), 可缺 —— 缺了只是少一个锚, 不影响矩阵。
        limit:   最多输出多少档(按量降序, 已由 vendor 截断; 这里再兜一次)。
    """
    if not payload or not payload.get("bins"):
        return _unavailable("腾讯分价表无数据(停牌/非交易时段/源不可用)", symbol, market)

    raw_bins = payload.get("bins") or []
    day_vol = sum((_num(b.get("total_vol")) or 0.0) for b in raw_bins) or None
    if not day_vol or day_vol <= 0:
        return _unavailable("分价表成交量合计为 0, 无法构建矩阵", symbol, market)

    bins: List[Dict[str, Any]] = []
    quote = quote or {}
    for raw in raw_bins[: max(1, int(limit))]:
        nb = normalize_bin(
            raw, day_vol,
            open_price=quote.get("open"), close_price=quote.get("close"),
        )
        if nb is not None:
            bins.append(nb)
    if len(bins) < MIN_BINS:
        return _unavailable(
            f"有效价档仅 {len(bins)} 个(需 ≥{MIN_BINS}), 样本不足不出矩阵", symbol, market
        )

    # 覆盖度: 只取前 limit 档时, 这些档能代表多少成交量(诚实披露, 不装作全量)
    bins_vol = sum(b["volume"] for b in bins)
    coverage = round(bins_vol / day_vol, 4) if day_vol else None

    # 方向可归因的档: 剔除集合竞价档(开盘/收盘价上的单向巨量)后再算多空与形态
    directed = [b for b in bins if not b.get("undirected")]
    directed_vol = sum(b["volume"] for b in directed)
    undirected_vol = bins_vol - directed_vol

    buy_vol = sum(b["buy_vol"] for b in bins)
    sell_vol = sum(b["sell_vol"] for b in bins)
    big_vol = sum(b["big_vol"] for b in bins)
    net_amount = sum(b["net_amount"] for b in bins)
    vwap = (sum(b["price"] * b["volume"] for b in bins) / bins_vol) if bins_vol > 0 else None

    day = {
        "total_vol": round(bins_vol, 2),
        "day_total_vol": round(day_vol, 2),
        "buy_vol": round(buy_vol, 2),
        "sell_vol": round(sell_vol, 2),
        "net_vol": round(buy_vol - sell_vol, 2),
        "net_amount": round(net_amount, 2),
        # 竞买率只算方向可归因的档: 竞价档(无主动方)混进来会把收盘竞价读成"砸盘"
        "directed_vol": round(directed_vol, 2),
        "undirected_vol": round(undirected_vol, 2),
        "undirected_share": round(_ratio(undirected_vol, bins_vol), 4) if bins_vol else None,
        "buy_ratio": (
            round(_ratio(sum(b["buy_vol"] for b in directed), directed_vol), 4)
            if directed_vol else None
        ),
        "big_vol": round(big_vol, 2),
        "big_ratio": round(_ratio(big_vol, bins_vol), 4) if bins_vol else None,
        "vwap": round(vwap, 3) if vwap else None,
        "coverage": coverage,
    }

    # 大单列是否真的有数据: 全日大单量为 0 ⇒ 视为该源未提供(不让 0 冒充"没有大单")
    big_data_present = big_vol > 0
    support = detect_support_levels(directed)
    resistance = detect_resistance_levels(directed)
    iceberg = detect_iceberg_signals(
        directed, day_vol=directed_vol or bins_vol, big_data_present=big_data_present,
        day_big_ratio=day["big_ratio"],
    )
    tag_bins(bins, support, resistance, iceberg)

    identity_ok = _num(payload.get("identity_ok"))
    identity_bad = _num(payload.get("identity_bad"))
    identity_ratio = None
    if identity_ok is not None and identity_bad is not None and (identity_ok + identity_bad) > 0:
        identity_ratio = round(identity_ok / (identity_ok + identity_bad), 4)

    quality = {
        "bins": len(bins),
        "limit": int(limit),
        "identity_ok_ratio": identity_ratio,
        "big_data_present": big_data_present,
        "coverage": coverage,
        "degraded": bool(identity_ratio is not None and identity_ratio < 0.95),
        "cross_check": None,
    }
    # 交叉披露(不参与计算): 分价表竞买率 vs 行情外盘占比 —— 两套口径, 差异属正常
    if quote and quote.get("outer_ratio") is not None and day["buy_ratio"] is not None:
        quality["cross_check"] = {
            "price_table_buy_ratio": day["buy_ratio"],
            "quote_outer_ratio": quote["outer_ratio"],
            "diff_pp": round((day["buy_ratio"] - quote["outer_ratio"]) * 100, 2),
            "note": "分价表竞买率(逐笔方向)与行情外盘(价格跳动)口径不同, 仅作披露, 不互校",
        }

    return {
        "available": True,
        "reason": None,
        "symbol": symbol,
        "market": market,
        "trade_date": payload.get("trade_date"),
        "snapshot_ts": payload.get("snapshot_ts"),
        "ticks": payload.get("ticks"),
        "caliber": CALIBER,
        "quote": quote,
        "day": day,
        "bins": bins,
        "support_levels": support,
        "resistance_levels": resistance,
        "iceberg_signals": iceberg,
        "summary": build_summary(support, resistance, iceberg),
        "bias": build_bias(day),
        "quality": quality,
        "as_of": payload.get("trade_date"),
        "source": "tencent_price",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- #
# 取数编排(注入式)
# --------------------------------------------------------------------------- #
def fetch_analysis(
    symbol: str,
    *,
    market: str = "CN",
    limit: int = DEFAULT_LIMIT,
    bins_fn: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
    quote_fn: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """取腾讯分价表 + 行情快照并产出矩阵。`bins_fn`/`quote_fn` 注入供单测。

    降级诚实: 任一侧异常都收敛成 `available=False` + `reason` 原文, 绝不返回估算值。
    """
    sym = str(symbol or "").strip()
    if not sym:
        return _unavailable("缺少标的代码", sym, market)
    if (market or "CN").upper() != "CN":
        return _unavailable("分价博弈矩阵依赖腾讯 A 股分价表, 仅支持 A 股(CN)", sym, market)

    try:
        from marketdata.symbol import Symbol

        mdsym = Symbol.parse(sym)
    except Exception as e:  # noqa: BLE001 - 代码非法时明确降级
        return _unavailable(f"标的代码无法解析: {e}", sym, market)

    try:
        if bins_fn is None:
            from marketdata.vendors.tencent_panel import fetch_price_bins as _fetch

            payload = _fetch(mdsym, limit=limit)
        else:
            payload = bins_fn(mdsym, limit=limit)
    except Exception as e:  # noqa: BLE001 - 网络边界
        logger.warning(f"分价矩阵[{sym}]取数失败: {e}")
        return _unavailable(f"腾讯分价表不可用: {e}", sym, market)

    if not payload:
        return _unavailable("腾讯分价表无数据(停牌/非交易时段/源不可用)", sym, market)

    quote = None
    try:
        if quote_fn is None:
            from marketdata.vendors.tencent_panel import fetch_quote_brief as _q

            quote = _q(mdsym)
        else:
            quote = quote_fn(mdsym)
    except Exception as e:  # noqa: BLE001 - 快照只是锚, 失败不影响矩阵
        logger.debug(f"分价矩阵[{sym}]行情快照失败(不致命): {e}")
        quote = None

    out = build_matrix(payload, symbol=sym, market=market, quote=quote, limit=limit)
    if quote and out.get("available"):
        out["name"] = quote.get("name")
        out["quote_ts"] = quote.get("quote_ts")
    return out
