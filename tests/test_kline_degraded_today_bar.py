# -*- coding: utf-8 -*-
"""今日 K 线「退化占位行」防线(2026-10-15)。

生产实测(2026-10-15, 600111 北方稀土):
  · `klines` 当日行 = `O=H=L=C=34.08 | volume=0 | source=tq`, 前收 34.86, 实时真价 34.85;
  · 写入方 = 盘前 09:20 预缓存 `kline_precache` → `klines_ingestor.ingest_symbol`
    (网关盘前/集合竞价把今日 bar 写成「价=竞价指示价、量 0」的占位行);
  · 读取方 `_finalize_daily_bars` 里旧的 `is_stub_bar` 要求「价 == 前收」⇒ 实测 147 行中
    **144 行漏判**(占位价 ≠ 前收) ⇒ 假平棒以 `today_bar='pg'` 端出去, 并顶掉了 live 合成;
    `/klines/{symbol}/summary` 的 `last_close` 同样端出 34.08。

本文件钉死三层防线(全离线, 禁真网络/真库):
  ① 纯函数: `is_flat_zero_volume` / `is_degenerate_placeholder`(今日 + 零振幅 + 量0,
     **不要求价=前收**);
  ② 读路径: 退化今日行 ⇒ 按缺失剔除 ⇒ live 合成 / 显式 missing; **绝不返回假值**;
     停牌日保留其真实(零成交)bar;
  ③ 写路径: `validate_bar` 判据补上退化占位; `ingest_symbol` **不入库**(0 量冒充真值红线)。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text

from src.collectors import kline_collector as kc
from src.collectors import klines_ingestor as ki
from src.models.market import MarketCode
from src.core import live_daily_bar as LDB
from src.web.api import klines as KL

REF_TODAY = "2026-10-15"  # 生产实测日(fixture 注入, 与运行日期解耦)


def _cst_today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _k(date, o, h, low, c, v):
    return kc.KlineData(date=date, open=o, high=h, low=low, close=c, volume=v)


# ── ① 纯函数 ────────────────────────────────────────────────────────────────
class TestDegeneratePredicate:
    def test_零振幅零量_是退化形态(self):
        b = _k(REF_TODAY, 34.08, 34.08, 34.08, 34.08, 0)
        assert LDB.is_flat_zero_volume(b) is True
        assert LDB.is_degenerate_placeholder(b, REF_TODAY) is True

    def test_量_None_也算退化形态(self):
        b = _k(REF_TODAY, 3.61, 3.61, 3.61, 3.61, None)
        assert LDB.is_flat_zero_volume(b) is True
        assert LDB.is_degenerate_placeholder(b, REF_TODAY) is True

    def test_有量的一字板_不是退化(self):
        # 一字板零振幅但**有成交** ⇒ 真数据, 不许当退化剔除
        b = _k(REF_TODAY, 5.0, 5.0, 5.0, 5.0, 1000)
        assert LDB.is_flat_zero_volume(b) is False
        assert LDB.is_degenerate_placeholder(b, REF_TODAY) is False

    def test_有振幅_不是退化(self):
        b = _k(REF_TODAY, 34.0, 34.9, 33.9, 34.5, 0)
        assert LDB.is_flat_zero_volume(b) is False

    def test_非今日_不是今日退化(self):
        b = _k("2026-10-14", 3.61, 3.61, 3.61, 3.61, 0)
        assert LDB.is_flat_zero_volume(b) is True
        assert LDB.is_degenerate_placeholder(b, REF_TODAY) is False

    def test_日期字面量归一_紧凑8位与ISO互认(self):
        """硬约束: 20261015 与 2026-10-15 必须视为同一天(直接 [:10] 比较会静默漏匹配)。"""
        compact = _k("20261015", 34.08, 34.08, 34.08, 34.08, 0)
        iso = _k("2026-10-15", 34.08, 34.08, 34.08, 34.08, 0)
        assert LDB.is_degenerate_placeholder(compact, "2026-10-15") is True
        assert LDB.is_degenerate_placeholder(iso, "20261015") is True
        # 非同一天(紧凑 vs ISO)仍不误判
        assert LDB.is_degenerate_placeholder(compact, "2026-10-14") is False

    def test_旧口径is_stub_bar_契约不变(self):
        """回归守卫: is_stub_bar 仍然要求「价=前收」, 不改其对外契约(既有断言依赖)。"""
        b = _k(REF_TODAY, 34.08, 34.08, 34.08, 34.08, 0)
        assert LDB.is_stub_bar(b, 34.86) is False, "价≠前收 ⇒ 旧桩判定不认(这正是漏判口子)"
        assert LDB.is_stub_bar(b, 34.08) is True


# ── ② 读路径 (_finalize_daily_bars) ─────────────────────────────────────────
def _fin(bars, symbol="600111", market=None, interval="1d"):
    return KL._finalize_daily_bars(bars, symbol, market or MarketCode.CN, interval, REF_TODAY)


def _prev(close=34.86):
    return _k("2026-10-14", 35.71, 35.71, 34.77, close, 38656732)


def _degen(price=34.08):
    """网关盘前/集合竞价占位行: OHLC 全等 + 量 0, 价 = 竞价指示价(≠前收)。"""
    return _k(REF_TODAY, price, price, price, price, 0)


def _real_live():
    return _k(REF_TODAY, 34.5, 34.9, 33.6, 34.85, 3350000)


class TestFinalizeReadDefense:
    def test_退化今日行_价不等于前收_剔除并由live合成(self, monkeypatch):
        """核心回归: 生产 144/147 行的形态 —— 旧口径漏判, 假平棒当 'pg' 端出。"""
        monkeypatch.setattr(KL, "_is_halted_today", lambda s, m, t: False)
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: _real_live())
        bars, state = _fin([_prev(), _degen()], interval="1d")
        assert state == "live", "退化占位行必须触发 live 合成, 不得标 'pg'"
        assert bars[-1].close == 34.85 and bars[-1].volume == 3350000
        assert all(b.close != 34.08 for b in bars), "占位价 34.08 与量 0 绝不许端出去"

    def test_退化今日行_合成不可得_显式missing_不端假值(self, monkeypatch):
        monkeypatch.setattr(KL, "_is_halted_today", lambda s, m, t: False)
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: None)
        bars, state = _fin([_prev(), _degen()], interval="1d")
        assert state == "missing"
        assert [b.date for b in bars] == ["2026-10-14"]
        assert all(b.close != 34.08 and b.volume != 0 for b in bars)

    def test_退化今日行_量None_同样剔除(self, monkeypatch):
        monkeypatch.setattr(KL, "_is_halted_today", lambda s, m, t: False)
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: _real_live())
        bars, state = _fin([_prev(), _k(REF_TODAY, 34.08, 34.08, 34.08, 34.08, None)])
        assert state == "live" and bars[-1].close == 34.85

    def test_停牌日_零振幅零量_保留真实bar(self, monkeypatch):
        """停牌日零成交是真实状态 ⇒ 保留(不判退化, 也不合成)。"""
        monkeypatch.setattr(KL, "_is_halted_today", lambda s, m, t: True)
        monkeypatch.setattr(KL, "_live_today_bar",
                            lambda s, m: pytest.fail("停牌日不该走 live 合成"))
        bars, state = _fin([_prev(3.61), _degen(3.55)], interval="1d")
        assert state == "pg" and len(bars) == 2
        assert bars[-1].close == 3.55

    def test_今日真bar_有量_仍走pg_不动(self, monkeypatch):
        """修复不得误伤: 今日真 bar(有量/有振幅)照常 'pg', 且不触网合成。"""
        monkeypatch.setattr(KL, "_is_halted_today",
                            lambda s, m, t: pytest.fail("真 bar 不该查停牌表"))
        monkeypatch.setattr(KL, "_live_today_bar",
                            lambda s, m: pytest.fail("已定型不该合成"))
        bars, state = _fin([_prev(), _real_live()], interval="1d")
        assert state == "pg" and len(bars) == 2 and bars[-1].close == 34.85

    def test_历史退化行_不剔除_只针对今日末根(self, monkeypatch):
        """历史零振幅零量(如停牌遗留)不属"今日占位", 一律不动。"""
        monkeypatch.setattr(KL, "_is_halted_today", lambda s, m, t: False)
        monkeypatch.setattr(KL, "_live_today_bar", lambda s, m: None)
        old = _k("2026-10-13", 3.61, 3.61, 3.61, 3.61, 0)
        bars, state = _fin([_prev(3.55), old], interval="1d")
        # 末根 10-13 ≠ today ⇒ 不属退化剔除; 也不合成(非交易日由 is_trading_day 决定)
        assert old in bars


# ── ②b 采集器读路径 (summary/预测 同一出口) ─────────────────────────────────
class TestCollectorReadDefense:
    def test_剔除今日退化占位行(self, monkeypatch):
        monkeypatch.setattr("src.core.halts.is_halted", lambda *a, **k: False)
        today = _cst_today()
        yday = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
        real = _k(yday, 35.71, 35.71, 34.77, 34.86, 38656732)
        degen = _k(today, 34.08, 34.08, 34.08, 34.08, 0)
        out = kc._drop_degenerate_today_bar([real, degen], "600111", MarketCode.CN)
        assert out == [real], "今日退化占位行必须剔除"

    def test_今日真bar_不动(self, monkeypatch):
        monkeypatch.setattr("src.core.halts.is_halted", lambda *a, **k: False)
        today = _cst_today()
        real = _k(today, 34.5, 34.9, 33.6, 34.85, 3350000)
        out = kc._drop_degenerate_today_bar([real], "600111", MarketCode.CN)
        assert out == [real]

    def test_停牌日_保留(self, monkeypatch):
        monkeypatch.setattr("src.core.halts.is_halted", lambda *a, **k: True)
        today = _cst_today()
        degen = _k(today, 3.55, 3.55, 3.55, 3.55, 0)
        out = kc._drop_degenerate_today_bar([degen], "600111", MarketCode.CN)
        assert out == [degen]

    def test_get_klines_端出的序列不带今日占位值(self, monkeypatch):
        """summary.get_klines 同一出口 —— 生产实测 summary.last_close=34.08。"""
        monkeypatch.setattr("src.core.halts.is_halted", lambda *a, **k: False)
        today = datetime.fromisoformat(_cst_today()).date()
        hist = [_k((today - timedelta(days=i)).isoformat(), 1.0 + i * 0, 1.0, 1.0, 1.0, 1.0)
                for i in range(40, 0, -1)]
        stub = _k(today.isoformat(), 34.08, 34.08, 34.08, 34.08, 0)
        monkeypatch.setattr(kc.KlineCollector, "_pg_read",
                            lambda self, symbol, days, adjust="qfq": [*hist, stub])
        monkeypatch.setattr(kc, "get_market_data",
                            lambda: pytest.fail("PG 命中不该联网"))
        c = kc.KlineCollector(MarketCode.CN)
        out = c.get_klines("600111", days=60)
        assert out and out[-1].date != _cst_today(), "今日占位行不得出现在末根"
        assert all(b.close != 34.08 and b.volume != 0 for b in out)


# ── ③ 写路径 (validate_bar 判据 + ingest_symbol 不入库) ──────────────────────
_SQL_CREATE_KLINES = (
    "CREATE TABLE klines (ts TIMESTAMP, symbol VARCHAR(16), market VARCHAR(4), "
    "period VARCHAR(4), source VARCHAR(16), adjust VARCHAR(4), open FLOAT, high FLOAT, "
    "low FLOAT, close FLOAT, volume BIGINT, amount FLOAT, quality_flag INT)"
)
_UQ = ("CREATE UNIQUE INDEX uq_klines_sym_ts ON klines"
       "(symbol, market, period, ts, source, adjust)")


def _mk_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'k.db'}")
    with engine.begin() as conn:
        conn.execute(text(_SQL_CREATE_KLINES))
        conn.execute(text(_UQ))
    return engine


def _sel(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT date(ts), close, volume FROM klines ORDER BY ts")
        ).fetchall()


class TestIngestorWriteDefense:
    def test_validate_bar_判据(self):
        """quality 判据补上「volume=0 非停牌日=退化」。"""
        deg = _k(REF_TODAY, 34.08, 34.08, 34.08, 34.08, 0)
        assert ki.validate_bar("600111", deg, 34.86, today=REF_TODAY) == "degraded_placeholder"
        # 停牌 → 不判退化
        assert ki.validate_bar("600111", deg, 34.86, today=REF_TODAY, is_halted=True) is None
        # 有量 → 不判退化
        ok = _k(REF_TODAY, 34.0, 34.9, 33.9, 34.5, 100)
        assert ki.validate_bar("600111", ok, 34.86, today=REF_TODAY) is None
        # 历史同形态(日期≠today) → 不判退化
        old = _k("2026-10-14", 3.61, 3.61, 3.61, 3.61, 0)
        assert ki.validate_bar("600111", old, 3.60, today=REF_TODAY) is None
        # 不传 today → 完全向后兼容(旧行为不变)
        assert ki.validate_bar("600111", deg, 34.86) is None
        # 日期字面量归一: 紧凑 20261015 也认(不与 today 的 ISO 形态错位)
        assert ki.validate_bar("600111", _k("20261015", 34.08, 34.08, 34.08, 34.08, 0),
                               34.86, today=REF_TODAY) == "degraded_placeholder"

    @pytest.mark.asyncio
    async def test_今日退化占位行不入库(self, tmp_path, monkeypatch):
        engine = _mk_db(tmp_path)
        today = _cst_today()
        yday = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
        bars = [
            _k(yday, 35.71, 35.71, 34.77, 34.86, 38656732),
            _k(today, 34.08, 34.08, 34.08, 34.08, 0),  # 盘前占位
        ]
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda s, m, d: (list(bars), "tq"))
        monkeypatch.setattr(ki, "_is_halted_today", lambda s, m, t: False)
        res = await ki.ingest_symbol(engine, "600111", MarketCode.CN, "1d", 5)
        assert res["skipped_degraded"] == 1, "今日退化占位行必须被拒绝入库"
        assert res["ingested"] == 1 and res["by_source"] == {"tq": 1}
        rows = _sel(engine)
        assert len(rows) == 1
        assert rows[0][1] == 34.86, "库里不得出现占位价 34.08"
        assert rows[0][2] != 0, "库里不得出现 volume=0 的今日行"

    @pytest.mark.asyncio
    async def test_停牌日今日行_仍入库(self, tmp_path, monkeypatch):
        """停牌日的零成交 bar 是真实状态 —— 不属退化, 照常入库。"""
        engine = _mk_db(tmp_path)
        today = _cst_today()
        bars = [_k(today, 3.55, 3.55, 3.55, 3.55, 0)]
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda s, m, d: (list(bars), "tq"))
        monkeypatch.setattr(ki, "_is_halted_today", lambda s, m, t: True)
        res = await ki.ingest_symbol(engine, "600111", MarketCode.CN, "1d", 5)
        assert res["skipped_degraded"] == 0 and res["ingested"] == 1

    @pytest.mark.asyncio
    async def test_今日真bar_照常入库(self, tmp_path, monkeypatch):
        engine = _mk_db(tmp_path)
        today = _cst_today()
        bars = [_k(today, 34.5, 34.9, 33.6, 34.85, 3350000)]
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda s, m, d: (list(bars), "tq"))
        monkeypatch.setattr(ki, "_is_halted_today", lambda s, m, t: False)
        res = await ki.ingest_symbol(engine, "600111", MarketCode.CN, "1d", 5)
        assert res["ingested"] == 1 and res["skipped_degraded"] == 0
        assert _sel(engine)[0][1] == 34.85

    @pytest.mark.asyncio
    async def test_历史退化行_不受今日判据影响(self, tmp_path, monkeypatch):
        """旧日期上的零振幅零量行(停牌遗留)不在本次防线范围 —— 不许误删。"""
        engine = _mk_db(tmp_path)
        old = "2026-10-14"
        bars = [_k(old, 3.93, 3.93, 3.93, 3.93, 0)]
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda s, m, d: (list(bars), "tq"))
        monkeypatch.setattr(ki, "_is_halted_today", lambda s, m, t: False)
        res = await ki.ingest_symbol(engine, "600293", MarketCode.CN, "1d", 5)
        assert res["skipped_degraded"] == 0 and res["ingested"] == 1
