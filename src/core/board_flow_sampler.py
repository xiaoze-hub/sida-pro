"""板块资金采样落库(2026-10-14, 资金类落库收口)。

审计定性: `/api/market-data/board-capital-flow` **完全现拉**同花顺行业/概念资金
(marketdata.board_capital_flow), 仅用 biz_cache 的 stale 备份兜源故障 —— 上游一挂
整块缺失。本模块仿 `src/core/market_flow_sampler.py` 写 `market_flow_snapshots`
的成熟先例, 把板块资金按 1~5min 采样落一条进 `board_flow_snapshots`(迁移 m191):

- 写: `write_snapshot()` 落一行; `try_write_async()` 后台线程 + 进程内节流(默认 180s,
  落在 1~5min 区间), 失败静默不阻断主流程; `collect_once()` 供 scheduler 独立采样
  (页面不开也落库, 与 market_flow_sampler 同动机)。
- 读: `read_fresh()` 返回够新且在交易时段内的最新一行(payload=整块 items), 否则 None
  —— 调用方据此『够新读库、过旧回源 + stale 标注』。

口径与 `/api/market-data/board-capital-flow` 完全一致(同一 vendor + 同字段映射)。
单位: 金额=亿(同花顺源口径), 仅资金面参考, 禁用于主力意图判定(AGENTS 红线)。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from sqlalchemy import text

from src.db.dialect import is_postgres

logger = logging.getLogger(__name__)

_TABLE = "board_flow_snapshots"
_BOARD_TYPES = ("industry", "concept")

# 采样节流(进程内): 前端高频轮询下同一 board_type 每 1~5min 只落一条。
_SNAPSHOT_INTERVAL_S = 180.0
_snapshot_lock = threading.Lock()
_snapshot_last_write_ts: dict[str, float] = {}

# 读侧新鲜度阈值(秒): 环境变量覆盖, 非法/非正回落默认 300。
_FRESH_SEC_DEFAULT = 300
_FRESH_ENV = "BOARD_FLOW_SNAPSHOT_FRESH_SEC"


def board_to_item(b) -> dict:
    """vendor 板块对象 → 前端契约 item(与接口实时分支同字段, 单一映射入口)。

    接受带同名属性的对象或 dict; 缺失字段一律 None(不编造), 不补 0。
    """
    get = (lambda k, d=None: b.get(k, d)) if isinstance(b, dict) else (lambda k, d=None: getattr(b, k, d))
    return {
        "board_name": get("board_name"),
        "board_type": get("board_type"),
        "index_value": get("index_value"),
        "change_pct": get("change_pct"),
        "inflow": get("inflow"),
        "outflow": get("outflow"),
        "net_inflow": get("net_inflow"),
        "stock_count": get("stock_count"),
        "leader_name": get("leader_name"),
        "leader_change_pct": get("leader_change_pct"),
        "leader_price": get("leader_price"),
        "rank": get("rank"),
    }


def _fresh_sec() -> int:
    raw = os.environ.get(_FRESH_ENV)
    if raw:
        try:
            v = int(float(raw))
            if v > 0:
                return v
        except (TypeError, ValueError):
            logger.debug("%s 非法(%r), 用默认 %ss", _FRESH_ENV, raw, _FRESH_SEC_DEFAULT)
    return _FRESH_SEC_DEFAULT


def _age_sql() -> str:
    """方言安全的「行龄(秒)」SQL: 用 DB 端 now 与 ts 比较(ts 由 DB 默认时间戳写入)。"""
    if is_postgres():
        return "EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - ts))"
    return "(julianday('now') - julianday(ts)) * 86400.0"


def write_snapshot(board_type: str, items: list[dict]) -> bool:
    """落一条板块资金快照。失败静默返回 False(采样/接口都不抛)。"""
    if not items:
        return False
    try:
        from src.db.session import engine

        with engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {_TABLE} (board_type, item_count, payload) "
                    "VALUES (:bt, :n, :p)"
                ),
                {
                    "bt": board_type,
                    "n": len(items),
                    "p": json.dumps(items, ensure_ascii=False, default=str),
                },
            )
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("板块资金快照写库失败(静默): %s", e)
        return False


def try_write_async(board_type: str, items: list[dict]) -> None:
    """后台线程写一条快照(节流 180s, 失败静默不阻断主流程)。

    先例: market_data._try_write_snapshot_async 写 market_flow_snapshots。
    """
    if not items:
        return
    now = time.monotonic()
    with _snapshot_lock:
        last = _snapshot_last_write_ts.get(board_type, 0.0)
        if now - last < _SNAPSHOT_INTERVAL_S:
            return  # 节流窗口内, 跳过
        _snapshot_last_write_ts[board_type] = now
    threading.Thread(
        target=write_snapshot,
        args=(board_type, list(items)),
        name=f"board-flow-snapshot-{board_type}",
        daemon=True,
    ).start()


def read_fresh(board_type: str) -> dict | None:
    """读最新一条板块资金快照; 仅「够新 ∩ 交易时段」时返回, 否则 None(回退实时链路)。

    返回: {snapshot_ts, snapshot_age_sec, items}; payload 缺失/非列表 → None(不猜)。
    """
    try:
        from src.core.quote_snapshots import in_trading_window

        if not in_trading_window():
            return None
    except Exception as e:  # noqa: BLE001
        logger.debug("交易时段判定不可用, 板块资金回退实时链路: %s", e)
        return None
    try:
        from src.db.session import engine

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT ts, payload, {_age_sql()} AS age_sec "
                    f"FROM {_TABLE} WHERE board_type = :bt ORDER BY ts DESC LIMIT 1"
                ),
                {"bt": board_type},
            ).fetchone()
    except Exception as e:  # noqa: BLE001
        logger.debug("板块资金快照读库失败(回退实时链路): %s", e)
        return None
    if row is None:
        return None
    try:
        age = float(row[2]) if row[2] is not None else None
    except (TypeError, ValueError):
        age = None
    if age is None or age > _fresh_sec() or age < -5:
        return None  # 过旧/算不出 → 不猜, 回源
    raw = row[1]
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(items, list) or not items:
        return None
    ts_val = row[0]
    ts_str = ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val)
    return {
        "snapshot_ts": ts_str,
        "snapshot_age_sec": int(max(0, round(age))),
        "items": items,
    }


def collect_once(now=None) -> dict:
    """采样一次(供 scheduler): 非交易时段跳过; 取数/写库失败静默(不抛)。"""
    from src.core.quote_snapshots import in_trading_window

    if not in_trading_window(now):
        return {"ok": True, "skipped": True, "reason": "非交易时段"}
    try:
        from src.core.marketdata_client import get_market_data

        md = get_market_data()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "skipped": False, "error": f"取数不可用: {e}"}
    written: dict[str, int] = {}
    for bt in _BOARD_TYPES:
        try:
            boards = md.board_capital_flow(board_type=bt) or []
        except Exception as e:  # noqa: BLE001
            logger.debug("板块资金采样[%s]取数失败: %s", bt, e)
            boards = []
        items = [board_to_item(b) for b in boards]
        written[bt] = len(items) if write_snapshot(bt, items) else 0
    return {"ok": True, "skipped": False, "written": written}
