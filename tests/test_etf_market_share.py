# -*- coding: utf-8 -*-
"""市场级 ETF 日度份额替代源：隔离单测（不连生产库、不部署）。

钉住的口径（任何一条被改坏都会让"份额"数据说谎）：

1. 上交所 `TOT_VOL` 是**万份**，必须 ×1e4；深交所 `基金规模(份)` 是**份**，不换算。
2. 输出里**没有**金额/市值字段，单位一律「份」——份额不等于金额。
3. 缺失/空结果/日期不符/重复冲突 → gap + `usable=False`，**绝不补 0**；
   日期必须严格解析（`YYYY-MM-DD` / `YYYYMMDD` 且是真实日历日），源里别的日子必须过滤。
4. 沪深**两个市场强制在场**：缺一个就是 gap，合计不得标完整。
5. 新上市与终止必须从"可比口径"里剔除并单列；恒等式要闭合。
6. 只有**相邻交易日**的份额差才允许叫"净申购"；跨日/跨节假日区间只是份额差。
7. 免费交易所接口不含货币 ETF，必须在报告里显式说明缺口，且不得称完整全市场。
8. 模块是纯只读：不 import web/DB 层，没有任何写库/写文件路径。
9. CLI 在报告不完整时必须返回非 0。
"""

from __future__ import annotations

import ast
import io
import json
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collectors import etf_market_share as m  # noqa: E402

MODULE_PATH = ROOT / "src" / "collectors" / "etf_market_share.py"


# ── 夹具：真实响应形状（字段名照抄实测，不臆造）──────────────────────────

def _sse_row(code: str, tot_vol: str, name: str = "测试ETF",
             etf_type: str = "单市", stat_date: str = "2026-11-09") -> dict:
    return {"STAT_DATE": stat_date, "ETF_TYPE": etf_type, "SEC_CODE": code,
            "NUM": "1", "SEC_NAME": name, "TOT_VOL": tot_vol}


def _sse(rows: list[dict], stat_date: str | None = None) -> dict:
    # 真实响应每行都带 STAT_DATE；给 stat_date 时按它覆盖（模拟"源内日期"），
    # 不给则沿用各行自带日期（用于构造日期不符的样例）。
    stamped = [dict(r, STAT_DATE=stat_date) for r in rows] if stat_date else list(rows)
    return {
        "result": stamped,
        "pageHelp": {"total": len(stamped), "pageSize": 10000, "pageNo": 1},
    }


def _szse_row(code, shares, name="测试ETF", d="2026-11-09") -> dict:
    return {"日期": d, "基金代码": code, "基金简称": name, "基金规模(份)": shares}


def _xlsx(records: list[dict]) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(records).to_excel(buf, index=False)
    return buf.getvalue()


def _pair(prev_rows, cur_rows, market="SSE", date_prev="2026-11-06",
          date_cur="2026-11-09"):
    if market == "SSE":
        p = m.parse_sse_payload(_sse(prev_rows, date_prev), date_prev)
        c = m.parse_sse_payload(_sse(cur_rows, date_cur), date_cur)
    else:
        p = m.parse_szse_records(prev_rows, date_prev)
        c = m.parse_szse_records(cur_rows, date_cur)
    return p, c


# ── 1. 单位：万份 → 份 / 份原值 ─────────────────────────────────────────

def test_sse_tot_vol_is_wan_shares_converted_to_shares():
    """510300 实测 2026-11-09 TOT_VOL=4666818.77（万份）→ 466.68 亿份。"""
    day = m.parse_sse_payload(_sse([_sse_row("510300", "4666818.77")]), "2026-11-09")
    assert day.usable is True
    assert day.n_funds == 1
    assert day.rows[0].shares == pytest.approx(4666818.77 * 1e4)
    assert day.sum_shares_yi == pytest.approx(466.681877, rel=1e-9)
    assert "万份" in day.unit_source


def test_szse_shares_passthrough_no_conversion():
    day = m.parse_szse_records([_szse_row(158000, 26925835.0)], "2026-11-09")
    assert day.usable is True
    assert day.rows[0].shares == 26925835.0
    assert "份" in day.unit_source


