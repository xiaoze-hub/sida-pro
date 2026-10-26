# -*- coding: utf-8 -*-
"""数智决策「主力净流入(L2·TQ)」非交易时段回退 + 0 不冒充三态回归(2026-10-11, 契约钉接线)。

真 bug(生产截图 600111 周末): 卡片走 **另一条取数路**(`fetch_decision_pioneer` → TQ
`get_more_info` 实时会话值), 非交易时段 TQ 一律回 0 → 卡片显示"0万 平衡 / 逐笔0笔·委托0笔",
把 0 冒充真实值。`/quotes/{symbol}/more-info` 同款回退此前已修(v0.13.61), 本文件钉的是
**decision-pioneer 这条路的同款回退**(`_apply_l2_fallback`)。

三态(全 mock, 禁真实网络):
- 会话任一字段非 0 → 原样返回(source='live');
- 非交易时段/会话值全 0 + 有收盘快照 → 回退快照, 显式 source/as_of/note(0 不冒充);
- 非交易时段/会话值全 0 + 无快照 → available:false + 显式无数据(绝不返 0)。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from src.web.api import decision_pioneer as dp  # noqa: E402


def _data(l2: dict) -> dict:
    return {"symbol": "600111", "market": "CN", "l2": l2}


def _patch_session(monkeypatch, in_session: bool):
    import src.core.trading_calendar as tc

    monkeypatch.setattr(tc, "is_trading_session", lambda now=None: in_session)


def _patch_snapshot(monkeypatch, s):
    import src.core.l2_fund_snapshot as snap

    monkeypatch.setattr(snap, "latest_snapshot", lambda symbol, market="CN": s)


# ─────────────── ① 实时有值 → 原样返回 ───────────────

def test_live_values_pass_through(monkeypatch):
    _patch_session(monkeypatch, True)
    _patch_snapshot(monkeypatch, None)
    l2 = {"available": True, "zjl_hb": -3762.99 * 1e4, "direction": "净流出",
          "l2_tick_num": 120327, "l2_order_num": 224629}
    out = dp._apply_l2_fallback(_data(l2))
    assert out["l2"]["available"] is True and out["l2"]["source"] == "live"
    assert out["l2"]["zjl_hb"] == -3762.99 * 1e4  # 未被回退改写


def test_tick_only_counts_as_live(monkeypatch):
    """净额恰为 0 但确有逐笔 → 属实时(真值的"平衡"), 不回退。"""
    _patch_session(monkeypatch, True)
    _patch_snapshot(monkeypatch, None)
    l2 = {"available": True, "zjl_hb": 0, "direction": "平衡", "l2_tick_num": 999}
    out = dp._apply_l2_fallback(_data(l2))
    assert out["l2"]["source"] == "live" and out["l2"]["zjl_hb"] == 0


# ─────────────── ② 全 0 + 有快照 → 回退并显式标注(0 不冒充) ───────────────

def test_closed_all_zero_falls_back_to_snapshot(monkeypatch):
    _patch_session(monkeypatch, False)
    _patch_snapshot(monkeypatch, {"zjl_hb": -3762.99, "l2_tick_num": 120327,
                                  "l2_order_num": 224629, "trade_date": "20261009",
                                  "origin": "snapshot"})
    l2 = {"available": True, "zjl_hb": 0, "direction": "平衡", "l2_tick_num": 0,
          "l2_order_num": 0}
    out = dp._apply_l2_fallback(_data(l2))["l2"]
    assert out["available"] is True and out["source"] == "snapshot"
    assert out["as_of"] == "2026-10-09" and "2026-10-09" in out["note"]
    assert "非交易时段" in out["note"]
    # 快照 zjl_hb 是万元 → 转元(与 _l2_summary 口径一致), 不是会话的 0
    assert out["zjl_hb"] == -3762.99 * 1e4
    assert out["direction"] == "净流出"
    assert out["l2_tick_num"] == 120327


def test_in_session_but_zero_also_falls_back(monkeypatch):
    """盘中但会话值全 0(异常空会话)也走同一回退, note 措辞区分。"""
    _patch_session(monkeypatch, True)
    _patch_snapshot(monkeypatch, {"zjl_hb": 12.5, "l2_tick_num": 3, "l2_order_num": 4,
                                  "trade_date": "20261009", "origin": "seal_sample"})
    l2 = {"available": True, "zjl_hb": 0, "l2_tick_num": 0, "l2_order_num": 0}
    out = dp._apply_l2_fallback(_data(l2))["l2"]
    assert out["source"] == "seal_sample"
    assert "实时会话值全 0" in out["note"]
    assert out["zjl_hb"] == 12.5 * 1e4


# ─────────────── ③ 全 0 + 无快照 → available:false(不返 0) ───────────────

def test_closed_all_zero_no_snapshot_no_fabrication(monkeypatch):
    _patch_session(monkeypatch, False)
    _patch_snapshot(monkeypatch, None)
    l2 = {"available": True, "zjl_hb": 0, "direction": "平衡", "l2_tick_num": 0,
          "l2_order_num": 0}
    out = dp._apply_l2_fallback(_data(l2))["l2"]
    assert out["available"] is False and out["source"] == "session_closed"
    assert "不冒充" in out["note"]
    assert "zjl_hb" not in out or out.get("zjl_hb") is None


# ─────────────── ④ 已 unavailable → 原样透传(不重复回退) ───────────────

def test_unavailable_passthrough(monkeypatch):
    _patch_session(monkeypatch, False)
    called = {"n": 0}

    import src.core.l2_fund_snapshot as snap

    def _boom(symbol, market="CN"):
        called["n"] += 1
        return None

    monkeypatch.setattr(snap, "latest_snapshot", _boom)
    out = dp._apply_l2_fallback(_data({"available": False}))
    assert out["l2"]["available"] is False
    assert called["n"] == 0  # 已不可用不该再查快照


# ─────────────── ⑤ 接线: /{symbol} 路由经 _get 走回退 ───────────────

def test_get_route_wires_fallback(monkeypatch):
    """钉接线: `_get()` 取数后必须调用 `_apply_l2_fallback`。"""
    import src.web.api.decision_pioneer as mod

    monkeypatch.setattr(mod, "fetch_decision_pioneer",
                        lambda s, m: {"symbol": s, "market": m,
                                      "l2": {"available": True, "zjl_hb": 0,
                                             "direction": "平衡", "l2_tick_num": 0,
                                             "l2_order_num": 0}})
    _patch_session(monkeypatch, False)
    _patch_snapshot(monkeypatch, {"zjl_hb": 5.0, "l2_tick_num": 1, "l2_order_num": 2,
                                  "trade_date": "20261009", "origin": "snapshot"})
    # 清缓存, 用未被占用的 key
    mod._CACHE.pop("CN:600111", None)
    out = mod._get("600111", "CN")
    assert out["l2"]["source"] == "snapshot" and out["l2"]["zjl_hb"] == 5.0 * 1e4
