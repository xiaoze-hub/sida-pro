# -*- coding: utf-8 -*-
"""2026-11-17 三轮审计缺口回归（真实路由 / 工具 / SQLite）。

② **名字/表述对不上时锚点误信旧快照**：`resolve_turn_target` 只在
   `problem == "ambiguous_name"` 时丢弃目标，其它问题码直接 `return explicit` ——
   于是「图为 600000（页面锚点/会话绑定）+ 本轮正文写了别的名字（解析不出）」时，
   600000 的快照仍被当成这一轮的可信图交给模型。现在：正文不满足**封闭追问语法**
   （`src/core/chan_followup.py`，2026-11-18 六轮审计）⇒ 锚点/绑定也不能替用户决定
   本轮标的，快照整份丢弃；而「这家银行」「缠论结构怎么样」这类**泛指当前这只**的
   说法仍在协议内，不许误杀锚点。

③ **同代码个股/指数混淆**：`ChatConversation` 只存 `stock_symbol`，target/recheck/tool
   三层都只比 6 位代码 —— 会话绑 000001（平安银行）时，随附的「上证指数 000001」快照
   （instrument=index，摘要合法）被接受并产出指数结构。现在：标的类型只认
   ①代码可证 ②本轮页面显式声明 ③服务端缓存的页面视图状态；都拿不到 ⇒ 无法证身份、
   同号场景明确丢弃；三层（消息入口 / 开流后 / 工具）都按类型再核一次。
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

from tests.test_chan_snapshot_audit_fixes import (  # noqa: F401 - 复用真实路由用例的夹具
    _resolved,
    _response,
    _short,
    no_network,
)


def _turn(text: str, snapshot: dict | None = None, *, anchor: str | None = None,
          instrument: str | None = None) -> dict:
    """一条消息：正文 + 本轮快照 + 页面锚点(代码/类型)。"""
    body = _body(text, snapshot)
    if anchor:
        body["stock_symbol"] = anchor
    if instrument:
        body["stock_instrument"] = instrument
    return body


# ── ② 名字解析失败时不许借锚点/绑定信旧快照 ────────────────────────────────

def test_anchor_snapshot_dropped_when_name_unresolved(
        client, accounts, feed, holder, no_network):
    """页面锚点 600000 + 正文「北方稀士」(核不出来) → 锚点不能替用户决定本轮标的。

    六轮审计后同图归属只认封闭语法；「北方稀士」既不是已验证全名、也不构成合规追问，
    所以整份快照丢弃（旧实现靠 ``resolve_mentions`` 的"相似建议"给出同一条结论，
    新实现不猜名字，结论相同但理由更弱：只声明"不在协议内"）。
    """
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, None, None)          # 会话无绑定, 只有页面锚点
    no_network["is_q"] = True
    no_network["code"] = None

    r = _response(client, token, conv_id,
                  _turn("北方稀士 缠论结构怎么样", _snap(digest, symbol="600000"),
                        anchor="600000"))
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    assert "无法唯一确定" in system and "不可读同图" in system
    assert "封闭追问语法" in system, "说明里要写清本轮只接受有限的追问语法"
    assert _short(digest) not in holder.outputs[-1]
    assert "缺少图表快照摘要" in holder.outputs[-1]


def test_bound_snapshot_dropped_when_name_unresolved(
        client, accounts, feed, holder, no_network):
    """会话绑定 600000 + 正文名字解析不出 → 绑定同样不许当作「本轮就是这个标的」。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")
    no_network["is_q"] = True
    no_network["code"] = None
    no_network["info"] = {"resolved": [], "suggestions": [],
                          "unknown": ["某某科技"]}

    r = _response(client, token, conv_id,
                  _body("某某科技 缠论结构怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    assert "没能解析成唯一代码" in holder.systems[-1]
    assert "缺少图表快照摘要" in holder.outputs[-1]
    assert (_conv_row(conv_id).stock_symbol or "") == "600000"


def test_multi_name_with_one_unmapped_fails_closed(
        client, accounts, feed, holder, no_network):
    """一句话两只票、其中一只查不到 → 判不出唯一目标(不许只按查到的那只算)。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")
    no_network["code"] = "600000"
    no_network["info"] = {"resolved": [("浦发银行", "600000", "浦发银行")],
                          "suggestions": [], "unknown": ["某某科技"]}

    r = _response(client, token, conv_id,
                  _body("浦发银行和某某科技 缠论结构怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    assert "没能解析成唯一代码" in holder.systems[-1]
    assert "缺少图表快照摘要" in holder.outputs[-1]


@pytest.fixture()
def real_names_no_network(monkeypatch):
    """保留真实的名字判定(后缀/泛指/候选), 只把 TQ 检索钉成"查不到", 零外网。"""
    import src.core.symbol_guard as sg
    import src.harness.quick_answer as qa

    monkeypatch.setattr(sg, "_match_cached", lambda word: [])
    monkeypatch.setattr(sg, "_tq_match", lambda word: [])
    monkeypatch.setattr("src.web.api.chat.correction_note", lambda t: None)

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(qa, "answer", _none)
    return sg


def test_generic_phrase_keeps_anchor_snapshot(
        client, accounts, feed, holder, real_names_no_network):
    """「这家银行」是泛指当前这只(页面锚点), 不是点名 → 不许误杀成"解析失败"。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("这家银行的缠论结构怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    assert "【缠论结构·只读观测】600000" in holder.outputs[-1]
    assert _short(digest) in holder.outputs[-1]


def test_specific_unknown_name_still_fails_closed(
        client, accounts, feed, holder, real_names_no_network):
    """真实解析器下「某某科技」(后缀词, 查不到) 仍要 fail-closed —— 不用 mock 也成立。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("某某科技 缠论结构怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    assert "没能解析成唯一代码" in holder.systems[-1]
    assert "缺少图表快照摘要" in holder.outputs[-1]


def test_plain_followup_without_any_name_still_serves(
        client, accounts, feed, holder, real_names_no_network):
    """完全没点名的一般追问(「这只股票的缠论怎么样」) 必须照常用锚点快照。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _body("这只股票的缠论结构怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    assert "【缠论结构·只读观测】600000" in holder.outputs[-1]
    assert _short(digest) in holder.outputs[-1]


def test_bare_segment_name_conflicts_with_anchor(
        client, accounts, feed, holder, no_network, monkeypatch):
    """「贵州茅台」(无后缀词, is_stock_question=False) 也要能确认出票并判冲突。

    只认**本地全名名单里一字不差**的名字（六轮审计后这是唯一的名字通道）；
    查不到的一律不在协议内（不猜、不误杀）。
    """
    import src.core.symbol_guard as sg

    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")
    no_network["is_q"] = False
    monkeypatch.setattr(sg, "_local_name_pairs",
                        lambda: [("600519", "贵州茅台"), ("600000", "浦发银行")])
    monkeypatch.setattr("src.core.chan_followup._NAME_VIEW", {"raw": None, "view": None})
    sg.reset_local_index()

    r = _response(client, token, conv_id,
                  _body("贵州茅台 缠论结构怎么样", _snap(digest, symbol="600000")))
    sg.reset_local_index()
    monkeypatch.setattr("src.core.chan_followup._NAME_VIEW", {"raw": None, "view": None})
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    assert "600519" in system and "自相矛盾" in system
    assert "缺少图表快照摘要" in holder.outputs[-1]


def test_non_ashare_name_does_not_reuse_anchor_snapshot(
        client, accounts, feed, holder, no_network):
    """正文写「创业板ETF」这类本链路不支持的标的 → 不许拿锚点 600000 的图顶替。

    六轮审计后不再靠 ``resolve_mentions`` 去解析它（那会把 159915 当"名字命中"），
    而是按封闭语法直接判"协议外、不可读同图" —— 结论仍是丢弃，理由不再声称
    "指向了另一只代码"（服务端根本没有核实过它）。
    """
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")
    no_network["code"] = None

    r = _response(client, token, conv_id,
                  _body("创业板ETF 缠论结构怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    assert "无法唯一确定" in system and "不可读同图" in system
    assert "缺少图表快照摘要" in holder.outputs[-1]


# ── ③ 同代码个股/指数: 缺可证身份就丢 ─────────────────────────────────────

def test_same_code_index_snapshot_in_stock_conversation_dropped(
        client, accounts, feed, holder, no_network):
    """会话绑 000001(平安银行) + 本轮随附「上证指数 000001」→ 同号不同标的, 丢。"""
    digest_index = _digest("000001", instrument="index")
    token = _login(client, "a")
    conv_id = _conv(client, token, "000001", "SZ")
    no_network["is_q"] = False

    r = _response(client, token, conv_id,
                  _body("缠论结构怎么样",
                        _snap(digest_index, symbol="000001", instrument="index")))
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    out = holder.outputs[-1]
    assert "无法确认本轮请求标的的类型" in system
    assert digest_index not in out and _short(digest_index) not in out
    assert "缺少图表快照摘要" in out
    assert (_conv_row(conv_id).stock_symbol or "") == "000001"


def test_same_code_stock_snapshot_served_when_page_declares_stock(
        client, accounts, feed, holder, no_network):
    """页面显式声明「个股」时, 000001 的个股快照照常出结构(不误杀深市个股)。"""
    digest = _digest("000001", instrument="stock")
    token = _login(client, "a")
    conv_id = _conv(client, token, "000001", "SZ")

    r = _response(client, token, conv_id,
                  _turn("缠论结构怎么样", _snap(digest, symbol="000001"),
                        anchor="000001", instrument="stock"))
    assert r.status_code == 200, r.text
    assert "【缠论结构·只读观测】000001" in holder.outputs[-1]
    assert _short(digest) in holder.outputs[-1]


def test_same_code_index_snapshot_served_when_page_declares_index(
        client, accounts, feed, holder, no_network):
    """用户真的在上证指数图页提问时, 000001(index) 的快照必须能用。"""
    digest_index = _digest("000001", instrument="index")
    token = _login(client, "a")
    conv_id = _conv(client, token, "000001", "SZ")

    r = _response(client, token, conv_id,
                  _turn("缠论结构怎么样",
                        _snap(digest_index, symbol="000001", instrument="index"),
                        anchor="000001", instrument="index"))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】000001" in out and " index " in out
    assert _short(digest_index) in out


def test_same_code_snapshot_dropped_when_declaration_mismatches(
        client, accounts, feed, holder, no_network):
    """页面声明个股、快照却是同号指数 → 类型对不上, 丢。"""
    digest_index = _digest("000001", instrument="index")
    token = _login(client, "a")
    conv_id = _conv(client, token, "000001", "SZ")

    r = _response(client, token, conv_id,
                  _turn("缠论结构怎么样",
                        _snap(digest_index, symbol="000001", instrument="index"),
                        anchor="000001", instrument="stock"))
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    assert "不是同一张图" in system
    assert "缺少图表快照摘要" in holder.outputs[-1]


def test_provable_code_ignores_page_declaration(
        client, accounts, feed, holder, no_network):
    """600000 的类型是**代码可证**的: 页面乱声明 index 既不改变结论也不误杀这张图。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    r = _response(client, token, conv_id,
                  _turn("缠论结构怎么样", _snap(digest, symbol="600000"),
                        anchor="600000", instrument="index"))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】600000" in out
    assert " stock " in out, "代码可证的个股不许被页面声明改成指数"
    assert _short(digest) in out


def test_same_code_snapshot_served_from_reported_view_state(
        client, accounts, feed, holder, no_network):
    """前端上报的页面视图状态(symbol+instrument)是独立证据: 能给 000001 定类型。"""
    from src.harness.viewstate import ViewState, save_view_state

    digest_index = _digest("000001", instrument="index")
    token = _login(client, "a")
    conv_id = _conv(client, token, "000001", "SZ")
    # 本工程 biz_cache 的 L1 一定写得进去(Redis 只是 L2, 测试里不可达时 L1 仍然有效,
    # set_json 会因此返回 False —— 返回值不代表 L1 写失败, 所以这里只核对读得回来)。
    save_view_state(accounts["a"], None,
                    ViewState(page="kline", symbol="000001",
                              market="CN", instrument="index"))
    from src.harness.viewstate import load_view_state

    assert (load_view_state(accounts["a"], None) or {}).get("instrument") == "index"

    r = _response(client, token, conv_id,
                  _body("缠论结构怎么样",
                        _snap(digest_index, symbol="000001", instrument="index")))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】000001" in out and " index " in out


def test_invalid_stock_instrument_is_422(client, accounts, feed):
    """页面类型字段是严格枚举: 别的值直接 422, 不落一个读不回来的声明。"""
    digest = _digest("000001", instrument="stock")
    token = _login(client, "a")
    conv_id = _conv(client, token, "000001", "SZ")
    r = _response(client, token, conv_id,
                  _turn("缠论结构怎么样", _snap(digest, symbol="000001"),
                        anchor="000001", instrument="board"))
    assert r.status_code == 422, r.text


# ── 名称判定的边界（不联网，直接钉住口径）────────────────────────────────

def test_symbol_guard_generic_vs_specific(monkeypatch):
    """「这家银行」是泛指、「浦发银行」是点名; 整段确认只认行情库真的命中的。"""
    import src.core.symbol_guard as sg

    for generic in ("这家银行", "整个医药", "该汽车", "银行", "医药"):
        assert sg.is_generic_reference(generic), generic
    for specific in ("浦发银行", "北方稀土", "某某科技"):
        assert not sg.is_generic_reference(specific), specific

    monkeypatch.setattr(
        sg, "_match_cached",
        lambda word: [("600519", "贵州茅台")] if word == "贵州茅台" else [],
    )
    assert sg.bare_name_codes("贵州茅台 缠论结构怎么样") == [("贵州茅台", "600519", "贵州茅台")]
    assert sg.bare_name_codes("缠论结构怎么样") == [], "泛问不许产生名字"
    assert sg.bare_name_codes("这家银行 缠论结构怎么样") == [], "泛指不许当名字"
    assert sg.bare_name_codes("600519 缠论结构怎么样") == [], "已给代码时不抢标的"
