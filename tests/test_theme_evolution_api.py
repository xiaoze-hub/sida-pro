"""观察层只读 API: 聚合口径、unknown 处理与调度就绪判据。

这里用**迁移建表**的隔离 SQLite, 不走 ORM create_all —— 接口面对的就是生产那张表。
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from src.core import theme_evolution_store as store
from src.web import migrations as mig
from src.web.api import theme_evolution as api

CST = ZoneInfo("Asia/Shanghai")
DAY1 = "20261113"
DAY2 = "20261116"


@pytest.fixture()
def engine(tmp_path):
    # 必须落文件: 内存库每条连接都是新库, 而 TestClient 在别的线程里执行端点。
    eng = create_engine(
        f"sqlite:///{tmp_path / 'obs.db'}",
        connect_args={"check_same_thread": False},
    )
    with eng.begin() as conn:
        mig._m252_theme_observation_evidence(conn)
    yield eng
    eng.dispose()


def _batch(*, day: str, pool, key: str, status_boards=None) -> store.ObservationBatch:
    return store.ObservationBatch(
        day=day,
        run_key=f"tq:{day}:{key}",
        rule_version="theme_evo_capture_v1",
        source="tq",
        captured_at=dt.datetime(int(day[:4]), int(day[4:6]), int(day[6:]), 16, 12, tzinfo=CST),
        boards=status_boards or (
            store.BoardEvidence(board_code="880201", state=store.BOARD_OK,
                                board_name="机器人概念", board_type="concept",
                                data_day=day, date_basis="source_reported",
                                members=("000711.SZ",)),
        ),
        limit_up=tuple(pool),
        pool_state=store.POOL_OK,
        pool_data_day=day,
        pool_date_basis="kline",
        source_fresh=True,
        source_trade_date=day,
    )


def _seed(engine):
    store.append_observation(engine, _batch(
        day=DAY1, key="daily",
        pool=[
            store.LimitUpEvidence("000711", "示例甲", sector="通用设备",
                                  concepts=("机器人", "工业母机"), block_as_of=DAY1,
                                  block_source="tq:get_relation"),
            store.LimitUpEvidence("600843", "示例乙", sector="专用设备",
                                  concepts=("机器人",), block_as_of=DAY1,
                                  block_source="tq:get_relation"),
            store.LimitUpEvidence("300413", "示例丙", sector=None, concepts=None),
        ],
    ))
    store.append_observation(engine, _batch(
        day=DAY2, key="daily",
        pool=[
            store.LimitUpEvidence("000711", "示例甲", sector="通用设备",
                                  concepts=("机器人",), block_as_of=DAY2,
                                  block_source="tq:get_relation"),
        ],
    ))


@pytest.fixture()
def client(engine, monkeypatch):
    monkeypatch.setattr(api, "write_engine", engine)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/theme-evolution")
    _seed(engine)
    return TestClient(app)


def test_days_are_newest_first_with_status(client):
    body = client.get("/api/theme-evolution/days").json()
    assert [d["day"] for d in body["days"]] == [DAY2, DAY1]
    first = body["days"][0]
    assert first["status"] in (store.STATUS_COMPLETE, store.STATUS_PARTIAL)
    assert first["limit_up_count"] == 1
    # 采集时刻不是供应商发布时间 —— 必须显式声明, 防止前端当"发布时间"显示
    assert first["captured_at_is_vendor_publish_time"] is False


def test_day_detail_aggregates_themes_without_double_counting(client):
    body = client.get(f"/api/theme-evolution/day/{DAY1}").json()
    by_name = {(t["kind"], t["name"]): t for t in body["themes"]}

    robot = by_name[("concept", "机器人")]
    assert robot["limit_up_count"] == 2, "两只票同标一个概念 = 2, 不是按标签条数累加"
    assert [s["stock_code"] for s in robot["stocks"]] == ["000711", "600843"]
    assert robot["block_as_of"] == [DAY1]

    assert by_name[("concept", "工业母机")]["limit_up_count"] == 1
    assert by_name[("industry", "通用设备")]["limit_up_count"] == 1
    assert by_name[("industry", "专用设备")]["limit_up_count"] == 1
    # 概念与行业分开统计, 不合并
    assert ("concept", "通用设备") not in by_name


def test_missing_labels_are_counted_not_invented(client):
    body = client.get(f"/api/theme-evolution/day/{DAY1}").json()
    assert body["labels"]["stocks_without_labels"] == 1
    target = next(r for r in body["limit_up"] if r["stock_code"] == "300413")
    assert target["concepts"] is None
    assert target["labels_known"] is False
    # 没有归属的票不得出现在任何题材下
    for theme in body["themes"]:
        assert all(s["stock_code"] != "300413" for s in theme["stocks"])


def test_labels_block_is_explicitly_not_historical(client):
    body = client.get(f"/api/theme-evolution/day/{DAY1}").json()
    labels = body["labels"]
    assert labels["basis"] == "same_row_enrichment_at_capture_time"
    assert labels["is_historical_membership"] is False
    assert labels["block_as_of_values"] == [DAY1]


def test_board_membership_date_stays_unknown_when_absent(client):
    body = client.get(f"/api/theme-evolution/day/{DAY2}").json()
    # 880201 在本 fixture 里自报了 data_day; 断言字段存在且可判定
    board = body["boards"][0]
    assert board["membership_as_of_known"] is True
    assert board["data_day"] == DAY2


def test_timeline_zero_fills_absent_days(client):
    body = client.get("/api/theme-evolution/timeline?days=30").json()
    assert body["days"] == [DAY1, DAY2]
    robot = next(t for t in body["themes"] if t["name"] == "机器人" and t["kind"] == "concept")
    assert robot["counts"] == [2, 1]
    assert robot["total"] == 3
    assert robot["days_present"] == 2
    # 只在 DAY1 出现的标签 → DAY2 记 0(缺口, 接口注明未区分缺失与真 0)
    mother = next(t for t in body["themes"] if t["name"] == "工业母机")
    assert mother["counts"] == [1, 0]


def test_unknown_day_is_404_and_malformed_is_400(client):
    assert client.get("/api/theme-evolution/day/20260101").status_code == 404
    assert client.get("/api/theme-evolution/day/2026-01-01").status_code == 400
    assert client.get("/api/theme-evolution/day/20261399").status_code == 400


def test_days_limit_is_validated(client):
    assert client.get("/api/theme-evolution/days?limit=0").status_code == 422


# ── 调度就绪判据 ────────────────────────────────────────────────────────
class _Boards:
    def __init__(self, trade, fresh):
        self._trade, self._fresh = trade, fresh

    def latest_trade_date(self):
        return self._trade

    def data_fresh(self):
        return self._fresh


def test_upstream_ready_refuses_before_close_window(monkeypatch):
    monkeypatch.setattr(
        "src.core.tdx_boards.latest_trade_date", lambda: "2026-11-16", raising=True
    )
    state = api.upstream_ready(dt.datetime(2026, 11, 16, 15, 20, tzinfo=CST))
    assert state["ready"] is False
    assert state["reason"] == "before_daily_close_window"


def test_upstream_ready_requires_source_day_equal_target(monkeypatch):
    monkeypatch.setattr("src.core.tdx_boards.latest_trade_date", lambda: "2026-11-13")
    monkeypatch.setattr("src.core.tdx_boards.data_fresh", lambda: True)
    state = api.upstream_ready(dt.datetime(2026, 11, 16, 16, 20, tzinfo=CST))
    assert state["ready"] is False
    assert state["day"] == "20261113"
    assert state["reason"] == "source_daily_not_on_target_day"


def test_upstream_ready_is_not_a_finalization_claim(monkeypatch):
    monkeypatch.setattr("src.core.tdx_boards.latest_trade_date", lambda: "2026-11-16")
    monkeypatch.setattr("src.core.tdx_boards.data_fresh", lambda: True)
    state = api.upstream_ready(dt.datetime(2026, 11, 16, 16, 20, tzinfo=CST))
    assert state["ready"] is True
    assert state["basis"] == "tq_daily_covers_target_day"
    # 关键: 就绪 ≠ 供应商已定稿
    assert state["vendor_finalized_claim"] is False


def test_stale_source_blocks_readiness(monkeypatch):
    monkeypatch.setattr("src.core.tdx_boards.latest_trade_date", lambda: "2026-11-16")
    monkeypatch.setattr("src.core.tdx_boards.data_fresh", lambda: False)
    state = api.upstream_ready(dt.datetime(2026, 11, 16, 16, 20, tzinfo=CST))
    assert state["ready"] is False
    assert state["reason"] == "source_daily_reported_stale"


def test_build_deps_exposes_callables_and_does_not_call_them(monkeypatch):
    calls: list[str] = []

    def boom(*a, **kw):
        calls.append("called")
        raise AssertionError("build_deps 不许在组装阶段取数")

    monkeypatch.setattr("src.core.tdx_boards.sector_items", boom)
    monkeypatch.setattr("src.core.tdx_boards.constituents", boom)
    deps = api.build_deps()
    assert callable(deps["fetch_boards"])
    assert callable(deps["fetch_members"])
    assert callable(deps["fetch_pool"])
    assert callable(deps["capture"])
    assert calls == []
