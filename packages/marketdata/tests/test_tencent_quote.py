import marketdata.vendors.tencent as tv
from marketdata.symbol import Symbol


def _fake_line() -> str:
    parts = ["0"] * 50
    parts[1] = "贵州茅台"
    parts[2] = "600519"
    parts[3] = "1700.0"      # current
    parts[4] = "1680.0"      # prev_close
    parts[5] = "1685.0"      # open
    parts[6] = "12345"       # volume
    parts[30] = "20260807161418"  # 行情源实际报价时间
    parts[31] = "20.0"       # change_amount
    parts[32] = "1.19"       # change_pct
    parts[33] = "1710.0"     # high
    parts[34] = "1670.0"     # low
    parts[35] = "1700/12345/6789.0"   # price/vol/turnover → turnover=6789.0
    parts[38] = "0.5"        # turnover_rate
    parts[39] = "35.0"       # pe
    parts[44] = "2000000"    # circulating mv
    parts[45] = "2100000"    # total mv
    parts[49] = "1.2"        # volume_ratio
    return 'v_sh600519="' + "~".join(parts) + '";'


def test_tencent_parses_quote(monkeypatch):
    monkeypatch.setattr(tv, "market_get", lambda *a, **k: _fake_line().encode("gbk"))
    v = tv.TencentQuoteVendor()
    out = v.fetch([Symbol.parse("600519")], {})
    assert len(out) == 1
    q = out[0]
    assert q.symbol == "600519" and q.name == "贵州茅台" and q.market == "CN"
    assert q.current_price == 1700.0 and q.change_pct == 1.19
    assert q.turnover == 6789.0 and q.volume_ratio == 1.2 and q.pe_ratio == 35.0
    assert q.quote_time is not None
    assert q.quote_time.isoformat() == "2026-08-07T16:14:18+08:00"


def test_tencent_empty_content_returns_empty(monkeypatch):
    monkeypatch.setattr(tv, "market_get", lambda *a, **k: None)
    assert tv.TencentQuoteVendor().fetch([Symbol.parse("600519")], {}) == []


# ---------------------------------------------------------------------------
# parts[30] 的时刻语义是**按市场分**的（2026-10-29 复审）
# ---------------------------------------------------------------------------

def _fake_line_for(code: str, stamp: str) -> str:
    parts = ["0"] * 50
    parts[1] = "测试标的"
    parts[2] = code
    parts[3] = "100.0"
    parts[4] = "99.0"
    parts[5] = "99.5"
    parts[6] = "1000"
    parts[30] = stamp
    parts[31] = "1.0"
    parts[32] = "1.01"
    parts[33] = "101.0"
    parts[34] = "98.0"
    parts[35] = "100.0/1000/100000.0"
    return f'v_{code}="' + "~".join(parts) + '";'


def test_cn_parts30_is_snapshot_refresh_but_hk_us_is_not_proven():
    """CN 可证"刷新时点"；港美的 parts[30] 只到"供应商报时"，不能反过来断言非成交。

    实测（2026-10-29 19:49，qt.gtimg.cn 同一批）：
      - CN sh600519 parts[30]=20261029161457 —— 15:00 收盘后仍刷到 16:14，且停牌股同样
        刷新 ⇒ 铁证是"刷新时点"，不是成交时刻；
      - HK 8 只 = 2026/10/29 16:08:24~16:08:29（收盘竞价随机收市窗口内）；
      - US 15 只 = 2026-10-28 16:00:01（收盘竞价打印），BRK.A=16:04:49（更晚）。
        港美这些时刻**可能就是最后一笔成交**，没有任何一条证据能否定 ⇒ 只能标
        "供应商报时（不证明成交）"，不许沿用 CN 那个更强的断言。
    """
    cn = tv._parse_line(_fake_line_for("sh600519", "20261029161457"), "CN")
    assert cn is not None and cn.quote_time_kind == "snapshot_refresh"
    hk = tv._parse_line(_fake_line_for("hk00700", "2026/10/29 16:08:29"), "HK")
    assert hk is not None and hk.quote_time is not None
    assert hk.quote_time_kind == "vendor_stamp"
    us = tv._parse_line(_fake_line_for("usAAPL", "2026-10-28 16:00:01"), "US")
    assert us is not None and us.quote_time is not None
    assert us.quote_time_kind == "vendor_stamp"
    # 没有报时就没有语义（不许留半条）
    assert tv._parse_line(_fake_line_for("hk00700", ""), "HK").quote_time_kind == ""
