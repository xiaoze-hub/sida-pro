"""P1 页面即上下文 + 动作回写 / P2 技能手册 / P3 轨迹只读。

设计文档: docs/design/agent-kernel.md §5、§6。
原则只有两条:
  1. 页面锚点**只补空**, 绝不覆盖用户文字里的标的;
  2. AI 只能**提议**动作, 落地必须用户确认(改持仓/下单永不在框架内)。
"""
from __future__ import annotations

import pytest

from src.db.models import CopilotAction, KlineAnnotation, Stock, User
from src.db.session import SessionLocal
from src.harness import actions as A
from src.harness.skills import load_skill, skills_block
from src.harness.viewstate import ViewState, render_view_state_block

UID = "84425dba-9944-4f35-b804-fcdfc7ce6fa4"


def _ensure_user(db) -> None:
    """stocks.user_id 有外键 → 测试账号必须先存在。"""
    if db.query(User).filter(User.id == UID).first() is None:
        db.add(User(id=UID, username="p1-tester", password_hash="x", role="member"))
        db.commit()


def _cleanup() -> None:
    db = SessionLocal()
    try:
        db.query(CopilotAction).filter(CopilotAction.user_id == UID).delete()
        db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).delete()
        db.query(Stock).filter(Stock.user_id == UID).delete()
        db.commit()
    finally:
        db.close()


# ---------------- 页面即上下文 ----------------

def test_view_state_renders_compact_block():
    block = render_view_state_block({
        "page": "kline", "symbol": "600111", "market": "SH", "period": "30m",
        "selection": {"from": "2026-10-10", "to": "2026-10-18"}, "anchor_line": 35.2,
    })
    assert "K线页" in block and "SH:600111" in block
    assert "30m" in block and "圈选 2026-10-10~2026-10-18" in block
    assert "光标价位 35.2" in block
    assert len(block) <= 300          # 不抢 K 线数据的位置


def test_view_state_empty_renders_nothing():
    assert render_view_state_block(None) == ""
    assert render_view_state_block({}) == ""
    assert render_view_state_block({"page": None, "symbol": None}) == ""


def test_view_state_model_ignores_unknown_fields():
    state = ViewState(**{"page": "kline", "symbol": "600111", "whatever": 1})
    assert state.symbol == "600111"


def test_view_state_block_states_precedence():
    """锚点只补空: 文案里必须写明"以用户文字为准"。"""
    block = render_view_state_block({"symbol": "600111"})
    assert "以文字为准" in block


# ---------------- 动作回写 ----------------

def test_propose_then_apply_watchlist():
    db = SessionLocal()
    try:
        _cleanup()
        _ensure_user(db)
        item = A.propose(db, user_id=UID, kind="watchlist", payload={"symbol": "600111", "name": "北方稀土"})
        assert item["status"] == "pending"
        # 提议阶段不写业务表
        assert db.query(Stock).filter(Stock.user_id == UID, Stock.symbol == "600111").count() == 0

        applied = A.apply_action(db, user_id=UID, action_id=item["id"])
        assert applied["status"] == "applied"
        assert db.query(Stock).filter(Stock.user_id == UID, Stock.symbol == "600111").count() == 1

        # 幂等: 再加一次不重复插
        item2 = A.propose(db, user_id=UID, kind="watchlist", payload={"symbol": "600111"})
        res = A.apply_action(db, user_id=UID, action_id=item2["id"])
        assert "已在自选" in res["result"]
        assert db.query(Stock).filter(Stock.user_id == UID, Stock.symbol == "600111").count() == 1
    finally:
        _cleanup()
        db.close()


def test_apply_annotation_writes_kline_layer():
    db = SessionLocal()
    try:
        _cleanup()
        item = A.propose(db, user_id=UID, kind="annotation", payload={
            "symbol": "600111", "label": "缩量假阳", "price": 35.23, "note": "量 23.94 万手/额 8.42 亿",
        })
        A.apply_action(db, user_id=UID, action_id=item["id"])
        row = db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).one()
        assert row.symbol == "600111" and row.price == 35.23 and row.label == "缩量假阳"
        assert row.source == "ai"
    finally:
        _cleanup()
        db.close()


