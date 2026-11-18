# -*- coding: utf-8 -*-
"""国家队持仓（汇金/证金/外汇局三平台）采集与读取测试（2026-11-18）。

不打上游：`_query_rows` 用假数据替换，测的是**本仓契约**——
①迁移确实建表并注册；②非法行丢弃不让「脏 0」进库；③sync 幂等 + 源端消失的行会被删；
④overview/holdings/changes 的口径（只数、市值合计、环比分类、未在榜主体）；
⑤表为空时 available=false（不编造）。

不覆盖（见 core docstring 的硬限制）：ETF 持仓、第 11 名以后的持仓、A/H 拆分 —— 上游本就不提供。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from src.core import national_team as nt
from src.web.migrations import MIGRATIONS, run_versioned_migrations


def _src_row(code: str, name: str, hold_num: float, *, end="2026-09-30",
             ratio=None, cap=None, rank=1, change="不变", stype="流通A股"):
    return {
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": name,
        "END_DATE": f"{end} 00:00:00",
        "HOLD_NUM": hold_num,
        "HOLD_NUM_RATIO": ratio,
        "HOLDER_MARKET_CAP": cap,
        "HOLDER_RANK": rank,
        "HOLD_NUM_CHANGE": change,
        "SHARES_TYPE": stype,
    }


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/national_team.db")
    run_versioned_migrations(eng)
    yield eng
    eng.dispose()


def _fake_query(rows_by_key):
    """按 (report_table, holder_name, report_date) 返回预设行。"""

    def _q(report_table, holder_name, report_date=None, **kwargs):
        return list(rows_by_key.get((report_table, holder_name, report_date), []))

    return _q


# ────────────────────────── 迁移 ──────────────────────────


def test_migration_registered_and_creates_table(engine):
    versions = [m.version for m in MIGRATIONS]
    assert 251 in versions, "v251 必须在 MIGRATIONS 元组里注册"
    assert versions == sorted(versions), "MIGRATIONS 必须按版本升序"
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(national_team_holdings)")).fetchall()}
    assert {"report_date", "report_kind", "holder_key", "stock_code", "hold_num",
            "hold_ratio", "market_cap", "change_type"} <= cols


# ────────────────────────── 归一化 ──────────────────────────


def test_normalize_drops_invalid_rows():
    holder = nt.HOLDER_BY_KEY["huijin_invest"]
    raw = [
        _src_row("601988", "中国银行", 1.887e11, cap=1.27e12, ratio=58.59),
        _src_row("601", "坏代码", 100.0),            # 代码长度非法
        _src_row("６０１０００", "全角代码", 100.0),   # 全角数字 → 必须拒绝, 不能"修复"
        _src_row("600000", "零持股", 0),             # hold_num=0 → 丢弃
        _src_row("600001", "缺持股", None),
        {**_src_row("600002", "日期不符", 10.0), "END_DATE": "2026-06-30 00:00:00"},
    ]
    rows, stats = nt.normalize_rows(
        raw, holder=holder, report_kind="float", report_date="2026-09-30", snapshot_at="2026-11-18T19:00:00+08:00"
    )
    assert len(rows) == 1 and rows[0]["stock_code"] == "601988"
    assert rows[0]["holder_key"] == "huijin_invest"
    assert stats == {"raw": 6, "kept": 1, "dropped_code": 2, "dropped_value": 2, "dropped_date": 1}


# ────────────────────────── 采集 ──────────────────────────


def test_sync_is_idempotent_and_removes_stale(engine, monkeypatch):
    key = (nt.REPORT_TABLES["float"], nt.HOLDER_BY_KEY["huijin_invest"]["name"], "2026-09-30")
    rows = {
        key: [
            _src_row("601988", "中国银行", 100.0, cap=1000.0),
            _src_row("601398", "工商银行", 200.0, cap=2000.0),
        ]
    }
    monkeypatch.setattr(nt, "_query_rows", _fake_query(rows))

    first = nt.sync(engine, periods=["2026-09-30"], report_kinds=["float"],
                    holders=[nt.HOLDER_BY_KEY["huijin_invest"]])
    assert first["ok"] is True and first["written"] == 2

    # 同一份数据再跑一次：行数不变（幂等），snapshot_at 更新为最新一次采集
    second = nt.sync(engine, periods=["2026-09-30"], report_kinds=["float"],
                     holders=[nt.HOLDER_BY_KEY["huijin_invest"]])
    assert second["written"] == 2
    h = nt.holdings(engine, report_date="2026-09-30", kind="float")
    assert h["count"] == 2
    assert h["items"][0]["snapshot_at"] >= first["snapshot_at"]

    # 源端少了一只（例如调出前十）→ 库里不能残留旧行
    rows[key] = [_src_row("601988", "中国银行", 100.0, cap=1000.0)]
    nt.sync(engine, periods=["2026-09-30"], report_kinds=["float"],
            holders=[nt.HOLDER_BY_KEY["huijin_invest"]])
    h2 = nt.holdings(engine, report_date="2026-09-30", kind="float")
    assert h2["count"] == 1 and h2["items"][0]["stock_code"] == "601988"


def test_sync_reports_error_when_all_rows_invalid(engine, monkeypatch):
    key = (nt.REPORT_TABLES["float"], nt.HOLDER_BY_KEY["csf"]["name"], "2026-09-30")
    monkeypatch.setattr(nt, "_query_rows", _fake_query({key: [_src_row("bad", "坏行", 1.0)]}))
    rc = nt.sync(engine, periods=["2026-09-30"], report_kinds=["float"],
                 holders=[nt.HOLDER_BY_KEY["csf"]])
    assert rc["ok"] is False
    assert rc["errors"] and "全部非法" in rc["errors"][0]["error"]
    assert rc["written"] == 0


def test_sync_marks_empty_holder_without_writing(engine, monkeypatch):
    monkeypatch.setattr(nt, "_query_rows", _fake_query({}))
    rc = nt.sync(engine, periods=["2026-09-30"], report_kinds=["float"],
                 holders=[nt.HOLDER_BY_KEY["wutongshu"]])
    assert rc["ok"] is True
    assert rc["empty"] == ["2026-09-30/float/wutongshu"]
    assert rc["written"] == 0


# ────────────────────────── 读取口径 ──────────────────────────


def _seed_two_periods(engine, monkeypatch):
    ht = nt.HOLDER_BY_KEY["huijin_asset"]
    csf = nt.HOLDER_BY_KEY["csf"]
    rows = {
        (nt.REPORT_TABLES["float"], ht["name"], "2026-09-30"): [
            _src_row("601318", "中国平安", 500.0, cap=5000.0, ratio=2.6),
            _src_row("000333", "美的集团", 100.0, cap=900.0, ratio=1.2, change="增持"),
        ],
        (nt.REPORT_TABLES["float"], ht["name"], "2026-06-30"): [
            _src_row("601318", "中国平安", 500.0, cap=4800.0, end="2026-06-30", ratio=2.5),
            _src_row("600585", "海螺水泥", 300.0, cap=600.0, end="2026-06-30", ratio=1.3),
        ],
        (nt.REPORT_TABLES["float"], csf["name"], "2026-09-30"): [
            _src_row("601988", "中国银行", 700.0, cap=7000.0, rank=2),
        ],
        (nt.REPORT_TABLES["float"], csf["name"], "2026-06-30"): [
            _src_row("601988", "中国银行", 700.0, cap=6900.0, end="2026-06-30", rank=2),
        ],
    }
    monkeypatch.setattr(nt, "_query_rows", _fake_query(rows))
    nt.sync(engine, periods=["2026-09-30", "2026-06-30"], report_kinds=["float"], holders=[ht, csf])


def test_overview_totals_and_absent_holders(engine, monkeypatch):
    _seed_two_periods(engine, monkeypatch)
    ov = nt.overview(engine, kind="float")
    assert ov["available"] is True
    assert ov["report_date"] == "2026-09-30"
    assert ov["prev_report_date"] == "2026-06-30"
    assert ov["totals"]["stocks"] == 3
    assert ov["totals"]["market_cap"] == pytest.approx(12900.0)
    keys = {g["holder_key"]: g for g in ov["groups"]}
    assert keys["huijin_asset"]["stocks"] == 2
    assert keys["huijin_asset"]["changed_stocks"] == 1  # 美的为新进, 平安持股不变
    absent = {a["holder_key"] for a in ov["absent_holders"]}
    assert {"wutongshu", "fengshan", "kunteng"} <= absent


def test_overview_without_data_is_explicit(engine):
    ov = nt.overview(engine, kind="float")
    assert ov["available"] is False
    assert "尚未采集" in ov["reason"]


def test_holdings_filters_and_sorts(engine, monkeypatch):
    _seed_two_periods(engine, monkeypatch)
    all_rows = nt.holdings(engine, kind="float")
    assert all_rows["count"] == 3
    assert all_rows["items"][0]["stock_code"] == "601988"  # 市值最大(7000)

    only_asset = nt.holdings(engine, kind="float", holder="huijin_asset", sort="hold_num", desc=False)
    assert only_asset["count"] == 2
    assert [i["stock_code"] for i in only_asset["items"]] == ["000333", "601318"]

    by_name = nt.holdings(engine, kind="float", q="平安")
    assert by_name["count"] == 1 and by_name["items"][0]["stock_code"] == "601318"


def test_changes_zero_movement_is_not_reported_as_missing(engine, monkeypatch):
    """零变动必须能和「没有可比报告期」区分开（两者都表现为空列表，语义完全不同）。"""
    ht = nt.HOLDER_BY_KEY["huijin_invest"]
    rows = {
        (nt.REPORT_TABLES["float"], ht["name"], "2026-09-30"): [_src_row("601988", "中国银行", 100.0, cap=1.0)],
        (nt.REPORT_TABLES["float"], ht["name"], "2026-06-30"): [
            _src_row("601988", "中国银行", 100.0, cap=1.0, end="2026-06-30")
        ],
    }
    monkeypatch.setattr(nt, "_query_rows", _fake_query(rows))
    nt.sync(engine, periods=["2026-09-30", "2026-06-30"], report_kinds=["float"], holders=[ht])
    ch = nt.changes(engine, kind="float")
    assert ch["available"] is True and ch["computed"] is True
    assert ch["items"] == [] and ch["counts"]["unchanged"] == 1
    assert "reason" not in ch


def test_changes_single_period_is_explicit(engine, monkeypatch):
    ht = nt.HOLDER_BY_KEY["huijin_invest"]
    rows = {(nt.REPORT_TABLES["float"], ht["name"], "2026-09-30"): [_src_row("601988", "中国银行", 100.0)]}
    monkeypatch.setattr(nt, "_query_rows", _fake_query(rows))
    nt.sync(engine, periods=["2026-09-30"], report_kinds=["float"], holders=[ht])
    ch = nt.changes(engine, kind="float")
    assert ch["available"] is False and "只有一期数据" in ch["reason"]


def test_changes_classifies_new_exit_increase(engine, monkeypatch):
    _seed_two_periods(engine, monkeypatch)
    ch = nt.changes(engine, kind="float")
    kinds = {(i["stock_code"], i["kind"]) for i in ch["items"]}
    assert ("000333", "new") in kinds        # 上期没有 → 新进
    assert ("600585", "exit") in kinds       # 本期没有 → 退出
    assert ch["counts"]["unchanged"] == 2    # 中国平安、中国银行持股数未变
    assert ch["counts"]["new"] == 1 and ch["counts"]["exit"] == 1


def test_routes_mounted():
    from src.web.app import app

    paths = app.openapi()["paths"]
    for p in ("/api/national-team/overview", "/api/national-team/holdings",
              "/api/national-team/changes", "/api/national-team/refresh"):
        assert p in paths, f"{p} 未挂载"
