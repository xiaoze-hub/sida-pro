# -*- coding: utf-8 -*-
"""四档资金结构消费 API 测试（2026-10-24, exday_daily 消费 MVP）。

不打 TQ 网关：只读 `exday_daily` 表（临时 SQLite），钉**消费端契约**——
口径标签必须带、缺日期不补 0、失败显式降级（吞异常前 safe_rollback）、
缓存只存成功结果。落库管线见 test_exday_collector.py，矩阵解析见 test_exday_structure.py。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import src.db.models  # noqa: F401 - 确保 ExdayDaily 已注册到 Base.metadata
from src.core.exday_structure import query_series
from src.db.models import Base, ExdayDaily
from src.web.api import fund_structure as api
from src.web.cache.biz_cache import BizCache


@pytest.fixture()
def session_factory(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/fund_structure.db")
    Base.metadata.create_all(bind=eng)
    yield sessionmaker(bind=eng)
    eng.dispose()


@pytest.fixture()
def db(session_factory):
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


def _day(day: str, symbol: str = "002361", **kw) -> ExdayDaily:
    """一行最小合法落库（金额=元、量=手；缺值 None 不补 0）。"""
    base = {
        "trade_date": day, "symbol": symbol, "market": "CN",
        "amount": 5.48e8, "volume_lots": 640592.0,
        "active_buy_amt": 2.76e8, "active_sell_amt": 2.72e8, "active_ratio": 50.4,
        "xl_buy_amt": 1.3e7, "lg_buy_amt": 5.9e7, "md_buy_amt": 1.6e8, "sm_buy_amt": 3.1e8,
        "xl_sell_amt": 1.6e7, "lg_sell_amt": 6.7e7, "md_sell_amt": 1.8e8, "sm_sell_amt": 2.8e8,
        "retail_ratio": 56.9, "main_active_net_pct": 0.96,
        "quality": "ok", "source": "tdx_tq_exday",
    }
    base.update(kw)
    return ExdayDaily(**base)


# ────────────────────────── 路由挂载 ──────────────────────────

def test_routes_mounted_at_api_market_prefix():
    from src.web.app import app

    paths = app.openapi()["paths"]
    assert "/api/market/fund-structure/{symbol}" in paths


# ────────────────────────── core: query_series ──────────────────────────

def test_series_carries_caliber_and_direction_semantics(db):
    """@spec EXDAY-001 响应必须带 tdx_exday4 口径标签 + 方向语义（禁用于主力意图判定）"""
    db.add(_day("20261023"))
    db.commit()
    res = query_series(db, "002361")
    assert res["available"] is True
    assert res["caliber"] == "tdx_exday4"
    assert "不可换算" in res["direction_semantics"]
    assert "禁止用于主力意图判定" in res["direction_semantics"]
    assert res["tier_thresholds_hint"]  # 档位阈值提示(推断)必须随行带出


def test_series_returns_rows_only_no_padding(db):
    """@spec EXDAY-002 只返实际落库行, 缺日期不补 0/不补平交易日轴"""
    db.add(_day("20261023"))
    db.add(_day("20261027"))  # 24/25/26 周末+未落库日, 库里没有就是没有
    db.commit()
    res = query_series(db, "002361")
    assert res["available"] is True
    assert res["count"] == 2
    assert [i["trade_date"] for i in res["items"]] == ["20261023", "20261027"]
    assert all(i["xl_buy_amt"] == 1.3e7 for i in res["items"])  # 字段按表原样出


def test_series_symbol_without_data_is_available_with_zero_rows(db):
    """@spec EXDAY-002 该股没落库 ≠ 表没数据: available=True + count=0(不是读库失败)"""
    db.add(_day("20261023", symbol="600519"))
    db.commit()
    res = query_series(db, "002361")
    assert res["available"] is True
    assert res["count"] == 0
    assert res["items"] == []


def test_series_data_since_is_table_origin(db):
    """@spec EXDAY-003 data_since = 全表累积起点(与个股无关), count = 该股实际行数"""
    db.add(_day("20261023"))
    db.add(_day("20261027"))
    db.add(_day("20261023", symbol="600519"))  # 全表最早日是别的票落的
    db.commit()
    res = query_series(db, "600519")
    assert res["data_since"] == "20261023"
    assert res["count"] == 1
    assert res["items"][0]["symbol"] == "600519"


def test_series_range_and_limit(db):
    """start/end 闭区间过滤; limit 截**最近** N 行再升序返回"""
    for d in ("20261023", "20261024", "20261027", "20261028", "20261029"):
        db.add(_day(d))
    db.commit()
    res = query_series(db, "002361", start="2026-10-24", end="20261028")
    assert [i["trade_date"] for i in res["items"]] == ["20261024", "20261027", "20261028"]

    res2 = query_series(db, "002361", limit=2)
    assert [i["trade_date"] for i in res2["items"]] == ["20261028", "20261029"]
    assert res2["limit"] == 2


def test_series_missing_table_degrades_with_safe_rollback(monkeypatch, db):
    """@spec EXDAY-004 缺表(迁移没跑)显式降级; 吞异常前必须 safe_rollback(db)"""
    rolled: list = []
    monkeypatch.setattr("src.core.db_safe.safe_rollback", lambda d: rolled.append(d))
    db.execute(text("DROP TABLE exday_daily"))
    db.commit()
    res = query_series(db, "002361")
    assert res["available"] is False
    assert "exday_daily 读取失败" in res["reason"]
    assert rolled == [db]


def test_series_db_none_degrades(db):
    """@spec EXDAY-004 db 为 None 显式无数据(不猜不炸)"""
    res = query_series(None, "002361")
    assert res["available"] is False
    assert "无数据库会话" in res["reason"]


def test_series_blank_symbol_degrades(db):
    """@spec EXDAY-004 空代码显式降级"""
    res = query_series(db, "   ")
    assert res["available"] is False
    assert "缺少标的代码" in res["reason"]


# ────────────────────────── API 端点 ──────────────────────────

@pytest.fixture()
def fresh_cache(monkeypatch):
    """干净 L1 缓存(测试环境无 Redis, 单例 L1 会跨用例串值)。"""
    c = BizCache()
    monkeypatch.setattr(api, "biz_cache", c)
    return c


def test_endpoint_returns_series_and_caches(monkeypatch, session_factory, fresh_cache):
    """@spec EXDAY-001 端点透传口径标签; 成功结果进缓存(第二次不打库)"""
    with session_factory() as s:
        s.add(_day("20261023"))
        s.add(_day("20261027"))
        s.commit()
    monkeypatch.setattr(api, "SessionLocal", session_factory)

    res = api.fund_structure_series("002361")
    assert res["available"] is True
    assert res["caliber"] == "tdx_exday4"
    assert res["count"] == 2
    assert res["data_since"] == "20261023"

    # 库清空后第二次调用仍返回缓存值(300s TTL 内)
    with session_factory() as s:
        s.execute(text("DELETE FROM exday_daily"))
        s.commit()
    res2 = api.fund_structure_series("002361")
    assert res2["count"] == 2


def test_endpoint_refresh_bypasses_cache(monkeypatch, session_factory, fresh_cache):
    """refresh=1 绕过缓存: 写穿/补采后立即可见"""
    with session_factory() as s:
        s.add(_day("20261023"))
        s.commit()
    monkeypatch.setattr(api, "SessionLocal", session_factory)
    assert api.fund_structure_series("002361")["count"] == 1

    with session_factory() as s:
        s.add(_day("20261024"))
        s.commit()
    assert api.fund_structure_series("002361", refresh=1)["count"] == 2


def test_endpoint_failure_not_cached(monkeypatch, fresh_cache):
    """@spec EXDAY-004 取数失败显式降级且**不写缓存**(恢复后下一个请求立即可见)"""
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(api, "SessionLocal", boom)
    res = api.fund_structure_series("002361")
    assert res["available"] is False
    assert "取数失败" in res["reason"]
    assert fresh_cache.get_json(api._cache_key("002361", "CN", None, None, 30)) is None


def test_endpoint_blank_symbol_degrades(monkeypatch, fresh_cache):
    """@spec EXDAY-004 空代码显式降级(不打库、不进缓存)"""
    res = api.fund_structure_series("  ")
    assert res["available"] is False
    assert "缺少标的代码" in res["reason"]


def test_endpoint_supports_sparse_ui_contract(monkeypatch, session_factory, fresh_cache):
    """@spec EXDAY-005 稀疏数据展示契约的供给端: count/data_since 必须随响应下发,
    前端据此「单日只展示当日结构不画趋势线」并标注「数据自 2026-10-23 起累积」
    (渲染侧行为由 frontend/src/components/__tests__/fund-structure-panel.test.tsx 钉)。"""
    with session_factory() as s:
        s.add(_day("20261023", symbol="600519"))
        s.commit()
    monkeypatch.setattr(api, "SessionLocal", session_factory)
    res = api.fund_structure_series("600519")
    assert res["available"] is True
    assert res["count"] == 1
    assert res["data_since"] == "20261023"
