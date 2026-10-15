"""/klines/{symbol}/wyckoff 端点契约钉子(本土三层滤网 P1, 与 patterns 同范式)。

钉四件事(全 mock, 不碰真实网络/DB):
1. 契约: 返回 symbol/market/asof/days/lookback/count/markers/verdict,
   每条 marker 字段齐全, verdict 含 stage/note 免责;
2. 同源: 与 /klines/{symbol} 同口径 —— PG 命中用 PG, 未命中回落 collector;
3. 诚实: 识别不出 → **显式空数组** + count=0 + stage=unknown(不硬凑、不编造);
4. 夹取: days∈[30,250], lookback∈[1,days](脏参数不许直达识别)。
"""

from __future__ import annotations

from src.collectors.kline_collector import KlineData
from src.web.api import klines as K


def _bar(d, o, h, l, c, v=100.0):
    return KlineData(date=d, open=o, close=c, high=h, low=l, volume=v)


def _spring_series():
    """24 根平淡 + 弹簧试探(跌破收回、回收充足)。"""
    bars = [_bar(f"2026-02-{i + 1:02d}", 10.0, 10.1, 9.9, 10.0) for i in range(24)]
    bars.append(_bar("2026-03-07", 9.9, 10.0, 9.7, 10.0, 120.0))
    return bars


def _flat_series(n=40):
    return [_bar(f"2026-02-{i + 1:02d}", 10, 10.1, 9.9, 10.0) for i in range(n)]


def _patch_pg(monkeypatch, bars, asof="2026-03-07"):
    monkeypatch.setattr(K, "_pg_klines", lambda s, m, d: (bars, asof))
    # 跳过"剔桩 + 补今日实时 bar"(避免联网; 契约不依赖它)
    monkeypatch.setattr(K, "_finalize_daily_bars", lambda kl, s, m, iv, today=None: (list(kl), None))


def test_契约_弹簧试探加verdict(monkeypatch):
    _patch_pg(monkeypatch, _spring_series())
    out = K.get_kline_wyckoff("600000", market="CN", days=120, lookback=60)

    assert out["symbol"] == "600000" and out["market"] == "CN"
    assert out["source"] == "pg_klines_hypertable"
    assert out["count"] == len(out["markers"]) >= 1
    codes = [m["code"] for m in out["markers"]]
    assert "spring_probe" in codes
    names = [m["name"] for m in out["markers"]]
    assert "弹簧试探" in names

    hit = next(m for m in out["markers"] if m["code"] == "spring_probe")
    for key in ("name", "code", "direction", "category", "index", "date",
                "position", "basis", "definition", "span"):
        assert key in hit, f"缺字段 {key}"
    assert hit["direction"] == "neutral" and hit["position"] == "低位"
    assert isinstance(hit["basis"], list) and hit["basis"]

    verdict = out["verdict"]
    assert verdict["stage"] == "C"
    assert "不得作为主力意图" in verdict["note"]
    blob = str(out["markers"]) + verdict.get("stage_label", "")
    for w in ("主力", "建仓", "出货", "吸筹", "派发"):
        assert w not in blob


def test_诚实_识别不出显式空(monkeypatch):
    _patch_pg(monkeypatch, _flat_series(), asof="2026-02-10")
    out = K.get_kline_wyckoff("600000", market="CN", days=120, lookback=60)
    assert out["markers"] == [] and out["count"] == 0
    assert out["verdict"]["stage"] == "unknown"


def test_夹取_脏参数不直达(monkeypatch):
    _patch_pg(monkeypatch, _flat_series())
    out = K.get_kline_wyckoff("600000", market="CN", days=9999, lookback=9999)
    assert out["days"] == 250 and out["lookback"] == 250
    out2 = K.get_kline_wyckoff("600000", market="CN", days=5, lookback=0)
    assert out2["days"] == 30 and out2["lookback"] == 1


def test_回落_collector(monkeypatch):
    """PG 未命中 → 回落 collector(同源口径不断)。"""
    monkeypatch.setattr(K, "_pg_klines", lambda s, m, d: (None, None))
    monkeypatch.setattr(K, "_finalize_daily_bars", lambda kl, s, m, iv, today=None: (list(kl), None))

    class _C:
        def __init__(self, *a, **k):
            pass

        def get_klines(self, symbol, days=60):
            return _spring_series()

    monkeypatch.setattr(K, "KlineCollector", _C)
    out = K.get_kline_wyckoff("600000", market="CN", days=120, lookback=60)
    assert "source" not in out  # collector 回落无 source 键(与 patterns 同约定)
    assert out["count"] >= 1
