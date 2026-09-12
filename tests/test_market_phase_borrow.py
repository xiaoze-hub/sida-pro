"""市场情绪周期"借鉴 TSP"增量测试(2026-09-12): 连板 run / 梯队完整度 / 分段 / 转移 / 分位 / 标定。

口径见 src/core/market_phase.py 模块头; 防抖动(EMA+2日确认)与"小样本记 null 不填 0"
是本次从 tick-stock-panel 借来的两条硬约束, 必须有测试钉住。
"""
from __future__ import annotations

from src.core import market_phase as mp


def _ev(d, sym, sealed=True, touched=True):
    return {"trade_date": d, "symbol": sym, "sealed": sealed, "touched": touched}


def test_consecutive_boards_run_and_reset():
    dates = ["20260901", "20260902", "20260903", "20260904"]
    sealed = {
        ("20260901", "A"): True, ("20260902", "A"): True, ("20260903", "A"): True,
        ("20260904", "A"): False,          # 断板 → 次日重新计 1
        ("20260904", "B"): True,
    }
    out = mp.consecutive_boards(dates, sealed)
    assert out[("20260901", "A")] == 1
    assert out[("20260902", "A")] == 2
    assert out[("20260903", "A")] == 3
    assert ("20260904", "A") not in out     # 未封板日不产生连板记录
    assert out[("20260904", "B")] == 1


def test_ladder_completeness_and_none_rules():
    assert mp.ladder_completeness([1, 2, 4], 4) == round(2 / 3, 4)   # 2..4 中 2,4 非空
    assert mp.ladder_completeness([1, 2, 3], 3) == 1.0
    assert mp.ladder_completeness([1, 2], 2) is None                # 最高板<3 记 None


def test_metrics_rows_promo_pool_and_seal_rate():
    dates = ["20260901", "20260902"]
    events = [_ev("20260901", f"S{i}") for i in range(4)] + \
             [_ev("20260901", f"S{i}", sealed=False) for i in range(4, 6)] + \
             [_ev("20260902", "S0"), _ev("20260902", "S1")]
    rows = mp.metrics_rows_from_events(dates, events)
    d1, d2 = rows
    assert d1["first_board"] == 4 and d1["max_height"] == 1
    assert d1["seal_rate"] == round(4 / 6, 4)                       # 真封板率 = 封/触
    assert d2["promo_rate"] is None                                 # 昨日池 4 < 10 → None 不填 0
    assert d2["promo_pool"] == 4


def test_classify_accumulating_when_history_short():
    rows = [{"date": f"2026090{i}", "first_board": 30, "ge2_count": 8, "ge3_count": 3,
             "ge5_count": 1, "max_height": 4, "promo_rate": 0.2, "seal_rate": 0.6,
             "sh_index_pct": 0.0} for i in range(1, 5)]
    assert mp.classify_phase_series(rows) == [mp.PHASE_ACCUMULATING] * 4


def test_two_day_confirmation_blocks_one_day_flip():
    base = {"first_board": 30, "ge2_count": 8, "ge3_count": 3, "ge5_count": 1,
            "max_height": 4, "promo_rate": 0.2, "seal_rate": 0.6, "sh_index_pct": 0.0}
    rows = [dict(base, date=f"202608{i:02d}") for i in range(1, 21)]
    labels = mp.classify_phase_series(rows)
    # 注入单日极端值: 确认机制应拦住, 不产生 1 天长的新段
    spike = dict(base, date="20260821", ge2_count=200, first_board=600, max_height=12)
    labels2 = mp.classify_phase_series(rows + [spike])
    assert labels2[-1] == labels[-1]


def test_segmentize_and_transition_stats():
    rows = [
        {"date": "20260901", "phase": "repair", "max_height": 3, "first_board": 20,
         "ge2_count": 5, "promo_rate": 0.2, "seal_rate": 0.6},
        {"date": "20260902", "phase": "repair", "max_height": 4, "first_board": 22,
         "ge2_count": 6, "promo_rate": 0.22, "seal_rate": 0.62},
        {"date": "20260903", "phase": "ignite", "max_height": 5, "first_board": 30,
         "ge2_count": 9, "promo_rate": 0.25, "seal_rate": 0.65},
        {"date": "20260904", "phase": "repair", "max_height": 3, "first_board": 18,
         "ge2_count": 4, "promo_rate": 0.18, "seal_rate": 0.58},
    ]
    segs = mp.segmentize(rows)
    assert [s["phase"] for s in segs] == ["repair", "ignite", "repair"]
    assert segs[0]["days"] == 2 and segs[0]["start"] == "20260901" and segs[0]["end"] == "20260902"
    assert segs[0]["avg_height"] == 3.5
    stats = mp.transition_stats(segs)
    assert stats["repair"]["count"] == 2
    # 转移只发生在段与段之间: repair→ignite, ignite→repair(两段 repair 不相邻, 无自转移)
    assert stats["repair"]["next"] == {"ignite": 1.0}
    assert stats["ignite"]["next_labels"] == {"修复": 1.0}
    assert stats["ignite"]["avg_days"] == 1.0


