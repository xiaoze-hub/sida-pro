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

from datetime import date as _date, timedelta as _td
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


def test_generation_filter_preopen_keeps_same_morning_snapshot():
    """生成发生在 D 日 09:30 前: 当天早上算的因子(factor_date=D)那一刻**确实可见**。

    这是「生成本身就是冻结凭证」的场景, 与历史重建不同 —— 历史重建没有这份凭证。
    """
    from src.core import premarket_ambush as pa

    factors = {
        "600183": {"total": 88.0, "factor_date": "20261111"},  # 当天早上算 → 盘前可见
        "600722": {"total": 70.0, "factor_date": "20261110"},
        "000636": {"total": 60.0},  # 无快照日 → 无法确认, 不猜
        "300001": {"total": 50.0, "factor_date": "20261112"},  # 晚于报告日 → 不可能当时可见
    }
    out = pa.filter_demon_for_generation(
        factors, "2026-11-11", generated_at="2026-11-11T08:30:00"
    )
    assert set(out["kept"]) == {"600183", "600722"}
    assert out["preopen"] is True
    assert out["asof"] == "2026-11-11"
    assert set(out["dropped"]) == {"000636", "300001"}
    assert out["dropped_unknown_date"] == ["000636"]


def test_generation_filter_post_open_drops_same_day_snapshot():
    """生成发生在收盘后: factor_date=D 的行已经含 D 日封板事件 → 盘前不可得。"""
    from src.core import premarket_ambush as pa

    factors = {
        "600183": {"total": 88.0, "factor_date": "20261111"},
        "600722": {"total": 70.0, "factor_date": "20261110"},
    }
    out = pa.filter_demon_for_generation(
        factors, "2026-11-11", generated_at="2026-11-11T20:00:00"
    )
    assert set(out["kept"]) == {"600722"}
    assert out["preopen"] is False


def test_generation_filter_without_timestamp_is_conservative():
    """拿不到生成时点 → 不能证明盘前可见, 只接受快照日严格早于报告日的行。"""
    from src.core import premarket_ambush as pa

    factors = {
        "600183": {"total": 88.0, "factor_date": "20261111"},
        "600722": {"total": 70.0, "factor_date": "20261110"},
    }
    out = pa.filter_demon_for_generation(factors, "2026-11-11")
    assert set(out["kept"]) == {"600722"}
    assert out["preopen"] is False


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
    out = pa.filter_demon_for_generation(
        factors, "not-a-date", generated_at="2026-11-11T08:30:00"
    )
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


def test_collect_signals_pit_requires_frozen_evidence_not_just_factor_date(monkeypatch):
    """因子快照日早于报告日 ≠ 当时可见: 没有生成时冻结凭证的历史重建一律 unknown。

    `demon_factors` 是每股一行、就地 upsert 的当前表 —— factor_date 只说明「这一行上次
    在哪天算过」, 既不证明生成时可见, 也不证明数据截至报告日前一交易日完整。所以即使
    factor_date < D, 只要调用方没给出**已验证的冻结快照**, 也不许当盘前信号。
    """
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
    assert out["600183"]["demon"] is None
    assert "冻结" in out["600183"]["demon_note"] or "凭证" in out["600183"]["demon_note"]