def test_units_are_never_called_amount():
    """报告里不许出现金额/亿元口径的数值或字段，且显式声明 unit=份。"""
    sse = m.parse_sse_payload(_sse([_sse_row("510300", "100")]), "2026-11-09")
    report = m.build_dry_run({"2026-11-09": [sse]})
    text = json.dumps(report, ensure_ascii=False)
    assert report["unit"] == "份"
    assert report["amount_fields_present"] is False
    # 不许出现以"亿元"标注的金额值（份额的唯一单位是份/亿份）
    assert "亿元" not in text
    assert report["market_total"]["unit"] == "份"
    assert report["share_change"]["unit"] == "份"

    def _kv(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k, v
                yield from _kv(v)
        elif isinstance(o, list):
            for v in o:
                yield from _kv(v)

    # 只对"数值/数值列表"字段禁金额命名；amount_fields_present / supports_amount
    # 这类"声明本模块不提供金额"的布尔旗标必须为 False，不算金额字段。
    for k, v in _kv(report):
        is_numeric = isinstance(v, (int, float)) and not isinstance(v, bool)
        if is_numeric:
            assert not any(t in k.lower() for t in
                           ("amount", "aum", "market_value", "mktcap", "金额", "市值")), k
        if k in ("amount_fields_present", "supports_amount"):
            assert v is False, k


# ── 2. 缺失 / 空结果：记 gap，不补零 ────────────────────────────────────

def test_empty_sse_result_is_gap_not_zero():
    day = m.parse_sse_payload(_sse([]), "2026-11-09")
    assert day.usable is False
    assert day.gap_reason == "empty_result"
    assert day.rows == ()
    report = m.build_dry_run({"2026-11-09": [day]})
    assert report["markets"]["SSE"]["usable"] is False
    assert report["verdict"]["status"] != "usable_for_daily_shares"
    assert report["market_total"]["complete"] is False


def test_blank_or_bad_tot_vol_dropped_and_counted_not_zero_filled():
    rows = [
        _sse_row("510300", "100"),
        _sse_row("510500", ""),      # 空值
        _sse_row("512100", "-"),     # 占位符
        _sse_row("588000", "abc"),   # 非数字
        _sse_row("515790", "-1"),    # 负数份额不可能
    ]
    day = m.parse_sse_payload(_sse(rows), "2026-11-09")
    assert day.n_funds == 1
    assert day.dropped_no_value == 4
    # 关键：被丢弃的没有变成 0 份额行混进合计
    assert [r.code for r in day.rows] == ["510300"]
    assert day.sum_shares == pytest.approx(100 * 1e4)


def test_missing_date_is_gap_not_zero():
    """两日中只有一日有数据 → 对差不可用，reason 说明缺边。"""
    cur = m.parse_sse_payload(_sse([_sse_row("510300", "100")]), "2026-11-09")
    d = m.diff_shares(None, cur)
    assert d.usable is False
    assert d.reason == "missing_side"
    assert d.comparable_change_shares == 0.0
    # 审计口径：份额差绝不称净申购，报告里不存在 net_subscription 数值字段
    assert "comparable_net_sub_shares" not in d.to_dict()


def test_date_mismatch_is_flagged_and_not_used():
    """源把别的日期当请求日返回 —— 不能当请求日的数据用。"""
    rows = [_sse_row("510300", "100", stat_date="2026-11-06")]
    day = m.parse_sse_payload(_sse(rows), "2026-11-09")
    assert day.data_date == "2026-11-06"
    assert day.date_mismatch is True
    assert day.usable is False
    assert "date_mismatch" in (day.gap_reason or "")
    # 别的日子的行不能留在 rows 里被求和
    assert day.rows == ()
    assert day.out_of_date_rows == 1


def test_pagination_incomplete_exposed():
    payload = _sse([_sse_row("510300", "100")])
    payload["pageHelp"] = {"total": 923, "pageSize": 10000, "pageNo": 1}
    day = m.parse_sse_payload(payload, "2026-11-09")
    assert day.usable is False
    assert "pagination_incomplete" in (day.gap_reason or "")


# ── 3. 日期严格解析与过滤 ────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("2026-11-09", "2026-11-09"),
    ("20261109", "2026-11-09"),
    (" 2026-11-09 ", "2026-11-09"),
    (20261109, "2026-11-09"),
])
def test_strict_date_parser_accepts_only_two_exact_forms(raw, expected):
    assert m.parse_date_strict(raw).isoformat() == expected


