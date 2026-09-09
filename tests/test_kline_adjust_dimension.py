"""风险方案1.2/B1: K线复权维度(adjust)落地测试。

0.7 勘查(docs/research/K线复权污染勘查_20260907.md)实证 qfq 与不复权混存、
DO NOTHING 冻结复权基准、单链复写三份假标签。本文件固化四条防线:
  1. 读取按 adjust 分区取数, 不同复权维度不共用缓存/不混读(qfq 绝不落新浪);
  2. PG 优先: 分区厚(min(30,days))且新鲜(最新柱 12 天内)时直接服务, 不发 HTTP;
  3. 写入诚实: ingestor 单链单标签(source=真源 vendor), DO UPDATE 自愈基准;
  4. 迁移 _m137: adjust 列 + 六列唯一索引, 幂等可重跑。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from src.collectors import kline_collector as kc
from src.collectors import klines_ingestor as ki
from src.models.market import MarketCode


@pytest.fixture(autouse=True)
def _clear_caches():
    """每个用例前后清空进程级缓存,避免相互污染。"""
    for name in ("_KLINE_CACHE", "_FAIL_UNTIL", "_FETCH_LOCKS"):
        d = getattr(kc, name, None)
        if isinstance(d, dict):
            d.clear()
    yield
    for name in ("_KLINE_CACHE", "_FAIL_UNTIL", "_FETCH_LOCKS"):
        d = getattr(kc, name, None)
        if isinstance(d, dict):
            d.clear()


def _recent_bars(n: int, end_offset_days: int = 0) -> list[kc.KlineData]:
    """n 根日K, 最新一根距今天 end_offset_days 天(默认今天)。"""
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    out = []
    for i in range(n, 0, -1):
        d = today - timedelta(days=i - 1 + end_offset_days)
        out.append(kc.KlineData(date=d.isoformat(), open=1.0, close=1.0,
                                high=1.0, low=1.0, volume=1.0))
    return out


_SQL_CREATE_KLINES = (
    "CREATE TABLE klines (ts TIMESTAMP, symbol VARCHAR(16), market VARCHAR(4), "
    "period VARCHAR(4), source VARCHAR(16), {adjust}open FLOAT, high FLOAT, low FLOAT, "
    "close FLOAT, volume BIGINT, amount FLOAT, quality_flag INT)"
)
_UQ_NEW = ("CREATE UNIQUE INDEX uq_klines_symbol_period_ts_adjust "
           "ON klines(symbol, market, period, ts, source, adjust)")


# ────────────────────────────────────────────────────────────────────
# 1. 迁移 _m137
# ────────────────────────────────────────────────────────────────────

class TestMigration137:
    def _mk_old_db(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'k.db'}")
        with engine.begin() as conn:
            conn.execute(text(_SQL_CREATE_KLINES.format(adjust="")))
            conn.execute(text(
                "INSERT INTO klines (ts, symbol, market, period, source, open, high, "
                "low, close, volume, quality_flag) VALUES "
                "('2026-09-01 00:00:00+00:00', '600519', 'CN', '1d', 'tencent', "
                "1, 2, 0.5, 1.5, 100, 1)"))
            conn.execute(text(
                "CREATE UNIQUE INDEX uq_klines_symbol_period_ts "
                "ON klines(symbol, market, period, ts, source)"))
        return engine

    def test_adds_adjust_backfills_none_and_swaps_unique_index(self, tmp_path):
        from src.web import migrations as M

        engine = self._mk_old_db(tmp_path)
        with engine.begin() as conn:
            M._m137_klines_adjust_dimension(conn)

        with engine.connect() as conn:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(klines)"))}
            assert "adjust" in cols
            row = conn.execute(text(
                "SELECT adjust FROM klines WHERE symbol='600519'")).scalar()
            assert row == "none", "存量行应回填默认 'none'"
            names = {r[0] for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index'"))}
            assert "uq_klines_symbol_period_ts" not in names, "旧唯一索引应让位"
            assert "uq_klines_symbol_period_ts_adjust" in names

    def test_idempotent_rerun(self, tmp_path):
        from src.web import migrations as M

        engine = self._mk_old_db(tmp_path)
        with engine.begin() as conn:
            M._m137_klines_adjust_dimension(conn)
        with engine.begin() as conn:
            M._m137_klines_adjust_dimension(conn)  # 不抛即幂等

    def test_do_update_after_migration(self, tmp_path):
        """六列唯一索引下, 同键重复插入应走 DO UPDATE(自愈), 不得产生第二行。"""
        from src.web import migrations as M

        engine = self._mk_old_db(tmp_path)
        with engine.begin() as conn:
            M._m137_klines_adjust_dimension(conn)
        ins = text(
            "INSERT INTO klines (ts, symbol, market, period, source, adjust, "
            "open, high, low, close, volume, quality_flag) VALUES "
            "(:ts, '600519', 'CN', '1d', 'tencent', 'qfq', "
            ":open, :high, :low, :close, 100, 1) "
            "ON CONFLICT (symbol, market, period, ts, source, adjust) "
            "DO UPDATE SET close=EXCLUDED.close")
        with engine.begin() as conn:
            conn.execute(ins, {"ts": "2026-09-05 00:00:00+00:00",
                               "open": 1, "high": 2, "low": 0.5, "close": 10.0})
            conn.execute(ins, {"ts": "2026-09-05 00:00:00+00:00",
                               "open": 1, "high": 2, "low": 0.5, "close": 9.0})
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT close, adjust FROM klines "
                "WHERE symbol='600519' AND adjust='qfq' AND source='tencent'"
            )).fetchall()
        assert rows == [(9.0, "qfq")], "同键重写应覆盖旧值且不新增行"
        # 存量迁移回填的 none 行(不同分区)不得被误伤
        with engine.connect() as conn:
            none_rows = conn.execute(text(
                "SELECT close, adjust FROM klines "
                "WHERE symbol='600519' AND adjust='none' AND source='tencent'"
            )).fetchall()
        assert none_rows == [(1.5, "none")]


# ────────────────────────────────────────────────────────────────────
# 2. 读取: 缓存键隔离 + adjust 路由 + PG 优先
# ────────────────────────────────────────────────────────────────────

class TestAdjustRouting:
    def test_cache_key_includes_adjust(self, monkeypatch):
        """不同复权维度不得共用缓存: qfq 与 none 各自取数一次, 各自命中各自缓存。"""
        calls: list[str] = []

        def fake_fetch(self, symbol, days, adjust="qfq"):
            calls.append(adjust)
            return _recent_bars(40)

        monkeypatch.setattr(kc.KlineCollector, "_fetch_all_sources", fake_fetch)
        c = kc.KlineCollector(MarketCode.CN)
        c.get_klines("600519", days=30, adjust="qfq")
        c.get_klines("600519", days=30, adjust="none")
        c.get_klines("600519", days=30, adjust="qfq")
        c.get_klines("600519", days=30, adjust="none")
        assert calls == ["qfq", "none"], "不同复权维度不得共用缓存"

    def test_pg_fresh_partition_served_without_http(self, monkeypatch):
        """PG qfq 分区厚且新鲜 → 直接服务, 不发 HTTP(AGENTS.md PG 优先红线)。"""
        fresh = _recent_bars(130)
        monkeypatch.setattr(kc.KlineCollector, "_pg_read",
                            lambda self, symbol, days, adjust="qfq": fresh)
        monkeypatch.setattr(kc, "get_market_data",
                            lambda: pytest.fail("PG 命中时不得联网"))
        c = kc.KlineCollector(MarketCode.CN)
        out = c.get_klines("600519", days=120)
        assert len(out) == 120
        assert out[-1].date == _recent_bars(1)[0].date

    def test_pg_stale_partition_falls_to_engine(self, monkeypatch):
        """PG 分区最新柱超 12 天(陈旧网关快照口径) → 回落联网 engine。"""
        stale = _recent_bars(130, end_offset_days=60)
        monkeypatch.setattr(kc.KlineCollector, "_pg_read",
                            lambda self, symbol, days, adjust="qfq": stale)

        class _MD:
            calls = 0

            def klines_with_vendor(self, symbol, *, market, days, min_count=1):
                _MD.calls += 1
                return _recent_bars(130), "eastmoney"

        monkeypatch.setattr(kc, "get_market_data", lambda: _MD())
        c = kc.KlineCollector(MarketCode.CN)
        out = c.get_klines("600519", days=120)
        assert _MD.calls == 1
        assert len(out) == 120

    def test_pg_thin_partition_falls_to_engine(self, monkeypatch):
        """PG 命中但厚度不足(< min(30,days)) → 视为无效, 联网拿完整历史。"""
        thin = _recent_bars(10)
        monkeypatch.setattr(kc.KlineCollector, "_pg_read",
                            lambda self, symbol, days, adjust="qfq": thin)

        class _MD:
            calls = 0

            def klines_with_vendor(self, symbol, *, market, days, min_count=1):
                _MD.calls += 1
                return _recent_bars(130), "tencent"

        monkeypatch.setattr(kc, "get_market_data", lambda: _MD())
        c = kc.KlineCollector(MarketCode.CN)
        assert c.get_klines("600519", days=120)
        assert _MD.calls == 1

    def test_qfq_never_falls_to_sina(self, monkeypatch):
        """qfq 请求在 PG 空 + engine 空时必须返回空, 绝不落新浪不复权数据。"""
        monkeypatch.setattr(kc.KlineCollector, "_pg_read",
                            lambda self, symbol, days, adjust="qfq": [])

        def _sina_boom(self, symbol, days):
            raise AssertionError("qfq 请求绝不允许落到新浪不复权数据")

        monkeypatch.setattr(kc.KlineCollector, "_sina_fallback", _sina_boom)

        class _MD:
            def klines_with_vendor(self, symbol, *, market, days, min_count=1):
                return [], ""

        monkeypatch.setattr(kc, "get_market_data", lambda: _MD())
        c = kc.KlineCollector(MarketCode.CN)
        assert c.get_klines("600519", days=60) == []

    def test_none_uses_sina_not_engine(self, monkeypatch):
        """adjust='none' → 新浪原始日K, 不走 qfq engine, 且以 none 分区查 PG。"""
        seen: dict = {}

        def fake_pg(self, symbol, days, adjust="qfq"):
            seen["adjust"] = adjust
            return []

        monkeypatch.setattr(kc.KlineCollector, "_pg_read", fake_pg)
        monkeypatch.setattr(kc, "get_market_data",
                            lambda: pytest.fail("none 请求不应走 qfq engine"))
        sina_bars = _recent_bars(80)
        sina_calls: list[str] = []

        def fake_sina(self, symbol, days):
            sina_calls.append(symbol)
            return sina_bars

        monkeypatch.setattr(kc.KlineCollector, "_sina_fallback", fake_sina)
        c = kc.KlineCollector(MarketCode.CN)
        out = c.get_klines("600519", days=60, adjust="none")
        assert seen["adjust"] == "none"
        assert sina_calls == ["600519"]
        assert len(out) == 60


class TestPgUsable:
    def test_thickness_threshold(self):
        c = kc.KlineCollector(MarketCode.CN)
        assert not c._pg_usable(_recent_bars(29), 60)
        assert c._pg_usable(_recent_bars(30), 60)

    def test_freshness_12d_boundary(self):
        c = kc.KlineCollector(MarketCode.CN)
        assert not c._pg_usable(_recent_bars(130, end_offset_days=13), 60)
        assert c._pg_usable(_recent_bars(130, end_offset_days=12), 60)
        assert c._pg_usable(_recent_bars(130), 60)


# ────────────────────────────────────────────────────────────────────
# 3. 写入: 诚实标签 + DO UPDATE 自愈
# ────────────────────────────────────────────────────────────────────

def _mk_sqlite_db(tmp_path, name="k.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    with engine.begin() as conn:
        conn.execute(text(_SQL_CREATE_KLINES.format(adjust="adjust VARCHAR(4), ")))
        conn.execute(text(_UQ_NEW))
    return engine


def _sel_all(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT source, adjust, close, volume FROM klines")).fetchall()


class TestIngestor:
    @pytest.mark.asyncio
    async def test_single_honest_label_from_vendor(self, tmp_path, monkeypatch):
        """单链单标签: source=真实胜出 vendor, 不再复写三份假标签。"""
        engine = _mk_sqlite_db(tmp_path)
        bars = [kc.KlineData(date="2026-09-05", open=10, high=11, low=9,
                             close=10.5, volume=100)]
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda symbol, market, days: (list(bars), "eastmoney"))
        res = await ki.ingest_symbol(engine, "600519", MarketCode.CN, "1d", 800)
        assert res["ingested"] == 1
        assert res["by_source"] == {"eastmoney": 1}
        assert _sel_all(engine) == [("eastmoney", "qfq", 10.5, 100)]

    @pytest.mark.asyncio
    async def test_unknown_vendor_honest_label(self, tmp_path, monkeypatch):
        """vendor 缺失时诚实标 'unknown', 不编造 tencent。"""
        engine = _mk_sqlite_db(tmp_path)
        bars = [kc.KlineData(date="2026-09-05", open=10, high=11, low=9,
                             close=10.5, volume=100)]
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda symbol, market, days: (list(bars), ""))
        res = await ki.ingest_symbol(engine, "600519", MarketCode.CN, "1d", 800)
        assert res["by_source"] == {"unknown": 1}
        assert _sel_all(engine)[0][0] == "unknown"

    @pytest.mark.asyncio
    async def test_rerun_updates_basis_no_duplicate(self, tmp_path, monkeypatch):
        """重跑(除权后 qfq 基准变化)→ DO UPDATE 覆盖同键, 不产生第二行。"""
        engine = _mk_sqlite_db(tmp_path)
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda symbol, market, days: (
                                [kc.KlineData(date="2026-09-05", open=10, high=11,
                                              low=9, close=10.5, volume=100)],
                                "tencent"))
        await ki.ingest_symbol(engine, "600519", MarketCode.CN, "1d", 800)
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda symbol, market, days: (
                                [kc.KlineData(date="2026-09-05", open=9, high=10,
                                              low=8, close=9.5, volume=120)],
                                "tencent"))
        res = await ki.ingest_symbol(engine, "600519", MarketCode.CN, "1d", 800)
        assert res["ingested"] == 1
        rows = _sel_all(engine)
        assert len(rows) == 1, "DO UPDATE 不得产生第二行"
        assert rows[0][2] == 9.5 and rows[0][3] == 120

    @pytest.mark.asyncio
    async def test_vendor_switch_prunes_stale_source_partition(self, tmp_path, monkeypatch):
        """vendor 切换(腾讯→东财)后, 旧 source 的 qfq 行必须清掉, 不得同日双柱。"""
        engine = _mk_sqlite_db(tmp_path)
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda symbol, market, days: (
                                [kc.KlineData(date="2026-09-05", open=10, high=11,
                                              low=9, close=10.5, volume=100)],
                                "tencent"))
        await ki.ingest_symbol(engine, "600519", MarketCode.CN, "1d", 800)
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda symbol, market, days: (
                                [kc.KlineData(date="2026-09-05", open=9, high=10,
                                              low=8, close=9.5, volume=120)],
                                "eastmoney"))
        await ki.ingest_symbol(engine, "600519", MarketCode.CN, "1d", 800)
        rows = _sel_all(engine)
        assert rows == [("eastmoney", "qfq", 9.5, 120)], "旧 source 分区应被清理, 只留胜出 vendor"

    @pytest.mark.asyncio
    async def test_empty_fetch_reports_fail_details(self, monkeypatch):
        """空结果必须带 fail_details(聚合日志依赖)。"""
        monkeypatch.setattr(ki, "_fetch_klines_with_vendor",
                            lambda symbol, market, days: ([], "tencent"))
        res = await ki.ingest_symbol(MagicMockLikeEngine(), "600519", MarketCode.CN, "1d", 800)
        assert res["ingested"] == 0
        assert res["fail_details"] == [{"source": "tencent", "error": "empty/no klines"}]
        assert res["by_source"] == {}


class MagicMockLikeEngine:
    """空结果路径不会触达 DB; 若触达则显式失败。"""

    def begin(self):  # pragma: no cover - 防御
        raise AssertionError("空结果不应写库")


class TestPersistBars:
    def test_sina_fallback_persists_none_partition(self, tmp_path, monkeypatch):
        """新浪兜底数据诚实落 PG(source='sina', adjust='none'), 供后续 none 读。"""
        engine = _mk_sqlite_db(tmp_path, name="p.db")
        url = f"sqlite:///{tmp_path / 'p.db'}"
        monkeypatch.setattr("src.web.database.DB_URL", url)
        c = kc.KlineCollector(MarketCode.CN)
        bars = [kc.KlineData(date="2026-09-05", open=10, high=11, low=9,
                             close=10.5, volume=100)]
        c._persist_bars("600519", bars, source="sina", adjust="none")
        assert _sel_all(engine) == [("sina", "none", 10.5, 100)]

    def test_persist_bars_fail_soft_on_bad_db(self, monkeypatch, tmp_path):
        """库不可达时 fail-soft 不抛(兜底路径不能反过来打死采集)。"""
        monkeypatch.setattr("src.web.database.DB_URL", f"sqlite:///{tmp_path / 'nope.db'}")
        c = kc.KlineCollector(MarketCode.CN)
        bars = [kc.KlineData(date="2026-09-05", open=10, high=11, low=9,
                             close=10.5, volume=100)]
        c._persist_bars("600519", bars, source="sina", adjust="none")  # 不抛即过
