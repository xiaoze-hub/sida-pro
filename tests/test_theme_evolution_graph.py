"""题材—标的—事件可追溯证据图(纯函数)验收。

这一层把调用者已核验的显式事件(事件唯一 id / 来源 / 发生日 / 主题原文 /
标的身分与身份状态 / 可未知的公开时间)整理成**有边界的证据图**:

* 节点: 观测周期窗口 / 主题 / 标的 / 事件;
* 边: 只允许"原文完全相等""调用者显式声明""同日同标的共现""日历核实窗口内成员"
  这几类观察性关系, 任何资金流、因果、接力、主线、买卖都不在输出里;
* 缺交易日历不允许推断"连续", 只能报 unknown;
* 未经验证身份的标的绝不被拼接到已核实标的上, 也不允许同名/相似字符串合并。

测试重点在"会不会编": 未来日期、重复 id、未切分标签、周末、日历断档一律抛错;
两条相似标签不许合并; 同名不同 id 不许合并; 缺日历不许出现跨日聚合边;
真实文件样例只做去标识后的事实核对, 不引入任何结论字段。
"""
from __future__ import annotations

import copy
import inspect
import json
import pathlib
import re

import pytest

from src.core import theme_evolution_graph as graph

AS_OF = "2026-01-12"
REPO = pathlib.Path(__file__).resolve().parents[1]
CAL_FIXTURE = REPO / "tests" / "fixtures" / "real_trading_days.txt"

#: 项目外只读研究目录(真实供应商快照); 不存在时相关用例跳过, 不伪造数据。
REAL_DIR = pathlib.Path("/home/ubuntu/mainline-evolution-research")

SYNTH_CALENDAR = ["2026-01-05", "2026-01-06", "2026-01-07",
                  "2026-01-08", "2026-01-09", "2026-01-12"]


# ---------------------------------------------------------------- 夹具构造
def ev(event_id, date, theme, instrument_id=None, *, status="verified", name=None,
       branch=None, source="src-a", basis: str | None = "caller-cross-check",
       published_at=None, reference=None, **kw):
    item = {"event_id": event_id, "occurred_on": date, "theme_text": theme,
            "source": source}
    if instrument_id is not None:
        inst = {"id": instrument_id, "identity_status": status}
        if name is not None:
            inst["name"] = name
        if basis is not None:
            inst["identity_basis"] = basis
        item["instrument"] = inst
    if branch is not None:
        item["branch"] = branch
    if published_at is not None:
        item["published_at"] = published_at
    if reference is not None:
        item["reference"] = reference
    item.update(kw)
    return item


def bar(instrument_id, date, *, o=10.0, h=10.5, lo=9.8, c=10.2, v=1000):
    return {"id": instrument_id, "date": date, "open": o, "high": h, "low": lo,
            "close": c, "volume": v, "cross_source_identity_basis": "synthetic-test-only"}


def base_events():
    return [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A", name="合成甲", branch="电池"),
        ev("E2", "2026-01-05", "硅碳负极", "SYN-A", name="合成甲", branch="电池"),
        ev("E3", "2026-01-05", "钠电材料", "SYN-B", name="合成乙", branch="电池"),
        ev("E4", "2026-01-06", "钠电材料", "SYN-A", name="合成甲", branch="电池"),
        ev("E5", "2026-01-07", "固态电池", "SYN-B", name="合成乙", branch="电池"),
    ]


def build(events=None, **kw):
    kw.setdefault("as_of", AS_OF)
    return graph.build_evidence_graph(
        base_events() if events is None else events, **kw)


def nodes_of(payload, kind):
    return [n for n in payload["nodes"] if n["kind"] == kind]


def node(payload, node_id):
    for item in payload["nodes"]:
        if item["node_id"] == node_id:
            return item
    raise AssertionError(f"节点不存在: {node_id}; 现有: "
                         f"{sorted(n['node_id'] for n in payload['nodes'])[:20]}")


def edges_of(payload, etype):
    return [e for e in payload["edges"] if e["type"] == etype]


def err(code, events=None, **kw):
    kw.setdefault("as_of", AS_OF)
    with pytest.raises(graph.GraphInputError) as ei:
        graph.build_evidence_graph(
            base_events() if events is None else events, **kw)
    assert ei.value.code == code, f"期望 {code}, 实得 {ei.value.code}: {ei.value}"
    return ei.value


def walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from walk_keys(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from walk_keys(value)


# ---------------------------------------------------------------- 输入契约
def test_empty_events_refused():
    err("events_empty", [])


def test_events_container_must_be_sequence():
    err("invalid_events_container", {"event_id": "E1"})


def test_duplicate_event_id_refused():
    events = base_events() + [ev("E1", "2026-01-06", "钠电材料", "SYN-C")]
    err("duplicate_event_id", events)


def test_event_id_must_be_clean_string():
    err("invalid_event_id", [ev(" E1", "2026-01-05", "钠电材料", "SYN-A")])


def test_missing_source_refused():
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-A")]
    del events[0]["source"]
    err("invalid_source", events)


def test_future_event_date_refused():
    err("future_event_date", [ev("E1", "2026-01-13", "钠电材料", "SYN-A")])


def test_weekend_event_date_refused():
    err("weekend_date", [ev("E1", "2026-01-10", "钠电材料", "SYN-A")])


def test_event_date_must_be_in_supplied_calendar():
    # 2026-01-08 是日历里的真实交易日, 但从日历中剔掉后必须拒绝而不是猜
    partial = [d for d in SYNTH_CALENDAR if d != "2026-01-08"]
    err("event_date_not_in_calendar",
        [ev("E1", "2026-01-08", "钠电材料", "SYN-A")],
        calendar=partial)


def test_theme_text_must_be_split():
    err("theme_text_not_split", [ev("E1", "2026-01-05", "钠电材料+硅碳负极", "SYN-A")])


def test_theme_text_must_be_clean_string():
    err("invalid_theme_text", [ev("E1", "2026-01-05", " 钠电材料 ", "SYN-A")])


def test_theme_text_cannot_be_empty():
    err("invalid_theme_text", [ev("E1", "2026-01-05", "", "SYN-A")])


def test_reserved_separator_in_event_id_refused():
    err("reserved_separator_in_event_id",
        [ev("E1|X", "2026-01-05", "钠电材料", "SYN-A")])


def test_reserved_separator_in_theme_text_refused():
    err("reserved_separator_in_theme_text",
        [ev("E1", "2026-01-05", "钠电|材料", "SYN-A")])


def test_reserved_separator_in_instrument_id_refused():
    err("reserved_separator_in_instrument_id",
        [ev("E1", "2026-01-05", "钠电材料", "SYN|A")])


def test_missing_instrument_refused():
    events = [{"event_id": "E1", "occurred_on": "2026-01-05",
               "theme_text": "钠电材料", "source": "src-a"}]
    err("missing_instrument", events)


def test_missing_identity_status_refused():
    events = base_events()
    del events[0]["instrument"]["identity_status"]
    err("missing_identity_status", events)


def test_unknown_identity_status_string_refused():
    events = base_events()
    events[0]["instrument"]["identity_status"] = "probably"
    err("invalid_identity_status", events)


def test_verified_identity_requires_basis():
    err("missing_identity_basis",
        [ev("E1", "2026-01-05", "钠电材料", "SYN-A", basis=None)])


def test_instrument_id_must_not_be_normalized():
    err("invalid_instrument_id", [ev("E1", "2026-01-05", "钠电材料", " SYN-A ")])


def test_instrument_id_alias_conflict_refused():
    events = base_events()
    events[0]["instrument"]["code"] = "OTHER"
    err("instrument_id_conflict", events)


def test_invalid_as_of_refused():
    err("invalid_as_of", as_of="2026-1-12")


def test_invalid_evidence_cap_refused():
    err("invalid_evidence_cap", evidence_cap=0)


def test_invalid_generated_at_refused():
    err("invalid_generated_at", generated_at="2026-01-12 10:00")


def test_future_published_at_refused():
    err("future_published_at",
        [ev("E1", "2026-01-05", "钠电材料", "SYN-A",
            published_at="2026-01-20T09:00:00")])


def test_malformed_date_string_refused():
    err("invalid_date_format", [ev("E1", "2026/01/05", "钠电材料", "SYN-A")])
    err("invalid_date_format", [ev("E1", "20260231", "钠电材料", "SYN-A")])


def test_event_must_be_mapping():
    err("invalid_event", ["E1", "2026-01-05"])


def test_instrument_name_must_be_string():
    err("invalid_instrument_name",
        [ev("E1", "2026-01-05", "钠电材料", "SYN-A", name=123)])


def test_identity_basis_must_be_string_when_given():
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-A",
                 status="unverified", basis=123)]
    err("invalid_identity_basis", events)


def test_published_at_must_be_iso_or_unknown():
    err("invalid_published_at",
        [ev("E1", "2026-01-05", "钠电材料", "SYN-A", published_at=20260105)])
    err("invalid_published_at",
        [ev("E1", "2026-01-05", "钠电材料", "SYN-A", published_at="05/01/2026")])


def test_reference_shapes_are_validated():
    err("invalid_reference",
        [ev("E1", "2026-01-05", "钠电材料", "SYN-A", reference="snap.json")])
    err("invalid_reference_row",
        [ev("E1", "2026-01-05", "钠电材料", "SYN-A",
            reference={"file": "snap.json", "row": -1})])
    err("invalid_reference_sha256",
        [ev("E1", "2026-01-05", "钠电材料", "SYN-A",
            reference={"file": "snap.json", "row": 1, "sha256": "abc"})])


# ---------------------------------------------------------------- 交易日历
def test_duplicate_calendar_date_refused():
    err("duplicate_calendar_date",
        calendar=SYNTH_CALENDAR + ["2026-01-06"])


def test_calendar_must_be_ascending():
    err("calendar_not_ascending",
        calendar=["2026-01-06", "2026-01-05"])


