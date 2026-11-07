"""受控修改三键通知设置的纵向闭环 + 2026-11-07 复审阻断项回归。

RED→GREEN 顺序（本轮修复的阻断项）:
  ① **空值必须拒绝**。运行时的解析口径是
     `bootstrap/runtime._build_notifier`: `_get_app_setting(k) or settings.<env默认>`
     —— 空串是 falsy，所以「写空串」不会关闭任何东西，而是**静默回退到环境变量默认值**。
     允许空值 = 让用户以为关了、实际照旧。所以 validate_setting 一律拒绝空/纯空白，
     且任何给用户看的文案都不许出现「空=关闭」。
     （absent 无行 / NULL 有行值为空 / 有值 三态仍然区分：旧值只用于如实展示。）
  ② 确认界面必须念出**本动作的旧值、新值与三态/风险**；旧值只能来自后端持久化的
     propose 基线（payload + diff），模型塞进 payload 的 before/before_present 必须被丢弃。
  ③ 无行并发 insert 的 SAVEPOINT 必须把 `add` 与 `flush` **都**放进 savepoint 内部：
     先 `db.add()` 再 `begin_nested()` 会在 savepoint 之前 autoflush，IntegrityError 落在
     savepoint 外面 → 调用方的整笔事务被毁（`Session.is_active=False`），409 变成"整个请求炸掉"。
  ④ `apply_setting` 只认**正整数** action_id；无效 id 不得写出 action_id=null/伪造的审计。
  ⑤ 事务语义：设置写入与审计同生共死 —— 任何一步失败、或调用方回滚，两者都不留痕。

其余既有契约（CAS 冲突不覆盖、跨用户/降权/禁用/服务身份拒绝、越界 key 拒绝、
不可撤销、完整链路可达）继续钉住。

隔离数据库：SQLite 内存库只做逻辑契约与事务语义；PG 并发/行锁语义见
`test_copilot_settings_pg.py`（真实 PostgreSQL，只建一次性随机库）。
**绝不把 SQLite 描述成 PG**。
"""
import json

import pytest
from sqlalchemy import create_engine
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


def _propose(db, uid, key, value, **extra):
    from src.harness.actions import propose
    payload = {"key": key, "value": value, **extra}
    return propose(db, user_id=uid, kind="setting", payload=payload)


def _persisted_payload(db, action_id):
    return json.loads(db.get(CopilotAction, action_id).payload)


# ── ① 空值一律拒绝：空串 ≠ 关闭（运行时回落 env 默认）─────────────────────
@pytest.mark.parametrize("key", [
    "notify_retry_attempts", "notify_retry_backoff_seconds", "notify_quiet_hours",
])
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_value_rejected_not_disable(db, key, value):
    from src.harness.setting_policy import validate_setting
    with pytest.raises(ValueError) as ei:
        validate_setting(key, value)
    msg = str(ei.value)
    # 文案必须说清「空值不会被当成关闭」的真相，而不是含糊的「非法值」
    assert "空" in msg
    assert "关闭" in msg or "默认" in msg


def test_blank_value_rejected_at_propose(db):
    from src.harness.actions import ActionError
    with pytest.raises(ActionError) as ei:
        _propose(db, "owner", "notify_quiet_hours", "")
    assert ei.value.status_code == 400
    # 被拒的提议不许留下 pending 行（否则用户会看到一条点了必然失败的确认）
    assert db.query(CopilotAction).count() == 0


def test_absent_and_null_are_distinct(db):
    from src.harness.setting_changes import prepare_setting
    # absent：没有该 key 的行
    p = prepare_setting(db, "owner", "notify_quiet_hours", "09:00-10:00")
    assert p["before_present"] is False and p["before"] is None
    # 有行且值为 NULL（历史脏数据）：present=True 但 before=None，与 absent 不同
    row = db.query(AppSettings).filter_by(key="notify_retry_attempts").one()
    row.value = None
    db.commit()
    p = prepare_setting(db, "owner", "notify_retry_attempts", "3")
    assert p["before_present"] is True and p["before"] is None


def test_tool_handler_rejects_blank_value_and_schema_says_so(db):
    """模型看到的那一段 schema/工具回执也不许把空值说成关闭（否则模型必然照做）。

    走**真实工具 handler**（`propose_action`），不是只测 validate_setting：
    模型是照着 schema 说"空字符串=关闭/继承默认"去传空串的，这条谎话必须一起改掉。
    """
    import asyncio

    from src.agents.chat.registry import CHAT_TOOL_REGISTRY, _tool_propose_action

    desc = json.dumps(CHAT_TOOL_REGISTRY["propose_action"].schema, ensure_ascii=False)
    assert "空字符串=关闭" not in desc
    assert "不是非法值" not in desc
    assert "不可为空" in desc

    msg = asyncio.run(
        _tool_propose_action(
            db, {"kind": "setting", "key": "notify_quiet_hours", "value": ""}, db.get(User, "owner")
        )
    )
    assert "动作提议失败" in msg
    assert "空" in msg and "默认值" in msg
    assert db.query(CopilotAction).count() == 0