def test_alert_requires_price_and_creates_rule():
    """alert 从「仅提议」升级为真可执行：必须带 price，落地为价格提醒规则。"""
    from src.db.models import PriceAlertRule

    db = SessionLocal()
    try:
        _cleanup()
        _ensure_user(db)
        # 缺 price → 明确报错，不静默成功
        item = A.propose(db, user_id=UID, kind="alert", payload={"symbol": "600111"})
        with pytest.raises(A.ActionError) as ei:
            A.apply_action(db, user_id=UID, action_id=item["id"])
        assert "price" in str(ei.value)

        ok = A.propose(db, user_id=UID, kind="alert", payload={
            "symbol": "600111", "price": 36.5, "direction": "above", "label": "上破 36.5",
        })
        out = A.apply_action(db, user_id=UID, action_id=ok["id"])
        assert out["status"] == "applied" and "盯盘提醒" in out["result"]
        rule = db.query(PriceAlertRule).filter(PriceAlertRule.user_id == UID).one()
        assert rule.condition_group == {"op": "above", "price": 36.5}
        assert rule.enabled is True
    finally:
        db.query(PriceAlertRule).filter(PriceAlertRule.user_id == UID).delete()
        db.commit()
        _cleanup()
        db.close()


def test_unknown_kind_rejected():
    db = SessionLocal()
    try:
        with pytest.raises(A.ActionError):
            A.propose(db, user_id=UID, kind="order_stock", payload={"symbol": "600111"})
    finally:
        db.close()


def test_cannot_apply_other_users_action():
    db = SessionLocal()
    try:
        _cleanup()
        item = A.propose(db, user_id=UID, kind="watchlist", payload={"symbol": "600111"})
        with pytest.raises(A.ActionError):
            A.apply_action(db, user_id="someone-else", action_id=item["id"])
    finally:
        _cleanup()
        db.close()


def test_reject_marks_status():
    db = SessionLocal()
    try:
        _cleanup()
        item = A.propose(db, user_id=UID, kind="watchlist", payload={"symbol": "600111"})
        out = A.reject_action(db, user_id=UID, action_id=item["id"])
        assert out["status"] == "rejected"
        assert A.list_pending(db, UID) == []
    finally:
        _cleanup()
        db.close()


# ---------------- 技能手册 ----------------

def test_skills_exist_and_load():
    for name in ("auction", "premarket", "chip"):
        text = load_skill(name)
        assert text and len(text) > 80
    assert "无竞价快照" in load_skill("auction")
    assert "不预测方向" in load_skill("premarket")


def test_skills_block_skips_missing():
    assert skills_block(("auction", "not-exist")) == load_skill("auction")
    assert skills_block(()) == ""
    assert skills_block(("../etc/passwd",)) == ""


def test_recipes_declare_skills():
    from src.harness.recipes import get_recipe

    assert get_recipe("auction_hunter").skills == ("auction",)
    assert get_recipe("chip_analyst").skills == ("chip",)
    assert get_recipe("premarket_strategist").skills == ("premarket",)


def test_annotation_market_codes_are_interchangeable():
    """线上实测：AI 写 market=CN，K线页用 SH 查 → 标注「确认成功但图上没有」。"""
    from src.db.models import KlineAnnotation

    db = SessionLocal()
    try:
        _cleanup()
        db.add(KlineAnnotation(user_id=UID, symbol="600111", market="CN", period="1d",
                               price=35.2, label="缩量假阳", source="ai"))
        db.commit()
        # 读路径按 A 股一组匹配（CN/SH/SZ/BJ 等价）
        from src.web.api.copilot import get_copilot_annotations

        class _U:
            id = UID

        rows = get_copilot_annotations(symbol="600111", market="SH", period="1d", db=db, user=_U())
        assert [r["label"] for r in rows] == ["缩量假阳"]
    finally:
        _cleanup()
        db.close()
