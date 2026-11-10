# -*- coding: utf-8 -*-
"""f28ae7e1 复审 12 条缺陷的回归钉子（只读、离线、不连库）。

每条用例对应复审报告 deleg_a1cf7e36 的一个缺陷；修复前应失败，修复后必须通过。"""
from __future__ import annotations
import io, json, sys
from datetime import date, timedelta

import pandas as pd
import pytest

from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from src.collectors import etf_market_share as m  # noqa: E402

D, DP = "2026-11-09", "2026-11-06"


def sse_row(code, vol, d=D):
    return {"STAT_DATE": d, "ETF_TYPE": "单市", "SEC_CODE": code,
            "SEC_NAME": "x", "TOT_VOL": vol}


def sse(rows, total=None):
    return {"result": rows,
            "pageHelp": {"total": len(rows) if total is None else total,
                         "pageSize": 10000, "pageNo": 1}}


def sz_row(code, sh, d=D):
    return {"日期": d, "基金代码": code, "基金简称": "x", "基金规模(份)": sh}


class R:
    def __init__(self, content=None, payload=None):
        self.content, self._p = content, payload

    def json(self):
        return self._p


def xlsx(recs):
    b = io.BytesIO()
    pd.DataFrame(recs).to_excel(b, index=False)
    return b.getvalue()


# ── D1: 大面积丢行仍标 complete/exit 0（合计静默低报）───────────────────
def test_D1_massive_dropped_rows_must_not_be_complete():
    rows = [sse_row("510300", "1000")] + [sse_row(f"51{i:04d}", "-") for i in range(899)]
    day = m.parse_sse_payload(sse(rows), D)
    sz = m.parse_szse_records([sz_row(158000, 1e6)], D)
    rep = m.build_dry_run({D: [day, sz]})
    # 900 行里 899 行份额无效 → 这个"全市场合计"不可能可用
    assert day.dropped_no_value == 899
    assert day.usable is False, f"899/900 行被丢弃仍 usable=True, sum={day.sum_shares_yi:.2f}亿份"
    assert rep["complete"] is False
    assert m.cli_exit_code(rep) != 0
    assert rep["markets"]["SSE"]["warnings"], "丢行没有任何 warning"


# ── D2: {date: days} 里更早的一天被静默丢弃，且 exit 0 ──────────────────
def test_D2_multi_date_mapping_must_not_silently_drop_prev():
    p = m.parse_sse_payload(sse([sse_row("510300", "1000", DP)]), DP)
    c = m.parse_sse_payload(sse([sse_row("510300", "1100", D)]), D)
    pz = m.parse_szse_records([sz_row(158000, 1e6, DP)], DP)
    cz = m.parse_szse_records([sz_row(158000, 2e6, D)], D)
    rep = m.build_dry_run({DP: [p, pz], D: [c, cz]})
    # 自然理解：给了两天的快照就该出变动；至少不能 complete=True 且 exit 0
    assert rep["prev_requested"] is not None, "prev 被静默丢弃(prev_requested=None)"
    assert rep["share_change"]["available"] is True
    assert not (rep["complete"] is True and rep["share_change"]["label"] == "not_available")


# ── D3: 单市场 diff 不可用 → 汇总份额差是部分和，无完整旗标 ─────────────
def test_D3_partial_aggregate_change_must_be_flagged():
    p = m.parse_sse_payload(sse([sse_row("510300", "1000", DP)]), DP)
    c = m.parse_sse_payload(sse([sse_row("510300", "1100", D)]), D)
    cz = m.parse_szse_records([sz_row(158000, 2e6, D)], D)
    p_bad = m.parse_szse_records([sz_row(158000, 1e6, "2026-11-05")], DP)  # date_mismatch
    sc = m.build_dry_run({D: [c, cz]}, {DP: [p, p_bad]})["share_change"]
    assert sc["by_market"]["SZSE"]["usable"] is False
    assert sc.get("complete") is False or sc.get("covered_markets") is not None, \
        "汇总 comparable_change_shares 只含 SSE，却没有任何完整旗标"
    assert sc["missing_prev_markets"] == ["SZSE"], "非 market_missing 的不可用市场没进 missing_prev_markets"
    assert sc["adjacent_session"] is not True, "还有一个市场不可用却标 adjacent_session=True"


# ── D4: 分页只看 total；total 缺失/字符串时静默接受截断 ──────────────────
def test_D4_pagination_must_not_trust_missing_or_string_total():
    no_page = m.parse_sse_payload({"result": [sse_row("510300", "100")]}, D)
    assert no_page.usable is False, "无 pageHelp.total 即无法证明取全，却 usable=True"
    str_total = m.parse_sse_payload(
        {"result": [sse_row("510300", "100")], "pageHelp": {"total": "923"}}, D)
    assert str_total.usable is False, "total 是字符串时 pagination 校验被跳过"


def test_D4b_no_page_loop_though_server_caps_pageSize():
    import inspect
    src = inspect.getsource(m.fetch_sse_day)
    assert "pageCount" in src or src.count("pageHelp.pageNo") > 1, \
        "只请求 pageNo=1，服务端把 pageSize 截到 2000（实测）后无翻页"


