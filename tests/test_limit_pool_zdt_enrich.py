"""主源成功后补齐封单/首封/炸板(2026-11-11)。

生产实测缺陷(修前): `MarketSentimentCollector._limit_up_pool_tq` 的 85 只涨停票
**ltsz / turnover_rate / order_amount 全 0、first_time 全空** —— K 线自算路径没有
这三类字段; 只有 TQ `get_zdt_data` 快照有(FirstTimeZT / OpenTimesZT / FDVolMaxZT),
而快照此前只在主源失败时当备源用。

本文件钉住新增的 `enrich_pool_with_zdt(pool, *, fetcher=None)` 与它的接线:

1. **只补缺失**: 已有非零/非空值的字段一个都不许被覆盖(主源/其他源的真值优先);
2. **空快照/异常原样返回**: 不改入参, 不加任何标记字段, 失败不影响主流程;
3. **不编造日期**: `enrich_as_of` 取不到就 None, 绝不用"今天"冒充;
4. **事故铁律**: 快照分片单次 ≤50 只;
5. **接线**: 仅请求当日/非历史日才补; 历史请求日的池子不拿当日快照盖。
"""
from __future__ import annotations

import copy
from datetime import datetime

import marketdata.vendors.tq as tqmod
from src.collectors.market_sentiment_collector import MarketSentimentCollector
from src.core.limit_pool_zdt import enrich_pool_with_zdt

_TODAY = datetime.now().strftime("%Y%m%d")


def _row(**kw) -> dict:
    """主源 `build_pool_items` 的真实形状(缺 first_time/open_times/ltsz)。"""
    base = {
        "code": "001234", "name": "样本", "price": 6.05, "pct": 10.0, "amount": 3e7,
        "ltsz": 0.0, "first_time": "", "last_time": "", "days": 2,
        "limit_pct": 10.0, "sector": "", "theme": "", "reason": "",
        "turnover_rate": 0.0, "order_amount": 0.0, "source": "tq",
    }
    base.update(kw)
    return base


# 快照样本: 001234 涨停(2 次炸板, 封单 1200 手) / 600519 涨停(未开板) / 002238 普通股
_SNAP = {
    "001234.SZ": {"FDVolMaxZT": "1200", "FirstTimeZT": "09:31:05", "OpenTimesZT": "2"},
    "600519.SH": {"FDVolMaxZT": "88", "FirstTimeZT": 145503, "OpenTimesZT": "0"},
    "002238.SZ": {"TimeNow": "15:00:00"},
}


def _fetcher_of(snap: dict, seen: list | None = None):
    """假 fetcher: 只回本题码表里的记录(模拟网关按码回), 并记录分片大小。"""

    def fetch(part: list[str]) -> dict:
        assert len(part) <= 50, f"事故铁律: 单次 ≤50 只, 实收 {len(part)}"
        if seen is not None:
            seen.append(list(part))
        return {c: snap[c] for c in part if c in snap}

    return fetch


