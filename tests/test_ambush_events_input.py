"""2026-09-07 hotfix: 事件流 subjects 落代码后并入埋伏漏斗。

It's working if:
- 无 symbol 的纯宏观日历 + 带 subjects 的事件流 → 漏斗产出含事件标的的榜单
- 全无 symbol 输入 → 仍为空榜(规则层语义不变)
"""
from __future__ import annotations

import src.core.catalyst_screener as M


def _macro_only_cal():
    return [
        {"symbol": "", "date": "2026-09-20", "type": "宏观", "title": "议息会议"},
        {"symbol": "", "date": "2026-09-25", "type": "解禁", "title": "大盘股解禁"},
    ]


def _events():
    return [
        {"time": "2026-09-07 08:10", "level": "高", "content": "铜加工费涨价函", "subjects": ["002361"], "ref": ""},
        {"time": "2026-09-06", "level": "", "content": "无标的事件", "subjects": [], "ref": ""},
        {"time": "", "level": "", "content": "", "subjects": ["002361"], "ref": ""},
    ]


def test_events_to_calendar_maps_subjects():
    out = M.events_to_calendar(_events(), today="2026-09-07")
    syms = {c["symbol"] for c in out}
    assert "002361" in syms
    assert all(c["symbol"] for c in out)
    assert all(c["date"] and c["type"] and c["title"] for c in out)


def test_events_to_calendar_empty_inputs():
    assert M.events_to_calendar([], today="2026-09-07") == []
    assert M.events_to_calendar(None, today="2026-09-07") == []
    assert M.events_to_calendar(
        [{"time": "x", "content": "y", "subjects": []}], today="2026-09-07"
    ) == []


def test_funnel_uses_events_when_calendar_symbol_less(monkeypatch):
    import src.core.catalyst_screener as MM

    monkeypatch.setattr(
        MM, "_snapshots",
        lambda syms: {"002361": {"change_pct": 1.0, "volume_ratio": 1.2}},
    )

    def _fake_llm(sym, extra):
        return {
            "expectation_gap": {"level": "高"},
            "reason": "涨价",
            "catalyst": "提价函",
            "beneficiary_pool": ["002361"],
        }

    merged = M.events_to_calendar(_events(), today="2026-09-07") + _macro_only_cal()
    out = MM.build_ambush_list(merged, today="2026-09-07", topn=8, run_llm=_fake_llm)
    assert any(c["symbol"] == "002361" for c in out)
