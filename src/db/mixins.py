"""通用列 Mixin: 把 97 个模型里重复手写的时间戳列收敛到一处。

口径 (与 src/core/timezone.py 保持一致):
    PG(TZ=Asia/Shanghai) 的 now() 写入 ``timestamp without time zone`` 列 = 北京 naive;
    Python 的 datetime.utcnow() = UTC naive。两者同名列混用会差 8 小时。
存量库里绝大多数时间列走 ``server_default=func.now()``(北京 naive), 本 Mixin 沿用该口径,
**不引入语义变更** —— 抽取前后 Base.metadata 完全一致。

用法:
    class Foo(AuditMixin, Base):        # created_at + updated_at
    class Bar(CreatedAtMixin, Base):    # 只有 created_at
    class Baz(UpdatedAtMixin, Base):    # 只有 updated_at

需要额外索引 / 不同默认值时, 在子类里重声明同名列覆盖即可 (Mixin 的 Column 会被复制到
每个子类, 子类自己的 Column 优先):
    class WithIndex(CreatedAtMixin, Base):
        created_at = Column(DateTime, server_default=func.now(), index=True)

注意: 新增表请继承本 Mixin, 不要再复制粘贴 Column 定义 —— 复制粘贴正是漂移成
``datetime.utcnow`` 的来源 (2026-11-02 时残留 16 列)。
"""

from sqlalchemy import Column, DateTime
from sqlalchemy.sql import func


class CreatedAtMixin:
    """创建时间: DB 侧 now() 填充, 北京 naive。"""

    created_at = Column(DateTime, server_default=func.now())


class UpdatedAtMixin:
    """更新时间: DB 侧 now() 填充 + UPDATE 时自动刷新。"""

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AuditMixin(CreatedAtMixin, UpdatedAtMixin):
    """创建 + 更新时间。"""
