# -*- coding: utf-8 -*-
"""summary API 图层数据(gs_signals / fund_flow / events) + gs_strategy 序列函数 单测。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import gs_strategy  # noqa: E402
from src.web.api import klines as kapi  # noqa: E402
from src.core import decision_pioneer as dp  # noqa: E402


def _mk_bars(n=40, trend=0.1, final_surge=False):
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


# ---------------------------------------------------------------------------
# compute_gs_signals 序列
# ---------------------------------------------------------------------------


def test_gs_signals_series_confirmed_flags():
    """末根交叉 confirmed=False(待确认), 历史交叉 confirmed=True。"""
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    sigs = gs_strategy.compute_gs_signals(bars)
    assert sigs, "应有信号"
    last = sigs[-1]
    assert last["side"] == "G"
    assert last["confirmed"] is False
    assert all(s["confirmed"] for s in sigs[:-1])
    assert all(s["side"] in ("G", "S") for s in sigs)
    assert all("date" in s and "price" in s for s in sigs)


def test_gs_signals_insufficient_bars_empty():
    assert gs_strategy.compute_gs_signals(_mk_bars(n=10)) == []
    assert gs_strategy.compute_gs_signals([]) == []


def test_gs_signals_matches_eval_gs_latest():
    """序列的最后一条与 eval_gs 的最近信号一致(同一公式)。"""
    bars = _mk_bars(n=45, trend=-0.05, final_surge=True)
    sigs = gs_strategy.compute_gs_signals(bars)
    ev = gs_strategy.eval_gs(bars)
    if sigs:
        assert sigs[-1]["side"] == ev["signal"]


# ---------------------------------------------------------------------------
# _build_layer_data
# ---------------------------------------------------------------------------


def _patch_bars(monkeypatch, bars):
    monkeypatch.setattr(dp, "fetch_bars", lambda s, m, days=120: bars)


_ALL_NONE = {"gs_signals": None, "fund_flow": None, "events": None,
             "orderbook": None, "unlock_levels": None, "chips": None,
             # resonance(2026-09-02): 非 A 股 → 安全默认(available=False, 不编造)
             "resonance": {"available": False, "row": 0, "phase": "无",
                            "action_label": "观望", "action_text": "趋势不明, 建议观望",
                            "tone": "neutral", "bad_count": 0}}


def test_build_layer_data_non_cn_returns_none():
    from src.models.market import MarketCode
    out = kapi._build_layer_data("000977", MarketCode.HK)
    assert out == _ALL_NONE


def test_build_layer_data_no_bars_only_bars_dependent_none(monkeypatch):
    """2026-09-02 行为变更: bars 为空(多 vendor 全挂)时**不再整体早退**。

    依赖 bars 的 gs_signals / fund_flow 保持 None(显式"无数据", 不编造);
    不依赖 bars 的 orderbook / events(.tck 拆单撤单 + wencai 龙虎榜公告) / chips
    **仍要计算** —— 否则生产上腾讯 WAF 501 打空 bars, 会把这些本可独立产出的
    数据一起拖成 None, 前端出现"整页无数据"(2026-09-02 实测)。
    """
    from src.models.market import MarketCode
    _patch_bars(monkeypatch, [])
    out = kapi._build_layer_data("000977", MarketCode.CN)
    assert out["gs_signals"] is None
    assert out["fund_flow"] is None
    # 不依赖 bars 的项必须"尝试过": events 至少是 list(空源=空列表), 不得是 None
    assert out["events"] is not None, "bars 空时 events 仍应计算(.tck/wencai 不依赖 bars)"
    assert out["chips"] is not None, "bars 空时 chips 仍应计算(标准筹码接口, 不依赖 bars)"
    assert isinstance(out["orderbook"], (dict, type(None)))


def test_build_layer_data_full(monkeypatch):
    from src.models.market import MarketCode
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    _patch_bars(monkeypatch, bars)
    out = kapi._build_layer_data("000977", MarketCode.CN)

    # gs_signals: 有序列
    assert isinstance(out["gs_signals"], list)
    # fund_flow: 长度对齐 klines, dark_net 有值, ming_net 历史为 null
    assert len(out["fund_flow"]) == len(bars)
    assert all(f["ming_net"] is None for f in out["fund_flow"][:-1])
    assert any(f["dark_net"] is not None for f in out["fund_flow"])
    # events: 末根 +20% → 涨停
    assert any(e["kind"] == "limit_up" for e in out["events"])


def test_build_layer_data_events_limit_down(monkeypatch):
    from src.models.market import MarketCode
    bars = _mk_bars(n=40, trend=0.05)
    o = bars[-1]["close"]
    bars.append({"date": "2026-08-31", "open": o, "high": o + 0.05,
                 "low": o * 0.85 - 0.05, "close": o * 0.85, "volume": 500000})
    _patch_bars(monkeypatch, bars)
    out = kapi._build_layer_data("000977", MarketCode.CN)
    assert any(e["kind"] == "limit_down" for e in out["events"])


def test_date_from_tck_name():
    """.tck 文件名里的交易日 → YYYY-MM-DD; 文件名无日期 → None。"""
    assert kapi._date_from_tck_name("/app/data/tck/sz002361_20260827.tck") == "2026-08-27"
    assert kapi._date_from_tck_name("sz002361_20260827.tck") == "2026-08-27"
    assert kapi._date_from_tck_name("/app/data/tck/sz002361.tck") is None
    assert kapi._date_from_tck_name("") is None


def test_build_events_tck_independent_of_bars(monkeypatch):
    """bars 为空时 .tck 拆单/撤单事件仍独立产出, 且日期取**文件名里的交易日**。

    2026-09-02 生产: bars 因多 vendor 全挂为空, 若 events 跟着早退, .tck 里已有的
    拆单/撤单会一起丢失; 且日期不能沿用 bars 末根(今日), 否则 8-27 的事件被画到
    今日 K 线上(日期错位)。
    """
    from src.core import l4_events as l4
    from src.core import tdx_tick_parser as ttp

    monkeypatch.setattr(l4, "find_tck_file",
                        lambda symbol, date_=None: "/app/data/tck/sz002361_20260827.tck")
    # 5 笔 × 10 万(单笔 < 30 万伪装小单), 相邻 1s(< 30s 窗口), 同价 → 簇总额 50 万 >= 30 万
    trades = [{"t": 90_000 + i * 1_000, "price": 10.00, "vol": 10_000,
               "dir": "B", "amt": 100_000.0} for i in range(5)]
    cancels = [{"t": 91_000, "vol": 60_000, "target": 1}]  # 单笔撤单 >= 5 万股
    monkeypatch.setattr(ttp, "parse_tck", lambda path: (trades, [], cancels))
    monkeypatch.setattr(kapi, "_wencai_event_pairs", lambda s, d: [])

    evs = kapi._build_events("002361", [])  # bars 空
    kinds = {e["kind"] for e in evs}
    assert "split_cluster" in kinds, evs
    assert "cancel_anomaly" in kinds, evs
    assert evs and all(e["date"] == "2026-08-27" for e in evs), evs


def test_tencent_code_normalizes():
    assert kapi._tencent_code("000977") == "sz000977"
    assert kapi._tencent_code("600103") == "sh600103"
    assert kapi._tencent_code("603893") == "sh603893"
    assert kapi._tencent_code("688981") == "sh688981"
    assert kapi._tencent_code("sz000977") == "sz000977"
    assert kapi._tencent_code("abc") is None


# ---------------------------------------------------------------------------
# gs_signals 日期规范化(v0.4.58)
# ---------------------------------------------------------------------------


def test_gs_norm_date_basic():
    """`_norm_date`: 8 位纯数字 → YYYY-MM-DD; 已是规范格式原样; 空 → None。"""
    from src.core.gs_strategy import _norm_date

    assert _norm_date("20260902") == "2026-09-02"
    assert _norm_date("2026-09-02") == "2026-09-02"
    assert _norm_date("") is None
    assert _norm_date(None) is None


def test_gs_signals_date_normalized_from_tq_format():
    """TQ 日K date 为 `20260902` → gs_signals 输出必须规范化为 `2026-09-02`。

    否则同一接口里 events/fund_flow 是 `2026-08-27`、gs_signals 却是 `20260902`,
    前端按日期把 GS 标记匹配到 K 线会错位; 且主源 TQ→东财降级时样式会突变。
    """
    from src.core import gs_strategy

    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    for b in bars:
        b["date"] = b["date"].replace("-", "")   # 模拟通达信 TQ 样式
    sigs = gs_strategy.compute_gs_signals(bars)
    assert sigs, "应有 GS 信号"
    assert all(len(s["date"]) == 10 and s["date"][4] == "-" for s in sigs), sigs


def test_gs_signals_date_keeps_eastmoney_format():
    """东财/新浪 date 已是 `2026-08-31`(规范格式) → 原样返回, 不被破坏。"""
    from src.core import gs_strategy

    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    sigs = gs_strategy.compute_gs_signals(bars)
    assert sigs, "应有 GS 信号"
    assert all(len(s["date"]) == 10 and s["date"][4] == "-" for s in sigs), sigs