def test_calendar_weekend_entry_refused():
    err("calendar_weekend_entry",
        calendar=SYNTH_CALENDAR + ["2026-01-10"])


def test_calendar_dates_after_as_of_are_excluded_explicitly():
    payload = build(calendar=SYNTH_CALENDAR + ["2026-01-13"])
    cal = payload["coverage"]["calendar"]
    assert cal["provided"] is True
    assert cal["excluded_after_as_of"] == 1
    assert cal["effective_entries"] == len(SYNTH_CALENDAR)
    assert payload["coverage"]["continuity"] == "relative_to_supplied_calendar"


def test_calendar_container_and_emptiness_are_validated():
    err("invalid_calendar", calendar="2026-01-05")
    err("calendar_empty", calendar=[])
    err("invalid_calendar_date", calendar=["2026/01/05"])
    err("calendar_empty_until_as_of", calendar=["2026-01-13"])


# ---------------------------------------------------------------- K线输入
def test_invalid_kline_value_refused():
    err("invalid_kline_value",
        klines=[bar("SYN-A", "2026-01-05", h=9.0, lo=10.5)])


def test_duplicate_kline_refused():
    err("duplicate_kline",
        klines=[bar("SYN-A", "2026-01-05"), bar("SYN-A", "2026-01-05")])


def test_kline_date_outside_calendar_refused():
    err("bar_date_not_in_calendar",
        calendar=[d for d in SYNTH_CALENDAR if d != "2026-01-09"],
        klines=[bar("SYN-A", "2026-01-09")])


def test_kline_shapes_are_validated():
    err("invalid_klines_container", klines="2026-01-05")
    err("invalid_kline", klines=[1])
    err("invalid_kline_value", klines=[bar("SYN-A", "2026-01-05", o=0, lo=0, c=0)])
    err("invalid_kline_value", klines=[bar("SYN-A", "2026-01-05", v=-1)])
    err("invalid_kline_value", klines=[bar("SYN-A", "2026-01-05", v="1000")])
    err("invalid_kline_value", klines=[bar("SYN-A", "2026-01-05", c=float("nan"))])
    err("invalid_kline_instrument_id",
        klines=[{"id": " SYN-A", "date": "2026-01-05", "open": 1, "high": 1,
                 "low": 1, "close": 1, "volume": 0}])
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-A")]
    events[0]["instrument"]["code"] = "SYN-B"
    err("instrument_id_conflict", events)


def test_generated_at_is_passed_through_verbatim():
    payload = build(calendar=SYNTH_CALENDAR, generated_at="2026-01-12T18:00:00+08:00")
    assert payload["provenance"]["created_at"] == "2026-01-12T18:00:00+08:00"


def test_future_kline_refused():
    err("future_kline_date", klines=[bar("SYN-A", "2026-01-13")])


# ---------------------------------------------------------------- 节点
def test_all_four_node_kinds_present():
    payload = build(calendar=SYNTH_CALENDAR)
    kinds = {n["kind"] for n in payload["nodes"]}
    assert kinds == {"cycle", "theme", "instrument", "event"}
    assert set(kinds) <= set(graph.NODE_KINDS)


def test_node_counts_match_nodes():
    payload = build(calendar=SYNTH_CALENDAR)
    for kind, count in payload["node_counts"].items():
        assert count == len(nodes_of(payload, kind))
    assert sum(payload["node_counts"].values()) == len(payload["nodes"])


def test_event_node_preserves_raw_facts():
    payload = build(calendar=SYNTH_CALENDAR)
    item = node(payload, "event:E1")
    assert item["date"] == "2026-01-05"
    assert item["source"] == "src-a"
    assert item["theme_text"] == "钠电材料"
    assert item["instrument"]["id"] == "SYN-A"
    assert item["instrument"]["identity_status"] == "verified"
    assert item["identity_resolved"] is True
    assert item["published_at"] == "unknown"
    assert item["visibility"] == "published_at_unknown"


def test_theme_node_keeps_exact_raw_label():
    payload = build(calendar=SYNTH_CALENDAR)
    item = node(payload, "theme:钠电材料")
    assert item["label"] == "钠电材料"
    assert item["raw_label"] == "钠电材料"
    assert item["event_count"] == 3
    assert item["sources"] == {"src-a": 3}


def test_similar_but_distinct_theme_text_not_merged():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
        ev("E2", "2026-01-06", "钠电材料设备", "SYN-A"),
        ev("E3", "2026-01-07", "钠电池", "SYN-A"),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    labels = sorted(n["label"] for n in nodes_of(payload, "theme"))
    assert labels == sorted(["钠电材料", "钠电材料设备", "钠电池"])
    ids = {e["edge_id"] for e in payload["edges"]}
    assert "theme_copresent_same_instrument_same_day|theme:钠电材料|theme:钠电材料设备" not in ids
    assert "theme_copresent_same_instrument_same_day|theme:钠电材料|theme:钠电池" not in ids