@pytest.mark.parametrize("raw", [
    "2026-11-9", "2026/11/09", "2026110", "2026-13-01", "2026-02-30",
    "2026-11-09abc", "abc", "", None, "2026-11", "2026-11-09 00:00:00",
    True, 20261109.0,
])
def test_strict_date_parser_rejects_ambiguous_or_impossible_dates(raw):
    assert m.parse_date_strict(raw) is None


def test_parse_functions_normalize_compact_requested_date():
    sse = m.parse_sse_payload(_sse([_sse_row("510300", "100")]), "20261109")
    assert sse.usable is True
    assert sse.data_date == "2026-11-09"
    szse = m.parse_szse_records([_szse_row(158000, 1e6)], "20261109")
    assert szse.usable is True
    assert szse.data_date == "2026-11-09"


def test_sse_rows_with_bad_or_foreign_dates_dropped_and_counted():
    payload = {
        "result": [
            {"SEC_CODE": "510300", "STAT_DATE": "2026-11-09", "TOT_VOL": "100"},
            {"SEC_CODE": "510500", "STAT_DATE": "2026-11-9", "TOT_VOL": "100"},
            {"SEC_CODE": "510900", "STAT_DATE": "2026-11-06", "TOT_VOL": "100"},
        ],
        "pageHelp": {"total": 3},
    }
    day = m.parse_sse_payload(payload, "2026-11-09")
    assert day.usable is True
    assert day.n_funds == 1
    assert day.rows[0].code == "510300"
    assert day.dropped_bad_date == 1
    assert day.out_of_date_rows == 1
    assert day.data_date == "2026-11-09"


def test_szse_rows_without_date_not_mixed_into_requested_day():
    recs = [
        {"基金代码": 158000, "基金规模(份)": 1e6},                    # 无日期
        {"日期": "2026-11-9", "基金代码": 158001, "基金规模(份)": 2e6},  # 非法日期
        {"日期": "2026-11-09", "基金代码": 158002, "基金规模(份)": 3e6},
    ]
    day = m.parse_szse_records(recs, "2026-11-09")
    assert day.n_funds == 1
    assert day.rows[0].code == "158002"
    assert day.dropped_bad_date == 2
    assert day.data_date == "2026-11-09"


# ── 4. 重复：同值去重、异值拒绝 ──────────────────────────────────────────

def test_duplicate_identical_deduped_not_double_counted():
    rows = [_sse_row("510300", "100"), _sse_row("510300", "100")]
    day = m.parse_sse_payload(_sse(rows), "2026-11-09")
    assert day.n_funds == 1
    assert day.duplicate_codes == ("510300",)
    assert day.conflicting_duplicates == ()
    assert day.sum_shares == pytest.approx(100 * 1e4)


def test_duplicate_conflicting_marks_unusable():
    rows = [_sse_row("510300", "100"), _sse_row("510300", "200")]
    day = m.parse_sse_payload(_sse(rows), "2026-11-09")
    assert day.usable is False
    assert day.conflicting_duplicates == ("510300",)
    assert "duplicate_conflict" in (day.gap_reason or "")


# ── 5. 新上市 / 终止 / 跨日口径 ──────────────────────────────────────────

def test_new_listing_excluded_from_comparable_change():
    """新股上市当天带进来的份额是一级发行，不是二级净申购。"""
    prev = [_sse_row("510300", "1000")]
    cur = [_sse_row("510300", "1100"), _sse_row("588999", "500")]  # 588999 新上市
    p, c = _pair(prev, cur)
    d = m.diff_shares(p, c)

    assert d.usable is True
    assert d.adjacent_session is True
    assert d.comparable_n == 1
    assert d.comparable_change_shares == pytest.approx(100 * 1e4)
    assert d.new_codes == ("588999",)
    assert d.new_shares == pytest.approx(500 * 1e4)
    # 朴素总额差会把 500 万份一级发行当成净申购
    assert d.naive_total_delta_shares == pytest.approx(600 * 1e4)
    assert d.naive_total_delta_shares != d.comparable_change_shares
    assert d.identity_ok is True


