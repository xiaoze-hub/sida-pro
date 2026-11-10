# -*- coding: utf-8 -*-
"""ETF 读侧「同日全篮子 + 同一分钟集合」口径验收（2026-11-10 复审）。

## 这条口径为什么必须钉死

复现到的反例：**19 个基线交易日每分钟只采到 16/20 只 ETF，今日 20/20 只**。
旧读侧把「≥16 只（80% 闸门）」当成「这一分钟可用」，于是基线均值是 16 只的钱、
今日累计是 20 只的钱 —— 同样的单只成交额被算出 `ratio = 1.25`，判成「偏活跃（active）」。
这是纯粹的覆盖口径错配，不是市场信号。

同类还有两种漏算：
- 基线日少了中间某根分钟（20 只齐备但缺 1 根）→ 该日累计只加到「有行的那几根」，
  基线被算低 → 今日凭空「放量」；
- 今日少了/缺只某根分钟 → 今日累计被算低，却仍拿它和基线比 → 凭空「清淡」。

## 现在的口径

1. **齐备 = 全篮子**：一分钟里 20 只都有非空金额才叫齐备。80% 只用来判「齐备分钟占
   已观测分钟的比例」，不再把 16/20 说成可用。
2. **基线只收同日全篮子**：基线日必须在「本次比较的同一批分钟」上齐备且无缺口；
   否则整日剔除并记原因（`incomplete_basket` / `minute_gap`），绝不进均值。
3. **比较用同一分钟集合累计**：今日和基线都只累加这一批分钟，缺分钟不会单边漏算。
4. **部分数据不给结论**：缺只 / 金额 NULL / 分钟缺口 / 齐备分钟占比不足 → `insufficient`
   且 `conclusive=False`、`ratio_vs_baseline=None`，只报已采集累计与覆盖明细。
5. **真实零值保留**：`amount_wan = 0.0` 是真实值（不是缺失），照常进累计与齐备计数。
6. **盘中实时可用**：盘中最后一根仍在成形的分钟（部分标尚未回报）单列 `in_progress`，
   不计入累计/结论，但不会让整张看板在盘中永远「数据不全」。

全部用**真 SQLite 表 + 真 SQL + 真渲染**（tests/conftest.py 已把 DATA_DIR 指到临时目录）。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from src.agents.chat import tools_catalog as tc
from src.collectors import tq_etf_watch as ew
from src.core import etf_watch_feed as feed
from src.web.database import engine

_CST = ZoneInfo("Asia/Shanghai")
_BASKET = [c for c, _n, _c in ew.ETF_BASKET]
_TODAY = "20261106"          # 2026-11-06 周五，交易日
_NOW_LIVE = datetime(2026, 11, 6, 10, 0, tzinfo=_CST)


@pytest.fixture()
def db():
    """真表夹具：写入 etf_minute_flow / etf_flow_daily，用例前后清空（不碰生产数据）。"""

    def _wipe():
        with engine.begin() as c:
            c.execute(text("DELETE FROM etf_minute_flow"))
            c.execute(text("DELETE FROM etf_flow_daily"))

    _wipe()
    yield engine
    _wipe()


def _runner(eng):
    """真 SQL 执行器：语句取自 `feed.SQL`，参数/IN 展开都真的走一遍。"""

    def run(key, params=None, fmt=None):
        sql = feed.SQL[key].format(**(fmt or {}))
        with eng.connect() as conn:
            rows = conn.execute(text(sql), params or {}).mappings().all()
        return [dict(r) for r in rows]

    return run


def _day(eng, day: str, minutes, codes, wan) -> None:
    rows = [{"d": day, "m": m, "c": c, "a": wan} for m in minutes for c in codes]
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO etf_minute_flow (trade_date, minute, etf_code, amount_wan, volume, source) "
                "VALUES (:d, :m, :c, :a, 0, 'tdx_tq')"
            ),
            rows,
        )


def _history(eng, n: int, minutes, codes, wan, *, end_day: int = 19) -> list[str]:
    """写 n 个历史交易日（10-{end_day-n+1} .. 10-{end_day}），返回日期列表。"""
    days = [f"202610{end_day - n + 1 + i:02d}" for i in range(n)]
    for d in days:
        _day(eng, d, minutes, codes, wan)
    return days


# ── 1. 核心反例：19 日 16/20 只 vs 今日 20/20 只 ──────────────────────────

def test_19_days_of_16_etfs_cannot_report_active(db):
    """19 日每分钟 16/20 只（旧口径「过 80% 闸门」）→ 基线 0 日可用，绝不出 active。

    旧实现这里得到 cum=10.0 / baseline=8.0 / ratio=1.25 → state=active（假活跃）。
    """
    hist = _history(db, 19, ("0930", "0931"), _BASKET[:16], 2_500.0)
    _day(db, _TODAY, ("0930", "0931"), _BASKET, 2_500.0)

    p = feed.intraday_payload(_runner(db), _TODAY, now=_NOW_LIVE)
    t = p["totals"]

    assert p["state"] not in ("active", "surge"), p["state"]
    assert p["state"] == "no_baseline"
    assert t["conclusive"] is False
    assert t["ratio_vs_baseline"] is None, "没有同口径基线就不许出比值"
    assert t["baseline_avg_yi"] is None
    # 19 个基线日全部因「篮子缺只」被剔除，不是「有 19 日样本」
    assert t["baseline_days"] == 0
    assert t["baseline_days_dropped"] == 19
    assert t["baseline_drop_reasons"] == {"incomplete_basket": 19}
    assert t["baseline_days_in_window"] == len(hist) == 19
    # 今日累计照实报（20 只 × 2500 万 × 2 分钟 = 10 亿），只是不给结论
    assert t["cum_yi"] == pytest.approx(10.0, abs=0.01)
    assert t["complete_minutes"] == 2 and t["minutes"] == 2
    assert t["coverage"] == pytest.approx(1.0)
    assert "不给活跃度结论" in p["note"]
    assert "篮子缺只" in p["note"]


def test_16_of_20_baseline_is_not_reported_as_available_sample(db):
    """16/20 的基线日不许被算进「可用样本」，也不许说成「过去 20 个交易日」。"""
    _history(db, 19, ("0930",), _BASKET[:16], 2_500.0)
    _day(db, _TODAY, ("0930",), _BASKET, 2_500.0)

    p = feed.intraday_payload(_runner(db), _TODAY, now=_NOW_LIVE)
    assert p["totals"]["baseline_days"] == 0
    assert "过去 20 个交易日" not in p["note"]
    out = tc.render_etf_flow(
        as_of=_TODAY, source="etf_minute_flow", intraday=p, support={"state": "no_data"},
    )
    assert "0/20 个交易日" in out
    assert "篮子缺只" in out
    assert "偏活跃" not in out and "放量（" not in out


# ── 2. 基线缺分钟不许把均值算低 ─────────────────────────────────────────

def test_baseline_day_with_interior_minute_hole_is_dropped(db):
    """基线日 20 只齐备但缺 0931 → 该日累计会少一根，必须整日剔除（否则凭空 surge）。"""
    _day(db, _TODAY, ("0930", "0931", "0932"), _BASKET, 2_500.0)
    # 10 个基线日只有 0930/0932（中间 0931 缺）→ 旧实现：cum@0932 = 两分钟 = 10 亿
    _history(db, 10, ("0930", "0932"), _BASKET, 2_500.0)

    p = feed.intraday_payload(_runner(db), _TODAY, now=_NOW_LIVE)
    t = p["totals"]
    assert t["baseline_days"] == 0
    assert t["baseline_drop_reasons"] == {"minute_gap": 10}
    assert t["ratio_vs_baseline"] is None
    assert p["state"] not in ("active", "surge")
    assert p["state"] == "no_baseline"


def test_baseline_curve_uses_same_minute_set_and_reports_drops(db):
    """`baseline_curve`：同一分钟集合的累计均值 + 样本数 + 剔除原因。"""
    _day(db, "20261001", ("0930", "0931"), _BASKET, 1_000.0)          # 2 亿/分钟
    _day(db, "20261002", ("0930", "0931"), _BASKET, 1_000.0)
    _day(db, "20261003", ("0930", "0931"), _BASKET[:16], 1_000.0)     # 篮子缺只
    _day(db, "20261004", ("0930",), _BASKET, 1_000.0)                 # 缺 0931

    out = feed.baseline_curve(
        _runner(db), ["20261001", "20261002", "20261003", "20261004"],
        minutes=["0930", "0931"], expected=20,
    )
    assert out["days_used"] == 2
    assert out["days_requested"] == 4
    assert out["days_dropped"] == 2
    assert out["drop_reasons"] == {"incomplete_basket": 1, "minute_gap": 1}
    assert out["minutes_compared"] == 2
    assert out["days"] == {"0930": 2, "0931": 2}
    assert out["cum"]["0930"] == pytest.approx(2.0, abs=0.01)
    assert out["cum"]["0931"] == pytest.approx(4.0, abs=0.01)


# ── 3. 今日自己缺分钟/缺只 → 只能无结论 + 覆盖明细 ──────────────────────

def test_today_minute_hole_withholds_conclusion(db):
    """今日 0930/0932 有、0931 缺：累计被少算一根，只能报覆盖、不许给结论。"""
    _day(db, "20261105", ("0930", "0932"), _BASKET, 2_500.0)
    _history(db, 5, ("0930", "0932"), _BASKET, 2_500.0)

    p = feed.intraday_payload(_runner(db), "20261105", now=_NOW_LIVE)
    t = p["totals"]
    assert p["state"] == "insufficient"
    assert t["conclusive"] is False
    assert t["ratio_vs_baseline"] is None
    assert t["minute_gaps"] == 1
    assert t["minute_gaps_detail"] == ["0931"]
    assert "minute_gaps" in t["withheld_reasons"]
    assert t["cum_yi"] == pytest.approx(10.0, abs=0.01), "已知金额照实累计，但不给结论"
    assert "分钟缺口" in p["note"]


def test_today_short_basket_minute_withholds_conclusion(db):
    """今日有一分钟只有 16/20 只 → 该分钟合计是残缺的，不给结论（不补 0、不无视）。"""
    _day(db, "20261105", ("0930",), _BASKET, 2_500.0)
    _day(db, "20261105", ("0931",), _BASKET[:16], 2_500.0)
    _history(db, 5, ("0930", "0931"), _BASKET, 2_500.0)

    p = feed.intraday_payload(_runner(db), "20261105", now=_NOW_LIVE)
    t = p["totals"]
    assert p["state"] == "insufficient"
    assert t["conclusive"] is False and t["ratio_vs_baseline"] is None
    assert t["minutes_short_basket"] == 1
    assert t["coverage"] == pytest.approx(0.5)
    assert t["baseline_days"] == 5, "基线本身可用，只是今日不给结论"
    assert p["series"][1]["complete"] is False


# ── 4. 对照：全篮子齐备时必须照旧给结论（不能因为修 bug 把功能关掉） ─────

def test_full_basket_baseline_and_today_still_conclude(db):
    _history(db, 19, ("0930", "0931"), _BASKET, 2_000.0)   # 基线 4 亿/分钟 → 8 亿
    _day(db, _TODAY, ("0930", "0931"), _BASKET, 2_500.0)   # 今日 5 亿/分钟 → 10 亿

    p = feed.intraday_payload(_runner(db), _TODAY, now=_NOW_LIVE)
    t = p["totals"]
    assert t["conclusive"] is True
    assert t["baseline_days"] == 19
    assert t["baseline_avg_yi"] == pytest.approx(8.0, abs=0.01)
    assert t["ratio_vs_baseline"] == pytest.approx(1.25, abs=0.02)
    assert p["state"] == "active"
    assert t["withheld_reasons"] == []
    assert t["baseline_minutes_compared"] == 2


# ── 5. 真实零值：0.0 是数据，不是缺失 ────────────────────────────────────

def test_real_zero_amount_is_kept_not_treated_as_missing(db):
    """20 只金额都是 0.0 → 齐备、累计 0.00；不得当成 NULL（缺失），也不得判数据不全。"""
    _day(db, "20261105", ("0930",), _BASKET, 0.0)
    _history(db, 3, ("0930",), _BASKET, 0.0)

    p = feed.intraday_payload(_runner(db), "20261105", now=_NOW_LIVE)
    t = p["totals"]
    assert t["cum_yi"] == 0.0, "真实的 0 要显示成 0.00，不能变成 None"
    assert t["minutes_without_amount"] == 0
    assert t["complete_minutes"] == 1
    assert t["conclusive"] is False, "基线也是 0，除不了 → 无基线，不是「常态」"
    assert p["state"] == "no_baseline"
    assert t["baseline_days"] == 3
    assert t["baseline_avg_yi"] is None


# ── 6. 盘中实时可用性：最后一根仍在成形的分钟单列 ────────────────────────

def test_live_in_flight_minute_is_excluded_not_blocking(db):
    """盘中「当前这一分钟」只回报了 12/20 只：单列 in_progress，不影响前 5 根的结论。"""
    _history(db, 19, ("0930", "0931", "0932", "0933", "0934"), _BASKET, 2_000.0)
    _day(db, _TODAY, ("0930", "0931", "0932", "0933", "0934"), _BASKET, 2_500.0)
    _day(db, _TODAY, ("0935",), _BASKET[:12], 2_500.0)

    # 时钟停在 09:35：0935 就是「当前这一分钟」。
    p = feed.intraday_payload(_runner(db), _TODAY, now=datetime(2026, 11, 6, 9, 35, tzinfo=_CST))
    t = p["totals"]
    assert p["in_progress"] and p["in_progress"]["minute"] == "0935"
    assert p["in_progress"]["etfs"] == 12
    assert t["in_progress_minute"] == "0935"
    assert t["minutes"] == 5, "在成形的分钟不进序列/不进累计"
    assert t["last_minute"] == "0934"
    assert t["conclusive"] is True
    assert t["cum_yi"] == pytest.approx(25.0, abs=0.01)
    assert t["ratio_vs_baseline"] == pytest.approx(1.25, abs=0.02)
    assert p["state"] == "active"


def test_stale_partial_minute_is_data_gap_not_in_progress(db):
    """不是「当前这一分钟」的残缺分钟 → 真的数据残缺，照旧不给结论。"""
    _history(db, 19, ("0930", "0931"), _BASKET, 2_000.0)
    _day(db, _TODAY, ("0930",), _BASKET, 2_500.0)
    _day(db, _TODAY, ("0931",), _BASKET[:12], 2_500.0)

    p = feed.intraday_payload(_runner(db), _TODAY, now=_NOW_LIVE)   # 10:00，0931 早就该走完
    assert p["in_progress"] is None
    assert p["state"] == "insufficient"
    assert p["totals"]["ratio_vs_baseline"] is None


def test_live_in_flight_minute_does_not_hide_earlier_holes(db):
    """「当前这一分钟在成形」不是免罪符：前面真有缺口时照样不给结论。"""
    _history(db, 5, ("0930", "0931", "0932"), _BASKET, 2_000.0)
    _day(db, _TODAY, ("0930",), _BASKET, 2_500.0)
    _day(db, _TODAY, ("0932",), _BASKET, 2_500.0)      # 0931 缺
    _day(db, _TODAY, ("0933",), _BASKET[:12], 2_500.0)  # 当前这一分钟在成形

    p = feed.intraday_payload(_runner(db), _TODAY, now=datetime(2026, 11, 6, 9, 33, tzinfo=_CST))
    assert p["in_progress"]["minute"] == "0933"
    assert p["state"] == "insufficient"
    assert p["totals"]["ratio_vs_baseline"] is None
    assert p["totals"]["minute_gaps_detail"] == ["0931"]


def test_http_intraday_surface_never_reports_active(db, monkeypatch):
    """产品面（`/api/etf-watch/intraday` 的处理函数）同样不许出 active。

    读侧口径错了不会抛异常，页面/接口只会安静地报一个假「偏活跃」；所以这条从 API
    入口再验一次，防止有人只在 feed 层修了、HTTP 层又拼一个状态回去。
    """
    from src.web.api import etf_watch as api

    _history(db, 19, ("0930", "0931"), _BASKET[:16], 2_500.0)
    _day(db, _TODAY, ("0930", "0931"), _BASKET, 2_500.0)
    monkeypatch.setattr(api, "_now_cn", lambda: _NOW_LIVE)
    monkeypatch.setattr(api, "_index_points", lambda target: [])

    body = api.get_intraday(date_str=_TODAY)
    assert body["state"] == "no_baseline"
    assert body["state"] not in ("active", "surge")
    assert body["totals"]["ratio_vs_baseline"] is None
    assert body["totals"]["baseline_days"] == 0
    assert body["totals"]["cum_yi"] == pytest.approx(10.0, abs=0.01)


# ── 7. 页面/参谋文案：只有全篮子同日样本 + 同一分钟集合才算基线 ──────────

def test_agent_render_states_caliber_and_no_conclusion(db):
    _history(db, 19, ("0930",), _BASKET[:16], 2_500.0)
    _day(db, _TODAY, ("0930",), _BASKET, 2_500.0)
    p = feed.intraday_payload(_runner(db), _TODAY, now=_NOW_LIVE)

    out = tc.render_etf_flow(
        as_of=_TODAY, source="etf_minute_flow", intraday=p, support={"state": "no_data"},
    )
    assert "同一分钟" in out
    assert "20/20" in out
    assert "≥16/20" not in out, "80% 闸门不许再被当成「可用分钟」出现在出口文案里"
    assert "16/20 只的日不算可用样本" in out, "要显式点名旧口径的 16/20 不算样本"
    assert "偏活跃" not in out
    assert "数据不全" in out or "不给结论" in out


def test_agent_render_reports_complete_basket_coverage(db):
    """齐备分钟/缺只分钟/缺额分钟/缺口都要分别报数，不能只给一个覆盖率。"""
    _day(db, "20261105", ("0930",), _BASKET, 1_000.0)
    _day(db, "20261105", ("0931",), _BASKET[:16], 1_000.0)
    _day(db, "20261105", ("0933",), _BASKET, 1_000.0)   # 0932 缺
    p = feed.intraday_payload(_runner(db), "20261105", now=_NOW_LIVE)

    out = tc.render_etf_flow(
        as_of="20261105", source="x", intraday=p, support={"state": "no_data"},
    )
    assert "缺少 1 根分钟" in out or "分钟缺口" in out
    assert "篮子缺只" in out
    assert "1 个分钟" in out