def test_same_display_name_distinct_ids_not_merged():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A", name="同名标的"),
        ev("E2", "2026-01-05", "硅碳负极", "SYN-B", name="同名标的"),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    instruments = nodes_of(payload, "instrument")
    assert sorted(i["id"] for i in instruments) == ["SYN-A", "SYN-B"]
    assert payload["diagnostics"]["shared_display_names"] == [
        {"name": "同名标的", "instrument_ids": ["SYN-A", "SYN-B"]}]
    assert not edges_of(payload, "theme_copresent_same_instrument_same_day")


def test_unverified_identity_never_reaches_instrument_node():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
        ev("E2", "2026-01-05", "钠电材料", "SYN-U", status="unverified", basis=None),
        ev("E3", "2026-01-06", "钠电材料", "SYN-K", status="unknown", basis=None),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    assert [i["id"] for i in nodes_of(payload, "instrument")] == ["SYN-A"]
    assert not edges_of(payload, "event_on_instrument") or all(
        "SYN-A" in (e["src"] + e["dst"]) for e in edges_of(payload, "event_on_instrument"))
    assert node(payload, "event:E2")["instrument"]["node_id"] is None
    assert node(payload, "event:E2")["identity_resolved"] is False
    ids = {row["event_id"] for row in
           payload["unresolved"]["events_without_verified_identity"]}
    assert ids == {"E2", "E3"}
    assert payload["coverage"]["unknowns"]["identity_unverified"] == 1
    assert payload["coverage"]["unknowns"]["identity_unknown"] == 1


def test_unverified_identity_does_not_launder_into_verified_code():
    # 同一原始代码先以 verified 出现, 再以 unverified 出现: 后者不接任何标的节点
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
        ev("E2", "2026-01-06", "钠电材料", "SYN-A", status="unverified", basis=None),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    assert len(nodes_of(payload, "instrument")) == 1
    assert node(payload, "event:E2")["instrument"]["node_id"] is None
    conflicts = payload["diagnostics"]["identity_status_conflicts"]
    assert conflicts == [{"instrument_id": "SYN-A",
                          "statuses": ["unverified", "verified"],
                          "has_verified_node": True}]
    touched = {e["src"] for e in edges_of(payload, "event_on_instrument")}
    assert touched == {"event:E1"}


def test_branch_conflict_is_not_resolved_silently():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A", branch="电池"),
        ev("E2", "2026-01-06", "钠电材料", "SYN-A", branch="新材料"),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    item = node(payload, "theme:钠电材料")
    assert item["branch"] is None
    assert item["branch_status"] == "conflicting_declarations"
    assert payload["diagnostics"]["branch_conflicts"] == [
        {"theme_text": "钠电材料", "branches": ["新材料", "电池"]}]


def test_branch_unknown_is_explicit():
    payload = build(calendar=SYNTH_CALENDAR)
    assert node(payload, "theme:固态电池")["branch"] == "电池"
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-A")]
    plain = build(events, calendar=SYNTH_CALENDAR)
    assert node(plain, "theme:钠电材料")["branch"] is None
    assert node(plain, "theme:钠电材料")["branch_status"] == "unknown"
    assert plain["coverage"]["unknowns"]["theme_branch_unknown"] == 1


def test_branch_summary_only_reports_declared_grouping():
    payload = build(calendar=SYNTH_CALENDAR)
    branches = payload["branches"]
    assert branches["inference"] == "not_performed"
    assert branches["declared"] == [
        {"branch": "电池", "themes": ["固态电池", "硅碳负极", "钠电材料"],
         "theme_count": 3, "events": 5}]
    assert branches["unknown"] == []
    assert branches["conflicting"] == []


def test_branch_summary_keeps_unknown_and_conflicting_out():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A", branch="电池"),
        ev("E2", "2026-01-06", "钠电材料", "SYN-A", branch="新材料"),
        ev("E3", "2026-01-06", "固态电池", "SYN-B"),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    branches = payload["branches"]
    assert branches["declared"] == []
    assert branches["conflicting"] == ["钠电材料"]
    assert branches["unknown"] == ["固态电池"]


def test_theme_merges_exact_text_across_sources_but_records_both():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A", source="src-a"),
        ev("E2", "2026-01-06", "钠电材料", "SYN-B", source="src-b"),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    item = node(payload, "theme:钠电材料")
    assert item["source_count"] == 2
    assert item["sources"] == {"src-a": 1, "src-b": 1}
    assert payload["diagnostics"]["themes_with_multiple_sources"] == ["钠电材料"]


# ---------------------------------------------------------------- 边
def test_event_in_theme_edge_is_exact_text_only():
    payload = build(calendar=SYNTH_CALENDAR)
    edges = edges_of(payload, "event_in_theme")
    assert len(edges) == 5
    assert all(e["semantics"] == "EXACT_RAW_TEXT_MEMBERSHIP" for e in edges)
    assert {e["edge_id"] for e in edges} >= {"event_in_theme|event:E1|theme:钠电材料"}


def test_event_on_instrument_edges_only_for_verified():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
        ev("E2", "2026-01-05", "钠电材料", "SYN-U", status="unverified", basis=None),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    edges = edges_of(payload, "event_on_instrument")
    assert [e["edge_id"] for e in edges] == ["event_on_instrument|event:E1|instrument:SYN-A"]
    assert edges[0]["semantics"] == "EXPLICIT_VERIFIED_IDENTITY"


