# -*- coding: utf-8 -*-
"""决策先锋四项增强单测: 暗盘 L2 / 1-3-5日+0轴 / 联合选股 / 回测。

全部 mock 掉外部数据源(thsdk / 网络), 只测**口径与判定逻辑**, 保证离线可跑。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import decision_pioneer as dp  # noqa: E402
from src.core import fund_flow_nd as fnd  # noqa: E402
from src.core import decision_backtest as dbt  # noqa: E402
from src.core import market_scan as ms  # noqa: E402


def _mk_bars(n=40, trend=-0.05, final_surge=True):
    """构造日K: 末根大涨 → G 信号 + 高活跃度 + OHLC 暗盘净额为正。"""
    bars = []
    price = 20.0
    for i in range(n):
        o = price
        price += trend
        c = price
        bars.append({"date": f"2026-0{(i // 28) + 5}-{(i % 28) + 1:02d}", "open": o,
                     "high": max(o, c) + 0.05, "low": min(o, c) - 0.05,
                     "close": c, "volume": 100000})
    if final_surge:
        o = bars[-1]["close"]
        c = o * 1.20
        bars.append({"date": "2026-08-31", "open": o, "high": c + 0.05,
                     "low": o - 0.05, "close": c, "volume": 500000})
    return bars


# ---------------------------------------------------------------------------
# ① 1/3/5 日 + 0 轴
# ---------------------------------------------------------------------------

def test_daily_dark_series_returns_per_day_net():
    """逐日暗盘序列: 收盘越靠上 net 越正; 字段非法 → None(不按 0 补)。"""
    bars = _mk_bars(n=5, final_surge=False)
    s = fnd.daily_dark_series(bars)
    assert len(s) == len(bars)
    assert all(x["approximation"] is True for x in s)
    # 跌势 bar(收盘接近最低) → 分摊净额为负
    assert all(x["net"] < 0 for x in s)
    # 脏数据(volume=0) → None
    bad = [{"date": "2026-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}]
    assert fnd.daily_dark_series(bad)[0]["net"] is None


def test_sum_or_none_refuses_missing():
    """含 None 的序列求和 → None(不允许把缺失当 0 累加)。"""
    assert fnd._sum_or_none([1.0, 2.0]) == 3.0
    assert fnd._sum_or_none([1.0, None]) is None
    assert fnd._sum_or_none([]) is None


def test_zero_axis_cross_up_and_down():
    """官方: 上穿 0 轴=看多, 下穿 0 轴=利空。"""
    up = fnd.zero_axis_cross([-100.0, 200.0])
    assert up["cross"] == "上穿0轴" and up["signal"] == "看多"

    down = fnd.zero_axis_cross([200.0, -100.0])
    assert down["cross"] == "下穿0轴" and down["signal"] == "利空"

    flat = fnd.zero_axis_cross([100.0, 150.0])
    assert flat["cross"] == "无穿越" and flat["signal"] == "流入"

    assert fnd.zero_axis_cross([None, 100.0])["cross"] is None
    assert fnd.zero_axis_cross([100.0])["cross"] is None


def test_fund_flow_nd_1d_complete_nd_refuses_partial():
    """当日净额 = 明盘 + 暗盘(完整可得); N>1 日明盘历史无源 → net_nd 必须 None。"""
    bars = _mk_bars(n=40)
    r = fnd.fund_flow_nd("002361", bars=bars, days=1,
                         ming_net_today=1000.0, dark_net_today=2000.0,
                         fetch_today=False)
    assert r["net_1d"] == 3000.0
    assert r["ming_1d"] == 1000.0 and r["dark_1d"] == 2000.0

    r3 = fnd.fund_flow_nd("002361", bars=bars, days=3, fetch_today=False)
    # 明盘历史无源 → 不合成净额(口径不完整就不给数)
    assert r3["ming_nd"] is None
    assert r3["net_nd"] is None
    assert "明盘历史无源" in (r3["note"] or "")
    # 暗盘 N 日仍可算(对照项), 且逐日序列长度 = N
    assert r3["dark_nd"] is not None
    assert len(r3["daily"]) == 3
    assert all(d["approximation"] for d in r3["daily"])


# ---------------------------------------------------------------------------
# ② 暗盘 L2 主线
# ---------------------------------------------------------------------------

def test_dark_flow_l2_marks_source_and_confidence(monkeypatch):
    """L2 逐笔成功 → source/confidence 标注为 L2, 且净额透传拆单识别结果。"""
    import src.core.dark_flow_l2 as dfl2

    monkeypatch.setattr("src.core.dark_l2.fetch_l2_ticks",
                        lambda code, source: [{"d": "B", "amt": 100.0, "vol": 1, "t": "09:30:00"}])
    monkeypatch.setattr("src.core.dark_flow._detect_split_orders",
                        lambda ticks, prev_close=None: {"net": 1234.0, "buy_amt": 2000.0,
                                                        "sell_amt": 766.0, "groups": [1, 2]})
    monkeypatch.setattr(dfl2, "_prev_close", lambda s: 10.0)

    r = dfl2.compute_dark_flow_l2("002361")
    assert r is not None
    assert r["net"] == 1234.0
    assert r["source"] == dfl2.DARK_SOURCE_L2
    assert r["confidence"] == "l2_thsdk"   # 不再是 L1_approx
    assert r["tick_count"] == 1


def test_dark_flow_l2_returns_none_on_unavailable(monkeypatch):
    """L2 不可达 → 返回 None(由调用方回退腾讯逐笔), 不返回 0 冒充。"""
    import src.core.dark_flow_l2 as dfl2

    monkeypatch.setattr("src.core.dark_l2.fetch_l2_ticks",
                        lambda code, source: (_ for _ in ()).throw(RuntimeError("thsdk 超时")))
    assert dfl2.compute_dark_flow_l2("002361") is None


def test_dark_pool_flow_prefers_fusion_with_detail(monkeypatch):
    """暗盘主线 = 融合(通达信 .tck + 同花顺 thsdk), 且带主动/拆单/被动分口径明细。"""
    from src.core import dark_pool_flow as dpf

    monkeypatch.setattr(dpf, "_ming_flow", lambda c: {"net": 100.0, "confidence": "official"})
    monkeypatch.setattr("src.core.dark_flow_fusion.compute_dark_fusion", lambda s: {
        "total": {"net": 500.0, "buy": 900.0, "sell": 400.0, "count": 100},
        "active": {"net": 400.0, "buy": 500.0, "sell": 100.0, "confidence": "official_exact"},
        "split": {"net": 300.0, "count": 2},
        "passive_est": 100.0, "passive_flag": "ok", "coverage": "fusion", "note": None,
    })
    r = dpf.compute_pool_flow("002361")
    assert r["dark"]["confidence"] == "fusion"
    assert r["dark"]["source"] == "fusion_tdx_tck+thsdk"
    assert r["dark"]["active_net"] == 400.0
    assert r["dark"]["active_confidence"] == "official_exact"
    assert r["dark"]["split_net"] == 300.0
    assert r["dark"]["passive_est"] == 100.0
    assert r["main_net"] == 600.0 and r["coverage"] == "full"


def test_dark_pool_flow_prefers_l2_then_falls_back(monkeypatch):
    """融合不可用时: L2 成功用 L2; L2 也挂 → 回退腾讯逐笔(L1 近似), confidence 降级。"""
    from src.core import dark_pool_flow as dpf

    # 关掉融合主线(否则本机 thsdk 可用会命中融合分支, 测不到 L2/回退)
    monkeypatch.setattr("src.core.dark_flow_fusion.compute_dark_fusion", lambda s: None)
    monkeypatch.setattr(dpf, "_ming_flow", lambda c: {"net": 100.0, "confidence": "official"})
    monkeypatch.setattr("src.core.dark_flow_l2.compute_dark_flow_l2",
                        lambda s, source="thsdk": {"net": 200.0, "inflow": 300.0, "outflow": 100.0,
                                                   "groups": 1, "source": "thsdk_tick_super_level1",
                                                   "confidence": "l2_thsdk"})
    r = dpf.compute_pool_flow("002361")
    assert r["dark"]["confidence"] == "l2_thsdk"
    assert r["main_net"] == 300.0 and r["coverage"] == "full"

    # L2 失败 → 回退腾讯逐笔
    monkeypatch.setattr("src.core.dark_flow_l2.compute_dark_flow_l2", lambda s, source="thsdk": None)
    monkeypatch.setattr(dpf, "_dark_flow_tencent", lambda s: None) if hasattr(dpf, "_dark_flow_tencent") else None
    monkeypatch.setattr("src.core.dark_flow.compute_dark_flow",
                        lambda sym: {"split_order": {"net": 50.0, "buy_amt": 80.0, "sell_amt": 30.0},
                                     "data_status": "ok", "tick_count": 10})
    r2 = dpf.compute_pool_flow("002361")
    assert r2["dark"]["confidence"] == "L1_approx"   # 降级标记
    assert r2["main_net"] == 150.0


# ---------------------------------------------------------------------------
# ③ 三指标联合选股(AND)
# ---------------------------------------------------------------------------

def test_resonance_pick_requires_all_three(monkeypatch):
    """三条件 AND: 只有 G 趋势 + 活跃度上线 + 资金流入 同时满足才入选。"""
    bars = _mk_bars(n=40, final_surge=True)          # 末根 +20% → G信号 + 高活跃度 + 暗盘正
    flat = _mk_bars(n=40, trend=0.0, final_surge=False)  # 无 G、活跃度低

    def fake_fetch(symbol, market="CN", days=60):
        return bars if symbol == "000001" else flat

    monkeypatch.setattr(dp, "fetch_bars", fake_fetch)

    out = ms.resonance_pick(symbols=["000001", "000002"], top_n=10,
                            bars_days=60, activity_line=3.0, fund_source="ohlc")
    picked = {p["symbol"] for p in out["picks"]}
    assert "000001" in picked, out
    assert "000002" not in picked
    p0 = out["picks"][0]
    assert p0["trend"] in ("G信号", "G区间")
    assert p0["activity"] >= 3.0
    assert p0["fund_net"] > 0
    assert p0["phase"] in ("向好", "拐点")
    assert p0["approximation"] is True   # ohlc 口径必须显式标记


def test_resonance_pick_skips_on_unknown_fund_source(monkeypatch):
    """未知资金口径: 单股报错被兜底吞掉(硬规则: 单股失败不拖垮全局), 该股计入 skipped。"""
    monkeypatch.setattr(dp, "fetch_bars", lambda s, market="CN", days=60: _mk_bars(n=40))
    out = ms.resonance_pick(symbols=["000001"], fund_source="不存在的口径")
    assert out["picks"] == []
    assert out["computed"] == 0
    assert out["skipped"] == 1


# ---------------------------------------------------------------------------
# ③b 通达信 .tck + 同花顺 thsdk 融合(互补闭环)
# ---------------------------------------------------------------------------

def test_active_net_from_tck_uses_official_direction():
    """.tck 官方方向 2B/2S → 主动侧净额(精确口径)。"""
    from src.core.dark_flow_fusion import active_net_from_tck

    trades = [
        {"dir": "B", "amt": 500_000.0},
        {"dir": "S", "amt": 200_000.0},
        {"dir": "B", "amt": 100_000.0},
        {"dir": "M", "amt": 999_999.0},   # 中性(集合竞价保护)不计入多空
    ]
    r = active_net_from_tck(trades)
    assert r["net"] == 400_000.0
    assert r["buy"] == 600_000.0 and r["sell"] == 200_000.0
    assert r["confidence"] == "official_exact"
    assert active_net_from_tck([]) is None


def test_split_clusters_needs_300k_and_density():
    """委托级拆单簇: 同方向 + 时间密集 + 价格相近 + 累计≥30万 才成簇。"""
    from src.core.dark_flow_fusion import split_clusters_from_orders

    # 3 笔主买委托, 相邻 1s、价格贴近、合计 45 万 → 成簇
    orders = [
        {"t": 93_000_000, "price": 10.00, "amt": 150_000.0, "a28": 1, "a32": 0},
        {"t": 93_001_000, "price": 10.01, "amt": 150_000.0, "a28": 2, "a32": 0},
        {"t": 93_002_000, "price": 10.01, "amt": 150_000.0, "a28": 3, "a32": 0},
    ]
    r = split_clusters_from_orders(orders)
    assert r["count"] == 1 and r["net"] == 450_000.0

    # 单笔 5 万(未达 30 万) → 不成簇
    small = [{"t": 93_000_000, "price": 10.0, "amt": 50_000.0, "a28": 1, "a32": 0}]
    assert split_clusters_from_orders(small)["count"] == 0

    # 间隔 10 秒(超 3s 窗口) → 断成两簇, 各自不足 30 万
    sparse = [
        {"t": 93_000_000, "price": 10.0, "amt": 200_000.0, "a28": 1, "a32": 0},
        {"t": 93_010_000, "price": 10.0, "amt": 200_000.0, "a28": 2, "a32": 0},
    ]
    assert split_clusters_from_orders(sparse)["count"] == 0

    # 方向由 a28/a32 判定: a32 → 主卖
    sells = [
        {"t": 93_000_000, "price": 10.0, "amt": 200_000.0, "a28": 0, "a32": 1},
        {"t": 93_001_000, "price": 10.0, "amt": 200_000.0, "a28": 0, "a32": 2},
    ]
    assert split_clusters_from_orders(sells)["net"] == -400_000.0


def test_dark_fusion_coverage_and_passive_estimation(monkeypatch):
    """融合: 主动取 .tck 真值, 被动 = 全量 − 主动(估计); 缺失侧 coverage 降级。"""
    import src.core.dark_flow_fusion as dff

    tck = (
        [{"dir": "B", "amt": 500_000.0}, {"dir": "S", "amt": 100_000.0}],  # active net=40万
        [{"t": 93_000_000, "price": 10.0, "amt": 150_000.0, "a28": 1, "a32": 0},
         {"t": 93_001_000, "price": 10.0, "amt": 150_000.0, "a28": 2, "a32": 0}],
        [],
    )
    monkeypatch.setattr(dff, "_fetch_tck", lambda s: tck)
    monkeypatch.setattr(dff, "_thsdk_total_net",
                        lambda s: {"net": 500_000.0, "buy": 900_000.0, "sell": 400_000.0,
                                   "count": 100, "source": "thsdk", "confidence": "l2_thsdk"})

    r = dff.compute_dark_fusion("002361")
    assert r["coverage"] == "fusion"
    assert r["active"]["net"] == 400_000.0
    assert r["passive_est"] == 100_000.0          # 50万 − 40万
    assert r["passive_flag"] == "ok"              # 10万/130万 ≈ 7.7%, 在经验区间内
    assert r["split"]["net"] == 300_000.0

    # 只有 thsdk(.tck 不可得) → 降级, 被动侧拆不出来
    monkeypatch.setattr(dff, "_fetch_tck", lambda s: None)
    r2 = dff.compute_dark_fusion("002361")
    assert r2["coverage"] == "thsdk_only"
    assert r2["passive_est"] is None
    assert "被动侧无法拆出" in (r2["note"] or "")

    # 只有 .tck → 主动精确但被动缺失
    monkeypatch.setattr(dff, "_fetch_tck", lambda s: tck)
    monkeypatch.setattr(dff, "_thsdk_total_net", lambda s: None)
    r3 = dff.compute_dark_fusion("002361")
    assert r3["coverage"] == "tck_only"
    assert "被动侧(maker)未落盘" in (r3["note"] or "")

    # 两源都无 → None(不返回 0)
    monkeypatch.setattr(dff, "_fetch_tck", lambda s: None)
    monkeypatch.setattr(dff, "_thsdk_total_net", lambda s: None)
    assert dff.compute_dark_fusion("002361") is None


def test_dark_fusion_flags_suspicious_passive(monkeypatch):
    """被动侧占比超出经验区间 → 标 suspect(口径差异过大, 不冒充精确值)。"""
    import src.core.dark_flow_fusion as dff

    monkeypatch.setattr(dff, "_fetch_tck",
                        lambda s: ([{"dir": "B", "amt": 1_500_000.0}], [], []))
    monkeypatch.setattr(dff, "_thsdk_total_net",
                        lambda s: {"net": 10_000.0, "buy": 1_000_000.0, "sell": 990_000.0,
                                   "count": 100, "source": "thsdk", "confidence": "l2_thsdk"})
    r = dff.compute_dark_fusion("002361")
    assert r["passive_est"] == -1_490_000.0
    assert r["passive_flag"] == "suspect"
    assert "仅供参考不用于下单" in (r["note"] or "")


def test_ming_history_disabled_by_default(monkeypatch):
    """明盘历史(ZLJC)默认关: 单位未校验前不得产出数据。"""
    monkeypatch.setattr(fnd, "MING_HISTORY_ENABLED", False)
    assert fnd.ming_history_tq("002361", 3) is None

    # 即便开启, days<=1 也不走历史(当日明盘用 big_order_flow 真值)
    monkeypatch.setattr(fnd, "MING_HISTORY_ENABLED", True)
    assert fnd.ming_history_tq("002361", 1) is None


def test_fund_flow_nd_uses_ming_history_when_enabled(monkeypatch):
    """明盘历史可用时, N 日主力净额自动合成(明盘 + 暗盘)。"""
    bars = _mk_bars(n=40)
    monkeypatch.setattr(fnd, "ming_history_tq",
                        lambda s, d: {"net": 1_000.0, "days": d, "source": "tq_zljc",
                                      "confidence": "proxy_approximation"})
    r = fnd.fund_flow_nd("002361", bars=bars, days=3, fetch_today=False)
    assert r["ming_nd"] == 1_000.0
    assert r["net_nd"] == pytest.approx(1_000.0 + r["dark_nd"])
    assert "ZLJC 代理口径" in (r["note"] or "")


# ---------------------------------------------------------------------------
# ④ 回测
# ---------------------------------------------------------------------------

def test_state_without_fund_two_factor():
    """缺资金维时的双指标降级判定(与官方四态不同口径, 结果需带 basis)。"""
    assert dbt._state_without_fund("G信号", 5.0, 3.0) == "向好"
    assert dbt._state_without_fund("G区间", 1.0, 3.0) == "分歧"
    assert dbt._state_without_fund("S信号", 1.0, 3.0) == "走坏"
    assert dbt._state_without_fund("无数据", 9.0, 3.0) is None
    assert dbt._state_without_fund("G信号", None, 3.0) is None


def test_outcome_max_gain_loss():
    bars = [
        {"date": "d0", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1},
        {"date": "d1", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 1},
        {"date": "d2", "open": 11, "high": 13, "low": 8, "close": 9, "volume": 1},
    ]
    oc = dbt._outcome(bars, 0, hold_days=2)
    assert oc["max_gain"] == pytest.approx(30.0)   # (13-10)/10
    assert oc["max_loss"] == pytest.approx(-20.0)  # (8-10)/10


def test_backtest_resonance_runs_and_marks_basis(monkeypatch):
    """回测可跑通, 且结果带 basis + 官方基准对照。"""
    bars = _mk_bars(n=90, trend=0.02, final_surge=True)
    bars = [dict(b, date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}") for i, b in enumerate(bars)]
    monkeypatch.setattr(dp, "fetch_bars", lambda s, market="CN", days=800: bars)

    r = dbt.backtest_resonance(symbols=["000001"], start_date="2026-01-01",
                               end_date="2026-12-31", hold_days=5, fund_source=None)
    assert r["basis"] == "双指标(缺资金维)"
    assert r["sample"]["symbols"] == 1
    assert r["sample"]["signals"] > 0
    assert set(r["by_phase"]) == {"向好", "拐点", "分歧", "走坏"}
    assert r["official"]["向好"]["win_rate"] == 0.7542   # 官方基准随结果带出
    assert "明盘历史无源" in r["note"]
