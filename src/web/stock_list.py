"""兼容 shim(KI-039 第二阶段, 2026-09-09): 股票列表缓存/搜索已下沉 `src/collectors/stock_list.py`。

`from src.web.stock_list import get_stock_list/refresh_stock_list` 保持可用;
新代码请直接 `from src.collectors.stock_list import ...`。
"""

from src.collectors.stock_list import *  # noqa: F401,F403
from src.collectors.stock_list import (  # noqa: F401
    get_stock_list,
    refresh_stock_list,
)
