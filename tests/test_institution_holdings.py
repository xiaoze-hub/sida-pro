# -*- coding: utf-8 -*-
"""机构披露持仓确定性直查（参谋首批 C，2026-11-21）。

测的是**本仓契约**，不打上游、不连生产：

① 问句解析：机构/报告期/榜单口径/变动类型都从字面确定；会话绑定不能当机构；
   个股分析、混合资金问题、信息不足各有明确出口（None / 追问）。
② 取数顺序：先按机构（SQL）与变动类型过滤、拿准确总量，再截断展示 ——
   数据量刻意超过默认 limit（260 条持仓、150 条新进），断言总量不是被截断后的条数。
③ 同股不同持有人、同机构多组、float/total 两套榜单：合计只数去重、账户相加、
   两级口径不混算。
④ 四种「没数据」分开说：无该报告期 / 比较期不足 / 确实零变动 / 取数错误。
⑤ 权限与身份：未登录、无 view_quotes 一律拒绝作答。
⑥ 与**同一查询**的 API 层对拍（monkeypatch 路由的 engine + 缓存直通）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from src.core import national_team as nt
from src.harness import institution_holdings as ih
from src.web.migrations import run_versioned_migrations

PERIOD_CUR = "2026-09-30"
PERIOD_PREV = "2026-06-30"


def _src(code, name, holder, hold_num, *, end, cap, ratio=None, rank=1, stype="流通A股"):
    return {
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": name,
        "HOLDER_NAME": holder,
        "END_DATE": f"{end} 00:00:00",
        "HOLD_NUM": hold_num,
        "HOLD_NUM_RATIO": ratio,
        "HOLDER_MARKET_CAP": cap,
        "HOLDER_RANK": rank,
        "HOLD_NUM_CHANGE": "不变",
        "SHARES_TYPE": stype,
    }


def _fake_rows(rows_by_key):
    def _rows_once(report_table, filt, page, *, http=None, page_size=2000):
        period = filt.split("END_DATE='")[1][:10]
        if page > 1:
            return [], 1, True
        return list(rows_by_key.get((report_table, period), [])), 1, True

    return _rows_once


def _seed(engine, monkeypatch):
    """两期数据：汇金 260 只（跨默认 limit）、社保 150 只新进、同股双账户、险资、total 口径另算。"""
    cur: list[dict] = []
    prev: list[dict] = []
    # 汇金：260 只。0-99 不变，100-199 加仓，200-249 本期新进（上期没有），250-259 上期持有本期退出
    for i in range(260):
        code = f"{600000 + i:06d}"
        cur.append(_src(code, f"测试{i}", "中央汇金投资有限责任公司",
                        hold_num=1000.0 + i, end=PERIOD_CUR, cap=(i + 1) * 1e8, ratio=1.0, rank=3))
        if i < 100:
            prev.append(_src(code, f"测试{i}", "中央汇金投资有限责任公司",
                             1000.0 + i, end=PERIOD_PREV, cap=(i + 1) * 0.9e8, ratio=1.0, rank=3))
        elif i < 200:
            prev.append(_src(code, f"测试{i}", "中央汇金投资有限责任公司",
                             1000.0, end=PERIOD_PREV, cap=(i + 1) * 0.9e8, ratio=1.0, rank=3))
        elif i >= 250:
            prev.append(_src(code, f"测试{i}", "中央汇金投资有限责任公司",
                             500.0, end=PERIOD_PREV, cap=1e8, ratio=1.0, rank=9))

    # 同股不同持有人：601398 被汇金投资与汇金资管同时持有（组内两个账户）
    cur.append(_src("601398", "工商银行", "中央汇金投资有限责任公司", 10000.0,
                    end=PERIOD_CUR, cap=1e11, ratio=0.5, rank=1))
    prev.append(_src("601398", "工商银行", "中央汇金投资有限责任公司", 9000.0,
                     end=PERIOD_PREV, cap=9e10, ratio=0.5, rank=1))
    cur.append(_src("601398", "工商银行", "中央汇金资产管理有限责任公司", 8000.0,
                    end=PERIOD_CUR, cap=8e10, ratio=0.4, rank=2))
    prev.append(_src("601398", "工商银行", "中央汇金资产管理有限责任公司", 8000.0,
                     end=PERIOD_PREV, cap=8e10, ratio=0.4, rank=2))

    # 社保：150 只本期新进（上期完全没有），另有 5 只加仓
    for i in range(150):
        code = f"{300000 + i:06d}"
        cur.append(_src(code, f"社保股{i}", "全国社保基金一一三组合",
                        2000.0 + i, end=PERIOD_CUR, cap=(i + 1) * 1e7, ratio=0.5, rank=5))
    for i in range(5):
        code = f"{301000 + i:06d}"
        cur.append(_src(code, f"社保加仓{i}", "全国社保基金四一三组合",
                        3000.0, end=PERIOD_CUR, cap=1e8, ratio=0.6, rank=4))
        prev.append(_src(code, f"社保加仓{i}", "全国社保基金四一三组合",
                         1000.0, end=PERIOD_PREV, cap=0.8e8, ratio=0.2, rank=8))

    # 险资
    cur.append(_src("601318", "中国平安", "中国平安人寿保险股份有限公司-分红-个险分红",
                    5000.0, end=PERIOD_CUR, cap=2e9, ratio=2.0, rank=4))

    # total 口径：只给 3 条汇金（与 float 的 262 条完全不同），验证两种榜单不混算
    total_cur = [
        _src("601988", "中国银行", "中央汇金投资有限责任公司", 1.0e11,
             end=PERIOD_CUR, cap=9e11, ratio=58.0, rank=1),
        _src("601398", "工商银行", "中央汇金投资有限责任公司", 9.0e10,
             end=PERIOD_CUR, cap=8e11, ratio=40.0, rank=1),
        _src("601288", "农业银行", "中央汇金投资有限责任公司", 7.0e10,
             end=PERIOD_CUR, cap=6e11, ratio=45.0, rank=1),
    ]
    total_prev = [
        _src("601988", "中国银行", "中央汇金投资有限责任公司", 1.0e11,
             end=PERIOD_PREV, cap=9e11, ratio=58.0, rank=1),
    ]

    rows = {
        (nt.REPORT_TABLES["float"], PERIOD_CUR): cur,
        (nt.REPORT_TABLES["float"], PERIOD_PREV): prev,
        (nt.REPORT_TABLES["total"], PERIOD_CUR): total_cur,
        (nt.REPORT_TABLES["total"], PERIOD_PREV): total_prev,
    }
    monkeypatch.setattr(nt, "_rows_once", _fake_rows(rows))
    nt.scan_period(engine, PERIOD_PREV, report_kinds=["float", "total"])
    nt.scan_period(engine, PERIOD_CUR, report_kinds=["float", "total"])


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/institution_holdings.db")
    run_versioned_migrations(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def seeded(engine, monkeypatch):
    _seed(engine, monkeypatch)
    return engine


def _pro():
    return SimpleNamespace(id="u-pro", role="pro")


async def _ask(engine, text, *, user=None, bound=None):
    return await ih.answer(engine, text, bound, user if user is not None else _pro())


# ────────────────────────── 问句解析 ──────────────────────────


@pytest.mark.parametrize(
    "text,scope,value",
    [
        ("汇金持仓", "group", "huijin"),
        ("中央汇金投资这季度持仓", "holder", "huijin_invest"),
        ("汇金资管持仓多少", "holder", "huijin_asset"),
        ("证金公司持仓", "group", "csf"),
        ("社保基金新进了哪些股", "group", "ssb"),
        ("险资持仓变动", "group", "insurance"),
        ("外汇局三平台持仓", "group", "safe"),
        ("梧桐树持仓", "group", "safe"),
    ],
)
def test_resolve_scopes(text, scope, value):
    scopes = nt.resolve_institution_scopes(text)
    assert scopes and scopes[0]["scope"] == scope and scopes[0]["value"] == value


def test_national_team_expands_to_all_groups():
    vals = {s["value"] for s in nt.resolve_institution_scopes("国家队持仓")}
    assert vals == set(nt.GROUP_ORDER)


def test_plain_company_name_is_not_an_institution():
    assert nt.resolve_institution_scopes("中国平安持仓") == []
    assert nt.resolve_institution_scopes("600988 现价") == []


def test_bound_is_never_treated_as_institution():
    """会话锚点只有个股代码 —— 不能因为上一轮聊过汇金就把「它这季加了吗」当机构问题。"""
    q = ih.parse_question("它这季加仓了吗")
    assert q.scopes == []
    assert ih.parse_question("600988 现价多少").scopes == []


@pytest.mark.parametrize(
    "text",
    [
        "汇金持仓的中国银行能买吗",     # 明示个股分析
        "汇金这季度怎么样",             # 判断意图
        "汇金和北向资金谁持仓多",       # 混合资金
        "社保和公募谁重仓中芯国际",     # 混合机构
        "汇金持仓有什么风险",
        "汇金和社保谁持仓多",           # 多机构比较：要分机构口径，不硬答合计
    ],
)
def test_analysis_and_mixed_questions_are_not_taken_over(text):
    q = ih.parse_question(text)
    assert q.mode == "not_ours" and q.ambiguous is None


@pytest.mark.parametrize(
    "text,needle",
    [
        ("汇金2026年情况", "只给了"),
        ("汇金2026-09 持仓", "完整报告期"),
        ("汇金十大股东和十大流通股东持仓", "两个口径"),
        ("汇金", "缺关键信息"),
    ],
)
def test_insufficient_info_asks_for_clarification(text, needle):
    q = ih.parse_question(text)
    assert q.ambiguous and needle in q.ambiguous


@pytest.mark.parametrize(
    "text,kind,mode,kinds",
    [
        ("汇金十大股东持仓", "total", "holdings", ()),
        ("汇金十大流通股东持仓", "float", "holdings", ()),
        ("社保最新一期新进了哪些股", "float", "changes", ("new",)),
        ("险资上季度减仓了什么", "float", "changes", ("decrease",)),
        ("汇金退出哪些股", "float", "changes", ("exit",)),
    ],
)
def test_parse_mode_and_kind(text, kind, mode, kinds):
    q = ih.parse_question(text)
    assert q.kind == kind and q.mode == mode and q.change_kinds == kinds


# ────────────────────────── 主路径：先过滤再截断 ──────────────────────────


async def test_holdings_over_limit_keeps_accurate_total(seeded):
    """汇金 261 只 / 262 条记录（> 默认 200）：答案必须说全量，不是只列 8 条。"""
    core = nt.holdings(seeded, kind="float", holder="huijin")
    summary = nt.scope_summary(seeded, kind="float", holder="huijin")
    assert core["count"] == 262 and summary["stocks"] == 261 and summary["holders"] == 2

    out = await _ask(seeded, "汇金最新一期十大流通股东持仓")
    assert "共 261 只 · 262 条持仓记录 · 2 个账户" in out
    assert "只列前 8 条" in out
    assert out.count("\n1. ") == 1  # 明细只列前 8 条
    for i in range(2, 9):
        assert f"\n{i}. " in out
    assert "\n9. " not in out
    assert "/national-team?kind=float" in out and "holder=huijin" in out


async def test_changes_filter_before_truncation(seeded):
    """社保新进 150 条（> 默认 100）：答案总量必须是 150，明细只列 10 条。"""
    core = nt.changes(seeded, kind="float", holder="ssb", change="new", limit=100)
    assert core["count"] == 150 and core["counts"]["new"] == 150 and core["truncated"] is True

    out = await _ask(seeded, "社保基金最新新进了哪些股")
    assert "新进 150" in out
    assert "共 150 条" in out and "只列前 10 条" in out
    assert "change=new" in out and "holder=ssb" in out
    # 普通环比深链不携带比较期，页面仍按相邻上一季算。
    assert "prev_report_date" not in out


async def test_changes_filter_by_institution_not_global_top(seeded):
    """按机构过滤必须发生在截断前：全市场新进 153 条里社保占 150。"""
    all_new = nt.changes(seeded, kind="float", change="new", limit=500)
    ssb_new = nt.changes(seeded, kind="float", holder="ssb", change="new", limit=500)
    assert all_new["count"] >= 153
    assert ssb_new["count"] == 150
    assert {i["group_key"] for i in ssb_new["items"]} == {"ssb"}


async def test_same_stock_two_holders_aggregated_in_changes(seeded):
    """601398 同时被汇金投资（10000/9000）与汇金资管（8000/8000）持有：组内相加后再比。"""
    core = nt.changes(seeded, kind="float", holder="huijin", limit=500)
    row = next(i for i in core["items"] if i["stock_code"] == "601398")
    assert row["kind"] == "increase"
    assert row["cur_num"] == pytest.approx(18000.0)   # 10000 + 8000
    assert row["prev_num"] == pytest.approx(17000.0)  # 9000 + 8000
    assert row["delta_num"] == pytest.approx(1000.0)
    assert row["cur_holders"] == 2

    # 明细里两个账户分别出现（同股不同持有人不合并成一条）
    hold = nt.holdings(seeded, kind="float", holder="huijin", q="601398")
    assert hold["count"] == 2
    assert len({i["holder_key"] for i in hold["items"]}) == 2


async def test_total_and_float_are_not_mixed(seeded):
    total = await _ask(seeded, "汇金最新十大股东持仓")
    assert "共 3 只" in total and "kind=total" in total
    assert "601988" in total
    float_out = await _ask(seeded, "汇金最新十大流通股东持仓")
    assert "共 261 只" in float_out and "kind=float" in float_out
    # 只出现在 float 里的代码不会跑进 total 答案
    assert "600005" not in total


async def test_stock_scoped_question_uses_code_filter(seeded):
    out = await _ask(seeded, "汇金在 601398 的持仓")
    assert "标的 601398" in out and "2 条持仓记录" in out
    assert "q=601398" in out


async def test_hujin_asset_account_filter_excludes_hujin_invest(seeded):
    """扫描路径落库的 holder_key 是 `组:哈希`；账户过滤必须走全称，不能只比种子 key。"""
    asset = nt.holdings(seeded, kind="float", holder="huijin_asset")
    invest = nt.holdings(seeded, kind="float", holder="huijin_invest")
    assert asset["count"] == 1
    assert asset["items"][0]["holder_name"] == "中央汇金资产管理有限责任公司"
    assert invest["count"] == 261
    assert all(i["holder_name"] == "中央汇金投资有限责任公司" for i in invest["items"])
    assert nt.scope_summary(seeded, kind="float", holder="huijin_asset")["stocks"] == 1

    out = await _ask(seeded, "汇金资管最新持仓")
    assert out
    assert "汇金·资管" in out and "1 条持仓记录" in out
    assert "holder=huijin_asset" in out


# ────────────────────────── 四种「没数据」 ──────────────────────────


async def test_unknown_period_is_not_no_holdings(seeded):
    out = await _ask(seeded, "汇金 2025-12-31 持仓")
    assert "没有 2025-12-31 这一报告期" in out
    assert "已采集" in out
    assert "没有持仓" not in out.split("站内同口径页面")[0][:40]


async def test_insufficient_periods_is_explicit(engine, monkeypatch):
    rows = {(nt.REPORT_TABLES["float"], PERIOD_CUR): [
        _src("601398", "工商银行", "中央汇金投资有限责任公司", 100.0, end=PERIOD_CUR, cap=1e8),
    ]}
    monkeypatch.setattr(nt, "_rows_once", _fake_rows(rows))
    nt.scan_period(engine, PERIOD_CUR, report_kinds=["float"])
    out = await _ask(engine, "汇金最新持股变动")
    assert "没有早于 2026-09-30 的报告期" in out and "无法计算环比" in out


async def test_zero_change_is_not_fetch_error(seeded):
    out = await _ask(seeded, "证金公司最新持仓变动")
    # 证金没数据 → empty_result（不在榜），不是零变动
    assert "未出现在十大股东" in out and "不是取数失败" in out


async def test_true_zero_change(engine, monkeypatch):
    rows = {
        (nt.REPORT_TABLES["float"], PERIOD_CUR): [
            _src("601398", "工商银行", "中央汇金投资有限责任公司", 100.0, end=PERIOD_CUR, cap=1e8),
        ],
        (nt.REPORT_TABLES["float"], PERIOD_PREV): [
            _src("601398", "工商银行", "中央汇金投资有限责任公司", 100.0, end=PERIOD_PREV, cap=1e8),
        ],
    }
    monkeypatch.setattr(nt, "_rows_once", _fake_rows(rows))
    nt.scan_period(engine, PERIOD_PREV, report_kinds=["float"])
    nt.scan_period(engine, PERIOD_CUR, report_kinds=["float"])
    out = await _ask(engine, "汇金最新持仓变动")
    assert "确实零变动" in out and "不是取数失败" in out


async def test_fetch_error_is_reported_as_error(seeded, monkeypatch):
    """取数异常要说「取数失败」，但**不许把底层异常/SQL/连接信息透给用户**。"""

    def _boom(*a, **k):
        raise RuntimeError("OperationalError: password=sup3rsecret host=db.internal")

    monkeypatch.setattr(nt, "holdings", _boom)
    out = await _ask(seeded, "汇金最新持仓")
    assert "取数失败" in out
    assert "零变动" not in out
    for leak in ("sup3rsecret", "db.internal", "OperationalError", "Traceback"):
        assert leak not in out, f"底层异常细节泄露: {leak}"


async def test_period_lookup_failure_is_guarded(seeded, monkeypatch):
    """`persisted_periods` 也是查库，必须落在同一段保护里（不能抛穿 async 调用方）。"""

    def _boom(*a, **k):
        raise RuntimeError("could not connect to server: db.internal:5432")

    monkeypatch.setattr(nt, "persisted_periods", _boom)
    out = await _ask(seeded, "汇金最新持仓")
    assert out and "取数失败" in out
    assert "db.internal" not in out and "could not connect" not in out


class _Trace:
    """轨迹句柄替身（字段与 CopilotTrace.step 对齐）。"""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.run_id = "run-t-ih"

    def step(self, tool, args=None, *, ok=True, error=None, result=None, elapsed_ms=None):
        self.steps.append({"tool": str(tool), "ok": bool(ok), "error": error,
                           "result": result, "args": dict(args or {})})


async def test_fetch_failure_leaves_a_failed_step(seeded, monkeypatch):
    """取数失败必须落 failed 步：否则 run 以 ok 收尾，把「没拿到数据」记成答完了。"""

    def _boom(*a, **k):
        raise RuntimeError("could not connect to server: db.internal:5432")

    monkeypatch.setattr(nt, "persisted_periods", _boom)
    trace = _Trace()
    out = await ih.answer(seeded, "汇金最新持仓", None, _pro(),
                          SimpleNamespace(trace=trace))
    assert out and "取数失败" in out

    failed = [s for s in trace.steps if not s["ok"]]
    assert len(failed) == 1
    assert failed[0]["tool"] == "institution_holdings"
    assert failed[0]["error"] == "fetch_failed"
    # 埋点里不许出现连接串/SQL/异常正文
    payload = f"{failed[0]['args']}{failed[0]['error']}{failed[0]['result']}"
    assert "db.internal" not in payload and "could not connect" not in payload


async def test_yoy_change_compares_latest_with_last_year_same_quarter(engine, monkeypatch):
    """「去年同期」问变动 = 本期 vs 去年同季；不能变成「去年同季 vs 它的上一季」。"""
    rows = {}
    for period, num in (("2026-09-30", 100.0), ("2026-06-30", 90.0), ("2025-09-30", 60.0)):
        rows[(nt.REPORT_TABLES["float"], period)] = [
            _src("601988", "中国银行", "中央汇金投资有限责任公司", num, end=period, cap=1e8)]
    monkeypatch.setattr(nt, "_rows_once", _fake_rows(rows))
    for period in ("2025-09-30", "2026-06-30", "2026-09-30"):
        nt.scan_period(engine, period, report_kinds=["float"])

    out = await _ask(engine, "汇金去年同期新进了哪些")

    assert "2026-09-30 vs 2025-09-30" in out
    assert "同比变动（与去年同季）" in out
    assert "环比变动" not in out
    assert "只有一期数据" not in out
    # 深链必须带同比比较期：只带本期 report_date 会让页面重算成环比。
    assert "report_date=2026-09-30" in out
    assert "prev_report_date=2025-09-30" in out
    assert "view=changes" in out


async def test_non_adjacent_period_is_refused_not_mislabelled(engine, monkeypatch):
    """缺季时不能把 5 个季度前的一期当「环比」上期。"""
    rows = {}
    for period, num in (("2026-09-30", 100.0), ("2025-09-30", 80.0)):
        rows[(nt.REPORT_TABLES["float"], period)] = [
            _src("601988", "中国银行", "中央汇金投资有限责任公司", num, end=period, cap=1e8)]
    monkeypatch.setattr(nt, "_rows_once", _fake_rows(rows))
    nt.scan_period(engine, "2025-09-30", report_kinds=["float"])
    nt.scan_period(engine, "2026-09-30", report_kinds=["float"])

    out = await _ask(engine, "汇金最新持仓变动")

    assert "缺少 2026-06-30" in out and "不做跨期" in out
    assert "环比变动" not in out


async def test_prev_period_is_not_substituted_by_latest(engine, monkeypatch):
    """只有一期时问「上期」→ 明确拒绝；绝不拿最新一期顶替上期（否则口径漂移）。"""
    rows = {(nt.REPORT_TABLES["float"], PERIOD_CUR): [
        _src("601398", "工商银行", "中央汇金投资有限责任公司", 100.0, end=PERIOD_CUR, cap=1e8),
    ]}
    monkeypatch.setattr(nt, "_rows_once", _fake_rows(rows))
    nt.scan_period(engine, PERIOD_CUR, report_kinds=["float"])

    out = await _ask(engine, "汇金上期持仓")
    assert "只有一期数据" in out and "不拿最新一期顶替" in out
    assert "601198" not in out
    assert "601398" not in out          # 没有把本期数据包装成「上期」
    assert "报告期 " not in out


# ────────────────────────── 线程 / 连接类型 ──────────────────────────


async def test_queries_run_off_the_event_loop_thread(seeded, monkeypatch):
    """core 是同步阻塞 SQL：必须在 worker 线程跑，且拿到的是 Engine（线程安全）。"""
    import threading

    from sqlalchemy.engine import Engine

    seen: dict[str, object] = {}
    real_periods, real_holdings = nt.persisted_periods, nt.holdings

    def _spy_periods(engine, kind="float"):
        seen["periods_thread"] = threading.current_thread()
        return real_periods(engine, kind)

    def _spy_holdings(engine, **kw):
        seen["query_thread"] = threading.current_thread()
        seen["engine"] = engine
        return real_holdings(engine, **kw)

    monkeypatch.setattr(nt, "persisted_periods", _spy_periods)
    monkeypatch.setattr(nt, "holdings", _spy_holdings)

    out = await _ask(seeded, "汇金最新持仓")
    assert out and "262 条持仓记录" in out
    assert isinstance(seen["engine"], Engine)
    assert seen["periods_thread"] is not threading.main_thread()
    assert seen["query_thread"] is not threading.main_thread()


async def test_session_input_is_unwrapped_to_engine(seeded):
    """请求传的是 Session：只取它的 Engine 进 worker 线程，绝不复用 Session。"""
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session

    with Session(seeded) as session:
        assert ih._engine_of(session) is seeded
        assert isinstance(ih._engine_of(session), Engine)
        out = await ih.answer(session, "汇金最新持仓", None, _pro())
    assert out and "262 条持仓记录" in out


def test_engine_of_never_returns_a_session(seeded):
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import Session

    assert ih._engine_of(None) is None
    assert ih._engine_of("sqlite://") is None
    with Session(seeded) as session:
        assert ih._engine_of(session) is seeded
    # 绑在单条 Connection 上时取它的 Engine（不把连接带进别的线程）
    with seeded.connect() as conn:
        assert isinstance(conn, Connection)
        assert ih._engine_of(conn) is seeded


# ────────────────────────── 文案对齐真实排序 ──────────────────────────


async def test_sort_wording_matches_real_order(seeded):
    core = nt.holdings(seeded, kind="float", holder="huijin")
    assert core["sort"] == "market_cap" and core["desc"] is True

    out = await _ask(seeded, "汇金最新持仓")
    assert "按持股市值降序" in out

    chg = nt.changes(seeded, kind="float", holder="huijin", limit=500)
    assert chg["sort"] == "kind_then_delta_abs"
    order = {"new": 0, "increase": 1, "decrease": 2, "exit": 3}
    kinds = [i["kind"] for i in chg["items"]]
    assert kinds == sorted(kinds, key=lambda k: order[k])

    chg_out = await _ask(seeded, "汇金最新持仓变动")
    assert "先按变动类型" in chg_out and "组内按变动股数绝对值从大到小" in chg_out


# ────────────────────────── 刷新后的缓存一致性 ──────────────────────────


def test_refresh_reads_committed_revision(seeded, monkeypatch):
    """刷新写库后，持仓筛选与概览紧接着看到实际结果。"""
    from src.web.api import national_team as api_mod

    monkeypatch.setattr(api_mod, "engine", seeded)

    def _hold(holder: str) -> dict:
        return api_mod.national_team_holdings(
            report_date=None, kind="float", holder=holder, q=None,
            sort="market_cap", desc=True, limit=8, offset=0,
        )

    assert _hold("huijin")["count"] == 262
    assert _hold("ssb")["count"] == 155
    assert api_mod.national_team_overview(report_date=None, kind="float")["totals"]["stocks"] > 1

    # 新采集（整期重写：scan 是该期完整真值）—— 汇金只剩 1 只，社保全无
    monkeypatch.setattr(nt, "_rows_once", _fake_rows({
        (nt.REPORT_TABLES["float"], PERIOD_CUR): [
            _src("601398", "工商银行", "中央汇金投资有限责任公司", 1.0, end=PERIOD_CUR, cap=1e8),
        ],
    }))
    receipt = api_mod.national_team_refresh(periods=PERIOD_CUR, mode="scan", _owner=None)
    assert receipt["ok"] is True and receipt["written"] >= 1

    assert _hold("huijin")["count"] == 1
    assert _hold("ssb")["count"] == 0
    assert _hold("ssb")["status"] == "empty_result"
    assert api_mod.national_team_overview(report_date=None, kind="float")["totals"]["stocks"] == 1


def test_same_timestamp_revision_is_visible_in_holdings(seeded, monkeypatch):
    from sqlalchemy import text
    from src.web.api import national_team as api_mod

    monkeypatch.setattr(api_mod, "engine", seeded)
    params = dict(report_date=PERIOD_CUR, kind="float", holder="huijin", q="601398",
                  sort="market_cap", desc=True, limit=8, offset=0)
    before = api_mod.national_team_holdings(**params)["items"][0]["hold_num"]
    # 同批修订保持采集时点、期数及行数不变。
    with seeded.begin() as conn:
        conn.execute(text("UPDATE national_team_holdings SET hold_num=hold_num+123 "
                          "WHERE report_kind='float' AND report_date=:d AND stock_code='601398'"),
                     {"d": PERIOD_CUR})
    assert api_mod.national_team_holdings(**params)["items"][0]["hold_num"] == before + 123


async def test_prev_does_not_skip_a_missing_quarter(engine, monkeypatch):
    missing = "2026-06-30"
    rows = {(nt.REPORT_TABLES["float"], end): [
        _src("601398", "工商银行", "中央汇金投资有限责任公司", 100, end=end, cap=1e8),
    ] for end in ("2026-09-30", "2026-03-31")}
    monkeypatch.setattr(nt, "_rows_once", _fake_rows(rows))
    for _, end in rows:
        nt.scan_period(engine, end, report_kinds=["float"])
    out = await _ask(engine, "汇金上期持仓")
    assert missing in out and "无法回答" in out
    assert "601398" not in out


def test_api_error_text_is_non_sensitive(seeded, monkeypatch):
    from fastapi import HTTPException

    from src.web.api import national_team as api_mod

    def _boom(*a, **k):
        raise RuntimeError("password=sup3rsecret host=db.internal table=national_team_holdings")

    monkeypatch.setattr(api_mod, "engine", seeded)
    monkeypatch.setattr(nt, "overview", _boom)
    with pytest.raises(HTTPException) as ei:
        api_mod.national_team_overview(report_date=None, kind="float")
    detail = str(ei.value.detail)
    assert detail == api_mod._SQL_ERROR_TEXT
    for leak in ("sup3rsecret", "db.internal", "national_team_holdings", "RuntimeError"):
        assert leak not in detail


# ────────────────────────── 权限 / 身份 ──────────────────────────


async def test_missing_user_is_refused(seeded):
    out = await ih.answer(seeded, "汇金持仓", None, None)
    assert out and "未识别到登录用户" in out


async def test_no_permission_is_refused(seeded):
    guest = SimpleNamespace(id="u-guest", role="guest")
    out = await ih.answer(seeded, "汇金持仓", None, guest)
    assert out and "没有行情浏览权限" in out


async def test_non_institution_returns_none(seeded):
    assert await _ask(seeded, "600988 现价多少") is None
    assert await _ask(seeded, "今天大盘怎么样") is None


async def test_etf_gap_is_stated_not_fabricated(seeded):
    out = await _ask(seeded, "汇金买了哪些ETF")
    assert out and "不含宽基 ETF" in out
    assert "亿" not in out.split("如需个股持仓")[0]  # 没编 ETF 数字


# ────────────────────────── 与 API 同查询对拍 ──────────────────────────


async def test_cross_check_against_api_layer(seeded, monkeypatch):
    """API 与参谋直查共用同一份隔离数据库，对拍真实查询结果。"""
    from src.web.api import national_team as api_mod

    monkeypatch.setattr(api_mod, "engine", seeded)

    api_hold = api_mod.national_team_holdings(
        report_date=None, kind="float", holder="huijin", q=None,
        sort="market_cap", desc=True, limit=200, offset=0,
    )
    core_hold = nt.holdings(seeded, kind="float", holder="huijin", limit=200)
    assert api_hold["count"] == core_hold["count"] == 262
    assert [i["stock_code"] for i in api_hold["items"]] == [
        i["stock_code"] for i in core_hold["items"]
    ]

    api_chg = api_mod.national_team_changes(
        report_date=None, kind="float", holder="ssb", change="new", limit=100,
    )
    core_chg = nt.changes(seeded, kind="float", holder="ssb", change="new", limit=100)
    assert api_chg["count"] == core_chg["count"] == 150
    assert api_chg["counts"] == core_chg["counts"]

    out = await _ask(seeded, "社保基金最新新进了哪些股")
    assert f"新进 {api_chg['counts']['new']}" in out
    assert f"共 {api_chg['count']} 条" in out

    hold_out = await _ask(seeded, "汇金最新持仓")
    assert hold_out
    assert f"{api_hold['count']} 条持仓记录" in hold_out
    assert f"{api_hold['count']} 条记录" in hold_out  # 截断说明里的总数同样是过滤后总数


async def test_status_fields_are_consistent_across_layers(seeded):
    assert nt.holdings(seeded, report_date="2025-12-31", kind="float")["status"] == "unknown_period"
    assert nt.changes(seeded, report_date="2025-12-31", kind="float")["status"] == "unknown_period"
    assert nt.scope_summary(seeded, report_date="2025-12-31", kind="float")["status"] == "unknown_period"
    assert nt.changes(seeded, kind="float", holder="csf")["status"] == "empty_result"
