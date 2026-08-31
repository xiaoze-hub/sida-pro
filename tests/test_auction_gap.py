"""竞价异动池 gap_pct/withdraw_rate 推导测试(2026-08-24 v0.3.2 字段口径二次修正)。

实测 thsdk call_auction_anomaly 返回 6 列:
  时间 / 价格 / 总金额 / 代码 / 名称 / 异动类型1

"价格" 列**不是价格**, 而是异动幅度的小数比例(或撤单率 / 占位 1.0)。
"总金额" 列恒为 2147483648 (int32 上限占位垃圾), _to_records 已 skip。

覆盖:
- _to_records: 各类异动类型的 gap_pct / withdraw_rate 推导
  * 急速上涨 / 急速下跌 / 大幅高开 / 大幅低开: gap_pct = 价格 × 100
  * 涨停试盘 / 跌停试盘:                价格恒为 1.0(占位) -> gap_pct = None
  * 涨停撤单 / 跌停撤单:                价格 = 撤单率 -> withdraw_rate = 价格 × 100
  * 其他类型(兜底安全网):             |价格| < 0.21 才按涨跌幅处理 -> gap_pct = 价格 × 100
                                       否则 -> None
- 总金额垃圾列不应进入 record 字段
- fetch_auction_anomaly 整合: 无 klines 依赖, 由 _to_records 直接推导
- API 响应 missing_fields 仅含 volume_ratio (withdraw_rate 已部分填充)
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pandas as pd
import pytest

_FX = os.path.join(os.path.dirname(__file__), "fixtures")
if _FX not in sys.path:
    sys.path.insert(0, _FX)
import mock_main_flow as mmf  # noqa: E402

from src.core import auction_pool  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_pool_cache():
    auction_pool.clear_cache()
    yield
    auction_pool.clear_cache()


@pytest.fixture
def mock_thsdk_l2(monkeypatch):
    """注入 data_source.thsdk_l2 内存桩(get_auction_anomaly 返回 fake DF)。"""
    mod = mmf.fake_thsdk_l2_module()
    mod.get_auction_anomaly = MagicMock(return_value=mmf.fake_auction_df())
    monkeypatch.setitem(sys.modules, "data_source.thsdk_l2", mod)
    return mod


# ── _to_records: 4 类涨跌幅比例类型 ──────────────────────────────────────
def test_to_records_jisu_up_typed_ratio():
    """急速上涨 + 价格=0.0523 → gap_pct = 5.23, withdraw_rate=None。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.0523, "总金额": 2147483648,
        "代码": "600000", "名称": "测试A", "异动类型1": "急速上涨",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] == pytest.approx(5.23, abs=0.01)
    assert recs[0]["withdraw_rate"] is None
    assert recs[0]["volume_ratio"] is None


def test_to_records_jisu_down_negative_ratio():
    """急速下跌 + 价格=-0.0418 → gap_pct = -4.18。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": -0.0418, "总金额": 2147483648,
        "代码": "600000", "名称": "测试A", "异动类型1": "急速下跌",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] == pytest.approx(-4.18, abs=0.01)
    assert recs[0]["withdraw_rate"] is None


def test_to_records_big_open_high():
    """大幅高开 + 价格=0.0335 → gap_pct = 3.35。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.0335, "总金额": 2147483648,
        "代码": "002361", "名称": "测试B", "异动类型1": "大幅高开",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] == pytest.approx(3.35, abs=0.01)
    assert recs[0]["withdraw_rate"] is None


def test_to_records_big_open_low():
    """大幅低开 + 价格=-0.0178 → gap_pct = -1.78。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": -0.0178, "总金额": 2147483648,
        "代码": "600000", "名称": "测试C", "异动类型1": "大幅低开",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] == pytest.approx(-1.78, abs=0.01)


# ── _to_records: 2 类涨停/跌停试盘(价格=1.0 占位) ────────────────────────
def test_to_records_limit_up_probe_no_gap():
    """涨停试盘 + 价格=1.0 (恒为 1.0 占位) → gap_pct = None。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 1.0, "总金额": 2147483648,
        "代码": "600000", "名称": "测试D", "异动类型1": "涨停试盘",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] is None
    assert recs[0]["withdraw_rate"] is None
    assert recs[0]["volume_ratio"] is None


def test_to_records_limit_down_probe_no_gap():
    """跌停试盘 + 价格=1.0 → gap_pct = None(对称)。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 1.0, "总金额": 2147483648,
        "代码": "600000", "名称": "测试E", "异动类型1": "跌停试盘",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] is None
    assert recs[0]["withdraw_rate"] is None


# ── _to_records: 2 类涨停/跌停撤单(价格=撤单率) ─────────────────────────
def test_to_records_limit_up_withdraw():
    """涨停撤单 + 价格=0.65 → withdraw_rate = 65.0, gap_pct = None。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.65, "总金额": 2147483648,
        "代码": "600000", "名称": "测试F", "异动类型1": "涨停撤单",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] is None
    assert recs[0]["withdraw_rate"] == pytest.approx(65.0, abs=0.01)
    assert recs[0]["volume_ratio"] is None


