"""K线组合形态识别(2026-08-10,基于同花顺教学文《K线经典形态》《K线形态大全》)。

输入: 日K序列(open/high/low/close/volume),输出识别到的形态列表。
仅做技术形态识别,不构成投资建议。

可量化识别形态(按同花顺原文特征):
- 金针探底: 极长下影线(下影>=实体2倍),且出现在近期低位
- 双针探底: 两根长下影线,低点接近,构成底部确认
- 红三兵: 三连阳,收盘价递增,实体稳步
- 涨停双响炮: 涨停(或大阳) → 1-3根小整理 → 再涨停(或大阳)
- 揭竿而起: 连续下跌后突然一根大阳线(涨幅>=5%)
- 上升三法: 大阳 → 3根小阴小阳(不破首阳低点) → 再大阳突破
- 小步上扬: 连续多根小阳线阶梯爬升(>=5根,每根涨幅<3%)
- 放量突破(均线多头·布林突破简化): 突破前N日高点+放量

启发式阈值基于同花顺原文描述,可调;识别结果供 AI 助手参考,需结合位置/量能/资金流综合判断。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 涨停阈值(简化:主板 10% 附近视为涨停)
LIMIT_UP_PCT = 9.5
# 大阳线涨幅阈值
BIG_YANG_PCT = 5.0
# 长下影线: 下影线长度 >= 实体长度的倍数
SHADOW_RATIO = 2.0
# 实体最小绝对值(价格单位): 实体过小的十字星/横盘不算针
MIN_BODY = 0.5


def _is_long_lower_shadow(bar) -> bool:
    """长下影判定: 实体足够大 + 下影>=实体*SHADOW_RATIO(带浮点容差)。"""
    body = _body_len(bar)
    if body < MIN_BODY:
        return False
    return _shadow_bottom(bar) + 1e-6 >= body * SHADOW_RATIO


@dataclass
class PatternHit:
    """识别到的形态。"""

    name: str            # 形态名(与同花顺原文一致)
    signal: str          # 信号方向: 看涨 / 看跌 / 中性
    description: str     # 特征描述
    position: str = ""   # 出现位置(低位/高位/趋势中)
    bars: list[int] = field(default_factory=list)  # 涉及的K线索引(从尾部倒数)
    extra: dict = field(default_factory=dict)


def _is_yang(bar) -> bool:
    return bar.close >= bar.open


def _body_len(bar) -> float:
    return abs(bar.close - bar.open)


def _shadow_bottom(bar) -> float:
    """下影线长度。"""
    return min(bar.open, bar.close) - bar.low


def _shadow_top(bar) -> float:
    """上影线长度。"""
    return bar.high - max(bar.open, bar.close)


def _change_pct(bar) -> float:
    """涨跌幅(相对前收)。"""
    return (bar.close - bar.open) / bar.open * 100 if bar.open else 0.0


def detect_patterns(bars: list, lookback: int = 30) -> list[PatternHit]:
    """识别 K 线形态。bars: 日K序列(升序,需含 open/high/low/close/volume)。"""
    if len(bars) < 3:
        return []
    seq = bars[-lookback:]
    hits: list[PatternHit] = []

    # 近期高低位判断(用于金针/双针的"低位"条件)
    window_low = min(b.low for b in seq)
    window_high = max(b.high for b in seq)

    # ---- 1. 金针探底 ----
    last = bars[-1]
    if _is_long_lower_shadow(last):
        # 出现在近期低位(最低价在窗口下 60% 区间内,即盘中确实探到低位)
        if last.low <= window_low + (window_high - window_low) * 0.6:
            hits.append(PatternHit(
                name="金针探底", signal="看涨", position="低位",
                description="极长下影线(下影>=实体2倍),急跌后快速拉回,下方有大资金托底,可能是见底信号",
                bars=[-1],
            ))

    # ---- 2. 双针探底 ----
    if len(bars) >= 3:
        b1, b2 = bars[-2], bars[-1]
        if (_is_long_lower_shadow(b1) and
                _is_long_lower_shadow(b2) and
                abs(b1.low - b2.low) / max(b1.low, b2.low) < 0.03):
            hits.append(PatternHit(
                name="双针探底", signal="看涨", position="低位",
                description="两次急跌都被拉回,两根长下影线低点接近,底部支撑更坚固,容易迎来反弹",
                bars=[-2, -1],
            ))

    # ---- 3. 红三兵 ----
    if len(bars) >= 3:
        b1, b2, b3 = bars[-3], bars[-2], bars[-1]
        if (_is_yang(b1) and _is_yang(b2) and _is_yang(b3) and
                b1.close < b2.close < b3.close):
            hits.append(PatternHit(
                name="红三兵", signal="看涨", position="趋势中",
                description="三根连续阳线收盘价递增,买盘积聚多头增强,后续大概率继续走强",
                bars=[-3, -2, -1],
            ))

    # ---- 4. 涨停双响炮 ----
    if len(bars) >= 5:
        b1, b2, b3 = bars[-3], bars[-2], bars[-1]
        if (_change_pct(b1) >= LIMIT_UP_PCT and _change_pct(b3) >= LIMIT_UP_PCT and
                abs(_change_pct(b2)) < 5.0):
            hits.append(PatternHit(
                name="涨停双响炮", signal="看涨", position="趋势中",
                description="涨停→短暂休整(不深跌)→再涨停,多头攻势干脆利落,短期往往还有冲高空间",
                bars=[-3, -2, -1],
            ))

    # ---- 5. 揭竿而起 ----
    if len(bars) >= 4:
        prev = bars[-4:-1]
        last = bars[-1]
        if (_change_pct(last) >= BIG_YANG_PCT and
                all(not _is_yang(b) for b in prev) and
                last.close > max(b.close for b in prev)):
            hits.append(PatternHit(
                name="揭竿而起", signal="看涨", position="低位",
                description="连续下跌后突然拉出一根大阳线,空头砸盘失败,多头强势反攻,行情有望反转",
                bars=[-1],
            ))

    # ---- 6. 上升三法 ----
    if len(bars) >= 5:
        b1, mid, b5 = bars[-5], bars[-4:-1], bars[-1]
        if (_change_pct(b1) >= BIG_YANG_PCT and _change_pct(b5) >= BIG_YANG_PCT and
                all(b.close >= b1.low and b.close <= b1.close for b in mid) and
                b5.close > b1.close):
            hits.append(PatternHit(
                name="上升三法", signal="看涨", position="趋势中",
                description="大阳线拉起→中间几根小K线横盘(不破首阳低点)→再大阳突破,主力拉升→洗盘→再拉升,蓄势再涨",
                bars=[-5, -4, -3, -2, -1],
            ))

    # ---- 7. 小步上扬 ----
    if len(bars) >= 5:
        last5 = bars[-5:]
        if (all(_is_yang(b) for b in last5) and
                all(0 < _change_pct(b) < 3.0 for b in last5) and
                all(last5[i].close >= last5[i-1].close for i in range(1, 5))):
            hits.append(PatternHit(
                name="小步上扬", signal="看涨", position="趋势中",
                description="每天小阳线慢慢爬升,买盘温和持续,典型慢牛走势,趋势相对稳健",
                bars=[-5, -4, -3, -2, -1],
            ))

    # ---- 8. 放量突破(均线多头·布林突破简化) ----
    if len(bars) >= 20:
        prev20 = bars[-21:-1]
        last = bars[-1]
        prior_high = max(b.high for b in prev20)
        avg_vol = sum(b.volume for b in prev20) / 20 if prev20 else 0
        if (last.close > prior_high and
                last.volume > avg_vol * 1.5 and
                avg_vol > 0):
            hits.append(PatternHit(
                name="放量突破(均线多头·布林突破)", signal="看涨", position="趋势中",
                description=f"收盘价突破前20日高点({prior_high:.2f})且放量(量能>20日均量1.5倍),趋势向上打开空间",
                bars=[-1],
                extra={"break_high": prior_high, "vol_ratio": round(last.volume / avg_vol, 2) if avg_vol else 0},
            ))

    # ================= 看跌形态(八大看跌K线形态,2026-08-10 学习) =================
    _detect_bearish_patterns(bars, hits)

    # ================= 八大进场信号(2026-08-10 学习文,底部看涨/抄底) =================
    _detect_entry_signals(bars, hits)

    # ================= 经典反转/持续形态(同花顺《K线形态大全》20 种,可量化部分) =================
    _detect_classic_patterns(bars, hits)

    return hits


def _detect_entry_signals(bars: list, hits: list[PatternHit]) -> None:
    """八大进场信号: 早晨之星/底部十字星/底部强势大阳线/底部大长腿/大锤小锤/大阳包小阴/大阴后两小阳/进击两阳线。"""
    if len(bars) < 3:
        return
    # 低位判断: 形态启动前价格处于窗口低位(进场信号都出现在底部)
    w_low = min(b.low for b in bars)
    w_high = max(b.high for b in bars)
    # 用最近5日最低价判断(形态出现时价格已反弹,但最低点仍在低位区)
    recent_low = min(b.low for b in bars[-5:]) if len(bars) >= 5 else bars[-1].low
    in_low_zone = recent_low <= w_low + (w_high - w_low) * 0.55

    # ---- 早晨之星: 长阴→星线→长阳 ----
    if len(bars) >= 3:
        b1, b2, b3 = bars[-3], bars[-2], bars[-1]
        if (not _is_yang(b1) and _is_yang(b3) and
                _body_len(b1) >= MIN_BODY and _body_len(b3) >= MIN_BODY and
                _body_len(b2) < _body_len(b1) * 0.3 and
                b3.close > b1.open and in_low_zone):
            hits.append(PatternHit(
                name="早晨之星", signal="看涨", position="低位",
                description="下跌末端长阴→星线→长阳,多空反转,经典抄底信号",
                bars=[-3, -2, -1],
            ))

    # ---- 底部十字星: 下跌末端十字星 ----
    last = bars[-1]
    # 十字星前需有明确下跌(前5日从高点跌超5%),排除横盘
    prev_high = max(b.high for b in bars[-6:-1]) if len(bars) >= 6 else None
    has_decline = prev_high is not None and prev_high > 0 and (last.close - prev_high) / prev_high * 100 < -5.0
    if (_body_len(last) < 0.3 and _shadow_bottom(last) > 0 and
            abs(_shadow_top(last) - _shadow_bottom(last)) / max(_shadow_top(last), _shadow_bottom(last)) < 0.5 and
            in_low_zone and _body_len(last) < 1.0 and has_decline):
        hits.append(PatternHit(
            name="底部十字星", signal="看涨", position="低位",
            description="下跌末端十字星,多空平衡,趋势转折点",
            bars=[-1],
        ))

    # ---- 底部强势大阳线: 底部放量大阳线 ----
    if _change_pct(last) >= BIG_YANG_PCT and in_low_zone:
        hits.append(PatternHit(
            name="底部强势大阳线", signal="看涨", position="低位",
            description=f"底部区域放量大阳线({_change_pct(last):.1f}%),强势上涨启动",
            bars=[-1],
        ))

    # ---- 底部大长腿: 底部极长下影(大长腿) ----
    if _is_long_lower_shadow(last) and _shadow_bottom(last) >= _body_len(last) * 3 and in_low_zone:
        hits.append(PatternHit(
            name="底部大长腿", signal="看涨", position="低位",
            description="底部极长下影线(下影>=实体3倍),空方抛压枯竭,下方有承接",
            bars=[-1],
        ))

    # ---- 大锤和小锤: 底部连续两根长下影(双重支撑) ----
    if len(bars) >= 2:
        b1, b2 = bars[-2], bars[-1]
        # 锤子线: 下影>=实体3倍 且 下影绝对值>实体(不依赖 MIN_BODY,实体小是锤子常态)
        def _is_hammer(b):
            return _shadow_bottom(b) >= max(_body_len(b) * 3, MIN_BODY)
        if (_is_hammer(b1) and _is_hammer(b2) and
                abs(b1.low - b2.low) / max(b1.low, b2.low) < 0.05 and in_low_zone):
            hits.append(PatternHit(
                name="大锤和小锤", signal="看涨", position="低位",
                description="底部连续两根长下影锤子线,双重支撑,底部稳固",
                bars=[-2, -1],
            ))

    # ---- 大阳包小阴: 大阳线完全吞没前日小阴线 ----
    if len(bars) >= 2:
        b1, b2 = bars[-2], bars[-1]
        if (not _is_yang(b1) and _is_yang(b2) and
                _body_len(b2) >= _body_len(b1) * 1.5 and
                b2.open <= b1.close and b2.close >= b1.open and in_low_zone):
            hits.append(PatternHit(
                name="大阳包小阴", signal="看涨", position="低位",
                description="大阳线完全吞没前日小阴线,多方碾压空方,底部反转",
                bars=[-2, -1],
            ))

    # ---- 大阴后两小阳: 大阴线后两根小阳线(空头宣泄休整) ----
    if len(bars) >= 3:
        b1, b2, b3 = bars[-3], bars[-2], bars[-1]
        if (not _is_yang(b1) and _body_len(b1) >= MIN_BODY * 2 and
                _is_yang(b2) and _is_yang(b3) and
                _body_len(b2) < _body_len(b1) and _body_len(b3) < _body_len(b1) and
                in_low_zone):
            hits.append(PatternHit(
                name="大阴后两小阳", signal="看涨", position="低位",
                description="大阴线后两根小阳线,空头宣泄进入休整期,企稳信号",
                bars=[-3, -2, -1],
            ))

    # ---- 进击两阳线: 两根连续阳线(第二根更强) ----
    if len(bars) >= 2:
        b1, b2 = bars[-2], bars[-1]
        if (_is_yang(b1) and _is_yang(b2) and
                b2.close > b1.close and _body_len(b2) >= _body_len(b1) and in_low_zone):
            hits.append(PatternHit(
                name="进击两阳线", signal="看涨", position="低位",
                description="两根连续阳线且第二根更强,多方强势进攻",
                bars=[-2, -1],
            ))


def _detect_classic_patterns(bars: list, hits: list[PatternHit]) -> None:
    """经典形态(可量化部分): 双底/双顶/上升下降三角形/对称三角形/上升下降楔形/旗形/通道。

    头肩顶底/圆弧/菱形/杯柄需要更长窗口+颈线拟合,留作后续增强。
    """
    if len(bars) < 15:
        return

    # ---- 双底(W底): 两个相近低点 + 中间反弹,突破颈线 ----
    low1 = min(b.low for b in bars[-20:-10])
    low2 = min(b.low for b in bars[-10:])
    if abs(low1 - low2) / max(low1, low2) < 0.02:
        # 找两个低点的位置,颈线 = 两点之间的反弹高点
        idx1 = min(range(len(bars) - 20, len(bars) - 10), key=lambda i: bars[i].low)
        idx2 = min(range(len(bars) - 10, len(bars)), key=lambda i: bars[i].low)
        lo, hi = min(idx1, idx2), max(idx1, idx2)
        neck = max(b.high for b in bars[lo + 1:hi])
        last = bars[-1]
        if last.close > neck:  # 突破颈线
            hits.append(PatternHit(
                name="双底突破(W底)", signal="看涨", position="低位",
                description=f"两个相近低点({low1:.2f}/{low2:.2f})构成W底,收盘突破颈线({neck:.2f}),底部确认",
                bars=[-20, -1],
                extra={"neck": neck},
            ))

    # ---- 双顶(M头): 两个相近高点 + 跌破颈线 ----
    high1 = max(b.high for b in bars[-20:-10])
    high2 = max(b.high for b in bars[-10:])
    if abs(high1 - high2) / max(high1, high2) < 0.02:
        idx1 = max(range(len(bars) - 20, len(bars) - 10), key=lambda i: bars[i].high)
        idx2 = max(range(len(bars) - 10, len(bars)), key=lambda i: bars[i].high)
        lo, hi = min(idx1, idx2), max(idx1, idx2)
        neck = min(b.low for b in bars[lo + 1:hi])
        last = bars[-1]
        if last.close < neck:  # 跌破颈线
            hits.append(PatternHit(
                name="双顶破位(M头)", signal="看跌", position="高位",
                description=f"两个相近高点({high1:.2f}/{high2:.2f})构成M头,收盘跌破颈线({neck:.2f}),顶部确认",
                bars=[-20, -1],
                extra={"neck": neck},
            ))

    # ---- 上升三角形: 水平阻力 + 上升支撑,放量突破 ----
    if len(bars) >= 20:
        recent = bars[-20:]
        highs = [b.high for b in recent]
        lows = [b.low for b in recent]
        # 阻力水平(高点集中) + 支撑上升(低点抬升)
        res_avg = sum(highs[-5:]) / 5
        res_spread = (max(highs[-5:]) - min(highs[-5:])) / res_avg if res_avg else 0
        lows_half1 = sum(lows[:10]) / 10
        lows_half2 = sum(lows[10:]) / 10
        last = bars[-1]
        if res_spread < 0.015 and lows_half2 > lows_half1 * 1.01 and last.close > res_avg:
            hits.append(PatternHit(
                name="上升三角形突破", signal="看涨", position="趋势中",
                description=f"水平阻力({res_avg:.2f}) + 低点抬升,放量突破上轨,看涨",
                bars=[-20, -1],
            ))

    # ---- 下降三角形: 水平支撑 + 下降阻力,跌破 ----
    if len(bars) >= 20:
        recent = bars[-20:]
        highs = [b.high for b in recent]
        lows = [b.low for b in recent]
        sup_avg = sum(lows[-5:]) / 5
        sup_spread = (max(lows[-5:]) - min(lows[-5:])) / sup_avg if sup_avg else 0
        highs_half1 = sum(highs[:10]) / 10
        highs_half2 = sum(highs[10:]) / 10
        last = bars[-1]
        if sup_spread < 0.015 and highs_half2 < highs_half1 * 0.99 and last.close < sup_avg:
            hits.append(PatternHit(
                name="下降三角形破位", signal="看跌", position="趋势中",
                description=f"水平支撑({sup_avg:.2f}) + 高点下移,跌破下轨,看跌",
                bars=[-20, -1],
            ))

    # ---- 上升旗形: 急升后向上平行通道整理,突破上沿 ----
    if len(bars) >= 15:
        recent = bars[-15:]
        lows = [b.low for b in recent]
        highs = [b.high for b in recent]
        # 旗形: 高点低点都小幅上移但幅度收窄(斜率小)
        low_slope = (lows[-1] - lows[0]) / lows[0] if lows[0] else 0
        high_slope = (highs[-1] - highs[0]) / highs[0] if highs[0] else 0
        last = bars[-1]
        if 0 < low_slope < 0.06 and 0 < high_slope < 0.06 and last.close > max(highs[:-1]):
            hits.append(PatternHit(
                name="上升旗形突破", signal="看涨", position="趋势中",
                description="急升后窄幅向上整理(旗形),突破上沿,看涨持续",
                bars=[-15, -1],
            ))

    # ---- 下降旗形: 急跌后向下平行通道整理,跌破下沿 ----
    if len(bars) >= 15:
        recent = bars[-15:]
        lows = [b.low for b in recent]
        highs = [b.high for b in recent]
        low_slope = (lows[-1] - lows[0]) / lows[0] if lows[0] else 0
        high_slope = (highs[-1] - highs[0]) / highs[0] if highs[0] else 0
        last = bars[-1]
        if -0.06 < low_slope < 0 and -0.06 < high_slope < 0 and last.close < min(lows[:-1]):
            hits.append(PatternHit(
                name="下降旗形破位", signal="看跌", position="趋势中",
                description="急跌后窄幅向下整理(旗形),跌破下沿,看跌持续",
                bars=[-15, -1],
            ))


def _detect_bearish_patterns(bars: list, hits: list[PatternHit]) -> None:
    """看跌形态: 三只乌鸦/黑三兵/空方炮/倾盆大雨/黄昏之星/看跌尽头线/兄弟剃平头/二级倒锤头。"""
    if len(bars) < 5:
        return
    # 高位判断: 形态出现前价格处于窗口高位(看跌形态出现在上涨末端)
    w_low = min(b.low for b in bars)
    w_high = max(b.high for b in bars)
    # 用形态启动前的位置: 前5日最高收盘价 vs 窗口(形态启动前应处于上涨末端高位)
    pre_closes = [b.close for b in bars[-6:-1]]
    pre_peak = max(pre_closes) if pre_closes else bars[-1].close
    # 涨幅门槛: 形态启动前需有明确上涨(前5日从低点涨超3%),排除横盘/下跌
    pre_low = min(pre_closes) if pre_closes else bars[-1].close
    has_rally = (pre_low > 0 and (pre_peak - pre_low) / pre_low * 100 > 3.0)
    in_high_zone = has_rally and pre_peak >= w_low + (w_high - w_low) * 0.6

    # ---- 三只乌鸦: 顶部三根连续阴线(中/长阴) ----
    if len(bars) >= 3:
        b1, b2, b3 = bars[-3], bars[-2], bars[-1]
        if (not _is_yang(b1) and not _is_yang(b2) and not _is_yang(b3) and
                b1.close > b2.close > b3.close and
                _body_len(b3) >= MIN_BODY and _body_len(b1) >= MIN_BODY and
                in_high_zone):
            hits.append(PatternHit(
                name="三只乌鸦", signal="看跌", position="高位",
                description="上涨趋势顶部连续三根阴线收盘递减,空头持续打压,强烈看跌信号",
                bars=[-3, -2, -1],
            ))

    # ---- 黑三兵: 三根连续下跌小阴线(实体小) ----
    if len(bars) >= 3:
        b1, b2, b3 = bars[-3], bars[-2], bars[-1]
        if (not _is_yang(b1) and not _is_yang(b2) and not _is_yang(b3) and
                b1.close > b2.close > b3.close and
                all(_body_len(b) < MIN_BODY * 2 for b in (b1, b2, b3)) and
                in_high_zone):
            hits.append(PatternHit(
                name="黑三兵", signal="看跌", position="高位",
                description="三根连续下跌的小阴线,阴跌趋势确立,弱势信号",
                bars=[-3, -2, -1],
            ))

    # ---- 空方炮: 阴-阳-阴 序列 ----
    if len(bars) >= 3:
        b1, b2, b3 = bars[-3], bars[-2], bars[-1]
        if (not _is_yang(b1) and _is_yang(b2) and not _is_yang(b3) and
                b3.close < b1.close and in_high_zone):
            hits.append(PatternHit(
                name="空方炮", signal="看跌", position="高位",
                description="阴-阳-阴序列,反弹被再次打压,空方占优,跌势延续",
                bars=[-3, -2, -1],
            ))

    # ---- 倾盆大雨: 大阳线后低开大阴线 ----
    if len(bars) >= 2:
        b1, b2 = bars[-2], bars[-1]
        if (_is_yang(b1) and not _is_yang(b2) and
                _body_len(b1) >= MIN_BODY and _body_len(b2) >= MIN_BODY and
                b2.open < b1.close and b2.close <= b1.open and
                b2.close < b1.close * 0.98 and in_high_zone):
            hits.append(PatternHit(
                name="倾盆大雨", signal="看跌", position="高位",
                description="大阳线后低开大阴线,多头被全面压制,顶部反转信号",
                bars=[-2, -1],
            ))

    # ---- 黄昏之星: 长阳→星线→长阴 ----
    if len(bars) >= 3:
        b1, b2, b3 = bars[-3], bars[-2], bars[-1]
        if (_is_yang(b1) and not _is_yang(b3) and
                _body_len(b2) < _body_len(b1) * 0.3 and
                _body_len(b1) >= MIN_BODY and _body_len(b3) >= MIN_BODY and
                b3.close < b1.close * 0.98 and in_high_zone):
            hits.append(PatternHit(
                name="黄昏之星", signal="看跌", position="高位",
                description="长阳→星线(十字/小实体)→长阴,经典顶部反转形态",
                bars=[-3, -2, -1],
            ))

    # ---- 看跌尽头线: 次日小实体完全位于首根长下影范围内 ----
    if len(bars) >= 2:
        b1, b2 = bars[-2], bars[-1]
        if (_shadow_bottom(b1) >= _body_len(b1) * SHADOW_RATIO and
                _body_len(b2) < _body_len(b1) * 0.5 and
                b2.high <= b1.low + (b1.open if _is_yang(b1) else b1.close) and
                in_high_zone):
            hits.append(PatternHit(
                name="看跌尽头线", signal="看跌", position="高位",
                description="次日小实体K线完全位于首根长下影线范围之内,探底失败跌势未尽",
                bars=[-2, -1],
            ))

    # ---- 兄弟剃平头: 两根或多根最高价同一水平 ----
    if len(bars) >= 2:
        b1, b2 = bars[-2], bars[-1]
        if (abs(b1.high - b2.high) / max(b1.high, b2.high) < 0.005 and
                not _is_yang(b2) and in_high_zone):
            hits.append(PatternHit(
                name="兄弟剃平头", signal="看跌", position="高位",
                description="顶部两根K线最高价同一水平,多头无法再创新高,顶部确认",
                bars=[-2, -1],
            ))

    # ---- 二级倒锤头: 连续两个倒锤头(上影长) ----
    if len(bars) >= 2:
        b1, b2 = bars[-2], bars[-1]
        if (_body_len(b1) >= MIN_BODY and _body_len(b2) >= MIN_BODY and
                _shadow_top(b1) >= _body_len(b1) * 2 and
                _shadow_top(b2) >= _body_len(b2) * 2 and
                in_high_zone):
            hits.append(PatternHit(
                name="二级倒锤头", signal="看跌", position="高位",
                description="上涨趋势极高价位区连续两个倒锤头线,上攻乏力滞涨见顶",
                bars=[-2, -1],
            ))


def format_patterns(hits: list[PatternHit]) -> str:
    """格式化识别结果(供 AI 助手 / API 使用)。"""
    if not hits:
        return "近期未识别到典型K线组合形态。"
    lines = [f"识别到 {len(hits)} 个K线形态:"]
    for h in hits:
        lines.append(f"- 【{h.name}】信号: {h.signal} 位置: {h.position or '未知'}")
        lines.append(f"  {h.description}")
    return "\n".join(lines)
