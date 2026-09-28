"""涨跌停快照备源接线(A-6, 2026-09-28) — 采集器层集成。

验证 `MarketSentimentCollector.get_limit_up_pool` 的新降级链:
  TQ 主源(K线全市场扫描) → TQ 快照备源(_limit_up_pool_zdt_tq) → wudao → 东财。
主源不动: `_limit_up_pool_tq` 有数据时**不**走快照; 快照有数据时**不**碰 wudao/东财。
"""
from __future__ import annotations

from src.collectors.market_sentiment_collector import MarketSentimentCollector


def _stop(method: str):
    def boom(*a, **kw):
        raise RuntimeError(f"{method} down")

    return boom


def test_主源有数据时不走快照(monkeypatch):
    coll = MarketSentimentCollector()
    monkeypatch.setattr(coll, "_limit_up_pool_tq", lambda: [{"code": "001234", "days": 1, "source": "tq"}])
    called = {"zdt": False}

    def fake_zdt():
        called["zdt"] = True
        return [{"code": "999999", "days": 1, "source": "tq_zdt"}]

    monkeypatch.setattr(coll, "_limit_up_pool_zdt_tq", fake_zdt)
    out = coll.get_limit_up_pool("20260928")
    assert [r["code"] for r in out] == ["001234"]
    assert not called["zdt"], "主源成功时绝不应触发备源"


def test_主源挂掉时快照备源接管(monkeypatch):
    coll = MarketSentimentCollector()
    monkeypatch.setattr(coll, "_limit_up_pool_tq", _stop("tq main"))
    zdt_rows = [
        {"code": "001234", "name": "泰慕士", "price": 6.05, "pct": 10.0, "amount": 3e7,
         "ltsz": 0.0, "first_time": "09:31:05", "last_time": "", "days": 2, "sector": "",
         "theme": "", "reason": "", "turnover_rate": 0.0, "order_amount": 120000.0,
         "open_times": 2, "source": "tq_zdt"},
    ]
    calls = {"zdt": 0}

    def fake_zdt():
        calls["zdt"] += 1
        return zdt_rows

    monkeypatch.setattr(coll, "_limit_up_pool_zdt_tq", fake_zdt)
    # wudao/东财路径全断, 证明是快照接管而不是漏到兜底
    monkeypatch.setattr(coll, "_limit_up_pool_wudao", _stop("wudao"))
    import src.collectors.market_sentiment_collector as msc

    monkeypatch.setattr(msc, "market_get", _stop("market_get"))

    out = coll.get_limit_up_pool("20260928")
    assert [r["code"] for r in out] == ["001234"]
    assert out[0]["source"] == "tq_zdt"
    assert out[0]["open_times"] == 2
    assert calls["zdt"] == 1


def test_快照也挂时继续走wudao(monkeypatch):
    coll = MarketSentimentCollector()
    monkeypatch.setattr(coll, "_limit_up_pool_tq", _stop("tq main"))
    monkeypatch.setattr(coll, "_limit_up_pool_zdt_tq", _stop("zdt fallback"))
    wudao_rows = [{"code": "002238", "days": 1, "source": "wudao"}]
    monkeypatch.setattr(coll, "_limit_up_pool_wudao", lambda date: wudao_rows)
    out = coll.get_limit_up_pool("20260928")
    assert [r["code"] for r in out] == ["002238"]
    assert out[0]["source"] == "wudao"


def test_快照返回空列表时继续走wudao(monkeypatch):
    coll = MarketSentimentCollector()
    monkeypatch.setattr(coll, "_limit_up_pool_tq", lambda: [])
    monkeypatch.setattr(coll, "_limit_up_pool_zdt_tq", lambda: [])
    wudao_rows = [{"code": "300750", "days": 3, "source": "wudao"}]
    monkeypatch.setattr(coll, "_limit_up_pool_wudao", lambda date: wudao_rows)
    out = coll.get_limit_up_pool("20260928")
    assert [r["code"] for r in out] == ["300750"]


def test_zdt方法委托到模块函数(monkeypatch):
    """_limit_up_pool_zdt_tq 只是 src.core.limit_pool_zdt.zdt_fallback_pool 的委托。"""
    import src.core.limit_pool_zdt as zdtmod

    marker = [{"code": "600519", "source": "tq_zdt"}]
    monkeypatch.setattr(zdtmod, "zdt_fallback_pool", lambda codes=None: marker, raising=True)
    out = MarketSentimentCollector()._limit_up_pool_zdt_tq()
    assert out == marker