def test_theme_on_instrument_support_counts_dates():
    payload = build(calendar=SYNTH_CALENDAR)
    edges = {(e["src"], e["dst"]): e
             for e in edges_of(payload, "theme_observed_on_instrument")}
    edge = edges[("theme:钠电材料", "instrument:SYN-A")]
    assert edge["support"]["events"] == 2
    assert edge["support"]["dates"] == 2
    assert edge["support"]["first_seen"] == "2026-01-05"
    assert edge["support"]["last_seen"] == "2026-01-06"


def test_copresence_requires_same_instrument_same_day():
    payload = build(calendar=SYNTH_CALENDAR)
    edges = edges_of(payload, "theme_copresent_same_instrument_same_day")
    assert len(edges) == 1
    edge = edges[0]
    assert edge["semantics"] == "SAME_DAY_CO_PRESENCE_ON_VERIFIED_INSTRUMENT"
    assert edge["support"]["instruments"] == 1
    assert edge["support"]["dates"] == 1
    assert edge["support"]["co_occurrences"] == 1
    assert edge["support"]["sources"] == ["src-a"]
    assert edge["support"]["crosses_sources"] is False
    assert edge["evidence"][0]["instrument_id"] == "SYN-A"
    assert edge["evidence"][0]["date"] == "2026-01-05"
    assert sorted(edge["evidence"][0]["event_ids"]) == ["E1", "E2"]


def test_copresence_aggregates_per_pair_with_unique_ids():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
        ev("E2", "2026-01-05", "硅碳负极", "SYN-A"),
        ev("E3", "2026-01-06", "钠电材料", "SYN-B"),
        ev("E4", "2026-01-06", "硅碳负极", "SYN-B"),
        ev("E5", "2026-01-06", "钠电材料", "SYN-C"),
        ev("E6", "2026-01-06", "硅碳负极", "SYN-C"),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    edges = edges_of(payload, "theme_copresent_same_instrument_same_day")
    assert len(edges) == 1  # 同一对主题只有一条边, 不按标的/日期重复
    edge = edges[0]
    assert edge["support"]["co_occurrences"] == 3
    assert edge["support"]["dates"] == 2
    assert edge["support"]["instruments"] == 3
    assert edge["support"]["first_seen"] == "2026-01-05"
    assert edge["support"]["last_seen"] == "2026-01-06"
    assert len(edge["evidence"]) == 3


def test_node_and_edge_ids_are_unique():
    payload = build(calendar=SYNTH_CALENDAR)
    node_ids = [n["node_id"] for n in payload["nodes"]]
    edge_ids = [e["edge_id"] for e in payload["edges"]]
    assert len(node_ids) == len(set(node_ids))
    assert len(edge_ids) == len(set(edge_ids))


def test_copresence_across_sources_is_flagged():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A", source="src-a"),
        ev("E2", "2026-01-05", "硅碳负极", "SYN-A", source="src-b"),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    edge = edges_of(payload, "theme_copresent_same_instrument_same_day")[0]
    assert edge["support"]["sources"] == ["src-a", "src-b"]
    assert edge["support"]["crosses_sources"] is True


def test_every_edge_declares_what_it_does_not_claim():
    payload = build(calendar=SYNTH_CALENDAR)
    assert payload["edges"]
    for edge in payload["edges"]:
        assert edge["not_claimed"] == list(graph.NOT_CLAIMED)
        assert "causality" in edge["not_claimed"]
        assert "capital_flow" in edge["not_claimed"]
        assert "mainline_status" in edge["not_claimed"]
        assert edge["semantics"] in graph.EDGE_SEMANTICS.values()


def test_evidence_cap_is_reported_not_silent():
    events = [ev(f"E{i}", "2026-01-05", "钠电材料", "SYN-A") for i in range(4)]
    payload = build(events, calendar=SYNTH_CALENDAR, evidence_cap=2)
    edge = edges_of(payload, "theme_observed_on_instrument")[0]
    assert edge["evidence_total"] == 4
    assert len(edge["evidence"]) == 2
    assert edge["evidence_truncated"] is True


# ---------------------------------------------------------------- 分段与周期
def test_consecutive_days_form_one_verified_window():
    payload = build(calendar=SYNTH_CALENDAR)
    assert payload["coverage"]["continuity"] == "relative_to_supplied_calendar"
    assert len(payload["segments"]) == 1
    cycle_nodes = nodes_of(payload, "cycle")
    assert len(cycle_nodes) == 1
    cycle = cycle_nodes[0]
    assert cycle["window"]["start"] == "2026-01-05"
    assert cycle["window"]["end"] == "2026-01-07"
    assert cycle["window"]["observed_trading_days"] == 3
    assert cycle["window"]["trading_days"] == 3
    assert cycle["interpretation"] == "OBSERVATION_WINDOW_ONLY"
    assert cycle["market_cycle_claim"] == "not_asserted"


