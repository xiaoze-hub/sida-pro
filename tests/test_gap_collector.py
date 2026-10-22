"""维度缺口采集（确定性后置解析）的守卫测试。

背景（三次真机验收的结论，别再把工具调用这条路捡回来）：
`agnes-3.0-flash` 在连续 10 个只吃 `symbol` 的工具之后，对第 11 个工具会无视 schema
直接照抄 `{"symbol": ...}` —— 9 次实验 6 次如此，**把 symbol 从 schema 删掉它照样吐**。
所以采集改道：回答生成后，用一次**不带任何工具**的独立调用做判定。

这里守的是这条链路的四个承重墙：
  ① 只在该触发时触发（否则每轮问答白烧一次调用）；
  ② 解析严格（宁可丢，绝不猜/补）；
  ③ 入库走 propose（只记录、不生效）；
  ④ 全链路旁路（判定失败/入库失败都不许影响作答）。
"""

from __future__ import annotations

import asyncio

import pytest

from src.core import gap_collector as gc

ANSWER_WITH_GAP = (
    "**结论** 该股处于区间震荡。\n**消息面** 未取到（近 7 日无规则筛出的消息）。\n"
    "**筹码与资金** 主力净额 -3.2 亿。"
)
ANSWER_CLEAN = "**结论** 该股处于区间震荡。**筹码与资金** 主力净额 -3.2 亿。"
QUESTION = "600111 全景诊断，我还想看股东户数变化和龙虎榜席位风格。"


# ── ① 触发条件 ────────────────────────────────────────────────────
def test_triggers_only_for_deep_diagnosis():
    assert gc.should_check("deep_diagnosis", ANSWER_WITH_GAP) is True
    assert gc.should_check("demon_diagnosis", ANSWER_WITH_GAP) is False
    assert gc.should_check(None, ANSWER_WITH_GAP) is False
    assert gc.should_check("", ANSWER_WITH_GAP) is False


def test_triggers_only_when_answer_marks_a_miss():
    assert gc.should_check("deep_diagnosis", ANSWER_CLEAN) is False
    assert gc.should_check("deep_diagnosis", "") is False


@pytest.mark.parametrize("marker", ["未取到", "暂无", "无法获取", "没有这块数据", "未覆盖", "取不到"])
def test_every_documented_marker_triggers(marker):
    assert gc.should_check("deep_diagnosis", f"某段 {marker}") is True


# ── ② 解析严格 ────────────────────────────────────────────────────
def test_parse_plain_json_array():
    raw = '[{"dimension": "股东户数变化", "reason": "只有十大股东名单"}]'
    out = gc.parse_gaps(raw)
    assert out == [{"dimension": "股东户数变化", "reason": "只有十大股东名单"}]


def test_parse_tolerates_prose_around_array():
    raw = '好的，我判断如下：\n[{"dimension":"龙虎榜席位风格","reason":"无席位明细"}]\n以上。'
    assert gc.parse_gaps(raw) == [{"dimension": "龙虎榜席位风格", "reason": "无席位明细"}]


def test_parse_rejects_garbage_without_inventing():
    for bad in ("", "没有缺口", "{}", "null", "[", '{"dimension": "x"}'):
        assert gc.parse_gaps(bad) == [], f"输入 {bad!r} 不该解析出任何缺口"


def test_parse_drops_items_that_break_the_contract():
    raw = (
        '[{"dimension": "股"},'                       # 太短(1 字)
        '{"dimension": "' + "长" * 41 + '"},'          # 太长(41 字)
        '{"reason": "缺 dimension"},'                  # 非 dict 字段缺失
        '{"dimension": "股东户数变化"},'                 # 合规
        '{"dimension": " 股东 户数变化 "}]'             # 归一后重复 → 丢
    )
    out = gc.parse_gaps(raw)
    assert [g["dimension"] for g in out] == ["股东户数变化"]


def test_parse_caps_at_three():
    raw = "[" + ",".join(f'{{"dimension":"维度{i}号","reason":"r"}}' for i in range(6)) + "]"
    assert len(gc.parse_gaps(raw)) == gc.MAX_GAPS


def test_build_user_content_truncates_long_answer():
    content = gc.build_user_content(QUESTION, "甲" * 5000)
    assert QUESTION in content
    assert len(content) < 3000
    assert "…" in content


# ── ③ 入库走 propose（只记录） ─────────────────────────────────────
class _FakeClient:
    def __init__(self, reply: str = "", raise_exc: Exception | None = None):
        self.reply = reply
        self.raise_exc = raise_exc
        self.calls: list[tuple] = []

    async def chat(self, system_prompt, user_content, images=None, temperature=None):
        self.calls.append((system_prompt, user_content, temperature))
        if self.raise_exc:
            raise self.raise_exc
        return self.reply


def test_collect_writes_through_propose(monkeypatch):
    saved: list[dict] = []

    def fake_propose(db, user_id, **kwargs):
        saved.append({"user_id": user_id, **kwargs})
        return {"id": len(saved), **kwargs}

    monkeypatch.setattr("src.core.harness_proposals.propose", fake_propose)

    client = _FakeClient('[{"dimension":"股东户数变化","reason":"只有十大股东名单"}]')
    rows = asyncio.run(gc.collect(
        client, object(), question=QUESTION, answer=ANSWER_WITH_GAP,
        user_id="u1", conversation_id=7, symbol="600111", recipe="deep_diagnosis",
    ))

    assert len(rows) == 1
    assert saved[0]["user_id"] == "u1"
    assert saved[0]["dimension"] == "股东户数变化"
    assert saved[0]["conversation_id"] == 7
    assert saved[0]["recipe"] == "deep_diagnosis"
    # 判定调用必须是「不带工具」的纯文本调用，且温度为 0（同一输入要同一结果）
    assert client.calls and client.calls[0][2] == 0.0


def test_collect_skips_llm_call_when_not_triggered(monkeypatch):
    monkeypatch.setattr("src.core.harness_proposals.propose", lambda *a, **k: pytest.fail("不该入库"))
    client = _FakeClient('[{"dimension":"股东户数变化","reason":"r"}]')

    rows = asyncio.run(gc.collect(
        client, object(), question=QUESTION, answer=ANSWER_CLEAN,
        user_id="u1", recipe="deep_diagnosis",
    ))
    assert rows == []
    assert client.calls == [], "没触发标记就不该发这次 LLM 调用（每轮问答都在烧钱）"


# ── ④ 旁路：失败不许影响作答 ───────────────────────────────────────
def test_collect_swallows_llm_failure():
    client = _FakeClient(raise_exc=RuntimeError("网关 502"))
    rows = asyncio.run(gc.collect(
        client, object(), question=QUESTION, answer=ANSWER_WITH_GAP,
        user_id="u1", recipe="deep_diagnosis",
    ))
    assert rows == []


def test_collect_swallows_propose_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("src.core.harness_proposals.propose", boom)
    client = _FakeClient('[{"dimension":"股东户数变化","reason":"r"},{"dimension":"龙虎榜席位风格","reason":"r"}]')
    rows = asyncio.run(gc.collect(
        client, object(), question=QUESTION, answer=ANSWER_WITH_GAP,
        user_id="u1", recipe="deep_diagnosis",
    ))
    assert rows == []


def test_spawn_noop_when_not_triggered():
    """没触发标记时 spawn 连 task 都不该建。"""
    client = _FakeClient("[]")
    gc.spawn(client, question=QUESTION, answer=ANSWER_CLEAN, user_id="u1", recipe="deep_diagnosis")
    assert client.calls == []
