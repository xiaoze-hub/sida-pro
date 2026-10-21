"""P0：长期记忆 + 会话定时任务（2026-10-21）。

铁律：
- 记忆只记用户**明确说**的（"记住/以后都/别再"），不从闲聊里推断偏好；
- 定时任务只在交易日出内容，同一天不重复跑；
- 所有副作用失败都不能把问答/调度搞挂。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core import copilot_memory as mem
from src.core import copilot_schedule as sched
from src.db.models import Base, ChatConversation, ChatMessage, CopilotMemory, CopilotSchedule


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'m.db'}")
    Base.metadata.create_all(bind=eng, tables=[
        CopilotMemory.__table__, CopilotSchedule.__table__,
        ChatConversation.__table__, ChatMessage.__table__,
    ])
    Sess = sessionmaker(bind=eng)
    s = Sess()
    yield s
    s.close()


@pytest.fixture()
def uid():
    return str(uuid.uuid4())


# ── 记忆 ────────────────────────────────────────────────────────────────

def test_extract_only_explicit_instructions():
    assert mem.extract_candidates("今天大盘怎么样") == []
    got = mem.extract_candidates("记住：我只做主板，不碰北交所")
    assert got and got[0][0] == "preference"
    assert "只做主板" in got[0][1]
    got2 = mem.extract_candidates("以后资金流别用东财方向位回答方向")
    assert got2 and got2[0][0] == "rule"
    # 只是问"以后会怎样"不是指令 —— 不能记
    assert mem.extract_candidates("以后行情会怎么走") == []


def test_remember_dedupes(db, uid):
    a = mem.remember(db, uid, content="我只做主板")
    b = mem.remember(db, uid, content="我只做主板")
    assert a["id"] == b["id"]
    assert db.query(CopilotMemory).count() == 1


def test_recall_puts_pinned_first(db, uid):
    mem.remember(db, uid, content="普通偏好A", weight=3)
    pinned = mem.remember(db, uid, content="钉住的规则", kind="rule", weight=10)
    rows = mem.recall(db, uid, limit=2)
    assert rows[0]["id"] == pinned["id"], "weight>=9 必须排在前面"


def test_disabled_memory_not_injected(db, uid):
    row = mem.remember(db, uid, content="这条要停用")
    mem.set_enabled(db, uid, row["id"], False)
    assert mem.recall(db, uid) == []


def test_memory_block_captures_then_injects(db, uid):
    block = mem.memory_block(db, uid, "记住：以后问资金流都用逐笔口径")
    assert "长期偏好" in block
    assert "逐笔口径" in block
    # 第二次没触发词，但仍应召回已有记忆
    block2 = mem.memory_block(db, uid, "600111 怎么样")
    assert "逐笔口径" in block2


def test_memory_block_never_raises(monkeypatch):
    class _Bad:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    assert mem.memory_block(_Bad(), "u1", "记住：x") == ""


# ── 定时任务 ────────────────────────────────────────────────────────────

def test_create_validates_time(db, uid):
    with pytest.raises(ValueError):
        sched.create(db, uid, kind="auction_digest", at_hhmm="9点25")
    with pytest.raises(ValueError):
        sched.create(db, uid, kind="nonsense", at_hhmm="09:25")
    row = sched.create(db, uid, kind="auction_digest", at_hhmm="9:5")
    assert row["at_hhmm"] == "09:05"


def test_run_due_respects_time_and_dedupes(db, uid, monkeypatch):
    row = sched.create(db, uid, kind="auction_digest", at_hhmm="09:25")

    calls = {"n": 0}

    def fake_text(_row):
        calls["n"] += 1
        return "【竞价摘要】测试内容"

    monkeypatch.setattr(sched, "_build_text", fake_text)
    monkeypatch.setattr(sched, "_match_weekday", lambda spec, d: True)

    from src.db import session as dbs

    monkeypatch.setattr(dbs, "SessionLocal", lambda: db)

    # 时间不匹配 → 不跑
    assert sched.run_due(datetime(2026, 10, 21, 9, 24))["ran"] == 0
    # 命中 → 跑一次并写消息
    out = sched.run_due(datetime(2026, 10, 21, 9, 25))
    assert out["ran"] == 1 and calls["n"] == 1
    msgs = db.query(ChatMessage).all()
    assert len(msgs) == 1 and msgs[0].role == "assistant"
    assert "竞价摘要" in msgs[0].content
    assert db.query(ChatConversation).count() == 1
    # 同一天再跑 → 跳过
    assert sched.run_due(datetime(2026, 10, 21, 9, 25))["ran"] == 0
    assert db.query(ChatMessage).count() == 1
    assert db.query(CopilotSchedule).one().id == row["id"]


def test_run_due_skips_when_no_content(db, uid, monkeypatch):
    sched.create(db, uid, kind="auction_digest", at_hhmm="09:25")
    monkeypatch.setattr(sched, "_build_text", lambda _r: None)
    monkeypatch.setattr(sched, "_match_weekday", lambda spec, d: True)
    from src.db import session as dbs

    monkeypatch.setattr(dbs, "SessionLocal", lambda: db)
    out = sched.run_due(datetime(2026, 10, 21, 9, 25))
    assert out["ran"] == 0 and out["skipped"] >= 1
    # 没内容时不标记已跑，下次还有机会
    assert db.query(CopilotSchedule).one().last_run_date is None


def test_weekday_matching():
    mon = date(2026, 10, 19)
    sat = date(2026, 10, 24)
    assert sched._match_weekday("mon-fri", mon) is True
    assert sched._match_weekday("mon-fri", sat) is False
    assert sched._match_weekday("daily", sat) is True


# ── 轮次用尽兜底（2026-10-21 线上：一次问两只票 → 只回"抱歉处理轮次过多"） ──

def test_summarize_on_budget_uses_collected_evidence():
    import asyncio

    from src.harness import runtime as rt

    class _Final:
        content = "强瑞技术：筹码集中度 12%；三花智控：暂无"

    class _AI:
        def __init__(self):
            self.calls = []

        async def chat_with_tools(self, messages, tools, temperature=0.4):
            self.calls.append((len(messages), tools))
            return _Final()

    ai = _AI()
    out = asyncio.run(rt.summarize_on_budget(ai, [{"role": "system", "content": "x"}]))
    assert "强瑞技术" in out
    assert "触达工具轮次上限" in out
    assert ai.calls[0][1] == [], "兜底总结必须不带工具，否则又会进入循环"


def test_summarize_on_budget_returns_empty_on_failure():
    import asyncio

    from src.harness import runtime as rt

    class _AI:
        async def chat_with_tools(self, *a, **k):
            raise RuntimeError("model down")

    assert asyncio.run(rt.summarize_on_budget(_AI(), [])) == ""
    hint = rt.budget_exhausted_hint()
    assert "一次只问一只" in hint, "建议必须可执行，不能只说请精简"


# ── 标的护栏（2026-10-21：问两只票却零工具作答、编出别的股票） ──

def test_is_stock_question():
    from src.core.symbol_guard import is_stock_question

    assert is_stock_question("301128强瑞技术今天怎么样")
    assert is_stock_question("全景数智深度诊断强锐技术和三花智控")
    assert is_stock_question("600519 还能拿吗")
    assert not is_stock_question("今天大盘怎么样")
    assert not is_stock_question("帮我记一下：以后都用逐笔口径")


def test_mention_candidates_extracts_names():
    from src.core.symbol_guard import mention_candidates

    got = mention_candidates("全景数智深度诊断强锐技术和三花智控")
    assert "强锐技术" in got and "三花智控" in got
    assert len(got) <= 4


def test_resolve_mentions_skips_when_code_given(monkeypatch):
    """用户已给代码就不许再"自动更正"成别的标的。"""
    from src.core import symbol_guard as sg

    called = []
    monkeypatch.setattr(sg, "mention_candidates", lambda t: called.append(t) or ["三花智控"])
    out = sg.resolve_mentions("301128 和 002050 怎么看")
    assert out == {"resolved": [], "suggestions": [], "unknown": []}
    assert called == [], "有代码时不该再做名称匹配"


def test_correction_note_flags_unknown_without_inventing(monkeypatch):
    """查不到的名字要明说查不到，不许编成别的票。"""
    from src.core import symbol_guard as sg

    monkeypatch.setattr(sg, "_tq_match", lambda w: [])
    monkeypatch.setattr(sg, "_suggest", lambda w: None)
    note = sg.correction_note("强锐技术和三花智控怎么看")
    assert "查不到" in note and "禁止换" in note
    assert "000" not in note.replace("0000", "")


def test_zero_tool_refusal_has_no_numbers():
    from src.core.symbol_guard import zero_tool_refusal

    t = zero_tool_refusal()
    assert "没有取到任何行情数据" in t
    assert "代码" in t
