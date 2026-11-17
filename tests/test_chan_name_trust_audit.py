# -*- coding: utf-8 -*-
"""名称识别「覆盖不足」时不许把「没认出名字」当成「正文没点别的票」（2026-11-17 五轮）。

上一轮已承认的结论是：本地名单只覆盖**全名**（句首 2 字简称才认），TQ 兜底只探
**段首** 3~4 字，所以「我想看看贵州茅台缠论怎么样」这类**段中**名字会漏检。但
旧 `_name_target` 只在 `coverage == "none"` 时 fail-closed，`coverage == "tq"` 且段首
探测无结果时直接返回「没点名」→ 上层信任页面锚点，把浦发(600000)的结构答给茅台。

2026-11-18 六轮审计后，同图归属不再走名称识别，而走 `src/core/chan_followup.py` 的
**封闭追问语法**（整串匹配才放行）。本文件钉住的安全不变量不变，只是"怎么放行"换了：
段中名字/简称一律不在协议内 → 同样丢弃快照。

安全不变量（本文件钉住的）：
  **不能从「没检测到名称」推出「没有其他目标」**。本地索引过期/部分/只含别名时同样
  不能声称"全集已确认"。只有
    ① 整句能被「只指当前页面」的追问语法**完全覆盖**（正向可证），或
    ② 明确单一代码/@提及，且正文里没有第二个标的
  才允许使用当前页面的快照；其余一律丢掉快照（宁可误拒，不可误答），并在说明里如实
  标注识别范围、给出改用代码/@提及的回退。

另外钉住「不能用代码存在就忽略第二标的」：一句里两个代码、代码 + 另一个名字，都属
判不出唯一目标。

只替换数据源边界（K 线 / 名称索引 / TQ 检索 / 模型）；摘要、分型、鉴权、落库全真实。
"""
from __future__ import annotations

import pytest

from tests.test_chan_ai_turn_context import (  # noqa: F401 - fixtures re-exported
    _body,
    _conv,
    _conv_row,
    _digest,
    _login,
    _snap,
    accounts,
    client,
    feed,
    holder,
)

#: 测试用「全市场名单」：真实的全名/代码对应关系（只覆盖全名，正是要复现的覆盖边界）。
_NAMES = [
    ("600519", "贵州茅台"),
    ("601318", "中国平安"),
    ("600000", "浦发银行"),
    ("000001", "平安银行"),
    ("000761", "本钢板材"),
    ("600111", "北方稀土"),
    ("300750", "宁德时代"),
]
_TQ = {name: [(code, name)] for code, name in _NAMES}


def _response(client, token, conv_id, payload):
    return client.post(f"/api/chat/conversations/{conv_id}/messages",
                       headers={"Authorization": f"Bearer {token}"}, json=payload)


def _short(digest: str) -> str:
    return digest[:8] + "…"


def _assert_dropped(holder, digest, *must_say):
    """快照整份丢弃：工具拿不到摘要、system 里说清为什么、并给出回退方式。"""
    out = holder.outputs[-1]
    assert digest not in out and _short(digest) not in out, "他票结构不许出现在工具结果里"
    assert "缺少图表快照摘要" in out
    system = holder.systems[-1]
    for phrase in must_say:
        assert phrase in system, f"说明里应含「{phrase}」:\n{system[-600:]}"
    assert "6 位代码" in system or "@" in system, "必须给出「用代码/@提及」的回退"


@pytest.fixture()
def tq_probe_only(monkeypatch):
    """本地全名索引**不可用**；TQ 名称检索可用 —— 于是走有界段首探测（段中名字照漏）。"""
    import src.core.symbol_guard as sg
    import src.harness.quick_answer as qa

    monkeypatch.setattr(sg, "_local_name_pairs", lambda: None)
    monkeypatch.setattr("src.core.stock_names.cached_pairs", lambda market="": None)
    monkeypatch.setattr(sg, "_tq_lookup", lambda word: list(_TQ.get(word, [])))
    monkeypatch.setattr(sg, "_match_cached", lambda word: list(_TQ.get(word, [])))
    monkeypatch.setattr("src.web.api.chat.correction_note", lambda t: None)
    sg.reset_local_index()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(qa, "answer", _none)
    yield sg
    sg.reset_local_index()


