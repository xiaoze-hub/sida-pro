# -*- coding: utf-8 -*-
"""thsdk_big_order 数据源单测(明盘链路)。

验证 fetch_l2_ticks(code, "thsdk_big_order") 的路由、格式、方向解码。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import dark_l2  # noqa: E402


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def test_fetch_l2_ticks_routestohsdk_big_order():
    """source='thsdk_big_order' 应路由到 _fetch_big_order, 不抛 NotImplementedError。"""
    # 无凭据时应抛 RuntimeError(不是 NotImplementedError)
    with pytest.raises(RuntimeError):
        dark_l2.fetch_l2_ticks("sz002361", "thsdk_big_order")


def test_fetch_l2_ticks_unknown_source():
    """未知 source 应抛 NotImplementedError。"""
    with pytest.raises(NotImplementedError, match="未接入"):
        dark_l2.fetch_l2_ticks("sz002361", "unknown_source")


# ---------------------------------------------------------------------------
# _rows_from_resp 兼容性
# ---------------------------------------------------------------------------


class FakeResp:
    def __init__(self, data):
        self._data = data

    @property
    def data(self):
        return self._data

    @property
    def df(self):
        return None


class FakeDfResp:
    def __init__(self, records):
        self._records = records
        self.data = None

    @property
    def df(self):
        import pandas as pd
        return pd.DataFrame(self._records)


def test_rows_from_resp_list():
    rows = dark_l2._rows_from_resp(FakeResp([{"a": 1}, {"b": 2}]))
    assert rows == [{"a": 1}, {"b": 2}]


def test_rows_from_resp_none():
    assert dark_l2._rows_from_resp(None) == []


def test_rows_from_resp_df():
    records = [{"时间": 1700000000, "成交量": 1000, "总金额": 10000}]
    rows = dark_l2._rows_from_resp(FakeDfResp(records))
    assert len(rows) == 1
    assert rows[0]["时间"] == 1700000000


# ---------------------------------------------------------------------------
# _fetch_big_order 格式验证(用 mock 数据)
# ---------------------------------------------------------------------------


def test_fetch_big_order_formats(monkeypatch):
    """模拟 big_order_flow 返回, 验证 ticks 格式。"""
    mock_rows = [
        {"时间": 1756693500, "成交量": 10000, "总金额": 100000, "成交方向": 1},  # 主动买
        {"时间": 1756693510, "成交量": 5000, "总金额": 50000, "成交方向": -1},  # 主动卖
        {"时间": 1756693520, "成交量": 8000, "总金额": 80000, "成交方向": 2},   # 被动买
        {"时间": 1756693530, "成交量": 3000, "总金额": 30000, "成交方向": -2},  # 被动卖
    ]

    class MockResp:
        data = mock_rows
        df = None

    monkeypatch.setattr(dark_l2, "_query_thsdk", lambda *a, **k: MockResp())
    ticks = dark_l2._fetch_big_order("sz002361")

    assert len(ticks) == 4
    # 检查格式
    for t in ticks:
        assert "d" in t and t["d"] in ("B", "S")
        assert "amt" in t and t["amt"] > 0
        assert "vol" in t and t["vol"] > 0
        assert "side" in t and t["side"] in ("active", "passive")
        assert "t" in t and len(t["t"]) == 8 and t["t"][2] == ":"

    # 检查方向映射
    assert ticks[0]["d"] == "B" and ticks[0]["side"] == "active"
    assert ticks[1]["d"] == "S" and ticks[1]["side"] == "active"
    assert ticks[2]["d"] == "B" and ticks[2]["side"] == "passive"
    assert ticks[3]["d"] == "S" and ticks[3]["side"] == "passive"

    # 检查金额
    assert ticks[0]["amt"] == 100000.0
    assert ticks[1]["amt"] == 50000.0


def test_fetch_big_order_filters_invalid():
    """过滤无效行(成交量=0/方向非法)。"""
    mock_rows = [
        {"时间": 1756693500, "成交量": 0, "总金额": 0, "成交方向": 1},  # 成交量=0 → 跳过
        {"时间": 1756693510, "成交量": 1000, "总金额": 10000, "成交方向": 99},  # 方向非法 → 跳过
        {"时间": 1756693520, "成交量": 5000, "总金额": 50000, "成交方向": 1},  # 有效
    ]

    class MockResp:
        data = mock_rows
        df = None

    monkeypatch = pytest.importorskip("pytest", reason="pytest required")
    import sys
    from pathlib import Path
    ROOT = Path(__file__).parent.parent
    sys.path.insert(0, str(ROOT))
    from src.core import dark_l2

    # 手动 monkeypatch (不在函数体内)
    original_query = dark_l2._query_thsdk
    dark_l2._query_thsdk = lambda *a, **k: MockResp()
    try:
        ticks = dark_l2._fetch_big_order("sz002361")
        assert len(ticks) == 1
        assert ticks[0]["amt"] == 50000.0
    finally:
        dark_l2._query_thsdk = original_query


def test_fetch_big_order_empty_raises():
    """空数据应抛 RuntimeError。"""
    monkeypatch = pytest.importorskip("pytest", reason="pytest required")
    import sys
    from pathlib import Path
    ROOT = Path(__file__).parent.parent
    sys.path.insert(0, str(ROOT))
    from src.core import dark_l2

    original_query = dark_l2._query_thsdk
    dark_l2._query_thsdk = lambda *a, **k: type('R', (), {'data': [], 'df': None})()
    try:
        with pytest.raises(RuntimeError, match="空数据"):
            dark_l2._fetch_big_order("sz002361")
    finally:
        dark_l2._query_thsdk = original_query


def test_fetch_big_order_no_env_raises():
    """THS_USERNAME/THS_PASSWORD 未设置应抛 RuntimeError。"""
    monkeypatch = pytest.importorskip("pytest", reason="pytest required")
    import sys
    from pathlib import Path
    ROOT = Path(__file__).parent.parent
    sys.path.insert(0, str(ROOT))
    from src.core import dark_l2

    original_query = dark_l2._query_thsdk
    dark_l2._query_thsdk = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("THS_USERNAME/THS_PASSWORD 未设置"))
    try:
        with pytest.raises(RuntimeError, match="THS_USERNAME"):
            dark_l2._fetch_big_order("sz002361")
    finally:
        dark_l2._query_thsdk = original_query
