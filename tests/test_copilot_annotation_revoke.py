"""本人可撤销**已确认**标注（2026-10-29, P2 撤销闭合）。

背景：`kline_annotations` 的线一旦由确认动作画上去，服务端此前没有任何撤销入口
（`rollback_action` 对 annotation 直接报「不支持回滚」）。用户点错一次价位/标的，
那条线就永久留在图上 —— 而图上也没有删除按钮。本文件钉住撤销的完整契约：

  - **只能撤销本人 + 有确认动作的标注**：他人（越权）一律拒绝且一行不改；
  - **无主标注（`user_id IS NULL` 全局行）拒绝**：它不属于任何账号，服务端不替谁删；
  - **没有确认动作的标注拒绝**：本条契约只管「已确认」，人工/AI 直插的历史行不在此列；
  - **状态明确**：撤销成功回 `status="revoked"`、动作变成 `rolled_back`；重复撤销
    （标注已消失）→ 404，动作状态不符/链接异常 → 409，全部带可执行文案；
  - **并发与事务一致**：动作状态用**条件 UPDATE 原子认领**，标注用条件 DELETE；
    任一失败整笔回滚 —— 不允许出现「动作已 rolled_back 但线还在图上」的半截状态；
  - **编号复用防御**：SQLite 会复用已删行的主键；若当前 `annotation_id` 指向的行
    与动作 payload 的目标（symbol/label/period/market/**instrument**/price/ts）对不上，
    一律拒绝删除 —— 同号同价的指数线与个股线是两条互不可见的通道，漏比类型就会删错。
  - **一次性、同尺子（2026-10-29 二次复审）**：
    ① 越权与「不存在」共用同一句 404 文案（否则回显就是标注存在性探针）；
    ② 条件 DELETE 复述**比对过的那一整份目标**，读→删之间被改写就 0 行 + 整笔回滚；
    ③ 审计与删除**同一事务**提交：审计写不进去就不许删（删了就没第二份快照）；
    ④ `can_rollback` 必须实测（有 session 才敢说 true，没 session 按 false）。

接口（后端契约，前端由其它任务接线）：

    DELETE /api/copilot/annotations/{annotation_id}?reason=<自由文本>
    POST   /api/copilot/actions/{action_id}/rollback?reason=<自由文本>   # kind=annotation 也走通

响应：`{ok, status:"revoked", annotation_id, action_id, action_status, symbol, label, reason, revoked_at}`

状态码：200 成功 / 404 不存在或不属于本账号（不泄露他人标注）/ 409 无主、无确认动作、
归属不一致、编号被复用、动作状态不允许、并发撤销失败。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import AuditLog, CopilotAction, KlineAnnotation, Stock, User
from src.db.session import SessionLocal
from src.harness import actions as A
from src.web.api import copilot as copilot_api
from src.web.api.auth import get_current_user
from src.web.database import get_db

UID = "c9e4a7f3-55d1-4b8c-9e43-annrevoke000"
OTHER_UID = "d1f5b8a4-66e2-4c9d-8f54-annrevoke001"

assert len(UID) == 36 and len(OTHER_UID) == 36


class _U:
    def __init__(self, uid: str):
        self.id = uid
        self.username = uid
        self.role = "member"
        self.is_admin = False


def _cleanup(db) -> None:
    db.rollback()
    for uid in (UID, OTHER_UID):
        db.query(CopilotAction).filter(CopilotAction.user_id == uid).delete()
        db.query(KlineAnnotation).filter(KlineAnnotation.user_id == uid).delete()
        db.query(Stock).filter(Stock.user_id == uid).delete()
        db.query(User).filter(User.id == uid).delete()
    db.query(KlineAnnotation).filter(KlineAnnotation.user_id.is_(None)).delete()
    db.query(AuditLog).filter(AuditLog.user_id.in_((UID, OTHER_UID))).delete()
    db.commit()


def _ensure_users(db) -> None:
    for uid, name in ((UID, "ann-revoke-owner"), (OTHER_UID, "ann-revoke-other")):
        if db.query(User).filter(User.id == uid).first() is None:
            db.add(User(id=uid, username=name, password_hash="x", role="member"))
    db.commit()


@pytest.fixture()
def db():
    session = SessionLocal()
    _cleanup(session)
    _ensure_users(session)
    try:
        yield session
    finally:
        _cleanup(session)
        session.close()


def _confirm(db, payload: dict, uid: str = UID) -> dict:
    item = A.propose(db, user_id=uid, kind="annotation", payload=payload)
    return A.apply_action(db, user_id=uid, action_id=item["id"])


def _add_annotation(db, *, uid: str | None, annotation_id: int | None = None,
                    symbol: str = "600519", label: str = "手工线",
                    price: float = 10.0, period: str = "1d",
                    market: str = "CN", instrument: str = "stock",
                    source: str = "user") -> KlineAnnotation:
    row = KlineAnnotation(
        id=annotation_id, user_id=uid, symbol=symbol, market=market,
        instrument=instrument, period=period, price=price, ts=None,
        label=label, note=None, color="up", source=source,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _revoke_audits(db, uid: str = UID) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.user_id == uid, AuditLog.action == "copilot_action_rollback")
        .all()
    )


# ── 1. 本人撤销成功：线消失、动作 rolled_back、审计留痕 ──────────────────


def test_owner_revokes_confirmed_annotation(db):
    out = _confirm(db, {"symbol": "600519", "label": "缩量假阳", "price": 35.23})
    assert out["can_rollback"] is True, "确认后应如实告诉前端这条线可以撤销"

    res = A.revoke_annotation(db, user_id=UID, annotation_id=out["annotation_id"],
                              reason="价位选错")

    assert res["ok"] is True and res["status"] == "revoked"
    assert res["annotation_id"] == out["annotation_id"]
    assert res["action_id"] == out["id"]
    assert res["action_status"] == "rolled_back"
    assert res["symbol"] == "600519" and res["label"] == "缩量假阳"
    assert res["reason"] == "价位选错" and res["revoked_at"]

    assert db.query(KlineAnnotation).filter(KlineAnnotation.id == out["annotation_id"]).first() is None
    action = db.query(CopilotAction).filter(CopilotAction.id == out["id"]).one()
    assert action.status == "rolled_back"

    # 读取端不再返回（硬删 ⇒ 图上不可能还留着这条线）
    from src.web.api.copilot import get_copilot_annotations

    rows = get_copilot_annotations(symbol="600519", market="CN", period="1d",
                                   db=db, user=_U(UID))
    assert rows == []

    # 审计：撤销本身必须留痕（含被删标注快照，行删了也能追溯）
    audits = _revoke_audits(db)
    assert len(audits) == 1
    detail = json.loads(audits[0].detail)
    assert detail["kind"] == "annotation"
    assert detail["annotation_id"] == out["annotation_id"]
    assert detail["action_id"] == out["id"]
    assert detail["reason"] == "价位选错"
    assert detail["annotation"]["symbol"] == "600519"
    assert detail["annotation"]["price"] == 35.23


def test_action_rollback_endpoint_path_also_revokes_annotation(db):
    out = _confirm(db, {"symbol": "600519", "label": "动作回滚路径", "price": 30.0})

    res = A.rollback_action(db, user_id=UID, action_id=out["id"], reason="走动作回滚")

    assert res["status"] == "rolled_back"
    assert res["can_rollback"] is False
    assert res["revoked_annotation"]["annotation_id"] == out["annotation_id"]
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    assert db.query(CopilotAction).filter(CopilotAction.id == out["id"]).one().status == "rolled_back"


# ── 2. 撤销后状态明确：重复撤销不会二次删别人/别的东西 ───────────────────


def test_second_revoke_is_rejected_with_explicit_status(db):
    out = _confirm(db, {"symbol": "600519", "label": "只能撤一次", "price": 31.0})
    A.revoke_annotation(db, user_id=UID, annotation_id=out["annotation_id"])

    with pytest.raises(A.ActionError) as ei:
        A.revoke_annotation(db, user_id=UID, annotation_id=out["annotation_id"])
    assert ei.value.status_code == 404
    # 与"不存在"、"不是你的"同句（不许出现只有这里才有的措辞）
    assert str(ei.value) == A._annotation_lookup_denied(out["annotation_id"])

    with pytest.raises(A.ActionError) as ei2:
        A.rollback_action(db, user_id=UID, action_id=out["id"])
    assert ei2.value.status_code in (400, 409)
    assert "rolled_back" in str(ei2.value)

    assert db.query(KlineAnnotation).count() == 0
    assert len(_revoke_audits(db)) == 1, "重复撤销不许再写一条审计"


# ── 3. 越权 / 无主 / 无确认动作：一律拒绝且一行不改 ───────────────────────


def test_cross_user_annotation_is_refused(db):
    theirs = _confirm(db, {"symbol": "600519", "label": "他人的线", "price": 20.0},
                      uid=OTHER_UID)

    with pytest.raises(A.ActionError) as ei:
        A.revoke_annotation(db, user_id=UID, annotation_id=theirs["annotation_id"])
    # 不泄露"这条标注存在但不属于你"
    assert ei.value.status_code == 404
    assert db.query(KlineAnnotation).filter(
        KlineAnnotation.id == theirs["annotation_id"]).one().user_id == OTHER_UID
    assert db.query(CopilotAction).filter(
        CopilotAction.id == theirs["id"]).one().status == "applied"
    assert _revoke_audits(db, UID) == []
    # 也不许用动作回滚接口越权
    with pytest.raises(A.ActionError) as ei2:
        A.rollback_action(db, user_id=UID, action_id=theirs["id"])
    assert ei2.value.status_code == 404


def test_ownerless_global_annotation_is_refused(db):
    out = _confirm(db, {"symbol": "600519", "label": "全局行", "price": 18.0})
    # 数据被改成"无主"（历史全局行 / 手工改库）：它的线在所有人图上
    db.query(KlineAnnotation).filter(
        KlineAnnotation.id == out["annotation_id"]).update({"user_id": None})
    db.commit()

    with pytest.raises(A.ActionError) as ei:
        A.revoke_annotation(db, user_id=UID, annotation_id=out["annotation_id"])
    assert ei.value.status_code == 409
    msg = str(ei.value)
    assert "无主" in msg or "全局" in msg
    assert db.query(KlineAnnotation).filter(
        KlineAnnotation.id == out["annotation_id"]).one() is not None
    assert db.query(CopilotAction).filter(CopilotAction.id == out["id"]).one().status == "applied"


def test_annotation_without_confirming_action_is_refused(db):
    manual = _add_annotation(db, uid=UID, label="人工画的")

    with pytest.raises(A.ActionError) as ei:
        A.revoke_annotation(db, user_id=UID, annotation_id=manual.id)
    assert ei.value.status_code == 409
    assert "确认动作" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.id == manual.id).one() is not None


def test_action_and_annotation_owner_mismatch_is_refused(db):
    """标注属我、动作属他人 = 链路被改坏；既不删标注也不改动作。"""
    out = _confirm(db, {"symbol": "600519", "label": "链路错位", "price": 17.0})
    db.query(CopilotAction).filter(CopilotAction.id == out["id"]).update(
        {"user_id": OTHER_UID})
    db.commit()

    with pytest.raises(A.ActionError) as ei:
        A.revoke_annotation(db, user_id=UID, annotation_id=out["annotation_id"])
    assert ei.value.status_code == 409
    assert "归属" in str(ei.value) or "不一致" in str(ei.value)
    assert db.query(KlineAnnotation).filter(
        KlineAnnotation.id == out["annotation_id"]).one() is not None
    assert db.query(CopilotAction).filter(
        CopilotAction.id == out["id"]).one().status == "applied"


def test_annotation_id_reuse_is_refused_not_collateral_deleted(db):
    """SQLite 会复用主键：旧链接指向同号新行时，绝不能删掉那条新标注。"""
    out = _confirm(db, {"symbol": "600519", "label": "旧线", "price": 16.0})
    aid = out["annotation_id"]
    db.execute(text("DELETE FROM kline_annotations WHERE id = :i"), {"i": aid})
    db.commit()
    reused = _add_annotation(db, annotation_id=aid, uid=UID, label="同号新行", price=9.9)

    with pytest.raises(A.ActionError) as ei:
        A.revoke_annotation(db, user_id=UID, annotation_id=aid)
    assert ei.value.status_code == 409
    assert "一致" in str(ei.value) or "复用" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.id == reused.id).one() is not None
    assert db.query(CopilotAction).filter(CopilotAction.id == out["id"]).one().status == "applied"


def test_missing_annotation_reports_404_and_keeps_action_applied(db):
    out = _confirm(db, {"symbol": "600519", "label": "线被清理了", "price": 15.0})
    db.execute(text("DELETE FROM kline_annotations WHERE id = :i"), {"i": out["annotation_id"]})
    db.commit()

    with pytest.raises(A.ActionError) as ei:
        A.revoke_annotation(db, user_id=UID, annotation_id=out["annotation_id"])
    assert ei.value.status_code == 404
    assert db.query(CopilotAction).filter(CopilotAction.id == out["id"]).one().status == "applied"


def test_historical_annotation_action_without_link_is_refused(db):
    """迁移前确认的标注动作没有 `annotation_id`：不许猜要删哪条线。"""
    out = _confirm(db, {"symbol": "600519", "label": "历史动作", "price": 14.2})
    db.query(CopilotAction).filter(CopilotAction.id == out["id"]).update({"annotation_id": None})
    db.commit()

    with pytest.raises(A.ActionError) as ei:
        A.rollback_action(db, user_id=UID, action_id=out["id"])
    assert ei.value.status_code == 409
    assert "v231" in str(ei.value) or "关联" in str(ei.value)
    # 不知道删哪条 → 一条都不删
    assert db.query(KlineAnnotation).filter(
        KlineAnnotation.id == out["annotation_id"]).count() == 1
    assert db.query(CopilotAction).filter(CopilotAction.id == out["id"]).one().status == "applied"


def test_revoke_clears_weak_link_so_next_id_reuse_is_not_blocked(db):
    """撤销后不许留悬空 `annotation_id`：SQLite 复用刚空出的主键时，别的账号也要能确认。"""
    out = _confirm(db, {"symbol": "600519", "label": "会被复用的线", "price": 14.5})
    A.revoke_annotation(db, user_id=UID, annotation_id=out["annotation_id"])

    action = db.query(CopilotAction).filter(CopilotAction.id == out["id"]).one()
    assert action.annotation_id is None, "行已删，弱引用必须置空"
    assert f"#{out['annotation_id']}" in (action.result or ""), "撤销对象仍可追溯"

    # 另一个账号紧接着确认：SQLite 的 max(id)+1 正好是刚撤销的那个号
    theirs = _confirm(db, {"symbol": "600519", "label": "他人新线", "price": 9.0},
                      uid=OTHER_UID)
    assert theirs["status"] == "applied"
    assert theirs["annotation_id"] == out["annotation_id"], "应复用空出的主键（SQLite 语义）"
    row = db.query(KlineAnnotation).filter(KlineAnnotation.id == theirs["annotation_id"]).one()
    assert row.user_id == OTHER_UID and row.label == "他人新线"
    # 我的旧动作仍能重新提议确认（没被别人的新行占住）
    again = _confirm(db, {"symbol": "600519", "label": "我再来一条", "price": 8.0})
    assert again["status"] == "applied"


# ── 4. 并发：两个会话同时撤，只允许一个赢，且不留半截状态 ────────────────


def test_concurrent_revoke_in_two_sessions_has_one_winner(db):
    out = _confirm(db, {"symbol": "600519", "label": "并发撤销", "price": 14.0})
    aid, act_id = out["annotation_id"], out["id"]
    db.close()

    s1, s2 = SessionLocal(), SessionLocal()
    try:
        # s2 先读到"仍可撤销"的旧快照（真实并发里的典型交错）
        assert s2.query(KlineAnnotation).filter(KlineAnnotation.id == aid).count() == 1
        assert s2.query(CopilotAction).filter(CopilotAction.id == act_id).one().status == "applied"

        first = A.revoke_annotation(s1, user_id=UID, annotation_id=aid)
        assert first["status"] == "revoked"

        with pytest.raises(A.ActionError) as ei:
            A.revoke_annotation(s2, user_id=UID, annotation_id=aid)
        assert ei.value.status_code in (404, 409)
    finally:
        s1.close()
        s2.close()

    chk = SessionLocal()
    try:
        assert chk.query(KlineAnnotation).filter(KlineAnnotation.id == aid).count() == 0
        assert chk.query(CopilotAction).filter(CopilotAction.id == act_id).one().status == "rolled_back"
        assert len(_revoke_audits(chk)) == 1
    finally:
        chk.close()


def test_atomic_claim_loses_to_a_prior_state_change(db):
    """动作状态在读到撤销之间被改掉：条件 UPDATE 必须 0 行，标注一行不动。"""
    out = _confirm(db, {"symbol": "600519", "label": "状态被抢", "price": 13.0})
    aid, act_id = out["annotation_id"], out["id"]
    db.query(CopilotAction).filter(CopilotAction.id == act_id).update({"status": "rejected"})
    db.commit()

    with pytest.raises(A.ActionError) as ei:
        A.revoke_annotation(db, user_id=UID, annotation_id=aid)
    assert ei.value.status_code == 409
    assert "rejected" in str(ei.value) or "状态" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.id == aid).one() is not None
    assert db.query(CopilotAction).filter(CopilotAction.id == act_id).one().status == "rejected"


def test_commit_failure_leaves_no_half_state(db):
    """写库失败必须整笔回滚：不许出现动作 rolled_back 但线还在图上。"""
    out = _confirm(db, {"symbol": "600519", "label": "提交失败", "price": 12.0})
    aid, act_id = out["annotation_id"], out["id"]

    orig_commit = db.commit
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("模拟提交失败")

    db.commit = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            A.revoke_annotation(db, user_id=UID, annotation_id=aid)
    finally:
        db.commit = orig_commit

    assert calls["n"] == 1
    assert db.query(KlineAnnotation).filter(KlineAnnotation.id == aid).one() is not None
    assert db.query(CopilotAction).filter(CopilotAction.id == act_id).one().status == "applied"
    assert _revoke_audits(db) == [], "没有提交成功的撤销不许留审计"


# ── 5. HTTP 层：状态码与响应体（路由/依赖注入真实接线）─────────────────────


@pytest.fixture()
def make_client():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    CopilotAction.__table__.create(eng, checkfirst=True)
    KlineAnnotation.__table__.create(eng, checkfirst=True)
    AuditLog.__table__.create(eng, checkfirst=True)
    Session = sessionmaker(bind=eng)

    def factory(user: _U):
        app = FastAPI()
        app.include_router(copilot_api.router, prefix="/api")
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: Session()
        return TestClient(app), Session()

    return factory


def test_http_delete_revoke_and_status_codes(make_client):
    client, sess = make_client(_U(UID))
    try:
        out = _confirm(sess, {"symbol": "600519", "label": "HTTP 撤销", "price": 11.0})
        aid = out["annotation_id"]

        r = client.delete(f"/api/copilot/annotations/{aid}?reason=UI%20%E6%92%A4%E9%94%80")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "revoked"
        assert body["action_status"] == "rolled_back"
        assert body["annotation_id"] == aid and body["action_id"] == out["id"]
        assert body["reason"] == "UI 撤销"

        # 再删一次：404（明确状态，不假装成功）
        again = client.delete(f"/api/copilot/annotations/{aid}")
        assert again.status_code == 404
    finally:
        sess.close()


def test_http_revoke_refuses_other_user_and_ownerless(make_client):
    owner_client, owner_sess = make_client(_U(UID))
    other_client, other_sess = make_client(_U(OTHER_UID))
    try:
        out = _confirm(owner_sess, {"symbol": "600519", "label": "越权目标", "price": 10.5})
        aid = out["annotation_id"]

        r = other_client.delete(f"/api/copilot/annotations/{aid}")
        assert r.status_code == 404
        assert owner_sess.query(KlineAnnotation).filter(KlineAnnotation.id == aid).count() == 1
        assert owner_sess.query(CopilotAction).filter(
            CopilotAction.id == out["id"]).one().status == "applied"

        owner_sess.query(KlineAnnotation).filter(KlineAnnotation.id == aid).update({"user_id": None})
        owner_sess.commit()
        r2 = owner_client.delete(f"/api/copilot/annotations/{aid}")
        assert r2.status_code == 409
        assert "无主" in r2.json()["detail"] or "全局" in r2.json()["detail"]
    finally:
        owner_sess.close()
        other_sess.close()


def test_http_rollback_endpoint_revokes_annotation_kind(make_client):
    client, sess = make_client(_U(UID))
    try:
        out = _confirm(sess, {"symbol": "600519", "label": "动作回滚 HTTP", "price": 10.0})
        r = client.post(f"/api/copilot/actions/{out['id']}/rollback?reason=%E7%82%B9%E9%94%99")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "rolled_back"
        assert body["revoked_annotation"]["annotation_id"] == out["annotation_id"]
        assert sess.query(KlineAnnotation).count() == 0
    finally:
        sess.close()


# ── 6. 独立复审阻断项（2026-10-29 第二次复审）──────────────────────────────
# 这一节钉的是实现细节里**能真删错数据 / 真丢快照**的缺口，不是文案润色：
#   ① 越权与不存在的 404 必须是同一句，否则按文案就能探出"这条线存在但不是你的"；
#   ② 编号复用比对漏了 `instrument`：000001 的指数线与个股线同号同价，漏比就删错通道；
#   ③ 读→比对→删之间是 TOCTOU：DELETE 只按 id+user_id，中间被改写也照删；
#   ④ 先提交删除、再 best-effort 写审计 ⇒ 审计失败时快照永久丢失（行已删，无源可查）；
#   ⑤ `_to_dict` 不传 db 时 annotation 的 `can_rollback` 只能靠猜 ⇒ 会谎报可撤销。


def test_cross_user_and_missing_annotation_are_indistinguishable(db):
    """越权与他人数据不存在必须**同句同码**：否则 404 文案就是存在性探针。"""
    theirs = _confirm(db, {"symbol": "600519", "label": "他人的线", "price": 20.0},
                      uid=OTHER_UID)
    other_id = theirs["annotation_id"]
    missing_id = other_id + 900000

    with pytest.raises(A.ActionError) as ei_other:
        A.revoke_annotation(db, user_id=UID, annotation_id=other_id)
    with pytest.raises(A.ActionError) as ei_missing:
        A.revoke_annotation(db, user_id=UID, annotation_id=missing_id)

    assert ei_other.value.status_code == 404
    assert ei_missing.value.status_code == 404
    assert str(ei_other.value).replace(f"#{other_id}", "#N") == str(ei_missing.value).replace(
        f"#{missing_id}", "#N"
    ), "越权与不存在的文案必须一致（只允许 id 数字不同）"


def test_id_reuse_with_other_instrument_is_not_deleted(db):
    """同号同价、只差 `instrument` 的行是**另一个通道**的线，绝不能被删。"""
    out = _confirm(db, {"symbol": "000001", "label": "同号通道", "price": 12.0,
                        "instrument": "index"})
    aid = out["annotation_id"]
    db.execute(text("DELETE FROM kline_annotations WHERE id = :i"), {"i": aid})
    db.commit()
    # SQLite 复用了同一个主键：symbol/label/price/period/market 全同，只有类型不同
    reused = _add_annotation(db, annotation_id=aid, uid=UID, symbol="000001",
                             label="同号通道", price=12.0, instrument="stock")

    with pytest.raises(A.ActionError) as ei:
        A.revoke_annotation(db, user_id=UID, annotation_id=aid)
    assert ei.value.status_code == 409
    survivor = db.query(KlineAnnotation).filter(KlineAnnotation.id == reused.id).one()
    assert survivor.instrument == "stock", "个股通道的行被指数动作删掉了"
    assert db.query(CopilotAction).filter(CopilotAction.id == out["id"]).one().status == "applied"


def test_row_rewritten_between_match_and_delete_is_not_deleted(db, monkeypatch):
    """读→比对→删之间的窗口里行被改写：条件 DELETE 必须 0 行、整笔回滚。"""
    out = _confirm(db, {"symbol": "600519", "label": "并发改写", "price": 21.0})
    aid = out["annotation_id"]

    orig_match = A._matched_target

    def racing_match(annotation, action):
        want = orig_match(annotation, action)
        # 模拟另一个写者在"已经比对过、还没删"的窗口里改掉这条线
        db.execute(text("UPDATE kline_annotations SET price = :p WHERE id = :i"),
                   {"p": 99.0, "i": aid})
        return want

    monkeypatch.setattr(A, "_matched_target", racing_match)

    with pytest.raises(A.ActionError) as ei:
        A.revoke_annotation(db, user_id=UID, annotation_id=aid)
    assert ei.value.status_code == 409
    assert "变更" in str(ei.value) or "改写" in str(ei.value) or "不一致" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.id == aid).count() == 1, \
        "行在窗口里被改写，仍按旧值删掉 = 删错数据"
    assert db.query(CopilotAction).filter(CopilotAction.id == out["id"]).one().status == "applied"
    assert _revoke_audits(db) == []


def test_audit_write_failure_rolls_back_the_delete(db):
    """审计和删除必须同一事务：审计写不进去就不许删（否则快照永久丢失）。"""
    out = _confirm(db, {"symbol": "600519", "label": "审计失败", "price": 22.0})
    aid, act_id = out["annotation_id"], out["id"]

    orig_add = db.add

    def add_boom(obj):
        if isinstance(obj, AuditLog):
            raise RuntimeError("模拟审计落库失败")
        return orig_add(obj)

    db.add = add_boom  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            A.revoke_annotation(db, user_id=UID, annotation_id=aid)
    finally:
        db.add = orig_add  # type: ignore[method-assign]

    assert db.query(KlineAnnotation).filter(KlineAnnotation.id == aid).count() == 1, \
        "审计没落库却把行删了：快照再也查不回来"
    assert db.query(CopilotAction).filter(CopilotAction.id == act_id).one().status == "applied"
    assert _revoke_audits(db) == []


def test_can_rollback_without_a_session_is_conservative(db):
    """没有 db 就证明不了"行还在"：不许凭 `annotation_id` 非空就报 true。"""
    out = _confirm(db, {"symbol": "600519", "label": "不许猜", "price": 23.0})
    row = db.query(CopilotAction).filter(CopilotAction.id == out["id"]).one()

    assert A._to_dict(row)["can_rollback"] is False
    assert A._to_dict(row, db=db)["can_rollback"] is True


def test_dedup_branch_hands_the_session_to_can_rollback(db, monkeypatch):
    """情景矩阵的去重分支也必须把 session 传下去（复审点名的 `_to_dict(hit)`）。"""
    seen: list[bool] = []
    orig = A._to_dict

    def spy(row, *, db=None):
        seen.append(db is not None)
        return orig(row, db=db)

    monkeypatch.setattr(A, "_to_dict", spy)
    matrix = {"available": True, "levels": {"support": 95.5}}
    first = A.propose_scenario_annotations(
        db, user_id=UID, symbol="600519", market="CN", matrix=matrix,
    )
    assert first, "矩阵可用时应至少提议一个关键位"
    # 第二次必然走"命中同 label 的 pending"那条去重分支
    again = A.propose_scenario_annotations(
        db, user_id=UID, symbol="600519", market="CN", matrix=matrix,
    )
    assert any(p["dedup"] is True for p in again), "第二次提议应命中去重分支"
    assert seen and all(seen), "去重分支没传 db → can_rollback 只能靠猜"

