"""服务端确认标注上图的后端契约（2026-10-28, P2 可追溯契约）。

问题：AI 提议的标注（`copilot_actions.kind == "annotation"`）点确认后确实落了
`kline_annotations`，但**确认响应里没有那条标注的 id**，`copilot_actions` 也没有指向
它的列 —— 于是三件事做不到：

1. 前端确认后无法把「图上这条线」和「刚才那次动作」对上（点第二次、刷新、回滚都失去锚点）；
2. 事后排障只能靠 symbol/label/价格猜，两条同名标注就分不清是哪次确认画的；
3. 越权/重复确认没有服务端可判定的边界（谁能确认、能确认几次、画到谁名下）。

本文件钉住最小契约：
  - 一次 action 至多关联一条 annotation（`copilot_actions.annotation_id`，唯一索引），
    确认响应用**真实** `annotation_id` 回传，并回传实际落库的 symbol/market/period；
  - 归属一律取自身份（action 的 user_id），payload 里的 `user_id`/`annotation_id` 一律拒绝；
  - 越权（他人 action）默认拒绝且不产生任何标注行；
  - 历史数据（迁移前已 applied、annotation_id 为 NULL）照样能读，**不猜测**补 id；
  - 周期/价位做写入端校验，避免再出现「确认成功但图上查不到」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.db.models import CopilotAction, KlineAnnotation, Stock, User
from src.db.session import SessionLocal
from src.harness import actions as A

UID = "a7c2e5d1-33b8-4f6a-9d21-anncontract01"
OTHER_UID = "b8d3f6e2-44c9-4a7b-8e32-anncontract02"
SYMBOL = "600519"


def _db():
    return SessionLocal()


def _cleanup(db) -> None:
    db.rollback()
    for uid in (UID, OTHER_UID):
        db.query(CopilotAction).filter(CopilotAction.user_id == uid).delete()
        db.query(KlineAnnotation).filter(KlineAnnotation.user_id == uid).delete()
        db.query(Stock).filter(Stock.user_id == uid).delete()
        db.query(User).filter(User.id == uid).delete()
    db.commit()


def _ensure_users(db) -> None:
    """stocks.user_id 有外键 → 归属用例里的账号必须先存在。"""
    for uid, name in ((UID, "ann-contract-owner"), (OTHER_UID, "ann-contract-other")):
        if db.query(User).filter(User.id == uid).first() is None:
            db.add(User(id=uid, username=name, password_hash="x", role="member"))
    db.commit()


@pytest.fixture()
def db():
    session = _db()
    _cleanup(session)
    _ensure_users(session)
    try:
        yield session
    finally:
        _cleanup(session)
        session.close()


def _propose(db, payload: dict, user_id: str = UID) -> dict:
    return A.propose(db, user_id=user_id, kind="annotation", payload=payload)


# ── 1. 确认响应带回真实 annotation_id，并与 action 双向可追溯 ────────────────

def test_confirm_returns_real_annotation_id_and_links_action(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "缩量假阳", "price": 35.23,
                         "note": "量 23.94 万手/额 8.42 亿"})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])

    assert out["status"] == "applied"
    assert isinstance(out["annotation_id"], int) and out["annotation_id"] > 0

    row = db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).one()
    assert row.id == out["annotation_id"], "响应里的 id 必须就是落库那条标注"
    assert row.symbol == SYMBOL and row.price == 35.23 and row.label == "缩量假阳"

    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.annotation_id == row.id, "action → annotation 的关联必须落库"

    # 明细块只回传事实，不额外编字段
    detail = out["annotation"]
    assert detail["id"] == row.id
    assert detail["symbol"] == SYMBOL and detail["period"] == "1d"
    assert detail["source"] == "ai"


def test_confirm_echoes_symbol_and_period_and_is_visible_on_chart(db):
    """确认时写的 symbol/market/period 必须就是图上按这些参数能查到的那条。"""
    item = _propose(db, {"symbol": SYMBOL, "label": "30 分钟放量", "price": 35.2,
                         "market": "SH", "period": "30m", "ts": "2026-10-28 10:00"})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])

    assert out["annotation"]["market"] == "SH"
    assert out["annotation"]["period"] == "30m"
    assert out["annotation"]["ts"] == "2026-10-28 10:00"

    from src.web.api.copilot import get_copilot_annotations

    class _U:
        id = UID

    # 页面用 CN 查（A 股一组等价）也必须命中，且 id 与确认响应一致
    rows = get_copilot_annotations(symbol=SYMBOL, market="CN", period="30m", db=db, user=_U())
    assert [r["id"] for r in rows] == [out["annotation_id"]]


def test_period_url_alias_is_normalized_to_chart_interval(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "周线级别", "period": "w1"})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])
    assert out["annotation"]["period"] == "1w"


def test_unknown_period_is_rejected_without_partial_write(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "三小时", "period": "3h"})
    with pytest.raises(A.ActionError) as ei:
        A.apply_action(db, user_id=UID, action_id=item["id"])
    assert "周期" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    # 认领失败必须回滚，动作继续可确认（不能留下 applied 却没有标注）
    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.status == "pending"


def test_invalid_price_is_rejected_instead_of_written_as_garbage(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "错价", "price": "abc"})
    with pytest.raises(A.ActionError) as ei:
        A.apply_action(db, user_id=UID, action_id=item["id"])
    assert "价位" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0


# ── 2. 越权默认拒绝 ──────────────────────────────────────────────────────

def test_apply_other_users_action_is_denied_and_writes_nothing(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "别人的线", "price": 35.0}, user_id=OTHER_UID)

    with pytest.raises(A.ActionError):
        A.apply_action(db, user_id=UID, action_id=item["id"])

    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == OTHER_UID).count() == 0
    owner_action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert owner_action.status == "pending", "越权尝试不能改变他人动作状态"


def test_payload_cannot_inject_annotation_id_or_owner(db):
    """归属和 annotation_id 只能由服务端定；payload 里带了就等于试图改别人的图。"""
    victim = KlineAnnotation(user_id=OTHER_UID, symbol=SYMBOL, market="CN", period="1d",
                             price=99.0, label="他人标注", source="user")
    db.add(victim)
    db.commit()
    db.refresh(victim)

    for bad in ({"annotation_id": victim.id}, {"user_id": OTHER_UID}):
        item = _propose(db, {"symbol": SYMBOL, "label": "伪造", "price": 35.0, **bad})
        with pytest.raises(A.ActionError):
            A.apply_action(db, user_id=UID, action_id=item["id"])

    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    assert db.query(KlineAnnotation).filter(KlineAnnotation.id == victim.id).one().user_id == OTHER_UID


def test_apply_is_one_shot_per_action(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "只画一次", "price": 35.0})
    first = A.apply_action(db, user_id=UID, action_id=item["id"])

    with pytest.raises(A.ActionError):
        A.apply_action(db, user_id=UID, action_id=item["id"])

    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 1
    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.annotation_id == first["annotation_id"]


def test_reject_cannot_overwrite_an_applied_action(db):
    """已 applied 的动作被 reject 掉会让「有标注但动作显示被拒绝」——必须拒绝。"""
    item = _propose(db, {"symbol": SYMBOL, "label": "已确认", "price": 35.0})
    applied = A.apply_action(db, user_id=UID, action_id=item["id"])

    with pytest.raises(A.ActionError):
        A.reject_action(db, user_id=UID, action_id=item["id"])

    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.status == "applied"
    assert action.annotation_id == applied["annotation_id"]


def test_concurrent_second_confirmer_loses_and_draws_nothing(db):
    """两个会话都以为还是 pending（连点/双击/两个标签页）→ 只有一个能画上线。"""
    item = _propose(db, {"symbol": SYMBOL, "label": "并发确认", "price": 35.0})
    other = _db()
    try:
        stale = other.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
        assert stale.status == "pending"

        won = A.apply_action(db, user_id=UID, action_id=item["id"])
        assert won["annotation_id"]

        with pytest.raises(A.ActionError):
            A.apply_action(other, user_id=UID, action_id=item["id"])
        assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 1
    finally:
        other.close()


# ── 3. 历史数据兼容（迁移前 applied、无 annotation_id）────────────────────

def test_legacy_applied_action_without_link_reads_as_null_not_guessed(db):
    legacy = CopilotAction(
        user_id=UID, kind="annotation", status="applied", result="已标注 600519 @ 35.0：旧线",
        payload='{"symbol": "600519", "label": "旧线", "price": 35.0}',
    )
    db.add(legacy)
    db.commit()
    db.refresh(legacy)

    out = A._to_dict(legacy)
    assert out["annotation_id"] is None, "历史记录没有关联就是没有，不许猜一个"
    assert out["annotation"] is None
    assert out["kind"] == "annotation" and out["status"] == "applied"


def test_non_annotation_actions_keep_null_link(db):
    item = A.propose(db, user_id=UID, kind="watchlist", payload={"symbol": SYMBOL})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])
    assert out["annotation_id"] is None and out["annotation"] is None


# ── 3.5 HTTP 层：确认响应与越权状态码 ─────────────────────────────────────

def test_api_confirm_returns_annotation_id_and_cross_user_is_400(db):
    from fastapi import HTTPException

    from src.web.api.copilot import apply_copilot_action

    class _U:
        def __init__(self, uid: str):
            self.id = uid

    item = _propose(db, {"symbol": SYMBOL, "label": "API 确认", "price": 35.0})
    out = apply_copilot_action(action_id=item["id"], db=db, user=_U(UID))
    assert out["annotation_id"] == out["annotation"]["id"]

    other = _propose(db, {"symbol": SYMBOL, "label": "他人动作", "price": 35.0}, user_id=OTHER_UID)
    with pytest.raises(HTTPException) as ei:
        apply_copilot_action(action_id=other["id"], db=db, user=_U(UID))
    assert ei.value.status_code == 400
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 1


def test_scenario_proposal_confirms_into_traceable_annotation(db):
    """真实链路：情景矩阵提议 → 用户确认 → 图上按页面参数能查到同一个 id。"""
    matrix = {"available": True, "levels": {"support": 95.5, "resist": 108.25}}
    proposed = A.propose_scenario_annotations(
        db, user_id=UID, symbol=SYMBOL, market="SH", matrix=matrix,
    )
    support = next(p for p in proposed if p["payload"]["label"] == "支撑位")

    out = A.apply_action(db, user_id=UID, action_id=support["id"])
    assert out["annotation_id"] and out["annotation"]["price"] == 95.5
    assert out["annotation"]["market"] == "SH" and out["annotation"]["period"] == "1d"

    from src.web.api.copilot import get_copilot_annotations

    class _U:
        id = UID

    rows = get_copilot_annotations(symbol=SYMBOL, market="SH", period="1d", db=db, user=_U())
    assert out["annotation_id"] in [r["id"] for r in rows]


# ── 4. 迁移：加列 + 唯一索引，可重跑 ──────────────────────────────────────

def _has_column(conn, table: str, column: str) -> bool:
    return any(len(r) > 1 and str(r[1]) == column
               for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall())


def _has_index(conn, table: str, name: str) -> bool:
    rows = conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
    return any(len(r) > 1 and str(r[1]) == name for r in rows)


def test_migration_adds_column_and_unique_link_index_idempotently(db):
    from src.web.migrations import MIGRATIONS, _m231_copilot_action_annotation_link

    assert 231 in [m.version for m in MIGRATIONS], "v231 必须在 MIGRATIONS 元组里注册"

    with db.get_bind().begin() as conn:
        _m231_copilot_action_annotation_link(conn)
        _m231_copilot_action_annotation_link(conn)  # 幂等：重跑不炸
        assert _has_column(conn, "copilot_actions", "annotation_id")
        assert _has_index(conn, "copilot_actions", "uq_copilot_actions_annotation")

    # 唯一索引兜住「一条标注被两次确认占用」：非空值重复必须失败
    a1 = CopilotAction(user_id=UID, kind="annotation", payload="{}", status="pending")
    a2 = CopilotAction(user_id=UID, kind="annotation", payload="{}", status="pending")
    db.add_all([a1, a2])
    db.commit()
    a1.annotation_id = 987654321
    db.commit()
    a2.annotation_id = 987654321
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


def test_migration_alters_a_legacy_table_that_lacks_the_column():
    """存量库路径：老 copilot_actions 没有 annotation_id 时, v231 必须真把它加上。"""
    from sqlalchemy import create_engine

    from src.web.migrations import _m231_copilot_action_annotation_link

    engine = create_engine("sqlite://")  # 内存库, 不碰任何真实文件
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE copilot_actions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id VARCHAR(36) NOT NULL, "
            "kind VARCHAR(24) NOT NULL, payload TEXT NOT NULL, status VARCHAR(16) NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO copilot_actions (user_id, kind, payload, status)"
            " VALUES ('legacy', 'annotation', '{}', 'applied')"
        ))
        assert not _has_column(conn, "copilot_actions", "annotation_id")

        _m231_copilot_action_annotation_link(conn)
        assert _has_column(conn, "copilot_actions", "annotation_id")
        # 历史行不被回填, 也不会因为唯一索引失败
        assert conn.execute(text(
            "SELECT annotation_id FROM copilot_actions WHERE user_id='legacy'"
        )).scalar() is None
        _m231_copilot_action_annotation_link(conn)  # 幂等
