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

    def fake_read(db, key, params=None, fmt=None):
        return list(table.get(key, []))

    monkeypatch.setattr(tc, "_read", fake_read)
    out = _run(tc.query_data_domain(None, {"domain": "etf.flow"}))
    assert "未知数据域" not in out
    assert "尚未接入实现" not in out
    assert out.startswith("**etf.flow")
    assert "20 只" in out and "SC8" in out
    assert len(out) <= tc.MAX_DOMAIN_CHARS


def test_etf_flow_reachable_with_empty_tables(monkeypatch):
    monkeypatch.setattr(tc, "_read", lambda db, key, params=None, fmt=None: [])
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
    payload_s = feed.support_payload(_runner(table), 750, now=_NOW_LIVE)
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

    raised: list[str] = []

    # 签名必须与 `_read(db, key, params, fmt)` 一致：少一个 fmt 参数时 CPython 抛
    # TypeError 而不是 Boom，用例会「因为桩没被正确调用」而假绿。
    def boom(db, key, params=None, fmt=None):
        raised.append(key)
        raise Boom("SELECT * FROM etf_minute_flow -- secret")

    monkeypatch.setattr(tc, "_read", boom)
    out = _run(tc.query_data_domain(None, {"domain": "etf.flow"}))
    assert raised, "桩没被调用到，异常脱敏路径未被执行"
    assert "未取到" in out
    assert "读取失败" in out or "数据库连接/驱动错误" in out
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


# ── 5. 独立复审回归：覆盖率 / 基线样本 / 时点 / 驱动凭据 / 唯一取数路径 ──────
#
# 这一批全部用**真 SQLite 表 + 真 SQL**（tests/conftest.py 已把库指向临时 SQLite 文件），
# 不用回放桩，目的是让 LIMIT、:d 绑定、GROUP BY、IN 展开、日期比较都真的被执行一次。

from sqlalchemy import text  # noqa: E402

from src.web.database import engine  # noqa: E402

_BASKET = [c for c, _n, _c in feed.ew.ETF_BASKET]
_TODAY = "20261106"          # 2026-11-06 周五，交易日
_BASE1 = "20261103"
_BASE2 = "20261104"
_NOW_LIVE = datetime(2026, 11, 6, 10, 0, tzinfo=_CST)


@pytest.fixture()
def flow_db():
    """真表夹具：写入 etf_minute_flow / etf_flow_daily，用例前后清空（不碰生产数据）。"""

    def _wipe():
        with engine.begin() as c:
            c.execute(text("DELETE FROM etf_minute_flow"))
            c.execute(text("DELETE FROM etf_flow_daily"))

    _wipe()
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()
        _wipe()


def _ins_min(conn, day: str, minute: str, code: str, wan: float) -> None:
    conn.execute(
        text(
            "INSERT INTO etf_minute_flow (trade_date, minute, etf_code, amount_wan, volume) "
            "VALUES (:d, :m, :c, :a, 0)"
        ),
        {"d": day, "m": minute, "c": code, "a": wan},
    )


def _ins_flow(conn, day: str, scale: float, net: float | None) -> None:
    conn.execute(
        text(
            "INSERT INTO etf_flow_daily (trade_date, scale_yi, net_sub_yi) "
            "VALUES (:d, :s, :n)"
        ),
        {"d": day, "s": scale, "n": net},
    )


def _day(conn, day: str, minute: str, n_etfs: int, wan_each: float) -> None:
    """写某日某分钟的 n 只 ETF（n 可 <20，用来构造「过半但不全」）。"""
    for code in _BASKET[:n_etfs]:
        _ins_min(conn, day, minute, code, wan_each)


def _sql_runner(conn):
    """真实 SQL 执行器：走 tools_catalog._read 唯一路径（含 LIMIT 守卫）。"""
    return lambda key, params=None, fmt=None: tc._read(conn, key, params or {}, fmt)


