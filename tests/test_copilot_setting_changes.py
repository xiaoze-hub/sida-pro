"""Transactional staging service for approved settings; SQLite is not PG validation."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import AppSettings, AuditLog, User


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    for table in (User.__table__, AppSettings.__table__, AuditLog.__table__):
        table.create(engine)
    with sessionmaker(bind=engine)() as session:
        session.add_all([
            User(id='owner', username='owner', password_hash='x', role='owner'),
            User(id='member', username='member', password_hash='x', role='member'),
            AppSettings(key='notify_retry_attempts', value='2'),
        ])
        session.commit()
        yield session
    engine.dispose()


def test_propose_only_stages_and_member_denied(db):
    from src.harness.setting_changes import prepare_setting, SettingPermissionError
    proposal = prepare_setting(db, 'owner', 'notify_retry_attempts', '3')
    assert proposal['before'] == '2' and proposal['value'] == '3'
    assert db.query(AppSettings).filter_by(key='notify_retry_attempts').one().value == '2'
    with pytest.raises(SettingPermissionError):
        prepare_setting(db, 'member', 'notify_retry_attempts', '3')


def test_apply_requires_unchanged_value_and_current_permission(db):
    from src.harness.setting_changes import prepare_setting, apply_setting, SettingConflict, SettingPermissionError
    proposal = prepare_setting(db, 'owner', 'notify_retry_attempts', '3')
    setting = db.query(AppSettings).filter_by(key='notify_retry_attempts').one()
    setting.value = '1'
    db.commit()
    with pytest.raises(SettingConflict):
        apply_setting(db, 'owner', proposal, action_id=1)
    assert setting.value == '1'
    owner = db.get(User, 'owner')
    owner.role = 'member'
    db.commit()
    with pytest.raises(SettingPermissionError):
        apply_setting(db, 'owner', proposal, action_id=1)
    assert db.query(AuditLog).count() == 0


def test_apply_and_audit_share_caller_transaction(db):
    from src.harness.setting_changes import prepare_setting, apply_setting
    proposal = prepare_setting(db, 'owner', 'notify_retry_attempts', '3')
    apply_setting(db, 'owner', proposal, action_id=7)
    assert db.query(AppSettings).filter_by(key='notify_retry_attempts').one().value == '3'
    assert db.query(AuditLog).filter_by(action='copilot_settings_update').count() == 1
    db.rollback()
    assert db.query(AppSettings).filter_by(key='notify_retry_attempts').one().value == '2'
    assert db.query(AuditLog).count() == 0


def test_existing_null_and_absent_baselines_are_distinct(db):
    from src.harness.setting_changes import prepare_setting, apply_setting, SettingConflict
    row = db.query(AppSettings).filter_by(key='notify_retry_attempts').one()
    row.value = None
    db.commit()
    proposal = prepare_setting(db, 'owner', 'notify_retry_attempts', '1')
    assert proposal['before_present'] and proposal['before'] is None
    db.delete(row)
    db.commit()
    with pytest.raises(SettingConflict):
        apply_setting(db, 'owner', proposal, action_id=2)
    assert db.query(AppSettings).filter_by(key='notify_retry_attempts').count() == 0