def test_terminated_fund_excluded_from_comparable_change():
    """终止/摘牌带来的份额消失是退出，不是净赎回。"""
    prev = [_sse_row("510300", "1000"), _sse_row("512390", "10")]  # 512390 次日终止
    cur = [_sse_row("510300", "1000")]
    p, c = _pair(prev, cur)
    d = m.diff_shares(p, c)

    assert d.comparable_n == 1
    assert d.comparable_change_shares == pytest.approx(0.0)
    assert d.gone_codes == ("512390",)
    assert d.gone_shares == pytest.approx(10 * 1e4)
    assert d.naive_total_delta_shares == pytest.approx(-10 * 1e4)
    assert d.identity_ok is True
    assert d.to_dict()["unit"] == "份"


def test_identity_holds_with_new_and_terminated_together():
    prev = [_sse_row("510300", "1000"), _sse_row("512390", "10")]
    cur = [_sse_row("510300", "1234"), _sse_row("588999", "500")]
    p, c = _pair(prev, cur)
    d = m.diff_shares(p, c)
    assert d.identity_ok is True
    assert math.isclose(
        d.comparable_change_shares + d.new_shares - d.gone_shares,
        d.naive_total_delta_shares, rel_tol=1e-9, abs_tol=1e-3,
    )


def test_diff_refuses_when_either_side_unusable():
    p = m.parse_sse_payload(_sse([], "2026-11-06"), "2026-11-06")
    c = m.parse_sse_payload(_sse([_sse_row("510300", "100")], "2026-11-09"), "2026-11-09")
    d = m.diff_shares(p, c)
    assert d.usable is False
    assert "source_unusable" in (d.reason or "")


def test_large_change_is_reported_honestly_without_jump_diagnosis():
    """不可信的'跳变/疑似折算'诊断已移除：大额变动如实计入，并明确无法判定。"""
    prev = [_sse_row("510300", "1000"), _sse_row("159335", "100")]
    cur = [_sse_row("510300", "1100"), _sse_row("159335", "189")]  # +89%
    p, c = _pair(prev, cur)
    d = m.diff_shares(p, c)

    payload = d.to_dict()
    assert d.comparable_change_shares == pytest.approx(189 * 1e4)
    assert d.adjacent_session is True
    assert "jump_codes" not in payload
    assert "jump_shares" not in payload
    assert "comparable_net_sub_shares" not in payload
    assert payload["comparable_change_shares"] == pytest.approx(189 * 1e4)
    assert payload["interpretation_warning"]
    assert "JUMP_RATIO" not in MODULE_PATH.read_text(encoding="utf-8")


def test_span_change_is_never_called_net_subscription():
    """跨多日的区间份额差不是'净申购'，字段与标签都必须改口。"""
    prev = [_sse_row("510300", "1000")]
    cur = [_sse_row("510300", "1100")]
    p, c = _pair(prev, cur, date_prev="2026-08-03", date_cur="2026-11-09")
    d = m.diff_shares(p, c)

    assert d.adjacent_session is False
    assert d.span_calendar_days == 98
    payload = d.to_dict()
    assert payload["change_label"] == "share_change"
    assert payload["not_net_subscription"] is True
    assert "comparable_net_sub_shares" not in payload
    assert payload["comparable_change_shares"] == pytest.approx(100 * 1e4)
    assert "净申购" in payload["not_net_subscription_reason"]

    report = m.build_dry_run(
        {"2026-11-09": [c]}, {"2026-08-03": [p]}
    )
    sc = report["share_change"]
    assert sc["label"] == "share_change"
    assert sc["not_net_subscription"] is True
    assert "comparable_net_sub_shares" not in sc
    assert report["complete"] is False


@pytest.mark.parametrize("prev_date,cur_date,expected", [
    ("2026-11-06", "2026-11-09", True),   # 周五 → 周一
    ("2026-11-05", "2026-11-06", True),   # 周四 → 周五
    ("2026-11-06", "2026-11-10", False),  # 跨过周一，中间缺一个交易日
    ("2026-11-09", "2026-11-09", False),  # 同一天
    ("2026-11-10", "2026-11-09", False),  # 倒序
])
def test_adjacent_session_rule(prev_date, cur_date, expected):
    assert m.is_adjacent_session(prev_date, cur_date) is expected


def test_prev_not_before_cur_is_unusable():
    prev = [_sse_row("510300", "1000")]
    cur = [_sse_row("510300", "1100")]
    p, c = _pair(prev, cur, date_prev="2026-11-10", date_cur="2026-11-09")
    d = m.diff_shares(p, c)
    assert d.usable is False
    assert d.reason == "prev_not_before_cur"


