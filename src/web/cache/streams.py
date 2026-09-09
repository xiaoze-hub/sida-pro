"""兼容 shim(KI-039 第二阶段, 2026-09-09): Redis Streams 已下沉 `src/db/streams.py`。"""

from src.db.streams import *  # noqa: F401,F403
from src.db.streams import STREAM_KLINE_BACKFILL, publish_kline_backfill  # noqa: F401
