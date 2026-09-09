"""封单成色纯函数单测(批次A, 2026-09-06 28号)。"""
from datetime import datetime, timedelta

from src.core.seal_quality import _pairwise_rates, compute_from_series
from src.core.limit_rules import limit_up_price, limit_ratio, limit_down_price


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _mk_series(minutes=30, cancel_per_min=0, buy_per_min=1000, sell_per_min=1000, base=10000, jitter=False):
    """构造等间隔 60s 采样: 累计字段线性增长; jitter=True 时分钟率有自然波动。"""
    t0 = datetime(2026, 9, 7, 10, 0, 0)
    rows = []
    cb = cs = tb = ts_ = base
    for i in range(minutes + 1):
        jb = ((i * 37) % 7) * 30 if jitter else 0
        js = ((i * 23) % 5) * 20 if jitter else 0
        rows.append({
            "ts": _iso(t0 + timedelta(minutes=i)),
            "cancel_buy": cb,
            "cancel_sell": cs,
            "total_buy_vol": tb,
            "total_sell_vol": ts_,
            "is_sealed": 1,
            "seal_amount": 5_000_000.0,
            "price": 11.0,
            "limit_price": 11.0,
        })
        cb += cancel_per_min
        cs += cancel_per_min
        tb += buy_per_min + jb
        ts_ += sell_per_min + js
    return rows


def test_steady_seal_high_quality():
    """封单稳定、撤单少 → 成色高。"""
    rows = _mk_series(cancel_per_min=10, buy_per_min=1000, sell_per_min=1000)
    r = compute_from_series(rows, now=datetime(2026, 9, 7, 10, 30, 0))
    assert r["available"] is True
    # 撤单率 = 20 / (20 + 2000 + 2000)
    assert r["cancel_rate_5m"] < 0.02
    assert r["seal_quality"] > 0.98
    assert r["is_sealed"] == 1


def test_heavy_cancel_low_quality():
    """撤单暴增 → 成色低、z-score 显著为正。"""
    rows = _mk_series(minutes=40, cancel_per_min=10, jitter=True)
    # 最后 5 分钟撤单暴增
    for i in range(36, 41):
        rows[i]["cancel_buy"] += 3000 * (i - 35)
    r = compute_from_series(rows, now=datetime(2026, 9, 7, 10, 40, 0))
    assert r["available"] is True
    assert r["cancel_rate_5m"] > 0.5
    assert r["seal_quality"] < 0.5
    assert r["cancel_zscore"] is not None and r["cancel_zscore"] > 3


def test_cancel_bias_sign():
    """卖撤增量远大于买撤 → bias 为负(压单虚, 偏多信号方向)。"""
    rows = _mk_series(minutes=15, cancel_per_min=5)
    for i in range(11, 16):
        rows[i]["cancel_sell"] += 2000 * (i - 10)
    r = compute_from_series(rows, now=datetime(2026, 9, 7, 10, 15, 0))
    assert r["available"] is True
    assert r["cancel_bias_5m"] < 0


def test_insufficient_samples_explicit_no_data():
    """样本不足 → available=False + reason(不编造)。"""
    r = compute_from_series(_mk_series(minutes=1))
    assert r["available"] is False
    assert r["reason"]


def test_cumulative_reset_detected():
    """累计字段回退(跨日重置) → 拒绝计算。"""
    rows = _mk_series(minutes=10)
    for k in ("cancel_buy", "cancel_sell", "total_buy_vol", "total_sell_vol"):
        rows[-1][k] = 5  # 突然清零
    r = compute_from_series(rows, now=datetime(2026, 9, 7, 10, 10, 0))
    assert r["available"] is False


def test_seal_success_rate():
    """窗口内部分炸板 → 封板成功率 0~1。"""
    rows = _mk_series(minutes=10)
    for i in range(8, 11):
        rows[i]["is_sealed"] = 0
    r = compute_from_series(rows, now=datetime(2026, 9, 7, 10, 10, 0))
    assert r["available"] is True
    assert r["seal_success_rate"] is not None
    assert 0.0 <= r["seal_success_rate"] < 1.0


def test_pairwise_rates_skips_reset_pairs():
    """重置对不入 z-score 基线。"""
    rows = _mk_series(minutes=15)
    rows[7]["cancel_buy"] = 1  # 单点重置
    rates = _pairwise_rates(rows)
    assert len(rates) >= 10  # 只有含重置点的对被跳过


# ---------------------------------------------------------------------------
# 涨停价规则(批次B 复用)
# ---------------------------------------------------------------------------

def test_limit_price_main_board():
    assert limit_ratio("600519", False) == 0.10
    assert limit_ratio("000001", False) == 0.10
    assert limit_up_price("600519", 10.00, False) == 11.00
    assert limit_up_price("000001", 3.07, False) == 3.38  # 3.377 四舍五入到分
    # B0.6: ST 未知(None) → 保守 5%
    assert limit_ratio("600519") == 0.05
    assert limit_up_price("600519", 10.00) == 10.50


def test_limit_price_gem_star():
    assert limit_ratio("300001") == 0.20
    assert limit_ratio("688001") == 0.20
    assert limit_up_price("688001", 100.00) == 120.00


def test_limit_price_bj():
    assert limit_ratio("920001") == 0.30
    assert limit_ratio("430047") == 0.30
    assert limit_up_price("920001", 10.00) == 13.00


def test_limit_price_st_and_down():
    assert limit_ratio("600519", is_st=True) == 0.05
    assert limit_up_price("600519", 10.00, is_st=True) == 10.50
    assert limit_down_price("600519", 10.00, False) == 9.00


def test_limit_price_edge_cases():
    # 银行家舍入陷阱: round(2.675,2)=2.67, 交易所口径应 2.68
    assert limit_up_price("600519", 2.43, False) == 2.67  # 2.673 → 2.67
    assert limit_up_price("600519", 2.435, False) is not None
    # 新股首日/无昨收 → None(显式无数据)
    assert limit_up_price("600519", None) is None
    assert limit_up_price("600519", 0) is None
    # 非法代码
    assert limit_ratio("12345") is None
    assert limit_ratio("880001") is None  # 板块指数代码段不在四类内
