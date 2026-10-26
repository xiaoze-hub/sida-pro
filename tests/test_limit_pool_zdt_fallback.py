"""涨跌停快照 TQ 备源(审计 A-6, 2026-09-28)。

口径钉子(与 test_limit_up_pool_source.py 同族):
  · 单次 TQ 请求 ≤50 只(事故铁律) —— 快照与 K线都分片;
  · 连板数仍走 K 线自算(build_pool_items), 快照只回填 首封/炸板/封单 真值;
  · 算不出的字段不编造 —— K线取不到的涨停股 days=0(未知, 不是猜 1),
    封单量缺失 order_amount=0;
  · source="tq_zdt" 与主源 "tq" / "eastmoney" 区分, 消费方可标注来源。
全部离线 monkeypatch, 不碰真实 TQ 网关:
  · get_zdt_data → 断点 tqmod._rpc(zdt_snapshot 的模块内调用点)
  · get_stock_list / get_market_data → 断点 tqmod.tq_rpc
"""
from __future__ import annotations

import marketdata.vendors.tq as tqmod
from src.core.limit_pool_zdt import (
    fmt_zdt_time,
    parse_zdt_snapshot,
    zdt_fallback_pool,
)


def _fake_rpc(codes: list | None = None, kline: dict | None = None):
    """造一个假的 tqmod.tq_rpc: get_stock_list / get_market_data。"""

    def fake_rpc(m, p, timeout=None, **kw):
        if m == "get_stock_list":
            return codes or [{"Code": "001234.SZ"}, {"Code": "600519.SH"}]
        if m == "get_market_data":
            assert len(p.get("stock_list") or []) <= 50, "事故铁律: 单次 ≤50 只"
            return kline or {}
        raise AssertionError(f"未预料的 tq_rpc 方法: {m}")

    return fake_rpc


def _fake_zdt(zdt: dict, by_part: bool = True):
    """造假的 tqmod._rpc: 只处理 get_zdt_data(快照断点)。"""

    def fake(m, p, timeout=None, **kw):
        if m == "get_zdt_data":
            assert len(p.get("stock_list") or []) <= 50, "事故铁律: 单次 ≤50 只"
            if by_part:
                return {c: zdt[c] for c in p["stock_list"] if c in zdt}
            return zdt
        raise AssertionError(f"未预料的 _rpc 方法: {m}")

    return fake


# 快照样本: 001234 涨停(2 次炸板) / 600519 涨停(未开板) / 002238 普通股(无 ZT 字段)
_SNAP = {
    "001234.SZ": {
        "FDVolMaxZT": "1200", "VolZT": "900", "FirstTimeZT": "09:31:05",
        "OpenTimesZT": "2", "ZDTStatusNow": "1",
    },
    "600519.SH": {
        "FDVolMaxZT": "88", "VolZT": "50", "FirstTimeZT": 145503,
        "OpenTimesZT": "0", "ZDTStatusNow": "1",
    },
    "002238.SZ": {"TimeNow": "15:00:00"},  # 无 ZT 字段 ⇒ 非涨停
}


class TestParseZdtSnapshot:
    def test_只保留真涨停(self):
        live = parse_zdt_snapshot(_SNAP)
        assert set(live) == {"001234.SZ", "600519.SH"}

    def test_未涨停的剔除(self):
        assert "002238.SZ" not in parse_zdt_snapshot(_SNAP)

    def test_脏行不崩(self):
        snap = {"a.SZ": "not-a-dict", "b.SH": {"OpenTimesZT": "0"}}
        live = parse_zdt_snapshot(snap)
        assert set(live) == {"b.SH"}

    def test_空输入(self):
        assert parse_zdt_snapshot(None) == {}
        assert parse_zdt_snapshot({}) == {}


class TestFmtZdtTime:
    def test_数字串补齐(self):
        assert fmt_zdt_time("92501") == "09:25:01"
        assert fmt_zdt_time(145503) == "14:55:03"

    def test_已带冒号原样(self):
        assert fmt_zdt_time("09:31:05") == "09:31:05"

    def test_取不到留空(self):
        assert fmt_zdt_time(None) == ""
        assert fmt_zdt_time("") == ""
        assert fmt_zdt_time("abc") == "abc"  # 非预期格式透传, 不猜