class TestEnrichFillsMissing:
    def test_补首封_炸板_封单(self):
        pool = [_row()]
        out = enrich_pool_with_zdt(pool, fetcher=_fetcher_of(_SNAP))
        r = out[0]
        assert r["first_time"] == "09:31:05"       # 主源 K线路径恒为 ""
        assert r["open_times"] == 2                # TQ 独有: 炸板次数
        assert r["order_amount"] == 1200 * 100.0   # 封单量 手 → 股
        assert r["enrich_source"] == "tq_zdt"
        # 连板数口径不动: 快照不参与
        assert r["days"] == 2
        assert r["source"] == "tq"                 # 主源标识不许被顶掉

    def test_未开板炸板次数为0也补(self):
        pool = [_row(code="600519")]
        out = enrich_pool_with_zdt(pool, fetcher=_fetcher_of(_SNAP))
        assert out[0]["open_times"] == 0
        assert out[0]["first_time"] == "14:55:03"  # 数字 145503 → HH:MM:SS

    def test_已有非零值不被覆盖(self):
        pool = [_row(first_time="09:25:00", order_amount=9.9e6, open_times=5, ltsz=1.2e10)]
        before = copy.deepcopy(pool)
        out = enrich_pool_with_zdt(pool, fetcher=_fetcher_of(_SNAP))
        assert out[0]["first_time"] == before[0]["first_time"]
        assert out[0]["order_amount"] == before[0]["order_amount"]
        assert out[0]["open_times"] == before[0]["open_times"]
        assert out[0]["ltsz"] == before[0]["ltsz"]
        # 一个字段都没补进去 ⇒ 不许挂"我补过"的标记
        assert "enrich_source" not in out[0]

    def test_部分缺失只补缺的那个(self):
        pool = [_row(first_time="09:25:00", order_amount=0.0, open_times=5)]
        out = enrich_pool_with_zdt(pool, fetcher=_fetcher_of(_SNAP))
        assert out[0]["first_time"] == "09:25:00"   # 已有真值保留
        assert out[0]["open_times"] == 5            # 已有真值保留
        assert out[0]["order_amount"] == 1200 * 100.0  # 只补缺失的
        assert out[0]["enrich_source"] == "tq_zdt"

    def test_盘中旧的0允许被最新炸板次数刷新(self):
        """0 = 池子默认值(没有该字段) ⇒ 可被快照刷新; 非零真值才永不覆盖。"""
        snap = {"001234.SZ": {"FirstTimeZT": "09:31:05", "OpenTimesZT": "3"}}
        out = enrich_pool_with_zdt(
            [_row(open_times=0, first_time="09:31:05", order_amount=1.0)],
            fetcher=_fetcher_of(snap),
        )
        assert out[0]["open_times"] == 3

    def test_快照无此票的行不动(self):
        pool = [_row(code="000001")]
        out = enrich_pool_with_zdt(pool, fetcher=_fetcher_of(_SNAP))
        assert out[0]["first_time"] == ""
        assert out[0]["order_amount"] == 0.0
        assert "enrich_source" not in out[0]

    def test_换手率快照没有就不编(self):
        """快照 14 字段无换手率 ⇒ 保持 0.0, 不拿封单量凑。"""
        out = enrich_pool_with_zdt([_row()], fetcher=_fetcher_of(_SNAP))
        assert out[0]["turnover_rate"] == 0.0

    def test_ltsz_有才补_单位为亿转元(self):
        snap = {"001234.SZ": {"FirstTimeZT": "09:31:05", "OpenTimesZT": 0, "Ltsz": 55.3}}
        out = enrich_pool_with_zdt([_row()], fetcher=_fetcher_of(snap))
        assert abs(out[0]["ltsz"] - 55.3e8) < 1e-3  # Ltsz 单位=亿(与 get_more_info 同源)

    def test_幂等_二次调用不再改值(self):
        pool = [_row()]
        fetch = _fetcher_of(_SNAP)
        enrich_pool_with_zdt(pool, fetcher=fetch)
        snap1 = copy.deepcopy(pool)
        enrich_pool_with_zdt(pool, fetcher=fetch)
        assert pool == snap1


class TestEnrichAsOf:
    def test_池子有统一as_of则照抄(self):
        pool = [_row(as_of="2026-11-06"), _row(code="600519", as_of="2026-11-06")]
        out = enrich_pool_with_zdt(pool, fetcher=_fetcher_of(_SNAP))
        assert all(r["enrich_as_of"] == "2026-11-06" for r in out)

    def test_池子无as_of则None_不拿今天冒充(self):
        out = enrich_pool_with_zdt([_row()], fetcher=_fetcher_of(_SNAP))
        assert out[0]["enrich_as_of"] is None
        assert out[0]["enrich_source"] == "tq_zdt"

    def test_as_of不一致则None(self):
        pool = [_row(as_of="2026-11-06"), _row(code="600519")]  # 一只缺日期
        out = enrich_pool_with_zdt(pool, fetcher=_fetcher_of(_SNAP))
        assert all(r.get("enrich_as_of") is None for r in out)


