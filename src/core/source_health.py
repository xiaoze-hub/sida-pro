# -*- coding: utf-8 -*-
"""L4 数据源健康检查(设计稿 v2.1 §12)。

## 为什么需要它

设计稿 §5.3 定义了 7 个事件标注图标, 其中 5 个在数据源缺位时要**灰显 + tooltip**,
另 2 个(解套盘位/支撑压力)**直接不显示**。前端靠这张表决定灰不灰:

    const ICON_READY = {
      '拆'        : await checkSource('.tck'),
      '⚠撤'       : await checkSource('.tck'),
      '🛡托/🔒压'  : await checkSource('.img'),
      '涨'        : await checkSource('wencai'),
      '我'        : await checkSource('shadow'),
      '明盘'      : await checkSource('tq_moreinfo'),
    }

即 6 个图标 → 5 个**逻辑数据源**: `tck` / `img` / `wencai` / `shadow` / `tq_moreinfo`。
本模块就是 `checkSource()` 的后端实现, 由 `/api/datasources/health` 暴露。

## ⚠️ 诚实口径(红线)

- `status` 只取 `connected` / `degraded` / `down` / `unknown` 四值
- **不做探测式乐观假设**: 目录没配就是 `down`, 不因为"可能等下会配"报 `connected`
- `degraded` 表示"配了但当前拿不到数据"(目录存在但空、凭据缺失等)
- 检查失败/异常 → `unknown`, 并把原因写进 `detail`, 不静默吞掉
- 缓存 TTL 30s: 前端 60s 轮询一次, 后端不必每次真检查(尤其 wencai 要 import thsdk)

## 四个源的判定口径

| id     | 检查内容                                                         |
|--------|------------------------------------------------------------------|
| tck    | `PANWATCH_TCK_DIR` 已配且目录内有 `.tck` 文件                     |
| img    | `PANWATCH_IMG_DIR` 已配且目录内有 `.img` 文件                     |
| wencai | thsdk 可 import 且 `THS_USERNAME`/`THS_PASSWORD` 均已注入          |
| shadow | 数据库可连且 `paper_trading_trades` 表可查                        |
| tq_moreinfo | TQ 网关(已解析地址或 `TDX_QUANT_URL`)轻探测 `get_stock_list` 可达 |
  (只探单个地址, 不跑全候选扫描; 从未解析过 → degraded, 等首次行情查询触发自动发现)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 状态取值(设计稿 §12 固定四值)
STATUS_CONNECTED = "connected"
STATUS_DEGRADED = "degraded"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"

# 缓存: 前端 60s 轮询, 后端 30s TTL 足够新鲜, 又不必每次真检查(wencai 要 import thsdk)
_HEALTH_CACHE: dict[str, tuple[float, dict]] = {}
_HEALTH_TTL = 30.0

TCK_DIR_ENV = "PANWATCH_TCK_DIR"
IMG_DIR_ENV = "PANWATCH_IMG_DIR"

# data_sources 表的健康累计列(2026-09-01 新增, 与 Hermes 方案合并时保留)。
# 值 = 建列用的 DDL 类型片段, 供 /health/data-sources 在老库上自动补齐。
# ⚠️ 用 SQLite/PG 都吃得下的宽松类型; 布尔/时间列刻意宽松, 避免跨库 DDL 不兼容。
HEALTH_COLUMNS: dict[str, str] = {
    "last_used_at": "TIMESTAMP",
    "last_error_at": "TIMESTAMP",
    "success_count": "INTEGER DEFAULT 0",
    "error_count": "INTEGER DEFAULT 0",
    "last_status": "VARCHAR(32)",
}


def _dir_has_files(env_key: str, suffix: str) -> dict[str, Any]:
    """检查"目录已配 + 有指定后缀文件"这一通用模式。

    返回 {status, detail}:
      - 环境变量未配 / 目录不存在 → down
      - 目录存在但无该后缀文件   → degraded(配了但当前没数据)
      - 有文件                   → connected(附文件数)
      - 目录不可读               → unknown(附原因)
    """
    base = (os.environ.get(env_key) or "").strip()
    if not base:
        return {"status": STATUS_DOWN, "detail": f"未配置环境变量 {env_key}"}
    if not os.path.isdir(base):
        return {"status": STATUS_DOWN, "detail": f"{env_key} 指向的目录不存在: {base}"}
    try:
        names = os.listdir(base)
    except Exception as e:  # noqa: BLE001
        return {"status": STATUS_UNKNOWN, "detail": f"目录不可读: {e}"}
    hits = [n for n in names if n.lower().endswith(suffix)]
    if not hits:
        return {"status": STATUS_DEGRADED,
                "detail": f"{env_key} 已配置但目录内无 {suffix} 文件: {base}"}
    return {"status": STATUS_CONNECTED, "detail": f"{len(hits)} 个 {suffix} 文件"}


def check_tck() -> dict[str, Any]:
    """.tck 逐笔数据源(拆单簇 / 撤单异常 两个图标共用)。"""
    return _dir_has_files(TCK_DIR_ENV, ".tck")


def check_img() -> dict[str, Any]:
    """.img 盘口队列数据源(托盘 / 压盘 图标)。"""
    return _dir_has_files(IMG_DIR_ENV, ".img")


def check_wencai() -> dict[str, Any]:
    """wencai / thsdk 数据源(涨停原因 图标)。

    只检查"能力": thsdk 可 import + 凭据已注入。
    **不发起真实查询** —— 一次 wencai 查询要几秒, 健康检查不能这么重。
    """
    try:
        from src.web.api.wencai import run_wencai  # noqa: F401
    except Exception as e:  # pragma: no cover
        return {"status": STATUS_DOWN, "detail": f"wencai 模块不可用: {e}"}
    try:
        import thsdk  # noqa: F401
    except Exception as e:  # pragma: no cover
        return {"status": STATUS_DOWN, "detail": f"thsdk 未安装: {e}"}
    user = (os.environ.get("THS_USERNAME") or "").strip()
    pwd = (os.environ.get("THS_PASSWORD") or "").strip()
    if not user or not pwd:
        return {"status": STATUS_DEGRADED,
                "detail": "thsdk 可用但 THS_USERNAME/THS_PASSWORD 未注入"}
    return {"status": STATUS_CONNECTED, "detail": "thsdk 可用且凭据已注入"}


def check_shadow() -> dict[str, Any]:
    """shadow 交割单数据源(我的买卖点 图标)。

    检查: DB 可连 + paper_trading_trades 表可查。查不到表 → unknown(附原因)。
    """
    try:
        from src.web.database import SessionLocal
        from src.web.models import PaperTradingTrade
    except Exception as e:  # pragma: no cover
        return {"status": STATUS_UNKNOWN, "detail": f"交割单模型不可用: {e}"}
    try:
        db = SessionLocal()
        try:
            db.query(PaperTradingTrade).limit(1).all()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        return {"status": STATUS_UNKNOWN, "detail": f"数据库不可查询: {e}"}
    return {"status": STATUS_CONNECTED, "detail": "交割单表可查询"}


def check_tq_moreinfo() -> dict[str, Any]:
    """TQ 扩展指标网关(more_info/明盘资金/决策先锋共用链路)。

    只做轻探测, 不发起真实行情查询:
      - `TDX_QUANT_URL` 已配 → 探该地址
      - 否则用 vendor 已缓存的解析地址(首次行情查询时自动发现)
      - 两者都没有 → degraded(诚实: 尚未发现, 不编造 connected)
    超时 2s, 失败 → degraded(配了但当前不通), 异常 → unknown。
    """
    try:
        from marketdata.vendors import tq as _tqmod  # type: ignore
    except Exception as e:  # pragma: no cover
        return {"status": STATUS_DOWN, "detail": f"TQ vendor 不可用: {e}"}
    try:
        env_url = (os.environ.get("TDX_QUANT_URL") or "").strip().rstrip("/") + "/"
        cached = getattr(_tqmod, "_TQ_URL_CACHE", None)
        target = env_url if len(env_url) > 1 else (cached or "")
        if not target:
            return {"status": STATUS_DEGRADED,
                    "detail": "未配置 TDX_QUANT_URL 且尚未自动发现, 等首次行情查询"}
        probe = getattr(_tqmod, "_probe_tq", None)
        if probe is None:  # pragma: no cover
            return {"status": STATUS_UNKNOWN, "detail": "vendor 无探测入口"}
        ok = probe(target, timeout=2.0)
        if ok:
            return {"status": STATUS_CONNECTED, "detail": f"TQ 网关可达: {target}"}
        return {"status": STATUS_DEGRADED, "detail": f"TQ 网关当前不通: {target}"}
    except Exception as e:  # noqa: BLE001
        return {"status": STATUS_UNKNOWN, "detail": f"TQ 探测异常: {e}"}


# 逻辑源定义: id → (展示名, 检查函数, 关联的事件图标)
SOURCE_DEFS: dict[str, dict[str, Any]] = {
    "tck":    {"name": ".tck 逐笔",    "check": check_tck,    "icons": ["拆", "⚠撤"]},
    "img":    {"name": ".img 盘口队列", "check": check_img,    "icons": ["🛡托/🔒压"]},
    "wencai": {"name": "wencai/thsdk", "check": check_wencai, "icons": ["涨"]},
    "shadow": {"name": "shadow 交割单", "check": check_shadow, "icons": ["我"]},
    "tq_moreinfo": {"name": "TQ 扩展指标(明盘/决策先锋)", "check": check_tq_moreinfo, "icons": ["明盘"]},
}


def check_source(source_id: str, use_cache: bool = True) -> dict[str, Any]:
    """检查单个逻辑源。

    Returns:
        {id, name, status, last_check_at, detail, icons}
        未知 source_id → status='unknown'(不抛异常, 前端按灰显处理)。
    """
    now = time.time()
    if use_cache:
        hit = _HEALTH_CACHE.get(source_id)
        if hit and now - hit[0] < _HEALTH_TTL:
            return hit[1]

    out: dict[str, Any] = {
        "id": source_id,
        "name": SOURCE_DEFS.get(source_id, {}).get("name", source_id),
        "icons": SOURCE_DEFS.get(source_id, {}).get("icons", []),
        "status": STATUS_UNKNOWN,
        "last_check_at": now,
        "detail": None,
    }
    fn: Callable[[], dict[str, Any]] | None = SOURCE_DEFS.get(source_id, {}).get("check")
    if fn is None:
        out["detail"] = f"未知数据源: {source_id}"
    else:
        try:
            r = fn() or {}
            out["status"] = r.get("status") or STATUS_UNKNOWN
            out["detail"] = r.get("detail")
        except Exception as e:  # noqa: BLE001
            out["status"] = STATUS_UNKNOWN
            out["detail"] = f"检查异常: {e}"

    if use_cache:
        _HEALTH_CACHE[source_id] = (now, out)
    return out


def check_all(source_ids: list[str] | None = None, use_cache: bool = True) -> list[dict[str, Any]]:
    """批量检查。source_ids 为空 → 检查全部 5 个逻辑源。"""
    ids = source_ids or list(SOURCE_DEFS.keys())
    return [check_source(sid, use_cache=use_cache) for sid in ids]


# ---------------------------------------------------------------------------
# 通用 data_sources 健康推断(与 Hermes 方案合并时保留的累计统计口径)
# ---------------------------------------------------------------------------
def infer_status_from_stats(enabled: bool | None, success_count: int | None,
                            error_count: int | None) -> str:
    """按累计成功/失败次数推断通用数据源状态。

    判定表(严格按"有多少证据说多少话", 不乐观假设):

    | 条件                                   | status    | 说明                     |
    |----------------------------------------|-----------|--------------------------|
    | enabled=False                          | down      | 已被显式停用             |
    | 成功>0 且 失败=0                        | connected | 只成功过                 |
    | 成功>0 且 失败>0                        | degraded  | 时好时坏                 |
    | 成功=0 且 失败>0                        | down      | 只失败过                 |
    | 成功=0 且 失败=0                        | unknown   | **从未调用过, 不编造**   |

    ⚠️ 最后一行的 `unknown` 是刻意的: 一次都没用过的源既不该显示"已连接"
    (会误导前端不灰显), 也不该显示"故障"。
    """
    if enabled is False:
        return STATUS_DOWN
    ok = int(success_count or 0)
    bad = int(error_count or 0)
    if ok > 0 and bad == 0:
        return STATUS_CONNECTED
    if ok > 0 and bad > 0:
        return STATUS_DEGRADED
    if ok == 0 and bad > 0:
        return STATUS_DOWN
    return STATUS_UNKNOWN


def summarize_source(row: Any) -> dict[str, Any]:
    """DataSource ORM 行 → 健康检查项(读不到字段一律按缺失处理, 不报错)。"""
    def _get(key: str, default=None):
        try:
            return getattr(row, key, default)
        except Exception:  # pragma: no cover
            return default

    success = _get("success_count", 0) or 0
    error = _get("error_count", 0) or 0
    last_used = _get("last_used_at")
    last_error = _get("last_error_at")

    # last_check_at = 最近一次调用(成功或失败), 都没有 → None(不补 0)
    stamps = [t for t in (last_used, last_error) if t is not None]
    ts_val = None
    if stamps:
        try:
            ts_val = max(stamps).timestamp()
        except Exception:  # pragma: no cover
            ts_val = None

    return {
        "id": _get("id"),
        "name": _get("name"),
        "type": _get("type"),
        "provider": _get("provider"),
        "enabled": _get("enabled"),
        "status": infer_status_from_stats(_get("enabled"), success, error),
        "success_count": int(success),
        "error_count": int(error),
        "last_used_at": last_used.isoformat() if last_used else None,
        "last_error_at": last_error.isoformat() if last_error else None,
        "last_check_at": ts_val,
        "last_status": _get("last_status"),
    }


def clear_health_cache() -> None:
    """清空缓存(测试隔离用)。"""
    _HEALTH_CACHE.clear()
