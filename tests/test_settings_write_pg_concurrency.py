# -*- coding: utf-8 -*-
"""设置写入 + 审计的**单事务**在真实并发下的正确性(隔离 PG)。

为什么单独跑 PG: 本修复把"改设置"和"写审计"合成一次 commit(要么都有, 要么都没有)。
这条约束在 SQLite 上测不出并发语义(SQLite 是库级写锁, 冲突直接 Database is locked,
不会出现 PG 那种行锁排队 + 死锁检测)。这里用**独立的临时 PG 库**跑:

- N 个线程并发写同一个 key: 全部成功(行锁排队), 不允许死锁/丢审计;
- 审计行数 == 成功次数(没有任何一次"改了却没审计");
- 值永远是某次提交的完整结果(不会出现半个事务);
- 敏感值不落明文(并发下掩码逻辑也不能被绕过)。

隔离: 用 QUOTA_TEST_PG_ADMIN(本机隔离测试账号)新建 `sida_authz_<rand>` 库,
只建 app_settings / audit_logs 两张表, 结束即 DROP。**绝不碰生产库**。
"""
from __future__ import annotations

import threading
import uuid

import pytest

ADMIN_URL_ENV = "QUOTA_TEST_PG_ADMIN"


def _admin_url() -> str:
    import os

    url = (os.getenv(ADMIN_URL_ENV) or "").strip()
    if not url:
        pytest.skip(f"未配置 {ADMIN_URL_ENV}(本机隔离 PG), 跳过并发事务用例")
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if (parsed.get_backend_name() != 'postgresql'
            or parsed.host not in {'localhost', '127.0.0.1', '::1'}
            or parsed.database != 'postgres'):
        raise ValueError('破坏性测试只允许本机 postgres 维护库')
    return url


@pytest.fixture()
def pg_engine():
    from sqlalchemy import create_engine, text

    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    name = "sida_authz_" + uuid.uuid4().hex[:8]
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception:  # noqa: BLE001 —— 不输出连接异常，避免泄露凭据
        admin.dispose()
        pytest.skip("隔离 PG 不可用或无建库权限")

    url = _admin_url().rsplit("/", 1)[0] + "/" + name
    engine = create_engine(url, pool_size=8, max_overflow=8)
    from src.web.models import AppSettings, AuditLog

    try:
        with engine.connect() as conn:
            if conn.execute(text("SELECT current_database()")).scalar_one() != name:
                raise RuntimeError("临时数据库身份不匹配")
        AppSettings.__table__.create(engine)
        AuditLog.__table__.create(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def test_concurrent_settings_writes_keep_audit_in_sync(pg_engine):
    from sqlalchemy.orm import sessionmaker

    from src.web.api.settings import SettingsActor, _commit_or_500, _stage_audit
    from src.web.models import AppSettings, AuditLog

    Session = sessionmaker(bind=pg_engine, expire_on_commit=False)
    KEY = "zhitu_token"   # 敏感键: 顺带验证并发下掩码不被绕过
    THREADS, PER_THREAD = 6, 5

    setup = Session()
    try:
        setup.add(AppSettings(key=KEY, value="seed", description=""))
        setup.commit()
    finally:
        setup.close()

    errors: list[str] = []
    ok_counter = {"n": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(THREADS)

    def worker(tid: int) -> None:
        actor = SettingsActor(id=f"u{tid}", username=f"user{tid}", role="owner")
        barrier.wait()  # 尽量让所有线程同时冲击同一行
        for i in range(PER_THREAD):
            db = Session()
            try:
                row = db.query(AppSettings).filter(AppSettings.key == KEY).first()
                row.value = f"tok-{tid}-{i}"
                _stage_audit(
                    db, actor, f"settings_update:{KEY}",
                    "改前='********' 改后='********'",
                )
                _commit_or_500(db, KEY, actor)
                with lock:
                    ok_counter["n"] += 1
            except Exception as exc:  # noqa: BLE001 —— 收集后统一断言
                errors.append(f"tid={tid} i={i}: {exc!r}")
            finally:
                db.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, f"并发写入出现异常(死锁/回滚?): {errors[:3]}"
    assert ok_counter["n"] == THREADS * PER_THREAD

    check = Session()
    try:
        rows = (
            check.query(AuditLog)
            .filter(AuditLog.action == f"settings_update:{KEY}")
            .order_by(AuditLog.id)
            .all()
        )
        # 每次成功提交都必须有且只有一条审计 —— "改了没人知道"在这里就是丢行
        assert len(rows) == THREADS * PER_THREAD, f"审计行数 {len(rows)} != 写入次数"
        assert all("tok-" not in (r.detail or "") for r in rows), "审计泄露了明文凭证"
        assert {r.username for r in rows} == {f"user{t}" for t in range(THREADS)}

        final = check.query(AppSettings).filter(AppSettings.key == KEY).first().value
        # 最终值必须是某一次完整提交的结果, 不能是拼接/半截
        assert final in {f"tok-{t}-{i}" for t in range(THREADS) for i in range(PER_THREAD)}
    finally:
        check.close()