@pytest.fixture()
def local_full_names(monkeypatch):
    """本地全名索引可用（钉死的名单，只有全名）+ TQ 钉成查不到 → 零外网。"""
    import src.core.symbol_guard as sg
    import src.harness.quick_answer as qa

    monkeypatch.setattr(sg, "_local_name_pairs", lambda: list(_NAMES))
    monkeypatch.setattr(sg, "_tq_lookup", lambda word: [])
    monkeypatch.setattr(sg, "_match_cached", lambda word: [])
    monkeypatch.setattr("src.web.api.chat.correction_note", lambda t: None)
    sg.reset_local_index()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(qa, "answer", _none)
    yield sg
    sg.reset_local_index()


@pytest.fixture()
def local_partial(monkeypatch):
    """本地索引**不完整**（只有 2 字别名，没有全名）—— 不能当成全集。"""
    import src.core.symbol_guard as sg
    import src.harness.quick_answer as qa

    monkeypatch.setattr(sg, "_local_name_pairs", lambda: [("600000", "浦发")])
    monkeypatch.setattr(sg, "_tq_lookup", lambda word: [])
    monkeypatch.setattr(sg, "_match_cached", lambda word: [])
    monkeypatch.setattr("src.web.api.chat.correction_note", lambda t: None)
    sg.reset_local_index()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(qa, "answer", _none)
    yield sg
    sg.reset_local_index()


@pytest.fixture()
def channels_down(monkeypatch):
    """本地索引与 TQ 都不可用（None=查不了，不是「没这个名字」）。"""
    import src.core.symbol_guard as sg
    import src.harness.quick_answer as qa

    monkeypatch.setattr(sg, "_local_name_pairs", lambda: None)
    monkeypatch.setattr("src.core.stock_names.cached_pairs", lambda market="": None)
    monkeypatch.setattr(sg, "_tq_lookup", lambda word: None)
    monkeypatch.setattr(sg, "_match_cached", lambda word: [])
    monkeypatch.setattr("src.web.api.chat.correction_note", lambda t: None)
    sg.reset_local_index()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(qa, "answer", _none)
    yield sg
    sg.reset_local_index()


# ── ① 覆盖不足：段中名字漏检 ≠ 没点名 ────────────────────────────────────

