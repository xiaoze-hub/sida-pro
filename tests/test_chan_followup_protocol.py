# -*- coding: utf-8 -*-
"""同图追问的**封闭语法**（2026-11-18 六轮审计重构）。

历史背景（独立审计的 4 条"必须拒绝"反例）::

    页面/会话 = 600519（贵州茅台）、消息带 600519 的图表快照;
    「万科怎么样」「万科A怎么样」「万科 缠论怎么样」「万科缠论结构怎么样」
    旧实现全部把贵州茅台的结构答了出去 ——
      · 「万」在 `_FOLLOWUP_CHARS`（单字虚词白名单）里被当虚词吞掉;
      · 「科」只剩 1 个字，被「残留 ≥ 2 字才算可疑」丢掉;
      ⇒ 整句被判成"纯追问"，于是页面上的旧图被当成用户问的那张。

结论：**词袋/黑名单不可能证明"店里没有未覆盖的简称"**，再补一条别名只是把已知的那只
补上。现在改成 `src/core/chan_followup.py` 的封闭正向语法：

  ① 整句是封闭模板（泛指主语 + 追问核心，字面量精确匹配）；
  ② 唯一 6 位代码/@提及 + 同一条追问语法；
  ③ 写在句首、一字不差的**已验证全名**（唯一命中）+ 同一条追问语法；
其余一律拒绝同图（允许误拒、不许误答）。协议只声明"字面识别范围"，不声称自然语言
绝无歧义。

本文件钉住：4 条历史反例、几条常见错误写法、合法路径不被误杀、拒绝时的说明（含
"不可读同图"和改用代码的回退）、零网络（判定不再打 TQ）、以及"普通不带快照的聊天
不受这条限制"。

只替换数据源边界（K 线 / 名称名单 / 模型）；摘要、分型、鉴权、落库全真实。
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

#: 真实的全名/代码对应（600519=贵州茅台、000002=万科**A**）。
#: 关键：名单里只有「万科A」，所以「万科」在任何窗口下都不该被认出来 —— 它必须靠
#: **协议拒绝**（而不是靠别名），否则「京东方」「三六零」这类新写法又会漏。
_NAMES = [
    ("600519", "贵州茅台"),
    ("600000", "浦发银行"),
    ("000002", "万科A"),
    ("601318", "中国平安"),
    ("600036", "招商银行"),
    ("000761", "本钢板材"),
    ("300750", "宁德时代"),
]


def _short(digest: str) -> str:
    return digest[:8] + "…"


@pytest.fixture()
def name_list(monkeypatch):
    """本地可信全名名单（协议唯一的名字通道）；TQ 检索钉成"查不到"，零外网。"""
    import src.core.chan_followup as cf
    import src.core.symbol_guard as sg
    import src.harness.quick_answer as qa

    monkeypatch.setattr(sg, "_local_name_pairs", lambda: list(_NAMES))
    monkeypatch.setattr(sg, "_tq_lookup", lambda word: [])
    monkeypatch.setattr(sg, "_match_cached", lambda word: [])
    monkeypatch.setattr("src.web.api.chat.correction_note", lambda t: None)
    monkeypatch.setattr(cf, "_NAME_VIEW", {"raw": None, "view": None})
    sg.reset_local_index()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(qa, "answer", _none)
    yield sg
    sg.reset_local_index()
    monkeypatch.setattr(cf, "_NAME_VIEW", {"raw": None, "view": None})


@pytest.fixture()
def index_view(name_list):
    """直接给单测用的归一化名字索引（与线上同一口径：NFKC + 小写）。"""
    import src.core.chan_followup as cf

    return cf._name_index()


def _parse(text: str, name_list=None):
    import src.core.chan_followup as cf

    return cf.parse(text)


def _ask(client, holder, text: str, symbol: str = "600519",
         *, conv_symbol: str | None = None, market: str = "SH"):
    digest = _digest(symbol)
    token = _login(client, "a")
    conv_id = _conv(client, token, conv_symbol if conv_symbol is not None else symbol, market)
    r = client.post(f"/api/chat/conversations/{conv_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json=_body(text, _snap(digest, symbol=symbol)))
    assert r.status_code == 200, r.text
    return digest, conv_id


def _dropped(holder, digest: str) -> str:
    out = holder.outputs[-1]
    assert digest not in out and _short(digest) not in out, f"他票结构泄漏:\n{out[:400]}"
    assert "缺少图表快照摘要" in out, f"快照未被丢弃:\n{out[:400]}"
    return out


def _served(holder, digest: str, symbol: str = "600519") -> str:
    out = holder.outputs[-1]
    assert f"【缠论结构·只读观测】{symbol}" in out, f"合法路径被误杀:\n{out[:400]}"
    assert _short(digest) in out
    return out


# ── ① 四条历史反例：必须全部拒绝 ────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "万科怎么样",
    "万科A怎么样",
    "万科 缠论怎么样",
    "万科缠论结构怎么样",
])
def test_wanke_variants_drop_other_stock_snapshot(
        client, accounts, feed, holder, name_list, text):
    """页面/会话 = 600519 + 600519 的快照，正文问的是 000002 万科 → 一律丢快照。

    「万科A怎么样」走的是**句首已验证全名**这条（名单里有「万科A」→ 000002 ≠ 600519），
    其余三条不在协议内 → 同样拒绝。两种结局都是"不可读同图"，不会出现"答成茅台"。
    """
    digest, _conv_id = _ask(client, holder, text, "600519")
    _dropped(holder, digest)


@pytest.mark.parametrize("text", [
    "600000和万科",                       # 代码 + 裸简称
    "查询600000是否强于万科",              # 代码 + 比较句
    "贵州茅台和浦发银行",                  # 两只全名
    "600519和万科",
])
def test_audit_counterexamples_are_rejected(
        client, accounts, feed, holder, name_list, text):
    """审计点名的三种写法都不许借当前快照作答。"""
    digest, _conv_id = _ask(client, holder, text, "600519")
    _dropped(holder, digest)


@pytest.mark.parametrize("text", [
    "我想看看万科怎么样",        # 名字在句中（协议只认句首）
    "帮我看看万科缠论怎么样",
    "万科的缠论结构怎么样",      # 全名缺失「A」
    "万科，缠论结构怎么样",
    "万科怎么样？600519 呢",     # 混入代码 + 裸简称
])
def test_alias_forms_outside_protocol_are_rejected(
        client, accounts, feed, holder, name_list, text):
    """简称/别名的各种写法都不在协议内 —— 精确拒绝，不靠别名表。"""
    digest, _conv_id = _ask(client, holder, text, "600519")
    _dropped(holder, digest)


# ── ② 合法路径：封闭模板 / 唯一代码 / 句首已验证全名 ─────────────────────────

@pytest.mark.parametrize("text", [
    "缠论结构怎么样",
    "这张图的缠论结构怎么样",
    "这只股票的缠论结构怎么样",
    "这只票的缠论结构怎么样",
    "这家银行的缠论结构怎么样",
    "请问现在这只股票的缠论结构怎么样呢？",
    "这只股票后面怎么操作",
    "现在能上车吗",
    "三买点确认了吗",
])
def test_generic_templates_keep_snapshot(client, accounts, feed, holder, name_list, text):
    """完全泛指的模板追问：照常使用当前页面的快照（不许把正常追问误杀）。"""
    digest, _conv_id = _ask(client, holder, text, "600519")
    _served(holder, digest, "600519")


@pytest.mark.parametrize("text", [
    "600519 缠论结构怎么样",
    "600519缠论结构怎么看",
    "600519",
    "缠论结构怎么样 600519",
    "600519 三买点确认了吗",
])
def test_explicit_code_with_short_question_serves(client, accounts, feed, holder, name_list, text):
    """唯一 6 位代码 + 合规追问（或只有代码）→ 照常出结构。"""
    digest, _conv_id = _ask(client, holder, text, "600519")
    _served(holder, digest, "600519")


@pytest.mark.parametrize("text", [
    "贵州茅台缠论怎么样",
    "贵州茅台缠论结构怎么样",
    "贵州茅台的缠论结构怎么样",
])
def test_verified_full_name_at_sentence_head_serves(client, accounts, feed, holder, name_list, text):
    """句首、一字不差的已验证全名 + 合规追问 → 与快照同一只，照常出结构。"""
    digest, _conv_id = _ask(client, holder, text, "600519")
    _served(holder, digest, "600519")


def test_other_full_name_drops_snapshot(client, accounts, feed, holder, name_list):
    """句首全名指向另一只（万科A=000002）→ 与当前 600519 冲突，丢快照。"""
    digest, _conv_id = _ask(client, holder, "万科A缠论结构怎么样", "600519")
    _dropped(holder, digest)
    system = holder.systems[-1]
    assert "000002" in system and "自相矛盾" in system


def test_mention_target_with_generic_followup_serves(client, accounts, feed, holder, name_list):
    """@提及 是显式目标；正文只写泛问 → 照常出结构。"""
    digest = _digest("600519")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600519", "SH")
    body = _body("这只票的缠论结构怎么样", _snap(digest, symbol="600519"))
    body["mentions"] = [{"type": "stock", "value": "600519", "label": "贵州茅台"}]
    r = client.post(f"/api/chat/conversations/{conv_id}/messages",
                    headers={"Authorization": f"Bearer {token}"}, json=body)
    assert r.status_code == 200, r.text
    _served(holder, digest, "600519")


def test_beijing_exchange_code_is_still_explicit(client, accounts, feed, holder, name_list):
    """北交所代码（83xxxx）也是合法显式代码：不能被判成"协议外表述"。"""
    digest, _conv_id = _ask(client, holder, "830799 缠论结构怎么样", "830799", market="BJ")
    _served(holder, digest, "830799")


# ── ③ 拒绝说明：不可读同图 + 回退方式 + 不影响普通聊天 ────────────────────────

def test_rejection_note_is_explicit_and_actionable(client, accounts, feed, holder, name_list):
    digest, conv_id = _ask(client, holder, "万科怎么样", "600519")
    _dropped(holder, digest)
    system = holder.systems[-1]
    assert "无法唯一确定" in system
    assert "不可读同图" in system, "不能说成「这张图没有结构」，要说明是读不到同图"
    assert "封闭追问语法" in system
    assert "6 位代码" in system and "@" in system, "必须给出改用代码/@提及的回退"
    assert "普通不带图表快照的聊天不受这条限制" in system
    assert "不要写成" in system and "这张图没有结构" in system, \
        "要明确禁止把「读不到同图」写成「这张图没有结构」"
    # 被拒的这条消息不许改会话标的
    assert (_conv_row(conv_id).stock_symbol or "") == "600519"


def test_plain_chat_without_snapshot_is_unaffected(client, accounts, feed, holder, name_list):
    """同一句在**不带快照**的普通聊天里照旧：没有协议说明，也没有快照机制介入。"""
    digest = _digest("600519")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600519", "SH")
    r = client.post(f"/api/chat/conversations/{conv_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json=_body("万科怎么样"))
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    assert "不可读同图" not in system
    assert "封闭追问语法" not in system
    assert _short(digest) not in system


def test_next_turn_after_rejection_still_serves(client, accounts, feed, holder, name_list):
    """拒绝是**单轮**的：下一轮换成合规问法，同一张图照常可用。"""
    digest, conv_id = _ask(client, holder, "万科怎么样", "600519")
    _dropped(holder, digest)
    token = _login(client, "a")
    r = client.post(f"/api/chat/conversations/{conv_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json=_body("这只股票的缠论结构怎么样",
                               _snap(digest, symbol="600519")))
    assert r.status_code == 200, r.text
    _served(holder, digest, "600519")


# ── ④ 判定本身：零网络、整串匹配、不靠字符集剥词 ───────────────────────────

def test_parse_makes_no_network_call(monkeypatch, index_view):
    """判定不许打 TQ：`_tq_lookup` 一旦被调用就炸。"""
    import src.core.symbol_guard as sg

    def _boom(*_a, **_k):  # pragma: no cover - 被调用即为失败
        raise AssertionError("同图追问判定不得发起网络检索")

    monkeypatch.setattr(sg, "_tq_lookup", _boom)
    monkeypatch.setattr(sg, "_match_cached", _boom)
    for text in ("万科怎么样", "缠论结构怎么样", "600519 缠论结构怎么样",
                 "贵州茅台缠论怎么样", "600000和万科", "查询600519是否强于万科"):
        _parse(text)


def test_normalize_is_closed(index_view):
    """归一化只做 NFKC + 空白 + 封闭标点表 + 小写，不删汉字。"""
    import src.core.chan_followup as cf

    assert cf.normalize("　６００５１９ ，缠论结构怎么样？ ") == "600519缠论结构怎么样"
    assert cf.normalize("贵州茅台\n缠论\t结构") == "贵州茅台缠论结构"
    assert cf.normalize("万科A") == "万科a"


@pytest.mark.parametrize("text,kind", [
    ("缠论结构怎么样", "generic"),
    ("600519 缠论结构怎么样", "code"),
    ("贵州茅台缠论怎么样", "name"),
    ("万科A怎么样", "name"),
    ("万科怎么样", "reject"),
    ("600000和万科", "reject"),
    ("查询600519是否强于万科", "reject"),
    ("贵州茅台和浦发银行", "reject"),
    ("我想看看贵州茅台缠论怎么样", "reject"),
    ("600519和600000", "reject"),
    ("", "generic"),
])
def test_parse_decision_table(index_view, text, kind):
    assert _parse(text).kind == kind, _parse(text)


def test_closed_lists_contain_no_security_name(index_view):
    """钉子：封闭词表里不许出现任何已验证证券全名。

    词表里每一条都是整串字面量，所以"名字=某条核心"这类风险只可能来自"把某个股票名
    抄进词表"。一旦有人这么加，这条会红 —— 因为那等于让「万科怎么样」匹配上模板。
    """
    import src.core.chan_followup as cf

    names = set(index_view or {})
    tokens = list(cf.CORES) + list(cf.POLITE) + list(cf.TIME_WORDS) + list(cf._PRONOUN_SUBJECTS)
    offenders = [t for t in tokens if t and cf.normalize(t) in names]
    assert not offenders, f"封闭词表里混入了证券名: {offenders}"

    # 反向：任何一条核心都不许包含 2 字及以上的证券名片段（避免把名字拼进模板）
    for token in cf.CORES:
        norm = cf.normalize(token)
        for name in names:
            if len(name) >= 2 and name in norm:
                raise AssertionError(f"核心「{token}」里含证券名「{name}」")
