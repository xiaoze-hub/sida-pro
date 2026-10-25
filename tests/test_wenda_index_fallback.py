"""问小达无数据时的指数兜底（2026-10-25 生产事故回归）。

现象：用户问「上证指数现在多少点」，模型这次调了 `tdx_wenda`（不是
`get_thsdk_market_data_index`），问小达返回空 → 直接答「查不到上证点位」。
同一时刻首页指数条正常 —— **不是没数据，是选错入口且这条入口没兜底**。

纪律：只有问题里真的在问**指数点位**时才兜底；市场级排行问题（今日主力净流入前10）
不许被指数行情顶掉，宁可如实说"问小达无结果"。
"""

from __future__ import annotations

import pytest

from src.agents.chat import registry as reg


@pytest.fixture()
def fake_tencent(monkeypatch):
    def _fake(code: str):
        return [{"name": "上证指数", "price": 3690.48, "change_pct": 0.89}] if code == "000001" else []

    import src.agents.chat.tools_thsdk as ths
    monkeypatch.setattr(ths, "_tencent_index_fallback", _fake, raising=True)
    return _fake


def test_指数全称会兜底(fake_tencent):
    out = reg._wenda_index_fallback("上证指数现在多少点")
    assert "腾讯实时兜底" in out
    assert "3690.48" in out
    assert "+0.89%" in out


def test_指数短名会兜底(fake_tencent):
    assert "3690.48" in reg._wenda_index_fallback("上证现在多少点")


def test_指数代码会兜底(fake_tencent):
    assert "3690.48" in reg._wenda_index_fallback("000001 现在多少点")


def test_市场级排行问题不兜底(fake_tencent):
    """今日主力净流入前10 —— 该由问小达自己答，不能被指数行情顶掉。"""
    assert reg._wenda_index_fallback("今日主力净流入前10的A股") == ""


def test_无指数关键词返回空(fake_tencent):
    assert reg._wenda_index_fallback("今天涨停家数最多的概念板块") == ""


def test_腾讯也拿不到时返回空_让原逻辑如实说无结果(monkeypatch):
    import src.agents.chat.tools_thsdk as ths
    monkeypatch.setattr(ths, "_tencent_index_fallback", lambda code: [], raising=True)
    assert reg._wenda_index_fallback("上证指数现在多少点") == ""


def test_兜底自身异常不抛_不能把工具搞挂(monkeypatch):
    import src.agents.chat.tools_thsdk as ths

    def boom(code):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(ths, "_tencent_index_fallback", boom, raising=True)
    assert reg._wenda_index_fallback("上证指数现在多少点") == ""


def test_工具数不变():
    """兜底是内部逻辑，不许新增工具（注册表 76 是钉子）。"""
    assert len(reg.CHAT_TOOL_REGISTRY) == 76
