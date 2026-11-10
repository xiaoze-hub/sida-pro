# -*- coding: utf-8 -*-
"""证据时效审计修复（2026-11-10，第二轮）—— 全部冻结时间，不依赖跑测当天。

审计暴露的 7 类问题，每条都用**真实调用**复现过反例（见文件末尾「反例对照」注释）：

1. 盘中把「最近已收盘交易日」（昨收）写成「历史·落后 1 个交易日」——
   昨收不是旧数据，它是盘中唯一可用的收盘基准，应标「前日」；
2. 盘中（未收盘）的快照写「可用于今日结论」——
   盘中数据会变，只能说「今日盘中，不得作为今日结论」；
3. 摘要拿 `snapshot_date`（生成日）当整份摘要的数据基准日——
   11-05 的三榜 + 11-10 生成 → 标题「基准日 11-10，可用于今日结论」；
4. 「不得仅凭提示词声称硬约束」——块内文字自己写「硬约束/可用于今日结论」不算数，
   结论只由 base_date + 交易日历算；
5. `market.sentiment` 从自由文本抓任意参考日期（「对比 2026-10-20 那次高潮」）当数据日——
   只认显式标注（数据日期/统计日期/数据截至）或未知；
6. 候选基准日里的未来日期要剔除（库内时区错位/预写脏数据），未来快照不参与「最新」选择；
7. 交易日历不可用时不写「今日」（不写 is_today=True、不写【今日机会候选】）。

所有用例都显式传 `now=`，或 monkeypatch `signal_summary.now_cn`，
**不允许**出现「今天跑通、明天挂」的墙钟依赖。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import signal_summary as ss

# ── 冻结时刻（北京） ───────────────────────────────────────────────
INTRADAY = datetime(2026, 11, 10, 14, 40)   # 周二 交易日 盘中未收盘；昨收=11-09(周一)
CLOSED = datetime(2026, 11, 10, 16, 10)     # 周二 已收盘
WEEKEND = datetime(2026, 11, 8, 10, 0)      # 周日 休市；最近交易日=11-06(周五)
TODAY = "2026-11-10"
PREV_CLOSED = "2026-11-09"
STALE_DAY = "2026-11-05"                    # 周四，落后 11-06 / 11-09 两个交易日


def _run(coro):
    return asyncio.run(coro)


def _freeze(monkeypatch, when: datetime):
    """冻结「现在」：所有不传 now 的入口都走这里，杜绝墙钟依赖。"""
    monkeypatch.setattr(ss, "now_cn", lambda now=None: now if now is not None else when.replace(tzinfo=ss._CST))


@pytest.fixture
def no_calendar(monkeypatch):
    """交易日历不可用（年份未覆盖/表缺失）。"""
    import src.core.ladder_freshness as lf

    def _boom(now=None):
        raise RuntimeError("交易日历未覆盖")

    monkeypatch.setattr(lf, "expected_latest_trading_date", _boom)


# ─────────────── ① 盘口时段：不是「开市/休市」二元 ───────────────

def test_market_session_intraday_closed_weekend():
    assert ss.market_session(INTRADAY)["session"] == "intraday"
    assert ss.market_session(INTRADAY)["closed_day"] == PREV_CLOSED  # 昨收，即前日
    assert ss.market_session(CLOSED)["session"] == "closed"
    assert ss.market_session(CLOSED)["closed_day"] == TODAY
    assert ss.market_session(WEEKEND)["session"] == "non_trading"
    assert ss.market_session(WEEKEND)["closed_day"] == "2026-11-06"


def test_market_session_unknown_when_calendar_missing(no_calendar):
    sess = ss.market_session(INTRADAY)
    assert sess["session"] == "unknown"
    assert sess["calendar"] == "unavailable"
    assert sess["closed_day"] is None


# ─────────────── ② 昨收 ≠ 历史；盘中 ≠ 可用于今日结论 ───────────────

def test_prev_closed_day_is_labelled_prev_day_not_history():
    """反例：盘中 14:40，基准日 11-09 曾被判「历史·落后 1 个交易日」。"""
    f = ss.resolve_freshness(PREV_CLOSED, now=INTRADAY)
    assert f["label"] == "前日"
    assert "历史" not in f["label"]
    assert f["is_latest_closed"] is True
    assert f["stale"] is False               # 昨收是最新的已收盘数据，不是过期
    assert f["lag_trading_days"] == 0
    assert f["usable_for_today"] is False
    assert "禁止写成今日" in f["note"]


def test_intraday_snapshot_never_says_usable_for_today():
    """反例：盘中 14:40 的快照曾写「今日，与最近交易日一致，可用于今日结论」。"""
    f = ss.resolve_freshness(TODAY, now=INTRADAY)
    assert f["label"] == "今日盘中"
    assert f["usable_for_today"] is False
    assert "可用于今日结论" not in f["note"]
    assert "尚未收盘" in f["note"]
    assert f["is_today"] is True


def test_closed_session_today_is_usable_and_verified_in_note():
    f = ss.resolve_freshness(TODAY, now=CLOSED)
    assert f["label"] == "今日"
    assert f["usable_for_today"] is True
    assert "可用于今日结论" in f["note"]
    assert "代码核验" in f["note"]


def test_lag_counts_only_closed_sessions_missed():
    """反例：盘中把 11-10 也算成「缺的交易日」→ 11-05 报落后 3 日；实为 2 日。"""
    f = ss.resolve_freshness(STALE_DAY, now=INTRADAY)
    assert f["label"] == "历史"
    assert f["stale"] is True
    assert f["lag_trading_days"] == 2        # 11-06、11-09
    # 收盘后同一个基准日才落后 3 个交易日（含 11-10）
    assert ss.resolve_freshness(STALE_DAY, now=CLOSED)["lag_trading_days"] == 3


def test_weekend_latest_trading_day_not_usable_for_today():
    f = ss.resolve_freshness("2026-11-06", now=WEEKEND)
    assert f["label"] == "最近交易日"
    assert f["stale"] is False
    assert f["usable_for_today"] is False     # 休市日的「最近收盘」不能当今日结论


def test_future_base_date_is_anomaly_and_excluded():
    f = ss.resolve_freshness("2026-12-01", now=INTRADAY)
    assert f["anomaly"] is True
    assert f["usable_for_today"] is False
    assert "未来日期" in f["note"]


def test_holiday_dated_snapshot_is_anomaly_not_today():
    """休市日却出现当天日期的「行情」→ 日期冲突，不许当盘面。"""
    f = ss.resolve_freshness("2026-11-08", now=WEEKEND)
    assert f["anomaly"] is True
    assert f["usable_for_today"] is False


# ─────────────── ③ 日历不可用：不写今日 ───────────────

def test_calendar_unavailable_never_writes_today(no_calendar):
    f = ss.resolve_freshness(TODAY, now=INTRADAY)
    assert f["calendar"] == "unavailable"
    assert f["is_today"] is None              # 不能既不知开市又说「今天是数据日」
    assert f["is_current"] is None
    assert f["usable_for_today"] is False
    assert f["label"].startswith("基准日")     # 只有日期，没有「今日」
    assert "今日" not in f["label"]
    assert "不得写成今日" in f["note"]


def test_time_anchor_intraday_marks_prev_closed_day_as_prev():
    block = ss.time_anchor_block(now=INTRADAY)
    assert "尚未收盘" in block
    assert f"最近已收盘交易日：{PREV_CLOSED}" in block
    assert "前日" in block
    assert "不得作为今日结论依据" in block


def test_time_anchor_closed_says_usable():
    block = ss.time_anchor_block(now=CLOSED)
    assert "已收盘" in block
    assert "可作为今日结论依据" in block


def test_time_anchor_calendar_missing_has_no_today_claim(no_calendar):
    block = ss.time_anchor_block(now=INTRADAY)
    assert "北京日期：" in block
    assert "今日：" not in block               # 日历不可用不写「今日：…」
    assert "可用于今日结论" not in block or "不得声称可用于今日结论" in block
    assert "不可用" in block


# ─────────────── ④ 机会候选：四态 header ───────────────

def test_opportunity_header_states():
    assert ss.opportunity_header(TODAY, now=CLOSED).startswith("【今日机会候选】")
    intraday_head = ss.opportunity_header(TODAY, now=INTRADAY)
    assert intraday_head.startswith("【今日盘中机会候选】")
    assert "不得作为今日结论" in intraday_head
    prev = ss.opportunity_header(PREV_CLOSED, now=INTRADAY)
    assert prev.startswith("【前日机会候选】")
    assert "最近已收盘交易日" in prev
    weekend = ss.opportunity_header("2026-11-06", now=WEEKEND)
    assert weekend.startswith("【最近交易日机会候选】")
    hist = ss.opportunity_header(STALE_DAY, now=INTRADAY)
    assert hist.startswith("【历史机会候选】") and "落后 2 个交易日" in hist
    fut = ss.opportunity_header("2026-12-01", now=INTRADAY)
    assert "基准日异常" in fut and "排除" in fut
    unknown = ss.opportunity_header(None, now=INTRADAY)
    assert "基准日未知" in unknown


def test_opportunity_header_calendar_unavailable_never_today(no_calendar):
    head = ss.opportunity_header(TODAY, now=INTRADAY)
    assert "今日机会候选" not in head
    assert "交易日历不可用" in head


def test_read_opportunities_skips_future_snapshot(monkeypatch):
    """库里有一条 2026-12-01 的未来脏行 + 一条 11-09 的真实行 → 只能用 11-09。"""
    _freeze(monkeypatch, INTRADAY)
    db, session_cls, engine = _sqlite_db()
    try:
        _seed_candidates(db, ["2026-12-01", PREV_CLOSED])
        from src.agents.chat.opportunities_read import read_opportunities

        out = read_opportunities(db, 10, now=INTRADAY)
        assert "前日机会候选" in out
        assert PREV_CLOSED in out
        assert "2026-12-01" not in out
        assert "今日机会候选" not in out
    finally:
        db.close()
        engine.dispose()


def test_read_opportunities_future_only_rows_are_reported_as_such(monkeypatch):
    """库里只有未来脏行 → 说清是「未来日期被排除」，不是「没有候选」。"""
    _freeze(monkeypatch, INTRADAY)
    db, _, engine = _sqlite_db()
    try:
        _seed_candidates(db, ["2026-12-01"])
        from src.agents.chat.opportunities_read import read_opportunities

        out = read_opportunities(db, 10, now=INTRADAY)
        assert "晚于今天" in out and "已排除" in out
        assert "今日机会候选" not in out
        assert "2026-12-01" not in out
    finally:
        db.close()
        engine.dispose()


# ─────────────── ⑤ 摘要：生成日 ≠ 数据基准日，逐块判 ───────────────

ALL_BLOCKS = ("sentiment", "mainline", "market_scan", "limit_up", "indices_flow")


def _blocks(bases: dict, *, claim: str | None = None):
    out = {}
    for k in ALL_BLOCKS:
        b = {"data_status": "ok", "content": f"{k} 内容"}
        if k in bases:
            b["base_date"] = bases[k]
        if claim:
            b["content"] = f"{k} 内容（{claim}）"
        out[k] = b
    return out


def test_summary_generation_day_is_not_the_data_base():
    """反例：11-05 的三榜 + 11-10 生成 → 标题「基准日 11-10，可用于今日结论」。"""
    text = ss.render_signal_summary(
        _blocks({"market_scan": STALE_DAY}), snapshot_date=TODAY, now=INTRADAY
    )
    head = text.splitlines()[0]
    assert "生成日 2026-11-10" in head
    assert "基准日 2026-11-10" not in head       # 生成日不许当基准日写进标题
    assert "可用于今日结论" not in text           # 四块未知 + 一块历史 → 不许声称可用
    assert "不是数据基准日" in text
    assert f"market_scan 基准日 {STALE_DAY}" in text
    assert "落后 2 个交易日" in text
    assert "基准日未知" in text                   # 其余四块如实标未知
    assert "4 块无显式基准日" in text


def test_summary_prompt_only_claim_cannot_upgrade_freshness():
    """块内自己写「硬约束：可用于今日结论」——不算数，结论只看 base_date。"""
    text = ss.render_signal_summary(
        _blocks({}, claim="硬约束：本块已核验，可直接用于今日结论"),
        snapshot_date=TODAY,
        now=INTRADAY,
    )
    verdict = [ln for ln in text.splitlines() if ln.startswith("时效：")][0]
    assert "不得作为今日结论依据" in verdict
    assert "只认本行核验结果" in verdict
    assert "可用于今日结论" not in verdict
    # 块正文里的自述仍然保留（不静默删），但被块级标签压住
    assert "硬约束" in text and "基准日未知" in text


def test_summary_all_blocks_verified_only_when_today_and_closed():
    closed_text = ss.render_signal_summary(
        _blocks({k: TODAY for k in ALL_BLOCKS}), snapshot_date=TODAY, now=CLOSED
    )
    assert "可用于今日结论" in closed_text
    assert "代码核验" in closed_text

    intraday_text = ss.render_signal_summary(
        _blocks({k: TODAY for k in ALL_BLOCKS}), snapshot_date=TODAY, now=INTRADAY
    )
    assert "可用于今日结论" not in intraday_text
    assert "盘中未收盘" in intraday_text


def test_summary_is_clock_independent_when_now_is_given(monkeypatch):
    """冻结进程时钟到 2035 也不会改变显式传 now 的结果 —— 证明没有隐式墙钟依赖。"""
    kwargs = dict(snapshot_date=TODAY, now=INTRADAY)
    blocks = _blocks({"market_scan": STALE_DAY})
    before = ss.render_signal_summary(blocks, **kwargs)
    monkeypatch.setattr(
        ss, "now_cn", lambda now=None: now or datetime(2035, 6, 1, 9, 0, tzinfo=ss._CST)
    )
    after = ss.render_signal_summary(blocks, **kwargs)
    assert before == after


# ─────────────── ⑥ 落库快照读取 ───────────────

def _sqlite_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from src.db.models import Base

    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)(), sessionmaker(
        bind=engine, expire_on_commit=False
    ), engine


def _seed_summary(db, snapshot_date: str, text: str, blocks: dict | None = None):
    from src.db.models import SignalSummaryDaily

    db.add(
        SignalSummaryDaily(
            snapshot_date=snapshot_date,
            stock_market="CN",
            blocks=blocks or {},
            text=text,
        )
    )
    db.commit()


def _seed_candidates(db, dates: list[str]):
    from src.web.models import EntryCandidate

    for i, d in enumerate(dates):
        db.add(
            EntryCandidate(
                snapshot_date=d,
                stock_market="CN",
                stock_symbol=f"60011{i}",
                stock_name=f"标的{i}",
                status="active",
                score=90 - i,
                action_label="关注",
                signal="放量突破",
                target_price=10.0 + i,
            )
        )
    db.commit()


def test_stored_summary_with_blocks_judges_each_block():
    db, _, engine = _sqlite_db()
    try:
        _seed_summary(
            db,
            TODAY,
            f"--- 系统信号摘要（生成日 {TODAY}）---\n时效：基准日 {TODAY}（今日，可用于今日结论）\nmarket_scan 内容",
            {"market_scan": {"data_status": "ok", "content": "market_scan 内容", "base_date": STALE_DAY}},
        )
        out = ss.read_latest_summary_text(db, now=INTRADAY)
        assert "生成日 2026-11-10" in out.splitlines()[0]
        assert "可用于今日结论" not in out
        assert f"market_scan 基准日 {STALE_DAY}" in out
        assert "market_scan 内容" in out            # 历史数据仍注入（不静默删）
        assert out.count("系统信号摘要") == 1
    finally:
        db.close()
        engine.dispose()


def test_legacy_text_only_snapshot_future_date_falls_back_to_unknown():
    db, _, engine = _sqlite_db()
    try:
        _seed_summary(db, TODAY, "--- 今日系统信号摘要(2026-12-01) ---\n情绪周期：发酵\n三榜 2026-12-01")
        out = ss.read_latest_summary_text(db, now=INTRADAY)
        head = out.splitlines()[0]
        assert "生成日 2026-11-10" in head          # 生成日照实写，但只叫「生成日」
        assert "基准日 未知" in head                # 文中唯一日期是未来日 → 剔掉后未知
        assert "2026-12-01" not in head
        assert "今日系统信号摘要" not in out
        assert "情绪周期：发酵" in out
    finally:
        db.close()
        engine.dispose()


def test_legacy_text_only_snapshot_conflicting_dates_is_unknown():
    db, _, engine = _sqlite_db()
    try:
        _seed_summary(db, TODAY, "情绪周期：发酵（对比 2026-10-20）\n三榜 2026-11-05")
        out = ss.read_latest_summary_text(db, now=INTRADAY)
        assert "基准日 未知" in out
        assert "可用于今日结论" not in out
    finally:
        db.close()
        engine.dispose()


# ─────────────── ⑦ market.sentiment：只认显式日期 ───────────────

def _patch_delegate(monkeypatch, payload: str):
    import src.agents.chat.tools_catalog as tc

    async def _fake(name, db, args, user):
        return payload

    monkeypatch.setattr(tc, "_delegate", _fake)
    return tc


def test_sentiment_does_not_grab_arbitrary_reference_date(monkeypatch):
    """反例：正文「对比 2026-10-20 那次高潮」曾被当成数据基准日 → 时点 2026-10-20。"""
    _freeze(monkeypatch, INTRADAY)
    tc = _patch_delegate(
        monkeypatch,
        "短线情绪周期: 发酵（对比 2026-10-20 那次高潮期的高度）\n今天涨停 58 家",
    )
    out = _run(tc._query_market_sentiment(None, {}, None))
    head = out.splitlines()[0]
    assert "时点 未知" in head
    assert "2026-10-20" not in head
    assert "历史" not in out                    # 不许拿参考日期判旧
    assert "未标注数据基准日" in out            # 未知就明说不可判


def test_sentiment_uses_explicit_labelled_date(monkeypatch):
    _freeze(monkeypatch, INTRADAY)
    tc = _patch_delegate(
        monkeypatch,
        f"短线情绪周期: 发酵\n\n📊 市场广度·情绪温度（数据日期 {STALE_DAY}）\n· 涨跌家数：上涨 3000",
    )
    out = _run(tc._query_market_sentiment(None, {}, None))
    head = out.splitlines()[0]
    assert f"时点 {STALE_DAY}" in head
    assert "落后 2 个交易日" in out


def test_sentiment_future_labelled_date_is_filtered(monkeypatch):
    _freeze(monkeypatch, INTRADAY)
    tc = _patch_delegate(monkeypatch, "📊 市场广度·情绪温度（数据日期 2026-12-01）")
    out = _run(tc._query_market_sentiment(None, {}, None))
    assert "时点 未知" in out.splitlines()[0]
    assert "2026-12-01" not in out.splitlines()[0]


def test_sentiment_conflicting_labelled_dates_is_unknown(monkeypatch):
    _freeze(monkeypatch, INTRADAY)
    tc = _patch_delegate(
        monkeypatch,
        f"分条A（数据日期 {STALE_DAY}）\n分条B（数据日期 {PREV_CLOSED}）",
    )
    out = _run(tc._query_market_sentiment(None, {}, None))
    assert "时点 未知" in out.splitlines()[0]


def test_sentiment_today_intraday_warns_not_usable(monkeypatch):
    _freeze(monkeypatch, INTRADAY)
    tc = _patch_delegate(monkeypatch, f"📊 市场广度·情绪温度（数据日期 {TODAY}）\n· 涨跌家数：上涨 3000")
    out = _run(tc._query_market_sentiment(None, {}, None))
    assert f"时点 {TODAY}" in out.splitlines()[0]
    assert "盘中快照" in out
    assert "可用于今日结论" not in out


def test_sentiment_today_closed_has_no_stale_note(monkeypatch):
    _freeze(monkeypatch, CLOSED)
    tc = _patch_delegate(monkeypatch, f"📊 市场广度·情绪温度（数据日期 {TODAY}）\n· 涨跌家数：上涨 3000")
    out = _run(tc._query_market_sentiment(None, {}, None))
    assert f"时点 {TODAY}" in out.splitlines()[0]
    assert "\n时效：" not in out and not out.startswith("时效：")


# ─────────────── ⑧ 广度块：显式数据日期 + 盘中不当收盘 ───────────────

def _bars(last_day: str, n: int = 30, symbols: int = 4000):
    from datetime import date, timedelta

    from src.core.market_breadth import BreadthBar

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


def test_breadth_render_has_explicit_data_date_label(monkeypatch):
    from src.collectors import market_breadth_series as M

    monkeypatch.setattr(M, "_load_stored_bars", lambda db, days=M.DEFAULT_DAYS: _bars(STALE_DAY))
    out = M.latest_with_percentile(None, now=INTRADAY)
    txt = M.render_text(out)
    assert f"数据日期 {STALE_DAY}" in txt
    assert "落后 2 个交易日" in txt
    assert ss.extract_labelled_dates(txt) == [STALE_DAY]


def test_breadth_intraday_today_is_not_called_closed(monkeypatch):
    from src.collectors import market_breadth_series as M

    monkeypatch.setattr(M, "_load_stored_bars", lambda db, days=M.DEFAULT_DAYS: _bars(TODAY))
    out = M.latest_with_percentile(None, now=INTRADAY)
    assert out["is_today"] is True
    txt = M.render_text(out)
    assert "今日盘中快照" in txt            # 盘中快照必须自报「未收盘」
    assert "不得写成收盘结论" in txt
    assert "可用于今日结论" not in txt

    closed = M.latest_with_percentile(None, now=CLOSED)
    assert "数据日期 2026-11-10" in M.render_text(closed)


def test_stored_summary_old_tags_are_stripped_and_recomputed(monkeypatch):
    """反例：11-10 收盘落库的「今日已收盘·可用于今日结论」，11-11 读回时不得原样复现。"""
    _freeze(monkeypatch, datetime(2026, 11, 11, 9, 30))   # 次日盘中，最近已收盘 = 11-10
    db, _, engine = _sqlite_db()
    try:
        blocks = _blocks({k: TODAY for k in ALL_BLOCKS})
        stored = ss.render_signal_summary(blocks, snapshot_date=TODAY, now=CLOSED)
        assert "可用于今日结论" in stored      # 写入时（收盘后）确实这么写
        _seed_summary(db, TODAY, stored, blocks)
        out = ss.read_latest_summary_text(db, now=datetime(2026, 11, 11, 9, 30))
        assert "可用于今日结论" not in out
        assert "前日" in out                    # 11-10 相对 11-11 是前日/最近已收盘
        assert out.count("系统信号摘要") == 1
        for k in ALL_BLOCKS:
            assert f"{k} 内容" in out           # 正文保留（只剥标签，不删证据）
    finally:
        db.close()
        engine.dispose()


def test_stored_summary_future_row_is_not_picked_as_latest(monkeypatch):
    """反例：一条 2026-12-01 的未来脏行挤掉 11-05 的真实快照。"""
    _freeze(monkeypatch, INTRADAY)
    db, _, engine = _sqlite_db()
    try:
        _seed_summary(
            db, "2026-12-01", "未来脏行\n情绪周期：不该被读到",
            {"sentiment": {"data_status": "ok", "content": "未来脏行", "base_date": "2026-12-01"}},
        )
        _seed_summary(
            db, STALE_DAY, f"--- 系统信号摘要（生成日 {STALE_DAY}）---\n情绪周期：发酵",
            {"market_scan": {"data_status": "ok", "content": "三榜：新G点[A]", "base_date": STALE_DAY}},
        )
        out = ss.read_latest_summary_text(db, now=INTRADAY)
        assert f"生成日 {STALE_DAY}" in out.splitlines()[0]
        assert "2026-12-01" not in out
        assert f"market_scan 基准日 {STALE_DAY}" in out
        assert "可用于今日结论" not in out
    finally:
        db.close()
        engine.dispose()


def test_stored_summary_with_blocks_but_empty_text_is_rendered():
    """无数据兜底：blocks 在、text 被清空 → 现场按块重渲染，不返回空壳/None。"""
    db, _, engine = _sqlite_db()
    try:
        _seed_summary(db, TODAY, "", _blocks({k: TODAY for k in ALL_BLOCKS}))
        out = ss.read_latest_summary_text(db, now=CLOSED)
        assert out is not None
        assert "可用于今日结论" in out
        for k in ALL_BLOCKS:
            assert f"{k} 内容" in out
    finally:
        db.close()
        engine.dispose()


def test_stored_summary_without_text_and_blocks_returns_none():
    db, _, engine = _sqlite_db()
    try:
        _seed_summary(db, TODAY, "")
        assert ss.read_latest_summary_text(db, now=CLOSED) is None
    finally:
        db.close()
        engine.dispose()


# ─────────────── ⑨ 无数据 ≠ 基准日未知 ───────────────

def test_missing_block_is_not_counted_as_unknown_date():
    """缺块/数据缺失 → 单独计「无数据」，不混进「有数据但没标日期」。"""
    blocks = _blocks({k: TODAY for k in ALL_BLOCKS})
    blocks["sentiment"] = {"data_status": "missing", "content": "情绪周期数据缺失"}
    closed = ss.render_signal_summary(blocks, snapshot_date=TODAY, now=CLOSED)
    assert "本块数据缺失" in closed
    assert "1 块无数据" in closed
    assert "无显式基准日" not in closed
    assert "可用于今日结论" not in closed      # 缺一块就不许声称可用

    bf = ss.block_freshness(blocks, now=CLOSED)
    assert bf["missing"] == ["sentiment"]
    assert bf["unknown"] == []
    assert bf["verified_usable_for_today"] is False


def test_empty_content_block_renders_explicit_gap():
    text = ss.render_signal_summary({"sentiment": {"data_status": "ok", "content": "   "}}, now=INTRADAY)
    assert "数据缺失（sentiment）" in text
    assert "〔本块数据缺失" in text


def test_partial_block_with_half_readings_counts_as_data():
    """partial 且确有读数（指数有了、资金缺）→ 有数据（只是没标基准日），不是「无数据」。"""
    blocks = _blocks({k: TODAY for k in ALL_BLOCKS})
    blocks["indices_flow"] = {
        "data_status": "partial",
        "content": "指数资金：上证+0.4%；主力净额未知",
    }
    bf = ss.block_freshness(blocks, now=CLOSED)
    assert bf["missing"] == []
    assert bf["unknown"] == ["indices_flow"]


def test_market_scan_block_skips_future_row():
    """三榜块：未来日期的脏行不许被选成「最新」。"""
    db, _, engine = _sqlite_db()
    try:
        from src.db.models import MarketScanRank

        db.add(MarketScanRank(snapshot_date=date(2026, 12, 1), stock_market="CN", payload={"new_g_points": [{"symbol": "FUT"}]}))
        db.add(MarketScanRank(snapshot_date=date(2026, 11, 5), stock_market="CN", payload={"new_g_points": [{"symbol": "OLD"}]}))
        db.commit()
        b = ss._block_market_scan(db)
        assert b["base_date"] == STALE_DAY
        assert "OLD" in b["content"] and "FUT" not in b["content"]
    finally:
        db.close()
        engine.dispose()


# ── 反例对照（真实调用，2026-11-10 14:40 冻结）────────────────────
# 修复前：
#   resolve_freshness("2026-11-09") → 历史 / 落后 1 个交易日
#   resolve_freshness("2026-11-10") → 「今日…可用于今日结论」
#   render(生成日 11-10, 三榜 11-05) → 标题「基准日 2026-11-10 … 可用于今日结论」
#   _query_market_sentiment("…对比 2026-10-20…") → 时点 2026-10-20 + 判历史
#   opportunity_header(今日, 日历不可用) → 【今日机会候选】
# 修复后全部落在上面这些用例里。
