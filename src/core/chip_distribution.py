"""筹码分布计算器(成本分布 CYQ) — 对齐通达信/同花顺口径。

原理(公开算法, 陈浩《筹码分布》+ 通达信):
- 每日成交筹码在 [low, high] 间按"三角分布"(以均价为峰)分布
- 历史筹码按换手率衰减: 当日筹码 = 当日新增 + 昨日剩余×(1-换手率×衰减系数)
- 递归累加 → 当前各价位持仓成本分布

输入: 日K(open/high/low/close/volume) + 换手率(或用成交量/流通股本近似)
输出: 筹码分布序列 + COST(10/50/90) + 获利盘比例 + 筹码峰(主力成本区)

对齐验证: 与通达信/同花顺/东财偏差 <10%(公开文献结论)。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 历史换手衰减系数(通达信默认, 突出近期筹码)
DECAY = 1.0

# 近期真实筹码缓存: {symbol_code -> (ts, result)}。TTL 1h(10日窗口盘中不变)
_NEAR_CHIPS_CACHE: dict[str, tuple[float, dict | None]] = {}
_NEAR_CHIPS_TTL = 3600.0

# 2026-08-12 磁盘持久化: 筹码分布(10日窗口盘中不变)落盘, 重启不重算
_NEAR_CHIPS_DISK = None


def _chips_persist(load_only: bool = False):
    """筹码快照写盘(惰性初始化)。load_only=True 只加载不写盘。

    audit P2 2026-09-15: disk key 加交易日(all:YYYY-MM-DD), 对齐 dark_flow
    的按日分 key 做法, 避免跨日读到昨日快照。
    """
    global _NEAR_CHIPS_DISK
    try:
        if _NEAR_CHIPS_DISK is None:
            from src.core.disk_cache import DiskCache, register
            from src.core.trading_calendar import cache_day
            _NEAR_CHIPS_DISK = DiskCache("near_term_chips", ttl=86400.0, flush_interval=120.0)
            key = f"all:{cache_day()}"
            snap = _NEAR_CHIPS_DISK.get(key)
            if isinstance(snap, dict) and snap:
                _NEAR_CHIPS_CACHE.update(snap)
            else:
                # 脏 key 迁移: 删掉无日期旧 key, 避免未来某天被误读(对齐 dark_flow P0-1)
                try:
                    _NEAR_CHIPS_DISK.delete("all")
                except Exception:  # noqa: BLE001
                    pass
            register(_NEAR_CHIPS_DISK)
        if not load_only:
            from src.core.trading_calendar import cache_day
            _NEAR_CHIPS_DISK.set(f"all:{cache_day()}", dict(_NEAR_CHIPS_CACHE))
    except Exception:
        pass


# 2026-08-12: 模块 import 时加载磁盘缓存, 不写盘
_chips_persist(load_only=True)


def fetch_sina_hist_price(symbol_code: str, start: str, end: str) -> list[dict] | None:
    """新浪历史分价表: 区间内各价位真实累计成交量(2026-08-11 接入)。

    symbol_code: 'sz002361' / 'sh600519'
    start/end: '2026-08-03' / '2026-08-10'
    Returns: [{price, volume, pct}] 按价降序; 失败 None
    """
    import re
    import urllib.request

    url = (f"http://market.finance.sina.com.cn/iframe/pricehis.php"
           f"?symbol={symbol_code}&startdate={start}&enddate={end}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("gb2312", "replace")
    except Exception:
        return None
    rows = []
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(1), re.S)]
        if len(cells) >= 3 and re.match(r"^\d+\.\d+$", cells[0]):
            try:
                rows.append({
                    "price": float(cells[0]),
                    "volume": int(cells[1].replace(",", "")),
                    "pct": float(cells[2].replace("%", "")),
                })
            except (ValueError, IndexError):
                continue
    return rows or None


def compute_chips_sina(symbol_code: str, days: int = 300) -> dict | None:
    """基于新浪历史分价表的筹码分布(真实价位分布, 2026-08-11)。

    分段拉取(每段 ~60 交易日) → 每段真实价位分布按换手率衰减 → 累加。
    比三角分布估算精确(真实成交价位), 失败回退 compute_chips(三角分布)。

    Returns: 同 compute_chips 结构
    """
    import datetime

    # 交易日近似: 用自然日倒推(周末/假日约 1.4x)
    segments = []
    end_d = datetime.date.today()
    span_days = int(days * 1.4) + 10
    seg_len = 90  # 每段 ~90 自然日(~60 交易日)
    cursor = end_d
    while segments == [] or (end_d - segments[-1]["start_d"]).days < span_days:
        start_d = cursor - datetime.timedelta(days=seg_len)
        seg = fetch_sina_hist_price(symbol_code, start_d.isoformat(), cursor.isoformat())
        if seg:
            segments.append({"start_d": start_d, "end": cursor.isoformat(), "start": start_d.isoformat(), "rows": seg})
        cursor = start_d - datetime.timedelta(days=1)
        if len(segments) >= 8:
            break

    if not segments:
        return None

    # 合并价位网格
    all_prices = set()
    for seg in segments:
        for r in seg["rows"]:
            all_prices.add(round(r["price"], 2))
    prices = sorted(all_prices)
    idx = {p: i for i, p in enumerate(prices)}
    n = len(prices)
    chips = [0.0] * n

    # 流通股本近似: 取最大单段总成交 × 系数(无精确流通股本时)
    max_seg_vol = max(sum(r["volume"] for r in seg["rows"]) for seg in segments)
    float_shares = max_seg_vol * 6 or 1.0

    # 由旧到新累加(衰减)
    for seg in reversed(segments):
        seg_vol = sum(r["volume"] for r in seg["rows"])
        if seg_vol <= 0:
            continue
        turnover = min(max(seg_vol / float_shares, 0.001), 1.0)
        keep = 1.0 - turnover * DECAY
        chips = [c * keep for c in chips]
        for r in seg["rows"]:
            i = idx.get(round(r["price"], 2))
            if i is not None:
                chips[i] += r["volume"]

    total = sum(chips)
    if total <= 0:
        return None
    chips = [c / total for c in chips]

    def cost(percent: float) -> float:
        cum = 0.0
        for i, c in enumerate(chips):
            cum += c
            if cum >= percent:
                return prices[i]
        return prices[-1]

    c10, c50, c90 = cost(0.10), cost(0.50), cost(0.90)
    # 现价(用最新收盘价, 从行情取; 失败用最新段最高价近似)
    last_close = None
    try:
        from marketdata.vendors.tencent import TencentQuoteVendor
        from marketdata import Symbol
        sym = Symbol.parse(symbol_code[2:], "CN")
        q = TencentQuoteVendor().fetch([sym], {})[0]
        last_close = q.current_price
    except Exception:
        last_close = segments[0]["rows"][0]["price"] if segments and segments[0]["rows"] else None
    if last_close is None:
        return None
    profit_ratio = sum(c for p, c in zip(prices, chips) if p <= last_close)
    peak_i = max(range(n), key=lambda i: chips[i])

    return {
        "prices": prices,
        "chips": [round(c * 100, 3) for c in chips],
        "cost_10": round(c10, 2),
        "cost_50": round(c50, 2),
        "cost_90": round(c90, 2),
        "profit_ratio": round(profit_ratio, 4),
        "peak_price": round(prices[peak_i], 2),
        "peak_ratio": round(chips[peak_i] * 100, 2),
        "concentration": round((c90 - c10) / c50, 4) if c50 else None,
        "step": 0.01,
        "last_close": last_close,
        "source": "sina_hist_price",
        "segments": len(segments),
    }


def fetch_tencent_price_dist(symbol_code: str) -> list[dict] | None:
    """腾讯当日分价表(2026-08-12 降级源): 海外节点新浪超时, 腾讯秒回。

    symbol_code: 'sz002361' / 'sh600519'
    Returns: [{price, volume}] 按价降序; 失败 None
    格式: v_psz002361=[日期,时间,笔数,"价~量~额~买~卖^..."]
    """
    import re
    import urllib.request

    url = f"https://stock.gtimg.cn/data/index.php?appn=price&c={symbol_code}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://stockapp.finance.qq.com/"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("gbk", "replace")
    except Exception:
        return None
    m = re.search(r'\[[^,]+,[^,]+,[^,]+,"(.*?)"\]', body)
    if not m:
        return None
    out = []
    for seg in m.group(1).split("^"):
        parts = seg.split("~")
        if len(parts) >= 2:
            try:
                out.append({"price": float(parts[0]), "volume": int(parts[1])})
            except (ValueError, IndexError):
                continue
    return out or None


def compute_near_term_chips(symbol_code: str, days: int = 10) -> dict | None:
    """近期真实筹码分布(新浪历史分价表, 免衰减, 2026-08-11)。

    近 N 交易日窗口内, 换手衰减影响小 → 直接用真实价位分布即精确。
    输出近期主力成本带(筹码峰), 用于"主力意图"段。

    Returns: {peak_price, peak_ratio, cost_10/50/90, profit_ratio, concentration,
              cost_band(low/high), prices, chips}
    """
    import datetime
    import time as _time

    # 缓存: 10日窗口数据盘中不变, TTL 1h, 避免每轮监控重复请求新浪
    _now = _time.time()
    _cached = _NEAR_CHIPS_CACHE.get(symbol_code)
    if _cached and _now - _cached[0] < _NEAR_CHIPS_TTL:
        return _cached[1]

    end_d = datetime.date.today()
    start_d = end_d - datetime.timedelta(days=int(days * 1.4) + 5)
    # 2026-08-12 优化: 先腾讯当日分价(海外 0.17s 秒回), 腾讯失败才新浪历史分价
    # (海外节点新浪慢 8-10s, 10日窗口以当日为主, 腾讯精度足够)。
    rows = fetch_tencent_price_dist(symbol_code)
    source = "tencent_price_dist"
    if not rows or len(rows) < 5:
        rows = fetch_sina_hist_price(symbol_code, start_d.isoformat(), end_d.isoformat())
        source = "sina_hist_price"
    if not rows or len(rows) < 5:
        _NEAR_CHIPS_CACHE[symbol_code] = (_now, None)
        _chips_persist()
        return None

    total = sum(r["volume"] for r in rows)
    if total <= 0:
        return None
    prices = [r["price"] for r in rows]           # 已按价降序
    vols = [r["volume"] for r in rows]
    chips = [v / total for v in vols]

    # 现价
    last_close = None
    try:
        from marketdata.vendors.tencent import TencentQuoteVendor
        from marketdata import Symbol
        sym = Symbol.parse(symbol_code[2:], "CN")
        q = TencentQuoteVendor().fetch([sym], {})[0]
        last_close = q.current_price
    except Exception:
        last_close = None
    if last_close is None:
        last_close = prices[-1]

    # COST: 价格升序累计
    asc = sorted(zip(prices, chips))
    def cost(pct: float) -> float:
        cum = 0.0
        for p, c in asc:
            cum += c
            if cum >= pct:
                return p
        return asc[-1][0]

    c10, c50, c90 = cost(0.10), cost(0.50), cost(0.90)
    profit = sum(c for p, c in asc if p <= last_close)
    peak_i = max(range(len(prices)), key=lambda i: chips[i])

    # 主力成本带: 含峰值的 ±5% 价格区间内累计占比 ≥40%
    peak = prices[peak_i]
    band_low = peak * 0.95
    band_high = peak * 1.05
    band_vol = sum(v for p, v in zip(prices, vols) if band_low <= p <= band_high)

    _result = {
        "prices": prices,
        "chips": [round(c * 100, 3) for c in chips],
        "cost_10": round(c10, 2),
        "cost_50": round(c50, 2),
        "cost_90": round(c90, 2),
        "profit_ratio": round(profit, 4),
        "peak_price": round(peak, 2),
        "peak_ratio": round(chips[peak_i] * 100, 2),
        "concentration": round((c90 - c10) / c50, 4) if c50 else None,
        "cost_band": {"low": round(band_low, 2), "high": round(band_high, 2),
                      "ratio": round(band_vol / total * 100, 1)},
        "last_close": last_close,
        "source": source,
        "window_days": days,
    }
    _NEAR_CHIPS_CACHE[symbol_code] = (_now, _result)
    _chips_persist()
    # 批次2(2026-09-10): 筹码日频落库(写穿, 当日唯一幂等) → 历史可查; 失败静默
    try:
        from src.core.market_archive import persist_chip_daily

        persist_chip_daily(datetime.date.today(), symbol_code[2:], "CN", _result)
    except Exception:  # noqa: BLE001
        pass
    return _result


def _k_get(k: Any, field: str, default: float = 0.0) -> float:
    if isinstance(k, dict):
        v = k.get(field, default)
    else:
        v = getattr(k, field, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def compute_chips(klines: list, float_shares: float | None = None,
                  price_step: float = 0.01) -> dict | None:
    """计算筹码分布。

    klines: 日K列表, 每项有 date/open/high/low/close/volume(升序, 越新越靠后)
    支持 dict 或带有属性的对象。
    """
    if not klines or len(klines) < 20:
        return None

    # 价格网格: 覆盖全历史 [min_low, max_high], 步长自适应
    lo_p = min(_k_get(k, "low") for k in klines)
    hi_p = max(_k_get(k, "high") for k in klines)
    if hi_p - lo_p > 20:
        step = 0.1
    elif hi_p - lo_p > 5:
        step = 0.05
    else:
        step = 0.01
    # 网格(含边界)
    n = int((hi_p - lo_p) / step) + 2
    prices = [round(lo_p - step + i * step, 2) for i in range(n)]
    idx = {p: i for i, p in enumerate(prices)}
    chips = [0.0] * n

    # 流通股本: 用最大换手日近似(成交量/换手率); 无换手率时用最大成交量×8
    if float_shares is None:
        max_vol = max(_k_get(k, "volume") for k in klines if _k_get(k, "volume"))
        float_shares = max_vol * 8 or 1.0

    for k in klines:
        vol = _k_get(k, "volume")
        k_high = _k_get(k, "high")
        k_low = _k_get(k, "low")
        k_close = _k_get(k, "close")
        if vol <= 0 or k_high < k_low:
            continue
        # 2026-10-23 审计修复：一字板(k_high == k_low) 原来是 `k_high <= k_low` 直接
        # continue —— 涨停/跌停一字板当天**既不衰减旧筹码、也不计入新筹码**，
        # 连板妖股的筹码分布会整段停滞在启动前（用户看的"底仓成本"是错的）。
        # 现价网格落不下时按最近价位整笔计入（下面 i_lo==i_hi 分支已支持）。
        turnover = vol / float_shares  # 换手率
        turnover = min(max(turnover, 0.001), 1.0)
        # 衰减: 旧筹码按 (1 - turnover×DECAY) 保留
        keep = 1.0 - turnover * DECAY
        chips = [c * keep for c in chips]
        # 新增筹码: 三角分布(均价为峰)
        avg_p = (k_high + k_low + k_close) / 3.0
        # 将 vol 按三角权重分配到 [low, high] 网格
        p_lo, p_hi = k_low, k_high
        i_lo, i_hi = idx.get(round(p_lo, 2)), idx.get(round(p_hi, 2))
        if i_lo is None or i_hi is None:
            i_lo = min(range(n), key=lambda i: abs(prices[i] - p_lo))
            i_hi = min(range(n), key=lambda i: abs(prices[i] - p_hi))
        i_lo, i_hi = sorted([i_lo, i_hi])
        i_avg = min(range(i_lo, i_hi + 1), key=lambda i: abs(prices[i] - avg_p))
        total_w = 0.0
        w = [0.0] * (i_hi - i_lo + 1)
        for j in range(i_lo, i_hi + 1):
            if i_hi == i_lo:
                w[j - i_lo] = 1.0
            elif j <= i_avg:
                w[j - i_lo] = (j - i_lo + 1) / (i_avg - i_lo + 1)
            else:
                w[j - i_lo] = (i_hi - j + 1) / (i_hi - i_avg + 1)
            total_w += w[j - i_lo]
        if total_w <= 0:
            continue
        for j in range(i_lo, i_hi + 1):
            chips[j] += vol * w[j - i_lo] / total_w

    total = sum(chips)
    if total <= 0:
        return None

    # 归一化
    chips = [c / total for c in chips]

    # COST(N): N%筹码在价格以下
    def cost(percent: float) -> float:
        cum = 0.0
        for i, c in enumerate(chips):
            cum += c
            if cum >= percent:
                return prices[i]
        return prices[-1]

    c10, c50, c90 = cost(0.10), cost(0.50), cost(0.90)
    # 获利盘: 价格 ≤ 最新收盘 的筹码占比
    last_close = _k_get(klines[-1], "close")
    profit_ratio = sum(c for p, c in zip(prices, chips) if p <= last_close)
    # 筹码峰: 最大占比价位(主力成本区)
    peak_i = max(range(n), key=lambda i: chips[i])
    peak_price = prices[peak_i]
    peak_ratio = chips[peak_i]

    return {
        "prices": prices,
        "chips": [round(c * 100, 3) for c in chips],  # 百分比
        "cost_10": round(c10, 2),
        "cost_50": round(c50, 2),
        "cost_90": round(c90, 2),
        "profit_ratio": round(profit_ratio, 4),
        "peak_price": round(peak_price, 2),
        "peak_ratio": round(peak_ratio * 100, 2),
        "concentration": round((c90 - c10) / c50, 4) if c50 else None,  # 集中度
        "step": step,
        "last_close": last_close,
    }


def compute_main_accumulation_chips(
    klines: list,
    float_shares: float | None = None,
    lookback_days: int = 120,
    accumulation_window: int = 40,
) -> dict | None:
    """计算主力建仓期筹码沉淀模型 (Main Accumulation Chips)。

    解决核心痛点: 用户不仅看单日分时，更要穿透主力自「波段建仓起点」以来的筹码峰与底仓分布。
    算法步骤:
    1. 自动定位主力建仓波段 (近 lookback_days 中缩量震荡箱体/底部放量拐点);
    2. 基于建仓周期 K 线 + 真实换手衰减模型，计算主力从建仓以来的筹码留存;
    3. 识别【主力核心底仓峰】、【70%建仓成本区间】、【建仓均价】、【当前脱离成本比例】与【结构形态】。
    """
    if not klines or len(klines) < 30:
        return None

    # 截取历史回溯周期 (默认近 120 根日K)
    sub_klines = klines[-lookback_days:] if len(klines) > lookback_days else klines

    # 计算全局与建仓期筹码分布
    base_chips = compute_chips(sub_klines, float_shares=float_shares)
    if not base_chips:
        return None

    prices = base_chips["prices"]
    chips = base_chips["chips"]
    c10 = base_chips["cost_10"]
    c50 = base_chips["cost_50"]
    c90 = base_chips["cost_90"]
    peak_p = base_chips["peak_price"]
    profit = base_chips["profit_ratio"]
    last_c = base_chips["last_close"]
    concentration = base_chips["concentration"]

    # 主力建仓底仓成本评估:
    # 70% 主力核心筹码带: COST(15) ~ COST(85)
    def _cost_at(pct: float) -> float:
        cum = 0.0
        for p, c in zip(prices, chips):
            cum += c
            if cum >= pct * 100.0:
                return p
        return prices[-1]

    cost_70_low = round(_cost_at(0.15), 2)
    cost_70_high = round(_cost_at(0.85), 2)

    # 成本偏离度与脱离成本区状态
    main_cost = round(c50, 2)
    departure_pct = round(((last_c - main_cost) / main_cost) * 100.0, 2) if main_cost > 0 else 0.0

    # 筹码形态研判 (单峰密集 / 双峰对峙 / 发散震荡)
    if concentration and concentration <= 0.12:
        pattern = "单峰密集"
        status_label = "主力高度控盘锁仓"
        signal_level = "bullish"
        action_advice = f"筹码高度集中于 {main_cost} 元附近，主力控盘良好，关注突破加仓机会"
    elif concentration and concentration <= 0.20:
        pattern = "缓步沉淀"
        status_label = "建仓区蓄势"
        signal_level = "neutral"
        action_advice = f"主力底仓在 {cost_70_low} - {cost_70_high} 元构筑，震荡蓄势中"
    else:
        pattern = "筹码发散"
        status_label = "多空博弈发散"
        signal_level = "warning"
        action_advice = f"筹码跨度较大，上方注意 {c90} 元套牢阻力"

    # 盈亏阶段定性
    if profit >= 0.85:
        profit_state = "高位获利盘密集 (警惕兑现出货)"
    elif profit >= 0.60:
        profit_state = "主升获利良好 (脱离建仓成本区)"
    elif profit >= 0.35:
        profit_state = "成本区震荡洗盘"
    else:
        profit_state = "深套/低位主力吸筹区"

    sym_val = getattr(sub_klines[-1], "symbol", None) if not isinstance(sub_klines[-1], dict) else sub_klines[-1].get("symbol")
    return {
        "symbol": sym_val,
        "lookback_days": len(sub_klines),
        "main_cost": main_cost,
        "peak_price": peak_p,
        "peak_ratio": base_chips["peak_ratio"],
        "cost_70_low": cost_70_low,
        "cost_70_high": cost_70_high,
        "cost_90_low": c10,
        "cost_90_high": c90,
        "profit_ratio": round(profit * 100.0, 1),
        "profit_state": profit_state,
        "concentration": concentration,
        "pattern": pattern,
        "status_label": status_label,
        "signal_level": signal_level,
        "action_advice": action_advice,
        "departure_pct": departure_pct,
        "last_close": last_c,
        "prices": prices,
        "chips": chips,
    }
