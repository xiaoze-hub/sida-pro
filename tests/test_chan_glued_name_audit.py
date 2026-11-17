# -*- coding: utf-8 -*-
"""无分隔符股票名（「贵州茅台缠论怎么样」）与「本/其/各/另」开头的真名（「本钢板材」）。

2026-11-17 四轮审计的三个残留缺口（全部走真实路由/工具/SQLite）：

① **紧贴提问词的名字检测不到**：`bare_name_codes` 只在非汉字处切段，所以
   「贵州茅台缠论结构怎么样」整段 10 字 > 4 被跳过 → 页面停在 600000 时仍把浦发
   的快照当成本轮结构答出去，用户真正想问的茅台被顶替。
   现在：本地全名索引（东财/通达信全市场列表，非快照自述）做整段窗口确认，
   命中**别的票** ⇒ name_conflict 丢快照；命中**就是这只** ⇒ 照常出结构。

② **泛指前缀误分真名**：`is_generic_reference` 只看首字（本/其/各/另/…），
   「本钢板材」被判成「泛指眼前这只」→ 同上一条的后果。现在：前缀必须接泛指名词
   （股票/个股/银行/医药…）才算泛指。

③ **「找不到」不等于「没提别票」**：本地索引不可用且 TQ 也查不了时，不能把
   「没检测到」当成「正文没有别的标的」——明确 fail-closed（丢快照），而
   「这只股票缠论怎么样」这类纯泛指追问必须照常可用。

只替换数据源边界（K 线 / 名称索引 / TQ 检索）与模型；摘要、分型、鉴权、落库全真实。
"""
from __future__ import annotations

import pytest

from tests.test_chan_ai_turn_context import (  # noqa: F401 - fixtures re-exported
    _body,
    _conv,
    _digest,
    _login,
    _snap,
    accounts,
    client,
    feed,
    holder,
)

#: 测试用「本地全名索引」：取自行情库的真实全名/代码对应关系。
_NAMES = [
    ("600519", "贵州茅台"),
    ("601318", "中国平安"),
    ("600000", "浦发银行"),
    ("000001", "平安银行"),
    ("000761", "本钢板材"),
    ("600111", "北方稀土"),
    ("300750", "宁德时代"),
]


def _response(client, token, conv_id, payload):
    return client.post(f"/api/chat/conversations/{conv_id}/messages",
                       headers={"Authorization": f"Bearer {token}"}, json=payload)


@pytest.fixture()
def names_offline(monkeypatch):
    """本地索引可用（钉死的全市场名单）+ TQ 检索「查不到」+ 快答关掉 = 零外网。"""
    import src.core.symbol_guard as sg
    import src.harness.quick_answer as qa

    monkeypatch.setattr(sg, "_local_name_pairs", lambda: list(_NAMES))
    monkeypatch.setattr(sg, "_match_cached", lambda word: [])
    monkeypatch.setattr(sg, "_tq_lookup", lambda word: [])
    monkeypatch.setattr("src.web.api.chat.correction_note", lambda t: None)
    sg.reset_local_index()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(qa, "answer", _none)
    yield sg
    sg.reset_local_index()


@pytest.fixture()
def no_name_source(monkeypatch):
    """本地索引不可用 + TQ 检索不可用（None=查不了，不是「没有这个名字」）。"""
    import src.core.symbol_guard as sg
    import src.harness.quick_answer as qa

    monkeypatch.setattr(sg, "_local_name_pairs", lambda: None)
    monkeypatch.setattr(sg, "_match_cached", lambda word: [])
    monkeypatch.setattr(sg, "_tq_lookup", lambda word: None)
    monkeypatch.setattr("src.web.api.chat.correction_note", lambda t: None)
    sg.reset_local_index()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(qa, "answer", _none)
    yield sg
    sg.reset_local_index()


# ── ② 泛指判定不许误分真名 ────────────────────────────────────────────────

def test_generic_prefix_does_not_swallow_real_names():
    """「本钢板材」「北方稀土」是真名；「本股票」「其他银行」才是泛指。"""
    import src.core.symbol_guard as sg

    for generic in ("这家银行", "该股", "本股票", "整个医药", "其他银行", "另一只",
                    "这只票", "银行", "医药"):
        assert sg.is_generic_reference(generic), generic
    for specific in ("本钢板材", "北方稀土", "中国平安", "宁德时代", "浦发银行"):
        assert not sg.is_generic_reference(specific), specific


# ── ① 无分隔符名字：贴紧提问词也要认出来 ──────────────────────────────────

def test_glued_name_detected_without_separator(names_offline):
    sg = names_offline
    assert sg.glued_name_codes("贵州茅台缠论结构怎么样") == [("贵州茅台", "600519", "贵州茅台")]
    assert sg.glued_name_codes("本钢板材缠论怎么样") == [("本钢板材", "000761", "本钢板材")]
    assert sg.glued_name_codes("帮我看看宁德时代缠论怎么样") == [
        ("宁德时代", "300750", "宁德时代")]
    assert sg.glued_name_codes("缠论结构怎么样") == [], "泛问不许产生名字"
    assert sg.glued_name_codes("这只股票缠论怎么样") == [], "泛指不许当名字"
    assert sg.glued_name_codes("600519 缠论结构怎么样") == [], "已给代码时不抢标的"


def test_glued_alias_at_sentence_head(names_offline):
    """「茅台缠论怎么样」这种简称只认**句首**且唯一指向一只全名的情形。"""
    sg = names_offline
    assert sg.glued_name_codes("茅台缠论怎么样") == [("茅台", "600519", "贵州茅台")]


def test_glued_name_against_anchor_snapshot_dropped(
        client, accounts, feed, holder, names_offline):
    """页面/会话停在 600000，正文「贵州茅台缠论结构怎么样」→ 快照必须整份丢弃。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("贵州茅台缠论结构怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    out = holder.outputs[-1]
    print("\nSYSTEM:", system[-400:])
    assert "600519" in system and "自相矛盾" in system
    assert "缺少图表快照摘要" in out
    assert digest not in out and digest[:8] + "…" not in out


def test_glued_name_same_as_target_keeps_snapshot(
        client, accounts, feed, holder, names_offline):
    """正文点名的就是这只票（「浦发银行缠论怎么样」）→ 快照照常出结构。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("浦发银行缠论怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】600000" in out
    assert digest[:8] + "…" in out


def test_generic_prefixed_real_name_dropped_against_anchor(
        client, accounts, feed, holder, names_offline):
    """「本钢板材」是真名（不是「本<名词>」泛指）→ 与 600000 冲突，丢。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("本钢板材缠论怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    assert "000761" in system and "自相矛盾" in system
    assert "缺少图表快照摘要" in holder.outputs[-1]


# ── ③ 索引不可用：明确保守，不把「找不到」当「没提别票」──────────────────

def test_no_index_no_tq_drops_snapshot_for_unverified_text(
        client, accounts, feed, holder, no_name_source):
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("贵州茅台缠论怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    print("\nSYSTEM:", system[-400:])
    assert "名称索引" in system
    assert "缺少图表快照摘要" in holder.outputs[-1]


def test_no_index_generic_followup_still_serves(
        client, accounts, feed, holder, no_name_source):
    """「这只股票缠论怎么样」是纯泛指追问：索引缺失也不许误杀。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("这只股票缠论怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】600000" in out
    assert digest[:8] + "…" in out