# 5.1 齐备口径：全篮子 20/20；16/20 只的分钟是残缺分钟，不给结论
def test_short_basket_minute_is_not_a_usable_minute(flow_db):
    """16/20 只 ≠ 齐备（旧口径把它当「达 80% 闸门」）：出口必须报残缺、不给结论。

    2026-11-10 复审反例：19 日 16/20 只的基线 + 今日 20/20 只 → 旧口径 ratio=1.25「偏活跃」。
    齐备改成全篮子后，16/20 只的分钟既不能算进累计，也不能算进覆盖。
    """
    _day(flow_db, _TODAY, "0930", 20, 1_000.0)
    _day(flow_db, _TODAY, "0931", 20, 1_000.0)
    _day(flow_db, _TODAY, "0932", 20, 1_000.0)
    _day(flow_db, _TODAY, "0933", 16, 1_000.0)   # 只有 16 只：不是齐备
    flow_db.commit()

    p = feed.intraday_payload(_sql_runner(flow_db), _TODAY, now=_NOW_LIVE)
    t = p["totals"]
    assert t["etfs_expected"] == 20
    assert t["min_etfs_for_complete"] == 20, "齐备门槛就是全篮子，不再有 16 只这条闸门"
    assert t["minutes"] == 4
    assert t["complete_minutes"] == 3
    assert t["full_minutes"] == t["strict_minutes"] == 3
    assert t["minutes_short_basket"] == 1
    assert t["coverage"] == pytest.approx(0.75)
    assert p["state"] == "insufficient", "有残缺分钟就不给活跃度结论"
    assert t["conclusive"] is False and t["ratio_vs_baseline"] is None
    assert p["series"][3]["complete"] is False

    out = tc.render_etf_flow(
        as_of=_TODAY, source="etf_minute_flow", intraday=p,
        support={"state": "no_data"},
    )
    assert "≥16/20" not in out, "80% 闸门不许再被当成「可用分钟」"
    assert "3 个 20/20 只齐备" in out
    assert "1 个分钟篮子缺只" in out
    assert "不给活跃度结论" in out
    assert "20 只齐全" not in out, "16 只过闸门被写成 20 只齐全就是虚报覆盖"
    assert "未披露 亿元" not in out, "没有基线时不要拼出半个单位"


def test_coverage_gate_boundary_15_of_20_is_insufficient(flow_db):
    """15 只 < 16 → 该分钟不可用；整天只有 15 只 → 判数据不全。"""
    _day(flow_db, _TODAY, "0930", 15, 1_000.0)
    flow_db.commit()
    p = feed.intraday_payload(_sql_runner(flow_db), _TODAY, now=_NOW_LIVE)
    assert p["totals"]["full_minutes"] == 0
    assert p["totals"]["strict_minutes"] == 0
    assert p["state"] == "insufficient"
    assert p["series"][0]["complete"] is False


# 5.2 基线样本：请求 20 日 ≠ 有 20 日；缺的日样本数要报实数，归因未确认
def test_baseline_sample_count_is_actual_and_unconfirmed(flow_db):
    """真表只有 2 个基线交易日：出口必须写 2/20 + 缺 18，且不许说「过去 20 个交易日」。"""
    for minute in ("0930", "0931"):
        _day(flow_db, _TODAY, minute, 20, 2_000.0)      # 4 亿/分钟 → 累计 8 亿
        _day(flow_db, _BASE1, minute, 20, 1_000.0)      # 2 亿/分钟
        _day(flow_db, _BASE2, minute, 20, 1_000.0)      # 2 亿/分钟
    _ins_flow(flow_db, _BASE1, 20_000.0, 90.0)
    _ins_flow(flow_db, _BASE2, 20_100.0, 120.0)
    flow_db.commit()

    p = feed.intraday_payload(_sql_runner(flow_db), _TODAY, now=_NOW_LIVE)
    t = p["totals"]
    assert t["cum_yi"] == pytest.approx(8.0, abs=0.01)
    assert t["baseline_avg_yi"] == pytest.approx(4.0, abs=0.01), "2 日同分钟累计均值 =(4+4)/2"
    assert t["ratio_vs_baseline"] == pytest.approx(2.0, abs=0.01)
    assert t["baseline_days"] == 2
    assert t["baseline_days_requested"] == 20
    assert t["baseline_missing_days"] == 18, "缺的交易日样本数必须精确"
    assert t["baseline_days_in_window"] == 2
    assert t["baseline_sample_days"] == 2, "09:31 这一分钟真正参与均值的交易日数"
    assert t["baseline_confirmed"] is False
    assert "仅 2 个交易日" in p["note"]
    assert "缺 18 个交易日样本" in p["note"]
    assert "未确认" in p["note"]
    assert "过去 20 个交易日" not in p["note"]

    support = feed.support_payload(_sql_runner(flow_db), 750, now=_NOW_LIVE)
    out = tc.render_etf_flow(as_of=_TODAY, source="etf_minute_flow", intraday=p, support=support)
    assert "2/20 个交易日" in out
    assert "缺 18 个交易日样本" in out and "未确认" in out
    assert "过去 20 个交易日" not in out
    assert len(out) <= tc.MAX_DOMAIN_CHARS


def test_full_baseline_window_is_confirmed(flow_db):
    """20 个基线交易日都在 → 才能说「过去 20 个交易日」，且 confirmed=True。"""
    for minute in ("0930", "0931"):
        _day(flow_db, _TODAY, minute, 20, 1_000.0)
        for i in range(20):
            _day(flow_db, f"202610{3 + i:02d}" if 3 + i <= 31 else f"202611{3 + i - 31:02d}", minute, 20, 1_000.0)
    flow_db.commit()
    p = feed.intraday_payload(_sql_runner(flow_db), _TODAY, now=_NOW_LIVE)
    t = p["totals"]
    assert t["baseline_days"] == 20
    assert t["baseline_missing_days"] == 0
    assert t["baseline_confirmed"] is True
    assert "过去 20 个交易日" in p["note"]


