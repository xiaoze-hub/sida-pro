# -*- coding: utf-8 -*-
"""主力资金聚合(明盘权威 + 暗盘 L2 主线 / 腾讯回退) 单测。

重点验证:
  1) 官方口径: 主力净额 = 明盘 + 暗盘, 散户 = -主力
  2) 置信度标注: 明盘=official, 暗盘=l2_thsdk(主线) / L1_approx(回退), 二者永不混用
  3) 缺失处理: 任一侧失败互不拖垮, main_net 只在双侧可得时输出(不编造)
  4) 代码归一: 002361 / sz002361 / 非法输入

2026-09-02 起暗盘**主线改为 L2 逐笔**(`dark_flow_l2`), 腾讯逐笔降为回退路径
(confidence 随之从 l2_thsdk 降级为 L1_approx)。因此下面标注 `_dark_flow` 真实路径的
用例统一挂 `no_l2` fixture 关掉主线, 才能稳定测到**回退分支**;
主线分支本身由 `test_decision_enhance.py` 覆盖。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import dark_pool_flow as dpf  # noqa: E402


# ---------------------------------------------------------------------------
# 代码归一
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("002361", "sz002361"),
        ("sz002361", "sz002361"),
        ("600519", "sh600519"),
        ("sh600519", "sh600519"),
        ("688981", "sh688981"),
        ("300750", "sz300750"),
        # 9 开头 → 沪(B股/指数), 与 dark_flow._tencent_code 同口径
        ("999999", "sh999999"),
    ],
)
def test_tencent_code_normalizes(raw, expected):
    assert dpf._tencent_code(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "abc123", "12345", "8xxxxx", "1234567"])
def test_tencent_code_rejects_invalid(raw):
    assert dpf._tencent_code(raw) is None


def test_compute_pool_flow_rejects_invalid_symbol():
    assert dpf.compute_pool_flow("not-a-code") is None


# ---------------------------------------------------------------------------
# 聚合口径
# ---------------------------------------------------------------------------


def _patch_both(monkeypatch, ming_net=-3_407_363.0, dark_net=1_052_800.0):
    """复刻官方示例的量级: 明盘为负、暗盘为正(杭州热电式反号)。"""
    monkeypatch.setattr(
        dpf, "_ming_flow",
        lambda code: {"net": ming_net, "buy": 1.0, "sell": 1.0 - ming_net,
                      "count": 253, "source": dpf.MING_SOURCE,
                      "confidence": dpf.MING_CONFIDENCE, "skipped": 0},
    )
    monkeypatch.setattr(
        dpf, "_dark_flow",
        lambda sym: {"net": dark_net, "inflow": 2_000_000.0, "outflow": 2_000_000.0 - dark_net,
                     "groups": 12, "source": dpf.DARK_SOURCE,
                     "confidence": dpf.DARK_CONFIDENCE,
                     "dark_flow_status": "ok", "tick_count": 4830},
    )


def test_main_net_equals_ming_plus_dark(monkeypatch):
    """官方公式: 主力 = 明盘 + 暗盘。"""
    _patch_both(monkeypatch, ming_net=-3_407_363.0, dark_net=1_052_800.0)
    r = dpf.compute_pool_flow("002361")
    assert r["coverage"] == "full"
    assert r["main_net"] == pytest.approx(-3_407_363.0 + 1_052_800.0)
    # 散户 = -主力(全市场净流入恒为 0)
    assert r["retail_net"] == pytest.approx(-(r["main_net"]))
    assert r["retail_net"] + r["main_net"] == 0


def test_confidence_labels_never_mixed(monkeypatch):
    """明盘=official / 暗盘=L1_approx, 置信度标签必须随字段走。"""
    _patch_both(monkeypatch)
    r = dpf.compute_pool_flow("002361")
    assert r["ming"]["confidence"] == "official"
    assert r["dark"]["confidence"] == "L1_approx"
    assert r["ming"]["source"] == "thsdk_big_order"
    assert r["dark"]["source"] == "dark_flow_split_v4"


def test_ming_failure_does_not_kill_dark(monkeypatch):
    """明盘挂掉时暗盘照常返回, coverage=dark_only, main_net 必须为 None。"""
    monkeypatch.setattr(dpf, "_ming_flow", lambda code: None)
    monkeypatch.setattr(
        dpf, "_dark_flow",
        lambda sym: {"net": 1.0, "inflow": 2.0, "outflow": 1.0, "groups": 1,
                     "source": dpf.DARK_SOURCE, "confidence": dpf.DARK_CONFIDENCE,
                     "dark_flow_status": "ok", "tick_count": 100},
    )
    r = dpf.compute_pool_flow("002361")
    assert r["coverage"] == "dark_only"
    assert r["ming"] is None and r["dark"]["net"] == 1.0
    assert r["main_net"] is None and r["retail_net"] is None


def test_dark_failure_does_not_kill_ming(monkeypatch):
    """暗盘挂掉时明盘照常返回, coverage=ming_only, main_net 必须为 None。"""
    monkeypatch.setattr(dpf, "_dark_flow", lambda sym: None)
    monkeypatch.setattr(
        dpf, "_ming_flow",
        lambda code: {"net": -100.0, "buy": 0.0, "sell": 100.0, "count": 10,
                      "source": dpf.MING_SOURCE, "confidence": dpf.MING_CONFIDENCE,
                      "skipped": 0},
    )
    r = dpf.compute_pool_flow("002361")
    assert r["coverage"] == "ming_only"
    assert r["ming"]["net"] == -100.0 and r["dark"] is None
    assert r["main_net"] is None


def test_both_fail_gives_none(monkeypatch):
    monkeypatch.setattr(dpf, "_ming_flow", lambda code: None)
    monkeypatch.setattr(dpf, "_dark_flow", lambda sym: None)
    r = dpf.compute_pool_flow("002361")
    assert r["coverage"] == "none"
    assert r["main_net"] is None


# ---------------------------------------------------------------------------
# 真实实现路径(打桩 IO 层, 走真函数)
# ---------------------------------------------------------------------------


def test_ming_flow_uses_big_order_source(monkeypatch):
    """_ming_flow 必须走 thsdk_big_order 数据源 + 权威汇总函数。"""
    from src.core import dark_l2, dark_split

    ticks = [
        {"amt": 500_000, "d": "B", "side": "active"},
        {"amt": 300_000, "d": "S", "side": "passive"},
    ]
    monkeypatch.setattr(dark_l2, "fetch_l2_ticks", lambda code, src: ticks)
    r = dpf._ming_flow("sz002361")
    assert r["net"] == 200_000
    assert r["count"] == 2
    assert r["source"] == "thsdk_big_order"
    assert r["confidence"] == "official"


def test_ming_flow_returns_none_when_no_ticks(monkeypatch):
    from src.core import dark_l2

    monkeypatch.setattr(dark_l2, "fetch_l2_ticks", lambda code, src: [])
    assert dpf._ming_flow("sz002361") is None


def test_ming_flow_swallows_source_failure(monkeypatch):
    from src.core import dark_l2

    def _boom(code, src):
        raise RuntimeError("thsdk 熔断中")

    monkeypatch.setattr(dark_l2, "fetch_l2_ticks", _boom)
    assert dpf._ming_flow("sz002361") is None


@pytest.fixture
def no_l2(monkeypatch):
    """关掉**融合 + L2** 两条主线, 让 `_dark_flow` 走腾讯逐笔**回退分支**。

    否则本机 thsdk 可用(游客账户也能出数)会命中主线, 测不到回退路径。
    """
    monkeypatch.setattr("src.core.dark_flow_fusion.compute_dark_fusion", lambda s: None)
    monkeypatch.setattr("src.core.dark_flow_l2.compute_dark_flow_l2",
                        lambda s, source="thsdk": None)


def test_dark_flow_reads_split_order(no_l2, monkeypatch):
    """暗盘取自 compute_dark_flow 的 split_order 字段(拆单识别 v4)。"""
    import src.core.dark_flow as df

    fake = {"split_order": {"buy_amt": 2_000_000.0, "sell_amt": 1_500_000.0,
                            "net": 500_000.0, "groups": [{"a": 1}, {"b": 2}]},
            "data_status": "ok", "tick_count": 4830}
    monkeypatch.setattr(df, "compute_dark_flow", lambda sym: fake)
    r = dpf._dark_flow("002361")
    assert r["net"] == 500_000.0
    assert r["inflow"] == 2_000_000.0 and r["outflow"] == 1_500_000.0
    assert r["groups"] == 2
    assert r["confidence"] == "L1_approx"


def test_dark_flow_returns_none_without_split(no_l2, monkeypatch):
    import src.core.dark_flow as df

    monkeypatch.setattr(df, "compute_dark_flow", lambda sym: {"data_status": "ok"})
    assert dpf._dark_flow("002361") is None


def test_dark_flow_returns_none_when_compute_fails(no_l2, monkeypatch):
    import src.core.dark_flow as df

    monkeypatch.setattr(df, "compute_dark_flow", lambda sym: None)
    assert dpf._dark_flow("002361") is None


def test_dark_flow_swallows_exception(no_l2, monkeypatch):
    import src.core.dark_flow as df

    def _boom(sym):
        raise RuntimeError("腾讯逐笔限流")

    monkeypatch.setattr(df, "compute_dark_flow", _boom)
    assert dpf._dark_flow("002361") is None
