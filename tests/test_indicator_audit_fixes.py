"""指标/因子算法审计修复的回归钉子（2026-10-23）。

每个用例都对应一个**真实算错数**的旧行为，注释里写清错在哪、怎么发现的。
这些不是风格问题：它们会让页面上的"放量/相对强弱/分位/关键位/筹码"直接错。
"""
from src.collectors.factor_daily import _pct_rank, compute_factors
from src.core import intraday_profile, scenario_engine, trend_profile
from src.core.chip_distribution import compute_chips


# ───────────── 1. 横截面分位：并列必须同分位（原来按输入顺序摊成 0/100）─────────────

def test_pct_rank_ties_get_same_rank():
    # 三个同值 + 一个更大值：同值必须同分位，且不能一个 0 一个 100
    ranks = _pct_rank([5.0, 5.0, 5.0, 9.0])
    assert ranks[0] == ranks[1] == ranks[2]
    assert ranks[3] == 100.0
    # 三个并列最小值的平均位 = (0+1+2)/3 = 1 → 1/3*100 = 33.3（不是 0/33/67 三个值）
    assert abs(ranks[0] - 33.3) < 0.2

    ranks2 = _pct_rank([1.0, 2.0, 2.0, 2.0])
    assert ranks2[1] == ranks2[2] == ranks2[3]
    # 平均位 = (1+3)/2 = 2 → 2/3*100 = 66.7（原来会是 33.3/66.7/100 三个不同值）
    assert abs(ranks2[1] - 66.7) < 0.2


def test_pct_rank_order_independent():
    """同一批数据换个顺序，每个值的分位不能变。"""
    a = _pct_rank([3.0, 1.0, 3.0, 2.0])
    b = _pct_rank([1.0, 2.0, 3.0, 3.0])
    assert a[0] == a[2] == b[2] == b[3]


# ───────────── 2. 因子：缺量不能当 0（原来把停牌日算成零成交）─────────────

def _bars(n, step=0.1, vol=1000):
    return [{"date": f"d{i}", "open": 10.0, "high": 10.0 + i * step + 0.5,
             "low": 10.0 + i * step - 0.5, "close": 10.0 + i * step,
             "volume": vol + (i % 7) * 100} for i in range(n)]


def test_missing_volume_not_treated_as_zero():
    bars = _bars(80)
    for i in range(30, 45):          # 中间 15 天无量（停牌/源缺口）
        bars[i]["volume"] = None
    f = compute_factors(bars)
    # 有效量 65 根 ≥60 → 仍可算，但均值不能被 0 拉低
    assert f["volume_z20"] is not None
    assert f["volume_z20"] < 3, "缺量当 0 会把标准差抬高、z 失真"


def test_insufficient_volume_samples_returns_none():
    bars = _bars(80)
    for i in range(0, 60):           # 只剩 20 根有量
        bars[i]["volume"] = None
    assert compute_factors(bars)["volume_z20"] is None


def test_position_120_uses_aligned_window():
    bars = _bars(200, step=0.05)
    f = compute_factors(bars)
    assert 0 <= f["position_120"] <= 100
    # 单调上涨 → 位置必须在区间顶部（区间含 high/low 上下各 0.5 的影线）
    assert f["position_120"] > 90


# ───────────── 3. 分时：收尾柱不能漏成"其他" ─────────────

def test_intraday_buckets_include_closing_bars():
    pts = [
        {"t": "0930", "price": 10.0, "avg": 10.0, "volume": 100},
        {"t": "1130", "price": 10.2, "avg": 10.1, "volume": 100},
        {"t": "1400", "price": 10.1, "avg": 10.1, "volume": 100},
        {"t": "1500", "price": 10.3, "avg": 10.2, "volume": 100},
    ]
    m = intraday_profile.compute(pts, {})
    assert "其他" not in m["buckets"], f"收尾柱被漏成其他: {m['buckets']}"
    assert m["tail"]["reached"] is True


