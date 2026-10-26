"""/klines/{symbol}/patterns 端点契约钉子(2026-10-10, K线形态图层标注)。

钉四件事(全 mock, 不碰真实网络/DB):
1. 契约: 返回 symbol/market/asof/days/lookback/count/patterns, 每条形态字段齐全;
2. 同源: 与 /klines/{symbol} 同口径 —— PG 命中用 PG, 未命中回落 collector;
3. 诚实: 识别不出 → **显式空数组** + count=0(不硬凑、不编造);
4. 夹取: days∈[30,250], lookback∈[1,days](脏参数不许直达识别)。
"""

from __future__ import annotations

from src.collectors.kline_collector import KlineData
from src.web.api import klines as K


def _bar(d, o, h, l, c, v=100.0):
    return KlineData(date=d, open=o, close=c, high=h, low=l, volume=v)


def _hammer_series(n_heads=6):
    """前置温和下跌 + 一根金针探底(严格定义成立)。"""
    bars = [
        _bar("2026-03-01", 10.0, 10.1, 9.8, 9.9),
        _bar("2026-03-02", 9.9, 10.0, 9.7, 9.8),
        _bar("2026-03-03", 9.8, 9.9, 9.6, 9.7),
        _bar("2026-03-04", 9.7, 9.8, 9.6, 9.7),
        _bar("2026-03-05", 9.7, 9.8, 9.5, 9.6),
        _bar("2026-03-06", 9.6, 9.7, 9.55, 9.65),
    ][:n_heads]
    bars.append(_bar("2026-03-07", 9.80, 9.95, 9.0, 9.90, 300))
    return bars


def _flat_series(n=40):
    return [_bar(f"2026-02-{i + 1:02d}", 10, 10.1, 9.9, 10.0) for i in range(n)]


def _patch_pg(monkeypatch, bars, asof="2026-03-07"):
    monkeypatch.setattr(K, "_pg_klines", lambda s, m, d: (bars, asof))
    # 跳过"剔桩 + 补今日实时 bar"(避免联网; 契约不依赖它)
    monkeypatch.setattr(K, "_finalize_daily_bars", lambda kl, s, m, iv, today=None: (list(kl), None))


def test_契约_识别金针探底(monkeypatch):
    _patch_pg(monkeypatch, _hammer_series())
    out = K.get_kline_patterns("600000", market="CN", days=120, lookback=60)

    assert out["symbol"] == "600000" and out["market"] == "CN"
    assert out["source"] == "pg_klines_hypertable"
    assert out["count"] == len(out["patterns"]) >= 1
    names = [p["name"] for p in out["patterns"]]
    assert "金针探底" in names

    hit = next(p for p in out["patterns"] if p["name"] == "金针探底")
    for key in ("name", "direction", "category", "index", "date", "position", "basis", "definition", "span"):
        assert key in hit, f"缺字段 {key}"
    assert hit["direction"] == "看涨" and hit["position"] == "低位"
    assert hit["index"] == len(_hammer_series()) - 1
    assert isinstance(hit["basis"], list) and hit["basis"]


def test_识别不出_显式空数组(monkeypatch):
    _patch_pg(monkeypatch, _flat_series())
    out = K.get_kline_patterns("600000")
    assert out["patterns"] == []
    assert out["count"] == 0


def test_同源_PG_未命中回落collector(monkeypatch):
    bars = _hammer_series()
    monkeypatch.setattr(K, "_pg_klines", lambda s, m, d: (None, None))
    called = {}

    class _C:
        def __init__(self, market):
            called["market"] = market

        def get_klines(self, symbol, days):
            called["symbol"] = symbol
            return bars

    monkeypatch.setattr(K, "KlineCollector", _C)
    monkeypatch.setattr(K, "_finalize_daily_bars", lambda kl, s, m, iv, today=None: (list(kl), None))
    out = K.get_kline_patterns("600519", days=120)
    assert called["symbol"] == "600519"
    assert "金针探底" in [p["name"] for p in out["patterns"]]
    assert "source" not in out  # 非 PG 来源不谎报 pg


def test_days_与_lookback_夹取(monkeypatch):
    seen = {}

    def _fake_pg(s, m, d):
        seen["days"] = d
        return (_hammer_series(), "2026-03-07")

    monkeypatch.setattr(K, "_pg_klines", _fake_pg)
    monkeypatch.setattr(K, "_finalize_daily_bars", lambda kl, s, m, iv, today=None: (list(kl), None))

    out = K.get_kline_patterns("600000", days=5, lookback=99999)
    assert seen["days"] == K.PATTERNS_MIN_DAYS  # 5 抬到 30
    assert out["lookback"] <= out["days"]

    out2 = K.get_kline_patterns("600000", days=99999, lookback=0)
    assert seen["days"] == K.PATTERNS_MAX_DAYS  # 99999 压到 250
    assert out2["lookback"] >= 1


def test_空序列_无异常依旧空数组(monkeypatch):
    _patch_pg(monkeypatch, [])
    out = K.get_kline_patterns("600000")
    assert out["patterns"] == [] and out["count"] == 0


def test_端点已注册在openapi():
    from src.web.app import app

    paths = app.openapi()["paths"]
    assert "/api/klines/{symbol}/patterns" in paths
    assert "get" in {m.lower() for m in paths["/api/klines/{symbol}/patterns"]}