# 5.3 时点：数据日相位 ≠ 当前时段相位（回退时尤其不能混）
def test_time_point_separates_data_date_from_current_session(flow_db):
    """周五 10:00 盘中，只有周四数据：数据日=已收盘，当前时段=盘中，且标非当日盘面。"""
    _day(flow_db, "20261105", "0930", 20, 1_000.0)
    flow_db.commit()

    p = feed.intraday_payload(_sql_runner(flow_db), None, now=_NOW_LIVE)
    assert p["date"] == "20261105"
    assert p["fallback_from"] == _TODAY
    assert p["phase"] == "closed", "数据日（11-05）已收盘"
    assert p["session_phase"] == "live", "当前时段是 11-06 盘中"
    assert p["session_time"] == "10:00"
    assert p["stale"] is True

    out = tc.render_etf_flow(as_of=p["date"], source="etf_minute_flow", intraday=p)
    assert "当前时段 盘中（10:00）" in out
    assert "数据日 20261105（已收盘）" in out
    assert "非当日盘面" in out


def test_fallback_never_uses_future_dated_rows(flow_db):
    """表里有未来日期的行时，回退基准必须是 < 今日的最近交易日，不能取未来日。"""
    _day(flow_db, "20261104", "0930", 20, 1_000.0)
    _day(flow_db, "20261109", "0930", 20, 1_000.0)   # 未来（下周一）
    flow_db.commit()

    p = feed.intraday_payload(_sql_runner(flow_db), None, now=_NOW_LIVE)
    assert p["date"] == "20261104", p["date"]
    assert p["date"] < _TODAY
    assert p["fallback_from"] == _TODAY