def test_collect_signals_pit_accepts_verified_frozen_snapshot(monkeypatch):
    """只有冻结证据被验证通过后, 调用方才把 demon_by_code 传进来(且带快照日)。"""
    from src.core import premarket_ambush as pa
    from src.core import decision_log

    monkeypatch.setattr(decision_log, "_close_series_pg", lambda eng, sym, start: [])
    out = pa.collect_signals(
        ["600183"],
        demon_by_code={"600183": {"total": 88.0, "grade": "妖", "factor_date": "2026-11-10"}},
        demon_notes={},
        verified_frozen=True,
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


def _seed_report(date="2026-11-11", raw_data=None):
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
            raw_data=raw_data or {},
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


def test_endpoint_live_demon_table_never_leaks_into_history(monkeypatch):
    """历史页不许拿当前 `demon_factors` 表重建「当时信号」。

    即使表里这一行的 factor_date 早于报告日, 它也可能是在报告日之后重算、只是快照日
    字段没变; 没有生成时冻结凭证就一律 unknown, 不造历史。
    """
    _clean()
    _seed_report("2026-11-11")
    _seed_demon(factor_date="20261110")
    _patched(monkeypatch, {"600183": [("2026-11-10", 10.5)]})

    from src.web.api import decisions as dec

    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    row = out["items"][0]["rows"][0]
    assert row["signals"]["demon"] is None
    assert "冻结" in (row["signals"]["demon_note"] or "") or "凭证" in (row["signals"]["demon_note"] or "")
    pit = out["items"][0]["pit"]
    assert pit["demon_evidence"] is False
    assert pit["demon_available"] == 0 and pit["demon_unavailable"] == 1


def test_endpoint_uses_verified_preopen_frozen_snapshot_from_raw_data(monkeypatch):
    """raw_data 里生成时冻结的因子快照(生成时点在 D 日 09:30 前)才是盘前凭证。"""
    _clean()
    _seed_report(
        "2026-11-11",
        raw_data={
            "timestamp": "2026-11-11T08:20:00",
            "demon_factors_frozen_at": "2026-11-11T08:20:00",
            "demon_factors": {
                "600183": {"total": 88.0, "grade": "妖", "factor_date": "20261110"},
            },
        },
    )
    _patched(monkeypatch, {"600183": [("2026-11-10", 10.5)]})

    from src.web.api import decisions as dec

    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    row = out["items"][0]["rows"][0]
    assert row["signals"]["demon"] is not None
    assert row["signals"]["demon"]["score"] == 88.0
    assert row["signals"]["demon"]["factor_date"] == "2026-11-10"
    pit = out["items"][0]["pit"]
    assert pit["demon_evidence"] is True
    assert pit["demon_frozen_at"].startswith("2026-11-11T08:20")


def test_endpoint_rejects_frozen_snapshot_captured_after_open(monkeypatch):
    """生成时点在开盘后/收盘后 → 冻结快照含当日事件, 不能证明盘前可见。"""
    _clean()
    _seed_report(
        "2026-11-11",
        raw_data={
            "timestamp": "2026-11-11T20:10:00",
            "demon_factors": {
                "600183": {"total": 88.0, "grade": "妖", "factor_date": "20261111"},
            },
        },
    )
    _patched(monkeypatch, {"600183": [("2026-11-10", 10.5)]})

    from src.web.api import decisions as dec

    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    row = out["items"][0]["rows"][0]
    assert row["signals"]["demon"] is None
    assert "盘前" in (row["signals"]["demon_note"] or "")
    assert out["items"][0]["pit"]["demon_evidence"] is False


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


def test_agent_signal_detail_block_keeps_preopen_same_morning_snapshot(monkeypatch):
    """08:30 生成时读到的 factor_date=D 快照是当天早上算的, 那一刻可见 → 保留并标快照日。"""
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
    assert "99.5" in cell
    assert "2026-11-11" in cell  # 快照日上屏, 不冒充「最新」


def test_agent_signal_detail_block_drops_post_close_snapshot(monkeypatch):
    """收盘后手工重生成: factor_date=D 的行已含 D 日封板事件 → 盘前不可得。"""
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
        "timestamp": "2026-11-11T20:00:00",
        "demon_factors": {"600183": {"total": 99.5, "grade": "极妖", "factor_date": "20261111"}},
    }
    out = PremarketOutlookAgent()._append_ambush_signal_details(text, data)
    cell = _detail_row(out)
    assert "99.5" not in cell
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


