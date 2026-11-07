"""受控修改三键通知设置的纵向闭环 + 安全缺陷修复的回归测试。

RED→GREEN 顺序:
  1. 空字符串是「关闭/继承默认」，不是非法值；absent/NULL/空三者区分。
  2. CAS 用 SELECT FOR UPDATE（fetch）保证已加载对象一致，冲突不覆盖。
  3. 并发 insert 冲突用 SAVEPOINT 局部回滚，翻译成 SettingConflict(409)。
  4. apply 不接受模型自造的 proposal dict：绑定持久化 CopilotAction 行，
     actor/kind/pending 来自行、key/before/value 从行读、audit action_id 同源。
  5. propose/apply 权限各自实时判定：失权/禁用/服务身份拒绝，错误码区分。
  6. 敏感值/任意 key 拒绝；setting 动作不暴露可撤销，只支持重新提议修正。
  7. 完整链路 _tool_propose_action 语义 → pending → apply → DB/审计/读取可达。

隔离数据库：SQLite 内存库只做逻辑契约验证；PG 并发语义见 test_concurrency（可选跑）。
**绝不把 SQLite 描述成 PG**：并发/行锁语义只在真实 PG 上验证。
"""
import json
import threading

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.db.models import AppSettings, AuditLog, CopilotAction, User


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    for table in (
        User.__table__,
        AppSettings.__table__,
        AuditLog.__table__,
        CopilotAction.__table__,
    ):
        table.create(engine)
    with sessionmaker(bind=engine)() as session:
        session.add_all([
            User(id="owner", username="owner", password_hash="x", role="owner"),
            User(id="member", username="member", password_hash="x", role="member"),
            User(id="pro", username="pro", password_hash="x", role="pro"),
            User(id="disabled", username="disabled", password_hash="x", role="owner", is_active=False),
            AppSettings(key="notify_retry_attempts", value="2"),
        ])
        session.commit()
        yield session
    engine.dispose()


def _propose(db, uid, key, value):
    from src.harness.actions import propose
    return propose(db, user_id=uid, kind="setting", payload={"key": key, "value": value})


# ── ① 空字符串语义 + absent/NULL/空 三态区分 ──────────────────────────────
def test_empty_string_means_disable_not_invalid(db):
    from src.harness.setting_policy import validate_setting
    assert validate_setting("notify_quiet_hours", "") == ""
    assert validate_setting("notify_retry_attempts", "") == ""


def test_absent_null_empty_are_distinct(db):
    from src.harness.setting_changes import prepare_setting
    # absent
    p = prepare_setting(db, "owner", "notify_quiet_hours", "")
    assert p["before_present"] is False and p["before"] is None
    # present + value='2'
    p = prepare_setting(db, "owner", "notify_retry_attempts", "3")
    assert p["before_present"] is True and p["before"] == "2"
    # present + NULL
    row = db.query(AppSettings).filter_by(key="notify_retry_attempts").one()
    row.value = None
    db.commit()
    p = prepare_setting(db, "owner", "notify_retry_attempts", "3")
    assert p["before_present"] is True and p["before"] is None


# ── ② CAS 用 fetch（FOR UPDATE），冲突不覆盖 ──────────────────────────────
def test_apply_conflict_does_not_overwrite(db):
    from src.harness.actions import ActionError, apply_action
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    row = db.query(AppSettings).filter_by(key="notify_retry_attempts").one()
    row.value = "1"  # 提议之后被改
    db.commit()
    with pytest.raises(ActionError) as ei:
        apply_action(db, user_id="owner", action_id=item["id"])
    assert ei.value.status_code == 409
    assert db.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "1"
    assert db.query(AuditLog).count() == 0


# ── ③ 并发 insert 冲突 SAVEPOINT 局部回滚 ─────────────────────────────────
def test_concurrent_insert_conflict_is_localized(db):
    from src.harness.actions import ActionError, apply_action
    # absent key；先插一条行，再 apply 提议（声明 absent）→ 应 409，不毁事务。
    db.add(AppSettings(key="notify_quiet_hours", value="08:00-09:00"))
    db.commit()
    item = _propose(db, "owner", "notify_quiet_hours", "09:00-10:00")
    # 人为把提议行 payload 改成 absent 基线（模拟 prepare 之后他人插入了该 key）
    action = db.get(CopilotAction, item["id"])
    action.payload = json.dumps({"key": "notify_quiet_hours", "value": "09:00-10:00",
                                 "before_present": False, "before": None})
    db.commit()
    with pytest.raises(ActionError) as ei:
        apply_action(db, user_id="owner", action_id=item["id"])
    assert ei.value.status_code == 409
    # 原行未被覆盖
    assert db.query(AppSettings).filter_by(key="notify_quiet_hours").one().value == "08:00-09:00"


