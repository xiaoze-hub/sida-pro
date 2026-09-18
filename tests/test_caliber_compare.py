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


def test_thsdk_l2_money_fields_are_wan_normalized_to_yuan(monkeypatch):
    """TQ `get_more_info` 的**金额**字段是万元 → 本端点必须归一到元, 笔数不换算。

    2026-09-18 由口径留痕发现: 本页曾把 zjl_hb 当"元"直接展示, 002361 实测原值 3992.67
    (实为 3992.67 万元 = 3.99e7 元) → 明盘那列小 1e4 倍, 漂移页三源可比性也随之失真。
    依据: mainflow_tri 文件头("TQ Zjl_HB(万元)") + 前端 L2Tab/DecisionPioneerCard 的万元口径。
    """
    monkeypatch.setattr(
        "src.core.decision_pioneer.fetch_tq_l2",
        lambda symbol, market="CN": {
            "zjl_hb": 3992.67,
            "zjl": 11485.27,
            "cancel_buy": 12.5,
            "cancel_sell": -3.25,
            "l2_tick_num": 8888,
            "l2_order_num": 777,
        },
    )
    out = cc._thsdk_l2("002361")
    got = {f["label"]: f for f in out["fields"]}
    assert got["主力净流入"]["value"] == pytest.approx(3992.67 * 1e4)
    assert got["主力净额（含主动买卖口径）"]["value"] == pytest.approx(11485.27 * 1e4)
    assert got["撤买额"]["value"] == pytest.approx(12.5 * 1e4)
    assert got["撤卖额"]["value"] == pytest.approx(-3.25 * 1e4)
    # 笔数类字段不能被 ×1e4
    assert got["L2 逐笔笔数"]["value"] == 8888
    assert got["L2 逐笔笔数"]["unit"] == "笔"
    assert got["L2 委托笔数"]["value"] == 777
    assert out["unit"] == "元", "归一后单位应声明为元"


def test_thsdk_l2_missing_money_stays_none(monkeypatch):
    """缺失保持 None(不补 0), 不能因为 ×1e4 变成 0。"""
    monkeypatch.setattr(
        "src.core.decision_pioneer.fetch_tq_l2",
        lambda symbol, market="CN": {"zjl_hb": None, "zjl": None, "l2_tick_num": 10, "l2_order_num": None},
    )
    out = cc._thsdk_l2("002361")
    got = {f["label"]: f for f in out["fields"]}
    assert got["主力净流入"]["value"] is None
    assert got["主力净额（含主动买卖口径）"]["value"] is None


def test_compare_exposes_pair_diffs_and_conclusion(monkeypatch):
    """P2-1: 对照响应必须带**成对差异 + 归因**, 且不合成单一数字。"""
    from src.web.api import caliber_compare as cc

    # 三源各给一个"主力净流入": 暗盘 2.75x、东财 1.01x —— 都该落在预期带(ok)
    fake_sources = [
        {"key": "thsdk_l2", "fields": [{"label": "主力净流入", "value": 1.0e7}]},
        {"key": "tencent_dark", "fields": [{"label": "主力净额（≥20万）", "value": 2.75e7}]},
        {"key": "eastmoney_flow", "fields": [{"label": "主力净流入", "value": 1.01e7}]},
    ]
    diffs = cc._pair_diffs(fake_sources)
    assert len(diffs) == 3
    assert all(d["level"] == "ok" for d in diffs), diffs
    assert all(d["note"] for d in diffs)          # 每对都要有"为什么差"的解释

    concl = cc._diff_conclusion(fake_sources)
    assert concl["level"] == "ok" and concl["hint"]
    # 不合成单一数字: 返回里不许出现"校准后/权威值"这类键
    for k in ("calibrated", "consensus", "authoritative"):
        assert k not in concl


def test_compare_pair_diffs_unknown_when_source_missing():
    """缺源 → unknown(不比较、不补 0)。"""
    from src.web.api import caliber_compare as cc

    srcs = [{"key": "thsdk_l2", "fields": [{"label": "主力净流入", "value": 1.0e7}]}]
    diffs = cc._pair_diffs(srcs)
    assert all(d["level"] == "unknown" for d in diffs)
    assert cc._diff_conclusion(srcs)["level"] == "unknown"