# ── 6. 强制沪深两市场 ───────────────────────────────────────────────────

def test_build_dry_run_forces_both_markets():
    sse = m.parse_sse_payload(_sse([_sse_row("510300", "100")]), "2026-11-09")
    report = m.build_dry_run({"2026-11-09": [sse]})

    assert set(report["markets"]) == {"SSE", "SZSE"}
    assert report["markets"]["SZSE"]["usable"] is False
    assert report["markets"]["SZSE"]["gap_reason"] == "market_missing"
    assert report["market_total"]["complete"] is False
    assert report["market_total"]["missing_markets"]["SZSE"] == "market_missing"
    assert report["verdict"]["status"] == "degraded"
    assert report["complete"] is False


def test_missing_prev_market_still_exposed_as_gap():
    sse_p = m.parse_sse_payload(_sse([_sse_row("510300", "100")], "2026-11-06"), "2026-11-06")
    sse_c = m.parse_sse_payload(_sse([_sse_row("510300", "110")], "2026-11-09"), "2026-11-09")
    sz_c = m.parse_szse_records([_szse_row(158000, 1e6)], "2026-11-09")
    report = m.build_dry_run(
        {"2026-11-09": [sse_c, sz_c]}, {"2026-11-06": [sse_p]}
    )
    assert report["share_change"]["by_market"]["SZSE"]["usable"] is False
    assert "market_missing" in (report["share_change"]["by_market"]["SZSE"]["reason"] or "")
    assert report["share_change"]["missing_prev_markets"] == ["SZSE"]
    assert report["complete"] is False


# ── 7. 深交所：多日 data_date 与 6 个月分窗 ──────────────────────────────

def test_szse_int_codes_normalized_so_cross_day_diff_is_not_all_new():
    """xlsx 里 158000 是 int，证据 JSON 里是 str；不补齐会 100% 误判新增+终止。"""
    p = m.parse_szse_records([_szse_row(158000, 1e6, d="2026-11-06")], "2026-11-06")
    c = m.parse_szse_records(
        [{"日期": "2026-11-09", "基金代码": 158000, "基金简称": "x",
          "基金规模(份)": 2e6}], "2026-11-09")
    assert p.rows[0].code == "158000"
    d = m.diff_shares(p, c)
    assert d.comparable_n == 1
    assert d.new_codes == ()
    assert d.gone_codes == ()
    assert d.comparable_change_shares == pytest.approx(1e6)


def test_szse_multi_day_window_data_date_is_requested_not_max():
    """区间接口返回多日时，data_date 必须是请求日，不能取区间最大日。"""
    recs = [
        _szse_row(158000, 1e6, d="2026-11-05"),
        _szse_row(158000, 2e6, d="2026-11-06"),
        _szse_row(158000, 3e6, d="2026-11-09"),
    ]
    day = m.parse_szse_records(recs, "2026-11-06")
    assert day.n_funds == 1
    assert day.rows[0].shares == pytest.approx(2e6)
    assert day.data_date == "2026-11-06"
    assert day.date_mismatch is False
    assert day.out_of_date_rows == 2


def test_iter_windows_splits_at_six_months():
    wins = m.iter_windows("2025-01-01", "2026-11-09")
    assert len(wins) >= 4
    for s, e in wins:
        d0 = m.parse_date_strict(s)
        d1 = m.parse_date_strict(e)
        assert (d1 - d0).days < m.SZSE_MAX_WINDOW_DAYS
    assert wins[0][0] == "2025-01-01"
    assert wins[-1][1] == "2026-11-09"


def test_fetch_szse_window_splits_and_merges_over_six_months():
    calls = []

    def fake(url, *, params, headers, timeout):
        calls.append(dict(params))
        return _Resp(content=_xlsx([_szse_row(158000, 1e6, d=params["txtStart"])]))

    days = m.fetch_szse_window("2025-01-01", "2026-11-09", http=fake)
    assert len(calls) >= 4
    for p in calls:
        span = (m.parse_date_strict(p["txtEnd"]) - m.parse_date_strict(p["txtStart"])).days
        assert span < m.SZSE_MAX_WINDOW_DAYS
    expected_keys = [s for s, _ in m.iter_windows("2025-01-01", "2026-11-09")]
    assert sorted(days) == sorted(expected_keys)
    assert all(d.usable for d in days.values())


