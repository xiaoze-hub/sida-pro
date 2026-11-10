# -*- coding: utf-8 -*-
"""市场级 ETF 日度份额替代源：隔离单测（不联网、不连库、不写盘）。

钉住的口径（任何一条被改坏都会让"份额"数据说谎）：

1. 上交所 `TOT_VOL` 是**万份**，必须 ×1e4；深交所 `基金规模(份)` 是**份**，不换算。
2. 输出里**没有**金额/市值字段，单位一律「份」——份额不等于金额。
3. 缺失/空结果/日期不符/重复冲突 → gap + `usable=False`，**绝不补 0**。
4. 新上市与终止必须从"可比净申购"里剔除并单列；恒等式要闭合。
5. 覆盖率按交易所返回的**全部** ETF 算，没有宽基篮子入口。
6. 模块是纯只读：不 import web/DB 层，没有任何写库路径。
"""

from __future__ import annotations

import ast
import json
import math
import sys
from pathlib import Path

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
    assert d.comparable_net_sub_shares == 0.0


def test_date_mismatch_is_flagged_and_not_used():
    """源把别的日期当请求日返回 —— 不能当请求日的数据用。"""
    rows = [_sse_row("510300", "100", stat_date="2026-11-06")]
    day = m.parse_sse_payload(_sse(rows), "2026-11-09")
    assert day.data_date == "2026-11-06"
    assert day.date_mismatch is True
    assert day.usable is False
    assert "date_mismatch" in (day.gap_reason or "")


def test_pagination_incomplete_exposed():
    payload = _sse([_sse_row("510300", "100")])
    payload["pageHelp"] = {"total": 923, "pageSize": 10000, "pageNo": 1}
    day = m.parse_sse_payload(payload, "2026-11-09")
    assert day.usable is False
    assert "pagination_incomplete" in (day.gap_reason or "")


# ── 3. 重复：同值去重、异值拒绝 ──────────────────────────────────────────

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


# ── 4. 新上市 / 终止：必须单列，不得混进净申购 ─────────────────────────

def _pair(prev_rows, cur_rows, market="SSE", date_prev="2026-11-06",
          date_cur="2026-11-09"):
    if market == "SSE":
        p = m.parse_sse_payload(_sse(prev_rows, date_prev), date_prev)
        c = m.parse_sse_payload(_sse(cur_rows, date_cur), date_cur)
    else:
        p = m.parse_szse_records(prev_rows, date_prev)
        c = m.parse_szse_records(cur_rows, date_cur)
    return p, c


def test_new_listing_excluded_from_comparable_net_subscription():
    """新股上市当天带进来的份额是一级发行，不是二级净申购。"""
    prev = [_sse_row("510300", "1000")]
    cur = [_sse_row("510300", "1100"), _sse_row("588999", "500")]  # 588999 新上市
    p, c = _pair(prev, cur)
    d = m.diff_shares(p, c)

    assert d.usable is True
    assert d.comparable_n == 1
    assert d.comparable_net_sub_shares == pytest.approx(100 * 1e4)
    assert d.new_codes == ("588999",)
    assert d.new_shares == pytest.approx(500 * 1e4)
    # 朴素总额差会把 500 万份一级发行当成净申购
    assert d.naive_total_delta_shares == pytest.approx(600 * 1e4)
    assert d.naive_total_delta_shares != d.comparable_net_sub_shares
    assert d.identity_ok is True


def test_terminated_fund_excluded_from_comparable_net_subscription():
    """终止/摘牌带来的份额消失是退出，不是净赎回。"""
    prev = [_sse_row("510300", "1000"), _sse_row("512390", "10")]  # 512390 次日终止
    cur = [_sse_row("510300", "1000")]
    p, c = _pair(prev, cur)
    d = m.diff_shares(p, c)

    assert d.comparable_n == 1
    assert d.comparable_net_sub_shares == pytest.approx(0.0)
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
        d.comparable_net_sub_shares + d.new_shares - d.gone_shares,
        d.naive_total_delta_shares, rel_tol=1e-9, abs_tol=1e-3,
    )


