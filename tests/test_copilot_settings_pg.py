"""真实 PostgreSQL 并发 / 行锁 / SAVEPOINT 语义验证（2026-11-07 复审阻断项重写）。

为什么必须真 PG：SQLite 的写事务是库级互斥，`SELECT ... FOR UPDATE` 与「无行时并发
INSERT」在 SQLite 上永远撞不到一起 —— 在 SQLite 上"自然通过"证明不了任何并发安全
性。生产是 PostgreSQL。

⚠️ 安全规矩（旧版是本轮被驳回的阻断项）:
  旧实现直接拿 `SIDA_TEST_PG_URL` 指向的库跑 `Base.metadata.drop_all/create_all`。
  只要有人把这个变量指向生产库（或默认值恰好命中某个真实库），跑一次测试就把整库
  抹掉。**本文件绝不读写任何既有库**：

  1. 唯一入口是 `SIDA_TEST_PG_ADMIN`（可 CREATEDB/CREATEROLE 的**隔离管理连接**；
     兼容既有的 `QUOTA_TEST_PG_ADMIN`）。没有这个变量 → 全部 skip，宁可显示
     "没验证"，也不拿 SQLite 冒充 PG，更不猜一个库名往里面写。
  2. 每次运行新建 `sida_copilot_settings_t_<10位hex>` 库 + `sida_copilot_settings_u_<hex>`
     角色；建完先**核对** `current_database()` / `current_user` 就是刚建的那两个，
     且它们不等于管理连接自己的库/角色（防止共用连接串时误落到既有库）。
  3. 库里必须是空的（public schema 表数 0）才允许 create_all。
  4. 所有标识符先过正则再拼 SQL；`finally` 里 pg_terminate_backend → DROP DATABASE
     → DROP ROLE。名字不合规的一律拒绝执行（见 `_checked_ident` 的反例测试）。

运行:
    SIDA_TEST_PG_ADMIN=postgresql+psycopg2://<有CREATEDB的角色>:<pw>@127.0.0.1:5432/postgres \
        python -m pytest -q tests/test_copilot_settings_pg.py
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

ADMIN_URL = (os.environ.get("SIDA_TEST_PG_ADMIN") or os.environ.get("QUOTA_TEST_PG_ADMIN") or "").strip()

#: 只能建/删这两种形状的一次性对象：库与角色都以固定前缀 + 10 位 hex 结尾。
_DB_NAME_RE = re.compile(r"^sida_copilot_settings_t_[0-9a-f]{10}$")
_ROLE_NAME_RE = re.compile(r"^sida_copilot_settings_u_[0-9a-f]{10}$")
#: 永不接受的目标名（防御：即使正则被写错也不许碰这些）
_FORBIDDEN = frozenset({"postgres", "template0", "template1", "sida", "sida-pro", "sida_pro"})


def _checked_ident(name: str, pattern: re.Pattern[str]) -> str:
    """只有符合专属命名、且不在禁用名单里的标识符才允许拼进 SQL。"""
    if not isinstance(name, str) or not pattern.fullmatch(name) or name in _FORBIDDEN:
        raise RuntimeError(f"拒绝操作非专属测试对象: {name!r}")
    return name


def _admin_engine():
    return create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)


@pytest.fixture()
def pg_env():
    """建一套**一次性**角色 + 数据库，用完 finally 拆干净。"""
    if not ADMIN_URL:
        pytest.skip(
            "需要 SIDA_TEST_PG_ADMIN（可建库建角色的隔离 PG 管理连接）；"
            "本测试只做真实 PG 并发语义，不接受 SQLite 冒充，也不会往传入的任意库写"
        )
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - 连不上就跳过，不伪装成通过
        admin.dispose()
        pytest.skip(f"SIDA_TEST_PG_ADMIN 连不上: {type(exc).__name__}: {exc}")

    suffix = uuid.uuid4().hex[:10]
    dbname = _checked_ident(f"sida_copilot_settings_t_{suffix}", _DB_NAME_RE)
    role = _checked_ident(f"sida_copilot_settings_u_{suffix}", _ROLE_NAME_RE)
    pwd = f"pw_{uuid.uuid4().hex[:16]}"
    admin_url = make_url(ADMIN_URL)

    # PG16+: 有 CREATEROLE 的角色也只能建「自己能被 SET ROLE 的目标」为 owner 的库，
    # 所以先建角色、把角色 GRANT 给管理连接，再建库（不这么做会 InsufficientPrivilege）。
    _ADMIN_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
    admin_user = ""
    with admin.connect() as conn:
        admin_user = _checked_ident(conn.execute(text("SELECT current_user")).scalar(), _ADMIN_IDENT)
        conn.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{pwd}\''))
    try:
        with admin.connect() as conn:
            conn.execute(text(f'GRANT "{role}" TO "{admin_user}"'))
            conn.execute(text(f'CREATE DATABASE "{dbname}" OWNER "{role}"'))
    except Exception:
        # 建库失败也要把角色收干净，不留垃圾
        with admin.connect() as conn:
            conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        admin.dispose()
        raise

    url = admin_url.set(username=role, password=pwd, database=dbname)
    engine = create_engine(
        url,
        future=True,
        pool_size=4,
        max_overflow=4,
        connect_args={"options": "-c statement_timeout=20000 -c lock_timeout=10000"},
    )
    try:
        # 核对：连的确实是刚建的一次性库/角色，不是管理连接自己的库
        with engine.connect() as conn:
            who = conn.execute(text("SELECT current_database(), current_user")).one()
            assert who[0] == dbname, f"连错库了: {who[0]!r} != {dbname!r}"
            assert who[1] == role, f"连错角色了: {who[1]!r} != {role!r}"
            assert who[0] != (admin_url.database or ""), "拒绝在管理连接自己的库上建表"
            assert who[1] != (admin_url.username or ""), "拒绝用管理角色建表"
            existing = conn.execute(text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
            )).scalar()
            assert existing == 0, f"新库必须是空的，实际有 {existing} 张表"
        from src.db.models import Base
        Base.metadata.create_all(engine)
        yield engine, dbname, role
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": dbname},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
            conn.execute(text(f'REVOKE "{role}" FROM "{admin_user}"'))
            conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        admin.dispose()


def _seed(engine):
    from src.db.models import AppSettings, User
    with sessionmaker(bind=engine)() as s:
        s.add_all([
            User(id="owner", username="owner", password_hash="x", role="owner"),
            User(id="member", username="member", password_hash="x", role="member"),
            AppSettings(key="notify_retry_attempts", value="2"),
        ])
        s.commit()


def _race(maker, fn_a, fn_b):
    """两个独立 session 在屏障后同时执行；返回 {'a': 结果/异常, 'b': 结果/异常}。"""
    barrier = threading.Barrier(2, timeout=15)
    out: dict[str, object] = {}

    def run(key, fn):
        s = maker()
        try:
            barrier.wait()
            out[key] = fn(s)
        except BaseException as exc:  # noqa: BLE001 - 断言要看异常本身
            out[key] = exc
        finally:
            s.close()

    threads = [threading.Thread(target=run, args=("a", fn_a)), threading.Thread(target=run, args=("b", fn_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(40)
    assert len(out) == 2, f"并发线程没跑完（可能死锁/超时）: {out}"
    return out


# ── 命名守卫：不依赖数据库，任何环境都跑 ──────────────────────────────────
@pytest.mark.parametrize("bad", [
    "postgres", "sida", "sida-pro", "template1",
    "sida_copilot_settings_t_short",       # 长度不对
    "sida_copilot_settings_t_XXXXXXXXXX",  # 非 hex
    'x"; DROP DATABASE "sida"; --',        # 注入形状
    "", None, 123,
])
def test_guard_refuses_foreign_names(bad):
    with pytest.raises(RuntimeError):
        _checked_ident(bad, _DB_NAME_RE)  # type: ignore[arg-type]


def test_guard_accepts_own_temp_names():
    suffix = uuid.uuid4().hex[:10]
    assert _checked_ident(f"sida_copilot_settings_t_{suffix}", _DB_NAME_RE)
    assert _checked_ident(f"sida_copilot_settings_u_{suffix}", _ROLE_NAME_RE)


# ── ① 连点确认：同一条动作只有一个赢家 ────────────────────────────────────
def test_pg_double_apply_single_winner(pg_env):
    engine, _, _ = pg_env
    _seed(engine)
    maker = sessionmaker(bind=engine, future=True)
    from src.harness.actions import propose
    with maker() as s:
        item = propose(s, user_id="owner", kind="setting",
                       payload={"key": "notify_retry_attempts", "value": "3"})
        action_id = item["id"]

    from src.harness.actions import ActionError, apply_action

    def apply(db):
        return apply_action(db, user_id="owner", action_id=action_id)

    out = _race(maker, apply, apply)
    oks = [k for k, v in out.items() if not isinstance(v, BaseException)]
    errs = [k for k, v in out.items() if isinstance(v, ActionError)]
    assert len(oks) == 1, f"并发确认应只有一个成功，实际: {out}"
    assert len(errs) == 1, f"另一个必须被明确拒绝，实际: {out}"

    from src.db.models import AppSettings, AuditLog, CopilotAction
    with maker() as s:
        assert s.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "3"
        assert s.query(AuditLog).filter_by(action="copilot_settings_update").count() == 1
        assert s.query(CopilotAction).filter_by(id=action_id).one().status == "applied"


# ── ② 无行并发 insert：局部回滚，输家的事务仍然活着 ────────────────────────
def test_pg_absent_insert_conflict_is_localized(pg_env):
    """两个并发 apply 同一条「absent 基线」的提议：
    一个写成功，另一个撞唯一约束 → SettingConflict(409)，且**它自己的事务没死**：
    savepoint 只回滚那条 INSERT，之后还能在同一 session 里写别的并 commit。
    """
    engine, _, _ = pg_env
    _seed(engine)
    maker = sessionmaker(bind=engine, future=True)
    from src.harness.actions import propose
    from src.db.models import CopilotAction
    with maker() as s:
        item = propose(s, user_id="owner", kind="setting",
                       payload={"key": "notify_quiet_hours", "value": "09:00-10:00"})
        payload = json.loads(s.query(CopilotAction).filter_by(id=item["id"]).one().payload)
    assert payload["before_present"] is False

    from src.harness.setting_changes import SettingConflict, apply_setting

    def apply(db):
        try:
            apply_setting(db, "owner", payload, action_id=item["id"])
            db.commit()
            return "ok"
        except SettingConflict:
            # 关键断言：savepoint 之后事务仍可用（旧实现这里会 PendingRollbackError）
            assert db.is_active is True
            from src.db.models import AppSettings
            db.add(AppSettings(key="notify_retry_backoff_seconds", value="1.5"))
            db.commit()
            return "conflict+committed"

    out = _race(maker, apply, apply)
    assert sorted(str(v) for v in out.values()) == ["conflict+committed", "ok"], out

    from src.db.models import AppSettings, AuditLog
    with maker() as s:
        rows = s.query(AppSettings).filter_by(key="notify_quiet_hours").all()
        assert len(rows) == 1, f"absent key 并发后只能有一行，实际 {len(rows)}"
        assert rows[0].value == "09:00-10:00"
        # 输家 savepoint 之外的写在它自己的事务里提交成功了
        assert s.query(AppSettings).filter_by(key="notify_retry_backoff_seconds").one().value == "1.5"
        # 只有赢家那次留下审计（审计与写入同事务）
        assert s.query(AuditLog).filter_by(action="copilot_settings_update").count() == 1


# ── ③ 有行时 CAS：propose 之后值被改，apply 必须 409 且不覆盖 ──────────────
def test_pg_cas_conflict_after_external_update(pg_env):
    engine, _, _ = pg_env
    _seed(engine)
    maker = sessionmaker(bind=engine, future=True)
    from src.harness.actions import propose
    with maker() as s:
        item = propose(s, user_id="owner", kind="setting",
                       payload={"key": "notify_retry_attempts", "value": "3"})
    from src.db.models import AppSettings
    with maker() as s:
        s.query(AppSettings).filter_by(key="notify_retry_attempts").one().value = "1"
        s.commit()

    from src.harness.actions import ActionError, apply_action
    with maker() as s:
        with pytest.raises(ActionError) as ei:
            apply_action(s, user_id="owner", action_id=item["id"])
        assert ei.value.status_code == 409
    from src.db.models import AuditLog, CopilotAction
    with maker() as s:
        assert s.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "1"
        assert s.query(AuditLog).count() == 0
        assert s.query(CopilotAction).filter_by(id=item["id"]).one().status == "pending"
