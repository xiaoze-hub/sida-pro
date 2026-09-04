import marketdata.vendors.kline as kv
from marketdata.symbol import Symbol
from marketdata.types import Bar


def test_tencent_kline_parses(monkeypatch):
    js = 'kline_dayqfq={"data":{"sh600519":{"day":[["2026-07-01","1","3","4","0.5","100"],["2026-07-02","3","5","6","2","200"]]}}};'
    monkeypatch.setattr(kv, "market_get", lambda *a, **k: js)
    out = kv.TencentKlineVendor().fetch([Symbol.parse("600519")], {"days": 60})
    assert len(out) == 2 and isinstance(out[0], Bar)
    assert out[0].date == "2026-07-01" and out[0].close == 3.0 and out[1].volume == 200.0 * 100


def test_tencent_kline_volume_unit_shares(monkeypatch):
    """2026-09-04: 腾讯 fqkline volume=手 → Bar 归一为股(002361 实测 1719711手=171971136股)。"""
    js = 'kline_dayqfq={"data":{"sz002361":{"qfqday":[["2026-09-02","10.45","11.04","11.32","10.40","1719711"]]}}};'
    monkeypatch.setattr(kv, "market_get", lambda *a, **k: js)
    out = kv.fetch_tencent_kline_raw("sz002361", 5)
    assert len(out) == 1 and out[0].volume == 171971100.0


def test_eastmoney_kline_parses(monkeypatch):
    payload = {"data": {"klines": ["2026-07-01,1,3,4,0.5,100", "2026-07-02,3,5,6,2,200"]}}
    monkeypatch.setattr(kv, "market_get", lambda *a, **k: payload)
    out = kv.EastmoneyKlineVendor().fetch([Symbol.parse("600519")], {"days": 60})
    assert len(out) == 2 and out[1].high == 6.0


def test_eastmoney_kline_cn_volume_unit_shares(monkeypatch):
    """2026-09-04: 东财 A股(secid 0./1.)volume=手 → 股; 港股(116.)不动。"""
    payload = {"data": {"klines": ["2026-09-02,10.45,11.04,11.32,10.40,1719711"]}}
    monkeypatch.setattr(kv, "market_get", lambda *a, **k: payload)
    out = kv.fetch_eastmoney_kline("0.002361", 5)
    assert len(out) == 1 and out[0].volume == 171971100.0
    out_hk = kv.fetch_eastmoney_kline("116.00700", 5)
    assert len(out_hk) == 1 and out_hk[0].volume == 1719711.0
    assert kv._is_cn_secid("0.002361") and kv._is_cn_secid("1.600519")
    assert not kv._is_cn_secid("116.00700")


def test_stooq_kline_parses(monkeypatch):
    csv = "Date,Open,High,Low,Close,Volume\n2026-07-01,1,4,0.5,3,100\n2026-07-02,3,6,2,5,200\n"
    monkeypatch.setattr(kv, "market_get", lambda *a, **k: csv)
    out = kv.StooqKlineVendor().fetch([Symbol.parse("AAPL")], {})
    assert len(out) == 2 and out[0].close == 3.0 and out[1].close == 5.0


def test_tencent_us_kline_resolves_exchange_suffix(monkeypatch):
    """腾讯美股日K:裸符号只回退化数据(2根)→ 自动试 .OQ/.N 后缀并记忆命中,二次直达。"""
    import json as _j

    kv._US_SUFFIX_CACHE.clear()
    calls = []

    def fake(url, **k):
        param = k["params"]["param"]
        calls.append(param)
        tsym = param.split(",")[0]
        if tsym == "usBABA.N":  # 纽交所后缀才是对的
            days = [[f"2026-07-{i:02d}", "1", "2", "3", "0.5", "10"] for i in range(1, 11)]
        elif tsym in ("usBABA.OQ", "usBABA"):  # 错后缀/裸符号 → 退化 2 根
            days = [["2011-06-02", "1", "2", "3", "0.5", "10"],
                    ["2026-07-31", "1", "2", "3", "0.5", "10"]]
        else:
            days = []
        return "k=" + _j.dumps({"data": {tsym: {"day": days}}})

    monkeypatch.setattr(kv, "market_get", fake)
    v = kv.TencentKlineVendor()
    out = v.fetch([Symbol.parse("BABA", market="US")], {"days": 30})
    assert len(out) == 10
    assert calls[0].startswith("usBABA.OQ")  # 先试纳斯达克
    assert kv._US_SUFFIX_CACHE.get("BABA") == ".N"

    calls.clear()
    out2 = v.fetch([Symbol.parse("BABA", market="US")], {"days": 30})
    assert len(out2) == 10 and len(calls) == 1  # 记忆后缀后一次请求直达
    kv._US_SUFFIX_CACHE.clear()
