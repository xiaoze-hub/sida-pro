"""兼容 shim(KI-039 切片 B, 2026-09-09): ORM 模型已下沉到 `src/db/models.py`。

`from src.web.models import X` 保持可用(re-export); 新代码请直接
`from src.db.models import X` —— core 不得再依赖 Web 层。
"""

from src.db.models import *  # noqa: F401,F403
from src.db.models import Base  # noqa: F401  (显式导出, 部分调用方 from src.web.models import Base)