def test_diff_refuses_when_either_side_unusable():
    p = m.parse_sse_payload(_sse([], "2026-11-06"), "2026-11-06")
    c = m.parse_sse_payload(_sse([_sse_row("510300", "100")], "2026-11-09"), "2026-11-09")
    d = m.diff_shares(p, c)
    assert d.usable is False
    assert "source_unusable" in (d.reason or "")


def test_suspected_split_is_flagged_not_silently_counted_as_inflow():
    """份额折算/拆分会让份额翻倍；必须单列，且给出去掉它之后的保守值。"""
    prev = [_sse_row("510300", "1000"), _sse_row("159335", "100")]
    cur = [_sse_row("510300", "1100"), _sse_row("159335", "189")]  # +89% 疑似折算
    p, c = _pair(prev, cur)
    d = m.diff_shares(p, c)

    assert d.jump_codes == ("159335",)
    assert d.jump_shares == pytest.approx(89 * 1e4)
    assert d.to_dict()["jump_threshold"] == m.JUMP_RATIO_THRESHOLD
    # 可比净申购仍包含它（诚实），但另有剔除后的保守值
    assert d.comparable_net_sub_shares == pytest.approx(189 * 1e4)
    assert d.comparable_ex_jump_net_sub_shares == pytest.approx(100 * 1e4)
    assert d.to_dict()["jump_note"]
    assert d.identity_ok is True


def test_normal_change_is_not_flagged_as_jump():
    prev = [_sse_row("510300", "1000")]
    cur = [_sse_row("510300", "1400")]  # +40% < 50%
    p, c = _pair(prev, cur)
    d = m.diff_shares(p, c)
    assert d.jump_codes == ()
    assert d.comparable_ex_jump_net_sub_shares == d.comparable_net_sub_shares


# ── 5. 深交所代码类型陷阱 ───────────────────────────────────────────────

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
    assert d.comparable_net_sub_shares == pytest.approx(1e6)


def test_szse_window_rows_filtered_to_requested_date():
    recs = [_szse_row(158000, 1e6, d="2026-11-06"),
            _szse_row(158000, 2e6, d="2026-11-09")]
    day = m.parse_szse_records(recs, "2026-11-09")
    assert day.n_funds == 1
    assert day.rows[0].shares == pytest.approx(2e6)
    assert day.data_date == "2026-11-09"


# ── 6. 全市场，不是 20 只宽基 ────────────────────────────────────────────

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


# ── 7. 只读保证 ─────────────────────────────────────────────────────────

def test_module_is_read_only_by_construction():
    src = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("src.web", "from src.core", "sqlite3", "sqlalchemy",
                      "INSERT INTO", "CREATE TABLE", "session.commit", "drop_all"):
        assert forbidden not in src, f"只读模块不允许出现 {forbidden}"

    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {"__future__", "argparse", "io", "json", "logging", "math", "time",
               "datetime", "dataclasses", "typing", "requests", "pandas"}
    assert imported <= allowed, imported - allowed


def test_report_declares_read_only_and_no_db_write():
    day = m.parse_sse_payload(_sse([_sse_row("510300", "100")]), "2026-11-09")
    report = m.build_dry_run({"2026-11-09": [day]})
    assert report["read_only"] is True
    assert report["wrote_database"] is False


# ── 8. 真实取数的错误路径（注入假 http，不联网）─────────────────────────

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


def test_iter_windows_splits_at_six_months():
    wins = m.iter_windows("2025-01-01", "2026-11-09")
    assert len(wins) >= 4
    for s, e in wins:
        d0 = m.datetime.strptime(s, "%Y-%m-%d").date()
        d1 = m.datetime.strptime(e, "%Y-%m-%d").date()
        assert (d1 - d0).days < m.SZSE_MAX_WINDOW_DAYS
    assert wins[0][0] == "2025-01-01"
    assert wins[-1][1] == "2026-11-09"