def test_szse_no_data_sentinel_reported_as_empty_result_not_schema_error():
    """深交所超保留期会回一行'没有找到符合条件的数据！'，不能误判成日期解析失败。"""
    sentinel = {"日期": "没有找到符合条件的数据！", "基金代码": None,
                "基金简称": None, "基金规模(份)": None}

    def fake(url, *, params, headers, timeout):
        return _Resp(content=_xlsx([sentinel]))

    days = m.fetch_szse_window("2026-01-05", "2026-07-06", http=fake)
    day = days["2026-01-05"]
    assert day.usable is False
    assert day.gap_reason == "empty_result"
    assert any("无数据" in w or "没有找到" in w for w in day.warnings)
    assert day.dropped_bad_date == 0


# ── 8. 全市场，不是 20 只宽基 ────────────────────────────────────────────

def test_coverage_counts_every_exchange_row_not_a_basket():
    rows = [_sse_row(f"51{i:04d}", str(10 + i)) for i in range(900)]
    day = m.parse_sse_payload(_sse(rows), "2026-11-09")
    report = m.build_dry_run({"2026-11-09": [day]})
    assert report["markets"]["SSE"]["n_funds"] == 900
    assert report["market_total"]["n_funds"] == 900
    assert report["basket_proxy_used"] is False


def test_module_has_no_basket_entry_point():
    """没有把 20 只宽基当全市场的入口。"""
    src = MODULE_PATH.read_text(encoding="utf-8")
    assert "ETF_BASKET" not in src
    assert "basket" not in {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)}


# ── 9. 货币 ETF 缺口必须显式说明 ────────────────────────────────────────

def test_money_etf_gap_is_documented_and_total_marked_not_full_market():
    sse = m.parse_sse_payload(_sse([_sse_row("510300", "100")]), "2026-11-09")
    szse = m.parse_szse_records([_szse_row(158000, 1e6)], "2026-11-09")
    report = m.build_dry_run({"2026-11-09": [sse, szse]})

    mt = report["market_total"]
    assert mt["excludes_money_etf"] is True
    assert mt["covers_money_etf"] is False
    assert mt["full_market_total"] is False
    assert "货币" in mt["coverage_note"]

    gap = report["known_gaps"]["sse_money_etf"]
    assert gap["affects_total"] is True
    assert gap["inferred"] is True
    assert "511990" in gap["codes"]
    assert "不含" in gap["note"]

    v = report["verdict"]
    assert v["covers_money_etf"] is False
    assert "货币" in v["note"]


# ── 10. 只读保证 ────────────────────────────────────────────────────────

def test_module_is_read_only_by_construction():
    src = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("src.web", "from src.core", "sqlite3", "sqlalchemy",
                      "INSERT INTO", "CREATE TABLE", "session.commit", "drop_all",
                      "open(", "Path.write_text", "to_csv", "to_parquet"):
        assert forbidden not in src, f"只读模块不允许出现 {forbidden}"

    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {"__future__", "argparse", "io", "json", "logging", "math", "re",
               "sys", "time", "datetime", "dataclasses", "typing", "requests", "pandas"}
    assert imported <= allowed, imported - allowed


def test_report_declares_read_only_and_no_db_write():
    day = m.parse_sse_payload(_sse([_sse_row("510300", "100")]), "2026-11-09")
    report = m.build_dry_run({"2026-11-09": [day]})
    assert report["read_only"] is True
    assert report["wrote_database"] is False


# ── 11. 真实取数：统一 market_http、注入与错误路径 ───────────────────────

class _Resp:
    def __init__(self, payload=None, content=None):
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


def test_fetch_sse_error_becomes_gap_not_exception():
    def boom(*a, **k):
        raise ConnectionResetError("boom")

    day = m.fetch_sse_day("2026-11-09", http=boom, retries=2)
    assert day.usable is False
    assert day.gap_reason == "fetch_error"
    assert "ConnectionResetError" in (day.fetch_error or "")


def test_fetch_sse_uses_trade_date_param():
    seen = {}

    def fake(url, *, params, headers, timeout):
        seen.update(params)
        return _Resp(_sse([_sse_row("510300", "100")]))

    day = m.fetch_sse_day("2026-11-09", http=fake)
    assert seen["STAT_DATE"] == "2026-11-09"
    assert seen["sqlId"] == m.SSE_SQLID
    assert day.usable is True


