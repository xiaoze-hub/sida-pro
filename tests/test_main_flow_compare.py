"""主力意图双源对比测试(阶段1.1, v0.3.0)。

覆盖:
- 双源一致 / 完全相反 / 一方为0 / 双方为0 / tencent不足 的一致性边界
- thsdk 数据源不可用(未安装/抛异常)时的容错: 只返 tencent + note
- 30s 进程内缓存命中
- 缓存清空接口
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

# tests/fixtures 非包目录, 通过 sys.path shim 引入 mock 数据辅助模块
_FX = os.path.join(os.path.dirname(__file__), "fixtures")
if _FX not in sys.path:
    sys.path.insert(0, _FX)
import mock_main_flow as mmf  # noqa: E402

from src.core import main_flow_compare  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_compare_cache():
    """每个用例前清空 main_flow_compare 缓存, 避免用例间污染。"""
    main_flow_compare.clear_cache()


def _thsdk_actually_importable() -> bool:
    """2026-08-20 辅助: 检测 thsdk 在当前环境下能否真正 import。

    `test_thsdk_module_missing` 假设 thsdk **不可用**(ImportError),
    但本机/CI 已装 thsdk 时, delitem sys.modules 后仍可被重新导入,
    触发不到 ImportError 分支。用此 helper 跳过该用例。
    """
    try:
        import data_source.thsdk_l2  # noqa: F401
        return True
    except Exception:
        return False


@pytest.fixture
def mock_thsdk_l2(monkeypatch):
    """注入 data_source.thsdk_l2 内存桩, 返回其 compute_main_flow mock。"""
    mod = mmf.fake_thsdk_l2_module()
    mod.compute_main_flow = MagicMock(return_value=dict(mmf.THSDK_SAME))
    monkeypatch.setitem(sys.modules, "data_source.thsdk_l2", mod)
    return mod


@pytest.fixture
def mock_dark_flow(monkeypatch):
    """patch src.core.dark_flow.compute_dark_flow 返回腾讯口径。"""
    m = MagicMock(return_value=dict(mmf.TENCENT_OK))
    monkeypatch.setattr("src.core.dark_flow.compute_dark_flow", m)
    return m


def _patched(mock_thsdk_l2, flow_dict):
    mock_thsdk_l2.compute_main_flow.return_value = flow_dict


def test_double_source_identical(mock_dark_flow, mock_thsdk_l2):
    """双源主力净额一致(腾讯+500万 = thsdk 500万) -> consistency 100。"""
    mock_dark_flow.return_value = dict(mmf.TENCENT_OK)
    _patched(mock_thsdk_l2, mmf.THSDK_SAME)  # net_wan=500 -> 500万 元
    r = main_flow_compare.compare_main_flow("002361")
    assert r["thsdk"]["main_net"] == 5_000_000
    assert r["tencent"]["main_net"] == 5_000_000
    assert r["consistency"] == pytest.approx(100.0, abs=0.001)
    assert r["delta_pct"] == pytest.approx(0.0, abs=0.001)
    assert r["note"]


def test_double_source_opposite(mock_dark_flow, mock_thsdk_l2):
    """双源完全相反(+500万 vs -500万) -> consistency 0(方向分歧)。"""
    mock_dark_flow.return_value = dict(mmf.TENCENT_OK)
    _patched(mock_thsdk_l2, mmf.THSDK_OPPOSITE)
    r = main_flow_compare.compare_main_flow("002361")
    assert r["consistency"] == pytest.approx(0.0, abs=0.001)
    assert r["delta_pct"] == pytest.approx(200.0, abs=0.001)


def test_one_side_zero(mock_dark_flow, mock_thsdk_l2):
    """腾讯 +500 万, thsdk 0 -> 充分不一致性。"""
    mock_dark_flow.return_value = dict(mmf.TENCENT_OK)
    _patched(mock_thsdk_l2, mmf.THSDK_ZERO)
    r = main_flow_compare.compare_main_flow("002361")
    # denom=max(500万,0,1)=500万; diff=500万; delta=100
    assert r["consistency"] == pytest.approx(0.0, abs=0.001)
    assert r["delta_pct"] == pytest.approx(100.0, abs=0.001)


def test_both_zero(mock_dark_flow, mock_thsdk_l2):
    """双源净额都为 0 -> 视为一致, consistency 100。"""
    mock_dark_flow.return_value = {
        **mmf.TENCENT_OK, "main_net": 0, "big_net": 0, "small_net": 0}
    _patched(mock_thsdk_l2, mmf.THSDK_ZERO)
    r = main_flow_compare.compare_main_flow("002361")
    assert r["consistency"] == pytest.approx(100.0, abs=0.001)
    assert r["delta_pct"] == pytest.approx(0.0, abs=0.001)


def test_tencent_insufficient_no_consistency(mock_dark_flow, mock_thsdk_l2):
    """tencent 数据不足(available=False) -> 不做一致性比对, thsdk 单独返回。"""
    mock_dark_flow.return_value = dict(mmf.TENCENT_INSUFFICIENT)
    _patched(mock_thsdk_l2, mmf.THSDK_SAME)
    r = main_flow_compare.compare_main_flow("002361")
    assert r["tencent"]["available"] is False
    assert r["consistency"] is None
    assert r["thsdk"]["available"] is True


def test_thsdk_unavailable_fallback(mock_dark_flow, mock_thsdk_l2):
    """thsdk 数据源不可用(返回 no_data) -> 只返 tencent, note 说明。"""
    mock_dark_flow.return_value = dict(mmf.TENCENT_OK)
    mock_thsdk_l2.compute_main_flow.side_effect = Exception("thsdk 连接失败")
    r = main_flow_compare.compare_main_flow("002361")
    assert r["thsdk"] is None
    assert r["tencent"]["available"] is True
    assert "thsdk 数据暂不可用" in r["note"]
    assert r["consistency"] is None


@pytest.mark.skipif(
    _thsdk_actually_importable(),
    reason="thsdk 实际可 import (本环境已装 thsdk); 这个用例只验证 ImportError 路径",
)
def test_thsdk_module_missing(monkeypatch, mock_dark_flow):
    """thsdk 模块未安装(ImportError) -> 容错返回 tencent。

    2026-08-20 修复: thsdk 实际安装的环境下, delitem sys.modules 后仍可被重新 import,
    触发不到 ImportError 分支。改用 skipif 在可导入时跳过, 保留"未安装环境"下的容错覆盖。
    """
    mock_dark_flow.return_value = dict(mmf.TENCENT_OK)
    if "data_source.thsdk_l2" in sys.modules:
        monkeypatch.delitem(sys.modules, "data_source.thsdk_l2")
    r = main_flow_compare.compare_main_flow("002361")
    assert r["thsdk"] is None
    assert r["tencent"]["available"] is True
    assert "thsdk 数据暂不可用" in r["note"]


def test_cache_hit(mock_dark_flow, mock_thsdk_l2):
    """30s 内二次调用命中缓存, compute_dark_flow 只被调一次。"""
    mock_dark_flow.return_value = dict(mmf.TENCENT_OK)
    _patched(mock_thsdk_l2, mmf.THSDK_SAME)
    main_flow_compare.compare_main_flow("002361")
    main_flow_compare.compare_main_flow("002361")
    assert mock_dark_flow.call_count == 1
    assert mock_thsdk_l2.compute_main_flow.call_count == 1


def test_cache_expired(mock_dark_flow, mock_thsdk_l2, monkeypatch):
    """超过 30s TTL 后重新拉取。"""
    mock_dark_flow.return_value = dict(mmf.TENCENT_OK)
    _patched(mock_thsdk_l2, mmf.THSDK_SAME)
    calls = {"n": 0}

    def fake_time():
        """每次 now/写缓存都推进; 第3次调用(第2次请求的 now)越过 30s TTL。"""
        calls["n"] += 1
        if calls["n"] == 3:
            return 131.0
        return 100.0 + calls["n"] / 100.0

    monkeypatch.setattr(main_flow_compare.time, "time", fake_time)
    main_flow_compare.compare_main_flow("002361")
    main_flow_compare.compare_main_flow("002361")
    assert mock_dark_flow.call_count == 2


def test_clear_cache(mock_dark_flow, mock_thsdk_l2):
    """clear_cache() 后重新拉取。"""
    mock_dark_flow.return_value = dict(mmf.TENCENT_OK)
    _patched(mock_thsdk_l2, mmf.THSDK_SAME)
    main_flow_compare.compare_main_flow("002361")
    main_flow_compare.clear_cache()
    main_flow_compare.compare_main_flow("002361")
    assert mock_dark_flow.call_count == 2
