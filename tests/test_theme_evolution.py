"""题材演化观察层(A 通道): 三态成分、幂等、覆盖门槛、缺口与措辞门禁。

重点不是"能不能出报告", 而是**取不到数据时会不会编**: 空列表/None/整段缺失
必须落成不同状态, 覆盖不足不许出排名, 涨停池没取到不许当 0 只。
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.core import theme_evolution as te
from src.db.models import ThemeBoardDaily, ThemeLimitUpDaily
from src.db.session import Base

CST = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 16, 15, 16, tzinfo=CST)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """测试绝不联网: 默认行情/新鲜度都打桩(需要时在用例里显式注入)。"""
    monkeypatch.setattr(te, "_default_quotes", lambda codes: None)
    monkeypatch.setattr(te, "_default_fresh", lambda: True)
    monkeypatch.setattr(te, "_default_trade_date", lambda: None)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[ThemeBoardDaily.__table__, ThemeLimitUpDaily.__table__]
    )
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _boards(*codes):
    return [{"code": c, "name": f"板块{c}", "board_type": "concept"} for c in codes]


def _members(mapping):
    def _f(code):
        return mapping.get(code)

    return _f


def _f_pool(*codes):
    return lambda day: [{"code": c, "name": f"股{c}"} for c in codes]


# ── 三态 ───────────────────────────────────────────────────────────────────────

def test_none_empty_and_list_are_three_distinct_states(db):
    out = te.collect_snapshot(
        db, day="20261116", now=NOW, source_fresh=True,
        boards=_boards("A1", "A2", "A3"),
        fetch_members=_members({"A1": ["000001", "000002"], "A2": [], "A3": None}),
        fetch_pool=_f_pool("000001"),
    )
    assert out["status"] == "partial"
    assert (out[te.STATE_OK], out[te.STATE_EMPTY], out[te.STATE_FAILED]) == (1, 1, 1)

    rows = {r.board_code: r for r in db.execute(select(ThemeBoardDaily)).scalars()}
    assert rows["A1"].state == te.STATE_OK and rows["A1"].member_count == 2
    assert rows["A2"].state == te.STATE_EMPTY and rows["A2"].member_count == 0
    assert rows["A3"].state == te.STATE_FAILED and rows["A3"].member_count is None
    # 失败行不得写成空集合
    assert rows["A3"].members_json is None
    assert json.loads(rows["A2"].members_json) == []


def test_empty_board_catalog_is_refused_and_writes_nothing(db):
    out = te.collect_snapshot(db, day="20261116", now=NOW, source_fresh=True,
                              boards=[], fetch_members=lambda c: ["000001"],
                              fetch_pool=_f_pool())
    assert out["status"] == "refused" and out["reason"] == "board_catalog_empty"
    assert db.execute(select(ThemeBoardDaily)).first() is None
    assert db.execute(select(ThemeLimitUpDaily)).first() is None


def test_stale_source_refuses_snapshot(db):
    out = te.collect_snapshot(db, day="20261116", now=NOW, source_fresh=False,
                              boards=_boards("A1"), fetch_members=lambda c: ["000001"],
                              fetch_pool=_f_pool("000001"))
    assert out["status"] == "refused" and out["reason"] == "source_stale"
    assert db.execute(select(ThemeBoardDaily)).first() is None


def test_exception_on_one_board_does_not_break_the_round(db):
    def boom(code):
        if code == "A2":
            raise RuntimeError("tq timeout")
        return ["000001"]

    out = te.collect_snapshot(db, day="20261116", now=NOW, source_fresh=True,
                              boards=_boards("A1", "A2"), fetch_members=boom,
                              fetch_pool=_f_pool("000001"))
    assert out[te.STATE_OK] == 1 and out[te.STATE_FAILED] == 1
    row = db.execute(select(ThemeBoardDaily).filter_by(board_code="A2")).scalar_one()
    assert row.state == te.STATE_FAILED and "RuntimeError" in (row.error or "")


def test_budget_exhaustion_marks_remaining_boards_failed(db):
    out = te.collect_snapshot(db, day="20261116", now=NOW, source_fresh=True,
                              boards=_boards("A1", "A2", "A3"),
                              fetch_members=lambda c: ["000001"],
                              fetch_pool=_f_pool("000001"), budget_s=0)
    assert out[te.STATE_FAILED] == 3
    rows = list(db.execute(select(ThemeBoardDaily)).scalars())
    assert all(r.error == "budget_exhausted" for r in rows)


# ── 幂等 ───────────────────────────────────────────────────────────────────────

def test_same_day_rerun_replaces_rows_without_duplicates(db):
    te.collect_snapshot(db, day="20261116", now=NOW, source_fresh=True,
                        boards=_boards("A1"), fetch_members=lambda c: ["000001", "000002"],
                        fetch_pool=_f_pool("000001"))
    te.collect_snapshot(db, day="20261116", now=NOW, source_fresh=True,
                        boards=_boards("A1"), fetch_members=lambda c: ["000002"],
                        fetch_pool=_f_pool("000001"))
    rows = list(db.execute(select(ThemeBoardDaily)).scalars())
    assert len(rows) == 1
    assert json.loads(rows[0].members_json) == ["000002"]
    assert len(list(db.execute(select(ThemeLimitUpDaily)).scalars())) == 1


# ── 报告口径 ───────────────────────────────────────────────────────────────────

def _seed(db, day, mapping, pool_codes):
    te.collect_snapshot(db, day=day, now=NOW, source_fresh=True,
                        boards=_boards(*mapping), fetch_members=_members(mapping),
                        fetch_pool=_f_pool(*pool_codes))


def test_ranking_uses_pool_intersection_and_is_sorted(db):
    _seed(db, "20261116",
          {"A1": ["000001", "000002", "000003"], "A2": ["000004"]},
          ["000001", "000002", "000004"])
    payload = te.build_payload(db, day="20261116")
    assert payload["ranking_available"] is True
    assert [b["board_code"] for b in payload["ranking"]] == ["A1", "A2"]
    assert payload["ranking"][0]["limit_up_hits"] == 2
    assert payload["ranking"][0]["hit_ratio"] == pytest.approx(2 / 3, abs=1e-4)


def test_empty_pool_means_unknown_not_zero(db):
    _seed(db, "20261116", {"A1": ["000001"]}, [])
    payload = te.build_payload(db, day="20261116")
    assert payload["limit_up"]["available"] is False
    assert payload["ranking_available"] is False
    assert "unknown" in (payload["ranking_reason"] or "")
    assert payload["ranking"] == []
    # 不写 0 只涨停的假事实
    assert db.execute(select(ThemeLimitUpDaily)).first() is None


def test_low_coverage_blocks_ranking(db):
    _seed(db, "20261116", {"A1": ["000001"], "A2": None, "A3": None}, ["000001"])
    payload = te.build_payload(db, day="20261116")
    assert payload["coverage"] < te.MIN_COVERAGE_FOR_RANKING
    assert payload["ranking_available"] is False
    assert "coverage" in (payload["ranking_reason"] or "")


def test_changes_need_previous_snapshot_and_report_unknown_first_day(db):
    _seed(db, "20261116", {"A1": ["000001", "000002"]}, ["000001"])
    first = te.build_payload(db, day="20261116")
    assert first["changes_available"] is False
    assert first["prev_day"] is None

    _seed(db, "20261117", {"A1": ["000002", "000009"]}, ["000002"])
    second = te.build_payload(db, day="20261117")
    assert second["prev_day"] == "20261116"
    assert second["changes_available"] is True
    chg = second["changes"][0]
    assert chg["added_vs_prev"] == ["000009"]
    assert chg["removed_vs_prev"] == ["000001"]

    text = te.render_markdown(first)
    assert "unknown" in text  # 首日必须显式说无法判断新增/剔除


def test_rendered_report_has_no_trading_wording(db):
    _seed(db, "20261116", {"A1": ["000001"]}, ["000001"])
    text = te.render_markdown(te.build_payload(db, day="20261116"))
    assert te.FORBIDDEN_WORDS
    for word in te.FORBIDDEN_WORDS:
        assert word not in text
    assert "不构成投资建议" in text
    assert te.assert_no_trading_wording(text) is None


def test_wording_gate_rejects_directional_text():
    with pytest.raises(ValueError):
        te.assert_no_trading_wording("持仓可买入，目标价 10 元")


def test_report_declares_coverage_and_missing_list(db):
    _seed(db, "20261116", {"A1": ["000001"], "A2": None}, ["000001"])
    payload = te.build_payload(db, day="20261116")
    text = te.render_markdown(payload)
    assert "齐全率" in text
    assert "A2" in text
    assert len(payload["missing"]) == 1


# ── 接线 ───────────────────────────────────────────────────────────────────────

def test_run_daily_collects_and_writes_shared_report(db, monkeypatch):
    captured = {}

    def fake_write(job_id, title, content, **kwargs):
        captured.update({"job_id": job_id, "title": title, "content": content, **kwargs})
        return {"path": "/tmp/x.md", "title": title, "size": len(content)}

    import src.core.report_generator as rg

    monkeypatch.setattr(rg, "write_report_document", fake_write)
    out = te.run_daily(db, day="20261116", now=NOW, source_fresh=True,
                       boards=_boards("A1"), fetch_members=lambda c: ["000001"],
                       fetch_pool=_f_pool("000001"))
    assert captured["job_id"] == te.JOB_ID
    assert captured["scope"] == "shared"
    assert captured["contains_private_holdings"] is False
    assert out["report"]["path"] == "/tmp/x.md"
    assert "题材演化观察" in captured["title"]


def test_register_cron_respects_switch_and_missing_scheduler(monkeypatch):
    class FakeScheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, func, trigger, **kwargs):
            self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    monkeypatch.delenv(te.ENV_ENABLED, raising=False)
    sched = FakeScheduler()
    assert te.register_cron(sched) is True
    job = sched.jobs[0]
    assert job["id"] == "theme_evolution_daily"
    assert (job["hour"], job["minute"]) == (15, 16)
    assert job["day_of_week"] == "mon-fri"

    monkeypatch.setenv(te.ENV_ENABLED, "0")
    assert te.register_cron(FakeScheduler()) is False
    assert te.register_cron(None) is False


def test_migration_251_creates_tables_idempotently():
    """迁移可重跑(启动路径上跑错就整站起不来): 两次调用不报错, 表/索引都在。"""
    from sqlalchemy import create_engine, inspect

    from src.web.migrations import _m251_theme_evolution

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        _m251_theme_evolution(conn)
        _m251_theme_evolution(conn)
    insp = inspect(engine)
    names = set(insp.get_table_names())
    assert {"theme_board_daily", "theme_limitup_daily"} <= names
    idx = {i["name"] for i in insp.get_indexes("theme_board_daily")}
    assert "uq_theme_board_day_code" in idx
    engine.dispose()


# ── 板块类型: 无行情的"选股条件板块"不参与排名 ──────────────────────────────

def test_condition_screen_board_is_excluded_from_ranking(db):
    quotes = {"A1": {"price": 100.0}, "A2": {"price": None}}
    te.collect_snapshot(db, day="20261116", now=NOW, source_fresh=True,
                        boards=_boards("A1", "A2"),
                        fetch_members=_members({"A1": ["000001"], "A2": ["000002"]}),
                        fetch_pool=_f_pool("000001", "000002"),
                        fetch_quotes=lambda codes: quotes)
    payload = te.build_payload(db, day="20261116")
    assert [b["board_code"] for b in payload["ranking"]] == ["A1"]
    assert payload["kind_counts"]["condition_screen"] == 1
    assert [b["board_code"] for b in payload["screen_rules"]] == ["A2"]
    text = te.render_markdown(payload)
    assert "条件/技术" in text and "不参与排名" in text


def test_unknown_quote_state_is_kept_but_labelled(db):
    te.collect_snapshot(db, day="20261116", now=NOW, source_fresh=True,
                        boards=_boards("A1"), fetch_members=_members({"A1": ["000001"]}),
                        fetch_pool=_f_pool("000001"), fetch_quotes=lambda codes: None)
    payload = te.build_payload(db, day="20261116")
    assert payload["kind_counts"]["unknown"] == 1
    assert [b["board_code"] for b in payload["ranking"]] == ["A1"]  # 未证实就不排除
    assert "类型未经证实" in te.render_markdown(payload)


def test_source_trade_date_behind_target_day_is_refused(db):
    """客户端最新日线还停在 11-12 时, 不能拿它充当 11-16 的快照(数据日错配)。"""
    out = te.collect_snapshot(
        db, day="20261116", now=NOW, source_fresh=True, source_trade_date="20261112",
        boards=_boards("A1"), fetch_members=lambda c: ["000001"], fetch_pool=_f_pool("000001"),
    )
    assert out["status"] == "refused" and out["reason"] == "source_not_updated_for_day"
    assert db.execute(select(ThemeBoardDaily)).first() is None


def test_refused_collection_is_visible_in_report(db):
    """采集被拒(源未更新)时, 报告必须写出原因, 而不是安静地出一份空表。"""
    out = te.collect_snapshot(
        db, day="20261116", now=NOW, source_fresh=True, source_trade_date="20261112",
        boards=_boards("A1"), fetch_members=lambda c: ["000001"], fetch_pool=_f_pool(),
    )
    payload = te.build_payload(db, day="20261116", collect=out)
    text = te.render_markdown(payload)
    assert "本次采集被拒绝" in text
    assert "source_not_updated_for_day" in text


def test_migration_adds_named_index_even_when_create_all_ran_first():
    """启动路径上 create_all 可能先建表(带 UNIQUE 约束), 迁移仍须补出命名索引。"""
    from sqlalchemy import create_engine, inspect

    from src.web.migrations import _m251_theme_evolution

    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[ThemeBoardDaily.__table__, ThemeLimitUpDaily.__table__]
    )
    with engine.begin() as conn:
        _m251_theme_evolution(conn)
    insp = inspect(engine)
    board_idx = {i["name"] for i in insp.get_indexes("theme_board_daily")}
    limit_idx = {i["name"] for i in insp.get_indexes("theme_limitup_daily")}
    assert "uq_theme_board_day_code" in board_idx
    assert "uq_theme_limitup_day_code" in limit_idx
    engine.dispose()
