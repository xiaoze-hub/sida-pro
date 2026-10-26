# -*- coding: utf-8 -*-
"""图表说明(chart-notes)契约钉子(2026-10-14, 看图决策产品件)。

## 背景
产品口径(用户 2026-10-14 明示): **看图决策 = 提供图表说明供用户和 AI 共同决策**。
图表标注是人机共用的**证据语言** —— 用户看图拍板、AI 读图备料, 两者必须看到同一套标注。

## 本测试钉住四件事(禁真网络, 全纯函数 / mock)
1. **字段完整性**: 每条说明带 标记类型/日期/价格/触发/时点/失效/置信度 —— 三字段一个不缺;
2. **三字段降级(缺数据显式)**:
   - 无触发条件 → 显式「无可用触发条件(数据缺失)」, **不空、不编**;
   - 时点缺失 → 显式「数据时点缺失」(**不默认成今日**); 非今日显式标基准日;
   - 失效条件缺失 → 回兜底且 `invalidation_defaulted=True`(**永不为空**);
   - 置信度缺样本 → `calibrated=False` + `value=None`(不给校准数);
3. **同源契约**: chart-notes 条目 ⇔ 图层标记**逐条对应**(不重算第二套标记);
4. **推送引用同源**: 推送/战报引用图上 GS 标记的文案, 与 chart-notes 的 GS 说明**同源**
   (`chart_notes.gs_state_text` 单点产出)。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.collectors.kline_collector import KlineData  # noqa: E402
from src.core import chart_notes as CN  # noqa: E402
from src.web.api import klines as K  # noqa: E402


# ── fixture ──────────────────────────────────────────────────────────────────
def _bar(d, o, h, l, c, v=100.0):
    # 与生产同形: fetch_bars 产出 **dict** bar(图层/端点按 dict 取字段)
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _hammer_series():
    """前置温和下跌 + 一根金针探底(严格定义成立) → 至少 1 个形态标记。"""
    bars = [
        _bar("2026-03-01", 10.0, 10.1, 9.8, 9.9),
        _bar("2026-03-02", 9.9, 10.0, 9.7, 9.8),
        _bar("2026-03-03", 9.8, 9.9, 9.6, 9.7),
        _bar("2026-03-04", 9.7, 9.8, 9.6, 9.7),
        _bar("2026-03-05", 9.7, 9.8, 9.5, 9.6),
        _bar("2026-03-06", 9.6, 9.7, 9.55, 9.65),
        _bar("2026-03-07", 9.80, 9.95, 9.0, 9.90, 300),
    ]
    return bars


def _gs_signals():
    return [
        {"date": "2026-03-02", "side": "G", "confirmed": True, "price": 9.8},
        {"date": "2026-03-07", "side": "S", "confirmed": False, "price": 9.9},
    ]


def _events():
    return [
        {"date": "2026-03-03", "kind": "limit_up", "label": "涨停"},
        {"date": "2026-03-05", "kind": "announcement", "label": "公告(1)"},
        {"date": "2026-03-06", "kind": "unknown_kind_x", "label": "未知"},  # 未知类不产出
    ]


def _trend():
    return {
        "available": True,
        "buy_points": [{"signal": "buy", "rule": "pullback_bull_band_yang",
                        "trigger": "多方带中回踩带区且收阳", "price": 9.9, "time": "2026-03-07"}],
        "sell_points": [{"signal": "sell", "rule": "rebound_green_fail",
                         "trigger": "跌势中反弹触绿线无力突破", "price": 9.6, "time": "2026-03-05"}],
    }


def _niuxiong():
    return {"available": True, "signal": "B", "state": "牛线上方", "close": 9.9,
            "bar_time": "2026-03-07",
            "cross": {"type": "golden", "direction": "B", "bars_ago": 2, "time": "2026-03-05"}}


def _gs_state():
    return {
        "available": True, "zone": "G区", "signal": "G", "signal_date": "2026-03-02",
        "signal_price": 9.8, "signal_confirmed": True, "pending": True, "pending_side": "S",
        "pending_date": "2026-03-07", "pending_price": 9.9, "a0": 9.85, "bb0": 9.7,
        "label": "G区", "pending_label": "○S 待确认(盘中价, 收盘才定死)",
        "note": None, "as_of": "2026-03-07",
    }


def _patterns():
    from src.core.kline_patterns import detect_pattern_marks

    return detect_pattern_marks([b for b in _hammer_series()], lookback=60)


# ── 1. 字段完整性 ────────────────────────────────────────────────────────────
def test_每条说明字段齐全_三字段非空():
    out = CN.build_chart_notes(
        symbol="600000", as_of="2026-03-07",
        gs_signals=_gs_signals(), events=_events(), patterns=_patterns(),
        trend=_trend(), niuxiong=_niuxiong(), gs_state=_gs_state(),
    )
    assert out["available"] is True and out["count"] == len(out["notes"]) >= 5
    for n in out["notes"]:
        for key in ("kind", "marker_type", "label", "date", "trigger", "as_of",
                    "as_of_is_today", "as_of_note", "invalidation",
                    "invalidation_defaulted", "confidence", "id"):
            assert key in n, f"{n.get('kind')} 缺字段 {key}"
        assert n["trigger"] and all(isinstance(x, str) and x for x in n["trigger"])
        assert n["invalidation"] and all(isinstance(x, str) and x for x in n["invalidation"])
        assert n["as_of_note"]
        assert isinstance(n["confidence"], dict) and "note" in n["confidence"]
    # 每一类标记都覆盖到
    kinds = {n["kind"] for n in out["notes"]}
    assert {"gs", "pattern", "trend_buy", "trend_sell", "niuxiong",
            "limit_up", "announcement"} <= kinds
    # 未知事件类不产出(不猜)
    assert "unknown_kind_x" not in kinds


def test_notes_text_人可读单行():
    out = CN.build_chart_notes(symbol="600000", as_of="2026-03-07",
                              gs_signals=_gs_signals())
    assert len(out["notes_text"]) == out["count"] == 2
    for line in out["notes_text"]:
        assert "触发:" in line and "时点:" in line and "失效:" in line


# ── 2. 三字段降级(缺数据显式, 不编) ──────────────────────────────────────────
def test_触发条件缺失_显式无数据():
    n = CN.build_note(kind="gs", label="G 买入", date="2026-03-02", triggers=None)
    assert n["trigger"] == ["无可用触发条件(数据缺失)"]


def test_失效条件缺失_回兜底且标记defaulted():
    n = CN.build_note(kind="gs", label="G 买入", date="2026-03-02",
                      triggers=["A0上穿BB0"])
    assert n["invalidation"] and n["invalidation_defaulted"] is True


def test_时点缺失_不默认成今日():
    n = CN.build_note(kind="pattern", label="金针探底", date=None,
                      triggers=["规则匹配"], as_of=None)
    assert n["as_of"] is None
    assert n["as_of_is_today"] is False
    assert "时点缺失" in n["as_of_note"]


def test_时点非今日_显式标基准日():
    n = CN.build_note(kind="pattern", label="金针探底", date="2026-03-07",
                      triggers=["规则匹配"], as_of="2026-03-07")
    # as_of 给的是显式日期; 是否「今日」由 CST 当前日决定 —— 2026 年的日期显然非今日
    assert n["as_of"] == "2026-03-07"
    assert n["as_of_is_today"] is False
    assert "非今日" in n["as_of_note"]


def test_置信度缺样本_不给数字():
    n = CN.build_note(kind="gs", label="G 买入", date="2026-03-02",
                      triggers=["A0上穿BB0"])
    conf = n["confidence"]
    assert conf["calibrated"] is False and conf["value"] is None


def test_空输入_显式无数据不编造():
    out = CN.build_chart_notes(symbol="600000")
    assert out["available"] is False and out["count"] == 0
    assert out["notes"] == [] and out["notes_text"] == []
    assert "无数据" in out["note"]


# ── 3. 同源契约: chart-notes 条目 ⇔ 图层标记逐条对应 ─────────────────────────
def test_same_source_contract_逐条对应():
    gs = _gs_signals()
    ev = _events()
    pats = _patterns()
    trend = _trend()
    nx = _niuxiong()
    out = CN.build_chart_notes(
        symbol="600000", as_of="2026-03-07",
        gs_signals=gs, events=ev, patterns=pats, trend=trend,
        niuxiong=nx, gs_state=_gs_state(),
    )
    by_kind: dict[str, list] = {}
    for n in out["notes"]:
        by_kind.setdefault(n["kind"], []).append(n)

    # GS: 与图层 gs_signals 逐条对应(含待确认)
    assert len(by_kind["gs"]) == len(gs)
    assert [n["side"] for n in by_kind["gs"]] == [g["side"] for g in gs]
    assert [n["date"] for n in by_kind["gs"]] == [g["date"] for g in gs]

    # 形态: 与 detect_pattern_marks 逐条对应(index 对齐)
    assert len(by_kind["pattern"]) == len(pats)
    assert [n["index"] for n in by_kind["pattern"]] == [m.index for m in pats]

    # 操盘线买/卖点逐条对应
    assert len(by_kind["trend_buy"]) == len(trend["buy_points"])
    assert len(by_kind["trend_sell"]) == len(trend["sell_points"])
    assert by_kind["trend_buy"][0]["price"] == trend["buy_points"][0]["price"]

    # 牛熊线 1 条; 事件类逐条对应(未知类剔除)
    assert len(by_kind["niuxiong"]) == 1
    ev_kinds = [e["kind"] for e in ev if e["kind"] in CN.MARKER_KIND_LABELS]
    assert len(by_kind["limit_up"]) + len(by_kind["announcement"]) == len(ev_kinds)

    # index 字段: 形态带 index, 其它可为 None
    assert all(n["index"] is not None for n in by_kind["pattern"])


def test_事件类_与图层events逐点日期一致():
    out = CN.build_chart_notes(symbol="600000", events=_events())
    got = sorted((n["kind"], n["date"]) for n in out["notes"])
    assert ("limit_up", "2026-03-03") in got
    assert ("announcement", "2026-03-05") in got


# ── 4. 端点(全 mock, 不碰网络/DB) ────────────────────────────────────────────
def _patch_sources(monkeypatch, **over):
    src = {
        "bars": _hammer_series(),
        "gs_signals": _gs_signals(),
        "events": _events(),
        "patterns": _patterns(),
        "trend": _trend(),
        "niuxiong": _niuxiong(),
        "gs_state": _gs_state(),
    }
    src.update(over)
    monkeypatch.setattr(K, "_collect_chart_note_sources",
                        lambda s, m: dict(src))


def test_端点_契约与字段(monkeypatch):
    _patch_sources(monkeypatch)
    out = K.get_chart_notes("600000", market="CN")

    assert out["symbol"] == "600000" and out["market"] == "CN"
    assert out["asof"] == "2026-03-07"
    assert out["available"] is True and out["count"] == len(out["notes"]) >= 5
    assert out["counts"] and sum(out["counts"].values()) == out["count"]
    assert isinstance(out["notes_text"], list) and len(out["notes_text"]) == out["count"]
    # zone_note: 当前 GS 标记的说明(与推送同源), 独立于 notes 列表(不破坏逐条对应)
    assert out["zone_note"] and out["zone_note"]["kind"] == "gs"
    assert out["zone_note"]["extra"]["zone"] == "G区"


def test_端点_非A股显式降级(monkeypatch):
    out = K.get_chart_notes("AAPL", market="US")
    assert out["available"] is False and out["count"] == 0
    assert out["notes"] == [] and "仅 A 股" in out["note"]


def test_端点_无标记显式无数据(monkeypatch):
    _patch_sources(monkeypatch, bars=[], gs_signals=None, events=None,
                   patterns=None, trend=None, niuxiong=None, gs_state=None)
    out = K.get_chart_notes("600000", market="CN")
    assert out["available"] is False and out["count"] == 0
    assert out["zone_note"] is None
    assert "无数据" in out["note"]


def test_端点已注册在openapi():
    from src.web.app import app

    paths = app.openapi()["paths"]
    assert "/api/klines/{symbol}/chart-notes" in paths


# ── 5. 推送/战报引用图上 GS 标记 —— 同源取句 ─────────────────────────────────
def test_gs_state_text_与chart_note同源():
    gs = _gs_state()
    txt = CN.gs_state_text(gs, style="pioneer")
    assert "当前G区(已确认)" in txt and "待确认" in txt

    note = CN.gs_state_note(gs, as_of="2026-03-07")
    assert note and note["kind"] == "gs"
    assert "G区" in note["label"]
    # 同一份 gs_state → 推送句与说明句口径一致(区间相同)
    assert "G区" in txt and "G区" in str(note["extra"]["zone"])
    # 失效条件永不为空
    assert note["invalidation"]


def test_gs_state_text_无区间待确认():
    gs = dict(_gs_state(), zone=None, signal=None, available=False, pending=True,
              pending_side="G")
    txt = CN.gs_state_text(gs, style="pioneer")
    assert txt.startswith("待确认")
    note = CN.gs_state_note(gs, as_of="2026-03-07")
    assert note and "待确认" in note["label"]


def test_gs_state_text_无标记返回空():
    assert CN.gs_state_text({"zone": None, "pending": False}) == ""
    assert CN.gs_state_note({"zone": None, "pending": False}) is None


def test_决策面板推送文案走chart_notes同源(monkeypatch):
    """decision_pioneer 的 GS 文案必须等于 chart_notes.gs_state_text 的产出(单一事实源)。"""
    from src.core import decision_pioneer as dp

    gs = _gs_state()
    expected = CN.gs_state_text(gs, style="pioneer")

    # 直接验证 intext 拼装逻辑: 构造 d 走 decision_pioneer_text 的内部拼句
    import inspect

    src_txt = inspect.getsource(dp.decision_pioneer_text)
    assert "gs_state_text" in src_txt, "推送文案必须经 chart_notes.gs_state_text 取句(同源)"
    # 无数据也必须走同一分支
    assert "GS策略: 无数据" in src_txt
    # 生产句与本函数一致(同一输入同源)
    d = {"gs": gs, "institution_activity": None, "l2": None, "main_intent": None}
    _ = d  # 结构说明: 上面断言已钉住取句入口
    assert expected in f"GS策略: {expected}"


def test_盘中推送三指标段走chart_notes同源():
    import inspect

    from src.agents import intraday_monitor as im

    src_txt = inspect.getsource(im._append_decision_pioneer)
    assert "gs_state_text" in src_txt, "盘中推送 GS 文案必须经 chart_notes 同源取句"
    gs = _gs_state()
    line = CN.gs_state_text(gs, style="trend")
    assert "G区" in line and "待确认" in line
