"""多周期走势画像（2026-10-21）：不许只给"今天"，也不许样本不足硬算。"""
from src.core.trend_profile import compute, render


def _bars(n, start=10.0, step=0.1, vol=1000):
    return [{"date": f"d{i}", "open": start, "high": start + i * step + 1,
             "low": start + i * step - 1, "close": start + i * step,
             "volume": vol + i * 10} for i in range(n)]


def test_compute_multi_period_returns():
    m = compute(_bars(130))
    assert m["available"] is True
    assert m["rets"][5] is not None and m["rets"][120] is not None
    assert m["position"] is not None and 0 <= m["position"] <= 100
    assert m["mas"][60] is not None


def test_insufficient_bars_is_explicit():
    m = compute(_bars(10))
    assert m["available"] is False
    assert "样本不足" in m["reason"]
    assert "不做推断" in render(m)


def test_render_has_no_blank_sections():
    text = render(compute(_bars(130)))
    for kw in ("涨跌幅", "阶段区间", "均线", "量能", "相对强弱"):
        assert kw in text


def test_relative_strength_needs_index():
    """没有指数数据时 RS 写「未取到」，不能当成 0。"""
    m = compute(_bars(130), index_bars=None)
    assert m["rs20"] is None
    assert "指数数据未取到" in render(m)


def test_rs_computed_when_index_given():
    stock = _bars(130, step=0.2)
    flat = [{"date": f"i{i}", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1}
            for i in range(130)]
    m = compute(stock, flat)
    assert m["rs20"] is not None and m["rs20"] > 0
