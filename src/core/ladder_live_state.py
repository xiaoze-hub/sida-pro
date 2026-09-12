# -*- coding: utf-8 -*-
"""连板梯队盘中态 Redis 读写(v0.5.87, spec §4)。

键: ladder_live:<yyyymmdd> (json state) / ladder_live:<yyyymmdd>:meta (json meta); TTL 6h(收盘后自毁兜底)。
fail-soft: cli 为 None 或任何异常 → save 返回 False / load 返回空, 不抛(调用方降级 finalized)。
连接构造照 md_redis_cache: REDIS_URL + 0.3s 超时 + ping。
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_TTL_SEC = 6 * 3600
_EMPTY_META = {"last_ok": None, "stale": False, "rounds_failed": 0}


def client():
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        return None
    try:
        import redis

        cli = redis.Redis.from_url(url, socket_connect_timeout=0.3,
                                   socket_timeout=0.3, decode_responses=True)
        cli.ping()
        return cli
    except Exception as e:  # noqa: BLE001
        logger.warning("ladder-live: Redis 不可用, 降级 finalized: %s", e)
        return None


def load_state(cli, date: str) -> dict:
    if cli is None:
        return {}
    try:
        raw = cli.get(f"ladder_live:{date}")
        return json.loads(raw) if raw else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("ladder-live: 读态失败: %s", e)
        return {}


def load_meta(cli, date: str) -> dict:
    if cli is None:
        return dict(_EMPTY_META)
    try:
        raw = cli.get(f"ladder_live:{date}:meta")
        return {**_EMPTY_META, **(json.loads(raw) if raw else {})}
    except Exception as e:  # noqa: BLE001
        logger.warning("ladder-live: 读 meta 失败: %s", e)
        return dict(_EMPTY_META)


def save_state(cli, date: str, state: dict, meta: dict) -> bool:
    if cli is None:
        return False
    try:
        cli.set(f"ladder_live:{date}", json.dumps(state, ensure_ascii=False), ex=_TTL_SEC)
        cli.set(f"ladder_live:{date}:meta", json.dumps(meta, ensure_ascii=False), ex=_TTL_SEC)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("ladder-live: 写态失败: %s", e)
        return False


def save_day(cli, date: str, day: dict) -> bool:
    """存预渲染的盘中列(live_day), 供 /ladder 直接读, 不必每请求重拉 TDX。"""
    if cli is None:
        return False
    try:
        cli.set(f"ladder_live:{date}:day", json.dumps(day, ensure_ascii=False), ex=_TTL_SEC)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("ladder-live: 写 live_day 失败: %s", e)
        return False


def load_day(cli, date: str) -> dict | None:
    if cli is None:
        return None
    try:
        raw = cli.get(f"ladder_live:{date}:day")
        return json.loads(raw) if raw else None
    except Exception as e:  # noqa: BLE001
        logger.warning("ladder-live: 读 live_day 失败: %s", e)
        return None
