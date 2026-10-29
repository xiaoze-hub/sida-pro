from types import SimpleNamespace

from marketdata import Quote
import src.core.marketdata_client as mc


def test_quote_to_row_keys():
    q = Quote(symbol="600519", market="CN", current_price=1700.0, name="贵州茅台",
              change_pct=1.2, volume_ratio=1.1, pe_ratio=35.0)
    row = mc._quote_to_row(q)
    assert row["symbol"] == "600519" and row["name"] == "贵州茅台"
    assert row["current_price"] == 1700.0 and row["change_pct"] == 1.2
    assert row["volume_ratio"] == 1.1 and row["pe_ratio"] == 35.0
    # 兼容旧 orchestrator dict 的关键键都在
    for k in ("symbol", "name", "market", "current_price", "change_pct",
              "change_amount", "prev_close", "open_price", "high_price",
              "low_price", "volume", "turnover", "turnover_rate",
              "circulating_market_value", "total_market_value"):
        assert k in row


def test_md_quote_rows_uses_marketdata(monkeypatch):
    class _MD:
        def quotes(self, symbols, *, market):
            return [Quote(symbol=s, market=market, current_price=9.0) for s in symbols]

    monkeypatch.setattr(mc, "get_market_data", lambda: _MD())
    rows = mc.md_quote_rows(["600519", "000001"], "CN")
    assert [r["symbol"] for r in rows] == ["600519", "000001"]
    assert all(r["current_price"] == 9.0 and r["market"] == "CN" for r in rows)


def test_db_config_provider_maps_rows(monkeypatch):
    rows = [
        SimpleNamespace(provider="tencent", priority=1, config={"k": "v"}, supports_batch=True),
        SimpleNamespace(provider="yfinance", priority=2, config=None, supports_batch=False),
    ]
    cp = mc.DbConfigProvider()
    monkeypatch.setattr(cp, "_query_rows", lambda datatype: rows)
    got = cp.sources_for("quote", "CN")
    assert [(s.vendor, s.priority, s.enabled, s.config) for s in got] == [
        ("tencent", 1, True, {"k": "v"}),
        ("yfinance", 2, True, {}),
    ]


def test_quote_to_row_carries_partial_batch_marker():
    """部分批次必须在**数据源字段**上显式标出（2026-10-29 复审）。"""
    q = Quote(symbol="600519", market="CN", current_price=1700.0,
              source="tq", source_partial=True, source_missing=["002827"])
    row = mc._quote_to_row(q)
    assert row["source"] == "tq"
    assert row["source_partial"] is True
    assert row["source_missing"] == ["002827"]

    full = mc._quote_to_row(Quote(symbol="600519", market="CN", current_price=1.0, source="tq"))
    assert full["source_partial"] is False and full["source_missing"] == []


def test_marketdata_quotes_marks_partial_batch_on_every_row(monkeypatch):
    """`MarketData.quotes()` 把 Response.partial/missing 落到每条 Quote 上（不静默）。"""
    from marketdata.client import MarketData
    from marketdata.types import Response

    md = MarketData(config=object())  # type: ignore[arg-type]

    class _Engine:
        def fetch(self, req, **_kw):
            return Response(ok=True, vendor="tq", latency_ms=7,
                            data=[Quote(symbol="600519", market="CN", current_price=9.9)],
                            partial=True, missing=("002827",), requested=2)

    md._quote_engine = _Engine()
    out = md.quotes(["600519", "002827"], market="CN")
    assert [q.symbol for q in out] == ["600519"]
    assert out[0].source == "tq" and out[0].latency_ms == 7
    assert out[0].source_partial is True
    assert out[0].source_missing == ["002827"]
