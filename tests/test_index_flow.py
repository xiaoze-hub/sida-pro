"""指数盘面资金（`src/core/index_flow.py` + `src/collectors/index_flow_daily.py`）。

钉住四件事：
1. 指数代码的交易所后缀 —— `000300` 是上交所，按代码首位推断会错成 `.SZ`；
2. 四档矩阵的三条恒等式（买卖相等 / 主动两侧=成交额 / 快照成交额对得上）；
3. `main_net` 的口径 = (特大+大) 主动净额，不是"全部"；
4. 取不到数据时**不补 0**（写 None / 跳过），避免把"没有"讲成"0 元"。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.core import index_flow
from src.core.index_codes import tq_index_code


def _amo(xl, lg, md, sm):
    """构造 4×4 矩阵：每档 [买入, 卖出, 主动买, 主动卖]。"""
    return [[float(x) for x in row] for row in (xl, lg, md, sm)]


# 一组自洽的样例：买=卖=1000，主动买=600 主动卖=400
SAMPLE = _amo(
    [100, 100, 80, 20],      # 特大
    [200, 200, 150, 50],     # 大
    [300, 300, 170, 130],    # 中
    [400, 400, 200, 200],    # 小
)


class TestIndexCode:
    def test_sh_index_not_sz(self):
        # 回归：exday_structure._tq_code 按首位判市会把 000300 判成 .SZ
        assert tq_index_code("000300") == "000300.SH"
        assert tq_index_code("000852") == "000852.SH"
        assert tq_index_code("000016") == "000016.SH"

    def test_sz_index(self):
        assert tq_index_code("399001") == "399001.SZ"
        assert tq_index_code("399006") == "399006.SZ"

    def test_non_index_returns_none(self):
        # 002361 是平安银行/神剑这类个股，绝不能当指数处理
        assert tq_index_code("002361") is None
        assert tq_index_code("600111") is None
        assert tq_index_code("") is None

    def test_already_suffixed(self):
        assert tq_index_code("000300.SH") == "000300.SH"
        assert tq_index_code("161725.SZ") is None  # 场内基金，不是指数表里的


class TestParseTiers:
    def test_ok(self):
        assert index_flow.parse_tiers(SAMPLE) == SAMPLE

    @pytest.mark.parametrize(
        "bad",
        [
            None,                       # 字段缺失
            [],                         # 空
            [[1, 2, 3]],                # 行数不足
            [[1, 2, 3, 4]] * 3 + [[1]],  # 末行长度不对
            [["a", "b", "c", "d"]] * 4,  # 非数值
        ],
    )
    def test_reject_bad_shape(self, bad):
        assert index_flow.parse_tiers(bad) is None


class TestValidate:
    def test_self_consistent_passes(self):
        ok, why = index_flow.validate(SAMPLE, snapshot_amount_yuan=1000.0)
        assert ok, why

    def test_buy_sell_mismatch(self):
        amo = _amo([100, 90, 80, 20], [200, 200, 150, 50], [300, 300, 170, 130], [400, 400, 200, 200])
        ok, why = index_flow.validate(amo)
        assert not ok
        assert "买卖" in why

    def test_active_side_not_equal_amount(self):
        # 主动买+主动卖 只有 600，而成交额 1000
        amo = _amo([100, 100, 50, 10], [200, 200, 150, 50], [300, 300, 170, 130], [400, 400, 230, 190])
        ok, why = index_flow.validate(amo)
        assert not ok
        assert "主动买+主动卖" in why

    def test_snapshot_amount_conflict_flags_suspect(self):
        # 快照成交额差 10 倍（万元/元 换算错就是这样，不会报错）
        ok, why = index_flow.validate(SAMPLE, snapshot_amount_yuan=100000.0)
        assert not ok
        assert "快照" in why

    def test_zero_amount(self):
        ok, why = index_flow.validate([[0.0, 0.0, 0.0, 0.0]] * 4)
        assert not ok
        assert "成交额为 0" in why


class TestSummarize:
    def test_main_net_is_xl_plus_lg(self):
        s = index_flow.summarize(SAMPLE)
        assert s["xl_net"] == pytest.approx(60.0)   # 80-20
        assert s["lg_net"] == pytest.approx(100.0)  # 150-50
        assert s["main_net"] == pytest.approx(160.0)
        assert s["net_all"] == pytest.approx(200.0)  # 600-400
        assert s["amount"] == pytest.approx(1000.0)

    def test_mid_small_not_in_main(self):
        s = index_flow.summarize(SAMPLE)
        assert s["md_net"] == pytest.approx(40.0)
        assert s["sm_net"] == pytest.approx(0.0)
        assert s["main_net"] != s["net_all"]


class TestFetchOne:
    def test_no_suffix_when_not_index(self):
        r = index_flow.fetch_one("002361")
        assert r["available"] is False
        assert "非指数" in r["reason"]
        assert r.get("main_net") is None  # 不补 0

    def test_unavailable_keeps_none(self, monkeypatch):
        monkeypatch.setattr(index_flow, "snapshot", lambda code: {})
        monkeypatch.setattr(index_flow, "tiers", lambda code: None)
        r = index_flow.fetch_one("000300")
        assert r["available"] is False
        assert r["caliber"] == index_flow.CALIBER
        assert r.get("main_net") is None


class TestCollector:
    def test_in_session_windows(self):
        from src.collectors.index_flow_daily import in_session

        tz = ZoneInfo("Asia/Shanghai")
        assert in_session(datetime(2026, 11, 3, 10, 0, tzinfo=tz))
        assert in_session(datetime(2026, 11, 3, 9, 30, tzinfo=tz))
        assert not in_session(datetime(2026, 11, 3, 12, 0, tzinfo=tz))
        assert not in_session(datetime(2026, 11, 3, 8, 0, tzinfo=tz))

    def test_to_row_skips_unavailable(self):
        from src.collectors.index_flow_daily import to_row

        assert to_row({"symbol": "000300", "available": False}, "20261103") is None

    def test_to_row_fills_trade_date(self):
        from src.collectors.index_flow_daily import to_row

        row = to_row(
            {"symbol": "000300", "name": "沪深300", "available": True, "trade_date": "",
             "main_net": None},
            "20261103",
        )
        assert row is not None
        assert row["trade_date"] == "20261103"
        assert row["main_net"] is None
        assert row["source"] == "tq"