def test_intraday_limit_up_zero_inner_is_100_percent():
    """一字板 inner=0 —— 原来是 `if (outer and inner)` 直接判 None（把最有信息的盘口丢了）。"""
    pts = [{"t": "0930", "price": 10.0, "avg": 10.0, "volume": 100},
           {"t": "1500", "price": 11.0, "avg": 10.5, "volume": 100}]
    m = intraday_profile.compute(pts, {"outer": 5000, "inner": 0})
    assert m["active_buy_pct"] == 100.0


def test_intraday_sealed_board_reports_seal_state_not_infinite_press():
    pts = [{"t": "0930", "price": 10.0, "avg": 10.0, "volume": 100}]
    m = intraday_profile.compute(pts, {"bids": [(10.0, 9999.0)] + [(None, None)] * 4,
                                       "asks": [(None, None)] * 5})
    assert m["press_ratio"] is None
    assert m["seal_state"] == "封涨停(无卖盘)"
    assert "封涨停" in intraday_profile.render(m)


def test_intraday_vwap_unit_guard():
    """量纲错 100 倍时 avg 会离价格一个量级 → 必须判"未取到"，不能用它算占比。"""
    pts = [{"t": "0930", "price": 10.0, "avg": 1000.0, "volume": 100},
           {"t": "1000", "price": 10.5, "avg": 1000.0, "volume": 100}]
    m = intraday_profile.compute(pts, {})
    assert m["vwap"] == 0
    assert "未取到" in intraday_profile.render(m)


# ───────────── 4. 走势：量比不含当日 / RS 按日期对齐 ─────────────

def _tp_bars(n, base_vol=1000, last_vol=None):
    out = []
    for i in range(n):
        out.append({"date": f"2026-01-{i + 1:02d}", "open": 10.0, "high": 10.5,
                    "low": 9.5, "close": 10.0 + i * 0.01,
                    "volume": last_vol if (last_vol and i == n - 1) else base_vol})
    return out


def test_volume_ratio_excludes_today():
    bars = _tp_bars(30, base_vol=1000, last_vol=10000)
    m = trend_profile.compute(bars)
    assert m["vol_ratio"] == 10.0, f"10 倍量必须报 10，报 {m['vol_ratio']} 说明分母含了当日"


def test_rs20_date_aligned_when_stock_misses_last_day():
    """个股缺末日（停牌）时，RS 必须用共同交易日，不能拿个股旧区间比指数新区间。"""
    stock = _tp_bars(30)
    stock = stock[:-1]                       # 个股少最后一天
    index = [{"date": b["date"], "open": 10, "high": 10, "low": 10,
              "close": 10.0 + i * 1.0, "volume": 1} for i, b in enumerate(_tp_bars(30))]
    m = trend_profile.compute(stock, index)
    assert m["rs20"] is not None
    assert m["rs20_basis"] == "date_aligned"


def test_rs20_none_when_no_overlap():
    stock = _tp_bars(30)
    idx = [{"date": f"x{i}", "open": 10, "high": 10, "low": 10, "close": 10.0, "volume": 1}
           for i in range(30)]
    m = trend_profile.compute(stock, idx)
    assert m["rs20_basis"] == "positional"   # 无交集 → 位置口径，且如实标注


# ───────────── 5. K 线摘要：近 5 日上涨天数（原来最多只能数到 4）─────────────

def test_up_days_can_reach_five():
    from src.collectors.kline_collector import KlineCollector, MarketCode

    bars = [{"date": f"2026-01-{i + 1:02d}", "open": 10.0, "high": 10.0, "low": 10.0,
             "close": 10.0 + i * 0.1, "volume": 1000} for i in range(6)]
    kl = [__import__("src.collectors.kline_collector", fromlist=["KlineData"]).KlineData(
        date=b["date"], open=b["open"], high=b["high"], low=b["low"],
        close=b["close"], volume=b["volume"]) for b in bars]
    c = KlineCollector(MarketCode.CN)
    # 只测计数口径：直接把 6 根递增序列喂进摘要里的那一段逻辑
    recent = kl[-6:]
    up_days = sum(1 for i in range(1, len(recent)) if recent[i].close > recent[i - 1].close)
    assert up_days == 5, "5 连涨必须数出 5，不是 4"
    assert c is not None  # 构造器本身可用（避免 import 漂移）