def test_gap_breaks_segment_and_reports_break():
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
              ev("E2", "2026-01-07", "钠电材料", "SYN-B")]
    payload = build(events, calendar=SYNTH_CALENDAR)
    assert len(payload["segments"]) == 2
    second = payload["segments"][1]
    assert second["break_after_previous"]["trading_days_between"] == 2
    assert second["break_after_previous"]["unobserved_trading_days"] == ["2026-01-06"]
    assert second["break_after_previous"]["coverage_gap_possible"] is True
    assert len(nodes_of(payload, "cycle")) == 2
    link = edges_of(payload, "cycle_precedes_cycle")
    assert len(link) == 1
    assert link[0]["semantics"] == "TEMPORAL_ORDER_ONLY"
    assert link[0]["support"]["trading_days_between"] == 2
    assert link[0]["support"]["unobserved_trading_days_between"] == 1


def test_holiday_gap_in_calendar_does_not_break_window():
    # 日历里 01-06~01-08 是休市(非交易日): 01-05 与 01-09 在交易日意义上相邻,
    # 因此属于同一观测窗口 —— 分段依据是日历, 不是自然日间隔。
    holiday_calendar = ["2026-01-05", "2026-01-09", "2026-01-12"]
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
              ev("E2", "2026-01-09", "钠电材料", "SYN-A")]
    payload = build(events, calendar=holiday_calendar)
    assert len(payload["segments"]) == 1
    seg = payload["segments"][0]
    assert seg["window_trading_days"] == 2
    assert seg["unobserved_trading_day_count"] == 0
    assert payload["coverage"]["unobserved_trading_days_in_span"]["count"] == 0


def test_code_alias_accepted_where_id_absent():
    events = [{"event_id": "E1", "occurred_on": "2026-01-05",
               "theme_text": "钠电材料", "source": "src-a",
               "instrument": {"code": "SYN-A", "identity_status": "verified",
                              "identity_basis": "caller-cross-check"}}]
    payload = build(events, calendar=SYNTH_CALENDAR,
                    klines=[{"code": "SYN-A", "date": "2026-01-05", "open": 1,
                             "high": 1, "low": 1, "close": 1, "volume": 0,
                             "cross_source_identity_basis": "synthetic-test-only"}])
    assert node(payload, "instrument:SYN-A")["id"] == "SYN-A"
    assert node(payload, "event:E1")["bar_present"] is True


def test_missing_calendar_never_claims_continuity():
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
              ev("E2", "2026-01-12", "钠电材料", "SYN-A")]
    payload = build(events)
    assert payload["coverage"]["continuity"] == "unknown"
    assert payload["coverage"]["calendar"]["provided"] is False
    assert payload["coverage"]["unobserved_trading_days_in_span"]["count"] is None
    assert payload["coverage"]["unobserved_trading_days_in_span"]["requires"] == "trading_calendar"
    assert len(payload["segments"]) == 1
    seg = payload["segments"][0]
    assert seg["continuity"] == "unknown"
    assert seg["window_trading_days"] is None
    assert seg["break_after_previous"] is None
    assert payload["cycles"]["status"] == "unavailable_missing_calendar"
    assert not nodes_of(payload, "cycle")
    assert not edges_of(payload, "cycle_contains_theme")
    assert not edges_of(payload, "cycle_precedes_cycle")


def test_missing_calendar_suppression_is_counted():
    payload = build()
    suppressed = payload["diagnostics"]["suppressed_relations"]
    assert suppressed["cycle_contains_theme"]["count"] == 3  # 三个不同的主题
    assert suppressed["cycle_contains_theme"]["reason"] == "requires_trading_calendar"
    assert suppressed["cycle_contains_instrument"]["count"] == 2
    assert suppressed["cycle_precedes_cycle"]["count"] == 0


def test_calendar_makes_unobserved_trading_days_visible():
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
              ev("E2", "2026-01-12", "钠电材料", "SYN-A")]
    payload = build(events, calendar=SYNTH_CALENDAR)
    coverage = payload["coverage"]
    assert coverage["unobserved_trading_days_in_span"]["count"] == 4
    assert coverage["unobserved_trading_days_in_span"]["dates"] == [
        "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    assert len(payload["segments"]) == 2


def test_window_membership_reports_recurrence_without_persistence_claim():
    payload = build(calendar=SYNTH_CALENDAR)
    edges = edges_of(payload, "cycle_contains_theme")
    recurrence = {e["dst"]: e["support"]["recurrence_within_window"] for e in edges}
    assert recurrence["theme:钠电材料"] is True
    assert recurrence["theme:固态电池"] is False
    for edge in edges:
        assert "theme_persistence" in edge["not_claimed"]
        assert "mainline_status" in edge["not_claimed"]


def test_theme_spanning_two_windows_is_flagged_not_linked():
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
              ev("E2", "2026-01-07", "钠电材料", "SYN-B")]
    payload = build(events, calendar=SYNTH_CALENDAR)
    assert payload["diagnostics"]["themes_spanning_segment_breaks"] == [
        {"theme_text": "钠电材料", "segment_ids": ["seg-0001", "seg-0002"]}]
    theme = node(payload, "theme:钠电材料")
    assert theme["crosses_segment_break"] is True
    # 不允许出现跨窗口的"持续/接力"边: 每个窗口各一条成员边, 没有第三个主题边
    assert len(edges_of(payload, "cycle_contains_theme")) == 2