# ── ② 旧值只能来自后端持久化基线，模型自带的 before 被丢弃 ────────────────
def test_propose_discards_model_supplied_before(db):
    from src.harness.actions import propose
    item = propose(db, user_id="owner", kind="setting", payload={
        "key": "notify_retry_attempts", "value": "3",
        # 模型虚构的基线：真实库里是 "2"
        "before": "999", "before_present": False,
    })
    payload = json.loads(db.get(CopilotAction, item["id"]).payload)
    assert payload["before"] == "2"
    assert payload["before_present"] is True
    assert payload["value"] == "3"


@pytest.mark.parametrize("raw,state", [
    ("notify_quiet_hours", "absent"),
    ("notify_retry_attempts", "value"),
])
def test_diff_carries_tristate_for_ui(db, raw, state):
    """确认界面用的 diff 必须带三态，且文案不许把空值说成关闭。"""
    item = _propose(db, "owner", raw, "09:00-10:00" if raw == "notify_quiet_hours" else "3")
    diff = item["diff"]
    assert len(diff) == 1
    entry = diff[0]
    assert entry["field"] == raw
    assert entry["before_state"] == state
    assert entry["after"] == ("09:00-10:00" if raw == "notify_quiet_hours" else "3")
    # 「空=关闭」是谎话：运行时是回退 env 默认值
    assert "空值关闭" not in str(entry)
    assert "（空，关闭" not in str(entry)


def test_diff_tristate_null_row(db):
    row = db.query(AppSettings).filter_by(key="notify_retry_attempts").one()
    row.value = None
    db.commit()
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    assert item["diff"][0]["before_state"] == "null"


def test_apply_message_reports_before_and_after(db):
    from src.harness.actions import apply_action
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    out = apply_action(db, user_id="owner", action_id=item["id"])
    assert "2" in out["result"] and "3" in out["result"]
    assert "关闭" not in out["result"]


# ── ③ 无行并发 insert：SAVEPOINT 局部回滚，外层事务必须活着 ────────────────
def test_insert_conflict_localized_outer_transaction_survives(db, monkeypatch):
    """模拟「prepare 之后、apply 之前别人插了同 key」的竞态窗口：
    强制 `_lock_setting_row` 返回 None（看到的仍是 absent），真实走到 INSERT → 撞唯一约束。
    断言 409 翻译 + **调用方同一笔事务仍然可用**（SAVEPOINT 之外的东西没被回滚）。
    """
    from src.harness import setting_changes as sc
    from src.harness.setting_changes import SettingConflict, apply_setting

    # 1) 先按 absent 基线提议（此刻库里确实没有 notify_quiet_hours）
    item = _propose(db, "owner", "notify_quiet_hours", "09:00-10:00")
    payload = _persisted_payload(db, item["id"])
    assert payload["before_present"] is False

    # 2) 同一笔事务里先放一条未提交的写：savepoint 回滚不能把它带走
    db.add(AppSettings(key="notify_retry_backoff_seconds", value="1.5"))
    db.flush()

    # 3) 竞态：别人在 propose 之后、apply 之前插了 notify_quiet_hours
    db.execute(
        AppSettings.__table__.insert().values(key="notify_quiet_hours", value="08:00-09:00")
    )

    # 4) 强制 apply 仍按 absent 走（模拟它读到的快照），真撞唯一约束
    monkeypatch.setattr(sc, "_lock_setting_row", lambda _db, _key: None)
    with pytest.raises(SettingConflict):
        apply_setting(db, "owner", payload, action_id=item["id"])

    # 1) 会话没被打死（旧实现在 begin_nested 之前 autoflush，这里会 PendingRollbackError）
    assert db.is_active is True
    # 2) savepoint 之前的未提交写仍在，且能一起提交
    db.commit()
    db.expire_all()
    assert db.query(AppSettings).filter_by(key="notify_retry_backoff_seconds").one().value == "1.5"
    # 3) 别人插的那条没被覆盖
    assert db.query(AppSettings).filter_by(key="notify_quiet_hours").one().value == "08:00-09:00"
    # 4) 冲突不得留下审计
    assert db.query(AuditLog).count() == 0