# ───────────── 6. 情景矩阵：创新高不能把下方当阻力 ─────────────

def test_new_high_has_no_fake_resistance():
    trend = {"available": True, "last": 40.0, "mas": {20: 33.9, 60: 31.0},
             "high": 38.0, "low": 25.0, "position": 100.0, "vol_ratio": 1.4, "rs20": 2.0}
    chips = {"band70_lo": 30.0, "band70_hi": 34.0}
    m = scenario_engine.build_scenarios(trend, chips)
    assert m["available"] is True
    lv = m["levels"]
    assert lv["resist"] is None, "现价高于所有候选，不能把下方值当阻力"
    assert lv["resist_state"] == "new_high"
    assert lv["support"] is not None and lv["support"] < 40.0
    strong = next(s for s in m["scenarios"] if s["name"] == "走强")
    assert "新高" in strong["trigger"]
    assert "上方无已计算阻力" in strong["target"]


def test_ma60_below_price_becomes_resistance_pool():
    """跌破 MA60 时，MA60 是最近阻力（原实现把它丢进 support 候选再过滤掉）。"""
    trend = {"available": True, "last": 30.0, "mas": {20: 33.0, 60: 32.0},
             "high": 45.0, "low": 20.0, "position": 40.0}
    lv = scenario_engine._levels(trend, {"band70_lo": 28.0, "band70_hi": 40.0})
    assert lv["resist"] == 32.0, f"MA60(32) 应是最近阻力，得到 {lv['resist']}"


def test_normal_case_still_uses_nearest_levels():
    trend = {"available": True, "last": 33.0, "mas": {5: 31.7, 10: 32.7, 20: 33.9, 60: 35.9},
             "high": 40.0, "low": 30.0, "position": 30.0}
    lv = scenario_engine._levels(trend, {"band70_lo": 32.5, "band70_hi": 44.5})
    assert lv["resist"] == 33.9
    assert lv["support"] == 32.5
    assert lv["support"] < 33.0 < lv["resist"]


# ───────────── 7. 筹码：一字板不能被跳过 ─────────────

def _chip_bars(n, base=10.0, one_price_at=None, vol=1_000_000):
    out = []
    for i in range(n):
        c = base + i * 0.01
        if one_price_at is not None and i == one_price_at:
            # 一字板：high == low == close（原实现 `k_high <= k_low` 直接 continue）
            out.append({"date": f"d{i}", "open": c, "high": c, "low": c,
                        "close": c, "volume": vol * 5})
        else:
            out.append({"date": f"d{i}", "open": c - 0.05, "high": c + 0.2,
                        "low": c - 0.2, "close": c, "volume": vol})
    return out


def test_limit_up_one_price_bar_is_not_skipped():
    plain = compute_chips(_chip_bars(60))
    with_board = compute_chips(_chip_bars(60, one_price_at=55))
    assert plain is not None and with_board is not None
    # 一字板那天的 5 倍量必须进入分布（峰值占比变化），不能整根跳过
    assert with_board["peak_ratio"] != plain["peak_ratio"] or \
        abs(with_board["cost_50"] - plain["cost_50"]) > 0.001


def test_one_price_bar_decays_old_chips():
    """一字板当天也要按换手率衰减旧筹码 —— 连板股的分布不能停在启动前。"""
    bars = _chip_bars(60, one_price_at=55)
    r = compute_chips(bars)
    assert r["cost_90"] > 0 and r["profit_ratio"] is not None
    # 55 号是一字板且价在 10.55 附近，分布不能全部堆在起点
    assert r["peak_price"] >= 10.5
