import marketdata.vendors.sina as sv
from marketdata.symbol import Symbol
from marketdata.types import Quote


def test_sina_us_quote(monkeypatch):
    # US: gb_ 逗号字段,idx 0 name,1 price,2 change%,5 open,6 high,7 low,10 vol,14 pe,26 prev_close
    parts = ["0"] * 30
    parts[0] = "苹果"; parts[1] = "150.5"; parts[2] = "1.2"; parts[5] = "149.0"
    parts[6] = "151.0"; parts[7] = "148.0"; parts[10] = "1000000"; parts[14] = "28.5"; parts[26] = "148.7"
    line = 'var hq_str_gb_aapl="' + ",".join(parts) + '";'
    monkeypatch.setattr(sv, "market_get", lambda *a, **k: line)
    out = sv.SinaQuoteVendor().fetch([Symbol.parse("AAPL", "US")], {})
    assert len(out) == 1 and isinstance(out[0], Quote)
    q = out[0]
    assert q.symbol == "AAPL" and q.market == "US" and q.name == "苹果"
    assert q.current_price == 150.5 and q.change_pct == 1.2 and q.prev_close == 148.7 and q.pe_ratio == 28.5


def test_sina_hk_quote(monkeypatch):
    # HK: rt_hk 逗号字段,idx 1 name,2 open,3 prev_close,4 high,5 low,6 price,7 change,8 change%,11 amount,12 vol
    parts = ["0"] * 15
    parts[1] = "腾讯控股"; parts[2] = "300.0"; parts[3] = "298.0"; parts[4] = "305.0"
    parts[5] = "297.0"; parts[6] = "302.0"; parts[7] = "4.0"; parts[8] = "1.34"; parts[11] = "5e8"; parts[12] = "1000000"
    line = 'var hq_str_rt_hk00700="' + ",".join(parts) + '";'
    monkeypatch.setattr(sv, "market_get", lambda *a, **k: line)
    out = sv.SinaQuoteVendor().fetch([Symbol.parse("00700", "HK")], {})
    q = out[0]
    assert q.symbol == "00700" and q.market == "HK" and q.name == "腾讯控股"
    assert q.current_price == 302.0 and q.prev_close == 298.0 and q.change_pct == 1.34


def test_sina_cn_unsupported():
    # CN 不支持 → supports_markets 拦截(vendor 只 US/HK);此处直接调不传 CN
    assert sv.SinaQuoteVendor().fetch([], {}) == []


def test_sina_index_quotes_cn_hk_us(monkeypatch):
    """指数兜底三族解析(C4 补链): CN 全格式/港指 hkHSI/美股 gb_$;
    输出码对齐腾讯 parts[2] 口径(裸码/点前缀), 与 fetch_raw 下游消费无缝。"""
    cn = ('var hq_str_sh000001="上证指数,3939.0948,3951.5068,3934.4036,3949.2549,3927.3472,'
          '0,0,484675114,779672692270,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-09-10,15:40:32,00,";')
    hk = ('var hq_str_hkHSI="HSI,恒生指数,24985.180,25274.961,25021.810,24889.640,24954.471,'
          '-320.490,-1.268,0.00000,0.00000,198865114,11773961670,0.000,0.000,28056.100,22518.000,2026/09/10,16:09";')
    us = ('var hq_str_gb_$ixic="纳斯达克,26253.3398,-0.64,2026-09-10 09:36:36,-168.0728,26325.0611,'
          '26366.5595,26184.2140,27190.2070,20690.2500,6592678910,5965919466,0,0.00,--,0.00,0.00,'
          '0.00,0.00,0,0,0.0000,0.00,0.00,,Sep 09 05:15PM EDT,26421.4126,0,1,2026";')
    monkeypatch.setattr(sv, "market_get", lambda *a, **k: "\n".join([cn, hk, us]))
    out = sv.fetch_index_quotes(["sh000001", "hkHSI", "usIXIC"])
    by = {r["symbol"]: r for r in out}
    assert set(by) == {"000001", "HSI", ".IXIC"}
    assert by["000001"]["name"] == "上证指数"
    assert by["000001"]["current_price"] == 3934.4036
    assert by["000001"]["prev_close"] == 3951.5068
    assert round(by["000001"]["change_amount"], 2) == -17.10
    assert round(by["000001"]["change_pct"], 2) == -0.43
    assert by["HSI"]["name"] == "恒生指数" and by["HSI"]["current_price"] == 24954.471
    assert by["HSI"]["change_amount"] == -320.49 and by["HSI"]["change_pct"] == -1.268
    assert by[".IXIC"]["current_price"] == 26253.3398
    assert by[".IXIC"]["prev_close"] == 26421.4126
    assert by[".IXIC"]["change_amount"] == -168.0728 and by[".IXIC"]["change_pct"] == -0.64
    # 量/额单位口径与腾讯(手)未对齐 → 一律 None, 不给错值(对齐 W1.1 缺失字段约定)
    assert all(r["volume"] is None and r["turnover"] is None for r in out)


def test_sina_index_quotes_skips_unknown_and_empty(monkeypatch):
    """非指数符号不发请求; 空响应行("")跳过; 无可映射符号 → 空列表且零请求。"""
    calls = {"n": 0}

    def _fake(*a, **k):
        calls["n"] += 1
        return 'var hq_str_sh000001="";'

    monkeypatch.setattr(sv, "market_get", _fake)
    assert sv.fetch_index_quotes(["600519", "jxSOMETHING"]) == []
    assert calls["n"] == 0
    assert sv.fetch_index_quotes(["sh000001"]) == []
    assert calls["n"] == 1