class TestEnrichDegrades:
    def test_空快照不改入参(self):
        pool = [_row()]
        before = copy.deepcopy(pool)
        out = enrich_pool_with_zdt(pool, fetcher=lambda part: {})
        assert out == before
        assert "enrich_source" not in out[0]

    def test_快照全是脏行不改入参(self):
        pool = [_row()]
        before = copy.deepcopy(pool)
        out = enrich_pool_with_zdt(pool, fetcher=lambda part: {"001234.SZ": "not-a-dict"})
        assert out == before

    def test_异常返回原对象(self):
        pool = [_row()]
        before = copy.deepcopy(pool)

        def boom(part):
            raise RuntimeError("TQ down")

        out = enrich_pool_with_zdt(pool, fetcher=boom)
        assert out is pool          # 原对象原样交回
        assert out == before

    def test_空池直接返回(self):
        pool: list = []
        assert enrich_pool_with_zdt(pool, fetcher=lambda part: _SNAP) is pool

    def test_连续两批失败即中止(self):
        calls = {"n": 0}

        def boom(part):
            calls["n"] += 1
            raise RuntimeError("TQ down")

        pool = [_row(code=f"{600000 + i:06d}") for i in range(200)]
        enrich_pool_with_zdt(pool, fetcher=boom)
        assert calls["n"] == 2  # 与主源同一铁律: 绝不压测式重试


class TestChunking:
    def test_分片不超过50(self):
        seen: list = []
        pool = [_row(code=f"{600000 + i:06d}") for i in range(120)]
        enrich_pool_with_zdt(pool, fetcher=_fetcher_of(_SNAP, seen))
        assert [len(p) for p in seen] == [50, 50, 20]

    def test_同码只取一次(self):
        seen: list = []
        pool = [_row(), _row(), _row(code="001234.SZ")]  # 裸码/带后缀同票
        enrich_pool_with_zdt(pool, fetcher=_fetcher_of(_SNAP, seen))
        assert [len(p) for p in seen] == [1]


# ════════════════ 接线: _limit_up_pool_tq 组装完成后补 ════════════════


def _tq_stubs(monkeypatch, dates: list[str], snap_calls: list, snap: dict | None = None,
              snap_boom: bool = False):
    def fake_rpc(m, p, timeout=None, **kw):
        if m == "get_stock_list":
            return [{"Code": "600001.SH", "Name": "样本"}]
        if m == "get_market_data":
            assert len(p.get("stock_list") or []) <= 50
            return {"600001.SH": {"Close": ["10.0", "11.0"], "Amount": ["1", "1"], "Date": dates}}
        raise AssertionError(f"未预料的 tq_rpc: {m}")

    def fake_snap(part):
        snap_calls.append(list(part))
        if snap_boom:
            raise RuntimeError("zdt down")
        return snap or {"600001.SH": {"FirstTimeZT": "09:31:05", "OpenTimesZT": "1",
                                      "FDVolMaxZT": "800"}}

    monkeypatch.setattr(tqmod, "tq_rpc", fake_rpc, raising=True)
    monkeypatch.setattr(tqmod, "zdt_snapshot", fake_snap, raising=True)
    coll = MarketSentimentCollector()
    monkeypatch.setattr(coll, "_enrich_pool_blocks", lambda pool, **kw: pool, raising=True)
    return coll


def test_当日请求_主源成功后补齐快照(monkeypatch):
    snap_calls: list = []
    coll = _tq_stubs(monkeypatch, ["20261110", _TODAY], snap_calls)
    pool = coll._limit_up_pool_tq(_TODAY)
    assert [r["code"] for r in pool] == ["600001"]
    assert snap_calls, "当日请求应调一次快照补齐"
    r = pool[0]
    assert r["first_time"] == "09:31:05"
    assert r["open_times"] == 1
    assert r["order_amount"] == 800 * 100.0
    assert r["enrich_source"] == "tq_zdt"
    assert r["enrich_as_of"] == datetime.strptime(_TODAY, "%Y%m%d").strftime("%Y-%m-%d")
    assert r["source"] == "tq"  # 主源标识不动


def test_历史请求日不补当日快照(monkeypatch):
    snap_calls: list = []
    coll = _tq_stubs(monkeypatch, ["20261105", "20261106"], snap_calls)
    pool = coll._limit_up_pool_tq("20261106")
    assert [r["code"] for r in pool] == ["600001"]
    assert not snap_calls, "历史日池子绝不能拿当日快照盖(日期错配)"
    assert pool[0]["first_time"] == ""


def test_补齐失败不影响主流程(monkeypatch):
    snap_calls: list = []
    coll = _tq_stubs(monkeypatch, ["20261110", _TODAY], snap_calls, snap_boom=True)
    pool = coll._limit_up_pool_tq(_TODAY)
    assert [r["code"] for r in pool] == ["600001"]  # 池子照常返回
    assert pool[0]["first_time"] == ""
    assert "enrich_source" not in pool[0]
