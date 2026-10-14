"""数智决策三指标复刻 + 主力意图 L2 增强 (2026-08-30, 周一开盘前上线)。

三指标 = GS策略(趋势) × 主力净流入(L2·明盘口径) × AI机构活跃度(强度)。

- AI机构活跃度: 零调参纯K线波动探测器。源码 7 因子 MAX × 1.2, 阈值 1.56/3/6。
- GS策略: BB0 慢线(MA3/7/13/27 均值) + A0 快线((H+L+2O+6C)/10) 交叉 → G买/S卖。
  ⚠️ 面板/推送/共振的**GS 区间**不在本模块判定 —— 统一走 `src/core/gs_state.py`
  (只消费 `gs_strategy.compute_gs_signals` 的**已确认**信号, 与 K 线图层逐字一致)。
  本模块的 `compute_gs_signal`/`compute_gs_windows` 是**低层内核**(供 `gs_strategy`
  复用), 其 `state` 是原始 a0/bb0 方向, **不是**官方 G/S 区间(会吃末根盘中价而抢跑)。
- L2 主力净流入(明盘口径, 非暗盘): TQ get_more_info.Zjl_HB 成品字段 = 同花顺"主力净额"——按单笔成交金额阈值(特大/大/中/小)汇总的**明盘资金流向**,不包含拆单识别。要做真正的暗盘请用 dark_flow(腾讯逐笔 + .tck 委托号拆单识别)。

盘中实时: 日线走 TQ(get_market_data period=1d 含当日), L2 走 TQ get_more_info。
数据源缺失时显式返回 None / 标注"无数据", 禁止推测编造。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── AI 机构活跃度阈值(同花顺官方, 零调参) ──────────────────────────────
# 实际生效值统一走 `src.core.thresholds` 配置层(env `SIDA_THRESHOLD_*` 可覆盖);
# 下列常量仅为**默认值镜像**(向后兼容: 他处 import LIFE_LINE 做比较), 判定时不再直用。
# 2026-10-11 P2: 原先本模块直用硬编码常量, env 对其无效 → 与 ai_activity(已走配置层)
# 档位不一致。现与 ai_activity 同源读配置层, 两处 level 恒一致。
LIFE_LINE = 1.56      # 生命线默认 = 1.3% × 1.2
STRONG_LINE = 3.0     # 强势线默认 = 2.5% × 1.2
BULL_LINE = 6.0       # 大牛线默认 = 5% × 1.2
ACTIVITY_MULT = 1.2   # 波动放大系数(源码写死)


def _bar_close_open_high_low(b: dict) -> tuple[float, float, float, float]:
    """统一取 bar 的 OHLC(兼容 Bar dataclass / dict / 对象属性)。"""
    def g(k: str) -> float:
        v = b.get(k) if isinstance(b, dict) else getattr(b, k, None)
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    return g("close"), g("open"), g("high"), g("low")


def compute_institution_activity(bars: list[dict]) -> dict | None:
    """AI 机构活跃度(零调参, 纯K线波动探测器)。

    bars: 按日期升序的日K(list[dict] 或 list[Bar]), 至少 2 根。
    返回: {activity, level, life_line, strong_line, bull_line, streak_days, ma5,
           factors, close, is_yang}
    - activity: 当日活跃度 = max(7因子) × 1.2
    - level: 大牛(≥6) / 强势(3~6) / 生命(1.56~3) / 弱(<1.56)
    - streak_days: 活跃度>生命线连续日数(含当日)
    - ma5: 活跃度 5 日均值
    数据不足(bar<2)返回 None。
    """
    if not bars or len(bars) < 2:
        return None
    # 逐日算活跃度(第 0 根无 ref 前置, 活跃度记 None)
    acts: list[float | None] = [None]
    prev_close = _bar_close_open_high_low(bars[0])[0]
    for i in range(1, len(bars)):
        c, o, h, l = _bar_close_open_high_low(bars[i])
        if prev_close <= 0 or o <= 0 or l <= 0:
            acts.append(None)
            prev_close = c
            continue
        upper_shadow = (h - max(c, o)) / max(c, o) * 100          # X_10 上影
        lower_shadow = (min(c, o) - l) / l * 100                  # X_5  下影
        body = (c - o) / o * 100                                  # X_8  实体
        rise = (c - prev_close) / prev_close * 100                # X_6  涨幅
        gap_open = (o - prev_close) / prev_close * 100            # X_7  高开
        factors = [
            upper_shadow,
            lower_shadow,
            body + upper_shadow,       # 实体+上影
            body + lower_shadow,       # 实体+下影
            upper_shadow + lower_shadow,  # 上影+下影
            rise,
            gap_open,
        ]
        acts.append(max(factors) * ACTIVITY_MULT)
        prev_close = c

    cur = acts[-1]
    if cur is None:
        return None

    # 阈值实时读统一配置层(env SIDA_THRESHOLD_* 可覆盖) —— 与 ai_activity 同源, 档位一致
    from src.core import thresholds as _thresholds

    life = _thresholds.life_line()
    strong = _thresholds.strong_line()
    bull = _thresholds.bull_line()

    # level
    if cur >= bull:
        level = "大牛"
    elif cur >= strong:
        level = "强势"
    elif cur >= life:
        level = "生命"
    else:
        level = "弱"

    # 连强天数(活跃度 > 生命线 连续日数, 含当日)
    streak = 0
    for a in reversed(acts):
        if a is not None and a > life:
            streak += 1
        else:
            break

    # 5 日均值(含当日, 只统计非 None)
    valid = [a for a in acts if a is not None]
    ma5 = round(sum(valid[-5:]) / max(1, len(valid[-5:])), 3) if valid else None

    last_bar = bars[-1]
    c, o, h, l = _bar_close_open_high_low(last_bar)
    return {
        "activity": round(cur, 3),
        "level": level,
        "life_line": life,
        "strong_line": strong,
        "bull_line": bull,
        "streak_days": streak,
        "ma5": ma5,
        "is_yang": c >= o,
        "close": c,
        "factors": {
            "upper_shadow": round(max([(h - max(c, o)) / max(c, o) * 100, 0]), 3),
            "lower_shadow": round(max([(min(c, o) - l) / l * 100, 0]), 3),
            "body": round((c - o) / o * 100, 3) if o else None,
        },
    }


def _sma(vals: list[float], n: int) -> float | None:
    """简单均线; 长度不足返回 None。"""
    if len(vals) < n or n <= 0:
        return None
    return sum(vals[-n:]) / n


def compute_gs_signal(bars: list[dict]) -> dict | None:
    """GS 策略**低层内核**(BB0/A0 交叉; 供 `gs_strategy` 复用, 勿直接做面板口径)。

    ⚠️ 面板/推送/共振的 GS 区间请用 `src/core/gs_state.py::resolve_gs_state` —— 见本文件头。
    此处 `state` 只是**原始** a0/bb0 方向(吃末根盘中价, 末根疑似交叉即翻面),
    **不是**官方「G区/S区」(官方非函数指标要等 confirmed)。

    BB0 = (MA3 + MA7 + MA13 + MA27) / 4  慢线(趋势)
    A0  = (H + L + 2O + 6C) / 10          快线(收盘 60% 权重)
    A0 上穿 BB0 → G买; A0 下穿 BB0 → S卖。

    返回: {signal, state, bb0, a0, last_cross}
    - signal: 最近一次交叉方向 "G"(买) / "S"(卖) / None(无交叉)
    - state: **低层**当前方向 "G区"(a0>bb0) / "S区"(a0<bb0)
    数据不足(bar<27)返回 None。
    """
    if not bars or len(bars) < 5:
        return None
    closes: list[float] = []
    a0_series: list[float] = []
    bb0_series: list[float] = []
    for b in bars:
        c, o, h, l = _bar_close_open_high_low(b)
        closes.append(c)
        a0_series.append((h + l + 2 * o + 6 * c) / 10)
        bb0s = []
        for n in (3, 7, 13, 27):
            m = _sma(closes, n)
            if m is None:
                bb0s = None
                break
            bb0s.append(m)
        bb0_series.append(sum(bb0s) / 4 if bb0s else None)

    # 找最近一次交叉
    last_cross = None
    last_cross_idx = -1
    for i in range(1, len(a0_series)):
        if bb0_series[i] is None or bb0_series[i - 1] is None:
            continue
        prev_diff = a0_series[i - 1] - bb0_series[i - 1]
        cur_diff = a0_series[i] - bb0_series[i]
        if prev_diff <= 0 < cur_diff:
            last_cross = "G"
            last_cross_idx = i
        elif prev_diff >= 0 > cur_diff:
            last_cross = "S"
            last_cross_idx = i

    a0 = a0_series[-1]
    bb0 = bb0_series[-1]
    if bb0 is None:
        return None
    state = "G区" if a0 > bb0 else "S区"
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    return {
        "signal": last_cross,
        "state": state,
        "bb0": round(bb0, 4),
        "a0": round(a0, 4),
        "last_cross_idx": last_cross_idx,
        "close": closes[-1],
        "ma5": round(ma5, 4) if ma5 is not None else None,
        "ma20": round(ma20, 4) if ma20 is not None else None,
    }


def compute_gs_windows(bars: list[dict], windows: tuple[int, ...] = (1, 3, 5)) -> dict | None:
    """数智决策 1/3/5 日序列 + 0 轴穿越(B6.8, 接口文档点名的 P0 缺口)。

    对 A0−BB0 差值序列, 给每个窗口统计: 窗口内 0 轴穿越次数、窗口累计差值、窗口末方向;
    并给出最近一次 0 轴穿越的方向与距今天数(0 = 最后一根)。

    ⚠️ 返回体里的 `state` 是**原始 0 轴方向**(dif 符号), **不是**官方 G/S 区间 ——
    面板/推送的区间请用 `src/core/gs_state.py::resolve_gs_state`(与 K 线图层同源)。
    本字段保留仅作低层对照(无已确认语义, 会吃末根盘中价)。
    """
    if not bars or len(bars) < 5:
        return None
    closes: list[float] = []
    a0s: list[float] = []
    bb0s: list[float | None] = []
    for b in bars:
        c, o, h, l = _bar_close_open_high_low(b)
        closes.append(c)
        a0s.append((h + l + 2 * o + 6 * c) / 10)
        parts: list[float] = []
        for n in (3, 7, 13, 27):
            m = _sma(closes, n)
            if m is None:
                parts = []
                break
            parts.append(m)
        bb0s.append(sum(parts) / 4 if parts else None)

    dif: list[float | None] = [
        None if bb0s[i] is None else a0s[i] - bb0s[i] for i in range(len(bars))
    ]
    valid = [(i, d) for i, d in enumerate(dif) if d is not None]
    if len(valid) < 2:
        return None

    last_cross_dir: str | None = None
    last_cross_bars_ago: int | None = None
    for k in range(1, len(valid)):
        _i_prev, d_prev = valid[k - 1]
        i_cur, d_cur = valid[k]
        if d_prev <= 0 < d_cur:
            last_cross_dir, last_cross_bars_ago = "G", len(bars) - 1 - i_cur
        elif d_prev >= 0 > d_cur:
            last_cross_dir, last_cross_bars_ago = "S", len(bars) - 1 - i_cur

    series: dict[str, dict] = {}
    for w in windows:
        w = max(1, int(w))
        seg = [d for _, d in valid[-(w + 1):]]
        crossings = sum(
            1
            for k in range(1, len(seg))
            if (seg[k - 1] <= 0 < seg[k]) or (seg[k - 1] >= 0 > seg[k])
        )
        series[f"{w}d"] = {
            "crossings": crossings,
            "cum_dif": round(sum(seg), 4),
            "last_dir": "G" if seg and seg[-1] > 0 else ("S" if seg and seg[-1] < 0 else None),
        }

    return {
        "dif": round(valid[-1][1], 4),
        "state": "G区" if valid[-1][1] > 0 else "S区",
        "last_cross": {"direction": last_cross_dir, "bars_ago": last_cross_bars_ago},
        "windows": series,
    }


def _bars_to_dicts(bars) -> list[dict]:
    """Bar 对象列表 → 统一 dict 列表(按 date 升序)。"""
    out: list[dict] = []
    for b in bars:
        if isinstance(b, dict):
            out.append(b)
        else:
            out.append({
                "date": getattr(b, "date", None),
                "open": getattr(b, "open", None),
                "high": getattr(b, "high", None),
                "low": getattr(b, "low", None),
                "close": getattr(b, "close", None),
                "volume": getattr(b, "volume", None),
            })
    return out


def _tencent_symbol(symbol: str) -> str | None:
    """6 位 A 股代码 → 腾讯/新浪风格(sz002361 / sh600519); 无法识别返回 None。"""
    s = (symbol or "").strip().lower()
    if s.startswith(("sz", "sh", "bj")):
        return s
    if s.isdigit() and len(s) == 6:
        if s[0] in ("6", "9") or s.startswith("688"):
            return "sh" + s
        if s[0] in ("0", "2", "3"):
            return "sz" + s
    return None


def _fallback_bars(symbol: str, market: str, days: int) -> list[dict]:
    """主链路(Engine)全挂时的**直连兜底**: 东财 → 新浪 → 腾讯, 任一源有数即返回。

    2026-09-02 生产实测(多 vendor 同时不可达):
      - 腾讯 web.ifzq.gtimg.cn 对生产出口 IP 返回 **WAF 501 拦截页**(重定向到
        waf.tencent.com/501page.html), 带 UA/Referer 同样被拦 → 是 IP 级风控, 非接口变更
      - 东财 push2his 部分时段 RemoteDisconnected
      - 新浪 money.finance.sina.com.cn 稳定 200(个股/指数同接口)
    故兜底按"东财 → 新浪 → 腾讯"顺序, 任一源出数即用; 全空才返回 [](调用方显式"无数据")。

    仅 CN 个股走兜底(腾讯/新浪/东财的港美符号规则不同, 不在此 blanket 套用)。
    """
    if (market or "").upper() != "CN":
        return []
    try:
        from marketdata.vendors.kline import (
            fetch_eastmoney_kline,
            fetch_sina_index_kline,
            fetch_tencent_kline_raw,
        )
    except Exception as e:  # noqa: BLE001  # marketdata 包不可用则无从兜底
        logger.debug("fetch_bars 兜底不可用(marketdata 未安装): %s", e)
        return []

    tsym = _tencent_symbol(symbol)
    if not tsym:
        return []
    secid = ("1." if tsym.startswith("sh") else "0.") + tsym[2:]  # 东财: 1.=沪 / 0.=深
    for name, fn, arg in (
        ("东财", fetch_eastmoney_kline, secid),
        ("新浪", fetch_sina_index_kline, tsym),
        ("腾讯", fetch_tencent_kline_raw, tsym),
    ):
        try:
            bars = _bars_to_dicts(fn(arg, days))
        except Exception as e:  # noqa: BLE001
            logger.warning("fetch_bars %s 兜底源 %s 失败: %s", symbol, name, e)
            continue
        if bars:
            logger.info("fetch_bars %s 兜底命中 %s(%d 根)", symbol, name, len(bars))
            return bars
    return []


# ── 读库优先参数(与 src/web/api/klines.py::_pg_klines 同口径) ──────────────
#: PG 命中需达到的最少根数; 低于此视作「空窗」→ 回落联网拿完整历史(避免库里只有
#: 三五根就把历史截断, 让 GS(需≥27)等消费方算不出)。
_PG_MIN_BARS = 30
#: PG 最新 bar 距今允许的最大滞后(天): 超过视为陈旧网关快照 → 全量联网
#: (与 get_klines 的 12 天陈旧策略一致, 覆盖春节级长假)。
_MAX_PG_LAG_DAYS = 12
#: 「仅最新 bar 联网」时向原联网通道请求的小窗口(足够覆盖周末/短假缺口)。
_LIVE_TAIL_DAYS = 5
#: 自然日 → bar 数换算系数(交易日 ≈ 0.68×自然日); 读库窗口放大 + 缓冲后截取末 days 根,
#: 保持与联网同口径的「长度语义」。
_PG_LOOKBACK_FACTOR = 1.6


def _pg_daily_bars(symbol: str, market: str, days: int) -> list[dict]:
    """PG klines hypertable 读前复权日K(升序, 单位=**股**)。库/表缺失/非 CN → [](fail-soft)。

    与 `src/web/api/klines.py::_pg_klines` 同一张表/同过滤条件
    (`period='1d'` / `source='tencent'` / `adjust='qfq'`), 但走**仓储层**
    `src/db/klines_repo.load_qfq_bars` —— core 不 import src.web(分层约束)。

    `days` 是「要几根 bar」: 按自然日放大窗口读库后截取**末 days 根**, 使返回序列
    在条数语义上与联网路径一致(联网也是返回最近 days 根)。
    """
    if (market or "CN").upper() != "CN":
        return []
    try:
        from src.db.klines_repo import load_qfq_bars
    except Exception as e:  # noqa: BLE001 仓储层不可用 → 无从读库
        logger.debug("fetch_bars 读库不可用(仓储层导入失败): %s", e)
        return []
    n = max(int(days or 60), 1)
    lookback = int(n * _PG_LOOKBACK_FACTOR) + 10
    try:
        bars = load_qfq_bars(symbol, market, days=lookback) or []
    except Exception as e:  # noqa: BLE001 查库失败 → 视为空窗(回落联网)
        logger.warning("fetch_bars %s 读库失败, 回落联网: %s", symbol, e)
        return []
    return bars[-n:]


def _network_bars(symbol: str, market: str, days: int) -> list[dict]:
    """原两级联网取数: marketdata Engine(TQ 优先, 含当日) → `_fallback_bars` 直连兜底。

    读库未命中/仅缺当日时, 当日/最新 bar 仍走本通道(单位: 日线 volume=股)。
    """
    from src.core.marketdata_client import get_market_data

    try:
        bars = _bars_to_dicts(get_market_data().klines(symbol, market=market, days=days))
        if bars:
            return bars
        logger.warning("fetch_bars %s 联网主链路返回空, 转直连兜底", symbol)
    except Exception as e:  # noqa: BLE001
        logger.warning("fetch_bars %s 联网主链路失败: %s, 转直连兜底", symbol, e)
    return _fallback_bars(symbol, market, days)


def fetch_bars(symbol: str, market: str = "CN", days: int = 60) -> list[dict]:
    """日K取数(**读库优先**, AGENTS『K线读取走 PG hypertable 优先(get_klines())』)。

    数智决策面板/工作台/聚宝盆/共振此前**每次全程联网现拉**(东财/新浪/腾讯直连), 与
    已落库的 PG `klines` hypertable 脱节。现改为:

      1. **历史日K 读库**: `_pg_daily_bars`(PG `klines` period='1d'/source='tencent'/
         adjust='qfq', 单位=股, 与 `get_klines()` 同源)→ 命中且够厚(≥min(30,days))
         且不陈旧(滞后 ≤ 12 天)时作为**基底**。
      2. **仅当日/最新 bar 走原联网通道**: 库里未含今日时, 只向 `_network_bars`
         (Engine TQ 优先 → 直连兜底)要**小窗口**, 取日期晚于库末根的 bar 追加
         (覆盖周末/短假缺口)。库里**已含今日 → 零联网**直接返回。
      3. **显式回落/标缺**: PG 空窗/过薄/陈旧 → 全量联网(改造前原路径); 联网失败
         → 回库(序列止于末根日期, 显式缺当日, **不编造**)。

    返回格式/长度语义与改造前**逐字段一致**: 单 bar =
    `{date, open, high, low, close, volume}`, 日线 volume=**股**。消费方(gs_strategy
    消费链/共振/聚宝盆/niuxiong/trend_pilot)无需改动。
    """
    n = max(int(days or 60), 1)
    pg = _pg_daily_bars(symbol, market, n)
    if len(pg) >= min(_PG_MIN_BARS, n):
        from datetime import date as _date
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _TZ

        today = _dt.now(_TZ("Asia/Shanghai")).date().isoformat()
        last = str(pg[-1].get("date") or "")[:10]
        if last >= today:
            return pg  # 库里已含今日 → 零联网
        try:
            lag = (_date.fromisoformat(today) - _date.fromisoformat(last)).days
        except ValueError:
            lag = _MAX_PG_LAG_DAYS + 1
        if lag <= _MAX_PG_LAG_DAYS:
            live = _network_bars(symbol, market, min(n, _LIVE_TAIL_DAYS))
            extra = [b for b in live if str(b.get("date") or "")[:10] > last]
            # 无更新 bar(非交易日/盘前) → 回库基底, 不编造当日
            return pg + extra if extra else pg
        logger.info("fetch_bars %s PG 滞后 %d 天(> %d), 回落全量联网", symbol, lag, _MAX_PG_LAG_DAYS)
    # PG 空窗/过薄/陈旧 → 全量联网(改造前原路径: Engine TQ 优先 → 直连兜底)
    return _network_bars(symbol, market, n)


def fetch_tq_l2(symbol: str, market: str = "CN") -> dict | None:
    """TQ get_more_info 的 L2 成品字段(盘中实时)。失败返回 None。

    ⚠️ 单位: 金额类字段是**万元**(不是元) —— 见 `src/core/mainflow_tri.py` 文件头
    ("腾讯四档(元) + 同花顺DDE官方(万元) + TQ Zjl_HB(万元) → 统一万元")与前端同口径
    格式化(L2Tab/DecisionPioneerCard 的 fmtWan)。消费方要元自行 ×1e4。

    返回: {zjl_hb(主力净流入,**万元**), zjl(万元), cancel_buy(万元), cancel_sell(万元),
           l2_tick_num, l2_order_num, total_buy_vol, total_sell_vol}
    """
    from src.core.marketdata_client import get_market_data

    try:
        items = get_market_data().more_info([symbol], market=market)
        if not items:
            return None
        m = items[0]
        return {
            "zjl_hb": getattr(m, "zjl_hb", None),
            "zjl": getattr(m, "zjl", None),
            "cancel_buy": getattr(m, "cancel_buy", None),
            "cancel_sell": getattr(m, "cancel_sell", None),
            "l2_tick_num": getattr(m, "l2_tick_num", None),
            "l2_order_num": getattr(m, "l2_order_num", None),
            "total_buy_vol": getattr(m, "total_buy_vol", None),
            "total_sell_vol": getattr(m, "total_sell_vol", None),
            "volume_ratio": getattr(m, "volume_ratio", None),
            "turnover_rate": getattr(m, "turnover_rate", None),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("decision_pioneer fetch_tq_l2 %s failed: %s", symbol, e)
        return None


def _l2_summary(l2: dict | None) -> dict:
    """L2 主力净流入结构化解读(对齐同花顺"主力资金"明盘口径, 非暗盘)。

    明盘口径: 按单笔成交金额汇总的特大/大/中/小单净额, 同花顺官方"主力净额"字段。
    暗盘口径(同花顺"暗盘资金")需要拆单识别, 走 dark_flow(腾讯逐笔 + .tck)。
    """
    if not l2:
        return {"available": False}
    zjl_hb = l2.get("zjl_hb")
    cancel_buy = l2.get("cancel_buy")
    cancel_sell = l2.get("cancel_sell")
    net = zjl_hb if isinstance(zjl_hb, (int, float)) else None
    direction = None
    if net is not None:
        direction = "净流入" if net > 0 else ("净流出" if net < 0 else "平衡")
    cancel_total = None
    if isinstance(cancel_buy, (int, float)) and isinstance(cancel_sell, (int, float)):
        cancel_total = cancel_buy + cancel_sell
    # P2 撤单率(2026-09-05): 撤单/(买卖+撤单)，>80%=假挂单多；
    # bias>0买方撤多(托单虚,偏空)，<0卖方撤多(压单虚,偏多)。阈值初版，实盘再调。
    cancel_rate = None
    cancel_bias = None
    cancel_signal = None
    tb = l2.get("total_buy_vol")
    ts = l2.get("total_sell_vol")
    if (
        isinstance(cancel_total, (int, float))
        and isinstance(tb, (int, float))
        and isinstance(ts, (int, float))
        and (tb + ts + cancel_total) > 0
    ):
        cancel_rate = round(cancel_total / (tb + ts + cancel_total) * 100, 1)
    if (
        isinstance(cancel_total, (int, float))
        and isinstance(cancel_buy, (int, float))
        and isinstance(cancel_sell, (int, float))
        and cancel_total > 0
    ):
        cancel_bias = round((cancel_buy - cancel_sell) / cancel_total * 100, 1)
    if cancel_rate is not None and cancel_rate >= 80:
        cancel_signal = "撤单频繁(假挂单多)"
    elif cancel_bias is not None and cancel_bias >= 20:
        cancel_signal = "买方撤单多(托单虚)"
    elif cancel_bias is not None and cancel_bias <= -20:
        cancel_signal = "卖方撤单多(压单虚)"
    return {
        "available": True,
        # Zjl_HB 单位=万元(通达信 get_more_info 约定, 与成交额 Amount 同量纲)。
        # 统一转元返回, 上层(前端 fmtWan / 推送 /1e4 / 选股池)按"元→万"换算口径一致。
        "zjl_hb": net * 1e4 if net is not None else None,
        "direction": direction,
        "cancel_buy": cancel_buy,
        "cancel_sell": cancel_sell,
        "cancel_total": cancel_total,
        "cancel_rate": cancel_rate,
        "cancel_bias": cancel_bias,
        "cancel_signal": cancel_signal,
        "l2_tick_num": l2.get("l2_tick_num"),
        "l2_order_num": l2.get("l2_order_num"),
    }


def fetch_decision_pioneer(symbol: str, market: str = "CN") -> dict:
    """组合三指标 + L2 主力净流入 + 主力意图 → 完整快照(盘中实时)。

    返回结构:
    {
      symbol, market,
      institution_activity: {...}|None,   # AI机构活跃度
      gs: {...}|None,                      # GS区间(单一事实源, 见 gs_state.resolve_gs_state)
      l2: {...},                           # TQ L2 主力净流入(含 available 标记)
      main_intent: {...}|None,             # 主力意图(腾讯逐笔, 复用 dark_flow)
      data_time: str,
    }
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # 取数窗口与 K 线图层(_build_layer_data)对齐: 同一份 bars 才谈得上「与图表逐字一致」
    # (窗口不同 → 窗口内是否含已确认交叉可能不同 → 区间/趋势会不一致)。
    bars = fetch_bars(symbol, market=market, days=120)
    act = compute_institution_activity(bars)
    # GS 区间:**单一事实源** —— 只消费 gs_strategy 输出(与 K 线图层逐字一致)。
    # 此前这里用 compute_gs_signal.state(自带 a0/bb0 比较, 吃末根盘中价),
    # 末根疑似反向交叉时面板立刻翻面, 与图表(停在上一根已确认信号)打架。
    from src.core.gs_state import resolve_gs_state

    gs_eval = resolve_gs_state(bars)
    gs = {
        "state": gs_eval["zone"],              # 兼容旧字段名: 现值=已确认区间(与图表同源)
        "zone": gs_eval["zone"],
        "label": gs_eval["label"],
        "signal": gs_eval["signal"],           # 最近一次**已确认**信号(图表实心 ▲/▼)
        "signal_date": gs_eval["signal_date"],
        "signal_confirmed": gs_eval["signal_confirmed"],
        "pending": gs_eval["pending"],         # 末根疑似交叉(图表空心 ○)
        "pending_side": gs_eval["pending_side"],
        "pending_date": gs_eval["pending_date"],
        "pending_label": gs_eval["pending_label"],
        "note": gs_eval["note"],
        "available": gs_eval["available"],
        "as_of": gs_eval["as_of"],             # 数据截止(末根 bar 日期)
        # 展示项(规格 §1 面板要求可见的快/慢线): 与区间同源, 不参与判定
        "a0": gs_eval["a0"],                   # 快线
        "bb0": gs_eval["bb0"],                 # 慢线
    }
    # B6.8: 1/3/5 日序列 + 0 轴穿越(接口文档点名的 P0 缺口)
    gs_windows = compute_gs_windows(bars)
    l2 = fetch_tq_l2(symbol, market=market)

    # 主力意图(复用现有 dark_flow, 失败不阻塞三指标)
    main_intent = None
    try:
        from marketdata.symbol import Symbol as MDSymbol
        from src.core.dark_flow import compute_dark_flow
        mdsym = MDSymbol.parse(symbol, market)
        dark = compute_dark_flow(mdsym)
        if dark:
            main_intent = {
                "main_net": dark.get("main_net"),
                "big_net": dark.get("big_net"),
                "mid_net": dark.get("mid_net"),
                "small_net": dark.get("small_net", dark.get("retail_net")),
                "main_intensity": dark.get("main_intensity"),
                "main_buy_ratio": dark.get("main_buy_ratio"),
                "signal": dark.get("signal"),
                "data_status": dark.get("data_status"),
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("decision_pioneer main_intent %s failed: %s", symbol, e)

    return {
        "symbol": symbol,
        "market": market,
        "institution_activity": act,
        "gs": gs,
        "gs_windows": gs_windows,
        "l2": _l2_summary(l2),
        "main_intent": main_intent,
        "data_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    }


def decision_pioneer_text(symbol: str, market: str = "CN") -> str:
    """三指标文本摘要(AI 助手工具 / 企微推送用)。"""
    d = fetch_decision_pioneer(symbol, market=market)
    parts: list[str] = []

    act = d.get("institution_activity")
    if act:
        parts.append(
            f"机构活跃度{act['activity']:.2f}({act['level']}, 连强{act['streak_days']}日"
            f"{', 5日均'+str(act['ma5']) if act.get('ma5') is not None else ''})"
        )
    else:
        parts.append("机构活跃度: 无数据")

    gs: dict = d.get("gs") or {}
    zone = gs.get("state")            # = gs_state.zone(已确认区间, 与 K 线图层同源)
    if zone or gs.get("pending"):
        sig = {"G": "G买", "S": "S卖"}.get(gs.get("signal"), "无")
        if zone:
            txt = f"当前{zone}(已确认), 最近已确认信号{sig}"
            if gs.get("signal_date"):
                txt += f"({gs['signal_date']})"
            if gs.get("pending"):
                txt += f"; {gs.get('pending_date') or '末根'}疑似转{gs.get('pending_side')}待确认"
        else:
            txt = f"待确认(尚无已确认区间), 末根疑似{gs.get('pending_side')}"
        parts.append(f"GS策略: {txt}")
    else:
        parts.append("GS策略: 无数据")

    l2 = d.get("l2") or {}
    if l2.get("available"):
        zjl = l2.get("zjl_hb")
        zjl_txt = f"{zjl / 1e4:+.0f}万" if isinstance(zjl, (int, float)) else "无"
        parts.append(f"主力净流入(L2·TQ): {zjl_txt}({l2.get('direction') or '平衡'})")
    else:
        parts.append("主力净流入(L2·TQ): 无数据")

    mi = d.get("main_intent")
    if mi and mi.get("data_status") != "insufficient":
        mn = mi.get("main_net") or 0
        parts.append(f"主力意图(逐笔): 主力净额{mn / 1e4:+.0f}万")
    elif mi:
        parts.append("主力意图(逐笔): 数据不足")

    return " | ".join(parts)
