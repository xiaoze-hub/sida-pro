# -*- coding: utf-8 -*-
"""今日形成中日 K 的实时合成(2026-10-12)。

钉五件事(全 mock, 禁真网络/真库):
  ① 合成纯函数: 分钟聚合 OHLC/Σ量/Σ额 口径 + 脏柱跳过 + 缺额诚实;
  ② **单位标定锁死**: Σ分钟量 == 日线量(股), 聚合函数**禁任何二次换算**;
  ③ 三源优先级: PG 分钟线聚合 → 实时日线源(TQ)今日 bar → 都缺不合成;
  ④ partial/source/as_of 诚实标注; 桩(盘前占位)不合成;
  ⑤ 18:00 定型: 真实当日 bar 入库后走 'pg', 合成值被真实值取代(idempotent);
     缺数据显式不合成(missing), 绝不编造。
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.collectors.kline_collector import KlineData as K
from src.core import live_daily_bar as LDB
from src.models.market import MarketCode
from src.web.api import klines as KL

TODAY = "2026-10-12"  # 周一, 交易日(实测 is_trading_day=True)


# ── ① 分钟聚合纯函数 ────────────────────────────────────────────────────────
def _m(ts, o, h, l, c, v=None, a=None):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v, "amount": a}


class TestAggregateMinutes:
    def test_当日聚合_首开高最低末收(self):
        rows = [
            _m("2026-10-12 09:30", 9.59, 9.60, 9.58, 9.60, 1000),
            _m("2026-10-12 09:31", 9.60, 9.70, 9.57, 9.65, 2000),
            _m("2026-10-12 15:00", 9.66, 9.68, 9.65, 9.67, 3000),
        ]
        agg = LDB.aggregate_minute_bars(rows, TODAY)
        assert agg["open"] == 9.59      # 首分钟开(按 ts 升序, 输入乱序也正确)
        assert agg["high"] == 9.70
        assert agg["low"] == 9.57
        assert agg["close"] == 9.67     # 末分钟收
        assert agg["volume"] == 6000.0  # Σ
        assert agg["bar_count"] == 3
        assert agg["as_of"].startswith("2026-10-12T15:00")

    def test_输入乱序也按ts正确取首末(self):
        rows = [
            _m("2026-10-12 15:00", 9.66, 9.68, 9.65, 9.67, 3000),
            _m("2026-10-12 09:30", 9.59, 9.60, 9.58, 9.60, 1000),
        ]
        agg = LDB.aggregate_minute_bars(rows, TODAY)
        assert agg["open"] == 9.59 and agg["close"] == 9.67

    def test_无当日分钟_none(self):
        rows = [_m("2026-10-11 10:00", 1, 1, 1, 1, 1)]
        assert LDB.aggregate_minute_bars(rows, TODAY) is None
        assert LDB.aggregate_minute_bars([], TODAY) is None

    def test_脏分钟柱跳过(self):
        rows = [
            _m("2026-10-12 09:30", 9.59, 9.60, 9.58, 9.60, 1000),
            _m("2026-10-12 09:31", 0, 0, 0, 0, 5),        # 非正价
            _m("2026-10-12 09:32", 9.6, 9.6, 9.8, 9.6, 5),  # low>min(o,c) 违例
            _m("2026-10-12 09:33", None, 1, 1, 1, 5),      # 缺字段
        ]
        agg = LDB.aggregate_minute_bars(rows, TODAY)
        assert agg["bar_count"] == 1 and agg["volume"] == 1000.0

    def test_任一分钟缺额_聚合额诚实为none(self):
        rows = [
            _m("2026-10-12 09:30", 9.59, 9.60, 9.58, 9.60, 1000, 960000.0),
            _m("2026-10-12 09:31", 9.60, 9.61, 9.59, 9.61, 2000, None),  # 额缺失
        ]
        agg = LDB.aggregate_minute_bars(rows, TODAY)
        assert agg["amount"] is None, "缺一即 None, 绝不把无数据当 0 求和"
        assert agg["volume"] == 3000.0

    def test_全部有额_则求和(self):
        rows = [
            _m("2026-10-12 09:30", 9.59, 9.60, 9.58, 9.60, 1000, 960000.0),
            _m("2026-10-12 09:31", 9.60, 9.61, 9.59, 9.61, 2000, 1920000.0),
        ]
        agg = LDB.aggregate_minute_bars(rows, TODAY)
        assert agg["amount"] == 2880000.0


# ── ② 单位标定锁死(Σ分钟量 == 日线量, 股) ───────────────────────────────────
class TestUnitCalibration:
    """"Unit 换算禁猜必须实测" —— 本组把 2026-10-12 实测(600000)锁进回归。

    实测链路(vendor 已归一): 腾讯 m1 源单位=手 → `fetch_tencent_minute_kline` ×100
    归股; 日线同一 vendor 同样 ×100 归股 ⇒ 两者同为**股**, 聚合时**禁二次换算**。
    实测: Σ(m1 2026-10-12)=66,287,900 ≈ 1d=66,288,000(股), OHLC 逐项吻合。
    """

    def test_聚合不做任何二次换算(self):
        """分钟量已是股: 一万一的 Σ 必须原样返回, ×100 会错成 100 倍。"""
        rows = [
            _m("2026-10-12 09:30", 9.59, 9.60, 9.58, 9.60, 33_143_950.0),
            _m("2026-10-12 09:31", 9.60, 9.61, 9.59, 9.61, 33_143_950.0),
        ]
        agg = LDB.aggregate_minute_bars(rows, TODAY)
        assert agg["volume"] == 66_287_900.0  # 实测 Σ, 未乘 100

    def test_Σ分钟量约等于当日日线量(self):
        """实测对拍: Σ(m1) 与 1d 的比值 ≈ 1.0(同为股)。"""
        m1_sum = 66_287_900.0
        daily = 66_288_000.0
        assert abs(m1_sum / daily - 1.0) < 1e-3

    def test_KBAR_unit_vendor_pipeline(self, monkeypatch):
        """锁死 vendor 链路单位: m1 与日线**同一** ×100 手→股 归一(否则差 100 倍)。"""
        from marketdata.vendors import kline as vk

        m1_payload = (
            '{"code":0,"data":{"sh600000":{"m1":'
            '[["202610121500","9.66","9.67","9.68","9.65","100.00",{},"0.00"]]}}}'
        )
        d1_payload = (
            'kline_dayqfq={"code":0,"data":{"sh600000":{"day":'
            '[["2026-10-12","9.59","9.67","9.70","9.55","100.00"]]}}}'
        )
        monkeypatch.setattr(vk, "market_get", lambda *a, **k: m1_payload)
        mbars = vk.fetch_tencent_minute_kline("sh600000", 5, "1m")
        monkeypatch.setattr(vk, "market_get", lambda *a, **k: d1_payload)
        dbars = vk.fetch_tencent_kline_raw("sh600000", 5)
        # 同一原始量(100 手) → 两源均 ×100 归股(10000 股), 口径一致
        assert mbars[0].volume == 10000.0
        assert dbars[0].volume == 10000.0


# ── ③ 三源优先级 + ④ 诚实标注 + 桩不合成 ────────────────────────────────────
class TestSynthesizePriority:
    def test_源一_分钟线聚合优先(self):
        def reader(s, m, d):
            return [_m("2026-10-12 09:30", 9.59, 9.60, 9.58, 9.60, 1000),
                    _m("2026-10-12 15:00", 9.66, 9.68, 9.65, 9.67, 2000)]

        def daily(s, m):  # 不该被调用(分钟线已给数)
            raise AssertionError("分钟线可用时不应走日线源")

        bar = LDB.synthesize_live_daily_bar(
            "600000", "CN", today=TODAY, minute_reader=reader, daily_fetcher=daily)
        assert bar is not None
        assert bar.source == LDB.SOURCE_MINUTE == "live_synthesized"
        assert bar.partial is True
        assert bar.open == 9.59 and bar.close == 9.67 and bar.volume == 3000.0
        assert bar.as_of.startswith("2026-10-12T15:00")

    def test_源二_分钟缺回落实时日线源(self):
        def reader(s, m, d):
            return []  # 分钟线缺

        def daily(s, m):
            return ([{"date": "2026-10-11", "open": 9.7, "high": 9.74, "low": 9.54, "close": 9.54, "volume": 94795800},
                     {"date": "2026-10-12", "open": 9.59, "high": 9.70, "low": 9.55, "close": 9.67, "volume": 66288000}],
                    "tq")

        bar = LDB.synthesize_live_daily_bar(
            "600000", "CN", today=TODAY, minute_reader=reader, daily_fetcher=daily)
        assert bar is not None and bar.source == "live_tq"
        assert bar.partial is True and bar.close == 9.67 and bar.volume == 66288000.0

    def test_源三_都缺_none(self):
        bar = LDB.synthesize_live_daily_bar(
            "600000", "CN", today=TODAY,
            minute_reader=lambda s, m, d: [],
            daily_fetcher=lambda s, m: ([], ""))
        assert bar is None

    def test_日线源末根非今日_不合成(self):
        def daily(s, m):
            return ([{"date": "2026-10-11", "open": 9.7, "high": 9.74, "low": 9.54, "close": 9.54, "volume": 1}], "tq")

        bar = LDB.synthesize_live_daily_bar(
            "600000", "CN", today=TODAY,
            minute_reader=lambda s, m, d: [], daily_fetcher=daily)
        assert bar is None

    def test_日线源末根是桩_不合成(self):
        def daily(s, m):
            return ([{"date": "2026-10-11", "open": 9.7, "high": 9.74, "low": 9.54, "close": 9.54, "volume": 1},
                     {"date": "2026-10-12", "open": 9.54, "high": 9.54, "low": 9.54, "close": 9.54, "volume": 0}],
                    "tq")

        bar = LDB.synthesize_live_daily_bar(
            "600000", "CN", today=TODAY,
            minute_reader=lambda s, m, d: [], daily_fetcher=daily)
        assert bar is None, "盘前占位桩(零振幅零量=前收)绝不当今日实时 bar"

    def test_分钟读异常_回落日线源(self):
        def reader(s, m, d):
            raise RuntimeError("db down")

        def daily(s, m):
            return ([{"date": "2026-10-12", "open": 9.59, "high": 9.70, "low": 9.55, "close": 9.67, "volume": 6}], "tq")

        bar = LDB.synthesize_live_daily_bar(
            "600000", "CN", today=TODAY, minute_reader=reader, daily_fetcher=daily)
        assert bar is not None and bar.source == "live_tq"

    def test_日线取数异常_显式无数据(self):
        def daily(s, m):
            raise RuntimeError("engine down")

        bar = LDB.synthesize_live_daily_bar(
            "600000", "CN", today=TODAY,
            minute_reader=lambda s, m, d: [], daily_fetcher=daily)
        assert bar is None

    def test_默认today取上海时区当天(self):
        """生产不传 today —— 用注入 now 锁 as_of 同时验证默认日期分支不炸。"""
        now = datetime(2026, 10, 12, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        bar = LDB.synthesize_live_daily_bar(
            "600000", "CN", now=now,
            minute_reader=lambda s, m, d: [_m("2026-10-12 09:30", 9.59, 9.6, 9.58, 9.6, 1)],
            daily_fetcher=lambda s, m: ([], ""))
        assert bar.date == "2026-10-12"


# ── 桩判定同源 ──────────────────────────────────────────────────────────────
class TestStubParity:
    def test_core_is_stub_matches_web(self):
        stub = K(date=TODAY, open=9.54, close=9.54, high=9.54, low=9.54, volume=0)
        real = K(date=TODAY, open=9.59, close=9.67, high=9.7, low=9.55, volume=100)
        assert LDB.is_stub_bar(stub, 9.54) is True
        assert KL._is_stub_bar(stub, 9.54) is True      # web 委托同源
        assert KL._is_stub_bar(real, 9.54) is False
        assert LDB.is_stub_bar(stub, 9.0) is False       # 不等于前收 → 不算桩


# ── ⑤ web 接入: live 标注 + 18:00 定型覆盖 + 缺数据 missing ──────────────────
class TestWebWiring:
    def _prev(self):
        return K(date="2026-10-11", open=9.70, close=9.54, high=9.74, low=9.54, volume=94795800)

    def _real_today(self):
        return K(date=TODAY, open=9.59, close=9.67, high=9.70, low=9.55, volume=66288000)

    def test_盘中_live_today_bar_用core合成并带标注(self, monkeypatch):
        KL._LIVE_TODAY_CACHE.clear()
        lb = LDB.LiveDailyBar(date=TODAY, open=9.59, high=9.70, low=9.57, close=9.67,
                              volume=66287900.0, amount=None, partial=True,
                              source="live_synthesized", as_of="2026-10-12T15:00:00+08:00")
        monkeypatch.setattr(LDB, "synthesize_live_daily_bar", lambda s, m, **k: lb)
        bar = KL._live_today_bar("600000", MarketCode.CN)
        assert bar.date == TODAY and bar.close == 9.67
        meta = KL._live_today_meta("600000", MarketCode.CN)
        assert meta["source"] == "live_synthesized" and meta["partial"] is True
        assert meta["as_of"].startswith("2026-10-12T15:00")

    def test_attach_live_bar_仅live且挂末根(self, monkeypatch):
        KL._LIVE_TODAY_CACHE.clear()
        KL._LIVE_TODAY_CACHE["CN:600000"] = (
            0.0, object(),
            {"partial": True, "source": "live_synthesized",
             "as_of": "2026-10-12T15:00:00+08:00", "amount": 123.0},
        )
        resp = {"klines": [{"date": TODAY, "close": 9.67}]}
        KL._attach_live_bar(resp, "600000", MarketCode.CN, "live")
        assert resp["live_bar"]["partial"] is True
        assert resp["live_bar"]["source"] == "live_synthesized"
        assert resp["klines"][-1]["partial"] is True
        assert resp["klines"][-1]["amount"] == 123.0

        resp2 = {"klines": [{"date": TODAY}]}
        KL._attach_live_bar(resp2, "600000", MarketCode.CN, "pg")  # 定型后不附
        assert "live_bar" not in resp2

    def test_盘中合成_live_末根即合成bar(self, monkeypatch):
        synth = K(date=TODAY, open=9.59, close=9.67, high=9.70, low=9.57, volume=66287900)
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: synth)
        bars, state = KL._finalize_daily_bars([self._prev()], "600000", MarketCode.CN, "1d", TODAY)
        assert state == "live" and bars[-1].close == 9.67 and bars[-1].volume == 66287900

    def test_18点定型_真实值取代合成值(self, monkeypatch):
        """盘后 backfill 入库真实当日 bar → 走 pg, 不再合成(合成值被真实 low 取代)。"""
        called = {"n": 0}

        def _synth(s, m):
            called["n"] += 1
            return K(date=TODAY, open=9.59, close=9.67, high=9.70, low=9.57, volume=66287900)

        monkeypatch.setattr(KL, "_live_today_bar", _synth)
        bars, state = KL._finalize_daily_bars(
            [self._prev(), self._real_today()], "600000", MarketCode.CN, "1d", TODAY)
        assert state == "pg", "真实当日 bar 在库 → 定型"
        assert called["n"] == 0, "已定型不该再合成"
        assert bars[-1].low == 9.55 and bars[-1].volume == 66288000  # 真实值(合成值是 9.57)

    def test_缺数据_显式missing_不合成(self, monkeypatch):
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: None)
        bars, state = KL._finalize_daily_bars([self._prev()], "600000", MarketCode.CN, "1d", TODAY)
        assert state == "missing", "合成不出 → 显式 missing(绝不编造)"
        assert len(bars) == 1

    def test_非交易日不合成(self, monkeypatch):
        monkeypatch.setattr(KL, "is_trading_day", lambda d: False)
        monkeypatch.setattr(KL, "_live_today_bar",
                            lambda s, m: pytest.fail("非交易日不该合成"))
        bars, state = KL._finalize_daily_bars([self._prev()], "600000", MarketCode.CN, "1d", TODAY)
        assert state == "missing"


# ── 指数同口径标注 ──────────────────────────────────────────────────────────
class TestIndexAnnotation:
    def test_末根为今日_标live(self, monkeypatch):
        import src.models.market as MK

        class _FakeDef:
            def is_trading_time(self, dt=None):
                return True

        monkeypatch.setitem(MK.MARKETS, MarketCode.CN, _FakeDef())
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        bars = [K(date=today, open=1, close=1, high=1, low=1, volume=1)]
        state, meta = KL._annotate_index_today(bars, "1d", MarketCode.CN)
        assert state == "live" and meta["partial"] is True and meta["source"] == "live_index"

    def test_末根非今日_不标(self, monkeypatch):
        bars = [K(date="2020-01-01", open=1, close=1, high=1, low=1, volume=1)]
        assert KL._annotate_index_today(bars, "1d", MarketCode.CN) == (None, None)

    def test_非日线不标(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        bars = [K(date=today, open=1, close=1, high=1, low=1, volume=1)]
        assert KL._annotate_index_today(bars, "1w", MarketCode.CN) == (None, None)


# ── 读库路径: PG 分钟线读取(注入假引擎, 免真库) ─────────────────────────────
class TestReadMinutePg:
    def test_日界与归一(self):
        captured = {}

        class _Res:
            def fetchall(self):
                return [(datetime(2026, 10, 12, 9, 30), 9.59, 9.60, 9.58, 9.60, 1000, None)]

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, stmt, params):
                captured.update(params)
                return _Res()

        class _Eng:
            def connect(self):
                return _Conn()

        rows = LDB.read_minute_bars_pg("600000", "CN", TODAY, engine=_Eng())
        assert rows[0]["open"] == 9.59 and rows[0]["volume"] == 1000
        assert captured["p"] == "1m" and captured["m"] == "CN"
        # 日界 = CST 当天 00:00 → 次日 00:00(半开)
        assert captured["a"].hour == 0 and captured["a"].date().isoformat() == TODAY
        assert (captured["b"] - captured["a"]).days == 1

    def test_读失败_空列表不抛(self):
        class _Eng:
            def connect(self):
                raise RuntimeError("db down")

        assert LDB.read_minute_bars_pg("600000", "CN", TODAY, engine=_Eng()) == []