# ── ④ apply_setting 只认正数 action_id ────────────────────────────────────
@pytest.mark.parametrize("bad", [None, 0, -1, True, "1", 1.0])
def test_apply_setting_requires_positive_action_id(db, bad):
    from src.harness.setting_changes import SettingNotFound, apply_setting
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    payload = _persisted_payload(db, item["id"])
    with pytest.raises(SettingNotFound):
        apply_setting(db, "owner", payload, action_id=bad)
    db.rollback()
    assert db.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "2"
    assert db.query(AuditLog).count() == 0


def test_apply_setting_rejects_fabricated_before(db):
    from src.harness.setting_changes import SettingConflict, apply_setting
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    with pytest.raises(SettingConflict):
        apply_setting(
            db, "owner",
            {"key": "notify_retry_attempts", "value": "3",
             "before_present": True, "before": "999"},
            action_id=item["id"],
        )
    db.rollback()
    assert db.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "2"
    assert db.query(AuditLog).count() == 0


# ── ⑤ 事务语义：写入 + 审计同生共死 ───────────────────────────────────────
def test_setting_and_audit_roll_back_together(db):
    from src.harness.setting_changes import apply_setting
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    payload = _persisted_payload(db, item["id"])
    apply_setting(db, "owner", payload, action_id=item["id"])
    # 调用方回滚：设置与审计都不许留痕
    db.rollback()
    db.expire_all()
    assert db.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "2"
    assert db.query(AuditLog).count() == 0


def test_audit_failure_leaves_setting_untouched(db, monkeypatch):
    from src.harness import setting_changes as sc
    from src.harness.setting_changes import apply_setting
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    payload = _persisted_payload(db, item["id"])

    class Boom:
        def __init__(self, **_kw):
            raise RuntimeError("audit store down")

    monkeypatch.setattr(sc, "AuditLog", Boom)
    with pytest.raises(RuntimeError):
        apply_setting(db, "owner", payload, action_id=item["id"])
    db.rollback()
    db.expire_all()
    assert db.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "2"
    assert db.query(AuditLog).count() == 0


# ── 既有契约：CAS 冲突不覆盖 ──────────────────────────────────────────────
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


def test_concurrent_insert_conflict_is_localized_via_action(db):
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
    # 动作仍是 pending（可以重新提议/忽略），不算被吞
    assert db.get(CopilotAction, item["id"]).status == "pending"


# ── 既有契约：audit 绑定本条动作行 ────────────────────────────────────────
def test_apply_binds_audit_to_own_action_row(db):
    from src.harness.actions import apply_action
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    apply_action(db, user_id="owner", action_id=item["id"])
    audit = db.query(AuditLog).filter_by(action="copilot_settings_update").one()
    detail = json.loads(audit.detail)
    assert detail["action_id"] == item["id"]
    assert detail["key"] == "notify_retry_attempts"
    assert detail["before"] == "2"
    assert detail["after"] == "3"
    assert detail["before_present"] is True


def test_cross_user_apply_denied(db):
    from src.harness.actions import ActionError, apply_action
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    with pytest.raises(ActionError) as ei:
        apply_action(db, user_id="member", action_id=item["id"])
    # 不是属主 → 「动作不存在」（与不存在同一句，不泄露他人动作是否存在）
    assert ei.value.status_code == 400
    assert db.query(AuditLog).count() == 0


# ── 既有契约：propose/apply 权限实时判定 ──────────────────────────────────
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
    # 服务身份没有 User 行；直接走 _authorized_user 的语义：非人账号拒绝
    with pytest.raises(SettingPermissionError):
        prepare_setting(db, "service", "notify_retry_attempts", "3")


# ── 既有契约：敏感值/任意 key 拒绝 + 不可撤销 ─────────────────────────────
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


# ── 完整链路可达（SQLite 逻辑契约；非 PG）─────────────────────────────────
def test_full_chain_persists_and_audits(db):
    from src.harness.actions import apply_action, list_pending
    item = _propose(db, "owner", "notify_retry_attempts", "3")
    # pending 可见，并且带着三态 diff 供界面显示旧值/新值
    pending = list_pending(db, "owner")
    assert [a["id"] for a in pending] == [item["id"]]
    assert pending[0]["diff"][0]["before_state"] == "value"
    # apply 前不落库
    assert db.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "2"
    out = apply_action(db, user_id="owner", action_id=item["id"])
    assert out["kind"] == "setting"
    assert db.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "3"
    audit = db.query(AuditLog).filter_by(action="copilot_settings_update").one()
    assert json.loads(audit.detail)["action_id"] == item["id"]