def test_to_records_limit_down_withdraw():
    """跌停撤单 + 价格=0.78 → withdraw_rate = 78.0, gap_pct = None。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.78, "总金额": 2147483648,
        "代码": "600000", "名称": "测试G", "异动类型1": "跌停撤单",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["withdraw_rate"] == pytest.approx(78.0, abs=0.01)
    assert recs[0]["gap_pct"] is None


def test_to_records_withdraw_low_edge():
    """涨停撤单 + 价格=0.5 (撤单率下界) → withdraw_rate = 50.0。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.5, "总金额": 2147483648,
        "代码": "600000", "名称": "测试GA", "异动类型1": "涨停撤单",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["withdraw_rate"] == pytest.approx(50.0, abs=0.01)


def test_to_records_withdraw_high_edge():
    """涨停撤单 + 价格=0.9 (撤单率上界) → withdraw_rate = 90.0。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.9, "总金额": 2147483648,
        "代码": "600000", "名称": "测试GB", "异动类型1": "涨停撤单",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["withdraw_rate"] == pytest.approx(90.0, abs=0.01)


# ── _to_records: 其他类型兜底安全网 ──────────────────────────────────────
def test_to_records_other_type_safe_zone_positive():
    """其他类型(如 '高开')+ |价格|=0.05 (在 -0.21~0.21 区间) → gap_pct = 5.0。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.05, "总金额": 2147483648,
        "代码": "600000", "名称": "测试H", "异动类型1": "高开",   # 非 ratio_types
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] == pytest.approx(5.0, abs=0.01)
    assert recs[0]["withdraw_rate"] is None


def test_to_records_other_type_safe_zone_negative():
    """其他类型 + 价格=-0.03 (在安全区) → gap_pct = -3.0。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": -0.03, "总金额": 2147483648,
        "代码": "600000", "名称": "测试J", "异动类型1": "低开",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] == pytest.approx(-3.0, abs=0.01)


def test_to_records_other_type_out_of_safe_zone_high():
    """其他类型 + |价格|=0.5 (超出安全区上限) → gap_pct = None (视为脏数据)。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.5, "总金额": 2147483648,
        "代码": "600000", "名称": "测试I", "异动类型1": "高开",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] is None
    assert recs[0]["withdraw_rate"] is None


def test_to_records_other_type_out_of_safe_zone_low():
    """其他类型 + |价格|=0.5 (超出安全区下限) → gap_pct = None。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": -0.5, "总金额": 2147483648,
        "代码": "600000", "名称": "测试IL", "异动类型1": "低开",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] is None


def test_to_records_other_type_safe_zone_boundary():
    """其他类型 + 价格 = 0.21 边界值 → 严格小于 0.21, 不算 ratio。

    `_SAFE_RATIO_ABS = 0.21`, 严格小于(不含等号): 0.21 视为 None。
    """
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.21, "总金额": 2147483648,
        "代码": "600000", "名称": "测试K", "异动类型1": "高开",
    }])
    recs = auction_pool._to_records(df)
    # 0.21 不在 (-0.21, 0.21) 严格开区间内 -> gap_pct = None
    assert recs[0]["gap_pct"] is None


# ── _to_records: 边界条件 ─────────────────────────────────────────────
def test_to_records_price_missing_in_ratio_type():
    """价格缺失 -> gap_pct = None (即使类型本应有 gap_pct)。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": None, "总金额": 2147483648,
        "代码": "600000", "名称": "测试M", "异动类型1": "大幅高开",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] is None
    assert recs[0]["withdraw_rate"] is None


