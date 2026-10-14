# -*- coding: utf-8 -*-
"""fetch_bars 读库优先回归(2026-10-14)。

AGENTS『K线读取走 PG hypertable 优先(get_klines())』—— 决策先锋/聚宝盆/工作台此前
`fetch_bars` **每次全程联网现拉**。本文件钉死改造后的取数契约(全 mock, **禁真网络**):

  ① PG 命中且含今日 → **零联网**直接返回;
  ② PG 命中缺今日 → **仅最新 bar 走联网**(小窗口 min(days,5)), 取日期晚于库末根的追加;
  ③ 联网无更新 bar → 回库基底(不编造当日);
  ④ PG 空窗/过薄/陈旧(滞后>12天) → 全量联网(改造前原路径);
  ⑤ 非 CN → 不读库直接联网;
  ⑥ 单位口径: 日线 volume=股, 合并后 PG 段 bar 逐字段不被动; 格式 keys 不变;
  ⑦ 读库窗口放大后截取末 days 根(长度语义与联网一致)。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.core import decision_pioneer as dp

_CST = ZoneInfo("Asia/Shanghai")


def _today() -> date:
    return datetime.now(_CST).date()


def _bars(dates: list[str], vol: float = 100_000.0) -> list[dict]:
    return [
        {"date": d, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": vol}
        for d in dates
    ]


def _asc_dates(end: date, n: int) -> list[str]:
    return [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


@pytest.fixture()
def no_network(monkeypatch):
    """把联网通道换成哨兵: 一旦被调用即炸(用于证明零联网)。"""
    called = {"n": 0, "days": []}

    def _net(symbol, market, days):  # noqa: ANN001
        called["n"] += 1
        called["days"].append(days)
        raise AssertionError("联网通道不应被调用")

    monkeypatch.setattr(dp, "_network_bars", _net)
    return called


# ══════════════════════════════════════════════════════════════════════
# ① PG 命中且含今日 → 零联网
# ══════════════════════════════════════════════════════════════════════
def test_pg_hit_with_today_zero_network(no_network, monkeypatch):
    today = _today()
    pg = _bars(_asc_dates(today, 40))
    monkeypatch.setattr(dp, "_pg_daily_bars", lambda s, m, d: pg)

    out = dp.fetch_bars("600519", "CN", days=40)
    assert out == pg
    assert no_network["n"] == 0
    # 格式/字段稳定
    assert set(out[-1].keys()) == {"date", "open", "high", "low", "close", "volume"}
    assert str(out[-1]["date"])[:10] == today.isoformat()


# ══════════════════════════════════════════════════════════════════════
# ② PG 命中缺今日 → 仅最新 bar 走小窗口联网后合并
# ══════════════════════════════════════════════════════════════════════
def test_pg_missing_today_uses_small_live_window(monkeypatch):
    today = _today()
    yesterday = today - timedelta(days=1)
    pg = _bars(_asc_dates(yesterday, 40), vol=111_111.0)
    live = _bars(_asc_dates(today, 3), vol=222_222.0)  # 联网小窗口含今日
    seen = {}

    def _net(symbol, market, days):  # noqa: ANN001
        seen["days"] = days
        return live

    monkeypatch.setattr(dp, "_pg_daily_bars", lambda s, m, d: pg)
    monkeypatch.setattr(dp, "_network_bars", _net)

    out = dp.fetch_bars("600519", "CN", days=40)
    # 只请求小窗口(≤ _LIVE_TAIL_DAYS)
    assert seen["days"] == dp._LIVE_TAIL_DAYS
    # PG 段逐字段保留(PG 段 volume 不动), 仅追加日期 > 库末根的联网 bar
    assert out[: len(pg)] == pg
    assert [b["date"] for b in out[len(pg):]] == [today.isoformat()]
    assert out[-1]["volume"] == 222_222.0   # 追加的是联网 bar
    assert out[0]["volume"] == 111_111.0    # 历史仍是 PG(股)


# ══════════════════════════════════════════════════════════════════════
# ③ 联网无更新 bar → 回库基底, 不编造当日
# ══════════════════════════════════════════════════════════════════════
def test_pg_missing_today_no_new_live_falls_back_to_pg(monkeypatch):
    today = _today()
    yesterday = today - timedelta(days=1)
    pg = _bars(_asc_dates(yesterday, 40))
    monkeypatch.setattr(dp, "_pg_daily_bars", lambda s, m, d: pg)
    # 联网也拿不到比库更新的 bar(非交易日/盘前: 末根仍是昨天)
    monkeypatch.setattr(dp, "_network_bars", lambda s, m, d: _bars(_asc_dates(yesterday, 2)))

    out = dp.fetch_bars("600519", "CN", days=40)
    assert out == pg  # 不加任何 bar, 序列止于库末根(显式缺当日)


# ══════════════════════════════════════════════════════════════════════
# ④ 空窗 / 过薄 / 陈旧 → 全量联网
# ══════════════════════════════════════════════════════════════════════
def test_pg_empty_full_network(monkeypatch):
    net = _bars(_asc_dates(_today(), 60))
    seen = {}

    def _net(symbol, market, days):  # noqa: ANN001
        seen["days"] = days
        return net

    monkeypatch.setattr(dp, "_pg_daily_bars", lambda s, m, d: [])
    monkeypatch.setattr(dp, "_network_bars", _net)

    out = dp.fetch_bars("600519", "CN", days=60)
    assert out == net
    assert seen["days"] == 60  # 全量, 不是小窗口


def test_pg_too_thin_full_network(monkeypatch):
    thin = _bars(_asc_dates(_today(), 10))  # 10 < min(30,60)
    net = _bars(_asc_dates(_today(), 60))
    seen = {}

    def _net(symbol, market, days):  # noqa: ANN001
        seen["days"] = days
        return net

    monkeypatch.setattr(dp, "_pg_daily_bars", lambda s, m, d: thin)
    monkeypatch.setattr(dp, "_network_bars", _net)

    out = dp.fetch_bars("600519", "CN", days=60)
    assert out == net
    assert seen["days"] == 60


def test_pg_stale_full_network(monkeypatch):
    today = _today()
    stale = _bars(_asc_dates(today - timedelta(days=20), 40))  # 滞后 20 天 > 12
    net = _bars(_asc_dates(today, 60))
    seen = {}

    def _net(symbol, market, days):  # noqa: ANN001
        seen["days"] = days
        return net

    monkeypatch.setattr(dp, "_pg_daily_bars", lambda s, m, d: stale)
    monkeypatch.setattr(dp, "_network_bars", _net)

    out = dp.fetch_bars("600519", "CN", days=60)
    assert out == net
    assert seen["days"] == 60


# ══════════════════════════════════════════════════════════════════════
# ⑤ 非 CN → 不读库直接联网
# ══════════════════════════════════════════════════════════════════════
def test_non_cn_skips_pg(monkeypatch):
    # 真 _pg_daily_bars 的 market 守卫: 非 CN 直接 [] (不触 DB)
    assert dp._pg_daily_bars("AAPL", "US", 60) == []

    net = [{"date": "2026-10-13", "open": 1.0, "high": 2.0, "low": 0.5,
            "close": 1.5, "volume": 10.0}]
    seen = {}

    def _net(symbol, market, days):  # noqa: ANN001
        seen["market"] = market
        return net

    monkeypatch.setattr(dp, "_network_bars", _net)
    out = dp.fetch_bars("AAPL", "US", days=60)
    assert out == net and seen["market"] == "US"


# ══════════════════════════════════════════════════════════════════════
# ⑥⑦ 读库长度语义: 窗口放大后截取末 days 根
# ══════════════════════════════════════════════════════════════════════
def test_pg_read_truncates_to_days(monkeypatch):
    import src.db.klines_repo as repo

    wide = _bars(_asc_dates(_today(), 200))
    seen = {}

    def _load(symbol, market, days):  # noqa: ANN001
        seen["lookback"] = days
        return wide

    monkeypatch.setattr(repo, "load_qfq_bars", _load)
    out = dp._pg_daily_bars("600519", "CN", 60)
    assert len(out) == 60                       # 截取末 60 根
    assert out[-1] == wide[-1]
    assert seen["lookback"] > 60                # 读库窗口按自然日放大


# ══════════════════════════════════════════════════════════════════════
# ⑧ 聚宝盆与决策先锋**共享同一读库优先机制**(不搞两套)
# ══════════════════════════════════════════════════════════════════════
def test_jbp_bars_of_reuses_decision_pioneer(monkeypatch):
    from src.core import jbp_pool

    seen = {}

    def _fake_fetch_bars(symbol, market="CN", days=60):  # noqa: ANN001
        seen.update(symbol=symbol, market=market, days=days)
        return [{"date": "2026-10-13", "open": 1.0, "high": 2.0, "low": 0.5,
                 "close": 1.5, "volume": 100.0}]

    monkeypatch.setattr(dp, "fetch_bars", _fake_fetch_bars)
    out = jbp_pool._bars_of("600519")
    assert seen == {"symbol": "600519", "market": "CN", "days": 60}
    assert out and out[0]["volume"] == 100.0
