"""intent_explain 解释层单测: 纯函数 + LLM 层分离(monkeypatch AIClient.chat)。"""

from __future__ import annotations

import json

import pytest

from src.core import intent_explain as ie


# ---------------------------------------------------------------- fixtures

def _dark(**overrides) -> dict:
    """构造一份完整的 dark dict(与 compute_dark_flow 返回结构同构)。"""
    base = {
        "main_net": 5967e4,          # 主力净额 5967 万(元)
        "big_net": 5967e4,           # 超大单 +5967 万
        "mid_net": -8433e4,          # 大单 -8433 万
        "small_net": -3000e4,        # 散户 -3000 万
        "main_intensity": 42.0,      # 参与度 42%
        "main_buy_ratio": 61.5,      # 买占比 61.5%
        "signal": "超大单大幅净流入, 主力吸筹迹象, 但大单净流出需警惕对倒",
        "phase": "持续吸筹(5日+今日均净流入)",
        "inner_outer": {
            "buy_pct": 58.3,
            "sell_pct": 41.7,
            "position": "20日分位高位",
        },
        "divergence": {
            "type": "托盘出货",
            "detail": "超大单拉抬+大单出逃+价格滞涨, 警惕诱多",
        },
        "price_divergence": None,
        "rhythm": {"pattern": "早吸尾抛", "detail": "早盘吸筹尾盘抛压, 拉高出货特征"},
        "split_order": {"detail": "疑似拆单: 主力伪装中小单"},
        "absorb_zones": [{"price": 12.30, "big_net": 1200e4, "small_net": -400e4}],
        "distribute_zones": [{"price": 13.10, "big_net": -900e4, "small_net": 350e4}],
        "data_status": "ok",
        "note": "v11",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- build_prompt

def test_build_prompt_returns_str_tuple_and_contains_signal_and_features():
    system, user = ie.build_explain_prompt(_dark())
    assert isinstance(system, str) and system
    assert isinstance(user, str) and user
    # 规则结论文本必须进 prompt
    assert "超大单大幅净流入" in user
    # 特征数字必须进 prompt(万为单位)
    assert "主力净额=+5967万" in user
    assert "超大单净额=+5967万" in user
    assert "大单净额=-8433万" in user
    assert "主力参与度=42.0%" in user
    assert "主力买占比=61.5%" in user
    # 内外盘/背离/节奏/承接位
    assert "内盘买占比=58.3%" in user
    assert "托盘出货" in user
    assert "早吸尾抛" in user
    assert "12.3" in user  # 承接位价位


def test_build_prompt_handles_missing_fields_gracefully():
    system, user = ie.build_explain_prompt({"main_net": 100e4, "data_status": "ok"})
    assert isinstance(user, str)
    assert "主力净额=+100万" in user
    assert "无" in user  # 缺失字段降级为"无"


def test_build_prompt_retail_net_fallback():
    system, user = ie.build_explain_prompt(
        {"retail_net": -500e4, "data_status": "ok"}
    )
    assert "散户净额=-500万" in user


def test_build_prompt_non_dict_returns_empty_features():
    system, user = ie.build_explain_prompt(None)
    assert isinstance(system, str) and system
    assert isinstance(user, str)
    assert "无" in user


# ---------------------------------------------------------------- parse_reply

def test_parse_valid_json():
    reply = json.dumps({"direction": "吸筹", "confidence": "高",
                        "why": "超大单+5967万但大单-8433万, 托盘出货嫌疑"})
    out = ie.parse_explain_reply(reply)
    assert out == {"direction": "吸筹", "confidence": "高",
                   "why": "超大单+5967万但大单-8433万, 托盘出货嫌疑"}


def test_parse_json_with_markdown_fence():
    reply = '```json\n{"direction": "洗盘", "confidence": "中", "why": "压盘吸筹"}\n```'
    out = ie.parse_explain_reply(reply)
    assert out == {"direction": "洗盘", "confidence": "中", "why": "压盘吸筹"}


def test_parse_invalid_json_returns_none():
    assert ie.parse_explain_reply("not json at all") is None
    assert ie.parse_explain_reply('{"direction": "吸筹"') is None  # 残缺


def test_parse_empty_and_none_return_none():
    assert ie.parse_explain_reply(None) is None
    assert ie.parse_explain_reply("") is None
    assert ie.parse_explain_reply("   ") is None


def test_parse_invalid_direction_or_confidence_returns_none():
    assert ie.parse_explain_reply(
        json.dumps({"direction": "买入", "confidence": "高", "why": "x"})
    ) is None
    assert ie.parse_explain_reply(
        json.dumps({"direction": "吸筹", "confidence": "极高", "why": "x"})
    ) is None


def test_parse_missing_why_returns_none():
    assert ie.parse_explain_reply(
        json.dumps({"direction": "吸筹", "confidence": "高"})
    ) is None


def test_parse_why_truncated_to_80_chars():
    long_why = "x" * 120
    out = ie.parse_explain_reply(
        json.dumps({"direction": "吸筹", "confidence": "高", "why": long_why})
    )
    assert out is not None
    assert len(out["why"]) == ie._WHY_MAX_LEN + 1  # 80 + "…"
    assert out["why"].endswith("…")


def test_parse_non_dict_json_returns_none():
    assert ie.parse_explain_reply('["吸筹"]') is None


# ---------------------------------------------------------------- explain_main_intent

def test_explain_main_intent_dark_none_returns_none(monkeypatch):
    called = []
    monkeypatch.setattr(ie, "_llm_chat", _async_fake_chat(called))
    assert ie.explain_main_intent(None) is None
    assert called == []


def test_explain_main_intent_insufficient_returns_none_no_llm(monkeypatch):
    called = []
    monkeypatch.setattr(ie, "_llm_chat", _async_fake_chat(called))
    assert ie.explain_main_intent(_dark(data_status="insufficient")) is None
    assert called == []


def test_explain_main_intent_ok_calls_llm_and_returns_parsed(monkeypatch):
    async def fake_chat(system, user, db=None):
        return json.dumps({"direction": "派发", "confidence": "中",
                           "why": "超大单+5967万但大单-8433万"})

    monkeypatch.setattr(ie, "_llm_chat", fake_chat)
    out = ie.explain_main_intent(_dark())
    assert out == {"direction": "派发", "confidence": "中",
                   "why": "超大单+5967万但大单-8433万"}


def test_explain_main_intent_llm_exception_returns_none(monkeypatch):
    async def fake_chat(system, user, db=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(ie, "_llm_chat", fake_chat)
    assert ie.explain_main_intent(_dark()) is None


def test_explain_main_intent_llm_invalid_json_returns_none(monkeypatch):
    async def fake_chat(system, user, db=None):
        return "garbage"

    monkeypatch.setattr(ie, "_llm_chat", fake_chat)
    assert ie.explain_main_intent(_dark()) is None


def test_explain_main_intent_llm_timeout_returns_none(monkeypatch):
    import asyncio

    async def fake_chat(system, user, db=None):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(ie, "_llm_chat", fake_chat)
    assert ie.explain_main_intent(_dark()) is None


def _async_fake_chat(called: list):
    async def _inner(system, user, db=None):
        called.append(1)
        return json.dumps({"direction": "中性", "confidence": "低", "why": "无"})

    return _inner