class TestZdtFallbackPool:
    def test_happy_path_首封炸板封单真值(self, monkeypatch):
        per = {
            "001234.SZ": {"Close": ["5.0", "5.5", "6.05"], "Amount": ["1000", "2000", "3000"]},
            "600519.SH": {"Close": ["1500", "1505", "1510"], "Amount": ["1", "2", "3"]},
        }
        monkeypatch.setattr(tqmod, "_rpc", _fake_zdt(_SNAP))
        monkeypatch.setattr(tqmod, "tq_rpc", _fake_rpc(None, per))

        pool = zdt_fallback_pool(["001234.SZ", "600519.SH", "002238.SZ"])
        by_code = {r["code"]: r for r in pool}
        # 普通股 002238 被剔除
        assert set(by_code) == {"001234", "600519"}
        r = by_code["001234"]
        assert r["source"] == "tq_zdt"
        assert r["first_time"] == "09:31:05"      # 快照真值(主源 K线路径恒为 "")
        assert r["open_times"] == 2                # TQ 独有: 炸板次数
        assert r["order_amount"] == 1200 * 100.0   # 封单量 手→股
        assert r["days"] == 2                      # K 线自算: 5.0→5.5→6.05 两段
        assert r["amount"] == 3000 * 1e4           # 万元→元(口径钉子)
        r2 = by_code["600519"]
        assert r2["first_time"] == "14:55:03"      # 数字串归一
        assert r2["open_times"] == 0
        assert r2["order_amount"] == 88 * 100.0

    def test_连板数仍走K线不拿快照猜(self, monkeypatch):
        """OpenTimesZT=2 是炸板次数不是连板数 —— days 必须来自 K 线。"""
        monkeypatch.setattr(tqmod, "_rpc", _fake_zdt(_SNAP))
        # 600519 三根都是 +0.33% (未涨停) → K线连板数 0, 不拿快照猜
        monkeypatch.setattr(
            tqmod, "tq_rpc", _fake_rpc(None, {"600519.SH": {"Close": ["1500", "1505", "1510"]}}))
        pool = zdt_fallback_pool(["001234.SZ", "600519.SH"])
        by_code = {r["code"]: r for r in pool}
        # 001234 的 K线没给 Close → days=0(未知, 不猜 1); 600519 快照说涨停但
        # K线判未涨停 → days=0(K线口径钉子: 连板数只看收盘价)
        assert by_code["001234"]["days"] == 0
        assert by_code["600519"]["days"] == 0
        assert by_code["600519"]["first_time"] == "14:55:03"  # 快照字段独立于 K线

    def test_K线取不到时days为0不猜1(self, monkeypatch):
        monkeypatch.setattr(tqmod, "_rpc", _fake_zdt(_SNAP))

        def _kline_boom(m, p, timeout=None, **kw):
            raise RuntimeError("K线批次全挂")

        monkeypatch.setattr(tqmod, "tq_rpc", _kline_boom)
        pool = zdt_fallback_pool(["001234.SZ", "600519.SH"])
        assert {r["code"] for r in pool} == {"001234", "600519"}
        for r in pool:
            assert r["days"] == 0  # 未知, 不猜
            assert r["source"] == "tq_zdt"
            assert r["first_time"]  # 快照真值仍在

    def test_快照全空返回空(self, monkeypatch):
        monkeypatch.setattr(tqmod, "_rpc", _fake_zdt({}, by_part=False))
        called = {"kline": False}

        def _kline(m, p, timeout=None, **kw):
            called["kline"] = True
            return {}

        monkeypatch.setattr(tqmod, "tq_rpc", _kline)
        assert zdt_fallback_pool(["001234.SZ"]) == []
        assert not called["kline"], "快照空时不应再取 K线"

    def test_连续两批快照失败即中止(self, monkeypatch):
        """绝不压测式重试 —— 与主源同一铁律。"""
        calls = {"n": 0}

        def _list(m, p, timeout=None, **kw):
            if m == "get_stock_list":
                return [{"Code": f"{600000 + i}.SH"} for i in range(200)]
            raise AssertionError("快照挂后不应再走 tq_rpc")

        def _zdt_boom(m, p, timeout=None, **kw):
            calls["n"] += 1
            raise RuntimeError("TQ down")

        monkeypatch.setattr(tqmod, "tq_rpc", _list)
        monkeypatch.setattr(tqmod, "_rpc", _zdt_boom)
        assert zdt_fallback_pool(None) == []
        assert calls["n"] == 2  # 只试两批快照就放弃

    def test_全A缺省时先取列表再分片(self, monkeypatch):
        seen: dict[str, list[int]] = {"snap": [], "kline": []}

        def _list(m, p, timeout=None, **kw):
            if m == "get_stock_list":
                return [{"Code": f"{600000 + i:06d}.SH"} for i in range(120)]
            if m == "get_market_data":
                seen["kline"].append(len(p["stock_list"]))
                return {c: {"Close": ["10.0", "11.0"]} for c in p["stock_list"]}
            raise AssertionError(f"未预料的 tq_rpc 方法: {m}")

        def _snap_zdt(m, p, timeout=None, **kw):
            if m == "get_zdt_data":
                seen["snap"].append(len(p["stock_list"]))
                return {c: {"OpenTimesZT": "0"} for c in p["stock_list"]}
            raise AssertionError(f"未预料的 _rpc 方法: {m}")

        monkeypatch.setattr(tqmod, "tq_rpc", _list)
        monkeypatch.setattr(tqmod, "_rpc", _snap_zdt)
        pool = zdt_fallback_pool(None)
        assert seen["snap"] == [50, 50, 20]    # 快照分片
        assert seen["kline"] == [50, 50, 20]    # K线同样分片
        assert len(pool) == 120
        assert all(r["days"] == 1 for r in pool)  # 10→11 +10%

    def test_封单量缺失时order_amount为0不猜(self, monkeypatch):
        snap = {"300750.SZ": {"OpenTimesZT": "1", "FirstTimeZT": "09:25:00"}}  # 无 FDVolMaxZT
        monkeypatch.setattr(tqmod, "_rpc", _fake_zdt(snap))
        monkeypatch.setattr(tqmod, "tq_rpc", _fake_rpc(None, {"300750.SZ": {"Close": ["10.0", "11.0"]}}))
        pool = zdt_fallback_pool(["300750.SZ"])
        assert pool[0]["order_amount"] == 0.0
        assert pool[0]["open_times"] == 1
        assert pool[0]["first_time"] == "09:25:00"
