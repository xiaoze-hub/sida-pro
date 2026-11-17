# -*- coding: utf-8 -*-
"""封闭追问语法的两处边界 + 快照市场复核（2026-11-18 七轮审计）。

本轮复核主助手在六轮「封闭语法」之上仍然找到两处破口：

① **泛指主语的前缀回退**：`chan_followup._is_subject` 曾把判定交给
   `symbol_guard.is_generic_reference`，而后者对 `_GENERIC_PHRASES` 用的是
   ``startswith`` —— 于是「**其他万科**」「所有万科」「整个万科」都会被当成
   「泛指这张图」，页面停在 600519 时把茅台的结构答给了万科。主语必须是
   **有限的显式完整字面量**（这只股票 / 该股 / 这张图 …），不许从任何前缀推出
   「安全」，也不许再向 `symbol_guard` 借判定。

② **代码原样性**：`normalize()` 会做 NFKC（全角→半角）并删空白/标点，于是
   ``６００５１９``、``600 519``、``600-519``、``600.519`` 都会被「修复」成
   600519 并当成用户明写的标的。**只有原样的 ASCII 6 位连续数字**才允许被当作
   股票身份；全角数字、被空白/标点拆开的数字簇一律拒绝（允许误拒、不许误答）。
   前端 @ 提及的规范代码字段保持它自己那条独立校验，不经过本协议。

③ **市场**：快照 schema 的 market 恒为 CN，而本轮目标的 market 可能来自页面声明
   （`stock_market`）。代码相同但市场不是 CN/A 股交易所时，这份 CN 快照不能冒充
   用户眼前那张图 —— 消息入口与开流后复核都要 fail-closed。

本文件全部走**真实路由 + 真实工具 + 真实摘要/分型/落库**，只替换数据源边界
（K 线 / 名称名单 / 模型），并断言当前进程用的是 scratch SQLite（绝不碰生产库）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_chan_ai_turn_context import (  # noqa: F401 - fixtures re-exported
    _body,
    _conv,
    _conv_row,
    _digest,
    _login,
    _short,
    _snap,
    accounts,
    client,
    feed,
    holder,
)

from tests.test_chan_followup_protocol import (  # noqa: F401 - 复用名单夹具/路由助手
    _ask,
    _dropped,
    _served,
    name_list,
)


# ── 运行进程必须落在 scratch SQLite（不是生产库）────────────────────────────

def test_running_process_uses_scratch_sqlite_db():
    """本进程的 engine 必须是 conftest 造的临时 SQLite，不是 data/sida.db / PG。"""
    from src.db.session import engine

    url = engine.url
    assert url.get_backend_name() == "sqlite", f"测试进程连的不是 SQLite: {url}"
    db_file = Path(str(url.database or "")).resolve()
    data_dir = Path(os.environ["DATA_DIR"]).resolve()
    assert db_file.is_relative_to(data_dir), f"库文件不在 scratch DATA_DIR 里: {db_file}"
    assert data_dir != (Path.cwd() / "data").resolve(), "DATA_DIR 指向了仓库真实 data/"
    assert db_file.name.endswith(".db")


# ── ① 泛指主语：前缀回退必须消失 ────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "其他万科怎么样",
    "其它万科怎么样",
    "所有万科怎么样",
    "整个万科怎么样",
    "全部万科怎么样",
    "别的万科怎么样",
    "上述万科怎么样",
    "多家万科怎么样",
    "几只万科怎么样",
    "主板万科怎么样",
])
def test_prefixed_generic_subject_is_not_generic(text, name_list):
    """「其他/所有/整个…」+ 名字 不是泛指 —— 主语不许靠前缀推出安全。"""
    import src.core.chan_followup as cf

    verdict = cf.parse(text)
    assert verdict.kind == "reject", f"{text} 被当成泛指: {verdict}"


def test_subject_judgement_never_consults_symbol_guard(monkeypatch, name_list):
    """主语判定不许回头问 `symbol_guard.is_generic_reference`（那条有前缀回退）。"""
    import src.core.chan_followup as cf
    import src.core.symbol_guard as sg

    cf._name_index()             # 预热名字索引, 免得别名索引构建期的调用混进来
    sg.local_name_index()

    calls: list[str] = []
    monkeypatch.setattr(sg, "is_generic_reference",
                        lambda w: calls.append(str(w)) or False)

    for text in ("这只股票的缠论结构怎么样", "该股怎么样", "这张图怎么样",
                 "它后面怎么走", "这家银行的缠论结构怎么样"):
        assert cf.parse(text).kind == "generic", text
    assert calls == [], f"主语判定仍然依赖 symbol_guard 前缀回退: {calls}"


def test_generic_subjects_are_closed_literals(name_list):
    """主语是**完整字面量**集合：含必要常用项，且不含任何证券名/前缀通配。"""
    import src.core.chan_followup as cf

    for ok in ("这只股票", "这只票", "该股", "这张图", "它", "这家银行", "该板块"):
        assert cf._is_subject(ok, None), ok
    for bad in ("其他万科", "所有万科", "整个万科", "万科", "其他", "所有", "整个"):
        assert not cf._is_subject(bad, None), bad


# ── ② 代码必须原样（全角/拆分一律拒绝）──────────────────────────────────────

@pytest.mark.parametrize("text", [
    "６００５１９ 缠论结构怎么样",
    "600 519 缠论结构怎么样",
    "600　519 缠论结构怎么样",
    "600-519 缠论结构怎么样",
    "600.519 缠论结构怎么样",
    "600,519 缠论结构怎么样",
    "6 00519 缠论结构怎么样",
    "６00519 缠论结构怎么样",
    "600519 ６０００００ 缠论结构怎么样",
])
def test_code_must_be_verbatim_ascii(text, name_list):
    """全角数字/被拆开的数字簇不许被「修复」成一个合法代码。"""
    import src.core.chan_followup as cf

    verdict = cf.parse(text)
    assert verdict.kind == "reject", f"{text} 被归一化救活了: {verdict}"


def test_verbatim_ascii_code_is_still_code(name_list):
    import src.core.chan_followup as cf

    for text, code in (("600519 缠论结构怎么样", "600519"),
                       ("600519", "600519"),
                       ("缠论结构怎么样 600519", "600519"),
                       ("830799 缠论结构怎么样", "830799")):
        verdict = cf.parse(text)
        assert (verdict.kind, verdict.symbol) == ("code", code), verdict


@pytest.mark.parametrize("text", [
    "其他万科怎么样",
    "所有万科怎么样",
    "整个万科怎么样",
    "６００５１９ 缠论结构怎么样",
    "600 519 缠论结构怎么样",
    "600-519 缠论结构怎么样",
])
def test_route_drops_snapshot_for_hardened_attacks(
        client, accounts, feed, holder, name_list, text):
    """真实路由：页面 600519 + 600519 快照，上述攻击一律「不可读同图」。"""
    digest, conv_id = _ask(client, holder, text, "600519")
    _dropped(holder, digest)
    system = holder.systems[-1]
    assert "无法唯一确定" in system and "不可读同图" in system
    assert "封闭追问语法" in system
    assert (_conv_row(conv_id).stock_symbol or "") == "600519", "被拒的消息不许改会话标的"


@pytest.mark.parametrize("text", [
    "缠论结构怎么样",
    "这只股票的缠论结构怎么样",
    "这只票后面怎么操作",
    "这家银行的缠论结构怎么样",
    "该股怎么样",
    "这张图的缠论结构怎么样",
    "600519 缠论结构怎么样",
    "600519",
    "贵州茅台缠论怎么样",
])
def test_route_still_serves_generic_and_verbatim_code(
        client, accounts, feed, holder, name_list, text):
    """合法泛问与**原样** ASCII 代码提问照常可用（不许过度收紧）。"""
    digest, _conv_id = _ask(client, holder, text, "600519")
    _served(holder, digest, "600519")


def test_mention_code_is_still_independent(client, accounts, feed, holder, name_list):
    """@ 提及的规范代码字段有它自己的校验：正文泛问 + @600519 仍照常出结构。"""
    digest = _digest("600519")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600519", "SH")
    body = _body("这只票的缠论结构怎么样", _snap(digest, symbol="600519"))
    body["mentions"] = [{"type": "stock", "value": "600519", "label": "贵州茅台"}]
    r = client.post(f"/api/chat/conversations/{conv_id}/messages",
                    headers={"Authorization": f"Bearer {token}"}, json=body)
    assert r.status_code == 200, r.text
    _served(holder, digest, "600519")


# ── ③ 市场：CN 快照不能冒充非 CN 市场的同码页面 ────────────────────────────

def _turn(client, token, conv_id, text, snapshot, anchor=None,
          instrument=None, market=None):
    body = _body(text, snapshot)
    if anchor:
        body["stock_symbol"] = anchor
    if instrument:
        body["stock_instrument"] = instrument
    if market:
        body["stock_market"] = market
    return client.post(f"/api/chat/conversations/{conv_id}/messages",
                       headers={"Authorization": f"Bearer {token}"}, json=body)


def test_non_cn_page_market_drops_cn_snapshot(client, accounts, feed, holder, name_list):
    """页面声明市场 HK（同码 600519），随附的 CN 快照不是同一张图 → 丢。"""
    digest = _digest("600519")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600519", "SH")

    r = _turn(client, token, conv_id, "缠论结构怎么样",
              _snap(digest, symbol="600519"), anchor="600519", market="HK")
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    out = holder.outputs[-1]
    assert "市场" in system, f"说明里要写清是市场对不上: {system[-400:]}"
    assert digest not in out and _short(digest) not in out
    assert "缺少图表快照摘要" in out
    assert (_conv_row(conv_id).stock_symbol or "") == "600519"


#: 代码 → 该代码可证的交易所。600519 只可能是 SH（见 src.harness.actions.stock_exchange_of）:
#: 用例必须拿**对得上**的交易所声明，否则断言的是"错声明也要放行" —— 那正是 2026-11-18
#: 八轮审计要拦的行为（600519+SZ 曾被当成同一张图）。
_MARKET_MATCHING_CODE = (("600519", "SH"), ("000002", "SZ"), ("830799", "BJ"))


@pytest.mark.parametrize("symbol,market", [*_MARKET_MATCHING_CODE, ("600519", "CN")])
def test_cn_exchange_markets_still_serve(client, accounts, feed, holder, name_list,
                                         symbol, market):
    """A 股各交易所（SH/SZ/BJ）与聚合口径 CN 都是同一市场，代码对得上就不许误杀。

    `000002` 这类 000xxx 代码同时可能是个股与指数（平安银行/上证指数的同号问题），
    代码本身不可证类型 —— 页面滑到头仍要显式声明 `stock_instrument`，这里按真实页面
    的行为带上（前端图表页会广播图表自己声明的类型）。
    """
    digest = _digest(symbol)
    token = _login(client, "a")
    conv_id = _conv(client, token, symbol, market if market != "CN" else "SH")

    r = _turn(client, token, conv_id, "缠论结构怎么样",
              _snap(digest, symbol=symbol), anchor=symbol, market=market,
              instrument="stock")
    assert r.status_code == 200, r.text
    _served(holder, digest, symbol)


@pytest.mark.parametrize("symbol,market", [("600519", "SZ"), ("600519", "BJ")])
def test_wrong_exchange_declaration_drops_snapshot(client, accounts, feed, holder,
                                                   name_list, symbol, market):
    """声明了具体交易所却与代码可证身份矛盾（600519 不可能在 SZ/BJ）→ 整份丢弃。

    以前这里会放行（只校验 market 是否属于 CN/SH/SZ/BJ 集合），用户看到的会是
    "600519 的 K 线"却带着一个不可能的市场声明 —— 同码不同标的不能被当成同一张图。
    """
    digest = _digest(symbol)
    token = _login(client, "a")
    conv_id = _conv(client, token, symbol, "SH")

    r = _turn(client, token, conv_id, "缠论结构怎么样",
              _snap(digest, symbol=symbol), anchor=symbol, market=market)
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert f"【缠论结构·只读观测】{symbol}" not in out, out[:400]
    assert digest not in out
    assert "市场" in (holder.systems[-1] or "")


def test_recheck_drops_market_drift():
    """开流后复核：本轮市场与快照 market 对不上 → 整份丢弃（同号不同市场）。"""
    from src.harness.context import TurnChartSnapshot, recheck_turn_snapshot

    snap = TurnChartSnapshot(
        ref={"symbol": "600519", "market": "CN", "instrument": "stock",
             "interval": "1d", "days": 60, "digest": "a" * 64},
        turn_symbol="600519", turn_instrument="stock", turn_market="HK",
    )
    out = recheck_turn_snapshot(snap, "600519")
    assert out.ref is None
    assert "市场" in (out.note or "")
    assert "整份快照已丢弃" in (out.note or "")
