#!/usr/bin/env python3
"""迁移一致性校验 (P2-A 部署防护 · 迁移校验)。

CI 门禁 + 同步/发版前本地必跑。只做 import 级静态检查, 不连 DB:

1. MIGRATIONS 版本号从 101 起严格 +1 连续 (无跳号/重号 — 跳号=小主机
   老库停在旧版本会静默缺迁移, 曾导致 /api/datasources 500)。
2. 每个 Migration: name 非空唯一、runner 可调用、函数名符合
   _m{version}_ 前缀约定 (防贴错函数)。
3. source_health.HEALTH_COLUMNS 的 key 必须全部存在于
   DataSource 模型列中 (模型删列但迁移还在加列会炸)。
4. success_count / error_count 必须带 server_default (P2-C 防御回归:
   新鲜 create_all 建库即带 DB 级默认值, 与 _m126 的 INTEGER DEFAULT 0 同口径)。
5. HEALTH_COLUMNS 中计数列的 dtype 必须含 DEFAULT 0
   (与模型 server_default 语义一致)。

用法: python scripts/check_migrations.py (exit 0=通过, 1=失败)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    errors: list[str] = []

    from src.web.migrations import MIGRATIONS
    from src.core.source_health import HEALTH_COLUMNS
    from src.web.models import DataSource

    # 1. 版本连续性
    versions = [m.version for m in MIGRATIONS]
    if not versions:
        errors.append("MIGRATIONS 为空")
    else:
        if versions[0] != 101:
            errors.append(f"起始版本不是 101: {versions[0]}")
        for prev, cur in zip(versions, versions[1:]):
            if cur != prev + 1:
                errors.append(f"版本不连续: {prev} -> {cur}(期望 {prev + 1})")

    # 2. 单条合法性
    seen_names: set[str] = set()
    for m in MIGRATIONS:
        if not m.name:
            errors.append(f"版本 {m.version} name 为空")
        if m.name in seen_names:
            errors.append(f"name 重复: {m.name}")
        seen_names.add(m.name)
        if not callable(m.runner):
            errors.append(f"版本 {m.version} runner 不可调用")
        else:
            want = f"_m{m.version}_"
            if not m.runner.__name__.startswith(want):
                errors.append(
                    f"版本 {m.version} runner 名 {m.runner.__name__!r} "
                    f"不符合前缀 {want!r}(可能贴错函数)"
                )

    # 3. HEALTH_COLUMNS ⊆ 模型列
    model_cols = set(DataSource.__table__.c.keys())
    for col in HEALTH_COLUMNS:
        if col not in model_cols:
            errors.append(f"HEALTH_COLUMNS.{col} 不在 DataSource 模型列中")

    # 4. 计数列 server_default (P2-C)
    for col in ("success_count", "error_count"):
        if col in model_cols:
            sd = DataSource.__table__.c[col].server_default
            if sd is None:
                errors.append(
                    f"DataSource.{col} 缺 server_default"
                    "(新鲜 create_all 建库无 DB 默认值, 与 _m126 不一致)"
                )

    # 5. 迁移 dtype 与模型默认值语义一致
    for col in ("success_count", "error_count"):
        dtype = HEALTH_COLUMNS.get(col, "")
        if "DEFAULT 0" not in dtype.upper().replace("  ", " "):
            errors.append(f"HEALTH_COLUMNS.{col} dtype {dtype!r} 缺 DEFAULT 0")

    if errors:
        print(f"❌ 迁移校验失败 ({len(errors)} 项):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(
        f"✅ 迁移校验通过: {len(MIGRATIONS)} 个迁移 "
        f"(v{versions[0]}-v{versions[-1]}), "
        f"{len(HEALTH_COLUMNS)} 个健康列与模型一致"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
