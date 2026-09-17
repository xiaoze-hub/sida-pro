# -*- coding: utf-8 -*-
"""口径对照(2026-09-18)回归 —— 三源并排、消歧不合并、无数据不补 0。

钉五件事:
  ① 三个源(明盘 L2 / 暗盘逐笔 / 东财四档)都在响应里, 各自带口径说明与单位;
  ② 任一源取不到 → `available=false` + 原因文案, **不补 0、不编记录**;
  ③ 源函数抛异常 → 同样降级为"取数异常"而不是 500(对照页不能因一个源挂掉整页崩);
  ④ `available_count` 与真实可用数一致;
  ⑤ 非法代码 → 400(不静默返回空结果)。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.web.api import caliber_compare as cc


def _by_key(res: dict, key: str) -> dict:
    for s in res["sources"]:
        if s["key"] == key:
            return s
    raise AssertionError(f"{key} 不在响应里: {[s['key'] for s in res['sources']]}")


def test_three_sources_present_with_caliber_and_unit():
    res = cc.build_caliber_compare("002361")
    assert res["symbol"] == "002361"
    assert res["market"] == "CN"
    assert res["as_of"]
    assert [s["key"] for s in res["sources"]] == ["thsdk_l2", "tencent_dark", "eastmoney_flow"]
    for s in res["sources"]:
        assert s["name"] and s["caliber"] and s["unit"] == "元"
        assert isinstance(s["fields"], list)
        assert isinstance(s["available"], bool)


def test_available_count_matches_sources():
    res = cc.build_caliber_compare("002361")
    assert res["available_count"] == sum(1 for s in res["sources"] if s["available"])
    assert 0 <= res["available_count"] <= 3


def test_differences_are_static_and_explained():
    res = cc.build_caliber_compare("002361")
    assert len(res["differences"]) >= 3
    for d in res["differences"]:
        assert d["topic"] and d["detail"]
    joined = " ".join(d["topic"] for d in res["differences"])
    assert "定义" in joined and "正确用法" in joined


def test_missing_source_reports_reason_and_no_fake_zero(monkeypatch):
    """源返回空 → available=false + 有原因; **不产生任何 value=0 的假字段**。"""
    monkeypatch.setattr(cc, "_thsdk_l2", lambda symbol: cc._wrap("thsdk_l2", fields=None, note="无数据测试"))
    res = cc.build_caliber_compare("002361")
    l2 = _by_key(res, "thsdk_l2")
    assert l2["available"] is False
    assert l2["fields"] == []
    assert "无数据测试" in l2["note"]


def test_source_exception_degrades_not_500(monkeypatch):
    """某个源抛异常 → 该源降级为"取数异常", 其余源照常返回(整页不崩)。"""
    def _boom(symbol: str):
        raise RuntimeError("boom")

    monkeypatch.setattr(cc, "_tencent_dark", _boom)
    res = cc.build_caliber_compare("002361")
    dark = _by_key(res, "tencent_dark")
    assert dark["available"] is False
    assert "取数异常" in dark["note"]
    # 其它两个源仍在(不因为一个源挂掉就整体失败)
    assert len(res["sources"]) == 3


def test_values_are_raw_yuan_not_pre_rounded(monkeypatch):
    """金额必须是**元**(前端统一折 亿/万), 后端不做二次单位换算。"""
    monkeypatch.setattr(
        cc,
        "_eastmoney_flow",
        lambda symbol: cc._wrap(
            "eastmoney_flow",
            fields=[{"label": "主力净流入", "value": 123_456_789}],
            note="",
        ),
    )
    res = cc.build_caliber_compare("002361")
    em = _by_key(res, "eastmoney_flow")
    assert em["fields"][0]["value"] == 123_456_789


def test_suspect_flag_is_passed_through(monkeypatch):
    """源自标"数据可疑"(如逐笔重复计数) 必须透传成 note, 不能吞掉。"""
    monkeypatch.setattr(
        cc,
        "_tencent_dark",
        lambda symbol: cc._wrap(
            "tencent_dark",
            fields=[{"label": "全量主动净额", "value": 1}],
            note="⚠️ 该源自标「数据可疑」",
        ),
    )
    res = cc.build_caliber_compare("002361")
    assert "数据可疑" in _by_key(res, "tencent_dark")["note"]


@pytest.mark.parametrize("bad", ["", "abc", "12345", "1234567", "00236A"])
def test_invalid_symbol_rejected(bad):
    with pytest.raises(HTTPException) as ei:
        cc.build_caliber_compare(bad)
    assert ei.value.status_code == 400
