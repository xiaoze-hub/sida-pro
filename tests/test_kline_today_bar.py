"""日K 末根桩剔除 + 今日实时 bar 补齐(2026-09-23 hotfix)。

报障: "K线图不显示今日最新的日K"。
根因: 库里当日那根日线是盘前网关占位 —— 603629 的 09-23 行是
`O=H=L=C=115.96(=前收)` 且 `volume=0`。零振幅+零量的 bar 画出来是一条看不见的横线,
所以用户以为今天的K没画; 而且它是"今天"的日期, 会通过厚度/新鲜度检查, 顶掉了回落联网的机会。

钉子: ① 桩(量0+零振幅+等于前收) 必须被识别并剔除;
     ② 真 bar(有量或有振幅) 不许被误剔;
     ③ 停牌式零振幅但**不等于前收**的 bar 不算桩(不能误删真数据);
     ④ 补不到实时数据时 today_state='missing'(宁缺勿造, 不许编数字);
     ⑤ 非日线(周/月)不做这套处理。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.collectors.kline_collector import KlineData as K
from src.models.market import MarketCode
from src.web.api import klines as KL


def _prev(close=115.96):
    return K(date="2026-09-22", open=112.86, close=close, high=118.0, low=111.19, volume=37928648)


def _stub(close=115.96):
    return K(date="2026-09-23", open=close, close=close, high=close, low=close, volume=0)


def _real(close=111.57):
    return K(date="2026-09-23", open=114.0, close=close, high=114.54, low=111.5, volume=12345678)


class TestStubDetection:
    def test_桩_bar_识别(self):
        assert KL._is_stub_bar(_stub(), 115.96) is True

    def test_真bar_不误剔(self):
        assert KL._is_stub_bar(_real(), 115.96) is False

    def test_有量就不算桩(self):
        b = K(date="2026-09-23", open=115.96, close=115.96, high=115.96, low=115.96, volume=100)
        assert KL._is_stub_bar(b, 115.96) is False

    def test_零振幅但不等于前收_不算桩(self):
        """停牌式: 不能因为零振幅就删真 bar。"""
        b = K(date="2026-09-23", open=100.0, close=100.0, high=100.0, low=100.0, volume=0)
        assert KL._is_stub_bar(b, 115.96) is False

    def test_volume_None_且零振幅算桩(self):
        b = K(date="2026-09-23", open=50.0, close=50.0, high=50.0, low=50.0, volume=None)
        assert KL._is_stub_bar(b, 50.0) is True


class TestFinalize:
    def test_剔桩并补今日实时(self, monkeypatch):
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: _real())
        bars, state = KL._finalize_daily_bars([_prev(), _stub()], "603629", MarketCode.CN, "1d")
        assert len(bars) == 2, "桩应被剔除 + 补回真实今日 bar"
        assert bars[-1].close == 111.57 and bars[-1].volume == 12345678
        assert state == "live"

    def test_补不到就不补_标注missing(self, monkeypatch):
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: None)
        bars, state = KL._finalize_daily_bars([_prev(), _stub()], "603629", MarketCode.CN, "1d")
        assert [b.date for b in bars] == ["2026-09-22"], "桩剔除后不许留假 bar"
        assert state == "missing"

    def test_库里今日是真bar_标pg且不动(self, monkeypatch):
        called = {"n": 0}

        def _boom(s, m):
            called["n"] += 1
            return _real()

        monkeypatch.setattr(KL, "_live_today_bar", _boom)
        bars, state = KL._finalize_daily_bars([_prev(), _real()], "603629", MarketCode.CN, "1d")
        assert state == "pg" and len(bars) == 2
        assert called["n"] == 0, "库里已有真今日 bar 时不该联网"

    def test_非交易日不补_标missing(self, monkeypatch):
        monkeypatch.setattr(KL, "is_trading_day", lambda d: False)
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: _real())
        bars, state = KL._finalize_daily_bars([_prev(), _stub()], "603629", MarketCode.CN, "1d")
        assert state == "missing" and len(bars) == 1

    def test_周线不做处理(self, monkeypatch):
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: pytest.fail("周线不该补实时"))
        bars, state = KL._finalize_daily_bars([_prev(), _stub()], "603629", MarketCode.CN, "1w")
        assert state is None and len(bars) == 2

    def test_空序列不炸(self):
        bars, state = KL._finalize_daily_bars([], "603629", MarketCode.CN, "1d")
        assert bars == [] and state is None

    def test_实时bar日期不前进则不补(self, monkeypatch):
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: _prev())  # 返回的是昨天的
        bars, state = KL._finalize_daily_bars([_prev(), _stub()], "603629", MarketCode.CN, "1d")
        assert state == "missing" and len(bars) == 1

    def test_实时bar本身是桩也不补(self, monkeypatch):
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: None)
        bars, state = KL._finalize_daily_bars([_prev(), _stub()], "603629", MarketCode.CN, "1d")
        assert state == "missing"