def test_percentile_of_basic_and_none():
    assert mp.percentile_of(5, [1, 2, 3, 4, 5, 6, 7, 8, 9]) == 44
    assert mp.percentile_of(None, [1, 2]) is None
    assert mp.percentile_of(5, []) is None


def test_calibrate_falls_back_then_overrides():
    small = [{"first_board": 30, "ge2_count": 8, "max_height": 4,
              "promo_rate": 0.2, "seal_rate": 0.6}] * 10
    thr, calibrated = mp.calibrate(small)
    assert calibrated is False and thr == mp.DEFAULT_THR

    big = []
    for i in range(200):
        big.append({"first_board": 20 + i % 50, "ge2_count": 5 + i % 30,
                    "max_height": 3 + i % 8, "promo_rate": 0.1 + (i % 30) / 100,
                    "seal_rate": 0.5 + (i % 20) / 100})
    thr2, calibrated2 = mp.calibrate(big)
    assert calibrated2 is True
    assert thr2["climax_ge2"] != mp.DEFAULT_THR["climax_ge2"]
    # 标定值必须来自自有分布: climax_ge2 = p90(ge2)*2
    assert abs(thr2["climax_ge2"] - mp._pct([r["ge2_count"] for r in big], 0.90) * 2) < 1e-9


# ── 端到端: 从 limit_up_events 回填(拦住行下标/SQL 方言这类只在写库时暴露的 bug) ──
def _mk_engines():
    """内存 SQLite: 建 limit_up_events + market_phase_daily 两张表(列与迁移 v164 一致)。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import StaticPool

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE limit_up_events (
              trade_date TEXT NOT NULL, symbol TEXT NOT NULL, market TEXT DEFAULT 'CN',
              touched INTEGER, is_sealed_close INTEGER, limit_days INTEGER
            )
            """))
        conn.execute(text(
            """
            CREATE TABLE market_phase_daily (
              date DATE PRIMARY KEY, first_board INTEGER NOT NULL DEFAULT 0,
              ge2_count INTEGER NOT NULL DEFAULT 0, ge3_count INTEGER NOT NULL DEFAULT 0,
              ge5_count INTEGER NOT NULL DEFAULT 0, max_height INTEGER NOT NULL DEFAULT 0,
              promo_rate REAL, seal_rate REAL, sh_index_pct REAL,
              phase TEXT NOT NULL DEFAULT '', updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              completeness REAL, phase_raw TEXT
            )
            """))
    return eng


def test_scan_from_events_end_to_end_and_idempotent(monkeypatch):
    import sqlalchemy as sa

    import src.db.session as dbs

    eng = _mk_engines()
    monkeypatch.setattr(dbs, "engine", eng)
    rows = []
    # 12 个交易日 × 12 只票: 前 11 只每天封板(连板递增), 第 12 只只首日封
    for i in range(12):
        d = f"202609{i + 1:02d}" if i < 10 else f"202610{i - 9:02d}"
        for j in range(11):
            rows.append((d, f"60000{j}", 1, 1))
        rows.append((d, "600099", 1, 1 if i == 0 else 0))
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO limit_up_events (trade_date, symbol, touched, is_sealed_close)"
                          " VALUES (:d, :s, :t, :sl)"),
                  [{"d": a, "s": b, "t": t, "sl": sl} for a, b, t, sl in rows])

    out = mp.scan_from_events(write=True)
    assert out["ok"] is True and out["days"] == 12, out
    with eng.begin() as c:
        got = c.execute(sa.text("SELECT date, first_board, ge2_count, max_height, phase, phase_raw,"
                                " completeness, seal_rate FROM market_phase_daily ORDER BY date")).fetchall()
    assert len(got) == 12
    assert got[0][1] == 12 and got[0][2] == 0 and got[0][3] == 1      # 首日全首板, 无连板
    assert got[3][1] == 0 and got[3][2] == 11 and got[3][3] == 4      # 第4日: 11 只全部 4 板(600099 仅首日封过)
    assert all(r[4] for r in got) and all(r[5] for r in got)           # phase 与 phase_raw 均已写
    assert got[3][6] == round(1 / 3, 4) and got[3][7] == round(11 / 12, 4)

    again = mp.scan_from_events(write=True)                            # 幂等: 不产生新行
    assert again["ok"] is True and again["days"] == 12
    with eng.begin() as c:
        assert c.execute(sa.text("SELECT COUNT(*) FROM market_phase_daily")).scalar() == 12
