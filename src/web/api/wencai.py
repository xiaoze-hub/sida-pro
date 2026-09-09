"""问财选股 API 端点(薄壳, KI-039 第二阶段, 2026-09-09)。

纯函数 `run_wencai` 已下沉 `src/collectors/wencai.py`(core 侧不再依赖 web);
本模块只保留 FastAPI 路由。`from src.web.api.wencai import run_wencai` 仍可用。
"""

import logging

from src.collectors.wencai import run_wencai  # noqa: F401  (兼容旧导入)

logger = logging.getLogger(__name__)

# ── FastAPI 路由(fastapi 缺失时模块仍可导入, 纯函数不受影响) ──
try:
    from fastapi import APIRouter, Query

    router = APIRouter(tags=["wencai"])

    @router.get("")
    def api_wencai(query: str = Query(..., description="问财自然语言选股条件(URL 编码)")) -> dict:
        return run_wencai(query)

except Exception as e:  # pragma: no cover - fastapi 不在环境时仅跳过路由
    logger.warning(f"[wencai] fastapi 不可用, 跳过路由注册: {e}")
    router = None
