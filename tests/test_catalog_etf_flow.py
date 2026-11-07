# -*- coding: utf-8 -*-
"""ETF 看板进数智参谋数据域（etf.flow）验收（2026-11-07）。

钉死的四条：
1. **可达**：`query_data_domain("etf.flow")` 真的走到实现，不报未知/未接入；
2. **同源**：域内 SQL 与 `/api/etf-watch/*` 用的是 `src/core/etf_watch_feed.py` 同一批语句；
3. **口径**：SC8 是市场级，必须与 20 只篮子成交额分开写，且声明「不能拆到单只」；
4. **不编数/不泄露**：空结果写「未取到」，不写 0、不吐 SQL 正文、不写库。
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.chat import tools_catalog as tc  # noqa: E402
from src.core import etf_watch_feed as feed  # noqa: E402

_CST = ZoneInfo("Asia/Shanghai")
_SRC = (ROOT / "src" / "agents" / "chat" / "tools_catalog.py").read_text(encoding="utf-8")
_API_SRC = (ROOT / "src" / "web" / "api" / "etf_watch.py").read_text(encoding="utf-8")


def _run(coro):
    return asyncio.run(coro)


def _runner(table: dict[str, list[dict]]):
    """按语句键回放行；未登记的键返回空（等价空表）。"""
    calls: list[str] = []

    def run(key, params=None, fmt=None):
        calls.append(key)
        return list(table.get(key, []))

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _intraday_table(day: str = "20261106") -> dict[str, list[dict]]:
    expected = len(feed.ew.ETF_BASKET)
    full = [{"minute": f"093{i}", "amount_wan": 1e4, "etfs": expected} for i in range(3)]
    return {
        "etf_minute_rows": full,
        "etf_latest_date": [{"d": day}],
        "etf_baseline_dates": [{"trade_date": day}],
        "etf_baseline_rows": [
            {"minute": f"093{i}", "trade_date": day, "amount_wan": 1e4, "etfs": expected}
            for i in range(3)
        ],
        "etf_support": [
            {"trade_date": day, "etf_scale": 20000.0, "etf_net_sub": 120.0},
        ],
    }


# ── 1. 可达 ──────────────────────────────────────────────────────────────

def test_domain_is_registered_in_catalog_and_yaml():
    assert "etf.flow" in tc.DOMAIN_IDS
    assert "ETF" in tc.render_domain_catalog()


def test_query_data_domain_routes_to_etf_render(monkeypatch):
    """端到端：统一入口 → feed → render，缺数据也要出首行与「未取到」。"""
    table = _intraday_table()

    def fake_read(db, key, params=None):
        return list(table.get(key, []))

    monkeypatch.setattr(tc, "_read", fake_read)
    out = _run(tc.query_data_domain(None, {"domain": "etf.flow"}))
    assert "未知数据域" not in out
    assert "尚未接入实现" not in out
    assert out.startswith("**etf.flow")
    assert "20 只" in out and "SC8" in out
    assert len(out) <= tc.MAX_DOMAIN_CHARS


def test_etf_flow_reachable_with_empty_tables(monkeypatch):
    monkeypatch.setattr(tc, "_read", lambda db, key, params=None: [])
    out = _run(tc.query_data_domain(None, {"domain": "etf.flow"}))
    assert "未知数据域" not in out and "尚未接入实现" not in out
    assert "未取到" in out


# ── 2. 同源 ──────────────────────────────────────────────────────────────

def test_catalog_sql_is_the_same_object_as_api_feed():
    for key, sql in feed.SQL.items():
        assert tc._SQL[key] == sql, key
    # API 只做 HTTP 收尾，取数走 feed（页面与参谋不会两套数）
    assert "etf_watch_feed" in _API_SRC
    assert "FROM etf_minute_flow" not in _API_SRC


def test_catalog_keys_cover_every_etf_statement():
    assert set(feed.SQL) <= set(tc._SQL)


# ── 3. 口径：篮子 vs 市场级 SC8 ─────────────────────────────────────────

def test_basket_amount_and_sc8_are_separated_and_labeled():
    table = _intraday_table()
    payload_i = feed.intraday_payload(_runner(table), "20261106",
                                     now=datetime(2026, 11, 6, 10, 0, tzinfo=_CST))
    payload_s = feed.support_payload(_runner(table), 750)
    out = tc.render_etf_flow(
        as_of="20261106", source="etf_minute_flow / etf_flow_daily",
        intraday=payload_i, support=payload_s,
    )
    assert "篮子成交额" in out and "市场级 SC8" in out
    assert "不等于 20 只篮子的净申购" in out
    assert "单只 ETF 净申购不可用" in out
    # 篮子口径只出现 20 只合计，不得给出「单只净申购」
    assert "单只净申购" not in out
    assert "覆盖" in out


def test_intraday_coverage_is_reported_and_gates_state():
    expected = len(feed.ew.ETF_BASKET)
    partial = [
        {"minute": "0930", "amount_wan": 1e4, "etfs": expected},      # 齐全
        {"minute": "0931", "amount_wan": 1e4, "etfs": expected // 4},  # 缺票
    ]
    table = {
        "etf_minute_rows": partial,
        "etf_baseline_dates": [],
        "etf_baseline_rows": [],
    }
    p = feed.intraday_payload(_runner(table), "20261106",
                              now=datetime(2026, 11, 6, 10, 0, tzinfo=_CST))
    assert p["state"] == "insufficient"
    assert p["totals"]["minutes"] == 2
    assert p["totals"]["full_minutes"] == 1
    assert p["totals"]["coverage"] == pytest.approx(0.5)
    assert p["totals"]["etfs_expected"] == 20


def test_non_trading_day_is_labeled():
    table = {"etf_minute_rows": [], "etf_latest_date": []}
    p = feed.intraday_payload(_runner(table), "20261107",
                              now=datetime(2026, 11, 7, 10, 0, tzinfo=_CST))
    assert p["state"] == "no_data"
    assert p["phase"] == "non_trading_day"


def test_fallback_from_is_explicit():
    """当日无数据回退时，必须带 fallback_from，不能把旧数据当今天。"""
    table = {
        "etf_minute_rows": [],
        "etf_latest_date": [{"d": "20261105"}],
    }

    def run(key, params=None, fmt=None):
        if key == "etf_minute_rows" and (params or {}).get("d") == "20261105":
            return [{"minute": "0930", "amount_wan": 1e4, "etfs": 20}]
        if key == "etf_minute_rows":
            return []
        return list(table.get(key, []))

    p = feed.intraday_payload(run, None, now=datetime(2026, 11, 6, 10, 0, tzinfo=_CST))
    assert p["date"] == "20261105"
    assert p["fallback_from"] == "20261106"


# ── 4. 缺失不补 0 / 不泄露 / 不写库 ─────────────────────────────────────

def test_empty_payload_says_missing_not_zero():
    out = tc.render_etf_flow(
        as_of="未知", source="etf_minute_flow / etf_flow_daily",
        intraday={"state": "no_data", "note": "当日无分钟数据"},
        support={"state": "no_data", "note": "尚无落库数据"},
    )
    assert "未取到" in out
    assert "0.00 亿元" not in out
    # 口径提醒即使空数据也必须保留（否则模型会自己猜）
    assert "不等于 20 只篮子的净申购" in out


def test_read_failure_is_reported_without_sql_leak(monkeypatch):
    class Boom(Exception):
        pass

    def boom(db, key, params=None):
        raise Boom("SELECT * FROM etf_minute_flow -- secret")

    monkeypatch.setattr(tc, "_read", boom)
    out = _run(tc.query_data_domain(None, {"domain": "etf.flow"}))
    assert "未取到" in out
    assert "SELECT" not in out.upper()
    assert "etf_minute_flow --" not in out


def test_domain_is_read_only():
    for key, sql in feed.SQL.items():
        assert sql.lstrip().upper().startswith("SELECT"), key
        assert "LIMIT" in sql.upper(), key
        for bad in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
            assert bad not in sql.upper(), key
    # 取数路径不得落库：feed 与 handler 源码里没有 commit/flush
    feed_src = (ROOT / "src" / "core" / "etf_watch_feed.py").read_text(encoding="utf-8")
    assert ".commit(" not in feed_src and ".flush(" not in feed_src
    # 域内不得出现针对 ETF 表的写语句（渲染/取数路径纯只读）
    for bad in ("INSERT INTO ETF", "UPDATE ETF", "DELETE FROM ETF"):
        assert bad not in _SRC.upper(), bad
    # 渲染输出不含行级明细（避免把库内容当结论倒出来）
    out = tc.render_etf_flow(
        as_of="20261106", source="x",
        intraday={"state": "no_data"}, support={"state": "no_data"}, missing=[],
    )
    assert re.search(r"etf_code|amount_wan", out) is None
