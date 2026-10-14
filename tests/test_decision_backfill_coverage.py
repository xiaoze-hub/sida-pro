"""决策账本回填**覆盖缺口**的钉子(2026-10-14)—— 生产实证的根因 + 修复回归。

生产真形态(2026-10-14 只读排查, 6759 行):
  - `resonance3` 块 5294 行(20260918~20260925, 一次性灌入), T+1 只填上 ~46%;
  - 逐日看: 20260918/20260921 100%, 而 **20260922/23/24 有 K 线却 0 填**(2352 行),
    20260925 是中秋休市(klines 全表无该日)却落了 597 条信号(锚点永久缺失);
  - 作业在跑(`app_jobs` 有 18:35 记录), 但 2026-10-10 10:17 一跑返回
    `{'scanned': 500, 'filled': 0}` —— **候选窗口被永久不可填的行占满**。

本文件锁死两件事:
  ① 不可填的行**不许挤掉可填的行**(查看预算与填充预算解耦), 且必须显式标注 `no_anchor`
     —— 收益列保持 NULL, **绝不用 0 冒充真值**;
  ② 定时 job 的单跑目标足够清积压(旧的 500/天 追不平 3000+ 积压)。

禁令: 断言强度不得降低; 测试**不触真实网络/真实库**(假 K 线 provider + 内存 sqlite)。
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from src.core import decision_log as dl
from src.web.migrations import _m175_decision_log, _m197_decision_log_outcome_state

NO_ANCHOR = dl.OUTCOME_NO_ANCHOR


@pytest.fixture(autouse=True)
def _clear_col_cache():
    """按引擎 id 的列存在性缓存会跨用例残留(内存库对象可能复用 id) → 每个用例前清一次。"""
    dl._state_col_cache.clear()
    dl._regime_col_cache.clear()
    yield
    dl._state_col_cache.clear()
    dl._regime_col_cache.clear()


def _engine(*, with_state: bool = True):
    """内存库: with_state=True 跑 v197(有标注列); False 只用 v175(模拟未迁移的老库)。"""
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m175_decision_log(conn)
        if with_state:
            _m197_decision_log_outcome_state(conn)
    return eng


def _insert(eng, *, symbol: str, trade_date: str, price: float, kind: str = "resonance3") -> None:
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO decision_log (symbol, trade_date, price_at_signal, signal_kind) "
                "VALUES (:s, :d, :p, :k)"
            ),
            {"s": symbol, "d": trade_date, "p": price, "k": kind},
        )


def _row(eng, symbol: str):
    with eng.connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT ret_t1, ret_t3, ret_t5, outcome_state, outcome_note "
                "FROM decision_log WHERE symbol = :s"
            ),
            {"s": symbol},
        ).fetchone()


def _iso(day: str) -> str:
    """紧凑 `20260925` / ISO `2026-09-25` → ISO(与生产 `_close_series_pg` 的 SQL 日期比较同义:
    库侧把 `ts >= '20260925 00:00:00+08'` 当日期解析, 不是字符串比较)。"""
    s = str(day).strip()[:10]
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _provider(bars: dict[str, list[tuple[str, float]]]):
    """假 K 线序列: 与生产 `_close_series_pg` 同口径(该日起升序, 每交易日一根)。"""

    def _p(_engine, symbol: str, start_day: str):
        start = _iso(start_day)
        return [b for b in bars.get(symbol, []) if _iso(b[0]) >= start]

    return _p


# ─────────────────── ① 迁移: 标注列 ───────────────────


def test_migration_197_idempotent_and_adds_columns():
    eng = _engine(with_state=False)
    with eng.begin() as conn:
        _m197_decision_log_outcome_state(conn)
        _m197_decision_log_outcome_state(conn)  # 再调一次不能报错
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(decision_log)"))}
    assert {"outcome_state", "outcome_note"} <= cols
    assert dl._has_outcome_state_column(eng) is True


def test_migration_197_skips_when_table_absent():
    """表都没有(极早的库)也不能炸 —— 迁移容错与 _m184 同风格。"""
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        _m197_decision_log_outcome_state(conn)
    assert dl._has_outcome_state_column(eng) is False


# ─────────────────── ② 核心回归: 不可填的行不许挤掉可填的行 ───────────────────


def test_unfillable_head_no_longer_starves_fillable_row():
    """生产根因的直接回归: 信号日无 K 线的行排在前面 + limit=1 → 旧口径整跑 0 填。

    修前: 候选 = `WHERE ret_* IS NULL ORDER BY trade_date ASC LIMIT 1` → 只看到 AAA(不可填),
    series[0] != 信号日 → 跳过 → `filled=0`, 而 BBB 永远轮不到。
    修后: 按 scan_cap 看、按 limit 填 + AAA 显式标注 → BBB 填上, AAA 收益列保持 NULL。
    """
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16", price=10.0)  # 不可填: 缺信号日那根
    _insert(eng, symbol="BBB", trade_date="2026-09-17", price=10.0)  # 可填
    prov = _provider(
        {
            "AAA": [("2026-09-17", 11.0), ("2026-09-18", 12.0)],  # 首根不是信号日 → 锚点缺失
            "BBB": [("2026-09-17", 10.0), ("2026-09-18", 11.0)],  # T+1 → +10%
        }
    )
    out = dl.backfill_outcomes(eng, limit=1, series_provider=prov)

    assert out["filled"] == 1, f"可填的行必须被填上(修前这里是 0): {out}"
    assert out["scanned"] == 2, "查看预算与 limit 解耦: 不可填的行不算进 limit"
    assert out["no_anchor"] == 1 and out["blocked"] == 0

    a = _row(eng, "AAA")
    assert a[0] is None and a[1] is None and a[2] is None, "不可填的档必须保持 NULL, 不许填 0"
    assert a[3] == NO_ANCHOR
    assert a[4] and "无 K 线" in a[4], "必须留下人类可读原因"

    b = _row(eng, "BBB")
    assert b[0] == pytest.approx(0.1) and b[3] is None
    assert b[4] is None, "填上的行不该留缺数据标注"


def test_nothing_is_filled_as_zero():
    """全域红线: 没有一根可用的未来 K 线 → 收益列一律 NULL(0 是真实收益, 不能冒充缺数据)。"""
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16", price=10.0)
    prov = _provider({"AAA": [("2026-09-17", 10.0)]})  # 缺信号日那根
    dl.backfill_outcomes(eng, series_provider=prov)
    a = _row(eng, "AAA")
    assert a[0] is None and a[1] is None and a[2] is None
    assert a[0] != 0.0 and a[1] != 0.0 and a[2] != 0.0


def test_scan_budget_decoupled_from_fill_budget():
    """600 行永久不可填挡在最前, 后面 1 行可填 → 仍必须填到(limit 只有 5)。"""
    eng = _engine()
    prov_bars: dict[str, list[tuple[str, float]]] = {}
    for i in range(600):
        sym = f"U{i:03d}"
        _insert(eng, symbol=sym, trade_date="2026-09-16", price=10.0)
        prov_bars[sym] = [("2026-09-17", 11.0)]  # 缺信号日那根 → 锚点缺失
    _insert(eng, symbol="BBB", trade_date="2026-09-17", price=10.0)
    prov_bars["BBB"] = [("2026-09-17", 10.0), ("2026-09-18", 11.0)]

    out = dl.backfill_outcomes(eng, limit=5, series_provider=_provider(prov_bars))
    assert out["filled"] == 1, f"扫过头部的不可填行后必须填到 BBB: {out}"
    assert out["scanned"] == 601, "查看预算默认 5000, 不该被 limit=5 卡住"
    assert out["no_anchor"] == 600
    assert _row(eng, "BBB")[0] == pytest.approx(0.1)


def test_default_scan_cap_keeps_margin_over_limit(monkeypatch):
    """缺省查看额度 = max(2*limit, DEFAULT_SCAN_CAP): 头部堆满不可填行时仍要够得到可填的。"""
    monkeypatch.setattr(dl, "DEFAULT_SCAN_CAP", 2)
    eng = _engine()
    bars: dict[str, list[tuple[str, float]]] = {}
    for i in range(5):  # 5 行挡在最前(空序列: 只会 blocked, 不会贴永久标注)
        sym = f"U{i}"
        _insert(eng, symbol=sym, trade_date="2026-09-16", price=10.0)
        bars[sym] = []
    _insert(eng, symbol="BBB", trade_date="2026-09-17", price=10.0)
    bars["BBB"] = [("2026-09-17", 10.0), ("2026-09-18", 11.0)]

    out = dl.backfill_outcomes(eng, limit=3, series_provider=_provider(bars))
    assert out["scanned"] == 6, "缺省额度 = max(2*limit, DEFAULT_SCAN_CAP) = 6"
    assert out["filled"] == 1, f"这一额度必须够到第 6 条(可填的那条): {out}"
    assert out["blocked"] == 5


def test_scan_cap_is_honored():
    """显式 scan_cap 仍要生效(保护单跑时长): 只看 3 条 → 够不到第 4 条可填行。"""
    eng = _engine()
    prov_bars: dict[str, list[tuple[str, float]]] = {}
    for i in range(3):
        sym = f"U{i:03d}"
        _insert(eng, symbol=sym, trade_date="2026-09-16", price=10.0)
        prov_bars[sym] = [("2026-09-17", 11.0)]
    _insert(eng, symbol="BBB", trade_date="2026-09-17", price=10.0)
    prov_bars["BBB"] = [("2026-09-17", 10.0), ("2026-09-18", 11.0)]

    out = dl.backfill_outcomes(eng, limit=50, scan_cap=3, series_provider=_provider(prov_bars))
    assert out["scanned"] == 3 and out["filled"] == 0
    assert _row(eng, "BBB")[0] is None, "没被看到就不该被填"


def test_holiday_20260925_production_shape_marked_not_filled():
    """生产形态: 中秋休市 20260925 落了 597 条信号, klines 全表无该日 → 显式标注不填 0。"""
    eng = _engine()
    _insert(eng, symbol="600000", trade_date="20260925", price=10.0)  # 紧凑格式(账本真形态)
    prov = _provider({"600000": [("2026-09-28", 10.5), ("2026-09-29", 10.6)]})
    out = dl.backfill_outcomes(eng, series_provider=prov)
    assert out["filled"] == 0 and out["no_anchor"] == 1
    r = _row(eng, "600000")
    assert r[0] is None and r[1] is None and r[2] is None
    assert r[3] == NO_ANCHOR and "2026-09-25" in r[4]


def test_no_anchor_marker_cleared_when_kline_arrives():
    """标注不是永久封印: K 线后续补齐(重跑) → 自动重新评估、清空标注并回填。"""
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16", price=10.0)

    # 第一跑: 信号日那根还没到(后面却有) → 标注
    out1 = dl.backfill_outcomes(eng, series_provider=_provider({"AAA": [("2026-09-17", 11.0)]}))
    assert out1["no_anchor"] == 1 and _row(eng, "AAA")[3] == NO_ANCHOR

    # 第二跑: 信号日那根补齐 → 填上并清标注(T+1 = 09-17 → +10%)
    out2 = dl.backfill_outcomes(
        eng, series_provider=_provider({"AAA": [("2026-09-16", 10.0), ("2026-09-17", 11.0)]})
    )
    assert out2["filled"] == 1 and out2["no_anchor"] == 0
    r = _row(eng, "AAA")
    assert r[0] == pytest.approx(0.1) and r[3] is None and r[4] is None


def test_empty_series_is_not_marked_permanent():
    """该标的从信号日起一根 K 线都没有(可能只是还没落库) → 只记 blocked, 不下永久结论。"""
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16", price=10.0)
    out = dl.backfill_outcomes(eng, series_provider=_provider({}))
    assert out == {"scanned": 1, "filled": 0, "no_anchor": 0, "blocked": 1, "no_base": 0}
    assert _row(eng, "AAA")[3] is None, "没有后续 K 线时不许贴『永久不可填』的标注"


def test_no_base_price_counted_not_filled():
    """当时没留价 → 显式计数(no_base), 不拿今天的价冒充当时。"""
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16", price=None)
    out = dl.backfill_outcomes(
        eng, series_provider=_provider({"AAA": [("2026-09-16", 10.0), ("2026-09-17", 11.0)]})
    )
    assert out == {"scanned": 1, "filled": 0, "no_anchor": 0, "blocked": 0, "no_base": 1}
    assert _row(eng, "AAA")[0] is None


def test_limit_caps_fills_and_leaves_rest_pending():
    """limit 仍是"单跑最多填多少": 10 行可填 + limit=3 → 只填 3 行。"""
    eng = _engine()
    bars: dict[str, list[tuple[str, float]]] = {}
    for i in range(10):
        sym = f"F{i}"
        _insert(eng, symbol=sym, trade_date="2026-09-16", price=10.0)
        bars[sym] = [("2026-09-16", 10.0), ("2026-09-17", 11.0)]
    out = dl.backfill_outcomes(eng, limit=3, series_provider=_provider(bars))
    assert out["filled"] == 3
    with eng.connect() as conn:
        n = conn.execute(
            sa.text("SELECT COUNT(*) FROM decision_log WHERE ret_t1 IS NOT NULL")
        ).scalar()
    assert int(n) == 3


# ─────────────────── ③ 老库降级 / 幂等 ───────────────────


def test_legacy_engine_without_state_column_degrades():
    """未跑 v197 的老库(无 outcome_state 列): 照常回填, 不写标注, 绝不因缺列炸掉。"""
    eng = _engine(with_state=False)
    assert dl._has_outcome_state_column(eng) is False
    _insert(eng, symbol="AAA", trade_date="2026-09-16", price=10.0)  # 不可填
    _insert(eng, symbol="BBB", trade_date="2026-09-17", price=10.0)  # 可填
    out = dl.backfill_outcomes(
        eng,
        limit=1,
        series_provider=_provider(
            {
                "AAA": [("2026-09-17", 11.0)],
                "BBB": [("2026-09-17", 10.0), ("2026-09-18", 11.0)],
            }
        ),
    )
    assert out["filled"] == 1, "缺标注列也必须能填(降级路径)"
    assert out["no_anchor"] == 0 and out["blocked"] == 1
    with eng.connect() as conn:
        row = conn.execute(
            sa.text("SELECT ret_t1 FROM decision_log WHERE symbol='AAA'")
        ).fetchone()
    assert row[0] is None, "老库路径同样不许填 0"


def test_idempotent_repeat_no_double_count_with_new_counters():
    """重跑不双计, 且新增计数器在空跑时为 0(返回体形状锁死)。"""
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16", price=10.0)
    prov = _provider(
        {
            "AAA": [
                ("2026-09-16", 10.0),
                ("2026-09-17", 11.0),
                ("2026-09-18", 10.0),
                ("2026-09-21", 9.0),
                ("2026-09-22", 10.0),
                ("2026-09-23", 12.0),
            ]
        }
    )
    first = dl.backfill_outcomes(eng, series_provider=prov)
    assert first == {"scanned": 1, "filled": 1, "no_anchor": 0, "blocked": 0, "no_base": 0}
    second = dl.backfill_outcomes(eng, series_provider=prov)
    assert second == {"scanned": 0, "filled": 0, "no_anchor": 0, "blocked": 0, "no_base": 0}


# ─────────────────── ④ 缺数据在读数面上显式可见 ───────────────────


def test_stats_reports_no_anchor_annotation():
    """分桶口径: 缺 K 锚点的行不进命中分母, 但要在 no_data 里显式报出来。"""
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16", price=10.0)  # 可填
    _insert(eng, symbol="BBB", trade_date="2026-09-16", price=10.0)  # 不可填
    dl.backfill_outcomes(
        eng,
        series_provider=_provider(
            {
                "AAA": [("2026-09-16", 10.0), ("2026-09-17", 11.0)],
                "BBB": [("2026-09-17", 11.0)],
            }
        ),
    )
    st = dl.stats(eng, min_sample=1)
    row = st["rows"][0]
    assert row["n_total"] == 2
    assert row["horizons"]["t1"]["n"] == 1, "缺锚点的行不许进分母"
    assert row["horizons"]["t1"]["hit_rate"] == 1.0
    assert st["no_data"]["state"] == NO_ANCHOR
    assert st["no_data"]["rows_by_kind"] == {"resonance3": 1}
    assert "不进命中分母" in st["no_data"]["note"]


def test_stats_no_data_empty_when_column_absent():
    """老库(无标注列) → no_data 口径显式为空, 不假装有数。"""
    eng = _engine(with_state=False)
    _insert(eng, symbol="AAA", trade_date="2026-09-16", price=10.0)
    st = dl.stats(eng, min_sample=1)
    assert st["no_data"]["rows_by_kind"] == {}


def test_query_log_exposes_outcome_state():
    """明细读数面: outcome_state/outcome_note 直出, 且未回填档位仍是 None(不是 0)。"""
    eng = _engine()
    _insert(eng, symbol="AAA", trade_date="2026-09-16", price=10.0)
    dl.backfill_outcomes(eng, series_provider=_provider({"AAA": [("2026-09-17", 11.0)]}))

    page = dl.query_log(eng)
    assert page["total"] == 1
    it = page["items"][0]
    assert it["outcome_state"] == NO_ANCHOR
    assert it["outcome_note"] and "无 K 线" in it["outcome_note"]
    assert it["outcomes"]["t1"]["ret"] is None
    assert it["outcomes"]["t1"]["hit"] is None
    assert "no_anchor" in page["note"]


# ─────────────────── ⑤ 调度: 单跑目标够清积压 ───────────────────


def test_daily_backfill_limit_covers_backlog(monkeypatch):
    """定时 job 的单跑目标必须能清掉生产积压(3000+), 旧的 500/天 追不平。"""
    from src.core import trading_calendar as tc

    assert dl.BACKFILL_DAILY_LIMIT >= 3000
    monkeypatch.setattr(tc, "is_trading_day", lambda d: True)
    captured: dict = {}
    monkeypatch.setattr(
        dl, "spawn_backfill",
        lambda **k: captured.update(k) or {"started": True, "job_id": "j1"},
    )
    out = dl.backfill_daily_job()
    assert out["started"] is True and out["job_id"] == "j1"
    assert captured["limit"] == dl.BACKFILL_DAILY_LIMIT
    assert captured["reason"] == "scheduled"


def test_spawn_and_runner_pass_scan_cap_through(monkeypatch):
    """scan_cap 必须一路传到 backfill_outcomes(手动口子与定时口子同一套语义)。"""
    seen: dict = {}
    monkeypatch.setattr(
        dl, "backfill_outcomes",
        lambda eng, **k: seen.update(k) or {"scanned": 0, "filled": 0},
    )
    dl.backfill_runner("job-x", engine=object(), limit=7, scan_cap=99)
    assert seen["limit"] == 7 and seen["scan_cap"] == 99