# 5.4 驱动异常：连接参数（账号/密码/host/DSN）不得出现在任何用户可见内容或日志
def test_driver_error_does_not_leak_connection_credentials(monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError

    dsn = "postgresql://sida:Secr3tPw@10.0.0.5:5432/sida"
    leak = Exception(f"could not connect to server: {dsn} password=hunter2 user=sida host=10.0.0.5")

    raised: list[str] = []

    def boom(db, key, params=None, fmt=None):
        raised.append(key)
        raise OperationalError("SELECT 1 FROM etf_minute_flow", {"d": "20261106"}, leak)

    monkeypatch.setattr(tc, "_read", boom)
    with caplog.at_level("WARNING", logger="src.agents.chat.tools_catalog"):
        out = _run(tc.query_data_domain(None, {"domain": "etf.flow"}))

    assert raised, "驱动异常路径没被触发（桩签名不匹配会抛 TypeError 掩盖目标异常）"
    for secret in ("Secr3tPw", "hunter2", "10.0.0.5", "postgresql://", "sida@", "password="):
        assert secret not in out, f"用户可见内容泄露了 {secret}"
        assert secret not in caplog.text, f"日志泄露了 {secret}"
    assert "数据库连接/驱动错误" in out or "读取失败" in out


def test_generic_exception_with_dsn_is_masked():
    """非驱动异常（RuntimeError 等）里夹带 DSN 时也要打码。"""
    reason = tc._etf_reason(RuntimeError("connect failed postgresql://u:Pa55wd@1.2.3.4:5432/db user=u"))
    assert "Pa55wd" not in reason
    assert "1.2.3.4" not in reason
    assert "postgresql://***" in reason


def test_safe_schema_fact_still_reported():
    """脱敏不等于把有用信息也吞掉：表不存在的 schema 事实要保留。"""
    reason = tc._etf_reason(RuntimeError("no such table: etf_minute_flow"))
    assert "no such table" in reason
    assert "etf_minute_flow" in reason


# 5.5 唯一取数路径：IN 展开也要经 _read（可被打桩/受 LIMIT 守卫）
def test_expanded_in_query_goes_through_read(monkeypatch):
    calls: list[tuple[str, dict, dict]] = []
    table = {
        "etf_minute_rows": [{"minute": "0930", "amount_wan": 1e4, "etfs": 20}],
        "etf_baseline_dates": [{"trade_date": "20261105"}],
        "etf_baseline_rows": [
            {"minute": "0930", "trade_date": "20261105", "amount_wan": 1e4, "etfs": 20}
        ],
        "etf_support": [{"trade_date": "20261105", "etf_scale": 20000.0, "etf_net_sub": 120.0}],
        "etf_daily_dates": [{"trade_date": "20261105"}],
        "etf_daily_minute": [{"trade_date": "20261105", "etf_code": _BASKET[0], "amount_wan": 1e4}],
        "etf_daily_net": [{"trade_date": "20261105", "etf_scale": 20000.0, "etf_net_sub": 120.0}],
    }

    def fake_read(db, key, params=None, fmt=None):
        calls.append((key, dict(params or {}), dict(fmt or {})))
        return list(table.get(key, []))

    monkeypatch.setattr(tc, "_read", fake_read)
    out = _run(tc.query_data_domain(None, {"domain": "etf.flow", "params_json": '{"days": 5}'}))
    assert out.startswith("**etf.flow")
    assert "读取失败" not in out
    keys = [c[0] for c in calls]
    assert "etf_baseline_rows" in keys, "IN 展开语句必须经 _read（旧实现自己开 session，桩拦不住）"
    base = [c for c in calls if c[0] == "etf_baseline_rows"][0]
    assert base[2].get("ph", "").startswith(":d0")
    assert "d0" in base[1]
    assert base[1]["d0"] == "20261105"


def test_read_rejects_expanded_statement_without_limit(monkeypatch):
    """_read 的 LIMIT 守卫对展开后的语句同样生效。"""
    monkeypatch.setitem(tc._SQL, "etf_baseline_rows", "SELECT minute FROM etf_minute_flow WHERE trade_date IN ({ph})")
    with pytest.raises(ValueError):
        tc._read(None, "etf_baseline_rows", {"d0": "20261105"}, {"ph": ":d0"})


# 5.6 SC8 不再用同源 daily 结果「救场」冒充全量样本
def test_sc8_not_fabricated_from_same_source_daily_window(monkeypatch):
    table = {
        "etf_support": [],   # SC8 读不到
        "etf_daily_dates": [{"trade_date": "20261105"}],
        "etf_daily_minute": [{"trade_date": "20261105", "etf_code": _BASKET[0], "amount_wan": 1e4}],
        "etf_daily_net": [{"trade_date": "20261105", "etf_scale": 20000.0, "etf_net_sub": 500.0}],
        "etf_minute_rows": [{"minute": "0930", "amount_wan": 1e4, "etfs": 20}],
    }

    def fake_read(db, key, params=None, fmt=None):
        if key == "etf_support":
            raise RuntimeError("SC8 表不可读")
        return list(table.get(key, []))

    monkeypatch.setattr(tc, "_read", fake_read)
    out = _run(tc.query_data_domain(None, {"domain": "etf.flow"}))
    assert "市场级 SC8" in out
    assert "托底日" not in out, "SC8 没取到时不许用 30 日 daily 窗口伪造托底日统计"
    assert "SC8 表不可读" in out or "读取失败" in out


# 5.7 与原 /api/etf-watch 数值等价（同一份真表数据，旧算法逐字复算）
def _old_reference_intraday(conn, target: str) -> dict:
    """照抄重构前 `src/web/api/etf_watch.py` 的算法（真 SQL，不含新字段）。"""
    rows = [dict(r) for r in conn.execute(
        text(
            "SELECT minute, SUM(amount_wan) AS amount_wan, COUNT(DISTINCT etf_code) AS etfs "
            "FROM etf_minute_flow WHERE trade_date = :d GROUP BY minute ORDER BY minute"
        ),
        {"d": target},
    ).mappings()]
    base_dates = [r["trade_date"] for r in conn.execute(
        text(
            "SELECT DISTINCT trade_date FROM etf_minute_flow WHERE trade_date < :d "
            "ORDER BY trade_date DESC LIMIT :n"
        ),
        {"d": target, "n": 20},
    ).mappings()]
    expected = len(_BASKET)
    per_day: dict[str, dict[str, float]] = {}
    if base_dates:
        ph = ", ".join(f":d{i}" for i in range(len(base_dates)))
        params = {f"d{i}": v for i, v in enumerate(base_dates)}
        for r in conn.execute(
            text(
                f"SELECT minute, trade_date, SUM(amount_wan) AS amount_wan, "
                f"COUNT(DISTINCT etf_code) AS etfs FROM etf_minute_flow "
                f"WHERE trade_date IN ({ph}) GROUP BY minute, trade_date"
            ),
            params,
        ).mappings():
            if int(r["etfs"] or 0) < expected * 0.8:
                continue
            per_day.setdefault(r["trade_date"], {})[r["minute"]] = float(r["amount_wan"] or 0.0)
    acc: dict[str, float] = {}
    cnt: dict[str, int] = {}
    for mins in per_day.values():
        cum = 0.0
        for m in sorted(mins):
            cum += mins[m]
            acc[m] = acc.get(m, 0.0) + cum
            cnt[m] = cnt.get(m, 0) + 1
    baseline = {m: (acc[m] / cnt[m]) / 1e4 for m in acc if cnt[m] > 0}

    cum = 0.0
    full = 0
    for r in rows:
        cum += float(r["amount_wan"] or 0.0) / 1e4
        if int(r["etfs"] or 0) >= expected * 0.8:
            full += 1
    last = rows[-1]["minute"] if rows else None
    base_at = baseline.get(last) if last else None
    ratio = (cum / base_at) if (base_at and base_at > 0) else None
    if full < max(1, len(rows)) * 0.8:
        state = "insufficient"
    elif ratio is None:
        state = "no_baseline"
    elif ratio >= 1.5:
        state = "surge"
    elif ratio >= 1.15:
        state = "active"
    elif ratio <= 0.7:
        state = "quiet"
    else:
        state = "normal"
    return {
        "cum_yi": round(cum, 2),
        "baseline_avg_yi": None if base_at is None else round(base_at, 2),
        "ratio_vs_baseline": None if ratio is None else round(ratio, 3),
        "state": state,
        "minutes": len(rows),
        "serie_cum": [round(sum(float(x["amount_wan"] or 0.0) / 1e4 for x in rows[: i + 1]), 2) for i in range(len(rows))],
    }


def test_numeric_equivalence_with_previous_api(flow_db):
    """同一份真表数据、同为全篮子时：新 payload 的关键数值/状态与重构前算法逐字段相等。

    2026-11-10 复审后口径收窄：18/20 只的今日与 16/20 只的基线不再「等价」—— 旧算法把
    它们当齐备，会算出 1.25 倍假活跃。等价的边界因此是「两侧都全篮子」，此时数值必须
    一字不差（重构没有偷偷改算法），口径收窄的影响由 test_etf_feed_completeness 钉住。
    """
    # 3 个基线交易日（含一天覆盖只有 10 只 → 旧/新都应剔除该日该分钟）
    for minute, wan in (("0930", 900.0), ("0931", 900.0), ("0932", 900.0)):
        _day(flow_db, _TODAY, minute, 20, 1_000.0)
        _day(flow_db, _BASE1, minute, 20, 800.0)
        _day(flow_db, _BASE2, minute, 20, 700.0)
        _day(flow_db, "20261102", minute, 10, 5_000.0)   # 不足 16 只 → 三天都被剔除
    flow_db.commit()

    old = _old_reference_intraday(flow_db, _TODAY)
    new = feed.intraday_payload(_sql_runner(flow_db), _TODAY, now=_NOW_LIVE)
    assert new["totals"]["cum_yi"] == old["cum_yi"]
    assert new["totals"]["baseline_avg_yi"] == old["baseline_avg_yi"]
    assert new["totals"]["ratio_vs_baseline"] == old["ratio_vs_baseline"]
    assert new["state"] == old["state"]
    assert new["totals"]["minutes"] == old["minutes"]
    assert [s["cum_yi"] for s in new["series"]] == old["serie_cum"]
    # 20261102 那天 10 只不达标 → 只应剩 2 个可用基线日
    assert new["totals"]["baseline_days"] == 2
    assert new["totals"]["baseline_days_in_window"] == 3
    assert new["totals"]["baseline_days_dropped"] == 1


def test_support_and_daily_payload_match_previous_api(flow_db):
    _ins_flow(flow_db, "20261103", 32_000.0, 50.0)
    _ins_flow(flow_db, "20261104", 32_238.13, 193.43)
    _ins_flow(flow_db, "20261105", 32_393.07, 154.94)
    for code in _BASKET:
        _ins_min(flow_db, "20261105", "0930", code, 1_000.0)
    flow_db.commit()

    sup = feed.support_payload(_sql_runner(flow_db), 750, now=_NOW_LIVE)
    rows = [dict(r) for r in flow_db.execute(
        text("SELECT trade_date, net_sub_yi FROM etf_flow_daily WHERE net_sub_yi IS NOT NULL ORDER BY trade_date")
    ).mappings()]
    assert [s["date"] for s in sup["series"]] == [r["trade_date"] for r in rows]
    assert [s["net_sub_yi"] for s in sup["series"]] == [round(r["net_sub_yi"], 2) for r in rows]
    assert sup["sample_count"] == len(rows)
    # 分位只在同一读源样本内算；没有任何「托底日」阈值筛选，旧字段必须消失。
    assert sup["latest_percentile_in_samples"] == round(
        sum(1 for r in rows if r["net_sub_yi"] < rows[-1]["net_sub_yi"]) / len(rows) * 100, 1
    )
    for gone in ("support_days", "support_count", "threshold_share_yi", "threshold_yi"):
        assert gone not in sup, gone
    assert sup["unit_confidence"] == "inferred"

    day = feed.daily_payload(_sql_runner(flow_db), 5)
    assert day["state"] == "ok"
    assert day["dates"] == ["20261105"]
    assert day["total_amount_yi"][0] == pytest.approx(2.0, abs=0.01)   # 20 × 1000 万 = 2 亿
    assert day["net_sub_yi"][0] == pytest.approx(154.94, abs=0.01)
    assert day["scale_yi"][0] == pytest.approx(32393.07, abs=0.01)


def test_daily_api_days_param_keeps_frontend_contract(flow_db):
    """日度 API（Web 看板）的 days 参数与列契约不变；参谋不再渲染日度多日合计。"""
    for i, d in enumerate(("20261103", "20261104", "20261105")):
        for code in _BASKET:
            _ins_min(flow_db, d, "0930", code, 1_000.0)
        _ins_flow(flow_db, d, 32_000.0, 100.0 + i)
    flow_db.commit()

    day2 = feed.daily_payload(_sql_runner(flow_db), 2)
    day3 = feed.daily_payload(_sql_runner(flow_db), 3)
    assert day2["dates"] == ["20261104", "20261105"]
    assert day3["dates"] == ["20261103", "20261104", "20261105"]
    assert day2["days_requested"] == 2
    assert day2["total_amount_yi"] == [pytest.approx(2.0, abs=0.01)] * 2
    assert day2["net_sub_yi"] == [pytest.approx(101.0, abs=0.01), pytest.approx(102.0, abs=0.01)]
    # 日度 payload 不再自带「完整日」判定字段（那套判定已被删掉，不做新口径）
    for gone in ("day_stats", "complete_days", "partial_days", "min_full_day_minutes"):
        assert gone not in day2, gone

    out = tc.render_etf_flow(
        as_of="20261105", source="etf_minute_flow",
        intraday=feed.intraday_payload(_sql_runner(flow_db), "20261105", now=_NOW_LIVE),
        support=feed.support_payload(_sql_runner(flow_db), 750, now=_NOW_LIVE),
    )
    assert "日度" not in out
    assert "完整日" not in out and "日合计" not in out and "完整" not in out


# 5.8 LIMIT 上界：不能把整窗口分组截断（原实现无 LIMIT）
def test_sql_limits_cover_full_windows():
    def _limit(key: str) -> int:
        m = re.search(r"LIMIT (\d+)\s*$", feed.SQL[key].strip(), re.I)
        assert m, key
        return int(m.group(1))

    assert _limit("etf_baseline_rows") >= feed.BASELINE_DAYS * feed.MAX_MINUTES_PER_DAY
    assert _limit("etf_daily_minute") >= 120 * len(_BASKET)   # days 上限 120 × 20 只
    assert _limit("etf_daily_net") >= 120                     # days 上限 120 天
    assert _limit("etf_minute_rows") >= feed.MAX_MINUTES_PER_DAY


# ── 6. 日度缺值：NULL 不得写成 0（API 层保留原语义，参谋层不再产出日度合计） ──────

def test_daily_all_none_is_missing_not_zero(flow_db):
    """成交额 NULL：原实现 `amount_wan or 0.0` 会把「全部没采到」写成本日 0.00 亿。"""
    for code in _BASKET:
        _ins_min(flow_db, "20261104", "0930", code, None)
    _ins_flow(flow_db, "20261104", 32_000.0, None)
    flow_db.commit()

    day = feed.daily_payload(_sql_runner(flow_db), 5)
    assert day["total_amount_yi"] == [None], "金额全 NULL 的日子必须是 None，不是 0.0"
    assert day["net_sub_yi"] == [None]

    out = tc.render_etf_flow(
        as_of="20261104", source="etf_minute_flow",
        intraday={"state": "no_data"}, support={"state": "no_data"},
    )
    assert "合计 0.00 亿元" not in out
    assert "净申购 0.00 亿元" not in out


def test_real_db_net_null_is_none_and_amounts_keep_actual_sample(flow_db):
    """真表：日度行存在但净申购为 NULL → 仍是 None，出口不许把它折成 0。"""
    for d, wan in (("20261104", 800.0), ("20261105", 1_000.0)):
        for code in _BASKET:
            _ins_min(flow_db, d, "0930", code, wan)
    _ins_flow(flow_db, "20261105", 32_000.0, None)
    flow_db.commit()

    day = feed.daily_payload(_sql_runner(flow_db), 5)
    assert day["state"] == "ok"
    assert day["days_requested"] == 5
    assert day["dates"] == ["20261104", "20261105"]
    assert day["total_amount_yi"] == [pytest.approx(1.6, abs=0.01), pytest.approx(2.0, abs=0.01)]
    assert day["net_sub_yi"] == [None, None], "净申购 NULL 必须原样保留 None"

    out = tc.render_etf_flow(
        as_of="20261105", source="etf_minute_flow",
        intraday={"state": "no_data"}, support={"state": "no_data"},
    )
    assert "3.60 亿元" not in out, "参谋不再给多日成交额合计"
    assert "净申购 0.00" not in out


# ── 7. 独立审计 5 处虚假数据：全部只留可证事实（2026-11-07 收敛） ──────────────
#
# 反例逐条落地：200/240 分钟、240 分钟但只有 16/20 只、金额全 NULL、
# SC8 落在分钟日期范围的空洞里、400+ 行且有未来日期 —— 参谋出口都不许
# 产出「完整日 / 虚假累计 / 空值 0 / 未来行」。

from datetime import timedelta  # noqa: E402

_NEXT_DAY = datetime(2026, 11, 7, 10, 0, tzinfo=_CST)   # 周六：当日无分钟 → 必须回退且不取未来行

_SESSION_MINUTES = [
    (datetime(2026, 11, 4, 9, 31) + timedelta(minutes=i)).strftime("%H%M")
    for i in range(120)
] + [
    (datetime(2026, 11, 4, 13, 1) + timedelta(minutes=i)).strftime("%H%M")
    for i in range(120)
]


def _bulk_minutes(conn, day: str, minutes, codes, wan) -> None:
    rows = [
        {"d": day, "m": m, "c": c, "a": wan}
        for m in minutes
        for c in codes
    ]
    conn.execute(
        text(
            "INSERT INTO etf_minute_flow (trade_date, minute, etf_code, amount_wan, volume) "
            "VALUES (:d, :m, :c, :a, 0)"
        ),
        rows,
    )


def test_200_of_240_minutes_is_not_a_complete_day(flow_db):
    """采了 200 分钟（不到收盘）：只能叫「已采集累计」，且必须点名收盘段缺失。"""
    _bulk_minutes(flow_db, "20261104", _SESSION_MINUTES[:200], _BASKET, 100.0)
    flow_db.commit()

    p = feed.intraday_payload(_sql_runner(flow_db), "20261104", now=_NOW_LIVE)
    assert p["totals"]["minutes"] == 200
    assert p["totals"]["cum_yi"] == pytest.approx(40.0, abs=0.01)   # 200 × 20 × 100 万

    out = tc.render_etf_flow(
        as_of="20261104", source="etf_minute_flow", intraday=p, support={"state": "no_data"},
    )
    assert "分钟级已采集累计" in out
    assert "已采集 200 分钟" in out
    assert "40.00" in out
    assert "收盘段（至 15:00）未采集" in out
    assert "完整" not in out and "日合计" not in out


def test_240_minutes_but_only_16_of_20_etfs_not_full(flow_db):
    """240 分钟全在、但每分钟只有 16/20 只：旧口径「过 80% 闸门」= 240 个可用分钟是虚报。"""
    _bulk_minutes(flow_db, "20261104", _SESSION_MINUTES, _BASKET[:16], 100.0)
    flow_db.commit()

    p = feed.intraday_payload(_sql_runner(flow_db), "20261104", now=_NOW_LIVE)
    t = p["totals"]
    assert (t["minutes"], t["complete_minutes"], t["strict_minutes"]) == (240, 0, 0)
    assert t["minutes_short_basket"] == 240
    assert t["coverage"] == 0.0
    assert p["state"] == "insufficient"
    assert t["conclusive"] is False and t["ratio_vs_baseline"] is None

    out = tc.render_etf_flow(
        as_of="20261104", source="etf_minute_flow", intraday=p,
        support={"state": "no_data"},
    )
    assert "0 个 20/20 只齐备" in out
    assert "240 个分钟篮子缺只" in out
    assert "完整" not in out and "日合计" not in out


def test_minute_amounts_all_null_never_counted_as_zero(flow_db):
    """分钟行在、金额全 NULL：累计必须是未知，不许补 0.00，也不算达标分钟。"""
    _bulk_minutes(flow_db, _TODAY, ["0930", "0931"], _BASKET, None)
    flow_db.commit()

    t = feed.intraday_payload(_sql_runner(flow_db), _TODAY, now=_NOW_LIVE)["totals"]
    assert t["cum_yi"] is None, "一笔金额都没采到 → 累计未知，不能是 0.00"
    assert t["minutes_without_amount"] == 2
    assert t["full_minutes"] == 0 and t["coverage"] == 0.0

    out = tc.render_etf_flow(
        as_of=_TODAY, source="etf_minute_flow",
        intraday=feed.intraday_payload(_sql_runner(flow_db), _TODAY, now=_NOW_LIVE),
        support={"state": "no_data"},
    )
    assert "0.00 亿元" not in out
    assert "已采集合计 未披露 亿元" in out
    assert "2 分钟金额全是 NULL" in out


def test_sc8_hole_and_newer_day_are_not_summed_into_basket(flow_db):
    """SC8 有 3 天（含分钟表的空洞日 20261104），参谋只报原始最新日/样本，不做跨表累计。"""
    _day(flow_db, "20261103", "0930", 20, 1_000.0)
    _day(flow_db, "20261105", "0930", 20, 1_000.0)   # 20261104 分钟缺失
    _ins_flow(flow_db, "20261103", 32_000.0, 100.0)
    _ins_flow(flow_db, "20261104", 32_100.0, 88.0)
    _ins_flow(flow_db, "20261105", 32_200.0, -20.0)
    flow_db.commit()

    sup = feed.support_payload(_sql_runner(flow_db), 750, now=_NOW_LIVE)
    assert sup["sample_count"] == 3
    assert sup["latest"] == {"date": "20261105", "scale_yi": 32200.0, "net_sub_yi": -20.0}

    out = tc.render_etf_flow(
        as_of="20261105", source="x", intraday={"state": "no_data"}, support=sup,
    )
    # SC8 是份额口径：不许再写「净申购 X 亿元」
    assert "20261105 份额变动 -20.00 亿份" in out
    assert "净申购 -20.00 亿元" not in out
    assert "样本窗口 20261103~20261105" in out
    assert "市场级累计净申购" not in out, "跨日 SC8 累计已被删除，不许复活"
    assert "168.00" not in out, "100 + 88 - 20 的伪造合计不得出现"
    assert "68.00" not in out, "100 + 88 - 120 之类跨窗口合计不得出现"


def test_future_and_bulk_rows_never_surface_as_data_day(flow_db):
    """400+ 行且含未来日期：回退只能取 < 目标日，未来行不得成为数据日。"""
    _day(flow_db, _TODAY, "0930", 20, 1_000.0)
    _bulk_minutes(flow_db, "20261231", _SESSION_MINUTES, _BASKET, 100.0)   # 4800 行未来数据
    flow_db.commit()

    p = feed.intraday_payload(_sql_runner(flow_db), None, now=_NEXT_DAY)
    assert p["date"] == _TODAY and p["fallback_from"] == "20261107"
    assert p["totals"]["minutes"] == 1

    out = tc.render_etf_flow(as_of=_TODAY, source="x", intraday=p, support={"state": "no_data"})
    assert "20261231" not in out
    assert "2.00 亿元" in out, "只报当日已采集的 20 × 1000 万"
    assert "完整" not in out and "日合计" not in out


# ── 8. 分钟来源：按 etf_minute_flow.source 读，不写死 TQ；混源/未知显式 ──────
#
# 三个反例：兜底日（em_trends2）被写成 TQ、同日混源被挑成单一来源、source 为 NULL
# 被默认认领成 TQ。出口必须分别给出 em_trends2 / mixed / unknown。

def _source_table(day: str, sources: list[str]) -> dict[str, list[dict]]:
    table = _intraday_table(day)
    table["etf_minute_sources"] = [
        {"trade_date": day, "source": s, "n": 60} for s in sources
    ]
    return table


def test_render_reports_real_em_source_not_hardcoded_tq():
    table = _source_table("20261106", ["em_trends2"])
    p = feed.intraday_payload(_runner(table), "20261106",
                              now=datetime(2026, 11, 6, 10, 0, tzinfo=_CST))
    assert p["source"] == "em_trends2"
    assert p["source_labels"] == ["东财 trends2"]
    out = tc.render_etf_flow(
        as_of="20261106", source="x", intraday=p, support={"state": "no_data"},
    )
    assert "分钟来源" in out and "东财 trends2" in out
    assert "通达信 TQ" not in out, "该日没有任何 TQ 行，出口不许写 TQ"


def test_render_flags_mixed_minute_sources():
    table = _source_table("20261106", ["tdx_tq", "em_trends2"])
    p = feed.intraday_payload(_runner(table), "20261106",
                              now=datetime(2026, 11, 6, 10, 0, tzinfo=_CST))
    assert p["source"] == "mixed"
    out = tc.render_etf_flow(
        as_of="20261106", source="x", intraday=p, support={"state": "no_data"},
    )
    assert "混合来源" in out
    assert "东财 trends2" in out and "通达信 TQ" in out
    assert "比值只作参考" in out, "混源基线必须显式降级为参考"


def test_render_unknown_source_is_not_labelled_tq():
    table = _intraday_table()          # 没有 etf_minute_sources 行 = 未取到/全 NULL
    p = feed.intraday_payload(_runner(table), "20261106",
                              now=datetime(2026, 11, 6, 10, 0, tzinfo=_CST))
    assert p["source"] == "unknown"
    assert p["source"] != "tdx_tq"
    out = tc.render_etf_flow(
        as_of="20261106", source="x", intraday=p, support={"state": "no_data"},
    )
    assert "来源未知" in out
    assert "不默认当成通达信 TQ" in out
