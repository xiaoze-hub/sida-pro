# -*- coding: utf-8 -*-
"""策略 META 与参数覆盖测试(v0.5.78, 借鉴 tick-stock-panel 的"一文件 + META")。

钉住三件事: ①参数表从**求值器真正读的阈值键**派生, 且 meta 只改描述不改语义;
②覆盖只认声明过的键、写回它原本所在的位置、不污染原 cfg(否则一次请求会影响下一次);
③覆盖确实改变判定结果(不是摆设)。
"""
from __future__ import annotations

import copy
import math

import pytest
import yaml

from src.core.strategy_library import (
    PARAM_DEFS,
    _evaluate_strategy,
    effective_config,
    strategy_params,
)
from src.web.api.strategies import STRATEGIES_FILE, _load_strategies


def _cfg(**filter_kwargs):
    return {"display_name": "测试", "filter": dict(filter_kwargs), "ranking_factors": {}}


def test_params_derive_from_declared_thresholds_only():
    ps = strategy_params(_cfg(price_min=3, change_pct_max=9.5, nonsense_min=1))
    assert [p["key"] for p in ps] == ["price_min", "change_pct_max"]   # 未声明键不进表单
    assert ps[0]["label"] == "股价下限" and ps[0]["unit"] == "元"
    assert ps[0]["value"] == 3 and ps[0]["step"] == PARAM_DEFS["price_min"]["step"]
    assert strategy_params({"filter": {}}) == []


def test_top_level_thresholds_are_picked_up_too():
    """dual_low 把 pe/pb/市值写在策略顶层(不在 filter 里), 也必须能编辑。"""
    cfg = {"filter": {"price_min": 3}, "pe_ttm_max": 15, "pb_max": 2.0, "market_cap_min": 50}
    assert {p["key"] for p in strategy_params(cfg)} == {"price_min", "pe_ttm_max", "pb_max", "market_cap_min"}


def test_meta_params_override_description_only():
    cfg = _cfg(change_pct_max=9.5)
    cfg["meta"] = {"params": {"change_pct_max": {"label": "追高上限", "help": "涨过头不追", "min": 3}}}
    p = strategy_params(cfg)[0]
    assert p["label"] == "追高上限" and p["help"] == "涨过头不追" and p["min"] == 3
    assert p["value"] == 9.5                      # 值仍以策略正文为准, meta 不许夹带新值
    # meta 里写了不存在的键 → 不会凭空长出参数
    cfg["meta"]["params"]["made_up_min"] = {"label": "幻觉"}
    assert [p["key"] for p in strategy_params(cfg)] == ["change_pct_max"]


def test_override_writes_back_to_its_own_place_and_keeps_original_intact():
    cfg = {"filter": {"price_min": 3}, "pe_ttm_max": 15}
    before = copy.deepcopy(cfg)
    out = effective_config(cfg, {"price_min": 8.0, "pe_ttm_max": 40})
    assert out["filter"]["price_min"] == 8.0 and out["pe_ttm_max"] == 40
    assert cfg == before                          # 原 cfg 不被污染(模块级 YAML 会被复用)


@pytest.mark.parametrize("bad", [{"ranking_factors": {"low_pe": 1}}, {"price_min": "abc"},
                                 {"price_min": float("nan")}, {"price_min": float("inf")},
                                 {"not_a_param": 5}, {}])
def test_bad_overrides_are_dropped_not_applied(bad):
    cfg = _cfg(price_min=3)
    out = effective_config(cfg, bad)
    assert out["filter"]["price_min"] == 3
    assert out.get("ranking_factors", {}) == {}


def test_override_actually_changes_the_verdict():
    """覆盖不是装饰: 同一份行情, 放宽追高上限后必须从"不通过"变"通过"。"""
    cfg = _cfg(price_min=3, price_max=220, change_pct_min=1.0, change_pct_max=9.5)
    q = {"current_price": 12.0, "change_pct": 9.6, "volume_ratio": 2.0, "turnover_rate": 3.0}
    assert _evaluate_strategy(cfg, q, "t", "000001", "CN")["passed"] is False
    loose = effective_config(cfg, {"change_pct_max": 11.0})
    assert _evaluate_strategy(loose, q, "t", "000001", "CN")["passed"] is True


def test_real_yaml_every_strategy_is_editable_and_meta_is_consistent():
    data = _load_strategies()
    assert STRATEGIES_FILE.exists()
    for key, cfg in data.items():
        if key == "data_completeness" or not isinstance(cfg, dict):
            continue
        ps = strategy_params(cfg)
        assert ps, f"{key} 没有任何可编辑参数"
        for p in ps:
            assert p["key"] in PARAM_DEFS
            assert p["value"] is not None and not (isinstance(p["value"], float) and math.isnan(p["value"]))
    # 真实文件里的 meta 覆盖生效(证明机制通了, 不只是单测自嗨)
    ch = {p["key"]: p for p in strategy_params(data["capital_heat"])}
    assert ch["change_pct_max"]["label"] == "追高上限" and ch["change_pct_max"].get("help")


def test_yaml_still_parses_after_meta_block():
    """meta 块插在 source 之后, 别让 YAML 缩进把后面的策略吞进去。"""
    raw = yaml.safe_load(STRATEGIES_FILE.read_text(encoding="utf-8"))
    assert "volume_breakout" in raw and raw["capital_heat"]["meta"]["params"]["change_pct_max"]["label"] == "追高上限"