def test_single_market_http_entry_used_by_both_exchanges(monkeypatch):
    """沪深两条取数路径必须走同一个 market_http 入口，不允许各自造轮子。"""
    calls = []

    def fake(url, *, params, headers, timeout):
        calls.append((url, dict(params)))
        if url == m.SSE_URL:
            return _Resp(payload=_sse([_sse_row("510300", "100")]))
        return _Resp(content=_xlsx([_szse_row(158000, 1e6)]))

    monkeypatch.setattr(m, "market_http", fake)
    day = m.fetch_sse_day("2026-11-09")
    assert day.usable is True
    days = m.fetch_szse_window("2026-11-09", "2026-11-09")
    assert days["2026-11-09"].usable is True

    assert [c[0] for c in calls] == [m.SSE_URL, m.SZSE_URL]
    assert not hasattr(m, "_default_get")


# ── 12. CLI 退出码 ──────────────────────────────────────────────────────

def _complete_report():
    prev_sse = m.parse_sse_payload(_sse([_sse_row("510300", "100")], "2026-11-06"), "2026-11-06")
    cur_sse = m.parse_sse_payload(_sse([_sse_row("510300", "110")], "2026-11-09"), "2026-11-09")
    prev_sz = m.parse_szse_records([_szse_row(158000, 1e6, d="2026-11-06")], "2026-11-06")
    cur_sz = m.parse_szse_records([_szse_row(158000, 2e6, d="2026-11-09")], "2026-11-09")
    return m.build_dry_run(
        {"2026-11-09": [cur_sse, cur_sz]},
        {"2026-11-06": [prev_sse, prev_sz]},
    )


def test_cli_exit_code_zero_only_when_complete():
    report = _complete_report()
    assert report["complete"] is True
    assert m.cli_exit_code(report) == 0

    incomplete = m.build_dry_run({"2026-11-09": [
        m.parse_sse_payload(_sse([_sse_row("510300", "100")]), "2026-11-09")
    ]})
    assert incomplete["complete"] is False
    assert m.cli_exit_code(incomplete) != 0

    unusable = m.build_dry_run({})
    assert m.cli_exit_code(unusable) == m.EXIT_UNUSABLE


def test_main_returns_nonzero_on_incomplete(monkeypatch, capsys):
    incomplete = m.build_dry_run({"2026-11-09": [
        m.parse_sse_payload(_sse([_sse_row("510300", "100")]), "2026-11-09")
    ]})
    monkeypatch.setattr(m, "dry_run", lambda *a, **k: incomplete)
    rc = m.main(["--date", "2026-11-09"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["complete"] is False


def test_main_returns_zero_on_complete(monkeypatch, capsys):
    monkeypatch.setattr(m, "dry_run", lambda *a, **k: _complete_report())
    rc = m.main(["--date", "2026-11-09", "--prev", "2026-11-06"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["complete"] is True


def test_main_rejects_loose_or_impossible_dates(capsys):
    assert m.main(["--date", "2026-11-9"]) == m.EXIT_UNUSABLE
    assert m.main(["--date", "2026-02-30"]) == m.EXIT_UNUSABLE
    assert m.main(["--date", "2026-11-09", "--prev", "2026-11-10"]) == m.EXIT_UNUSABLE
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_report_never_claims_net_subscription_even_for_adjacent_days():
    """相邻交易日也只叫份额差：报告里没有 comparable_net_sub 数值字段。"""
    report = _complete_report()
    sc = report["share_change"]
    assert sc["adjacent_session"] is True
    assert sc["label"] == "share_change"
    assert sc["not_net_subscription"] is True

    def _keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from _keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from _keys(v)

    assert not [k for k in _keys(report) if k.startswith("comparable_net")]
    assert "daily_net_subscription" not in json.dumps(report, ensure_ascii=False)


def test_share_change_unavailable_without_prev():
    sse = m.parse_sse_payload(_sse([_sse_row("510300", "100")]), "2026-11-09")
    szse = m.parse_szse_records([_szse_row(158000, 1e6)], "2026-11-09")
    report = m.build_dry_run({"2026-11-09": [sse, szse]})
    sc = report["share_change"]
    assert sc["available"] is False
    assert sc["label"] == "not_available"
    assert "comparable_net_sub_shares" not in sc
    assert report["complete"] is True
    assert m.cli_exit_code(report) == 0