# ── 严格日期解析: 不把非法日期拼成「假日期」 ────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-10", "2026-11-10"),
        ("20261110", "2026-11-10"),
        ("2026-11-10 08:30:00", "2026-11-10"),
        ("2026-11-10T08:30:00+08:00", "2026-11-10"),
        (_date(2026, 11, 10), "2026-11-10"),
        ("2026-02-30", None),   # 不存在的日期: 拒绝, 不拼成 2026-02-30
        ("20261132", None),
        ("2026-13-01", None),
        ("20269999", None),
        ("2026/11/10", None),   # 未支持的格式: 拒绝, 不猜
        ("2026-1-1", None),
        ("abc20261110", None),  # 前缀垃圾: 不从字符串里抠数字
        ("", None),
        (None, None),
    ],
)
def test_norm_day_strictly_validates_and_never_invents(raw, expected):
    from src.core import premarket_ambush as pa

    assert pa._norm_day(raw) == expected


def test_parse_dt_strict_and_normalizes_to_beijing_naive():
    from datetime import datetime

    from src.core import premarket_ambush as pa

    assert pa._parse_dt("2026-11-11T08:30:00") == datetime(2026, 11, 11, 8, 30)
    assert pa._parse_dt("2026-11-11 08:30") == datetime(2026, 11, 11, 8, 30)
    assert pa._parse_dt("2026-11-11") == datetime(2026, 11, 11, 0, 0)
    # UTC 00:30 = 北京 08:30
    assert pa._parse_dt("2026-11-11T00:30:00Z") == datetime(2026, 11, 11, 8, 30)
    assert pa._parse_dt("2026-11-11T25:00:00") is None
    assert pa._parse_dt("not-a-date") is None
    assert pa._parse_dt(None) is None


def test_price_signals_drops_unparseable_bars_instead_of_comparing_garbage():
    from src.core import premarket_ambush as pa

    closes = [("2026-11-09", 10.0), ("not-a-date", 99.0), ("2026-11-10", 10.5)]
    sig = pa.price_signals(closes, "600183", before="2026-11-11")
    assert sig["close"] == 10.5
    assert sig["close_date"] == "2026-11-10"
    assert sig["pct"] == 5.0  # 用的是 10.5/10.0, 不是脏数据的 99


# ── 20 日指标: 必须足 21 根收盘, 否则 null(不拿短窗口冒充) ─────────────────


def _series(n: int, start: str = "2026-09-01", first: float = 10.0) -> list[tuple[str, float]]:
    d0 = _date.fromisoformat(start)
    return [((d0 + _td(days=i)).isoformat(), first + i * 0.1) for i in range(n)]


def test_price_signals_20d_is_null_with_only_20_bars():
    from src.core import premarket_ambush as pa

    sig = pa.price_signals(_series(20), "600183")
    assert sig["close"] is not None
    assert sig["pct_20d"] is None
    assert sig["from_20d_high"] is None
    assert "21" in (sig["note"] or "")


def test_price_signals_20d_uses_21st_bar_as_base():
    from src.core import premarket_ambush as pa

    closes = _series(20) + [("2026-10-01", 12.0)]
    sig = pa.price_signals(closes, "600183")
    assert sig["pct_20d"] is not None and sig["pct_20d"] > 0
    assert sig["from_20d_high"] is not None
    assert "21" not in (sig["note"] or "")


# ── 历史股性: 只认可验证的「生成时冻结快照」 ────────────────────────────────


def test_demon_evidence_requires_a_frozen_snapshot():
    from src.core import premarket_ambush as pa

    out = pa.demon_evidence_from_record({}, "2026-11-11")
    assert out["verified"] is False and out["kept"] == {}
    assert "冻结" in (out["reason"] or "") or "凭证" in (out["reason"] or "")


def test_demon_evidence_requires_parseable_generation_time():
    from src.core import premarket_ambush as pa

    raw = {"demon_factors": {"600183": {"total": 88.0, "factor_date": "20261110"}}}
    out = pa.demon_evidence_from_record(raw, "2026-11-11")
    assert out["verified"] is False
    assert "时点" in (out["reason"] or "")

    raw_bad = {
        "timestamp": "not-a-time",
        "demon_factors": {"600183": {"total": 88.0, "factor_date": "20261110"}},
    }
    assert pa.demon_evidence_from_record(raw_bad, "2026-11-11")["verified"] is False