# ---------------------------------------------------------------- unknown 覆盖
def test_unknown_coverage_counts_are_explicit():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
        ev("E2", "2026-01-06", "钠电材料", "SYN-U", status="unverified", basis=None),
        ev("E3", "2026-01-07", "固态电池", "SYN-K", status="unknown", basis=None,
           published_at="2026-01-07T08:30:00"),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    unknowns = payload["coverage"]["unknowns"]
    assert unknowns["published_at_unknown"] == 2
    assert unknowns["published_at_known"] == 1
    assert unknowns["identity_unverified"] == 1
    assert unknowns["identity_unknown"] == 1
    assert unknowns["theme_branch_unknown"] == 2
    assert unknowns["suspension_status"] == "unknown"
    assert unknowns["price_adjustment"] == "unknown"
    assert unknowns["klines"] == "not_provided"


def test_bar_presence_is_evidence_not_price():
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-A"),
              ev("E2", "2026-01-06", "钠电材料", "SYN-A")]
    payload = build(events, calendar=SYNTH_CALENDAR,
                    klines=[bar("SYN-A", "2026-01-05")])
    assert node(payload, "event:E1")["bar_present"] is True
    assert node(payload, "event:E1")["bar_identity_association"] == "caller_declared_unverified"
    assert node(payload, "event:E1")["bar_identity_basis"] == "synthetic-test-only"
    assert node(payload, "event:E2")["bar_present"] is None
    assert node(payload, "event:E2")["bar_identity_association"] == "unknown"
    assert payload["coverage"]["unknowns"]["klines"] == "provided"
    assert [row["event_id"] for row in payload["unresolved"]["events_without_bar"]] == ["E2"]
    assert payload["unresolved"]["events_without_bar"][0]["reason"] == "bar_or_cross_source_identity_unknown"
    dumped = json.dumps(payload, ensure_ascii=False).lower()
    for price_key in ('"close":', '"open":', '"high":', '"low":', '"volume":',
                      '"pct_chg":', '"change_pct":', "涨跌幅"):
        assert price_key not in dumped


def test_klines_without_verified_instrument_are_reported():
    payload = build(calendar=SYNTH_CALENDAR,
                    klines=[bar("SYN-A", "2026-01-05"), bar("SYN-Z", "2026-01-05")])
    unmatched = payload["unresolved"]["klines_without_instrument"]
    assert [row["instrument_id"] for row in unmatched] == ["SYN-Z"]
    assert unmatched[0]["reason"] == "identity_not_verified_by_events"
    assert unmatched[0]["bars"] == 1


def test_unverified_event_gets_no_bar_verdict():
    events = [ev("E1", "2026-01-05", "钠电材料", "SYN-U", status="unverified", basis=None)]
    payload = build(events, calendar=SYNTH_CALENDAR, klines=[bar("SYN-U", "2026-01-05")])
    assert node(payload, "event:E1")["bar_present"] is None
    assert payload["unresolved"]["klines_without_instrument"][0]["instrument_id"] == "SYN-U"


def test_duplicate_observation_is_flagged_not_merged():
    events = [
        ev("E1", "2026-01-05", "钠电材料", "SYN-A",
           reference={"file": "snap.json", "row": 7}),
        ev("E2", "2026-01-05", "钠电材料", "SYN-A",
           reference={"file": "snap.json", "row": 7}),
    ]
    payload = build(events, calendar=SYNTH_CALENDAR)
    assert payload["node_counts"]["event"] == 2  # 不静默合并, 由调用者判断
    flagged = payload["diagnostics"]["duplicate_observation_candidates"]
    assert flagged == [{"date": "2026-01-05", "instrument_id": "SYN-A",
                        "theme_text": "钠电材料", "source": "src-a",
                        "reference_file": "snap.json", "reference_row": 7,
                        "count": 2}]


# ---------------------------------------------------------------- 纯函数与门禁
def test_inputs_are_not_mutated():
    events = base_events()
    klines = [bar("SYN-A", "2026-01-05")]
    calendar = list(SYNTH_CALENDAR)
    before = copy.deepcopy((events, klines, calendar))
    build(events, klines=klines, calendar=calendar)
    assert (events, klines, calendar) == before


def test_output_is_deterministic_and_json_serializable():
    first = build(calendar=SYNTH_CALENDAR)
    second = build(calendar=SYNTH_CALENDAR)
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == \
        json.dumps(second, ensure_ascii=False, sort_keys=True)
    assert json.loads(json.dumps(first, ensure_ascii=False))["schema_version"] == graph.SCHEMA_VERSION