def test_mid_sentence_name_with_tq_probe_only_drops_snapshot(
        client, accounts, feed, holder, tq_probe_only):
    """「我想看看贵州茅台缠论怎么样」：TQ 只探段首 → 探不到茅台的**段中**名字。

    探不到不等于没提 —— 页面停在 600000 也必须丢快照，并把「识别范围有限」说明白。
    """
    sg = tq_probe_only
    assert sg.local_name_index() is None
    assert sg.whole_text_name_codes("我想看看贵州茅台缠论怎么样") == ([], "tq"), \
        "本地索引不可用时应如实报告 tq 覆盖（且结果为空）"

    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("我想看看贵州茅台缠论怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    _assert_dropped(holder, digest, "无法确认")
    assert (_conv_row(conv_id).stock_symbol or "") == "600000", "被拒的快照不许改会话标的"


def test_partial_local_index_is_not_full_coverage(
        client, accounts, feed, holder, local_partial):
    """本地索引只有 2 字别名（过期/部分）时同样不许当全集：漏检的茅台必须挡住快照。"""
    sg = local_partial
    assert sg.whole_text_name_codes("帮我看看贵州茅台缠论怎么样") == ([], "local"), \
        "coverage=local 只说明「哪个通道答的」，不是「全集已确认」"

    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("帮我看看贵州茅台缠论怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    _assert_dropped(holder, digest, "无法确认")


def test_full_name_index_still_blocks_unrecognized_alias(
        client, accounts, feed, holder, local_full_names):
    """名单只有全名：句中的「茅台」认不出 → 不许拿 600000 的图顶替。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("帮我看看茅台缠论怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    _assert_dropped(holder, digest, "无法确认")


def test_channels_down_drops_unverified_text(client, accounts, feed, holder, channels_down):
    """本地索引与 TQ 都不可用 + 正文有核对不了的词 → 丢快照（既有口径不许退化）。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("我想看看贵州茅台缠论怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    _assert_dropped(holder, digest, "名称索引")


# ── ② 不能用「有代码」吞掉第二个标的 ──────────────────────────────────────

def test_two_codes_in_one_sentence_is_ambiguous(
        client, accounts, feed, holder, local_full_names):
    """一句里两个不同代码 → 判不出唯一目标，快照丢弃（代码不是免检通行证）。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("600000 和 600519 哪个缠论结构更好", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    _assert_dropped(holder, digest, "不止一个标的")


def test_code_plus_other_name_is_ambiguous(
        client, accounts, feed, holder, local_full_names):
    """代码 + 另一只票的名字（「600000 ... 还是贵州茅台」）→ 同样判不出唯一目标。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("600000 浦发银行还是贵州茅台 缠论更好", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    _assert_dropped(holder, digest, "多只不同标的")


def test_code_plus_unverified_second_name_drops(
        client, accounts, feed, holder, tq_probe_only):
    """代码 + 段中漏检的第二个名字：仍然不许把 600000 的图当成这轮那张。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("600000 我想看看贵州茅台缠论怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    _assert_dropped(holder, digest, "无法确认")


# ── ③ 合法路径不许被误杀 ──────────────────────────────────────────────────

def test_named_target_same_as_page_serves(client, accounts, feed, holder, local_full_names):
    """正文全名就是页面这只（浦发银行）→ 照常出结构。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("浦发银行缠论怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】600000" in out and _short(digest) in out


def test_explicit_code_with_plain_followup_serves(
        client, accounts, feed, holder, local_full_names):
    """明确单一代码 + 纯追问（「600000 三买点确认了吗」）→ 照常出结构。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("600000 三买点确认了吗", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】600000" in out and _short(digest) in out


def test_generic_followup_serves_with_all_channels_down(
        client, accounts, feed, holder, channels_down):
    """「这只股票缠论结构怎么样」明确只指当前页面 —— 索引/检索全挂也不许误杀。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("这只股票缠论结构怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】600000" in out and _short(digest) in out


def test_mention_target_with_generic_followup_serves(
        client, accounts, feed, holder, channels_down):
    """@提及 是显式目标：正文只写泛指追问 → 照常出结构。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    body = _body("这只票的缠论结构怎么样", _snap(digest, symbol="600000"))
    body["mentions"] = [{"type": "stock", "value": "600000", "label": "浦发银行"}]
    r = _response(client, token, conv_id, body)
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】600000" in out and _short(digest) in out


# ── ④ 同码个股/指数：代码相同 ≠ 同一张图 ─────────────────────────────────

def test_same_code_index_served_only_with_page_declaration(
        client, accounts, feed, holder, channels_down):
    """页面显式声明 index 时 000001 的指数快照可用；没声明就丢（既有口径）。"""
    digest_index = _digest("000001", instrument="index")
    token = _login(client, "a")
    conv_id = _conv(client, token, "000001", "SZ")

    body = _body("缠论结构怎么样", _snap(digest_index, symbol="000001", instrument="index"))
    body["stock_symbol"] = "000001"
    body["stock_instrument"] = "index"
    r = _response(client, token, conv_id, body)
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】000001" in out and " index " in out
    assert _short(digest_index) in out


def test_same_code_index_without_declaration_is_dropped(
        client, accounts, feed, holder, channels_down):
    """会话绑 000001(平安银行) + 上证指数 000001 快照、无类型声明 → 丢。"""
    digest_index = _digest("000001", instrument="index")
    token = _login(client, "a")
    conv_id = _conv(client, token, "000001", "SZ")

    r = _response(client, token, conv_id,
                  _body("缠论结构怎么样",
                        _snap(digest_index, symbol="000001", instrument="index")))
    assert r.status_code == 200, r.text
    assert "无法确认本轮请求标的的类型" in holder.systems[-1]
    assert "缺少图表快照摘要" in holder.outputs[-1]
    assert " index " not in holder.outputs[-1]