# ── D5: 183 天窗口 > 深交所 6 个日历月上限，整窗丢数且不再收窄 ──────────
def test_D5_szse_windows_must_respect_calendar_six_months():
    def months_after(d, k):
        y, mo = d.year, d.month + k
        y += (mo - 1) // 12
        mo = (mo - 1) % 12 + 1
        return date(y, mo, d.day)

    def fake(url, *, params, headers, timeout):
        s = m.parse_date_strict(params["txtStart"])
        e = m.parse_date_strict(params["txtEnd"])
        if e > months_after(s, 6):          # 实测：>6 个日历月 → 占位行
            return R(xlsx([{"日期": "没有找到符合条件的数据！", "基金代码": None,
                            "基金简称": None, "基金规模(份)": None}]))
        recs, d = [], s
        while d <= e:
            if d.weekday() < 5:
                recs.append(sz_row(158000, 1e6, d.isoformat()))
            d += timedelta(days=1)
        return R(xlsx(recs))

    got = m.fetch_szse_window("2026-01-05", "2026-07-06", http=fake)
    usable = [k for k, v in got.items() if v.usable]
    assert "2026-01-06" in usable and "2026-06-30" in usable, \
        f"窗口过长整段丢失，仅恢复 {len(usable)} 天: {usable[:3]}"


def test_D5b_doc_retention_claim_is_false():
    """文档称'只能取到近约 6 个月/更早取不到'，实测 2021-11 起的 6 个月窗口仍有数据。"""
    src = open(f"{ROOT}/src/collectors/etf_market_share.py", encoding="utf-8").read()
    assert "只能取到近约 6 个月" not in src, "保留期结论未证伪，与实测(2025-11-10 可取)矛盾"


# ── D6: 目标日落在失败窗口 → 归因变成 no_rows_for_date，fetch_error 丢失 ─
def test_D6_failed_window_must_not_be_reported_as_source_no_data():
    def fake(url, *, params, headers, timeout):
        if params["txtStart"] <= "2026-03-09" <= params["txtEnd"]:
            raise ConnectionResetError("boom")
        return R(xlsx([sz_row(158000, 1e6, d=params["txtStart"])]))

    cur_sse = m.parse_sse_payload(sse([sse_row("510300", "100")]), D)
    rep = m.dry_run("2026-03-09", D, prev="2026-03-06", http=fake)
    sz = rep["markets"]["SZSE"]
    assert sz["usable"] is False
    assert sz["gap_reason"] == "fetch_error", f"网络失败被报成 {sz['gap_reason']}（看起来像源无数据）"
    assert sz["fetch_error"]


# ── D7: 货币 ETF 缺口写成 3 只硬编码，且无运行时校验 ────────────────────
def test_D7_money_etf_gap_must_not_be_hardcoded_stale():
    # 如果源真的返回了 511990，报告仍宣称缺它 → 缺口清单是陈旧的
    rows = [sse_row("510300", "100"), sse_row("511990", "200")]
    sse_d = m.parse_sse_payload(sse(rows), D)
    sz = m.parse_szse_records([sz_row(159001, 1e6)], D)
    rep = m.build_dry_run({D: [sse_d, sz]})
    gap = rep["known_gaps"]["sse_money_etf"]
    assert "511990" not in gap["codes"], "511990 已在源里出现，报告仍宣称缺失"
    assert gap.get("verified_at") or gap.get("checked_codes"), "缺口清单从未与当日实际返回比对"


def test_D7b_szse_money_etf_coverage_should_be_stated():
    rep = m.build_dry_run({D: [m.parse_sse_payload(sse([sse_row("510300", "100")]), D),
                              m.parse_szse_records([sz_row(159001, 1e6)], D)]})
    assert "szse" in json.dumps(rep["known_gaps"]).lower() or \
        rep["market_total"].get("covers_money_etf") is not False, \
        "深市 159001/159003/159005 货币 ETF 实测在源里，报告却笼统写 covers_money_etf=False"


# ── D8: 不可用 diff 仍输出份额差数值 + 测试文件口径自相矛盾 ──────────────
def test_D8_unusable_diff_must_not_expose_change_numbers():
    p = m.parse_sse_payload(sse([], DP), DP)          # 空源 → unusable
    c = m.parse_sse_payload(sse([sse_row("510300", "1100"), sse_row("588999", "500")]), D)
    d = m.diff_shares(p, c).to_dict()
    assert d["usable"] is False
    assert d["comparable_change_shares"] == 0.0 and d["new_listed_shares"] == 0.0, \
        f"不可用 diff 仍输出 new_listed_shares={d['new_listed_shares']}"


def test_D9_test_docstring_must_not_say_net_subscription_allowed():
    txt = open(f"{ROOT}/tests/test_etf_market_share.py", encoding="utf-8").read()
    assert "才允许叫\"净申购\"" not in txt, "测试头部口径仍写着'相邻交易日可称净申购'，与模块硬口径冲突"