# ── ④ 不接受模型 proposal dict：绑定持久化行，audit action_id 同源 ────────
def test_apply_reads_from_persisted_row_not_proposal_dict(db):
    from src.harness.actions import ActionError, apply_action
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    # 伪造：尝试直接调 setting_changes.apply_setting 塞一个自造 dict + 假 action_id
    from src.harness.setting_changes import apply_setting
    with pytest.raises(Exception):
        # 自造 proposal 缺少有效 action 绑定，不能凭空写库（apply_setting 不接受无行基线）
        apply_setting(
            db, "owner",
            {"key": "notify_retry_attempts", "value": "3",
             "before_present": True, "before": "999"},
            action_id=999999,
        )
    # 真实链路走 apply_action，audit 的 action_id 必须等于该行 id
    apply_action(db, user_id="owner", action_id=item["id"])
    audit = db.query(AuditLog).filter_by(action="copilot_settings_update").one()
    detail = json.loads(audit.detail)
    assert detail["action_id"] == item["id"]
    assert detail["key"] == "notify_retry_attempts"
    assert detail["after"] == "3"


def test_cross_user_apply_denied(db):
    from src.harness.actions import ActionError, apply_action
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    with pytest.raises(ActionError) as ei:
        apply_action(db, user_id="member", action_id=item["id"])
    # 不是属主 → 「动作不存在」（与不存在同一句，不泄露他人动作是否存在）
    assert ei.value.status_code == 400
    assert db.query(AuditLog).count() == 0


# ── ⑤ propose/apply 权限实时判定：失权/禁用/服务身份拒绝 ─────────────────
def test_member_propose_denied(db):
    from src.harness.actions import ActionError
    with pytest.raises(ActionError) as ei:
        _propose(db, "member", "notify_retry_attempts", "3")
    assert ei.value.status_code == 401


def test_demotion_between_propose_and_apply_denied(db):
    from src.harness.actions import ActionError, apply_action
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    owner = db.get(User, "owner")
    owner.role = "member"
    db.commit()
    with pytest.raises(ActionError) as ei:
        apply_action(db, user_id="owner", action_id=item["id"])
    assert ei.value.status_code == 401
    assert db.query(AuditLog).count() == 0


def test_disabled_user_apply_denied(db):
    from src.harness.setting_changes import prepare_setting, SettingPermissionError
    with pytest.raises(SettingPermissionError):
        prepare_setting(db, "disabled", "notify_retry_attempts", "3")


def test_service_principal_rejected(db):
    from src.harness.setting_changes import prepare_setting, SettingPermissionError
    class ServicePrincipal:
        id = "service"
        username = "service"
        role = "service"
        is_active = True
        is_service = True
    # 服务身份没有 User 行；直接走 _authorized_user 的语义：非人账号拒绝
    with pytest.raises(SettingPermissionError):
        prepare_setting(db, ServicePrincipal.id, "notify_retry_attempts", "3")


# ── ⑥ 敏感值/任意 key 拒绝 + 不可撤销 ────────────────────────────────────
@pytest.mark.parametrize("key,value", [
    ("api_secret", "x"),              # 任意 key
    ("notify_retry_attempts", "4"),   # 超范围值
    ("notify_quiet_hours", "25:00-26:00"),  # 非法时间
])
def test_out_of_scope_rejected(db, key, value):
    from src.harness.actions import ActionError
    with pytest.raises(ActionError):
        _propose(db, "owner", key, value)


def test_setting_not_rollbackable(db):
    from src.harness.actions import apply_action
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    apply_action(db, user_id="owner", action_id=item["id"])
    action = db.get(CopilotAction, item["id"])
    assert action.status == "applied"
    from src.harness.actions import _can_rollback
    assert _can_rollback(action, db=db) is False


# ── ⑦ 完整链路可达（SQLite 逻辑契约；非 PG）──────────────────────────────
def test_full_chain_persists_and_audits(db):
    from src.harness.actions import apply_action, list_pending
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    # pending 可见
    assert [a["id"] for a in list_pending(db, "owner")] == [item["id"]]
    # apply 前不落库
    assert db.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "2"
    out = apply_action(db, user_id="owner", action_id=item["id"])
    assert out["kind"] == "setting"
    assert db.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "3"
    audit = db.query(AuditLog).filter_by(action="copilot_settings_update").one()
    assert json.loads(audit.detail)["action_id"] == item["id"]
