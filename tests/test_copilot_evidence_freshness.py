# -*- coding: utf-8 -*-
"""市场参谋证据时效硬校验（2026-11-10）。

用户现场：11-05 的证据被当 11-10 的今日盘面。本文件钉住四条硬约束：

1. 时间锚 = **北京时间**（不是宿主墙钟/UTC），由代码算出来写进 system，
   不是靠提示词叮嘱；
2. 历史信号可以在场，但**必须写真实基准日 + 过期说明**，不得冒充「今日」，
   也不得被静默删掉（历史对比仍有价值）；
3. `market.sentiment` 信封的「时点」只能是**数据真实基准日**或「未知」，
   绝不允许写调用时刻；
4. 机会候选头「今日」只在基准日 == 今天时出现。

另覆盖：无交易日历（不猜、不谎称今日）、周末（写最近交易日）、
以及每条注入路径**各自独立**判定时效（不靠上游某一条）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import signal_summary as ss
from src.core.market_breadth import BreadthBar

CST_TODAY = datetime(2026, 11, 10, 14, 40)   # 周二，交易日
STALE_DAY = "2026-11-05"                      # 上周四
LEGACY_TEXT = (
    f"--- 今日系统信号摘要({STALE_DAY}) ---\n"
    "情绪周期：发酵(置信度中)。涨停58家/连板5板/炸板率18%\n"
    f"📊 市场广度·情绪温度（{STALE_DAY}）\n"
    "指数资金：上证+0.42%、深证+0.31%；主力净流入+18亿"
)


def _run(coro):
    return asyncio.run(coro)


# ──────────────────────── ① 时效判定核心 ────────────────────────

def test_today_data_is_today_only_when_dates_match():
    f = ss.resolve_freshness("2026-11-10", now=CST_TODAY)
    assert f["base_date"] == "2026-11-10"
    assert f["today"] == "2026-11-10"
    assert f["expected"] == "2026-11-10"
    assert f["is_today"] is True
    assert f["is_current"] is True
    assert f["stale"] is False
    assert f["lag_trading_days"] == 0
    assert "今日" in f["label"]


def test_stale_data_is_labelled_historical_and_counted_in_trading_days():
    f = ss.resolve_freshness(STALE_DAY, now=CST_TODAY)
    assert f["is_today"] is False
    assert f["is_current"] is False
    assert f["stale"] is True
    assert f["expected"] == "2026-11-10"
    # 2026-11-10 时段修复：14:40 盘中，11-10 尚未收盘，缺的是 11-06 / 11-09 两个已收盘交易日
    assert f["lag_trading_days"] == 2
    # 收盘后（同一基准日）才算落后 3 个交易日
    assert ss.resolve_freshness(STALE_DAY, now=datetime(2026, 11, 10, 16, 10))["lag_trading_days"] == 3
    assert "历史" in f["label"]
    # 盘中「最近已收盘交易日」是 11-09（前日），不是今天 11-10
    assert STALE_DAY in f["note"] and "2026-11-09" in f["note"]
    # 允许作背景，禁止冒充今日
    assert "不得" in f["note"]


def test_weekend_labels_latest_trading_day_not_today():
    sunday = datetime(2026, 11, 8, 10, 0)
    f = ss.resolve_freshness("2026-11-06", now=sunday)
    assert f["today"] == "2026-11-08"
    assert f["expected"] == "2026-11-06"
    assert f["is_today"] is False      # 周五数据不是周日
    assert f["is_current"] is True     # 但它就是最近交易日
    assert f["stale"] is False
    assert "最近交易日" in f["label"]


def test_calendar_unavailable_never_claims_today(monkeypatch):
    import src.core.ladder_freshness as lf

    def _boom(now=None):
        raise RuntimeError("交易日历未覆盖 2031 年")

    monkeypatch.setattr(lf, "expected_latest_trading_date", _boom)
    f = ss.resolve_freshness("2031-01-06", now=datetime(2031, 1, 6, 10, 0))
    assert f["calendar"] == "unavailable"
    assert f["expected"] is None
    assert f["is_current"] is None
    assert f["stale"] is None
    assert "无法判定" in f["note"]
    assert "不得当作今日" in f["note"]


def test_missing_base_date_is_unknown_not_today():
    f = ss.resolve_freshness(None, now=CST_TODAY)
    assert f["base_date"] is None
    assert f["is_today"] is None
    assert "未知" in f["label"] or "未标注" in f["note"]


# ──────────────────────── ② 时间锚（system） ────────────────────────

def test_time_anchor_block_is_beijing_clock_with_calendar():
    block = ss.time_anchor_block(now=CST_TODAY)
    assert "2026-11-10 14:40" in block
    assert "周二" in block
    assert "北京时间" in block


def test_time_anchor_block_weekend_says_closed():
    block = ss.time_anchor_block(now=datetime(2026, 11, 8, 10, 0))
    assert "2026-11-08" in block
    assert "休市" in block


def test_time_anchor_block_no_calendar_is_honest(monkeypatch):
    import src.core.ladder_freshness as lf

    def _boom(now=None):
        raise RuntimeError("no calendar")

    monkeypatch.setattr(lf, "expected_latest_trading_date", _boom)
    block = ss.time_anchor_block(now=CST_TODAY)
    assert "2026-11-10" in block
    assert "不可用" in block


# ──────────────────────── ③ signal_summary 注入 ────────────────────────

def test_render_uses_real_snapshot_date_not_wall_clock():
    text = ss.render_signal_summary(
        {"sentiment": {"content": "情绪周期：发酵", "base_date": STALE_DAY}},
        snapshot_date=STALE_DAY,
        now=CST_TODAY,
    )
    head = text.splitlines()[0]
    assert "系统信号摘要" in head and f"生成日 {STALE_DAY}" in head
    assert "今日系统信号摘要" not in head
    assert f"基准日 {STALE_DAY}" in text and "落后 2 个交易日" in text
    assert "仅背景" in text and "不得作为今日结论依据" in text


def test_render_fresh_snapshot_marks_today():
    """今日结论只能来自「块自带今日基准日 + 已收盘」的代码核验。"""
    blocks = {
        "sentiment": {"content": "情绪周期：发酵", "base_date": "2026-11-10"},
        "mainline": {"content": "市场主线：机器人", "base_date": "2026-11-10"},
        "market_scan": {"content": "三榜：新G点[A]", "base_date": "2026-11-10"},
        "limit_up": {"content": "涨停复盘：42家", "base_date": "2026-11-10"},
        "indices_flow": {"content": "指数资金：上证+0.4%", "base_date": "2026-11-10"},
    }
    closed = ss.render_signal_summary(
        blocks, snapshot_date="2026-11-10", now=datetime(2026, 11, 10, 16, 10)
    )
    assert "基准日 2026-11-10" in closed
    assert "可用于今日结论" in closed

    intraday = ss.render_signal_summary(blocks, snapshot_date="2026-11-10", now=CST_TODAY)
    assert "可用于今日结论" not in intraday
    assert "今日盘中未收盘" in intraday


def _sqlite_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from src.db.models import Base

    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _seed_summary(db, snapshot_date: str, text: str):
    from src.db.models import SignalSummaryDaily

    db.add(
        SignalSummaryDaily(
            snapshot_date=snapshot_date, stock_market="CN", blocks={}, text=text
        )
    )
    db.commit()


def test_read_latest_legacy_text_bare_date_is_unknown_not_today():
    """老 text 快照的裸「（2026-11-05）」是参考日期，不是数据日期标签。"""
    db = _sqlite_db()
    _seed_summary(db, STALE_DAY, LEGACY_TEXT)
    out = ss.read_latest_summary_text(db, now=CST_TODAY)
    head = out.splitlines()[0]
    assert "今日系统信号摘要" not in out
    # 2026-11-10 时效终审：裸日期不再当基准日；有读数没标日期 → 整体未知
    assert "基准日 未知" in head
    assert "可用于今日结论" not in out
    assert "已核验" not in out
    # 历史数据仍在（不静默删除），只是不许冒充今日
    assert "涨停58家" in out


def test_read_latest_legacy_text_all_labelled_is_background_only():
    """老 text 每条都带显式数据日期 → 可判「历史/落后」，但纯文本永远不算已核验。"""
    text = (
        f"--- 今日系统信号摘要({STALE_DAY}) ---\n"
        f"情绪周期：发酵（数据日期 {STALE_DAY}）\n"
        f"指数资金：上证+0.42%（数据日期 {STALE_DAY}）"
    )
    db = _sqlite_db()
    _seed_summary(db, STALE_DAY, text)
    out = ss.read_latest_summary_text(db, now=CST_TODAY)
    assert f"基准日 {STALE_DAY}" in out.splitlines()[0]
    assert "历史" in out and "落后" in out
    assert "可用于今日结论" not in out
    assert "逐块" in out or "纯文本" in out


def test_read_latest_legacy_text_labelled_today_still_not_verified():
    """即使每条都标「今天」，纯文本老快照也没有逐块结构化基准日 → 不许宣称已核验。"""
    text = (
        "--- 今日系统信号摘要(2026-11-10) ---\n"
        "情绪周期：发酵（数据日期 2026-11-10）\n"
        "指数资金：上证+0.42%（数据日期 2026-11-10）"
    )
    db = _sqlite_db()
    _seed_summary(db, "2026-11-10", text)
    out = ss.read_latest_summary_text(db, now=datetime(2026, 11, 10, 16, 10))
    assert "可用于今日结论" not in out
    assert "代码核验通过" not in out
    assert "不得声称已核验" in out



def test_read_latest_fresh_snapshot_keeps_today_but_no_duplicate_header():
    db = _sqlite_db()
    _seed_summary(db, "2026-11-10", "--- 今日系统信号摘要(2026-11-10) ---\n情绪周期：冰点")
    out = ss.read_latest_summary_text(db, now=CST_TODAY)
    assert out.count("系统信号摘要") == 1
    assert "今日系统信号摘要" not in out
    assert "情绪周期：冰点" in out


def test_read_latest_no_row_is_none():
    db = _sqlite_db()
    assert ss.read_latest_summary_text(db, now=CST_TODAY) is None


# ──────────────────────── ④ 市场广度块 ────────────────────────

def _bars(last_day: str, n: int = 30, symbols: int = 4000):
    from datetime import date, timedelta

    days: list[str] = []
    d = date.fromisoformat(last_day)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= timedelta(days=1)
    days.reverse()
    return [
        BreadthBar(
            trade_date=day,
            up=symbols // 2 + i,
            down=symbols // 2,
            flat=10,
            up_volume=1.0e9,
            down_volume=8.0e8,
        )
        for i, day in enumerate(days)
    ]


def test_breadth_stale_flags_expected_and_render_text_warns(monkeypatch):
    from src.collectors import market_breadth_series as M

    monkeypatch.setattr(M, "_load_stored_bars", lambda db, days=M.DEFAULT_DAYS: _bars(STALE_DAY))
    out = M.latest_with_percentile(None, now=CST_TODAY)
    assert out["trade_date"] == STALE_DAY
    assert out["expected"] == "2026-11-10"
    assert out["stale"] is True
    assert out["lag_trading_days"] == 2   # 盘中：只数 11-06 / 11-09
    txt = M.render_text(out)
    assert "时效" in txt and "历史" in txt
    assert "今日读数" not in txt or "非今日" in txt


def test_breadth_weekend_is_current_not_stale(monkeypatch):
    from src.collectors import market_breadth_series as M

    monkeypatch.setattr(
        M, "_load_stored_bars", lambda db, days=M.DEFAULT_DAYS: _bars("2026-11-06")
    )
    out = M.latest_with_percentile(None, now=datetime(2026, 11, 8, 10, 0))
    assert out["is_today"] is False
    assert out["stale"] is False
    assert out["expected"] == "2026-11-06"


# ──────────────────────── ⑤ market.sentiment 信封时点 ────────────────────────
# 2026-11-10 时效审计：只认**显式标注**的数据日期（数据日期/统计日期/数据截至），
# 正文里任意参考日期（「对比 2026-10-20」）不许当基准日；全部用例冻结在 11-10 14:40。

def _freeze(monkeypatch, when: datetime = CST_TODAY):
    monkeypatch.setattr(ss, "now_cn", lambda now=None: now if now is not None else when.replace(tzinfo=ss._CST))


def _patch_delegate(monkeypatch, payload: str):
    import src.agents.chat.tools_catalog as tc

    async def _fake(name, db, args, user):
        return payload

    monkeypatch.setattr(tc, "_delegate", _fake)
    return tc


def test_sentiment_envelope_asof_is_data_date_not_clock(monkeypatch):
    """分条日期不一致（情绪周期未标）→ 时点未知，但该条自己的日期与历史判定如实保留。"""
    _freeze(monkeypatch)
    payload = f"短线情绪周期: 发酵\n\n📊 市场广度·情绪温度（数据日期 {STALE_DAY}）\n· 涨跌家数：上涨 3000"
    tc = _patch_delegate(monkeypatch, payload)
    out = _run(tc._query_market_sentiment(None, {}, None))
    head = out.splitlines()[0]
    assert "market.sentiment" in head
    assert "时点 未知" in head                      # 一个分条的日期不许给整封盖戳
    assert "14:40" not in head
    assert f"本分条数据日期 {STALE_DAY}" in out
    assert f"{STALE_DAY}（历史）" in out             # 该条自己的时效仍如实给出
    assert "可用于今日结论" not in out



def test_sentiment_envelope_asof_unknown_when_no_evidence(monkeypatch):
    _freeze(monkeypatch)
    tc = _patch_delegate(monkeypatch, "")
    out = _run(tc._query_market_sentiment(None, {}, None))
    head = out.splitlines()[0]
    assert "时点 未知" in head
    assert "未取到" in out


def test_sentiment_envelope_fresh_data_has_no_stale_note(monkeypatch):
    """一条今日已收盘、另一条没标日期 → 整体仍未知，不许因「今天」就免掉时效说明。"""
    _freeze(monkeypatch, datetime(2026, 11, 10, 16, 10))   # 已收盘
    payload = "短线情绪周期: 发酵\n\n📊 市场广度·情绪温度（数据日期 2026-11-10）\n· 涨跌家数：上涨 3000"
    tc = _patch_delegate(monkeypatch, payload)
    out = _run(tc._query_market_sentiment(None, {}, None))
    assert "时点 未知" in out.splitlines()[0]
    assert "整体基准日未知" in out
    assert "可用于今日结论" not in out


def test_sentiment_all_items_labelled_same_day_stays_dated(monkeypatch):
    """所有分条都标了同一个显式日期 → 时点成立（今日已收盘时也不额外唠叨）。"""
    _freeze(monkeypatch, datetime(2026, 11, 10, 16, 10))
    payload = (
        "短线情绪周期: 发酵（数据日期 2026-11-10）\n\n"
        "📊 市场广度·情绪温度（数据日期 2026-11-10）\n· 涨跌家数：上涨 3000"
    )
    tc = _patch_delegate(monkeypatch, payload)
    out = _run(tc._query_market_sentiment(None, {}, None))
    assert "时点 2026-11-10" in out.splitlines()[0]
    assert "\n时效：" not in out and not out.startswith("时效：")



def test_sentiment_domain_route_uses_data_date(monkeypatch):
    """走公开入口 query_data_domain，同一结论（防有人绕过 _query_market_sentiment）。"""
    _freeze(monkeypatch)
    payload = f"📊 市场广度·情绪温度（数据日期 {STALE_DAY}）\n· 涨跌家数：上涨 3000"
    tc = _patch_delegate(monkeypatch, payload)
    out = _run(
        tc.query_data_domain(
            None, {"domain": "market.sentiment", "params_json": "{}"}, None
        )
    )
    head = out.splitlines()[0]
    assert "market.sentiment" in head and f"时点 {STALE_DAY}" in head
    assert "历史" in out


# ──────────────────────── ⑥ 机会候选伪「今日」 ────────────────────────

def _candidate_db(latest: str | None, rows=()):
    """极简 session 替身：只回放 _read_opportunities 需要的聚合查询。"""

    class _Q:
        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def scalar(self):
            return latest

        def count(self):
            return len(rows)

        def all(self):
            return list(rows)

    class _DB:
        def query(self, *a, **k):
            return _Q()

    return _DB()


class _Row:
    stock_symbol = "600111"
    stock_name = "北方稀土"
    score = 88.0
    action_label = "关注"
    signal = "放量突破"
    target_price = 30.5


def test_opportunities_header_marks_stale_snapshot(monkeypatch):
    import src.web.api.chat as chat

    out = chat._read_opportunities(_candidate_db(STALE_DAY, [_Row()]), 10)
    assert "今日机会候选" not in out
    assert f"基准日 {STALE_DAY}" in out
    assert "历史" in out
    assert "600111" in out


def test_opportunities_header_today_only_when_data_is_today(monkeypatch):
    """盘中（14:40）不许叫「今日机会候选」，只有收盘后（16:10）才行。"""
    import src.web.api.chat as chat

    monkeypatch.setattr(chat, "_chat_now_cn", lambda: datetime(2026, 11, 10, 16, 10), raising=False)
    out = chat._read_opportunities(_candidate_db("2026-11-10", [_Row()]), 10)
    assert "今日机会候选" in out

    monkeypatch.setattr(chat, "_chat_now_cn", lambda: CST_TODAY, raising=False)
    intraday = chat._read_opportunities(_candidate_db("2026-11-10", [_Row()]), 10)
    assert "今日机会候选" not in intraday
    assert "今日盘中机会候选" in intraday and "不得作为今日结论" in intraday


def test_opportunities_empty_has_no_pseudo_today():
    import src.web.api.chat as chat

    out = chat._read_opportunities(_candidate_db(None), 10)
    assert "今日" not in out


# ──────────────────────── ⑦ 端到端：system 里两条硬锚都在 ────────────────────────

def test_build_ai_messages_anchors_clock_and_labels_stale_summary():
    import src.web.api.chat as chat
    from src.db.models import ChatConversation

    db = _sqlite_db()
    _seed_summary(db, STALE_DAY, LEGACY_TEXT)
    conv = ChatConversation(user_id=None, title="时效锚", stock_symbol=None, stock_market=None)
    db.add(conv)
    db.commit()

    msgs = _run(chat._build_ai_messages(db, conv, None))
    system = msgs[0]["content"]
    assert "北京时间" in system
    assert "2026-11" in system            # 真实年月
    assert "今日系统信号摘要" not in system
    # 2026-11-10 时效终审：老 text 的裸「（2026-11-05）」是参考日期 → 整体基准日未知，
    # 但证据仍在场，且明确写出「不得当作今日」。
    assert "基准日 未知" in system
    assert "不得当作今日" in system
    assert "涨停58家" in system
    assert "可用于今日结论" not in system
