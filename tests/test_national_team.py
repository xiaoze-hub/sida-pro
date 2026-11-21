# -*- coding: utf-8 -*-
"""国家队 / 机构持仓（汇金 · 证金 · 社保 · 险资 · 外汇局）采集与读取测试（2026-11-18）。

不打上游：`_rows_once` / `_query_names_in` 用假数据替换，测的是**本仓契约**——
①迁移建表并注册；②名称归类（社保组合 / 险资产品账户 / 汇金 / 证金，且不互相误收）；
③全市场扫描只留命中行并整期替换；④注册表被填充，定向刷新只查名单里的人；
⑤汇总按机构组聚合（股票去重、持股相加、账户数）；⑥零变动 vs 没有可比报告期可区分。

不覆盖（上游本就不提供）：ETF 持仓、第 11 名以后的持仓、A/H 拆分。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from src.core import national_team as nt
from src.web.migrations import MIGRATIONS, run_versioned_migrations


def _src(code: str, name: str, holder: str, hold_num: float, *, end="2026-09-30",
         ratio=None, cap=None, rank=1, change="不变", stype="流通A股"):
    return {
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": name,
        "HOLDER_NAME": holder,
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


def _fake_scan(rows_by_period):
    """替换 `_rows_once`：按 (报表, 报告期) 一次返回全量行（模拟不分页）。"""

    def _rows_once(report_table, filt, page, *, http=None, page_size=2000):
        period = filt.split("END_DATE='")[1][:10]
        rows = rows_by_period.get((report_table, period), [])
        if page > 1:
            return [], 1, True
        return list(rows), 1, True

    return _rows_once


# ────────────────────────── 迁移 ──────────────────────────


def test_migrations_registered(engine):
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(versions)
    assert versions == list(range(versions[0], versions[-1] + 1))
    assert {251, 252} <= set(versions)
    with engine.connect() as conn:
        hold_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(national_team_holdings)")).fetchall()}
        reg_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(national_team_holder_registry)")).fetchall()}
    assert {"report_date", "report_kind", "holder_key", "stock_code", "group_key"} <= hold_cols
    assert {"holder_name", "group_key", "holder_group", "last_seen"} <= reg_cols


# ────────────────────────── 归类 ──────────────────────────


@pytest.mark.parametrize(
    "name,expect",
    [
        ("中央汇金投资有限责任公司", "huijin"),
        ("中央汇金资产管理有限责任公司", "huijin"),
        ("中国证券金融股份有限公司", "csf"),
        ("梧桐树投资平台有限责任公司", "safe"),
        ("北京凤山投资有限责任公司", "safe"),
        ("北京坤藤投资有限责任公司", "safe"),
        ("全国社保基金一一三组合", "ssb"),
        ("全国社保基金四一三组合", "ssb"),
        ("全国社会保障基金理事会转持二户", "ssb"),
        ("中国人寿保险股份有限公司-传统-普通保险产品-005L-CT001沪", "insurance"),
        ("中国平安人寿保险股份有限公司-分红-个险分红", "insurance"),
        ("中国平安保险(集团)股份有限公司-集团本级-自有资金", "insurance"),
        ("中国人民财产保险股份有限公司-传统-普通保险产品", "insurance"),
        ("平安养老保险股份有限公司-传统-普通保险产品", "insurance"),
        # 不该被收的：普通机构/个人
        ("香港中央结算有限公司", None),
        ("中国工商银行股份有限公司", None),
        ("招商银行股份有限公司-某某基金", None),
        ("张三", None),
        ("", None),
    ],
)
def test_classify_holder(name, expect):
    assert nt.classify_holder(name) == expect


def test_holder_key_is_stable_and_grouped():
    a = nt.holder_key_for("全国社保基金一一三组合", "ssb")
    b = nt.holder_key_for("全国社保基金一一三组合", "ssb")
    assert a == b and a.startswith("ssb:") and len(a) <= 32
    assert a != nt.holder_key_for("全国社保基金一一四组合", "ssb")


# ────────────────────────── 归一化 ──────────────────────────


def test_normalize_drops_invalid_and_unclassified():
    raw = [
        _src("601988", "中国银行", "中央汇金投资有限责任公司", 1.887e11, cap=1.27e12, ratio=58.59),
        _src("601318", "中国平安", "中国平安人寿保险股份有限公司-分红-个险分红", 5e8, cap=5e9),
        _src("601", "坏代码", "中央汇金投资有限责任公司", 100.0),
        _src("６０１０００", "全角代码", "中央汇金投资有限责任公司", 100.0),
        _src("600000", "零持股", "中央汇金投资有限责任公司", 0),
        {**_src("600002", "日期不符", "中央汇金投资有限责任公司", 10.0), "END_DATE": "2026-06-30 00:00:00"},
        _src("600003", "漏网之鱼", "香港中央结算有限公司", 10.0),
    ]
    rows, stats = nt.normalize_rows(
        raw, holder=None, report_kind="float", report_date="2026-09-30", snapshot_at="2026-11-18T19:00:00+08:00"
    )
    codes = {r["stock_code"] for r in rows}
    assert codes == {"601988", "601318"}
    groups = {r["stock_code"]: r["group_key"] for r in rows}
    assert groups["601988"] == "huijin" and groups["601318"] == "insurance"
    assert stats["dropped_unclassified"] == 1
    assert stats["dropped_code"] == 2 and stats["dropped_value"] == 1 and stats["dropped_date"] == 1


# ────────────────────────── 全市场扫描 ──────────────────────────


def _scan_rows(period="2026-09-30"):
    return [
        _src("601988", "中国银行", "中央汇金投资有限责任公司", 100.0, cap=1000.0, ratio=58.59, end=period),
        _src("601398", "工商银行", "中央汇金资产管理有限责任公司", 50.0, cap=500.0, ratio=0.28, end=period),
        _src("601398", "工商银行", "全国社保基金一一三组合", 20.0, cap=200.0, ratio=0.1, end=period),
        _src("000333", "美的集团", "全国社保基金四一三组合", 10.0, cap=100.0, ratio=1.1, end=period),
        _src("601318", "中国平安", "中国平安人寿保险股份有限公司-分红-个险分红", 30.0, cap=300.0, ratio=2.6, end=period),
        _src("600000", "浦发银行", "香港中央结算有限公司", 999.0, cap=999.0, end=period),  # 不归类 → 丢
    ]


def test_scan_keeps_only_institutions_and_replaces_period(engine, monkeypatch):
    rows = {
        (nt.REPORT_TABLES["float"], "2026-09-30"): _scan_rows("2026-09-30"),
        (nt.REPORT_TABLES["total"], "2026-09-30"): _scan_rows("2026-09-30"),
    }
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    rc = nt.scan_period(engine, "2026-09-30", report_kinds=["float"])
    assert rc["ok"] is True
    assert rc["scanned_rows"]["2026-09-30/float"] == 6
    assert rc["written"] == 5  # 香港中央结算被丢弃
    assert rc["groups"]["float"] == {"huijin": 2, "ssb": 2, "insurance": 1}

    # 幂等：同数据再扫一次行数不变
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])
    h = nt.holdings(engine, report_date="2026-09-30", kind="float")
    assert h["count"] == 5

    # 整期替换：源端少几只 → 库里不残留
    rows[(nt.REPORT_TABLES["float"], "2026-09-30")] = _scan_rows("2026-09-30")[:3]
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])
    h2 = nt.holdings(engine, report_date="2026-09-30", kind="float")
    assert h2["count"] == 3


def test_scan_populates_registry_and_targeted_uses_it(engine, monkeypatch):
    rows = {(nt.REPORT_TABLES["float"], "2026-09-30"): _scan_rows("2026-09-30")}
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])

    names = nt.registry_names(engine)
    assert "全国社保基金一一三组合" in names and "中央汇金投资有限责任公司" in names
    assert "香港中央结算有限公司" not in names
    assert set(nt.registry_names(engine, "insurance")) == {
        "中国平安人寿保险股份有限公司-分红-个险分红"
    }

    # 定向刷新只查名单里的人（`in (...)`），且能吸收「源端新出现一只」
    captured: dict[str, list[str]] = {}

    def _fake_names_in(report_table, names_arg, report_date, **kwargs):
        captured["names"] = list(names_arg)
        return [
            _src("601988", "中国银行", "中央汇金投资有限责任公司", 100.0, cap=1000.0),
            _src("600036", "招商银行", "全国社保基金四一三组合", 5.0, cap=50.0),  # 新出现
        ]

    monkeypatch.setattr(nt, "_query_names_in", _fake_names_in)
    rc = nt.sync_targeted(engine, periods=["2026-09-30"], report_kinds=["float"])
    assert rc["written"] == 2
    assert "香港中央结算有限公司" not in captured["names"]
    h = nt.holdings(engine, report_date="2026-09-30", kind="float")
    assert "600036" in {i["stock_code"] for i in h["items"]}


# ────────────────────────── 汇总 / 明细 / 环比 ──────────────────────────


def _seed_two_periods(engine, monkeypatch):
    cur = _scan_rows("2026-09-30")
    prev = [
        _src("601988", "中国银行", "中央汇金投资有限责任公司", 100.0, cap=900.0, end="2026-06-30"),
        _src("601398", "工商银行", "中央汇金资产管理有限责任公司", 50.0, cap=480.0, end="2026-06-30"),
        _src("601398", "工商银行", "全国社保基金一一三组合", 20.0, cap=190.0, end="2026-06-30"),
        _src("000333", "美的集团", "全国社保基金四一三组合", 10.0, cap=95.0, end="2026-06-30"),
        # 上期持有招商银行（本期退出）
        _src("600036", "招商银行", "全国社保基金四一三组合", 8.0, cap=80.0, end="2026-06-30"),
        # 上期没有平安人寿（本期新进）
    ]
    rows = {
        (nt.REPORT_TABLES["float"], "2026-09-30"): cur,
        (nt.REPORT_TABLES["float"], "2026-06-30"): prev,
    }
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    nt.scan_period(engine, "2026-06-30", report_kinds=["float"])
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])


def test_overview_aggregates_by_group(engine, monkeypatch):
    _seed_two_periods(engine, monkeypatch)
    ov = nt.overview(engine, kind="float")
    assert ov["available"] is True
    assert ov["report_date"] == "2026-09-30" and ov["prev_report_date"] == "2026-06-30"
    groups = {g["group_key"]: g for g in ov["groups"]}
    # 汇金系：中国银行 + 工商银行两只（两个账户）
    assert groups["huijin"]["stocks"] == 2 and groups["huijin"]["holders"] == 2
    assert groups["huijin"]["market_cap"] == pytest.approx(1500.0)
    # 社保：工商银行 + 美的集团（两个组合）
    assert groups["ssb"]["stocks"] == 2 and groups["ssb"]["holders"] == 2
    assert groups["ssb"]["market_cap"] == pytest.approx(300.0)
    # 险资：只有平安
    assert groups["insurance"]["stocks"] == 1
    # 合计只数**按股票去重**：601988/601398/000333/601318 = 4（601398 同时被汇金与社保持有）
    assert ov["totals"]["stocks"] == 4
    assert ov["totals"]["group_stocks_summed"] == 5
    # 未在榜组：证金、外汇局
    absent = {a["group_key"] for a in ov["absent_holders"]}
    assert {"csf", "safe"} <= absent


def test_overview_without_data_is_explicit(engine):
    ov = nt.overview(engine, kind="float")
    assert ov["available"] is False and "尚未采集" in ov["reason"]


def test_holdings_filters_by_group_and_account(engine, monkeypatch):
    _seed_two_periods(engine, monkeypatch)
    all_rows = nt.holdings(engine, kind="float")
    assert all_rows["count"] == 5

    ssb = nt.holdings(engine, kind="float", holder="ssb")
    assert ssb["count"] == 2
    assert {i["group_key"] for i in ssb["items"]} == {"ssb"}
    # 具体账户名保留在明细里（社保组合 / 险资产品名）
    assert any("全国社保基金" in (i["holder_name"] or "") for i in ssb["items"])

    by_name = nt.holdings(engine, kind="float", q="平安")
    assert by_name["count"] == 1 and by_name["items"][0]["stock_code"] == "601318"


def test_changes_are_group_level(engine, monkeypatch):
    _seed_two_periods(engine, monkeypatch)
    ch = nt.changes(engine, kind="float")
    kinds = {(i["group_key"], i["stock_code"], i["kind"]) for i in ch["items"]}
    assert ("insurance", "601318", "new") in kinds       # 平安新进
    assert ("ssb", "600036", "exit") in kinds            # 招行退出
    assert ch["available"] is True and ch["computed"] is True


def test_changes_zero_movement_is_not_missing(engine, monkeypatch):
    rows = {
        (nt.REPORT_TABLES["float"], "2026-09-30"): [
            _src("601988", "中国银行", "中央汇金投资有限责任公司", 100.0, cap=1.0),
        ],
        (nt.REPORT_TABLES["float"], "2026-06-30"): [
            _src("601988", "中国银行", "中央汇金投资有限责任公司", 100.0, cap=1.0, end="2026-06-30"),
        ],
    }
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    nt.scan_period(engine, "2026-06-30", report_kinds=["float"])
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])
    ch = nt.changes(engine, kind="float")
    assert ch["available"] is True and ch["items"] == [] and ch["counts"]["unchanged"] == 1
    assert "reason" not in ch


def test_changes_single_period_is_explicit(engine, monkeypatch):
    rows = {(nt.REPORT_TABLES["float"], "2026-09-30"): [
        _src("601988", "中国银行", "中央汇金投资有限责任公司", 100.0)]}
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])
    ch = nt.changes(engine, kind="float")
    # 文案不再用「只有一期数据」猜 idx==0：说清「没有更早一期」，且不拿最新一期顶替。
    assert ch["available"] is False
    assert "没有早于 2026-09-30 的报告期" in ch["reason"]
    assert "不拿最新一期顶替" in ch["reason"]


def test_changes_refuses_non_adjacent_prev_period(engine, monkeypatch):
    """缺季时不许拿早几个季度的一期当「上期」：跨期的「新进/退出」会失真。"""
    rows = {
        (nt.REPORT_TABLES["float"], "2026-09-30"): [
            _src("601988", "中国银行", "中央汇金投资有限责任公司", 100.0, end="2026-09-30")],
        (nt.REPORT_TABLES["float"], "2025-09-30"): [
            _src("601988", "中国银行", "中央汇金投资有限责任公司", 80.0, end="2025-09-30")],
    }
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    nt.scan_period(engine, "2025-09-30", report_kinds=["float"])
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])

    ch = nt.changes(engine, kind="float")

    assert ch["available"] is False and ch["status"] == "non_adjacent_period"
    assert "缺少 2026-06-30" in ch["reason"] and "跨期" in ch["reason"]
    assert ch["items"] == [] and ch["count"] == 0


def test_changes_allows_explicit_yoy_prev(engine, monkeypatch):
    """显式比较期允许跨 4 个季度（同比），但结果必须标成 yoy，不能叫环比。"""
    rows = {}
    for period, num in (("2026-09-30", 100.0), ("2026-06-30", 90.0), ("2025-09-30", 60.0)):
        rows[(nt.REPORT_TABLES["float"], period)] = [
            _src("601988", "中国银行", "中央汇金投资有限责任公司", num, end=period)]
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    for period in ("2025-09-30", "2026-06-30", "2026-09-30"):
        nt.scan_period(engine, period, report_kinds=["float"])

    ch = nt.changes(engine, kind="float", report_date="2026-09-30",
                    prev_report_date="2025-09-30")

    assert ch["available"] is True and ch["comparison"] == "yoy"
    assert ch["prev_report_date"] == "2025-09-30" and ch["compare_gap"] == 4
    assert ch["counts"]["increase"] == 1


def test_routes_mounted():
    from src.web.app import app

    paths = app.openapi()["paths"]
    for p in ("/api/national-team/overview", "/api/national-team/holdings",
              "/api/national-team/changes", "/api/national-team/refresh"):
        assert p in paths, f"{p} 未挂载"


# ────────────────────────── 问句解析（2026-11-21 直查） ──────────────────────────


@pytest.mark.parametrize(
    "text,scope,value",
    [
        ("汇金资管这季持仓", "holder", "huijin_asset"),
        ("中央汇金投资持仓", "holder", "huijin_invest"),
        ("汇金持仓", "group", "huijin"),
        ("社保基金持仓", "group", "ssb"),
        ("中国证券金融持仓", "group", "csf"),
    ],
)
def test_resolve_institution_scopes_prefers_account_over_group(text, scope, value):
    """账户别名命中后不能再被组别名吃掉：问「汇金资管」不能答「汇金系合计」。"""
    scopes = nt.resolve_institution_scopes(text)
    assert scopes and scopes[0]["scope"] == scope and scopes[0]["value"] == value
    assert len(scopes) == 1


def test_resolve_institution_scopes_ignores_unrelated_names():
    assert nt.resolve_institution_scopes("中国平安持仓") == []
    assert nt.resolve_institution_scopes("招商银行分红") == []


def test_national_team_word_expands_to_all_groups():
    vals = {s["value"] for s in nt.resolve_institution_scopes("国家队持仓")}
    assert vals == set(nt.GROUP_ORDER)


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("new", ("new",)),
        ("新进,加仓", ("new", "increase")),
        (["exit", "减仓"], ("exit", "decrease")),
        ("bogus", ()),
        (None, ()),
    ],
)
def test_normalize_change_kinds(raw, expect):
    assert nt.normalize_change_kinds(raw) == expect


# ────────────────────────── 状态区分与过滤顺序 ──────────────────────────


def test_overview_unknown_period_is_explicit(engine, monkeypatch):
    rows = {(nt.REPORT_TABLES["float"], "2026-09-30"): _scan_rows("2026-09-30")}
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])
    ov = nt.overview(engine, report_date="2025-12-31", kind="float")
    assert ov["available"] is False and ov["status"] == "unknown_period"
    assert "没有 2025-12-31 这一报告期" in ov["reason"]
    assert ov["periods"] == ["2026-09-30"]


def test_holdings_limit_does_not_shrink_total(engine, monkeypatch):
    """`count` 是过滤后总数；`items` 才是被 limit 截断的页。"""
    big = [_src(f"{600000 + i:06d}", f"票{i}", "中央汇金投资有限责任公司", 10.0 + i)
           for i in range(260)]
    rows = {(nt.REPORT_TABLES["float"], "2026-09-30"): big}
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])
    page = nt.holdings(engine, kind="float", holder="huijin", limit=20)
    assert page["count"] == 260 and len(page["items"]) == 20
    assert nt.scope_summary(engine, kind="float", holder="huijin")["stocks"] == 260


def test_changes_filter_before_limit(engine, monkeypatch):
    """先按机构+变动过滤再截断：不能拿全市场前 N 条冒充某机构全集。"""
    cur = [_src(f"{600000 + i:06d}", f"汇金{i}", "中央汇金投资有限责任公司", 10.0)
           for i in range(5)]
    cur += [_src(f"{300000 + i:06d}", f"社保{i}", "全国社保基金一一三组合", 20.0)
            for i in range(150)]
    rows = {
        (nt.REPORT_TABLES["float"], "2026-09-30"): cur,
        # 上期至少留一行，否则该期不会落库、会被当成「只有一期」
        (nt.REPORT_TABLES["float"], "2026-06-30"): [
            _src("600999", "旧持仓", "中央汇金投资有限责任公司", 1.0, end="2026-06-30", cap=1.0)
        ],
    }
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    nt.scan_period(engine, "2026-06-30", report_kinds=["float"])
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])

    ssb_new = nt.changes(engine, kind="float", holder="ssb", change="new", limit=10)
    assert ssb_new["counts"]["new"] == 150
    assert ssb_new["count"] == 150 and ssb_new["truncated"] is True
    assert len(ssb_new["items"]) == 10
    assert {i["group_key"] for i in ssb_new["items"]} == {"ssb"}
    assert ssb_new["status"] == "ok"


def test_changes_explicit_older_period_uses_next_period_as_base(engine, monkeypatch):
    """显式指定较早报告期时，比较基准应是它的**下一期**，不是最新期。"""
    periods = {"2026-03-31": 10.0, "2026-06-30": 20.0, "2026-09-30": 30.0}
    rows = {
        (nt.REPORT_TABLES["float"], p): [
            _src("601988", "中国银行", "中央汇金投资有限责任公司", n, end=p, cap=n)
        ]
        for p, n in periods.items()
    }
    monkeypatch.setattr(nt, "_rows_once", _fake_scan(rows))
    for p in periods:
        nt.scan_period(engine, p, report_kinds=["float"])
    ch = nt.changes(engine, report_date="2026-06-30", kind="float", holder="huijin")
    assert ch["prev_report_date"] == "2026-03-31"
    assert ch["items"][0]["kind"] == "increase"
    assert ch["items"][0]["delta_num"] == pytest.approx(10.0)


def test_changes_holder_absent_is_empty_result_not_zero_change(engine, monkeypatch):
    _seed_two_periods(engine, monkeypatch)
    ch = nt.changes(engine, kind="float", holder="csf")
    assert ch["status"] == "empty_result"
    assert ch["available"] is False
    assert "不是取数失败" in ch["reason"]


def test_holdings_unknown_holder_is_empty_result(engine, monkeypatch):
    _seed_two_periods(engine, monkeypatch)
    h = nt.holdings(engine, kind="float", holder="csf")
    assert h["status"] == "empty_result" and h["count"] == 0