def test_demon_evidence_rejects_snapshot_not_taken_preopen_on_report_day():
    from src.core import premarket_ambush as pa

    snap = {"600183": {"total": 88.0, "factor_date": "20261110"}}
    for stamp in ("2026-11-11T09:30:00", "2026-11-11T20:00:00", "2026-11-10T08:20:00"):
        out = pa.demon_evidence_from_record({"timestamp": stamp, "demon_factors": snap}, "2026-11-11")
        assert out["verified"] is False, stamp
        assert out["kept"] == {}
        assert "盘前" in (out["reason"] or "")


def test_demon_evidence_accepts_preopen_snapshot_and_drops_bad_rows():
    from src.core import premarket_ambush as pa

    raw = {
        "demon_factors_frozen_at": "2026-11-11T08:20:00",
        "demon_factors": {
            "600183": {"total": 88.0, "factor_date": "20261110"},
            "600722": {"total": 70.0},                      # 无快照日 → 剔
            "000636": {"total": 60.0, "factor_date": "20261112"},  # 晚于报告日 → 剔
            "300750": {"total": 50.0, "factor_date": "2026-02-30"},  # 非法日期 → 剔
        },
    }
    out = pa.demon_evidence_from_record(raw, "2026-11-11")
    assert out["verified"] is True
    assert set(out["kept"]) == {"600183"}
    assert out["kept"]["600183"]["factor_date"] == "2026-11-10"
    assert out["frozen_at"] == "2026-11-11T08:20:00"
    assert set(out["dropped"]) == {"600722", "000636", "300750"}


# ── 跨期缓存: 不能用新一期的起点截断旧一期的日线 ───────────────────────────


def test_endpoint_close_cache_does_not_truncate_older_period(monkeypatch):
    from datetime import date as _d, timedelta as _td2

    from src.core import decision_log

    _clean()
    _seed_report("2026-09-01")
    _seed_report("2026-11-11")
    calls: list[tuple[str, str]] = []

    def _series_by_start(_eng, sym, start):
        calls.append((sym, start))
        out = []
        day = _d(2026, 8, 1)
        for i in range(120):
            cur = day + _td2(days=i)
            if cur.weekday() >= 5:
                continue
            iso = cur.isoformat()
            if iso >= start:
                out.append((iso, 10.0 + i * 0.01))
        return out

    monkeypatch.setattr(decision_log, "_close_series_pg", _series_by_start)

    from src.web.api import decisions as dec

    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    items = {it["date"]: it for it in out["items"]}
    old = items["2026-09-01"]["rows"][0]["signals"]["price"]
    assert old["close"] is not None, "旧一期被新一期的日线起点截断 → 盘前位置变空"
    assert old["close_date"] == "2026-08-31"  # 09-01 之前最后一个交易日
    # 两次取数用同一(最早)起点; 不是各自 date-45 各取一份, 更不是缓存复用错起点
    assert len({start for _sym, start in calls}) == 1


def test_endpoint_invalid_report_date_gives_no_price_numbers(monkeypatch):
    """报告日非法 → 不拿收盘拼一个「当时位置」; 事后对账也明确留空。"""
    _clean()
    _seed_report("2026-13-40")
    _patched(monkeypatch, {"600183": [("2026-11-09", 10.0), ("2026-11-10", 10.5)]})

    from src.web.api import decisions as dec

    out = dec.decisions_premarket(days=5, max_rows=30, user=_User())
    item = out["items"][0]
    assert item["date"] == "2026-13-40"
    row = item["rows"][0]
    assert row["signals"]["price"]["close"] is None
    assert "无法解析" in (row["signals"]["price"]["note"] or "")
    assert row["outcome"]["t0"] is None
    assert "无法解析" in (row["outcome"]["note"] or "")
    assert item["pit"]["price_close_dates"] == []
