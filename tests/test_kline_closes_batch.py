"""批量收盘(给列表行 sparkline 用)的口径钉子(2026-09-22)。

四条硬口径(改了就是 bug):
1. **只读 PG, 绝不联网** —— 列表页几十行, 每行一次联网抓取会把页面拖成几十秒; 库里没有的标的
   如实进 `missing`(前端该行不画 sparkline)。库连不上时**只能是没有**, 不许回落抓取。
2. **不挑单一数据源** —— 生产实测: 库里 tq 375 万行 / tencent 75 万行, 而 002361 **只有 tq**
   (801 行)。按 `source='tencent'` 过滤会让这只票整只丢失(v0.12.0 部署后实测: 三只真票全进
   missing)。同一交易日多源都有时**只取一支**(混源成一条序列就是口径污染)。
3. **绝不补 0 / 绝不编造平线** —— 缺数据只能"没有", 只有 1 个点也画不出形状 ⇒ 算"没有"。
4. **一次批量查** —— 逐只查实测 3-7s/只; 批量 `symbol = ANY(...)` 实测 3 只 60 天 0.02s。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.web.api import klines as K


def _row(sym, day, close, source, market="CN"):
    return (sym, f"{day} 00:00:00+00", close, source)


class TestClosesFromRows:
    """纯函数口径(不依赖 DB/网络, 逐条钉死)。"""

    def test_同一天多源只取一支_且优先腾讯(self):
        rows = [
            _row("002361", "2026-09-18", 10.0, "tq"),
            _row("002361", "2026-09-18", 99.9, "tencent"),
            _row("002361", "2026-09-19", 11.0, "tq"),
        ]
        items, missing = K._closes_from_rows(["002361"], rows, days=20)
        assert missing == []
        # 09-18 只出现一次, 且取 tencent(99.9) → 序列长度 2 而不是 3(不许混源成两个点)
        assert items[0]["closes"] == [99.9, 11.0]
        assert items[0]["source"] == "tq"  # 末日来源如实报出
        assert items[0]["asof"] == "2026-09-19"

    def test_只有_tq_的票不许被丢弃(self):
        """生产实际形态: 002361 在库里只有 tq 源 —— 这正是 v0.12.0 出问题的原因。"""
        rows = [_row("002361", f"2026-09-{d:02d}", 10.0 + d, "tq") for d in range(1, 6)]
        items, missing = K._closes_from_rows(["002361"], rows, days=20)
        assert missing == []
        assert items[0]["closes"] == [11.0, 12.0, 13.0, 14.0, 15.0]

    def test_缺数据的标的进_missing_不补零(self):
        rows = [_row("002361", "2026-09-18", 10.0, "tq"), _row("002361", "2026-09-19", 11.0, "tq")]
        items, missing = K._closes_from_rows(["002361", "600519"], rows, days=20)
        assert [i["symbol"] for i in items] == ["002361"]
        assert missing == ["600519"]  # 不是 0、不是空数组冒充

    def test_只有_1_个点算没有(self):
        items, missing = K._closes_from_rows(["002361"], [_row("002361", "2026-09-18", 10.0, "tq")], days=20)
        assert items == [] and missing == ["002361"]

    def test_只取最后_days_个(self):
        rows = [_row("002361", f"2026-08-{d:02d}", float(d), "tq") for d in range(1, 29)]
        items, _ = K._closes_from_rows(["002361"], rows, days=20)
        assert len(items[0]["closes"]) == 20
        assert items[0]["closes"][-1] == 28.0

    def test_close_为_None_的行被跳过_不当作_0(self):
        rows = [
            _row("002361", "2026-09-17", None, "tq"),
            _row("002361", "2026-09-18", 10.0, "tq"),
            _row("002361", "2026-09-19", 11.0, "tq"),
        ]
        items, _ = K._closes_from_rows(["002361"], rows, days=20)
        assert items[0]["closes"] == [10.0, 11.0]  # 没有 0

    def test_带口径标记(self):
        rows = [_row("002361", "2026-09-18", 10.0, "tq"), _row("002361", "2026-09-19", 11.0, "tq")]
        items, _ = K._closes_from_rows(["002361"], rows, days=20)
        assert items[0]["caliber"] == "pg_klines_hypertable"


class TestEndpoint:
    """端点级: 门禁 + 只读 PG + 天数夹取。"""

    def test_空代码集_400(self):
        with pytest.raises(HTTPException) as ei:
            K.get_closes_batch(symbols="  , ")
        assert ei.value.status_code == 400

    def test_超过上限_400(self):
        too_many = ",".join(f"{i:06d}" for i in range(K.MAX_CLOSES_SYMBOLS + 1))
        with pytest.raises(HTTPException) as ei:
            K.get_closes_batch(symbols=too_many)
        assert ei.value.status_code == 400
        assert str(K.MAX_CLOSES_SYMBOLS) in str(ei.value.detail)

    def test_上限本身是_60(self):
        assert K.MAX_CLOSES_SYMBOLS == 60

    def test_db_不可用时全部进_missing_且不抛错(self, monkeypatch):
        """**不联网**的强证明: 库连不上就只能"没有", 绝不回落抓取。"""
        import src.web.database as dbmod

        class BoomEngine:
            def connect(self, *a, **k):
                raise RuntimeError("db down")

        monkeypatch.setattr(dbmod, "engine", BoomEngine(), raising=False)
        out = K.get_closes_batch(symbols="002361,600519", market="CN", days=20)
        assert out["items"] == []
        assert sorted(out["missing"]) == ["002361", "600519"]

    def test_不触发联网抓取(self, monkeypatch):
        """库里没有的标的 → missing, 而不是回头去联网抓。"""
        import src.web.database as dbmod

        class BoomEngine:
            def connect(self, *a, **k):
                raise RuntimeError("db down")

        monkeypatch.setattr(dbmod, "engine", BoomEngine(), raising=False)

        def boom(*_a, **_k):
            raise AssertionError("列表页 sparkline 数据不该触发联网抓取")

        from src.collectors import kline_collector

        monkeypatch.setattr(kline_collector.KlineCollector, "get_klines", boom, raising=False)
        monkeypatch.setattr(kline_collector, "get_klines", boom, raising=False)
        out = K.get_closes_batch(symbols="002361")
        assert out["missing"] == ["002361"]

    def test_days_夹取在_2_到_120(self, monkeypatch):
        """负数/超大值不许直达 SQL(窗口会算成负区间或全表)。"""
        import src.web.database as dbmod

        seen: list[int] = []

        class _FakeResult:
            def fetchall(self):
                return []

        class _FakeConn:
            def execute(self, *a, **k):
                return _FakeResult()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeEngine:
            def connect(self, *a, **k):
                return _FakeConn()

        def fake_rows(wanted, rows, days):
            seen.append(days)
            return [], list(wanted)

        monkeypatch.setattr(dbmod, "engine", _FakeEngine(), raising=False)
        monkeypatch.setattr(K, "_closes_from_rows", fake_rows)
        monkeypatch.setattr(K, "_CLOSES_TTL", 0)
        # 用**不同代码集**避免命中同一缓存键(缓存键含代码集, 与天数是两回事)
        K.get_closes_batch(symbols="002361", days=-5)
        K.get_closes_batch(symbols="600519", days=9999)
        assert seen == [2, 120]
