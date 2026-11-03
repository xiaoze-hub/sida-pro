#!/usr/bin/env python3
"""一次性/幂等地开通 SIDA 的 DB 角色拆分（Phase 2，2026-11-03）。

为什么拆：
- 现在运行时用的 `sida` 是库 owner，有 DDL 权限 —— 任何逻辑漏洞/注入都能改表、删表；
- 拆成两个角色后，**运行时角色只能读写数据**，schema 变更只能由迁移角色做。

角色设计（刻意只建两个）：
- `sida_app`      —— 运行时。CONNECT + USAGE + 表级 SELECT/INSERT/UPDATE/DELETE + 序列；
                    **不给** CREATE（建表）/TRUNCATE/REFERENCES/TRIGGER，也不是 owner。
- `sida_migrator` —— 迁移/建表。`GRANT <owner> TO sida_migrator` 继承 DDL 能力。

为什么现在不建 `sida_admin`：管理面还没拆成独立进程（Phase 3）。先建一个没人用、
却带着密码的角色 = 多一把可能泄漏的钥匙。等 Phase 3 真正需要独立连接时再建。

用法（在能连到生产库的机器上，用 owner 凭据）：
    SIDA_DB_URL=postgresql+psycopg2://sida:***@host:5432/sida \
    python scripts/provision_db_roles.py [--print-dsn]

密码来源：环境变量 `SIDA_APP_PASSWORD` / `SIDA_MIGRATOR_PASSWORD`；缺省则自动生成
32 字节随机密码并打印一次（请立刻存进 ~/.hush，不要提交到仓库）。

幂等：角色已存在则只补授权；密码不会被覆盖（想轮换用 --rotate）。
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url

APP_ROLE = "sida_app"
MIGRATOR_ROLE = "sida_migrator"

# 运行时角色：能读写数据，不能改结构
APP_PRIVS = "SELECT, INSERT, UPDATE, DELETE"


def _owner_role(db_url: str) -> str:
    """当前连接用的角色名（就是现在的库 owner，迁移角色要继承它）。"""
    return make_url(db_url).username or "sida"


def _db_name(db_url: str) -> str:
    return make_url(db_url).database or "sida"


def _ensure_role(conn, role: str, password: str, rotate: bool) -> None:
    exists = conn.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).first()
    if exists:
        if rotate:
            conn.execute(text(f'ALTER ROLE "{role}" PASSWORD :p'), {"p": password})
            print(f"  · {role}: 已存在，密码已按 --rotate 重置")
        else:
            print(f"  · {role}: 已存在（保留原密码；要轮换用 --rotate）")
        return
    conn.execute(
        text(
            f'CREATE ROLE "{role}" LOGIN PASSWORD :p '
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
        ),
        {"p": password},
    )
    print(f"  · {role}: 已创建")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rotate", action="store_true", help="重置密码（否则保留原密码）")
    ap.add_argument("--print-dsn", action="store_true", help="打印可直接用的连接串")
    args = ap.parse_args()

    db_url = (os.getenv("DATABASE_URL_WRITE") or os.getenv("SIDA_DB_URL") or "").strip()
    if not db_url.startswith("postgresql"):
        print("只支持 PostgreSQL（SQLite 是开发库，不需要角色拆分）", file=sys.stderr)
        return 2

    app_pw = (os.getenv("SIDA_APP_PASSWORD") or "").strip() or secrets.token_urlsafe(24)
    mig_pw = (os.getenv("SIDA_MIGRATOR_PASSWORD") or "").strip() or secrets.token_urlsafe(24)
    owner = _owner_role(db_url)
    dbname = _db_name(db_url)

    from sqlalchemy import create_engine

    eng = create_engine(db_url)
    with eng.begin() as conn:
        print("1) 角色")
        _ensure_role(conn, APP_ROLE, app_pw, args.rotate)
        _ensure_role(conn, MIGRATOR_ROLE, mig_pw, args.rotate)

        # 迁移角色继承 owner：拥有 owner 的全部权限（含 DDL），但登录名不同，
        # 便于在日志/审计里区分"谁动了结构"。
        conn.execute(text(f'GRANT "{owner}" TO "{MIGRATOR_ROLE}"'))
        print(f"  · {MIGRATOR_ROLE}: 已继承 owner 角色 {owner} 的权限")

        print("2) 运行时角色授权")
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{dbname}" TO "{APP_ROLE}"'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{APP_ROLE}"'))
        conn.execute(text(f'GRANT {APP_PRIVS} ON ALL TABLES IN SCHEMA public TO "{APP_ROLE}"'))
        conn.execute(
            text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{APP_ROLE}"')
        )
        # 未来新建的表/序列也自动带上（由 owner 创建时生效）
        conn.execute(
            text(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public '
                f'GRANT {APP_PRIVS} ON TABLES TO "{APP_ROLE}"'
            )
        )
        conn.execute(
            text(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public '
                f'GRANT USAGE, SELECT ON SEQUENCES TO "{APP_ROLE}"'
            )
        )
        # 明确收回建表权（PG15+ 默认已收回，这里写成显式约定，不靠默认值）
        conn.execute(text(f'REVOKE CREATE ON SCHEMA public FROM "{APP_ROLE}"'))
        print(f"  · {APP_ROLE}: {APP_PRIVS} + 序列；已收回 CREATE/DDL")

    print("3) 验证")
    with eng.begin() as conn:
        checks = {
            "app 可 SELECT users": f"SELECT has_table_privilege('{APP_ROLE}', 'users', 'SELECT')",
            "app 可 INSERT users": f"SELECT has_table_privilege('{APP_ROLE}', 'users', 'INSERT')",
            "app 无建表权(public)": f"SELECT has_schema_privilege('{APP_ROLE}', 'public', 'CREATE')",
            "app 不是超级用户": f"SELECT rolsuper FROM pg_roles WHERE rolname='{APP_ROLE}'",
            "migrator 可建表(public)": f"SELECT has_schema_privilege('{MIGRATOR_ROLE}', 'public', 'CREATE')",
        }
        for label, sql in checks.items():
            val = conn.execute(text(sql)).scalar()
            print(f"  · {label}: {val}")

    if args.print_dsn:
        base = make_url(db_url)
        host = base.host or "postgres"
        port = base.port or 5432
        print("\n运行时(容器内):")
        print(f"  DATABASE_URL_WRITE=postgresql+psycopg2://{APP_ROLE}:{app_pw}@{host}:{port}/{dbname}")
        print("迁移:")
        print(f"  DATABASE_URL_MIGRATE=postgresql+psycopg2://{MIGRATOR_ROLE}:{mig_pw}@{host}:{port}/{dbname}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
