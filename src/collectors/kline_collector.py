"""K线和技术指标采集器 - 基于腾讯 API（更稳定）"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import threading
import time
from zoneinfo import ZoneInfo

from src.collectors.market_http import fetch_source
from src.models.market import MARKETS, MarketCode

logger = logging.getLogger(__name__)


def get_market_data():
    """惰性 import,避免包未装/循环 import 影响本模块加载。"""
    from src.core.marketdata_client import get_market_data as _g
    return _g()


# 调用来源标记统一在 market_http(全项目共享一个 contextvar)。
# 保留 kline_source 名称,兼容已有调用方(schedulers 等)。
kline_source = fetch_source


# ── K线按市场状态缓存 ──────────────────────────────────────────────────────
# 日K一天只定稿一次(收盘后),但调度任务每轮都逐只重新联网拉 → 批量突发触发限流。
# 交易时段用短 TTL(末根K线盘中会动),收盘后用长 TTL(数据已定稿,无需重复拉)。
_KLINE_CACHE: dict[str, tuple[float, int, list["KlineData"]]] = {}
_KLINE_TTL_AUCTION_S = 15  # 集合竞价(9:15-9:25): 数据秒级变动, 最短 TTL
_KLINE_TTL_TRADING_S = 60  # 连续竞价: 盘中末根K线会更新, 中等 TTL
_KLINE_TTL_CLOSED_S = 1800

# 失败负缓存:源短暂故障(Server disconnected/限流)时,冷却窗口内不再联网。
# 复活的批量消费者(entry_candidates/strategy_engine/backtest/组合归因)会并发地
# 对同一批标的取数,空结果若不缓存则每个消费者每轮都重复打爆数据源。
_FAIL_UNTIL: dict[str, float] = {}
_FAIL_COOLDOWN_S = 60.0  # 交易时段:短冷却,便于尽快重试
_FAIL_COOLDOWN_CLOSED_S = 900.0  # 收盘后:数据已定稿,失败/不足时长冷却,避免批量任务反复刷屏


def _fail_cooldown(market: MarketCode) -> float:
    """取数失败/不足时的冷却时长:交易时段短(尽快重试),收盘后长(重试无意义且易刷屏)。"""
    try:
        md = MARKETS.get(market)
        if md and md.is_trading_time():
            return _FAIL_COOLDOWN_S
    except Exception:
        pass
    return _FAIL_COOLDOWN_CLOSED_S


# 同标的并发合并:同一 cache_key 的并发取数串行化,只联网一次,其余复用缓存。
_FETCH_LOCKS: dict[str, threading.Lock] = {}
_FETCH_LOCKS_GUARD = threading.Lock()


def _get_fetch_lock(cache_key: str) -> threading.Lock:
    """返回某 cache_key 的取数锁(进程内复用),用于合并同标的并发请求。"""
    with _FETCH_LOCKS_GUARD:
        lk = _FETCH_LOCKS.get(cache_key)
        if lk is None:
            lk = threading.Lock()
            _FETCH_LOCKS[cache_key] = lk
        return lk


def _is_auction_time() -> bool:
    """判断当前是否处于集合竞价时段(9:15-9:25, 仅交易日)。"""
    try:
        import datetime as _dt
        now = _dt.datetime.now(ZoneInfo("Asia/Shanghai"))
        if now.weekday() >= 5:
            return False
        t = now.time()
        return _dt.time(9, 15) <= t <= _dt.time(9, 25)
    except Exception:
        return False


def _kline_cache_ttl(market: MarketCode) -> float:
    try:
        md = MARKETS.get(market)
        if md and md.is_trading_time():
            # 集合竞价期间数据秒级变动, 用最短 TTL
            if _is_auction_time():
                return _KLINE_TTL_AUCTION_S
            return _KLINE_TTL_TRADING_S
    except Exception:
        pass
    return _KLINE_TTL_CLOSED_S


def clear_kline_cache() -> None:
    """清空 K线内存缓存与失败冷却标记(测试隔离用)。"""
    _KLINE_CACHE.clear()
    _FAIL_UNTIL.clear()


def get_index_klines(index_code: str, market: MarketCode, days: int = 120) -> list[KlineData]:
    """取大盘/指数日K:走 marketdata 包 index_klines(INDEX_SECID 显式映射,未映射如美股指数
    → 空列表,fail-soft;见 packages/marketdata/src/marketdata/client.py)。
    """
    try:
        bars = get_market_data().index_klines(index_code, market=market.value, days=days)
    except Exception as e:
        logger.debug(f"指数K线获取失败 {index_code}: {e}")
        return []
    return [
        KlineData(date=b.date, open=b.open, close=b.close, high=b.high, low=b.low, volume=b.volume)
        for b in bars
    ]


@dataclass
class KlineData:
    """K线数据"""

    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float


@dataclass
class TechnicalIndicators:
    """技术指标"""

    # 均线
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    # MACD
    macd_dif: float | None = None
    macd_dea: float | None = None
    macd_hist: float | None = None
    macd_cross: str | None = None  # 金叉/死叉
    macd_cross_days: int | None = None  # 距离上次交叉天数
    # RSI
    rsi6: float | None = None
    rsi12: float | None = None
    rsi24: float | None = None
    # KDJ
    kdj_k: float | None = None
    kdj_d: float | None = None
    kdj_j: float | None = None
    kdj_cross: str | None = None  # 金叉/死叉
    # 布林带
    boll_upper: float | None = None
    boll_mid: float | None = None
    boll_lower: float | None = None
    boll_width: float | None = None  # 带宽百分比
    # 量能
    volume_ratio: float | None = None  # 量比（今日成交量/5日均量）
    volume_ma5: float | None = None
    volume_ma10: float | None = None
    volume_trend: str | None = None  # 放量/缩量/平量
    # 涨跌幅
    change_5d: float | None = None
    change_20d: float | None = None
    # 振幅
    amplitude: float | None = None  # 今日振幅
    amplitude_avg5: float | None = None  # 5日平均振幅
    # 波动率(ATR)
    atr: float | None = None  # 平均真实波幅(绝对值)
    atr_pct: float | None = None  # ATR / 最新收盘 * 100(相对波动率%)
    # 支撑压力（多级别）
    support_s: float | None = None  # 短期支撑（5日）
    support_m: float | None = None  # 中期支撑（20日）
    support_l: float | None = None  # 长期支撑（60日）
    resistance_s: float | None = None  # 短期压力
    resistance_m: float | None = None  # 中期压力
    resistance_l: float | None = None  # 长期压力
    # 兼容旧字段
    support: float | None = None
    resistance: float | None = None
    # K线形态
    kline_pattern: str | None = None  # 十字星/锤子线/吞没等


def _calculate_ma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _ema(data: list[float], period: int) -> list[float]:
    """计算 EMA"""
    if not data:
        return []
    result = [data[0]]
    multiplier = 2 / (period + 1)
    for price in data[1:]:
        result.append((price - result[-1]) * multiplier + result[-1])
    return result


def _calculate_atr(klines: list[KlineData], period: int = 14) -> float | None:
    """计算 ATR(平均真实波幅)。

    TR = max(high-low, |high-prevClose|, |low-prevClose|)。
    与本模块其它指标一致,取最近 period 个 TR 的简单均值(非 Wilder 递归平滑),
    便于复现与手算校验。

    需要至少 period+1 根 K 线(才能算出 period 个含前收的 TR);
    数据不足或异常一律返回 None,不抛异常(fail-soft)。
    """
    try:
        if not klines or len(klines) < period + 1:
            return None
        trs: list[float] = []
        for i in range(1, len(klines)):
            cur = klines[i]
            prev_close = klines[i - 1].close
            tr = max(
                cur.high - cur.low,
                abs(cur.high - prev_close),
                abs(cur.low - prev_close),
            )
            trs.append(tr)
        if len(trs) < period:
            return None
        return sum(trs[-period:]) / period
    except Exception:
        return None


def _calculate_macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float], list[float]] | None:
    """计算 MACD，返回完整序列用于判断交叉"""
    if len(closes) < slow + signal:
        return None

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    macd_hist = [(d - e) * 2 for d, e in zip(dif, dea)]
    return dif, dea, macd_hist


def _calculate_rsi(closes: list[float], period: int) -> float | None:
    """计算 RSI"""
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    # 使用最近 period 天计算
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _calculate_kdj(
    klines: list[KlineData], n: int = 9, m1: int = 3, m2: int = 3
) -> tuple[list[float], list[float], list[float]] | None:
    """计算 KDJ，返回完整序列"""
    if len(klines) < n:
        return None

    k_values = []
    d_values = []
    j_values = []

    for i in range(n - 1, len(klines)):
        period_klines = klines[i - n + 1 : i + 1]
        highest = max(k.high for k in period_klines)
        lowest = min(k.low for k in period_klines)
        close = klines[i].close

        if highest == lowest:
            rsv = 50
        else:
            rsv = (close - lowest) / (highest - lowest) * 100

        if not k_values:
            k = 50
            d = 50
        else:
            k = (2 / 3) * k_values[-1] + (1 / 3) * rsv
            d = (2 / 3) * d_values[-1] + (1 / 3) * k

        j = 3 * k - 2 * d

        k_values.append(k)
        d_values.append(d)
        j_values.append(j)

    return k_values, d_values, j_values


def _calculate_boll(
    closes: list[float], period: int = 20, num_std: int = 2
) -> tuple[float, float, float, float] | None:
    """计算布林带：上轨、中轨、下轨、带宽"""
    if len(closes) < period:
        return None

    recent = closes[-period:]
    mid = sum(recent) / period
    variance = sum((x - mid) ** 2 for x in recent) / period
    std = variance**0.5

    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower) / mid * 100 if mid > 0 else 0

    return upper, mid, lower, width


def _detect_kline_pattern(klines: list[KlineData]) -> str | None:
    """检测 K 线形态"""
    if len(klines) < 2:
        return None

    curr = klines[-1]
    prev = klines[-2]

    body = abs(curr.close - curr.open)
    upper_shadow = curr.high - max(curr.close, curr.open)
    lower_shadow = min(curr.close, curr.open) - curr.low
    total_range = curr.high - curr.low

    if total_range == 0:
        return None

    body_ratio = body / total_range

    # 十字星：实体很小
    if body_ratio < 0.1:
        if upper_shadow > body * 2 and lower_shadow > body * 2:
            return "十字星"
        elif upper_shadow > body * 3:
            return "倒T字"
        elif lower_shadow > body * 3:
            return "T字线"

    # 锤子线：下影线很长，实体在上方
    if lower_shadow > body * 2 and upper_shadow < body * 0.5:
        if curr.close > curr.open:
            return "锤子线(阳)"
        else:
            return "锤子线(阴)"

    # 倒锤子：上影线很长
    if upper_shadow > body * 2 and lower_shadow < body * 0.5:
        if curr.close > curr.open:
            return "倒锤子(阳)"
        else:
            return "射击之星"

    # 吞没形态
    prev_body = abs(prev.close - prev.open)
    if body > prev_body * 1.5:
        if prev.close < prev.open and curr.close > curr.open:  # 前阴后阳
            if curr.close > prev.open and curr.open < prev.close:
                return "看涨吞没"
        elif prev.close > prev.open and curr.close < curr.open:  # 前阳后阴
            if curr.open > prev.close and curr.close < prev.open:
                return "看跌吞没"

    # 大阳线/大阴线
    if body_ratio > 0.7:
        change_pct = (curr.close - curr.open) / curr.open * 100 if curr.open > 0 else 0
        if change_pct > 3:
            return "大阳线"
        elif change_pct < -3:
            return "大阴线"

    # 组合形态(同花顺教学体系): 单根形态未命中时,识别多K线组合
    combined = _detect_combined_patterns(klines)
    if combined:
        return combined

    return None


def _detect_combined_patterns(klines: list[KlineData]) -> str | None:
    """组合 K 线形态(同花顺教学文: 金针探底/双针探底/红三兵/涨停双响炮/揭竿而起/上升三法/小步上扬/放量突破)。

    复用 src.core.kline_pattern 的启发式识别,返回最重要的形态名。
    """
    try:
        from src.core.kline_pattern import detect_patterns

        hits = detect_patterns(list(klines))
        if not hits:
            return None
        # 按信号强度排序: 看涨形态按出现顺序返回第一个(金针/双针=强底部信号优先)
        return hits[0].name
    except Exception:
        return None


# TA-Lib 形态中文名映射(2026-08-10 接入,61 种标准形态)
_TALIB_CN: dict[str, str] = {
    "CDLDOJI": "十字星",
    "CDLDRAGONFLYDOJI": "蜻蜓十字",
    "CDLGRAVESTONEDOJI": "墓碑十字",
    "CDLLONGLEGGEDDOJI": "长脚十字",
    "CDLENGULFING": "吞没形态",
    "CDLHAMMER": "锤子线",
    "CDLHANGINGMAN": "上吊线",
    "CDLSHOOTINGSTAR": "射击之星",
    "CDLINVERTEDHAMMER": "倒锤子",
    "CDLMORNINGSTAR": "早晨之星",
    "CDLEVENINGSTAR": "黄昏之星",
    "CDLMORNINGDOJISTAR": "十字晨星",
    "CDLEVENINGDOJISTAR": "十字暮星",
    "CDL3WHITESOLDIERS": "红三兵",
    "CDL3BLACKCROWS": "三只乌鸦",
    "CDL3INSIDE": "三内三外",
    "CDL3LINESTRIKE": "三线打击",
    "CDL3OUTSIDE": "外侧三日",
    "CDL3STARSINSOUTH": "南方三星",
    "CDLABANDONEDBABY": "弃婴形态",
    "CDLADVANCEBLOCK": "推进阻挡",
    "CDLBELTHOLD": "捉腰带线",
    "CDLBREAKAWAY": "脱离形态",
    "CDLCLOSINGMARUBOZU": "收盘无影",
    "CDLCONCEALBABYSWALL": "藏婴吞没",
    "CDLCOUNTERATTACK": "反击形态",
    "CDLDARKCLOUDCOVER": "乌云盖顶",
    "CDLDOJISTAR": "十字星线",
    "CDLDOWNTRENDGAP": "下跌缺口",
    "CDLHARAMI": "孕线形态",
    "CDLHARAMICROSS": "十字孕线",
    "CDLHIGHWAVE": "高浪线",
    "CDLHIKKAKE": "上升三法(日)",
    "CDLHIKKAKEMOD": "修正三法",
    "CDLHOMINGPIGEON": "家鸽形态",
    "CDLIDENTICAL3CROWS": "三乌同类",
    "CDLINNECK": "颈内线",
    "CDLISLANDBOTTOM": "岛形底",
    "CDLISLANDTOP": "岛形顶",
    "CDLKICKING": "踢出形态",
    "CDLKICKINGBYLENGTH": "放量踢出",
    "CDLLADDERBOTTOM": "梯底形态",
    "CDLLONGLINE": "长线形态",
    "CDLMARUBOZU": "光头光脚",
    "CDLMATCHINGLOW": "低点相同",
    "CDLMATHOLD": "铺垫形态",
    "CDLPIERCING": "刺透形态",
    "CDLRICKSHAWMAN": "黄包车夫",
    "CDLRISEFALL3METHODS": "上升三法",
    "CDLSEPARATINGLINES": "分离线",
    "CDLSHORTLINE": "短线形态",
    "CDLSPINNINGTOP": "纺锤线",
    "CDLSTALLEDPATTERN": "停滞形态",
    "CDLSTICKSANDWICH": "三明治形态",
    "CDLTAKURI": "探水竿",
    "CDLTASUKIGAP": "跳空并列",
    "CDLTHRUSTING": "推进形态",
    "CDLTRISTAR": "三星形态",
    "CDLUNIQUE3RIVER": "独特三河",
    "CDLUPSIDEGAP2CROWS": "上行双鸦",
    "CDLXSIDEGAP3METHODS": "上升缺口三法",
}


# 十字星变体(高度相似, 去重时合并): 保留第一个, 其余丢弃
_DOJI_VARIANTS = {"十字星", "长脚十字", "蜻蜓十字", "墓碑十字", "黄包车夫", "十字孕线"}


def _dedupe_patterns(patterns: list[dict]) -> list[dict]:
    """K线形态去重+限流(2026-08-12): 十字星变体合并, 同信号方向最多3个。

    问题: 自研+TA-Lib双引擎会重复识别, 且十字星变体(长脚/蜻蜓/墓碑/黄包车夫)
    本质同源, 6个看涨形态堆一起会刷屏误导LLM过度乐观。
    """
    result: list[dict] = []
    seen_names: set[str] = set()
    seen_signal = {"看涨": 0, "看跌": 0}
    for p in patterns:
        name = p.get("cn_name") or p.get("name") or ""
        signal = p.get("signal") or ""
        # 十字星变体只保留第一个
        if name in _DOJI_VARIANTS:
            if "十字" in seen_names or any(v in seen_names for v in _DOJI_VARIANTS):
                continue
            seen_names.add("十字")
            result.append(p)
            continue
        if name in seen_names:
            continue
        # 同信号方向限流
        if seen_signal.get(signal, 0) >= 3:
            continue
        seen_names.add(name)
        seen_signal[signal] = seen_signal.get(signal, 0) + 1
        result.append(p)
    return result


def _detect_talib_patterns(klines: list[KlineData], lookback: int = 5) -> list[dict]:
    """TA-Lib 标准 K 线形态识别(61 种,2026-08-10 接入)。

    返回: [{name, cn_name, signal, strength}],最近 lookback 根内有信号的形态。
    """
    try:
        import numpy as np
        import talib
    except ImportError:
        return []

    if not klines or len(klines) < 10:
        return []

    o = np.array([k.open for k in klines], dtype=float)
    h = np.array([k.high for k in klines], dtype=float)
    l = np.array([k.low for k in klines], dtype=float)
    c = np.array([k.close for k in klines], dtype=float)

    results: list[dict] = []
    seen: set[str] = set()
    for fn in dir(talib):
        if not fn.startswith("CDL"):
            continue
        try:
            r = getattr(talib, fn)(o, h, l, c)
        except Exception:
            continue
        if len(r) == 0:
            continue
        # 只取最近 lookback 根内有信号的
        tail = r[-lookback:]
        nonzero = tail[tail != 0]
        if len(nonzero) == 0:
            continue
        strength = int(nonzero[-1])
        signal = "看涨" if strength > 0 else "看跌"
        cn = _TALIB_CN.get(fn, fn)
        key = cn
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "name": fn,
            "cn_name": cn,
            "signal": signal,
            "strength": abs(strength),
            "source": "talib",
        })

    # 看涨在前
    results.sort(key=lambda x: 0 if x["signal"] == "看涨" else 1)
    return results


def _get_combined_patterns(klines: list[KlineData]) -> list[dict]:
    """全部识别到的组合形态(含信号方向),供技术指标建议评分使用。

    返回: [{name, signal, description, position}],看涨在前。
    """
    try:
        from src.core.kline_pattern import detect_patterns

        hits = detect_patterns(list(klines))
        if not hits:
            return []
        result = []
        for h in hits:
            result.append({
                "name": h.name,
                "signal": h.signal,
                "position": h.position,
                "description": h.description,
            })
        # 看涨在前,看跌在后
        result.sort(key=lambda x: 0 if x["signal"] == "看涨" else 1)
        return result
    except Exception:
        return []


def _find_cross_days(
    series1: list[float], series2: list[float], cross_type: str
) -> int | None:
    """找到最近一次交叉距今的天数"""
    if len(series1) < 2 or len(series2) < 2:
        return None

    for i in range(len(series1) - 2, -1, -1):
        if cross_type == "金叉":
            # 金叉：series1 从下方穿越 series2
            if series1[i] <= series2[i] and series1[i + 1] > series2[i + 1]:
                return len(series1) - 1 - i
        else:
            # 死叉：series1 从上方穿越 series2
            if series1[i] >= series2[i] and series1[i + 1] < series2[i + 1]:
                return len(series1) - 1 - i

    return None


class KlineCollector:
    """K线数据采集器（腾讯 API）"""

    def __init__(self, market: MarketCode):
        self.market = market

    def get_klines(self, symbol: str, days: int = 60) -> list[KlineData]:
        """获取日K线数据。

        正缓存(按市场状态 TTL)+ 同标的并发合并(只联网一次)+ 失败负缓存
        (源短暂故障时冷却窗口内不再联网),避免多消费者并发把数据源打爆。
        """
        cache_key = f"{self.market.value}:{symbol}"
        need = max(1, int(days or 1))

        # 1) 快路径:命中新鲜正缓存,无需加锁
        hit = self._cache_hit(cache_key, need)
        if hit is not None:
            return hit

        # 2) 同标的并发合并:仅一个线程实际联网,其余等待后复用结果
        with _get_fetch_lock(cache_key):
            hit = self._cache_hit(cache_key, need)
            if hit is not None:
                return hit

            now = time.time()
            # 3) 负缓存:刚失败过的标的,冷却窗口内返回陈旧/空,不再联网
            if now < _FAIL_UNTIL.get(cache_key, 0.0):
                stale = _KLINE_CACHE.get(cache_key)
                bars = stale[2] if stale else []
                return bars[-need:] if len(bars) > need else bars

            klines = self._fetch_all_sources(symbol, days)
            if klines and len(klines) >= need:
                # 成功且条数足够:固化正缓存并清除冷却标记
                _KLINE_CACHE[cache_key] = (now, len(klines), list(klines))
                _FAIL_UNTIL.pop(cache_key, None)
            else:
                # 空 或 拿到部分但不足 need(常见:HK 腾讯不足 + eastmoney 补全失败,
                # 正缓存因 count<need 永不命中 → 每轮重打补全源刷屏)→ 固化冷却。
                # 部分结果仍缓存下来,冷却窗口内直接服务,避免反复联网。
                if klines:
                    _KLINE_CACHE[cache_key] = (now, len(klines), list(klines))
                _FAIL_UNTIL[cache_key] = now + _fail_cooldown(self.market)
            return klines[-need:] if len(klines) > need else klines

    def _cache_hit(self, cache_key: str, need: int) -> list[KlineData] | None:
        """命中新鲜正缓存(TTL 内且条数足够)则返回切片,否则 None。"""
        cached = _KLINE_CACHE.get(cache_key)
        if (
            cached
            and (time.time() - cached[0]) < _kline_cache_ttl(self.market)
            and cached[1] >= need
        ):
            bars = cached[2]
            return bars[-need:] if len(bars) > need else bars
        return None

    def _fetch_all_sources(self, symbol: str, days: int) -> list[KlineData]:
        """走 marketdata 包取数(不含缓存/合并逻辑):Engine 按 DataSource 优先级 +
        min_count 取数(条数不足则换源/取最长,tencent → stooq(US) / eastmoney(CN/HK))。
        v0.4.6.3: 腾讯风控+东财被掐+智兔429 全挂时, 回落 PG klines hypertable
        (800天缓存, 与 /api/klines 的 PG 优先路径同源)。
        """
        need = (max(10, min(days, 30)) if self.market == MarketCode.US
                else (max(120, int(days * 0.6)) if self.market in (MarketCode.CN, MarketCode.HK) else 1))
        want = min(max(days, 3000), 20000) if self.market in (MarketCode.CN, MarketCode.HK) else days
        bars = get_market_data().klines(symbol, market=self.market.value, days=want, min_count=need)
        if bars:
            return [KlineData(date=b.date, open=b.open, close=b.close, high=b.high,
                              low=b.low, volume=b.volume) for b in bars]
        pg = self._pg_fallback(symbol, days)
        if pg:
            return pg
        # v0.4.9.1: 新浪日K直拉兜底(容器内实测可达; 腾讯风控+东财断连时的最后防线)
        return self._sina_fallback(symbol, days)

    def _sina_fallback(self, symbol: str, days: int) -> list[KlineData]:
        """新浪 CN_MarketData.getKLineData 日K兜底(v0.4.9.1)。fail-soft。"""
        try:
            from marketdata.http import market_get

            tsym = ("sh" if self.market == MarketCode.HK else
                    ("sh" if symbol.startswith(("6", "9")) else "sz")) + symbol if self.market == MarketCode.CN else None
            if not tsym:
                return []
            text = market_get(
                "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "CN_MarketData.getKLineData",
                host_key="money.finance.sina.com.cn", min_interval_s=0.15,
                params={"symbol": tsym, "scale": "240", "ma": "no",
                        "datalen": str(min(max(days, 1), 1023))},
                timeout=10, retries=2, parse="text", log_label="新浪个股日K", symbol=symbol,
            )
            import json as _json
            rows = _json.loads(text) if isinstance(text, str) and text.strip() else []
            out = [
                KlineData(date=r["day"], open=float(r["open"]), high=float(r["high"]),
                          low=float(r["low"]), close=float(r["close"]),
                          volume=float(r.get("volume") or 0))
                for r in (rows or []) if isinstance(r, dict) and r.get("day")
            ]
            if out:
                logger.info(f"[kline-sina-fallback] {self.market.value}:{symbol} 新浪兜底 {len(out)} 根")
            return out[-days:] if len(out) > days else out
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[kline-sina-fallback] {symbol}: {e!r}")
            return []

    def _pg_fallback(self, symbol: str, days: int) -> list[KlineData]:
        """PG klines hypertable 兜底(v0.4.6.3): 联网源全挂时读本地缓存。fail-soft。"""
        try:
            from datetime import datetime, timedelta, timezone
            from sqlalchemy import create_engine, text
            from src.web.database import DB_URL
            engine = create_engine(DB_URL, pool_pre_ping=True)
            cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 5) * 2)
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT ts, open, high, low, close, volume "
                        "FROM klines "
                        "WHERE symbol=:s AND market=:m AND period='1d' AND ts >= :c "
                        "ORDER BY ts ASC"
                    ),
                    {"s": symbol, "m": self.market.value, "c": cutoff},
                ).fetchall()
            engine.dispose()
            out = [
                KlineData(date=str(r[0])[:10], open=float(r[1]), high=float(r[2]),
                          low=float(r[3]), close=float(r[4]), volume=float(r[5] or 0))
                for r in rows
            ]
            if out:
                logger.info(f"[kline-pg-fallback] {self.market.value}:{symbol} PG 兜底 {len(out)} 根")
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[kline-pg-fallback] {symbol}: {e!r}")
            return []

    def get_technical_indicators(
        self, symbol: str = "", klines: list[KlineData] | None = None
    ) -> TechnicalIndicators:
        """计算技术指标(可传入已取的 klines 复用,避免重复联网)。"""
        if klines is None:
            klines = self.get_klines(symbol, days=120)

        if not klines:
            return TechnicalIndicators()

        closes = [k.close for k in klines]
        volumes = [k.volume for k in klines]

        # 均线
        ma5 = _calculate_ma(closes, 5)
        ma10 = _calculate_ma(closes, 10)
        ma20 = _calculate_ma(closes, 20)
        ma60 = _calculate_ma(closes, 60)

        # MACD
        macd_result = _calculate_macd(closes)
        macd_dif, macd_dea, macd_hist = None, None, None
        macd_cross, macd_cross_days = None, None
        if macd_result:
            dif_list, dea_list, hist_list = macd_result
            macd_dif = dif_list[-1]
            macd_dea = dea_list[-1]
            macd_hist = hist_list[-1]
            # 判断金叉/死叉
            if macd_dif > macd_dea:
                macd_cross = "金叉"
                macd_cross_days = _find_cross_days(dif_list, dea_list, "金叉")
            else:
                macd_cross = "死叉"
                macd_cross_days = _find_cross_days(dif_list, dea_list, "死叉")

        # RSI
        rsi6 = _calculate_rsi(closes, 6)
        rsi12 = _calculate_rsi(closes, 12)
        rsi24 = _calculate_rsi(closes, 24)

        # KDJ
        kdj_k, kdj_d, kdj_j = None, None, None
        kdj_cross = None
        kdj_result = _calculate_kdj(klines)
        if kdj_result:
            k_list, d_list, j_list = kdj_result
            kdj_k = k_list[-1]
            kdj_d = d_list[-1]
            kdj_j = j_list[-1]
            if kdj_k > kdj_d:
                # 临界保护: K≈D 差值过小时不硬断言方向(开盘瞬间/横盘易翻转, 防误报)
                if (kdj_k - kdj_d) < 1.0:
                    kdj_cross = "临界(金叉弱)"
                else:
                    kdj_cross = "金叉"
            else:
                if (kdj_d - kdj_k) < 1.0:
                    kdj_cross = "临界(死叉弱)"
                else:
                    kdj_cross = "死叉"

        # 布林带
        boll_upper, boll_mid, boll_lower, boll_width = None, None, None, None
        boll_result = _calculate_boll(closes)
        if boll_result:
            boll_upper, boll_mid, boll_lower, boll_width = boll_result

        # 量能分析
        volume_ma5 = _calculate_ma(volumes, 5) if volumes else None
        volume_ma10 = _calculate_ma(volumes, 10) if volumes else None
        volume_ratio = None
        volume_trend = None
        if volumes and volume_ma5 and volume_ma5 > 0:
            volume_ratio = volumes[-1] / volume_ma5
            if volume_ratio > 1.5:
                volume_trend = "放量"
            elif volume_ratio < 0.7:
                volume_trend = "缩量"
            else:
                volume_trend = "平量"

        # 涨跌幅
        change_5d = None
        change_20d = None
        if len(closes) >= 6:
            change_5d = (closes[-1] - closes[-6]) / closes[-6] * 100
        if len(closes) >= 21:
            change_20d = (closes[-1] - closes[-21]) / closes[-21] * 100

        # 振幅
        amplitude = None
        amplitude_avg5 = None
        if klines:
            curr = klines[-1]
            if curr.low > 0:
                amplitude = (curr.high - curr.low) / curr.low * 100
            if len(klines) >= 5:
                amps = []
                for k in klines[-5:]:
                    if k.low > 0:
                        amps.append((k.high - k.low) / k.low * 100)
                if amps:
                    amplitude_avg5 = sum(amps) / len(amps)

        # ATR(波动率):个股自身波动基准,供自适应异动判定使用
        atr = _calculate_atr(klines, period=14)
        atr_pct = None
        if atr is not None and closes and closes[-1]:
            atr_pct = round(atr / closes[-1] * 100, 2)

        # 多级支撑压力位
        support_s, support_m, support_l = None, None, None
        resistance_s, resistance_m, resistance_l = None, None, None
        if len(klines) >= 5:
            support_s = min(k.low for k in klines[-5:])
            resistance_s = max(k.high for k in klines[-5:])
        if len(klines) >= 20:
            support_m = min(k.low for k in klines[-20:])
            resistance_m = max(k.high for k in klines[-20:])
        if len(klines) >= 60:
            support_l = min(k.low for k in klines[-60:])
            resistance_l = max(k.high for k in klines[-60:])

        # 兼容旧字段
        support = support_m
        resistance = resistance_m

        # K线形态
        kline_pattern = _detect_kline_pattern(klines)

        return TechnicalIndicators(
            ma5=ma5,
            ma10=ma10,
            ma20=ma20,
            ma60=ma60,
            macd_dif=macd_dif,
            macd_dea=macd_dea,
            macd_hist=macd_hist,
            macd_cross=macd_cross,
            macd_cross_days=macd_cross_days,
            rsi6=rsi6,
            rsi12=rsi12,
            rsi24=rsi24,
            kdj_k=kdj_k,
            kdj_d=kdj_d,
            kdj_j=kdj_j,
            kdj_cross=kdj_cross,
            boll_upper=boll_upper,
            boll_mid=boll_mid,
            boll_lower=boll_lower,
            boll_width=boll_width,
            volume_ratio=volume_ratio,
            volume_ma5=volume_ma5,
            volume_ma10=volume_ma10,
            volume_trend=volume_trend,
            change_5d=change_5d,
            change_20d=change_20d,
            amplitude=amplitude,
            amplitude_avg5=amplitude_avg5,
            atr=atr,
            atr_pct=atr_pct,
            support_s=support_s,
            support_m=support_m,
            support_l=support_l,
            resistance_s=resistance_s,
            resistance_m=resistance_m,
            resistance_l=resistance_l,
            support=support,
            resistance=resistance,
            kline_pattern=kline_pattern,
        )

    def get_kline_summary(self, symbol: str) -> dict:
        """获取 K 线摘要（用于 prompt 和前端展示）"""
        klines = self.get_klines(symbol, days=120)
        if not klines:
            return {"error": "无K线数据"}
        indicators = self.get_technical_indicators(klines=klines)

        # 最近5日表现
        recent_5 = klines[-5:] if len(klines) >= 5 else klines
        up_days = sum(
            1
            for i, k in enumerate(recent_5)
            if i > 0 and k.close > recent_5[i - 1].close
        )

        # 趋势判断
        trend = "数据不足"
        if indicators.ma5 and indicators.ma10 and indicators.ma20:
            if indicators.ma5 > indicators.ma10 > indicators.ma20:
                trend = "多头排列"
            elif indicators.ma5 < indicators.ma10 < indicators.ma20:
                trend = "空头排列"
            else:
                trend = "均线交织"

        # MACD 状态（更详细）
        macd_status = "无数据"
        if indicators.macd_cross:
            days_str = (
                f"({indicators.macd_cross_days}日)"
                if indicators.macd_cross_days
                else ""
            )
            macd_status = f"{indicators.macd_cross}{days_str}"

        # RSI 状态
        rsi_status = None
        if indicators.rsi6 is not None:
            if indicators.rsi6 > 80:
                rsi_status = "超买"
            elif indicators.rsi6 > 70:
                rsi_status = "偏强"
            elif indicators.rsi6 < 20:
                rsi_status = "超卖"
            elif indicators.rsi6 < 30:
                rsi_status = "偏弱"
            else:
                rsi_status = "中性"

        # KDJ 状态
        kdj_status = None
        if indicators.kdj_k is not None and indicators.kdj_d is not None:
            if indicators.kdj_j is not None and indicators.kdj_j > 100:
                kdj_status = f"{indicators.kdj_cross}/超买"
            elif indicators.kdj_j is not None and indicators.kdj_j < 0:
                kdj_status = f"{indicators.kdj_cross}/超卖"
            else:
                kdj_status = indicators.kdj_cross

        # 布林带状态
        boll_status = None
        last_close = klines[-1].close if klines else None
        if last_close and indicators.boll_upper and indicators.boll_lower:
            if last_close > indicators.boll_upper:
                boll_status = "突破上轨"
            elif last_close < indicators.boll_lower:
                boll_status = "跌破下轨"
            elif indicators.boll_width:
                if indicators.boll_width < 5:
                    boll_status = "收口窄幅"
                elif indicators.boll_width > 15:
                    boll_status = "开口放大"
                else:
                    boll_status = "正常波动"

        last_date = klines[-1].date if klines else None
        now = datetime.now(timezone.utc).isoformat()

        return {
            # meta
            "timeframe": "1d",
            "computed_at": now,
            "asof": last_date,
            "params": {
                "ma": [5, 10, 20, 60],
                "macd": {"fast": 12, "slow": 26, "signal": 9},
                "rsi": {"periods": [6, 12, 24]},
                "kdj": {"n": 9, "m1": 3, "m2": 3},
                "boll": {"period": 20, "num_std": 2},
                "support_resistance": {"windows": [5, 20, 60]},
            },
            "last_close": last_close,
            "recent_5_up": up_days,
            "trend": trend,
            # MACD
            "macd_status": macd_status,
            "macd_cross": indicators.macd_cross,
            "macd_cross_days": indicators.macd_cross_days,
            "macd_hist": indicators.macd_hist,
            # RSI
            "rsi6": indicators.rsi6,
            "rsi_status": rsi_status,
            # KDJ
            "kdj_k": indicators.kdj_k,
            "kdj_d": indicators.kdj_d,
            "kdj_j": indicators.kdj_j,
            "kdj_status": kdj_status,
            # 布林带
            "boll_upper": indicators.boll_upper,
            "boll_mid": indicators.boll_mid,
            "boll_lower": indicators.boll_lower,
            "boll_width": indicators.boll_width,
            "boll_status": boll_status,
            # 量能
            "volume_ratio": indicators.volume_ratio,
            "volume_trend": indicators.volume_trend,
            # 均线
            "ma5": indicators.ma5,
            "ma10": indicators.ma10,
            "ma20": indicators.ma20,
            "ma60": indicators.ma60,
            # 涨跌幅
            "change_5d": indicators.change_5d,
            "change_20d": indicators.change_20d,
            # 振幅
            "amplitude": indicators.amplitude,
            "amplitude_avg5": indicators.amplitude_avg5,
            # 波动率(ATR)
            "atr": indicators.atr,
            "atr_pct": indicators.atr_pct,
            # 多级支撑压力
            "support_s": indicators.support_s,
            "support_m": indicators.support_m,
            "support_l": indicators.support_l,
            "resistance_s": indicators.resistance_s,
            "resistance_m": indicators.resistance_m,
            "resistance_l": indicators.resistance_l,
            # 兼容旧字段
            "support": indicators.support,
            "resistance": indicators.resistance,
            # K线形态
            "kline_pattern": indicators.kline_pattern,
            # 组合形态列表(同花顺教学体系 + TA-Lib 标准形态,含信号方向,供技术指标建议评分)
            # 2026-08-12 打磨: 去重+限流 — 十字星变体(长脚/蜻蜓/墓碑/黄包车夫)高度相似,
            # 全堆一起会刷屏误导LLM。去重后同信号方向最多保留 3 个。
            "kline_patterns": _dedupe_patterns(
                _get_combined_patterns(klines) + _detect_talib_patterns(klines)
            ),
        }
