"""通知实时推送槽(中立层, KI-039 第二阶段, 2026-09-09)。

core 的 `notify_center` 落库后要推给在线 WS 连接, 但 WS Hub 是 Web 层实现。
这里留一个**可注册的推送槽**:

- Web 侧 `src/web/notifications/ws_hub.py` 导入时调用 `register(...)` 注入实现;
- 未注册时(CLI / 单测 / 无 WS 场景)全部 no-op, 与旧行为一致(旧代码靠 ImportError 静默)。

依赖方向: core → core(本模块); web → core(注册)。不再有 core → web。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_broadcast: Callable[..., Any] | None = None
_incr_unread: Callable[..., Any] | None = None


def register(
    broadcast: Callable[..., Any] | None,
    incr_unread: Callable[..., Any] | None = None,
) -> None:
    """注入实现(幂等; 传 None 可注销, 主要给测试用)。"""
    global _broadcast, _incr_unread
    _broadcast = broadcast
    _incr_unread = incr_unread


def broadcast_notification(
    user_id: str | None, payload: dict, *, category: str | None = None
) -> int:
    """推给该用户的在线 WS 连接; 未注册实现时返回 0。"""
    if _broadcast is None:
        return 0
    try:
        return int(_broadcast(user_id, payload, category=category) or 0)
    except Exception as e:  # noqa: BLE001 - 推送失败绝不影响主流程
        logger.debug("[notify_sink] broadcast 失败: %s", e)
        return 0


def incr_unread(user_id: str | None, n: int = 1) -> int:
    """累加未读数; 未注册实现时返回 0。"""
    if _incr_unread is None or not user_id:
        return 0
    try:
        return int(_incr_unread(user_id, n) or 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("[notify_sink] incr_unread 失败: %s", e)
        return 0
