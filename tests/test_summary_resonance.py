# -*- coding: utf-8 -*-
"""summary API resonance 字段(决策先锋三指标共振状态)单测。

字段契约(前端严格按此消费, 2026-09-02):
    resonance = {
      "available": bool,     # 三指标(趋势/活跃度/资金)是否都可得
      "row": int,            # 0-7(resonance.evaluate_state 的行号)
      "phase": str,          # "向好"/"拐点"/"分歧"/"走坏"/"无"
      "action_label": str,   # "GO"/"STOP"/"持有"/"警惕"/"观望"
      "action_text": str,    # 完整文案
      "tone": str,           # "bull"/"bear"/"warn"/"neutral"
      "bad_count": int       # 走坏指标数(0-3)
    }
缺任一指标 → available=False + 安全默认(row=0/phase="无"/action_label="观望"), 不编造。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import ai_activity, dark_pool_flow, gs_strategy  # noqa: E402
from src.core import decision_pioneer as dp  # noqa: E402
from src.core import resonance as res_mod  # noqa: E402
from src.web.api import klines as kapi  # noqa: E402

CONTRACT_KEYS = {"available", "row", "phase", "action_label", "action_text",
                 "tone", "bad_count"}
PHASES = {"向好", "拐点", "分歧", "走坏", "无"}
LABELS = {"GO", "STOP", "持有", "警惕", "观望"}
TONES = {"bull", "bear", "warn", "neutral"}


def _mk_bars(n=40, trend=-0.05, final_surge=True):
    """与 test_summary_layer_api 同构的日K: 缓跌后末根 +20% 大阳(触发 G区 + 高活跃度)。"""
    bars = []
    price = 20.0
    for i in range(n):
        o = price
        price += trend
        c = price
        bars.append({"date": f"2026-07-{i+1:02d}", "open": o,
                     "high": max(o, c) + 0.05, "low": min(o, c) - 0.05,
                     "close": c, "volume": 100000})
    if final_surge:
        o = bars[-1]["close"]
        c = o * 1.20
        bars.append({"date": "2026-08-31", "open": o, "high": c + 0.05,
                     "low": o - 0.05, "close": c, "volume": 500000})
    return bars


def _patch_pool_flow(monkeypatch, main_net):
    """mock compute_pool_flow(网络部分), 返回 {"main_net": ...} 或 None。"""
    if main_net is _RAISE:
        monkeypatch.setattr(dark_pool_flow, "compute_pool_flow",
                            lambda s: (_ for _ in ()).throw(RuntimeError("tick 源全挂")))
    elif main_net is None:
        monkeypatch.setattr(dark_pool_flow, "compute_pool_flow", lambda s: None)
    else:
        monkeypatch.setattr(dark_pool_flow, "compute_pool_flow",
                            lambda s: {"symbol": s, "main_net": main_net, "coverage": "full"})


class _RaiseSentinel:
    def __repr__(self):
        return "<RAISE>"


_RAISE = _RaiseSentinel()


def _assert_contract(r):
    """字段契约: 7 个键齐全, 类型/取值域正确。"""
    assert set(r.keys()) == CONTRACT_KEYS, f"契约键不符: {sorted(r.keys())}"
    assert isinstance(r["available"], bool)
    assert isinstance(r["row"], int) and 0 <= r["row"] <= 7
    assert r["phase"] in PHASES
    assert r["action_label"] in LABELS
    assert isinstance(r["action_text"], str) and r["action_text"]
    assert r["tone"] in TONES
    assert isinstance(r["bad_count"], int) and 0 <= r["bad_count"] <= 3


def _assert_safe_default(r):
    """available=False 时的安全默认(不编造)。"""
    _assert_contract(r)
    assert r["available"] is False
    assert r["row"] == 0
    assert r["phase"] == "无"
    assert r["action_label"] == "观望"
    assert r["tone"] == "neutral"
    assert r["bad_count"] == 0


# ---------------------------------------------------------------------------
# _build_resonance: 三指标齐全 → 按 7 行状态表判定
# ---------------------------------------------------------------------------


def test_resonance_g_zone_strong_inflow_row3(monkeypatch):
    """G区间 + 强势线上 + 流入 → 行3 平稳(向好/持有)。

    末根 +20% 大阳: 趋势 G区间、活跃度 24+(较前日翻倍成立);
    fund_net_prev 历史明盘不可得恒 None → 双翻倍的行2 不触发, 落行3。
    """
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    _patch_pool_flow(monkeypatch, 5_000_000.0)
    r = kapi._build_resonance("000977", bars)
    _assert_contract(r)
    assert r["available"] is True
    assert r["row"] == 3
    assert r["phase"] == "向好"
    assert r["action_label"] == "持有"
    assert r["tone"] == "bull"
    assert r["bad_count"] == 0


def test_resonance_g_zone_strong_outflow_row4(monkeypatch):
    """G区间 + 强势线上 + 流出 → 行4(1个走坏, 分歧/警惕)。"""
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    _patch_pool_flow(monkeypatch, -3_000_000.0)
    r = kapi._build_resonance("000977", bars)
    _assert_contract(r)
    assert r["available"] is True
    assert r["row"] == 4
    assert r["phase"] == "分歧"
    assert r["action_label"] == "警惕"
    assert r["tone"] == "warn"
    assert r["bad_count"] == 1


def test_resonance_g_signal_row1_go(monkeypatch):
    """G信号 + 强势线上 + 流入 → 行1 首次共振(向好/GO)。"""
    bars = _mk_bars(n=40)
    monkeypatch.setattr(gs_strategy, "eval_gs", lambda b: {
        "zone": gs_strategy.ZONE_G, "signal": "G", "signal_confirmed": True,
        "pending": False, "bb0": 1.0, "a0": 1.1,
        "new_g_today": True, "new_s_today": False})
    monkeypatch.setattr(ai_activity, "eval_activity",
                        lambda b: {"activity": 4.2})
    _patch_pool_flow(monkeypatch, 800_000.0)
    r = kapi._build_resonance("000977", bars)
    _assert_contract(r)
    assert r["available"] is True
    assert r["row"] == 1
    assert r["phase"] == "向好"
    assert r["action_label"] == "GO"
    assert r["tone"] == "bull"
    assert r["bad_count"] == 0
    assert r["action_text"] == "趋势有望启动, 建议逢低建仓"


def test_resonance_s_signal_row7_stop(monkeypatch):
    """S信号 + 跌破强势线 + 流出 → 行7 全走坏(走坏/STOP, bad_count=3)。"""
    bars = _mk_bars(n=40)
    monkeypatch.setattr(gs_strategy, "eval_gs", lambda b: {
        "zone": gs_strategy.ZONE_S, "signal": "S", "signal_confirmed": True,
        "pending": False, "bb0": 1.1, "a0": 1.0,
        "new_g_today": False, "new_s_today": True})
    monkeypatch.setattr(ai_activity, "eval_activity",
                        lambda b: {"activity": 1.2})
    _patch_pool_flow(monkeypatch, -900_000.0)
    r = kapi._build_resonance("000977", bars)
    _assert_contract(r)
    assert r["available"] is True
    assert r["row"] == 7
    assert r["phase"] == "走坏"
    assert r["action_label"] == "STOP"
    assert r["tone"] == "bear"
    assert r["bad_count"] == 3


# ---------------------------------------------------------------------------
# 缺数/异常 → available=False 安全默认, 绝不编造, 绝不抛出
# ---------------------------------------------------------------------------


def test_resonance_fund_missing_available_false(monkeypatch):
    """资金不可得(main_net=None, 明暗未同时覆盖) → available=False。

    趋势/活跃度明明可得, 也不允许只拿两指标硬判 —— 三指标缺一即整体不可用。
    """
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    monkeypatch.setattr(dark_pool_flow, "compute_pool_flow",
                        lambda s: {"symbol": s, "main_net": None, "coverage": "ming_only"})
    _assert_safe_default(kapi._build_resonance("000977", bars))


def test_resonance_pool_flow_none_available_false(monkeypatch):
    """compute_pool_flow 整体返回 None(代码无法识别等) → available=False。"""
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    _patch_pool_flow(monkeypatch, None)
    _assert_safe_default(kapi._build_resonance("000977", bars))


def test_resonance_pool_flow_raises_available_false(monkeypatch):
    """compute_pool_flow 抛异常 → 兜底 available=False, 不允许向上传播拖垮 summary。"""
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    _patch_pool_flow(monkeypatch, _RAISE)
    _assert_safe_default(kapi._build_resonance("000977", bars))


def test_resonance_no_bars_available_false():
    """bars 为空(多 vendor 全挂) → available=False 安全默认。"""
    _assert_safe_default(kapi._build_resonance("000977", []))


def test_resonance_trend_missing_available_false(monkeypatch):
    """日K < 28 根 → eval_gs 无数据 → 趋势缺失 → available=False(资金可得也不判)。"""
    bars = _mk_bars(n=10, trend=0.1, final_surge=False)  # eval_activity 可算, eval_gs 不足
    _patch_pool_flow(monkeypatch, 1_000_000.0)
    r = kapi._build_resonance("000977", bars)
    _assert_safe_default(r)
    # 交叉验证: 活跃度确实可得(缺的只是趋势), 仍必须整体 available=False
    assert ai_activity.eval_activity(bars)["activity"] is not None


def test_resonance_activity_missing_available_false(monkeypatch):
    """活跃度缺失(末根脏数据算不出) → available=False。"""
    bars = _mk_bars(n=40)
    monkeypatch.setattr(ai_activity, "eval_activity", lambda b: {"activity": None})
    _patch_pool_flow(monkeypatch, 1_000_000.0)
    _assert_safe_default(kapi._build_resonance("000977", bars))


def test_resonance_inner_exception_safe_default(monkeypatch):
    """内部任何意外(如 eval_gs 抛异常) → 外层兜底安全默认, 不抛出。"""
    bars = _mk_bars(n=40)

    def _boom(b):
        raise ValueError("unexpected")

    monkeypatch.setattr(gs_strategy, "eval_gs", _boom)
    _patch_pool_flow(monkeypatch, 1_000_000.0)
    _assert_safe_default(kapi._build_resonance("000977", bars))


# ---------------------------------------------------------------------------
# 契约一致性: API 默认值 == state_action_label(0)
# ---------------------------------------------------------------------------


def test_default_matches_state_action_label_row0():
    """安全默认的文案/色彩必须与 resonance.state_action_label(0) 一致(单一事实源)。"""
    al = res_mod.state_action_label(0)
    d = kapi._resonance_default()
    assert d["action_label"] == al["label"]
    assert d["action_text"] == al["text"]
    assert d["tone"] == al["tone"]
    assert d["row"] == 0 and d["phase"] == "无" and d["bad_count"] == 0
    assert d["available"] is False


# ---------------------------------------------------------------------------
# _build_layer_data / summary 接线: resonance 字段进入响应
# ---------------------------------------------------------------------------


def _patch_layer_network(monkeypatch, bars, main_net):
    """把 _build_layer_data 里所有网络/重 IO 依赖 mock 掉, 只留纯计算 + 受控资金。"""
    from src.core import l4_events as l4
    from src.core import dark_l2, orderbook_engine as obe

    monkeypatch.setattr(dp, "fetch_bars", lambda s, m, days=120: bars)
    _patch_pool_flow(monkeypatch, main_net)
    # events: .tck 无文件 + wencai 空
    monkeypatch.setattr(l4, "find_tck_file", lambda symbol, date_=None: None)
    monkeypatch.setattr(kapi, "_wencai_event_pairs", lambda s, d: [])
    # events 涨停/跌停用 bars 自算, 保留; 暗盘逐日分摊纯计算保留
    monkeypatch.setattr(dark_l2, "fetch_l2_ticks",
                        lambda code, source="thsdk": (_ for _ in ()).throw(RuntimeError("no net")))
    # orderbook: 无 .img, thsdk 快照立即失败
    monkeypatch.setattr(obe, "find_img_file", lambda symbol, market: None)
    monkeypatch.setattr(obe, "fetch_snapshot",
                        lambda code: (_ for _ in ()).throw(RuntimeError("no thsdk")))
    # chips: 解锁位纯函数兜底
    monkeypatch.setattr(l4, "unlock_levels_from_chips", lambda chips: None)


def test_build_layer_data_contains_resonance(monkeypatch):
    """CN + bars 齐全 → layer 输出带 resonance(available=True, 契约完整)。"""
    from src.models.market import MarketCode

    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    _patch_layer_network(monkeypatch, bars, 5_000_000.0)
    out = kapi._build_layer_data("000977", MarketCode.CN)
    assert "resonance" in out
    _assert_contract(out["resonance"])
    assert out["resonance"]["available"] is True
    assert out["resonance"]["row"] == 3
    assert out["resonance"]["phase"] == "向好"


def test_build_layer_data_no_bars_resonance_default(monkeypatch):
    """bars 为空 → resonance 保持安全默认(available=False), 不拖垮其余字段。"""
    from src.models.market import MarketCode

    _patch_layer_network(monkeypatch, [], 5_000_000.0)
    out = kapi._build_layer_data("000977", MarketCode.CN)
    _assert_safe_default(out["resonance"])


def test_build_layer_data_non_cn_resonance_default():
    """非 A 股(决策先锋体系仅 A 股) → resonance 安全默认。"""
    from src.models.market import MarketCode

    out = kapi._build_layer_data("00700", MarketCode.HK)
    _assert_safe_default(out["resonance"])


def test_summary_endpoint_exposes_resonance(monkeypatch):
    """GET /{symbol}/summary 响应顶层带 resonance 字段(契约完整)。"""

    class _FakeCollector:
        def __init__(self, market):
            pass

        def get_kline_summary(self, symbol):
            return {"close": 20.0}

    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    _patch_layer_network(monkeypatch, bars, 5_000_000.0)
    monkeypatch.setattr(kapi, "KlineCollector", _FakeCollector)
    # dark_clusters / main_intent 与共振无关, mock 掉避免网络
    from src.core import postmarket_review
    monkeypatch.setattr(postmarket_review, "dark_review_from_tck",
                        lambda symbol, date_=None, tck_path=None: {"available": False})

    kapi._SUMMARY_CACHE.clear()
    try:
        result = kapi.get_kline_summary("000977", market="CN")
    finally:
        kapi._SUMMARY_CACHE.clear()

    assert "resonance" in result
    _assert_contract(result["resonance"])
    assert result["resonance"]["available"] is True
    assert result["resonance"]["action_label"] == "持有"