def test_to_records_price_missing_in_withdraw_type():
    """撤单类型 + 价格缺失 -> withdraw_rate = None (不能误算成 0)。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": None, "总金额": 2147483648,
        "代码": "600000", "名称": "测试N", "异动类型1": "涨停撤单",
    }])
    recs = auction_pool._to_records(df)
    assert recs[0]["gap_pct"] is None
    assert recs[0]["withdraw_rate"] is None


def test_to_records_empty_df():
    """空 DataFrame -> 空 list。"""
    assert auction_pool._to_records(None) == []
    assert auction_pool._to_records(pd.DataFrame()) == []


def test_to_records_total_amount_garbage_ignored():
    """总金额列恒为 int32 上限占位(2147483648), 不应进入 record 字段。

    v0.3.2 修复: 总金额列已加入 skip_norm, 不再作为 '总金额' key 出现在 record dict 中。
    """
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.05, "总金额": 2147483648,
        "代码": "600000", "名称": "测试L", "异动类型1": "急速上涨",
    }])
    recs = auction_pool._to_records(df)
    assert "总金额" not in recs[0]
    # 也不能用 2147483648 当作金额影响 gap_pct
    assert recs[0]["gap_pct"] == pytest.approx(5.0, abs=0.01)


def test_to_records_no_anomaly_type():
    """异动类型缺失(空字符串) -> 走兜底安全网分支。"""
    df = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.02, "总金额": 2147483648,
        "代码": "600000", "名称": "测试O", "异动类型1": "",
    }])
    recs = auction_pool._to_records(df)
    # 兜底: |0.02| < 0.21 -> gap_pct = 2.0
    assert recs[0]["gap_pct"] == pytest.approx(2.0, abs=0.01)
    assert recs[0]["withdraw_rate"] is None


# ── fetch_auction_anomaly 整合(无 klines 依赖) ──────────────────────────
def test_fetch_auction_anomaly_uses_new_logic(mock_thsdk_l2):
    """完整链路: thsdk 拉 6 列 -> _to_records 推导 -> 直接拿到 gap_pct/withdraw_rate。

    fake_auction_df 已用真实口径:
      002361 大幅高开 0.0335 → gap_pct = 3.35
      600000 大幅低开 -0.0178 → gap_pct = -1.78
    """
    recs = auction_pool.fetch_auction_anomaly("CN")
    assert len(recs) == 2
    assert recs[0]["gap_pct"] == pytest.approx(3.35, abs=0.01)
    assert recs[0]["withdraw_rate"] is None
    assert recs[1]["gap_pct"] == pytest.approx(-1.78, abs=0.01)
    # volume_ratio 数据源不提供, 固定 None
    assert recs[0]["volume_ratio"] is None
    assert recs[1]["volume_ratio"] is None


def test_fetch_auction_anomaly_withdraw_via_fixture(mock_thsdk_l2):
    """涨停撤单类型记录 -> withdraw_rate 由价格列直接推导, 无需 klines。"""
    mock_thsdk_l2.get_auction_anomaly.return_value = pd.DataFrame([{
        "时间": "09:25:00", "价格": 0.72, "总金额": 2147483648,
        "代码": "600000", "名称": "测试撤单", "异动类型1": "涨停撤单",
    }])
    recs = auction_pool.fetch_auction_anomaly("CN")
    assert recs[0]["withdraw_rate"] == pytest.approx(72.0, abs=0.01)
    assert recs[0]["gap_pct"] is None


def test_fetch_auction_anomaly_probe_via_fixture(mock_thsdk_l2):
    """涨停试盘类型记录 -> gap_pct = None (价格=1.0 占位无信息)。"""
    mock_thsdk_l2.get_auction_anomaly.return_value = pd.DataFrame([{
        "时间": "09:25:00", "价格": 1.0, "总金额": 2147483648,
        "代码": "600000", "名称": "测试试盘", "异动类型1": "涨停试盘",
    }])
    recs = auction_pool.fetch_auction_anomaly("CN")
    assert recs[0]["gap_pct"] is None
    assert recs[0]["withdraw_rate"] is None


def test_missing_fields_constant():
    """MISSING_FIELDS / MISSING_NOTE 仅声明 volume_ratio(数据源不提供)。

    v0.3.2 修正: withdraw_rate 不再 always-missing(仅对撤单类型记录填入)。
    """
    assert auction_pool.MISSING_FIELDS == ["volume_ratio"]
    assert "量比" in auction_pool.MISSING_NOTE
    # withdraw_rate 不再 always-missing, NOTE 不应再列它
    assert "withdraw_rate" not in auction_pool.MISSING_NOTE


# ── API 响应: missing_fields + note ──────────────────────────────────────
def test_api_anomaly_response_includes_missing_fields(mock_thsdk_l2):
    """anomaly 接口响应带 missing_fields(仅含 volume_ratio) + 真实 gap_pct。"""
    from src.web.api.auction_pool import anomaly as _anomaly_handler

    body = _anomaly_handler(market="CN")
    assert body["available"] is True
    assert body["missing_fields"] == ["volume_ratio"]
    assert "量比" in body["note"]
    # record 字段: gap_pct 真实值 / withdraw_rate / volume_ratio 固定 None
    first = body["records"][0]
    assert first["volume_ratio"] is None
    assert first["gap_pct"] == pytest.approx(3.35, abs=0.01)


def test_api_anomaly_unavailable_includes_missing_fields(monkeypatch):
    """数据源不可用 -> available=false, 响应仍带 missing_fields + note。"""
    from src.web.api.auction_pool import anomaly as _anomaly_handler

    def fake_fetch_empty(market="CN"):
        return []

    monkeypatch.setattr(
        "src.web.api.auction_pool.fetch_auction_anomaly", fake_fetch_empty
    )
    body = _anomaly_handler(market="CN")
    assert body["available"] is False
    assert body["count"] == 0
    assert body["missing_fields"] == ["volume_ratio"]
    assert "量比" in body["note"]
    assert "数据源未接入" in body["note"] or "不可用" in body["note"]
