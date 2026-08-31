"""thsdk 板块数据采集/轮动/API 单元测试(阶段2.1/2.2, v0.3.0)。

覆盖:
- fetch 转换(DataFrame → list[dict], block_code 归一化)
- TTL 缓存命中(fetch 只调一次覆盖层)
- 失败容错(thsdk 失败返回 None + 不抛)
- compute_rotation 排序逻辑(5 板块, 强度分降序)
- sync_boards_to_db 幂等写入
- /api/boards 路由挂载
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.core.thsdk_board as tb
import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _fake_client(**methods) -> MagicMock:
    """构造 fake thsdk 客户端, 每个 thsdk 方法返回指定 DataFrame/dict。"""
    client = MagicMock()
    for name, ret in methods.items():
        getattr(client, name).return_value = ret
    return client


@pytest.fixture(autouse=True)
def _clear_board_cache():
    tb.clear_cache()
    yield
    tb.clear_cache()


# ---------------------------------------------------------------------------
# 1) fetch 转换 + block_code 归一化
# ---------------------------------------------------------------------------
def test_fetch_ths_industry_converts_df():
    """行业列表 DataFrame → list[dict], 并补 URFI 前缀。"""
    df = pd.DataFrame(
        {
            "代码": ["883404", "883402"],
            "名称": ["半导体", "白酒"],
            "涨跌幅": [2.3, -1.1],
        }
    )
    tb._client = lambda: _fake_client(get_ths_industry=df)

    rows = tb.fetch_ths_industry()
    assert rows is not None
    assert len(rows) == 2
    assert rows[0]["block_code"] == "URFI883404"
    assert rows[0]["name"] == "半导体"
    assert rows[0]["涨跌幅"] == 2.3
    # 已是 URFI 前缀则不加
    assert rows[1]["block_code"] == "URFI883402"


def test_fetch_ths_concept_normalizes_existing_prefix():
    """概念列表带 URFI 前缀不重复拼接。"""
    df = pd.DataFrame({"代码": ["URFI883404"], "名称": ["芯片"]})
    tb._client = lambda: _fake_client(get_ths_concept=df)

    rows = tb.fetch_ths_concept()
    assert rows[0]["block_code"] == "URFI883404"


def test_fetch_block_detail_returns_sanitized_dict():
    """板块详情 dict 经 NaN 清洗后返回。"""
    tb._client = lambda: _fake_client(
        get_block_market={"代码": "URFI883404", "涨跌幅": 3.2, "净流入": float("nan")}
    )
    detail = tb.fetch_block_detail("URFI883404")
    assert detail["涨跌幅"] == 3.2
    assert detail["净流入"] is None  # NaN → None


def test_fetch_block_constituents_converts_df():
    """板块成分股 DataFrame → list[dict]。"""
    df = pd.DataFrame({"代码": ["USZA002361"], "名称": ["神剑股份"]})
    tb._client = lambda: _fake_client(get_block_constituents=df)

    rows = tb.fetch_block_constituents("URFI883404")
    assert rows == [{"代码": "USZA002361", "名称": "神剑股份"}]


# ---------------------------------------------------------------------------
# 2) 缓存命中
# ---------------------------------------------------------------------------
def test_cache_hit_avoids_repeat_call():
    """第二次 fetch 走缓存, 底层 thsdk 只调一次。"""
    df = pd.DataFrame({"代码": ["883404"], "名称": ["半导体"]})
    client = _fake_client(get_ths_industry=df)
    tb._client = lambda: client

    tb.fetch_ths_industry()
    tb.fetch_ths_industry()
    assert client.get_ths_industry.call_count == 1


def test_cache_ttl_differs_by_endpoint():
    """列表 30 分钟 / 详情 1 小时缓存常量。"""
    assert tb._CACHE_TTL_INDUSTRY == 30 * 60
    assert tb._CACHE_TTL_DETAIL == 60 * 60


# ---------------------------------------------------------------------------
# 3) 失败容错
# ---------------------------------------------------------------------------
def test_fetch_industry_failure_returns_none(monkeypatch):
    """thsdk 抛异常 → 返回 None, 不向上抛。"""
    monkeypatch.setattr(tb, "_client", lambda: _raise_client())
    assert tb.fetch_ths_industry() is None
    assert tb.fetch_ths_concept() is None


def _raise_client():
    def _boom(*a, **k):
        raise RuntimeError("thsdk 连接失败")

    return MagicMock(get_ths_industry=_boom, get_ths_concept=_boom)


def test_fetch_block_detail_empty_code_returns_none():
    """空 block_code 直接返回 None, 不调 thsdk。"""
    assert tb.fetch_block_detail("") is None
    assert tb.fetch_block_constituents(None) is None


def test_fetch_constituents_failure_returns_none(monkeypatch):
    """成分股失败 → None。"""
    monkeypatch.setattr(
        tb, "_client", lambda: MagicMock(get_block_constituents=MagicMock(side_effect=RuntimeError("x")))
    )
    assert tb.fetch_block_constituents("URFI883404") is None


# ---------------------------------------------------------------------------
# 4) compute_rotation 排序逻辑
# ---------------------------------------------------------------------------
def _seed_rotation_data(db, spec):
    """把 spec 写入 DB: {block_code: [(name, board_type, [(date_offset, change_pct, fund_net)]), ...]}"""
    from datetime import date, timedelta

    from src.web.models import Board, BoardDaily

    today = date.today()
    for code, (name, btype, days_rows) in spec.items():
        db.add(Board(block_code=code, name=name, board_type=btype))
        for offset, change, fund in days_rows:
            db.add(
                BoardDaily(
                    block_code=code,
                    date=today - timedelta(days=offset),
                    change_pct=change,
                    fund_net=fund,
                    volume=1000.0,
                )
            )
    db.commit()


def test_compute_rotation_sorts_by_strength():
    """5 板块: 强动量正资金排前, 负动量排后; 分数 0-100, 降序。"""
    db = _mem_db()
    try:
        _seed_rotation_data(
            db,
            {
                # code: (name, type, [(offset, change_pct, fund_net)])
                "URFI1": ("强势板块", "industry", [(0, 10.0, 1000.0), (1, 10.0, 500.0), (2, 10.0, 200.0)]),
                "URFI2": ("温和上涨", "concept", [(0, 3.0, 300.0), (1, 2.0, 100.0)]),
                "URFI3": ("连涨股", "concept", [(0, 2.0, 200.0), (1, 2.0, 100.0), (2, 2.0, 50.0)]),
                "URFI4": ("平盘", "industry", [(0, 0.5, 0.0), (1, 0.0, 0.0)]),
                "URFI5": ("弱势板块", "concept", [(0, -10.0, -500.0), (1, -10.0, -300.0)]),
            },
        )

        results = tb.compute_rotation(days=5, db=db)

        assert len(results) == 5
        # 所有分数在 0-100
        for r in results:
            assert 0 <= r["rotation_score"] <= 100
        # 降序
        scores = [r["rotation_score"] for r in results]
        assert scores == sorted(scores, reverse=True)
        # 强者第一, 弱者最后
        assert results[0]["block_code"] == "URFI1"
        assert results[-1]["block_code"] == "URFI5"
        # 字段齐全
        r0 = results[0]
        assert {"block_code", "name", "rotation_score", "change_5d", "fund_net", "consecutive_days"} <= set(r0)
        # 强势板块连续 3 天上涨
        assert results[0]["consecutive_days"] >= 3
        # 5 日累计涨幅: (1.1^3 - 1)*100 ≈ 33.1
        assert abs(results[0]["change_5d"] - 33.1) < 1.0
    finally:
        db.close()


def test_compute_rotation_empty_db():
    """无数据 → 返回空列表。"""
    db = _mem_db()
    try:
        assert tb.compute_rotation(days=5, db=db) == []
    finally:
        db.close()


def test_compute_rotation_days_clamp():
    """days 下限 1。"""
    db = _mem_db()
    try:
        _seed_rotation_data(
            db,
            {"URFI1": ("A", "industry", [(0, 1.0, 1.0)]), "URFI2": ("B", "concept", [(0, 2.0, 2.0)])},
        )
        res = tb.compute_rotation(days=0, db=db)
        assert len(res) == 2
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5) sync_boards_to_db 幂等写入
# ---------------------------------------------------------------------------
def test_sync_boards_to_db_writes_and_idempotent(monkeypatch):
    """拉列表 + 写日线; 二次同步不产生重复 (block_code, date)。"""
    db = _mem_db()

    monkeypatch.setattr(
        tb,
        "fetch_ths_industry",
        lambda: [{"block_code": "URFI883404", "name": "半导体", "涨跌幅": 3.0}],
    )
    monkeypatch.setattr(
        tb,
        "fetch_ths_concept",
        lambda: [{"block_code": "URFI883405", "name": "白酒", "涨跌幅": 1.0}],
    )
    monkeypatch.setattr(
        tb,
        "fetch_block_detail",
        lambda code: {"涨跌幅": 2.5, "主力净流入": 100.0, "成交额": 5000.0},
    )

    try:
        s1 = tb.sync_boards_to_db(db=db)
        assert s1["boards_upserted"] == 2
        assert s1["daily_rows"] == 2
        assert s1["industry_failed"] is False and s1["concept_failed"] is False

        from src.web.models import Board, BoardDaily

        assert db.query(Board).count() == 2
        b = db.query(Board).filter(Board.block_code == "URFI883404").first()
        assert b.board_type == "industry"
        daily_rows = db.query(BoardDaily).count()
        assert daily_rows == 2

        # 幂等: 再跑一遍, 板块不重复新增, 日线更新不重复插入
        s2 = tb.sync_boards_to_db(db=db)
        assert s2["boards_upserted"] == 0
        assert s2["daily_rows"] == 2
        assert db.query(Board).count() == 2
        assert db.query(BoardDaily).count() == 2
    finally:
        db.close()


def test_sync_boards_both_fail_skips(monkeypatch):
    """列表都拉取失败 → 跳过入库, 返回 failed 标记。"""
    db = _mem_db()
    monkeypatch.setattr(tb, "fetch_ths_industry", lambda: None)
    monkeypatch.setattr(tb, "fetch_ths_concept", lambda: None)
    try:
        s = tb.sync_boards_to_db(db=db)
        assert s["industry_failed"] is True and s["concept_failed"] is True
        assert s["daily_rows"] == 0
    finally:
        db.close()


def test_register_board_sync_job_reuses_scheduler():
    """注册到已有 AsyncIOScheduler(不新建), 工作日 08:30, 同 id 幂等。"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BackgroundScheduler()
    sched.start()  # 匹配生产: 调度器已启动后 replace_existing 才生效
    tb.register_board_sync_job(sched)
    job = sched.get_job(tb.BOARD_SYNC_JOB_ID)
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    # 触发的 cron 字段覆盖 时/分/day_of_week(由 register 的 hour=8, minute=30, mon-fri 决定)
    field_names = {f.name for f in job.trigger.fields}
    assert {"hour", "minute", "day_of_week"} <= field_names
    # 幂等: 再次注册(同 id + replace_existing)不产生重复 job
    tb.register_board_sync_job(sched)
    assert len(sched.get_jobs()) == 1
    sched.shutdown(wait=False)


# ---------------------------------------------------------------------------
# 6) /api/boards 路由挂载
# ---------------------------------------------------------------------------
def test_boards_router_mounted():
    """/api/boards 系列路由已挂载到 app。"""
    from src.web.app import app

    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/boards" in paths
    assert "/api/boards/rotation" in paths
    assert "/api/boards/{block_code}" in paths
    assert "/api/boards/{block_code}/constituents" in paths