# -*- coding: utf-8 -*-
"""口径对照契约标签回归(2026-09-29 A2 第一步)。

钉四件事(全部 **mock 三个数据源, 不触真实网络**):
  ① 三套口径齐全时: 字段/口径标签/单位/数据时间正确;
  ② 某源不可用 → 该源显式「无数据」且**绝不返回 0**;
  ③ 非法/空 symbol → 友好 400; 带后缀(600519.SH)归一为 6 位;
  ④ 口径标签 + direction_semantics 严格来自 `src/core/caliber.py` 契约(不自创)。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.core import caliber as C
from src.web.api import caliber_compare as cc

_ALL = ("_thsdk_l2", "_tencent_dark", "_eastmoney_flow")


def _src(key: str, label: str, value: float) -> dict:
    return cc._wrap(key, fields=[{"label": label, "value": value}], note="")


@pytest.fixture
def all_three(monkeypatch) -> dict:
    """三源全部可用(固定值, 不触网)。"""
    monkeypatch.setattr(cc, "_thsdk_l2", lambda s: _src("thsdk_l2", "主力净流入", 1.0e7))
    monkeypatch.setattr(cc, "_tencent_dark", lambda s: _src("tencent_dark", "主力净额（≥20万）", 2.75e7))
    monkeypatch.setattr(cc, "_eastmoney_flow", lambda s: _src("eastmoney_flow", "主力净流入", 1.01e7))
    return cc.build_caliber_compare("002361")


# ── ① 三套口径齐全: 字段/标签/单位/数据时间 ──────────────────────────────────


def test_three_calibers_present_with_labels_units_and_time(all_three):
    assert [s["key"] for s in all_three["sources"]] == ["thsdk_l2", "tencent_dark", "eastmoney_flow"]
    assert all_three["available_count"] == 3
    for s in all_three["sources"]:
        assert s["unit"] == "元"                       # 单位约定(AGENTS)
        assert s["caliber_tag"]["label"]               # 可见口径标签
        assert s["as_of"]                              # 数据时间
        assert s["fields"], f"{s['key']} 应有字段"
        for f in s["fields"]:
            assert f["label"] and f["value"] is not None

    by = {s["key"]: s for s in all_three["sources"]}
    # 三家“主力净流入”字段名各不相同 —— 显式映射, 不猜
    assert by["tencent_dark"]["fields"][0]["label"] == "主力净额（≥20万）"
    assert by["eastmoney_flow"]["fields"][0]["label"] == "主力净流入"
    assert by["thsdk_l2"]["fields"][0]["label"] == "主力净流入"
    # 逐笔那列用“元”且数值原样透传(后端不做二次换算)
    assert by["tencent_dark"]["fields"][0]["value"] == 2.75e7


def test_response_declares_caliber_contract(all_three):
    c = all_three["caliber_contract"]
    assert "tick" in c["rule"] and "方向" in c["rule"]
    assert c["contract"].endswith("caliber.py")
    assert set(c["sources"]) == {"tencent_dark", "eastmoney_flow", "thsdk_l2"}


# ── ④ 口径标签 + direction_semantics 严格来自契约 ────────────────────────────


def test_caliber_tags_and_direction_semantics_match_contract(all_three):
    valid = {"tick", "eastmoney4", "ths", "unknown"}
    by = {s["key"]: s for s in all_three["sources"]}

    # 用途映射(AGENTS.md 硬约束): 逐笔 tick / 东财四档 / TQ L2 ths
    assert by["tencent_dark"]["caliber_type"] == "tick"
    assert by["eastmoney_flow"]["caliber_type"] == "eastmoney4"
    assert by["thsdk_l2"]["caliber_type"] == "ths"

    for s in all_three["sources"]:
        assert s["caliber_type"] in valid
        assert s["caliber_tag"]["caliber"] == s["caliber_type"]
        # caliber_tag 是契约 CaliberTag.to_dict(): 四个 key 齐全
        assert set(s["caliber_tag"]) == {"caliber", "direction_semantics", "source", "label"}
        assert s["caliber_tag"]["direction_semantics"] == s["direction_semantics"]

    # direction_semantics 与契约常量**逐字**一致(不得自创)
    assert by["tencent_dark"]["direction_semantics"] == C.DIRECTION_TICK
    assert by["eastmoney_flow"]["direction_semantics"] == C.DIRECTION_EASTMONEY4
    assert by["thsdk_l2"]["direction_semantics"] == C.DIRECTION_THS

    # 仅 tick 允许方向判定; 其余一律禁止
    assert by["tencent_dark"]["directional_allowed"] is True
    assert by["eastmoney_flow"]["directional_allowed"] is False
    assert by["thsdk_l2"]["directional_allowed"] is False

    # 复用契约出口: tick 通过, 非 tick 抛 CaliberViolationError
    C.require_directional(cc.SOURCE_CALIBER["tencent_dark"], "测试")
    with pytest.raises(C.CaliberViolationError):
        C.require_directional(cc.SOURCE_CALIBER["eastmoney_flow"], "测试")


def test_direction_reconcile_uses_contract_when_all_sources_present(all_three):
    dr = all_three["direction_reconcile"]
    assert dr is not None
    assert dr["agree"] is True                    # 逐笔 +2.75e7 与东财 +1.01e7 同向
    assert dr["statement"]


# ── ② 某源不可用 → 显式「无数据」, 不返回 0 ──────────────────────────────────


def test_unavailable_source_reports_no_data_and_never_zero(monkeypatch):
    monkeypatch.setattr(cc, "_tencent_dark", lambda s: cc._wrap("tencent_dark", fields=None))
    monkeypatch.setattr(cc, "_thsdk_l2", lambda s: _src("thsdk_l2", "主力净流入", 1.0e7))
    monkeypatch.setattr(cc, "_eastmoney_flow", lambda s: _src("eastmoney_flow", "主力净流入", 1.01e7))

    res = cc.build_caliber_compare("002361")
    dark = next(s for s in res["sources"] if s["key"] == "tencent_dark")

    assert dark["available"] is False
    assert dark["fields"] == []                    # 不产生假字段
    assert "无数据" in dark["note"]                # 显式标注
    assert res["available_count"] == 2
    # 全响应里任何字段都不得用 0 冒充缺失
    assert all(f.get("value") != 0 for s in res["sources"] for f in s["fields"])


def test_unavailable_tick_source_has_no_reconcile(monkeypatch):
    monkeypatch.setattr(cc, "_tencent_dark", lambda s: cc._wrap("tencent_dark", fields=None))
    monkeypatch.setattr(cc, "_thsdk_l2", lambda s: _src("thsdk_l2", "主力净流入", 1.0e7))
    monkeypatch.setattr(cc, "_eastmoney_flow", lambda s: _src("eastmoney_flow", "主力净流入", 1.01e7))
    res = cc.build_caliber_compare("002361")
    assert res["direction_reconcile"] is None      # 逐笔缺数 → 不裁决(不猜)


# ── ③ 非法/空 symbol 友好报错; 后缀归一 ─────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", "abc", "12345", "1234567", "00236A", "600519.XX", "600519.SHX"])
def test_invalid_symbol_friendly_400(bad):
    with pytest.raises(HTTPException) as ei:
        cc.build_caliber_compare(bad)
    assert ei.value.status_code == 400
    assert "非法股票代码" in ei.value.detail


@pytest.mark.parametrize(
    "raw,expected",
    [("600519.SH", "600519"), ("000001.sz", "000001"), ("830799.BJ", "830799"), ("002361", "002361")],
)
def test_symbol_suffix_normalized(monkeypatch, raw, expected):
    for k in _ALL:
        monkeypatch.setattr(cc, k, lambda s, k=k: cc._wrap(k, fields=None))
    res = cc.build_caliber_compare(raw)
    assert res["symbol"] == expected


def test_query_endpoint_function_and_route_registered(monkeypatch):
    for k in _ALL:
        monkeypatch.setattr(cc, k, lambda s, k=k: cc._wrap(k, fields=None))
    out = cc.caliber_compare_query("600519.SH", None)   # 直接调端点函数(无 auth 依赖)
    assert out["symbol"] == "600519"
    assert len(out["sources"]) == 3

    paths = {(r.path, tuple(sorted(getattr(r, "methods", []) or []))) for r in cc.router.routes}
    assert ("", ("GET",)) in paths, "查询参数路由 GET /api/caliber-compare?symbol= 未注册"
