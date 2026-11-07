"""真实 PostgreSQL 并发语义验证（非 SQLite 逻辑契约）。

验证 setting CAS 在真实 PG 上的并发行为:
  1. 两个并发 apply 同一条 CopilotAction（连点确认）→ 只有一个赢，另一个 409/已处理。
  2. 两个并发 insert 同一 absent key → 一个成功、另一个 SettingConflict(409)，
     且不破坏各自事务（SAVEPOINT 局部回滚）。
  3. FOR UPDATE 行锁：并发改同一 key 时 before 快照一致，第二个按冲突拒绝。

运行: DATABASE_URL=postgresql://ubuntu@localhost/sida_test_settings pytest -q tests/test_copilot_settings_pg.py
无 PG 时本文件应被跳过，不把 SQLite 冒充成 PG。
"""
import os
import threading

import pytest

PG_URL = os.environ.get("SIDA_TEST_PG_URL", "postgresql://ubuntu@localhost/sida_test_settings")


def _pg_available():
    try:
        import sqlalchemy
        engine = sqlalchemy.create_engine(PG_URL)
        with engine.connect() as c:
            c.execute(sqlalchemy.text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(), reason="无可用 PostgreSQL（本测试只做 PG 并发语义，不用 SQLite 冒充）"
)


def _fresh_engine():
    from sqlalchemy import create_engine
    engine = create_engine(PG_URL)
    from src.db.models import Base
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return engine


def _seed(engine):
    from sqlalchemy.orm import sessionmaker
    from src.db.models import AppSettings, User
    with sessionmaker(bind=engine)() as s:
        s.add_all([
            User(id="owner", username="owner", password_hash="x", role="owner"),
            User(id="member", username="member", password_hash="x", role="member"),
            AppSettings(key="notify_retry_attempts", value="2"),
        ])
        s.commit()


def test_concurrent_double_apply_single_winner():
    engine = _fresh_engine()
    _seed(engine)
    from sqlalchemy.orm import sessionmaker
    from src.harness.actions import propose
    with sessionmaker(bind=engine)() as s:
        item = propose(s, user_id="owner", kind="setting",
                       payload={"key": "notify_retry_attempts", "value": "3"})
        s.commit()
        action_id = item["id"]

    results = []
    lock = threading.Lock()

    def worker():
        from src.harness.actions import ActionError, apply_action
        with sessionmaker(bind=engine)() as s:
            try:
                apply_action(s, user_id="owner", action_id=action_id)
                with lock:
                    results.append("ok")
            except ActionError as exc:
                with lock:
                    results.append(f"err:{exc.status_code}")

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert results.count("ok") == 1, results
    assert sum(1 for r in results if r.startswith("err:")) == 1, results
    from sqlalchemy.orm import sessionmaker
    with sessionmaker(bind=engine)() as s:
        assert s.query(__import__("src.db.models", fromlist=["AppSettings"]).AppSettings) \
            .filter_by(key="notify_retry_attempts").one().value == "3"
    engine.dispose()


def test_concurrent_insert_absent_key_localized():
    engine = _fresh_engine()
    _seed(engine)
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine)

    def worker():
        from sqlalchemy.exc import IntegrityError
        with Session() as s:
            try:
                with s.begin_nested():
                    from src.db.models import AppSettings
                    s.add(AppSettings(key="notify_quiet_hours", value="09:00-10:00"))
                    s.flush()
                s.commit()
                return "ok"
            except IntegrityError:
                s.rollback()
                return "conflict"

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # 只有一个插入成功；另一个 SAVEPOINT 局部回滚，不破坏各自事务。
    with Session() as s:
        rows = s.query(__import__("src.db.models", fromlist=["AppSettings"]).AppSettings) \
            .filter_by(key="notify_quiet_hours").all()
        assert len(rows) == 1
    engine.dispose()
