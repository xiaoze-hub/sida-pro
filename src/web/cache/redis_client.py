"""兼容 shim(KI-039 第二阶段, 2026-09-09): Redis 客户端已下沉 `src/db/redis_client.py`。

`from src.web.cache.redis_client import redis_client/REDIS_URL` 保持可用; 新代码请直接
`from src.db.redis_client import ...` —— core 不得再依赖 Web 层。
"""

from src.db.redis_client import *  # noqa: F401,F403
from src.db.redis_client import REDIS_DISABLED, REDIS_URL, RedisClient, redis_client  # noqa: F401
