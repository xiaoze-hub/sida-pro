# -*- coding: utf-8 -*-
"""数智参谋 5 个 chat 工具验收（设计稿 §12.2，2026-10-18）。

覆盖：
1. 5 个工具全部注册 + caliber 非空 + 导出顺序 index 31~35（连续前缀铁律）；
2. 空参不炸（返回提示语，不抛异常）；
3. dict bars 能吃（P0 锁定：CYQ 底仓模型不再 AttributeError）；
4. 撤单字段映射（小写归一化键 → TQ 原始键，漏这步就永远「无数据」）；
5. 会话值全 0 → 回退收盘快照（不把 0 当真实值）；
6. 竞价空池 / 妖股空库 → 明说「不猜」，不返回数字；
7. 权限拒绝时原样返回拒绝文案（不继续取数）；
8. 阶段提示文案 5 条齐全（少一条用户以为卡死）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.chat import tools_market as tm  # noqa: E402
from src.agents.chat.registry import CHAT_TOOL_REGISTRY  # noqa: E402

NEW_TOOLS = [
    "get_chip_accumulation",
    "get_cancel_order_intent",
    "get_wyckoff_structure",
    "get_demon_factor",
    "scan_auction_anomaly",
]


def _run(coro):
    return asyncio.run(coro)


def _bars(n: int = 80, start: float = 10.0) -> list[dict]:
    """升序日 K（dict 口径，与 decision_pioneer.fetch_bars 返回一致）。"""
    out = []
    px = start
    for i in range(n):
        px = px * 1.001
        out.append({
            "date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "open": round(px * 0.995, 2),
            "high": round(px * 1.02, 2),
            "low": round(px * 0.98, 2),
            "close": round(px, 2),
            "volume": 1_000_000 + i * 5_000,
        })
    return out


# ──────────────────────────── 1. 注册与顺序 ────────────────────────────

def test_tools_registered_with_caliber():
    assert set(NEW_TOOLS) <= set(CHAT_TOOL_REGISTRY)
    for name in NEW_TOOLS:
        tool = CHAT_TOOL_REGISTRY[name]
        assert tool.caliber and tool.caliber.strip(), f"{name} 缺口径标签"
        assert tool.schema is not None, f"{name} 应对 LLM 可见"
        assert tool.schema["function"]["name"] == name
        assert tool.requires == ""


def test_exported_order_is_core_prefix():
    from src.web.api import chat as chat_api

    names = [t["function"]["name"] for t in chat_api.CHAT_TOOLS]
    idx = [names.index(n) for n in NEW_TOOLS]
    assert idx == list(range(31, 36)), f"参谋工具不在 core 连续前缀尾: {idx}"
    # core 前缀之后的第一个工具必须是 thsdk 段（顺序错了 EXPECTED_CORE_ORDER 必红）
    assert names[37].startswith("get_thsdk_") or names[37] in {"get_wencai_enhanced"}


def test_stage_labels_present():
    from src.web.api.chat import _TOOL_STAGE_LABELS

    for name in NEW_TOOLS:
        assert name in _TOOL_STAGE_LABELS, f"{name} 缺流式阶段文案"
        assert _TOOL_STAGE_LABELS[name].strip()


# ──────────────────────────── 2. 空参不炸 ────────────────────────────

@pytest.mark.parametrize(
    "handler",
    [
        tm._tool_get_chip_accumulation,
        tm._tool_get_cancel_order_intent,
        tm._tool_get_wyckoff_structure,
        tm._tool_get_demon_factor,
    ],
)
def test_empty_args_returns_prompt(handler):
    out = _run(handler(None, {}))
    assert isinstance(out, str) and out.strip()
    assert "请提供股票代码" in out


def test_non_cn_rejected():
    for handler in (
        tm._tool_get_chip_accumulation,
        tm._tool_get_cancel_order_intent,
        tm._tool_get_wyckoff_structure,
    ):
        out = _run(handler(None, {"symbol": "600111", "market": "US"}))
        assert "仅支持 A 股" in out


def test_scan_auction_empty_args_ok(monkeypatch):
    monkeypatch.setattr("src.core.auction_pool.fetch_auction_anomaly", lambda market="CN": [])
    out = _run(tm._tool_scan_auction_anomaly(None, {}))
    assert isinstance(out, str) and out.strip()


# ──────────────────────────── 3. 底仓筹码（P0 dict bars） ────────────────────────────

def test_chip_accumulation_with_dict_bars(monkeypatch):
    monkeypatch.setattr("src.core.decision_pioneer.fetch_bars", lambda *a, **kw: _bars(80))
    out = _run(tm._tool_get_chip_accumulation(None, {"symbol": "600111"}))
    assert "底仓成本" in out
    assert "失败" not in out
    assert "70% 筹码带" in out
    assert "自算CYQ" in out  # 口径标注必须在


def test_chip_accumulation_short_series(monkeypatch):
    monkeypatch.setattr("src.core.decision_pioneer.fetch_bars", lambda *a, **kw: _bars(20))
    out = _run(tm._tool_get_chip_accumulation(None, {"symbol": "600111"}))
    assert "样本不足" in out
    assert "底仓成本" not in out  # 不足就绝不给数字


# ──────────────────────────── 4/5. 撤单意图 ────────────────────────────

def _live_row(**kw):
    row = {
        "symbol": "600111",
        "change_pct": 2.1,
        "cancel_buy": 900_000.0,
        "cancel_sell": 300_000.0,
        "total_buy_vol": 1_200_000.0,
        "total_sell_vol": 1_500_000.0,
        "l2_order_num": 40_000.0,
        "l2_tick_num": 30_000.0,
    }
    row.update(kw)
    return row


def test_cancel_order_intent_field_mapping(monkeypatch):
    """md_more_info 返回小写归一化键 → 必须搬到 TQ 原始键，否则 intent_tag 恒「无数据」。"""
    monkeypatch.setattr(
        "src.core.marketdata_client.md_more_info", lambda *a, **kw: [_live_row()]
    )
    out = _run(tm._tool_get_cancel_order_intent(None, {"symbol": "600111"}))
    assert "买盘撤单率" in out
    assert "无数据" not in out
    assert "盘中实时会话值" in out


def test_cancel_order_intent_falls_back_to_snapshot(monkeypatch):
    """会话值全 0（收盘/周末）→ 回退收盘快照并标注日期，绝不把 0 当真实值。"""
    zero = {k: 0.0 for k in (
        "cancel_buy", "cancel_sell", "total_buy_vol", "total_sell_vol",
        "l2_order_num", "l2_tick_num", "zjl", "zjl_hb",
    )}
    monkeypatch.setattr("src.core.marketdata_client.md_more_info", lambda *a, **kw: [dict(zero)])
    monkeypatch.setattr(
        "src.core.l2_fund_snapshot.latest_snapshot",
        lambda symbol, market="CN": {**_live_row(), "trade_date": "20261017", "origin": "snapshot"},
    )
    out = _run(tm._tool_get_cancel_order_intent(None, {"symbol": "600111"}))
    assert "收盘快照 20261017" in out
    assert "买盘撤单率" in out


def test_cancel_order_intent_no_data_honest(monkeypatch):
    zero = {"cancel_buy": 0.0, "cancel_sell": 0.0, "total_buy_vol": 0.0,
            "total_sell_vol": 0.0, "l2_order_num": 0.0, "l2_tick_num": 0.0}
    monkeypatch.setattr("src.core.marketdata_client.md_more_info", lambda *a, **kw: [dict(zero)])
    monkeypatch.setattr("src.core.l2_fund_snapshot.latest_snapshot", lambda *a, **kw: None)
    out = _run(tm._tool_get_cancel_order_intent(None, {"symbol": "600111"}))
    assert "不伪造" in out


# ──────────────────────────── 6. 妖股 / 竞价空数据 ────────────────────────────

def test_demon_empty_pool(monkeypatch):
    monkeypatch.setattr("src.core.demon_factors.load_factor_pool", lambda *a, **kw: [])
    out = _run(tm._tool_get_demon_factor(None, {"symbol": "002361"}))
    assert "因子库为空" in out
    assert "不猜" in out


def test_demon_hit_and_rank(monkeypatch):
    pool = [
        {"symbol": "600000", "total": 99, "grade": "极妖", "factor_date": "20261017"},
        {
            "symbol": "002361", "name": "神剑股份", "total": 88.5, "grade": "妖",
            "factor_date": "20261017", "max_streak": 5, "n_events": 9, "n_sealed": 7,
            "participation": 0.62, "dims": {"连板": 90, "封板": 80}, "flags": ["龙虎榜"],
        },
    ]
    monkeypatch.setattr("src.core.demon_factors.load_factor_pool", lambda *a, **kw: pool)
    out = _run(tm._tool_get_demon_factor(None, {"symbol": "002361.SZ"}))
    assert "第 2/200" in out
    assert "5 连板" in out


def test_auction_empty_pool_is_honest(monkeypatch):
    monkeypatch.setattr("src.core.auction_pool.fetch_auction_anomaly", lambda market="CN": [])
    out = _run(tm._tool_scan_auction_anomaly(None, {"level": "buy"}))
    assert "无数据，不猜" in out


def test_auction_filters_by_level_and_amount(monkeypatch):
    rows = [
        {"symbol": "600001", "name": "甲", "verdict_level": "buy", "verdict_tag": "🔥 爆量抢筹",
         "open_amount": 3.2e7, "gap_pct": 3.1, "volume_ratio": 3.4, "withdraw_rate": 12.0},
        {"symbol": "600002", "name": "乙", "verdict_level": "risk", "verdict_tag": "⚡ 恐慌砸盘",
         "open_amount": 5e7, "gap_pct": -4.0},
        {"symbol": "600003", "name": "丙", "verdict_level": "buy", "verdict_tag": "🔥 爆量抢筹",
         "open_amount": 1e6, "gap_pct": 2.6},
    ]
    monkeypatch.setattr("src.core.auction_pool.fetch_auction_anomaly", lambda market="CN": list(rows))
    out = _run(tm._tool_scan_auction_anomaly(
        None, {"level": "buy", "min_open_amount": 1e7, "limit": 5}
    ))
    assert "600001" in out
    assert "600002" not in out  # level 不匹配
    assert "600003" not in out  # 金额不足阈值
    assert "口径" in out


# ──────────────────────────── 7. 权限拒绝 ────────────────────────────

def test_permission_denied_short_circuits(monkeypatch):
    monkeypatch.setattr(
        "src.agents.chat.registry._perm_denied",
        lambda user, db, perm: "该功能需升级 Pro 后可用。",
    )
    out = _run(tm._tool_get_chip_accumulation(None, {"symbol": "600111"}, user=object()))
    assert out == "该功能需升级 Pro 后可用。"


# ──────────────────────────── 8. 威科夫 ────────────────────────────

def test_wyckoff_runs_on_dict_bars(monkeypatch):
    monkeypatch.setattr("src.core.decision_pioneer.fetch_bars", lambda *a, **kw: _bars(90))
    out = _run(tm._tool_get_wyckoff_structure(None, {"symbol": "600111"}))
    assert "日K量价结构" in out
    assert "失败" not in out


def test_wyckoff_no_bars(monkeypatch):
    monkeypatch.setattr("src.core.decision_pioneer.fetch_bars", lambda *a, **kw: [])
    out = _run(tm._tool_get_wyckoff_structure(None, {"symbol": "600111"}))
    assert "不猜" in out


# ──────────────────────────── 9. 三指标共振文案（§5.6） ────────────────────────────

def test_decision_pioneer_text_has_resonance(monkeypatch):
    from src.core import decision_pioneer as dp

    monkeypatch.setattr(dp, "fetch_decision_pioneer", lambda symbol, market="CN": {
        "institution_activity": None,
        "gs": {},
        "l2": {"available": False},
        "main_intent": None,
        "resonance_state": "👀 潜伏吸筹·主力在买",
        "resonance_action": "活跃度未破强势线但主力暗中流入，关注蓄势突破",
    })
    text = dp.decision_pioneer_text("600111")
    assert "共振状态" in text
    assert "潜伏吸筹" in text
