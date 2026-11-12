"""盘前账 PIT(时点)门禁 —— 事后信息绝不能被当成盘前信号(2026-11-12)。

背景(生产链路核实的两个泄漏):
① 盘前报告在 D 日开盘前生成, 但「位置/连板」取的是日线序列的**最后一根**收盘;
   decisions 端点虽按 `as_of=D` 截断, 用的仍是 `<=` 语义 → **D 日收盘(当天收盘后才知道)** 
   被当成盘前窗口的位置/连板。报告生成侧(premarket_outlook)干脆没有截断。
② 「股性因子」读的是 `demon_factors` 的**当前快照**(每股一行, 就地 upsert); 历史报告页
   把今天的因子表当「当时的信号」展示 —— 数字真, 时点假。表里 `factor_date` 是快照日,
   可用来判定: 快照日 >= 报告日 ⇒ 该行含报告日(或之后)的封板事件 ⇒ 盘前不可得。

口径(本文件钉死):
- 价格: 报告日 D 的报告, 只能用 **trade_date < D** 的收盘; 返回里带真实 `close_date`。
- 股性因子: 只有 `factor_date < D` 的快照可用于 D 日报告; 否则 `demon=None` + 原因文本,
  **不伪造历史快照, 不拿今天的表冒充当时**。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy import create_engine, text


# ── 纯函数层: price_signals 的盘前口径 ────────────────────────────────────


def test_price_signals_before_excludes_report_day_close():
    """D 日收盘不能出现在 D 日盘前报告的位置信号里。"""
    from src.core import premarket_ambush as pa

    closes = [
        ("2026-11-09", 10.0),
        ("2026-11-10", 10.5),
        ("2026-11-11", 12.0),  # 报告日收盘 —— 盘前不可得
    ]
    sig = pa.price_signals(closes, "600183", as_of=None, before="2026-11-11")
    assert sig["close"] == 10.5
    assert sig["close_date"] == "2026-11-10"
    assert sig["pct"] == pytest.approx(5.0, abs=0.01)


def test_price_signals_before_keeps_streak_from_known_closes_only():
    from src.core import premarket_ambush as pa

    closes = [
        ("2026-11-09", 10.0),
        ("2026-11-10", 11.0),  # 涨停(盘前可见)
        ("2026-11-11", 12.1),  # 报告日涨停 —— 不可见, 不能算进连板
    ]
    sig = pa.price_signals(closes, "600183", before="2026-11-11")
    assert sig["close_date"] == "2026-11-10"
    assert sig["limit_up_streak"] == 1


def test_price_signals_before_with_no_known_close_is_null():
    from src.core import premarket_ambush as pa

    sig = pa.price_signals([("2026-11-11", 12.0)], "600183", before="2026-11-11")
    assert sig["close"] is None
    assert sig["note"]


def test_price_signals_without_before_keeps_legacy_semantics():
    """老调用/老测试口径(as_of 含当日)保持不变 —— 收窄只发生在盘前路径。"""
    from src.core import premarket_ambush as pa

    closes = [("2026-11-05", 10.0), ("2026-11-06", 10.5), ("2026-11-11", 12.0)]
    assert pa.price_signals(closes, "600183", as_of="2026-11-06")["close"] == 10.5
    assert pa.price_signals(closes, "600183")["close"] == 12.0


def test_price_signals_unparseable_before_gives_no_number():
    """报告日认不出来就不给数字 —— 默认放行等于默认泄漏。"""
    from src.core import premarket_ambush as pa

    sig = pa.price_signals([("2026-11-09", 10.0), ("2026-11-10", 10.5)], "600183", before="???")
    assert sig["close"] is None and sig["close_date"] is None
    assert "无法解析" in sig["note"]


# ── 股性因子快照的时点判定 ────────────────────────────────────────────────


def test_filter_demon_by_asof_drops_snapshot_from_report_day_or_later():
    from src.core import premarket_ambush as pa

    factors = {
        "600183": {"total": 88.0, "factor_date": "20261111"},  # 报告日当天重算 → 不可得
        "600722": {"total": 70.0, "factor_date": "20261110"},  # 报告日前 → 可得
        "000636": {"total": 60.0},  # 无快照日 → 无法确认, 不猜
    }
    kept = pa.filter_demon_by_asof(factors, "2026-11-11")
    assert set(kept["kept"]) == {"600722"}
    assert kept["asof"] == "2026-11-10"
    assert set(kept["dropped"]) == {"600183", "000636"}
    assert kept["dropped_unknown_date"] == ["000636"]


def test_demon_map_cutoff_filters_at_sql_layer():
    """历史账页走 demon_map 直读表: 快照日 >= 报告日的行必须被剔除。"""
    from src.core import premarket_ambush as pa

    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE demon_factors (factor_date TEXT, symbol TEXT, name TEXT, total REAL,"
                " grade TEXT, n_sealed INT, max_streak INT, participation TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO demon_factors VALUES"
                " ('20261111','600183','生益科技',88.0,'妖',12,4,'活跃')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO demon_factors VALUES"
                " ('20261110','600722','金牛化工',70.0,'活跃',5,2,'一般')"
            )
        )
    with eng.connect() as conn:
        out = pa.demon_map(conn, ["600183", "600722"], cutoff="2026-11-11")
    assert set(out) == {"600722"}
    assert out["600722"]["score"] == 70.0
    assert out["600722"]["factor_date"] == "2026-11-10"


def test_demon_filters_refuse_unparseable_report_day():
    from src.core import premarket_ambush as pa

    factors = {"600183": {"total": 88.0, "factor_date": "20261110"}}
    out = pa.filter_demon_by_asof(factors, "not-a-date")
    assert out["kept"] == {}
    assert "无法解析" in out["notes"]["600183"]

    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE demon_factors (factor_date TEXT, symbol TEXT, name TEXT, total REAL,"
                " grade TEXT, n_sealed INT, max_streak INT, participation TEXT)"
            )
        )
        conn.execute(text("INSERT INTO demon_factors VALUES ('20261110','600183','x',88,'妖',1,1,'')"))
    with eng.connect() as conn:
        assert pa.demon_map(conn, ["600183"], cutoff="not-a-date") == {}


# ── collect_signals: 报告日 D 的两块都按 D 之前收窄 ────────────────────────


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Engine:
    def connect(self):
        return _Ctx()


def test_collect_signals_uses_closes_before_report_day(monkeypatch):
    from src.core import premarket_ambush as pa
    from src.core import decision_log

    series = {
        "600183": [("2026-11-09", 10.0), ("2026-11-10", 10.5), ("2026-11-11", 12.0)],
    }
    monkeypatch.setattr(
        decision_log, "_close_series_pg", lambda eng, sym, start: series.get(sym, [])
    )
    monkeypatch.setattr(pa, "demon_map", lambda *a, **k: {})

    out = pa.collect_signals(
        ["600183"], engine=_Engine(), start_day="2026-10-01", report_day="2026-11-11"
    )
    price = out["600183"]["price"]
    assert price["close"] == 10.5 and price["close_date"] == "2026-11-10"
    assert "盘前口径" in (price["note"] or "")


def test_collect_signals_marks_demon_unavailable_when_snapshot_is_not_earlier(monkeypatch):
    from src.core import premarket_ambush as pa
    from src.core import decision_log

    monkeypatch.setattr(decision_log, "_close_series_pg", lambda eng, sym, start: [])
    out = pa.collect_signals(
        ["600183"],
        demon_by_code={"600183": {"total": 88.0, "factor_date": "20261111"}},
        engine=_Engine(),
        start_day="2026-10-01",
        report_day="2026-11-11",
    )
    assert out["600183"]["demon"] is None
    assert "无" in out["600183"]["demon_note"] or "快照" in out["600183"]["demon_note"]


def test_collect_signals_keeps_demon_when_snapshot_precedes_report_day(monkeypatch):
    from src.core import premarket_ambush as pa
    from src.core import decision_log

    monkeypatch.setattr(decision_log, "_close_series_pg", lambda eng, sym, start: [])
    out = pa.collect_signals(
        ["600183"],
        demon_by_code={"600183": {"total": 88.0, "grade": "妖", "factor_date": "20261110"}},
        engine=_Engine(),
        start_day="2026-10-01",
        report_day="2026-11-11",
    )
    assert out["600183"]["demon"] is not None
    assert out["600183"]["demon"]["factor_date"] == "2026-11-10"
    assert out["600183"]["demon_note"] is None


def test_collect_signals_without_report_day_keeps_legacy_behaviour(monkeypatch):
    from src.core import premarket_ambush as pa
    from src.core import decision_log

    monkeypatch.setattr(
        decision_log,
        "_close_series_pg",
        lambda eng, sym, start: [("2026-11-10", 10.5), ("2026-11-11", 12.0)],
    )
    monkeypatch.setattr(pa, "demon_map", lambda *a, **k: {})
    out = pa.collect_signals(["600183"], engine=_Engine(), start_day="2026-10-01")
    assert out["600183"]["price"]["close"] == 12.0


def test_build_signal_detail_and_block_expose_real_asof():
    from src.core import premarket_ambush as pa

    row = {
        "code": "600183",
        "name": "生益科技",
        "tags": [],
        "event": "PCB涨价",
        "sector": "覆铜板",
    }
    price = {
        "close": 10.5,
        "close_date": "2026-11-10",
        "pct": 5.0,
        "limit_up_streak": 0,
        "pct_20d": None,
        "from_20d_high": None,
        "note": "盘前口径: 截至 2026-11-10 收盘(报告日 2026-11-11 收盘不计入)",
    }
    detail = pa.build_signal_detail(
        row, demon=None, price=price, demon_note="股性因子快照日不早于报告日, 盘前不可得"
    )
    assert detail["signals"]["demon"] is None
    assert "盘前不可得" in detail["signals"]["demon_note"]

    block = pa.build_signal_block([row], {"600183": {"demon": None, "price": price,
                                                     "demon_note": detail["signals"]["demon_note"]}})
    assert "2026-11-10" in block  # 真实收盘日上屏
    assert "盘前" in block  # 口径标注
    assert "盘前不可得" in block


# ── 决策账本端点: 端到端时点门禁 ────────────────────────────────────────────

REPORT = """
0. 事件驱动前瞻（双轨制 · 潜伏表）
| 事件 | 受益板块 | 代码 | 名称 | 受益逻辑 | 强度 | 埋伏方式 | 分批计划 | 止损 | 目标 | 证伪条件 |
|---|---|---|---|---|---|---|---|---|---|---|
| PCB涨价 | 覆铜板 | 600183 | 生益科技 | 龙头 | A | 回调埋伏 | 1/3 | 35.60 | +10% | 提价落空 |
"""


class _User:
    id = "u-pit"
    role = "owner"


def _clean():
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM analysis_history WHERE agent_name='premarket_outlook'"))
        db.execute(text("DELETE FROM demon_factors WHERE symbol='600183'"))
        db.commit()
    finally:
        db.close()


def _seed_report(date="2026-11-11"):
    from src.db.models import AnalysisHistory
    from src.db.session import SessionLocal

    db = SessionLocal()
    db.add(
        AnalysisHistory(
            user_id=None,
            agent_name="premarket_outlook",
            stock_symbol="*",
            analysis_date=date,
            title=f"【盘前分析】{date}",
            content=REPORT,
            raw_data={},
            status="success",
        )
    )
    db.commit()
    db.close()


def _seed_demon(symbol="600183", factor_date="20261111", total=88.0):
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT INTO demon_factors (factor_date, symbol, market, name, total, grade,"
                " n_sealed, max_streak, participation) VALUES"
                " (:d, :s, 'CN', '生益科技', :t, '妖', 12, 4, '活跃')"
            ),
            {"d": factor_date, "s": symbol, "t": total},
        )
        db.commit()
    finally:
        db.close()


def _patched(monkeypatch, series):
    from src.core import decision_log

    monkeypatch.setattr(
        decision_log, "_close_series_pg", lambda eng, sym, start: series.get(sym, [])
    )


def test_endpoint_signals_price_cannot_include_report_day_close(monkeypatch):
    _clean()
    _seed_report("2026-11-11")
    _patched(
        monkeypatch,
        {
            "600183": [
                ("2026-11-09", 10.0),
                ("2026-11-10", 10.5),
                ("2026-11-11", 12.0),  # 报告日收盘: 只能出现在「事后表现」, 不能进信号
                ("2026-11-12", 13.0),
            ]
        },
    )

    from src.web.api import decisions as dec

    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    row = out["items"][0]["rows"][0]
    price = row["signals"]["price"]
    assert price["close"] == 10.5, "报告日收盘被当成了盘前位置信号"
    assert price["close_date"] == "2026-11-10"
    # 事后表现仍然照算(它就是 post-hoc 字段, 与盘前信号分开)
    assert row["outcome"]["t0"] == pytest.approx((12.0 / 10.5 - 1) * 100, abs=0.05)
    # 端点上屏真实时点
    assert out["items"][0]["pit"]["price_before"] == "2026-11-11"


def test_endpoint_demon_snapshot_not_before_report_day_is_unavailable(monkeypatch):
    _clean()
    _seed_report("2026-11-11")
    _seed_demon(factor_date="20261111")  # 报告日当天重算的快照 → 盘前不可得
    _patched(monkeypatch, {"600183": [("2026-11-10", 10.5)]})

    from src.web.api import decisions as dec

    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    row = out["items"][0]["rows"][0]
    assert row["signals"]["demon"] is None
    assert row["signals"]["demon_note"]
    assert out["items"][0]["pit"]["demon_before"] == "2026-11-11"


def test_endpoint_demon_snapshot_before_report_day_is_kept(monkeypatch):
    _clean()
    _seed_report("2026-11-11")
    _seed_demon(factor_date="20261110")
    _patched(monkeypatch, {"600183": [("2026-11-10", 10.5)]})

    from src.web.api import decisions as dec

    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    row = out["items"][0]["rows"][0]
    assert row["signals"]["demon"] is not None
    assert row["signals"]["demon"]["score"] == 88.0
    assert row["signals"]["demon"]["factor_date"] == "2026-11-10"


# ── 报告生成侧: 明细块只用报告日之前的收盘 ───────────────────────────────────


def _detail_row(text: str) -> str:
    """取「信号明细」块里以代码开头的四列表行（原潜伏表行只有两列, 会被排除）。"""
    for line in reversed(text.splitlines()):
        if line.startswith("| 600183 |") and line.count("|") >= 5:
            return line
    raise AssertionError(f"明细块里没有该代码的行:\n{text}")


def test_agent_signal_detail_block_uses_previous_close(monkeypatch):
    from src.agents.premarket_outlook import PremarketOutlookAgent
    from src.core import premarket_ambush as pa

    monkeypatch.setattr(
        "src.core.decision_log._close_series_pg",
        lambda eng, sym, start: [
            ("2026-11-09", 9.5),
            ("2026-11-10", 10.0),
            ("2026-11-11", 12.0),
        ],
    )
    monkeypatch.setattr(pa, "demon_map", lambda *a, **k: {})
    monkeypatch.setattr("src.db.session.get_read_engine", lambda: _Engine())

    text = "0. 事件驱动前瞻\n| 代码 | 名称 |\n|---|---|\n| 600183 | 生益科技 |\n"
    data = {"timestamp": "2026-11-11T08:30:00", "demon_factors": {}}
    out = PremarketOutlookAgent()._append_ambush_signal_details(text, data)

    cell = _detail_row(out)
    assert "10.00" in cell and "2026-11-10" in cell
    assert "12.00" not in out, "报告日收盘被写进了盘前报告的信号明细"


def test_agent_signal_detail_block_drops_demon_snapshot_from_report_day(monkeypatch):
    from src.agents.premarket_outlook import PremarketOutlookAgent
    from src.core import premarket_ambush as pa

    monkeypatch.setattr(
        "src.core.decision_log._close_series_pg",
        lambda eng, sym, start: [("2026-11-10", 10.0)],
    )
    monkeypatch.setattr(pa, "demon_map", lambda *a, **k: {})
    monkeypatch.setattr("src.db.session.get_read_engine", lambda: _Engine())

    text = "0. 事件驱动前瞻\n| 代码 | 名称 |\n|---|---|\n| 600183 | 生益科技 |\n"
    data = {
        "timestamp": "2026-11-11T08:30:00",
        "demon_factors": {"600183": {"total": 99.5, "grade": "极妖", "factor_date": "20261111"}},
    }
    out = PremarketOutlookAgent()._append_ambush_signal_details(text, data)
    cell = _detail_row(out)
    assert "99.5" not in cell, "报告日的股性因子快照被当成盘前信号"
    assert "无数据" in cell


def test_prompt_header_states_real_data_time_and_drops_report_day_snapshot():
    """Prompt 里写明采集时点与盘前口径, 并把被剔除的因子快照说清楚。"""
    from src.agents.premarket_outlook import PremarketOutlookAgent

    class _Ctx:
        watchlist: list = []
        model_label = ""

    data = {
        "timestamp": "2026-11-11T08:30:00",
        "demon_factors": {"600183": {"total": 88.0, "grade": "妖", "factor_date": "2026-11-10"}},
        "demon_factors_asof": "2026-11-10",
        "demon_factors_dropped": ["600722"],
    }
    _system, user = PremarketOutlookAgent().build_prompt(data, cast(Any, _Ctx()))
    assert "数据采集时点：2026-11-11 08:30" in user
    assert "报告日当天收盘" in user
    assert "因子快照日 ≤ 2026-11-10" in user
    assert "已剔除 1 只" in user
