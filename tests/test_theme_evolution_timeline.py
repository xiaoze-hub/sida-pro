"""题材演化时间线(纯函数)验收: 精确日期落点、缺K线清单、引用溯源、fail-closed。

这一层只做**事后描述**: 调用者传入已验证的历史涨停原因标签事件与日K线,
函数把它们按日期排成时间线, 只在**日期完全相同**时落到K线, 落不到的单列清单。
不插值、不跨源猜代码映射、不产生任何方向/收益字段。

测试重点在"会不会编": 日期写错、重复、引用缺失、K线数值无效一律抛错;
缺K线不许用邻近日补齐; 代码不许自动补 `.SZ/.SH` 后缀。
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import pathlib

import pytest

from src.core import theme_evolution_timeline as tl

SHA = "0f" * 32  # 64 位十六进制, 与真实文件无关的占位摘要

#: 项目外只读研究目录(真实样本); 不存在时相关用例跳过, 不伪造数据。
REAL_DIR = pathlib.Path("/home/ubuntu/mainline-evolution-research")


# ---------------------------------------------------------------- 夹具构造
def ev(date, code, labels=("题材甲",), *, line=1, sha=SHA, source="src-vendor",
       name=None, **kw):
    out = {
        "date": date, "code": code, "labels": list(labels), "source": source,
        "reference": {"file": f"snap/{date}.json", "line": line, "sha256": sha},
    }
    if name is not None:
        out["name"] = name
    out.update(kw)
    return out


def bar(code, date, *, o=10.0, h=10.5, lo=9.8, c=10.2, v=1000):
    return {"code": code, "date": date, "open": o, "high": h, "low": lo,
            "close": c, "volume": v}


def two_day_inputs():
    events = [
        ev("20261012", "SYN001", ("题材甲", "题材乙"), line=3),
        ev("20261012", "SYN002", ("题材甲",), line=4),
        ev("20261013", "SYN001", ("题材甲",), line=5),
    ]
    klines = [
        bar("SYN001", "20261012"), bar("SYN001", "20261013"),
        bar("SYN002", "20261012"),
    ]
    return events, klines


def _assert_error(code, fn, *args, **kwargs):
    with pytest.raises(tl.TimelineInputError) as ei:
        fn(*args, **kwargs)
    assert ei.value.code == code, f"期望 {code}, 实得 {ei.value.code}: {ei.value}"


# ---------------------------------------------------------------- 正常路径
def test_timeline_groups_by_date_and_lands_on_exact_bar():
    events, klines = two_day_inputs()
    out = tl.build_timeline(events, klines, generated_at="2026-11-16T09:00:00+08:00")

    assert out["schema_version"] == tl.SCHEMA_VERSION
    assert [d["date"] for d in out["timeline"]] == ["2026-10-12", "2026-10-13"]
    assert [d["date_compact"] for d in out["timeline"]] == ["20261012", "20261013"]

    day1 = out["timeline"][0]
    assert day1["events_total"] == 2 and day1["bars_present"] == 2 and day1["bars_missing"] == 0
    assert [e["code"] for e in day1["events"]] == ["SYN001", "SYN002"]
    first = day1["events"][0]
    assert first["has_bar"] is True and first["code_join"] == "exact"
    assert first["kline_code"] == "SYN001"
    assert first["bar"]["close"] == 10.2 and first["bar"]["volume"] == 1000
    assert first["labels"] == ["题材甲", "题材乙"]
    assert first["reference"]["line"] == 3
    assert first["retrospective"] is True
    assert first["published_at"] == tl.PUBLISHED_AT_UNKNOWN

    assert out["landings"] == {"total": 3, "exact": 3, "mapped": 0, "unresolved": 0}
    assert out["missing_klines"] == []
    # 日度标签宽度: 同一天同一标签覆盖几只, 不可跨标签相加
    widths = {x["label"]: x["stocks"] for x in day1["labels"]}
    assert widths == {"题材甲": 2, "题材乙": 1}
    assert day1["labels"][0]["codes"] == ["SYN001", "SYN002"]


def test_provenance_marks_retrospective_and_unknown_publication():
    events, klines = two_day_inputs()
    out = tl.build_timeline(events, klines)
    p = out["provenance"]
    assert p["retrospective"] is True
    assert p["published_at"] == "unknown"
    assert p["point_in_time_verified"] is False
    assert p["source_kind"] == "post_hoc_limit_up_reason_label"
    assert p["trading_calendar"] == "not_used"
    assert p["contains_trading_advice"] is False
    assert p["contains_direction"] is False
    assert p["contains_price_target"] is False
    assert p["events"] == 3 and p["klines"] == 3
    assert isinstance(p["limits"], list) and p["limits"]
    assert out["limitations"] == p["limits"]


def test_references_keep_file_line_hash_and_source():
    events, klines = two_day_inputs()
    refs = tl.build_timeline(events, klines)["references"]
    assert len(refs) == 3
    top = refs[0]
    assert top["file"] == "snap/20261012.json"
    assert top["line"] == 3
    assert top["sha256"] == SHA
    assert top["source"] == "src-vendor"
    assert top["retrospective"] is True and top["published_at"] == "unknown"
    assert {(r["date"], r["code"]) for r in refs} == {
        ("2026-10-12", "SYN001"), ("2026-10-12", "SYN002"), ("2026-10-13", "SYN001"),
    }


def test_both_date_formats_normalize_to_same_payload():
    a_events, a_klines = two_day_inputs()
    b_events = copy.deepcopy(a_events)
    b_events[0]["date"] = "2026-10-12"
    b_events[1]["date"] = "2026-10-12"
    b_events[2]["date"] = "2026-10-13"
    b_klines = copy.deepcopy(a_klines)
    b_klines[0]["date"] = "2026-10-12"
    kw = {"generated_at": "2026-11-16T09:00:00+08:00"}
    assert tl.build_timeline(a_events, a_klines, **kw) == tl.build_timeline(b_events, b_klines, **kw)
    assert tl.parse_trade_date("20261012") == tl.parse_trade_date("2026-10-12")


def test_input_order_does_not_leak_into_output():
    events, klines = two_day_inputs()
    out_a = tl.build_timeline(events, klines)
    out_b = tl.build_timeline(list(reversed(events)), list(reversed(klines)))
    assert out_a == out_b


def test_inputs_are_not_mutated_and_output_is_deterministic():
    events, klines = two_day_inputs()
    before = copy.deepcopy((events, klines))
    first = tl.build_timeline(events, klines)
    second = tl.build_timeline(events, klines)
    assert (events, klines) == before
    assert first == second


# ---------------------------------------------------------------- 日期 fail-closed
@pytest.mark.parametrize("bad", [
    "", None, 20261012, "2026-1-2", "2026102", "20261301", "2026-13-01",
    "2026-02-30", " 20261012", "20261012 ", "2026/10/12", "20261012T00:00:00",
])
def test_invalid_date_format_fails_closed(bad):
    events, klines = two_day_inputs()
    events[0]["date"] = bad
    _assert_error("invalid_date_format", tl.build_timeline, events, klines)
    _assert_error("invalid_date_format", tl.parse_trade_date, bad)


def test_weekend_date_fails_closed():
    """周六/日不是 A 股交易日; 出现即视为日期写错(节假日无法在此判断)。"""
    events, klines = two_day_inputs()
    events[0]["date"] = "20261017"  # 2026-10-17 星期六
    _assert_error("weekend_date", tl.build_timeline, events, klines)


def test_kline_invalid_date_fails_closed():
    events, klines = two_day_inputs()
    klines[0]["date"] = "2026-9-8"
    _assert_error("invalid_date_format", tl.build_timeline, events, klines)
    klines[0]["date"] = "20261018"  # 星期日
    _assert_error("weekend_date", tl.build_timeline, events, klines)


# ---------------------------------------------------------------- 重复 fail-closed
def test_duplicate_event_same_date_and_code_fails_closed():
    events, klines = two_day_inputs()
    events.append(ev("20261012", "SYN001", ("题材丙",), line=9))
    _assert_error("duplicate_event", tl.build_timeline, events, klines)


def test_duplicate_after_date_normalization_fails_closed():
    events, klines = two_day_inputs()
    events.append(ev("2026-10-12", "SYN001", ("题材丙",), line=9))
    _assert_error("duplicate_event", tl.build_timeline, events, klines)


def test_duplicate_kline_same_code_and_date_fails_closed():
    events, klines = two_day_inputs()
    klines.append(bar("SYN001", "20261012", c=10.3))
    _assert_error("duplicate_kline", tl.build_timeline, events, klines)


def test_duplicate_label_inside_event_fails_closed():
    events, klines = two_day_inputs()
    events[0]["labels"] = ["题材甲", "题材甲"]
    _assert_error("duplicate_label", tl.build_timeline, events, klines)


def test_unsplit_vendor_label_fails_closed():
    """标签里还带 `+` 说明调用者没切分, 直接存会把原始串当成一个题材名。"""
    events, klines = two_day_inputs()
    events[0]["labels"] = ["题材甲+题材乙"]
    _assert_error("label_not_split", tl.build_timeline, events, klines)


# ---------------------------------------------------------------- 缺K线: 不插值
def test_missing_bar_on_exact_date_is_listed_not_interpolated():
    events, klines = two_day_inputs()
    # SYN002 在K线源里存在, 但 10-13 没有这天的行(相当于当日停牌/无成交)
    klines = [dict(b, date="20261013") if b["code"] == "SYN002" else b for b in klines]
    out = tl.build_timeline(events, klines)
    missing = out["missing_klines"]
    assert missing == [{"date": "2026-10-12", "code": "SYN002", "kline_code": "SYN002",
                        "reason": "no_bar_on_exact_date"}]
    day1 = out["timeline"][0]
    hit = [e for e in day1["events"] if e["code"] == "SYN002"][0]
    assert hit["has_bar"] is False and hit["bar"] is None
    assert day1["bars_present"] == 1 and day1["bars_missing"] == 1
    assert out["landings"] == {"total": 3, "exact": 2, "mapped": 0, "unresolved": 1}
    # 不许出现任何邻近日/前收/后收之类的插值字段
    flat = json.dumps(out, ensure_ascii=False)
    for token in ("prev_close", "next_close", "interpolat", "ffill", "bfill", "forward_return"):
        assert token not in flat
    # 邻近日(10-13)的K线不得被用来给 10-12 落点
    assert hit["kline_code"] == "SYN002"
    assert out["timeline"][1]["events"][0]["bar"]["close"] == 10.2


def test_unknown_code_is_not_renamed_with_guessed_suffix():
    events, klines = two_day_inputs()
    klines = [b for b in klines if b["code"] != "SYN002"]
    out = tl.build_timeline(events, klines)
    missing = out["missing_klines"][0]
    assert missing["reason"] == "code_not_in_kline_source"
    assert missing["code"] == "SYN002" and missing["kline_code"] == ""
    assert "SYN002.SZ" not in json.dumps(out, ensure_ascii=False)
    assert "SYN002.SH" not in json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------- 调用方显式映射
def test_caller_supplied_code_map_is_used_and_marks_provenance():
    events, klines = two_day_inputs()
    klines = [dict(b, code=b["code"] + ".SZ") for b in klines]
    out = tl.build_timeline(events, klines, verified_code_map={"SYN001": "SYN001.SZ"})
    land = [x for x in out["timeline"][0]["events"] if x["code"] == "SYN001"][0]
    assert land["code"] == "SYN001" and land["kline_code"] == "SYN001.SZ"
    assert land["code_join"] == "caller_supplied_map" and land["has_bar"] is True
    assert out["landings"] == {"total": 3, "exact": 0, "mapped": 2, "unresolved": 1}
    assert out["provenance"]["verified_code_map"] == {"entries": 1, "used": 1, "unused": 0}
    other = [x for x in out["timeline"][0]["events"] if x["code"] == "SYN002"][0]
    assert other["has_bar"] is False and other["code_join"] == "unresolved"


def test_code_map_must_be_injective_and_well_formed():
    events, klines = two_day_inputs()
    _assert_error("invalid_code_map", tl.build_timeline, events, klines,
                  verified_code_map={"SYN001": ""})
    _assert_error("invalid_code_map", tl.build_timeline, events, klines,
                  verified_code_map={"": "SYN001.SZ"})
    _assert_error("invalid_code_map", tl.build_timeline, events, klines,
                  verified_code_map=["SYN001"])
    _assert_error("ambiguous_code_map", tl.build_timeline, events, klines,
                  verified_code_map={"SYN001": "SYN009.SZ", "SYN002": "SYN009.SZ"})


def test_code_map_target_absent_from_klines_stays_unresolved():
    events, klines = two_day_inputs()
    events = events + [ev("20261012", "SYN003", ("题材丁",), line=6)]
    out = tl.build_timeline(events, klines, verified_code_map={"SYN003": "SYN777.SZ"})
    miss = [m for m in out["missing_klines"] if m["code"] == "SYN003"][0]
    assert miss["reason"] == "code_not_in_kline_source" and miss["kline_code"] == "SYN777.SZ"


def test_exact_code_wins_over_caller_map():
    """代码本来就在K线源里时按原码落点, 映射不覆盖已确证的身份。"""
    events, klines = two_day_inputs()
    out = tl.build_timeline(events, klines, verified_code_map={"SYN001": "SYN777.SZ"})
    land = [x for x in out["timeline"][0]["events"] if x["code"] == "SYN001"][0]
    assert land["code_join"] == "exact" and land["kline_code"] == "SYN001"
    assert out["provenance"]["verified_code_map"] == {"entries": 1, "used": 0, "unused": 1}


# ---------------------------------------------------------------- 引用/时点 fail-closed
def test_missing_or_malformed_reference_fails_closed():
    events, klines = two_day_inputs()
    broken = copy.deepcopy(events)
    broken[0].pop("reference")
    _assert_error("missing_reference", tl.build_timeline, broken, klines)

    broken = copy.deepcopy(events)
    broken[0]["reference"]["sha256"] = "abc"
    _assert_error("invalid_reference_sha256", tl.build_timeline, broken, klines)

    broken = copy.deepcopy(events)
    broken[0]["reference"]["line"] = 0
    _assert_error("invalid_reference_line", tl.build_timeline, broken, klines)

    broken = copy.deepcopy(events)
    broken[0]["reference"]["file"] = ""
    _assert_error("invalid_reference_file", tl.build_timeline, broken, klines)

    broken = copy.deepcopy(events)
    broken[0]["source"] = ""
    _assert_error("invalid_source", tl.build_timeline, broken, klines)


def test_point_in_time_claims_are_refused():
    events, klines = two_day_inputs()
    broken = copy.deepcopy(events)
    broken[0]["retrospective"] = False
    _assert_error("unverifiable_retrospective_claim", tl.build_timeline, broken, klines)

    broken = copy.deepcopy(events)
    broken[0]["published_at"] = "2026-10-12T09:30:00+08:00"
    _assert_error("unverifiable_published_at", tl.build_timeline, broken, klines)

    ok = copy.deepcopy(events)
    ok[0]["retrospective"] = True
    ok[0]["published_at"] = "unknown"
    assert tl.build_timeline(ok, klines)["provenance"]["published_at"] == "unknown"


# ---------------------------------------------------------------- K线数值 fail-closed
@pytest.mark.parametrize("patch", [
    {"open": float("nan")}, {"high": float("inf")}, {"close": -1.0},
    {"low": 0.0}, {"volume": -1}, {"high": 9.0}, {"low": 11.0},
    {"close": 11.0}, {"volume": True}, {"open": "10.0"},
])
def test_invalid_kline_values_fail_closed(patch):
    events, klines = two_day_inputs()
    klines[0].update(patch)
    _assert_error("invalid_kline_value", tl.build_timeline, events, klines)


def test_kline_identity_fields_required():
    events, klines = two_day_inputs()
    klines[0]["code"] = ""
    _assert_error("invalid_kline_code", tl.build_timeline, events, klines)


# ---------------------------------------------------------------- 结构 fail-closed
def test_empty_or_malformed_inputs_fail_closed():
    events, klines = two_day_inputs()
    _assert_error("events_empty", tl.build_timeline, [], klines)
    _assert_error("klines_empty", tl.build_timeline, events, [])
    _assert_error("invalid_events_container", tl.build_timeline, {"a": 1}, klines)
    _assert_error("invalid_klines_container", tl.build_timeline, events, {"a": 1})

    broken = copy.deepcopy(events)
    broken[0]["labels"] = []
    _assert_error("event_without_labels", tl.build_timeline, broken, klines)

    broken = copy.deepcopy(events)
    broken[0]["labels"] = "题材甲"
    _assert_error("invalid_labels_type", tl.build_timeline, broken, klines)

    broken = copy.deepcopy(events)
    broken[0]["code"] = " SYN001"
    _assert_error("invalid_event_code", tl.build_timeline, broken, klines)

    broken = copy.deepcopy(events)
    broken[0]["name"] = 12345
    _assert_error("invalid_event_name", tl.build_timeline, broken, klines)


def test_invalid_generated_at_fails_closed():
    events, klines = two_day_inputs()
    _assert_error("invalid_generated_at", tl.build_timeline, events, klines,
                  generated_at="2026-11-16 09:00")


# ---------------------------------------------------------------- 无交易建议
def test_payload_has_no_directional_or_return_fields():
    events, klines = two_day_inputs()
    out = tl.build_timeline(events, klines)

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in tl.FORBIDDEN_OUTPUT_KEYS, k
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
    walk(out)
    flat = json.dumps(out, ensure_ascii=False)
    for token in ("建议买入", "可买入", "建议卖出", "目标价", "加仓", "减仓", "必买"):
        assert token not in flat


def test_render_markdown_states_gaps_and_unknowns():
    events, klines = two_day_inputs()
    klines = [b for b in klines if b["code"] != "SYN002"]
    out = tl.build_timeline(events, klines)
    text = tl.render_markdown(out)
    assert "2026-10-12" in text and "2026-10-13" in text
    assert "缺K线" in text and "SYN002" in text
    assert "事后" in text
    assert "unknown" in text or "未知" in text
    assert "不构成投资建议" in text
    tl.assert_no_trading_wording(text)
    for token in ("建议买入", "可买入", "建议卖出", "目标价", "必买"):
        assert token not in text


def test_render_caps_label_width_line_but_payload_keeps_all_labels():
    events, klines = two_day_inputs()
    labels = [f"主题{i:02d}" for i in range(15)]
    events[0]["labels"] = labels
    out = tl.build_timeline(events, klines)
    width_labels = [x["label"] for x in out["timeline"][0]["labels"]]
    assert len(width_labels) == 16 and set(labels) <= set(width_labels)  # 载荷不截断
    text = tl.render_markdown(out)
    assert "共 16 个不同标签" in text  # 15 个新标签 + 题材乙
    assert "…另 4 个" in text


def test_vendor_label_text_is_preserved_verbatim_without_breaking_guard():
    """来源标签原文必须原样保留; 措辞门禁只约束本层自己的叙述, 不改写来源文本。"""
    events, klines = two_day_inputs()
    events[0]["labels"] = ["机构推荐"]
    out = tl.build_timeline(events, klines)
    assert out["timeline"][0]["events"][0]["labels"] == ["机构推荐"]
    text = tl.render_markdown(out)
    assert "机构推荐" in text


def test_assert_no_trading_wording_rejects_advice_phrases():
    with pytest.raises(ValueError):
        tl.assert_no_trading_wording("该题材建议买入")
    tl.assert_no_trading_wording("本报告不构成投资建议")


# ---------------------------------------------------------------- 真实样本科普检查
REAL_SNAPSHOT = REAL_DIR / "ths_history_snapshots_v2" / "20261012.json"
REAL_KLINE = REAL_DIR / "tq_daily_20260831_20261113.csv"
_real_ready = pytest.mark.skipif(
    not (REAL_SNAPSHOT.is_file() and REAL_KLINE.is_file()),
    reason="项目外真实样本不可用, 跳过(不伪造)",
)


@_real_ready
def test_real_sample_references_verify_and_no_identity_is_guessed():
    """真实样本: 摘要与文件一致、行数与源自报总数一致、代码不自动补后缀。"""
    raw = REAL_SNAPSHOT.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf8"))
    date_compact = payload["requested_date"]
    assert date_compact == REAL_SNAPSHOT.stem
    pool = payload["items"]["limit_up_pool"]["body"]["data"]
    rows = pool["info"]
    assert pool["page"]["total"] == len(rows)  # 分页未截断
    assert pool["date"] == date_compact
    assert payload["retrospective"] is True
    assert payload["historical_availability"] == "unknown"

    events = []
    for idx, row in enumerate(rows):
        labels = [x.strip() for x in str(row.get("reason_type") or "").split("+") if x.strip()]
        events.append({
            "date": date_compact, "code": row["code"], "name": row.get("name"),
            "labels": labels, "source": "同花顺 dataapi", "retrospective": True,
            "published_at": "unknown",
            "reference": {"file": str(REAL_SNAPSHOT.relative_to(REAL_DIR)),
                          "line": idx + 1, "sha256": digest},
        })

    klines = []
    with REAL_KLINE.open(encoding="utf8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["date"] != date_compact:
                continue
            klines.append({"code": row["code"], "date": row["date"],
                           "open": float(row["open"]), "high": float(row["high"]),
                           "low": float(row["low"]), "close": float(row["close"]),
                           "volume": float(row["volume_shares"])})

    out = tl.build_timeline(events, klines)
    assert out["provenance"]["events"] == len(events) == pool["page"]["total"]
    assert out["provenance"]["published_at"] == "unknown"
    assert all(r["sha256"] == digest for r in out["references"])
    assert out["timeline"][0]["date_compact"] == date_compact
    # 快照代码无交易所后缀, K线代码带后缀: 本层不得自行推断, 必须全部未落点
    assert out["landings"]["mapped"] == 0
    assert out["landings"]["exact"] == 0
    assert all(m["reason"] == "code_not_in_kline_source" for m in out["missing_klines"])
    for m in out["missing_klines"]:
        assert "." not in m["code"]
    tl.assert_no_trading_wording(tl.render_markdown(out))
