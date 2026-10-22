"""历史相似分形对标（2026-10-23）：找得到植入形态、样本不足不许算赔率、不许拿未来当历史。"""
from datetime import date, timedelta

from src.core.analog_finder import build, find_analogs, render

BASE = 100.0
PLANT_SHAPE = (0.98, 0.96, 0.955, 0.99)     # 4 根：连跌三天后反抽
PLANT_VOL = 5000.0
BASE_VOL = 1000.0
PLANTS = [152 + i * 22 for i in range(10)]   # 400 根里落在参照窗口 [150, 377] 内


def _dates(n: int) -> list[str]:
    start = date(2025, 1, 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def _bar(day: str, close: float, open_: float, vol: float) -> dict:
    return {
        "date": day,
        "open": open_,
        "high": max(open_, close) * 1.005,
        "low": min(open_, close) * 0.995,
        "close": close,
        "volume": vol,
    }


def _series(n: int, plants: list[int], forwards: dict[int, list[float]] | None = None) -> list[dict]:
    """平盘底 + 在指定下标处植入同一段形态（绝对价位相同 → 特征向量可复现）。

    forwards: {形态最后一根的下标: [T+1..T+5 收盘价]}，用来造不同的后验路径。
    """
    dates = _dates(n)
    bars = [_bar(d, BASE, BASE, BASE_VOL) for d in dates]
    for j in plants:
        prev = BASE
        for off, mult in enumerate(PLANT_SHAPE):
            idx = j - len(PLANT_SHAPE) + 1 + off
            close = BASE * mult
            bars[idx] = _bar(dates[idx], close, prev, PLANT_VOL)
            prev = close
    for j, path in (forwards or {}).items():
        prev = bars[j]["close"]
        for off, close in enumerate(path, start=1):
            bars[j + off] = _bar(dates[j + off], close, prev, BASE_VOL)
            prev = close
    return bars


def test_planted_shape_is_found():
    bars = _series(400, PLANTS + [399])
    res = find_analogs(bars, lookback_days=250)

    assert res["available"] is True
    assert res["samples"] == 10                      # 10 个同形样本（≥ min_samples）
    got = [nb["date"] for nb in res["neighbors"]]
    assert got == [bars[j]["date"] for j in PLANTS]  # 完全同形 → 距离 0，按日期定序
    assert all(nb["distance"] == 0.0 for nb in res["neighbors"])


def test_small_sample_refuses_to_quote_odds():
    """只有 2 个近邻时必须写样本不足，且结果里不许出现任何 up_rate。"""
    bars = _series(200, [130, 160, 199])
    res = find_analogs(bars, lookback_days=250)      # min_samples 默认 10

    assert res["available"] is False
    assert res["samples"] == 2
    assert "样本不足" in res["reason"] and "n=2" in res["reason"] and "至少 10" in res["reason"]
    assert "up_rate" not in str(res)
    assert "t1" not in res and "t5" not in res
    assert "不做推断" in render(res)


def test_forward_window_never_touches_query_bar():
    """邻样本必须离当前 bar 至少 21 根（否则 T+5 会压到今天）。"""
    bars = _series(400, PLANTS + [399])
    idx = {b["date"]: i for i, b in enumerate(bars)}
    res = find_analogs(bars, lookback_days=250)

    assert res["available"] is True
    last = len(bars) - 1
    for nb in res["neighbors"]:
        assert idx[nb["date"]] <= last - 22
        assert idx[nb["date"]] + 5 < last


def test_deterministic_same_input_same_output():
    bars = _series(400, PLANTS + [399])
    assert find_analogs(bars, lookback_days=250) == find_analogs(bars, lookback_days=250)


def test_forward_stats_match_raw_closes_and_failed_flag():
    """T+1 / T+5 的均值、上涨率、最差路径回撤、破位标记必须能用原始收盘价复算。"""
    # A 组：形态后反抽 +2%；B 组：形态后跌穿形态低点 -4%（应打 failed）
    bars = _series(200, [130, 160, 199], forwards={130: [99.4, 99.8, 100.4, 100.8, 101.0],
                                                   160: [98.4, 97.2, 96.0, 95.0, 94.9]})
    closes = [b["close"] for b in bars]
    idx = {b["date"]: i for i, b in enumerate(bars)}
    res = find_analogs(bars, lookback_days=250, k=2, min_samples=2)

    assert res["available"] is True
    n = len(res["neighbors"])
    assert res["samples"] == n >= 2
    f1s, f5s, mdds = [], [], []
    for nb in res["neighbors"]:
        j = idx[nb["date"]]
        f1 = (closes[j + 1] - closes[j]) / closes[j] * 100
        f5 = (closes[j + 5] - closes[j]) / closes[j] * 100
        assert nb["fwd1_pct"] == round(f1, 2)
        assert nb["fwd5_pct"] == round(f5, 2)
        peak, mdd = closes[j], 0.0
        for m in range(1, 6):
            peak = max(peak, closes[j + m])
            mdd = min(mdd, (closes[j + m] - peak) / peak * 100)
        f1s.append(f1)
        f5s.append(f5)
        mdds.append(mdd)

    assert res["t1"]["mean_pct"] == round(sum(f1s) / n, 2)
    assert res["t5"]["mean_pct"] == round(sum(f5s) / n, 2)
    assert res["t1"]["up_rate"] == round(sum(1 for v in f1s if v > 0) / n * 100, 2)
    assert res["t5"]["max_drawdown_pct"] == round(min(mdds), 2)
    assert res["t1"]["n"] == res["t5"]["n"] == n
    assert any(nb["failed"] for nb in res["neighbors"])      # B 组跌穿形态低点
    assert res["t5"]["failed"] == sum(1 for nb in res["neighbors"] if nb["failed"])
    assert "破位" in render(res)


def test_missing_feature_is_named():
    bars = _series(400, PLANTS + [399])
    bars[-1]["volume"] = 0
    res = find_analogs(bars, lookback_days=250)
    assert res["available"] is False
    assert "20日量比" in res["reason"]
    assert "up_rate" not in str(res)


def test_short_history_names_range_feature():
    res = find_analogs(_series(60, []), lookback_days=250)
    assert res["available"] is False
    assert "120日区间位置" in res["reason"]


def test_empty_bars_unavailable():
    res = find_analogs([])
    assert res["available"] is False
    assert res["samples"] == 0
    assert "不做推断" in render(res)


def test_render_footer_and_no_prediction_sentence():
    bars = _series(400, PLANTS + [399])
    text = render(find_analogs(bars, lookback_days=250))
    assert "[数据源: PG 前复权日线 · 相似形态分形统计 · 窗口 250 天 · 样本 10]" in text
    assert "不是预测" in text
    assert len(text.splitlines()) <= 15


def test_build_reports_load_failure(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("pg down")

    import src.db.klines_repo as repo

    monkeypatch.setattr(repo, "load_qfq_bars_cached", boom)
    assert build("600111") == "[相似形态] 取数失败：RuntimeError——不做推断。"