def test_no_direction_or_causal_keys_anywhere():
    payload = build(calendar=SYNTH_CALENDAR)
    # claims 是本层唯一写明"否认哪些概念"的区块, 其余位置不得出现这些字段名
    body = {k: v for k, v in payload.items() if k != "claims"}
    leaked = {key for key in walk_keys(body) if key in graph.FORBIDDEN_OUTPUT_KEYS}
    assert not leaked, leaked
    assert payload["claims"]["capital_flow"] == "not_used"
    assert payload["claims"]["causality"] == "not_inferred"
    assert payload["claims"]["buy_sell"] == "not_provided"
    assert payload["claims"]["mainline_status"] == "not_asserted"
    assert payload["claims"]["market_cycle"] == "not_asserted"


def test_trading_wording_gate():
    graph.assert_no_trading_wording("同日同标的共现, 仅为观察记录。")
    with pytest.raises(ValueError):
        graph.assert_no_trading_wording("建议买入该股")


def test_module_has_no_hardcoded_historical_case():
    source = pathlib.Path(inspect.getfile(graph)).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # 跳过模块文档串里的示例说明
    for word in ("新华", "九阳", "目标价", "止损"):
        assert word not in body, f"模块实现里出现历史案例/交易措辞: {word}"
    assert re.search(r"mainline_claim|主线判定", body) is None


def test_limitations_are_returned_with_payload():
    payload = build(calendar=SYNTH_CALENDAR)
    assert payload["limitations"] == list(graph.LIMITATIONS)
    text = "".join(payload["limitations"])
    assert "交易日历" in text
    assert "身份" in text


# ---------------------------------------------------------------- 真实文件(去标识)
def read_calendar_fixture():
    days = []
    for line in CAL_FIXTURE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        days.append(line)
    return days


def test_real_calendar_fixture_drives_verified_windows():
    days = read_calendar_fixture()
    assert len(days) > 100
    tail = days[-3:]
    events = [ev(f"E{i}", day, "去标识主题", "REAL-CAL-1", name="去标识标的")
              for i, day in enumerate(tail)]
    payload = build(events, calendar=days, as_of=tail[-1])
    assert payload["coverage"]["calendar"]["provided"] is True
    assert payload["coverage"]["continuity"] == "relative_to_supplied_calendar"
    assert len(payload["segments"]) == 1
    assert nodes_of(payload, "cycle")[0]["window"]["trading_days"] == len(tail)
    assert payload["coverage"]["unobserved_trading_days_in_span"]["count"] == 0
    for item in nodes_of(payload, "event"):
        assert item["date"] in days


def _deidentified_rows(limit=6):
    """从真实供应商快照取有限行并去标识: 代码/名称替换, 标签原文保留。"""
    path = REAL_DIR / "hist_window_jan" / "20260105.json"
    if not path.exists():
        pytest.skip(f"缺少真实快照: {path}")
    obj = json.loads(path.read_text(encoding="utf-8"))
    data = (obj.get("raw_body") or {}).get("data") or {}
    rows = data.get("info") or []
    checks = obj.get("checks") or {}
    if not rows or checks.get("rows_equals_total") is not True:
        pytest.skip("真实快照不满足分页完整性断言")
    out = []
    for idx, row in enumerate(rows[:limit]):
        labels = [x.strip() for x in re.split(r"[+＋]", row.get("reason_type") or "")
                  if x.strip()]
        if not labels:
            continue
        out.append({
            "row": idx,
            "date": str(data.get("date")),
            "labels": sorted(set(labels)),
            "raw_reason": row.get("reason_type"),
            "instrument_id": f"R{idx:03d}",
            "instrument_name": f"标的{idx:02d}",
        })
    return path, out, checks


def test_real_snapshot_rows_become_traceable_events():
    path, rows, checks = _deidentified_rows()
    if not rows:
        pytest.skip("真实快照无可解析标签行")
    events = []
    for row in rows:
        for j, label in enumerate(row["labels"]):
            events.append(ev(f"R{row['row']:03d}-L{j}", row["date"], label,
                             row["instrument_id"], name=row["instrument_name"],
                             source="deidentified-vendor-snapshot",
                             reference={"file": str(path.name), "row": row["row"]}))
    payload = build(events, calendar=read_calendar_fixture(), as_of=rows[0]["date"])

    assert payload["node_counts"]["event"] == len(events)
    assert payload["node_counts"]["instrument"] == len(rows)
    # 标签原文逐字保留, 与源文件切分结果完全一致 (去标识不动标签)
    themes = sorted(n["label"] for n in nodes_of(payload, "theme"))
    assert themes == sorted({lb for row in rows for lb in row["labels"]})
    # 每个事件都能追回文件名与行号
    for item in nodes_of(payload, "event"):
        assert item["reference"]["file"] == path.name
        assert isinstance(item["reference"]["row"], int)
    # 跨行同名标签只按原文合并, 不按相似度
    for theme in nodes_of(payload, "theme"):
        assert theme["label"] == theme["raw_label"]
    assert payload["coverage"]["continuity"] == "relative_to_supplied_calendar"
    assert checks.get("duplicate_count") == 0
