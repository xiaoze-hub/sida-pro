"""数据源管理 API"""

import logging
import time
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.web.database import get_db
from src.web.models import DataSource

logger = logging.getLogger(__name__)

router = APIRouter()


# 数据源类型说明
TYPE_LABELS = {
    "news": "新闻资讯",
    "kline": "K线数据",
    "capital_flow": "资金流向",
    "quote": "实时行情",
    "events": "事件日历",
    "flash_news": "快讯",
    "fundamentals": "基本面",
    "dragon_tiger": "龙虎榜",
    "margin": "融资融券",
    "shareholders": "股东户数",
    "dividend": "分红",
    "northbound": "北向资金",
    "board_capital_flow": "板块资金",
    "market_capital_flow": "大盘资金",
}


class DataSourceCreate(BaseModel):
    name: str
    type: str  # news / kline / capital_flow / quote / events / chart / flash_news
    provider: str
    config: dict = {}
    enabled: bool = True
    priority: int = 0
    supports_batch: bool = False
    test_symbols: list[str] = []


class DataSourceUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    provider: str | None = None
    config: dict | None = None
    enabled: bool | None = None
    priority: int | None = None
    supports_batch: bool | None = None
    test_symbols: list[str] | None = None


class DataSourceResponse(BaseModel):
    id: int
    name: str
    type: str
    type_label: str = ""
    provider: str
    config: dict
    enabled: bool
    priority: int
    supports_batch: bool = False
    test_symbols: list[str] = []

    class Config:
        from_attributes = True


# 已接入 marketdata 新引擎的数据类型(随各类型逐步迁移扩充)
_ENGINE_ATTACHED_TYPES = {
    "news",
    "quote",
    "kline",
    "capital_flow",
    "events",
    "flash_news",
    "fundamentals",
    "dragon_tiger",
    "margin",
    "shareholders",
    "dividend",
    "northbound",
    "board_capital_flow",
    "market_capital_flow",
}


# 2026-09-01 审计修复: DataSource 模型新增的 5 个健康累计列在已存在的旧库上,
# 因 PostgreSQL create_all 不会给已有表自动加列而缺失, 导致 list/get 接口
# SELECT * 报 UndefinedColumn → 500(前端设置页整页空白)。查询前幂等补齐一次。
_HEALTH_COLUMNS_ENSURED = False


def _ensure_health_columns(db: Session) -> None:
    global _HEALTH_COLUMNS_ENSURED
    if _HEALTH_COLUMNS_ENSURED:
        return
    try:
        from sqlalchemy import inspect as sa_inspect, text
        from src.core.source_health import HEALTH_COLUMNS

        existing = {c["name"] for c in sa_inspect(db.bind).get_columns("data_sources")}
        missing = [c for c in HEALTH_COLUMNS if c not in existing]
        if missing:
            for col in missing:
                db.execute(text(f"ALTER TABLE data_sources ADD COLUMN {col} {HEALTH_COLUMNS[col]}"))
            db.commit()
        _HEALTH_COLUMNS_ENSURED = True
    except Exception as e:  # noqa: BLE001
        logger.warning("data_sources 健康列补齐失败(下次请求重试): %s", e)


def _is_orphan(type_: str, provider: str) -> bool:
    """判定 (type, provider) 是否为孤儿数据源:不在包内引擎 vendor 集合、也不在当前 seed 列表里。

    与 server.reconcile_data_sources 的孤儿判定保持一致(legal = 包内集合 | seed 集合)。
    """
    from marketdata import PACKAGE_VENDORS_BY_TYPE
    from server import _seed_providers_by_type

    legal = PACKAGE_VENDORS_BY_TYPE.get(type_, frozenset()) | _seed_providers_by_type().get(type_, set())
    return provider not in legal


def _to_response(source: DataSource, health_map: dict | None = None) -> dict:
    """转换为响应格式。health_map: {provider: 指标快照};缺失则 health=None。"""
    health = (health_map or {}).get(source.provider)
    raw_keys = (source.config or {}).get("api_keys", [])
    if isinstance(raw_keys, str):
        key_count = 1 if raw_keys.strip() else 0
    elif isinstance(raw_keys, list):
        key_count = sum(1 for key in raw_keys if isinstance(key, str) and key.strip())
    else:
        key_count = 0
    return {
        "id": source.id,
        "name": source.name,
        "type": source.type,
        "type_label": TYPE_LABELS.get(source.type, source.type),
        "provider": source.provider,
        "key_count": key_count,
        "config": source.config or {},
        "enabled": source.enabled,
        "priority": source.priority,
        "supports_batch": source.supports_batch or False,
        "test_symbols": source.test_symbols or [],
        "engine_attached": source.type in _ENGINE_ATTACHED_TYPES,
        "health": health,
        "is_orphan": _is_orphan(source.type, source.provider),
    }


@router.get("")
def list_datasources(type: str | None = None, db: Session = Depends(get_db)):
    """获取数据源列表，可按类型筛选"""
    _ensure_health_columns(db)
    query = db.query(DataSource)
    if type:
        query = query.filter(DataSource.type == type)
    sources = query.order_by(DataSource.type, DataSource.priority, DataSource.id).all()
    from src.core.marketdata_client import get_market_data
    health_map = get_market_data().health()
    return [_to_response(s, health_map) for s in sources]


@router.get("/types")
def get_datasource_types():
    """获取数据源类型列表"""
    return [{"type": k, "label": v} for k, v in TYPE_LABELS.items()]


# ---------------------------------------------------------------------------
# 设计稿 v2.1 §12: 数据源健康检查(L4 事件图标灰显依据)
# ---------------------------------------------------------------------------
# ⚠️ 路由顺序: /health 与 /health/{id} 必须注册在 /{source_id} **之前**,
#    否则 "health" 会被 /{source_id} 捕获成 id 导致 422。
@router.get("/health")
def get_datasources_health(ids: str | None = None, refresh: bool = False):
    """L4 事件数据源健康状态(前端每 60s 轮询一次)。

    设计稿 §5.3 的 5 个事件图标缺位时灰显, 依据来自这里:

        拆 / ⚠撤   → .tck
        🛡托/🔒压   → .img
        涨         → wencai(thsdk)
        我         → shadow(交割单)

    Args:
        ids:     逗号分隔的逻辑源 id(tck/img/wencai/shadow); 不传 → 全部
        refresh: True 跳过 30s 缓存强制重查

    Returns:
        {checked_at, items: [{id, name, status, last_check_at, detail, icons}]}
        status ∈ connected / degraded / down / unknown
    """
    from src.core.source_health import check_all

    wanted = None
    if ids:
        wanted = [s.strip() for s in ids.split(",") if s.strip()]
    try:
        items = check_all(wanted, use_cache=not refresh)
    except Exception as e:  # noqa: BLE001
        logger.warning("数据源健康检查失败: %s", e)
        return {"checked_at": time.time(), "items": []}
    return {"checked_at": time.time(), "items": items}


@router.get("/health/data-sources")
def get_configured_sources_health(db: Session = Depends(get_db)):
    """通用 data_sources 表的健康状态(按累计成功/失败推断), 与 4 个逻辑源互补。

    分工:
      - `/health`               → 4 个 L4 逻辑源(**探测式**, 决定事件图标灰显)
      - `/health/data-sources`  → 配置源(**累计统计式**, 看源本身好没好)

    ⚠️ 从未调用过的源 → `unknown`, 不冒充 connected(详见 source_health.infer_status_from_stats)。
    """
    from src.core.source_health import summarize_source

    # 老库可能没有这 5 列(2026-09-01 新增) → 补齐后再查(幂等, 全进程一次)
    _ensure_health_columns(db)

    try:
        rows = db.query(DataSource).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("查询 data_sources 失败: %s", e)
        return {"checked_at": time.time(), "items": []}

    return {"checked_at": time.time(), "items": [summarize_source(r) for r in rows]}


@router.get("/health/{source_id}")
def get_datasource_health(source_id: str, refresh: bool = False):
    """单个逻辑源的健康状态; 未知 id 返回 status=unknown(不抛 404, 前端按灰显处理)。

    ⚠️ 必须排在 `/health/data-sources` **之后**: 否则 "data-sources" 会被当成
    逻辑源 id 匹配到这里(返回单源结构, 前端拿不到 items)。
    """
    from src.core.source_health import check_source

    return check_source(source_id, use_cache=not refresh)


@router.post("/reset-to-seed")
def reset_datasources_to_seed(db: Session = Depends(get_db)):
    """数据源表温和对账:补齐缺失的预置默认 + 删除孤儿行,保留用户有效自定义/凭证。"""
    from server import reconcile_data_sources

    summary = reconcile_data_sources(db)
    logger.info(f"数据源手动对账完成: {summary}")
    return summary


@router.get("/{source_id}")
def get_datasource(source_id: int, db: Session = Depends(get_db)):
    """获取单个数据源"""
    _ensure_health_columns(db)
    source = db.query(DataSource).filter(DataSource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return _to_response(source)


@router.post("")
def create_datasource(data: DataSourceCreate, db: Session = Depends(get_db)):
    """创建数据源"""
    # v2.1 §11 补丁(2026-09-01): P0-B 自愈一致性 — update/delete/create 也补列防御,
    # 老库即使缺 5 健康列, 任何 ORM 操作也绝不 500(归 get 操作之后, 留作一致性兜底)
    _ensure_health_columns(db)
    source = DataSource(
        name=data.name,
        type=data.type,
        provider=data.provider,
        config=data.config,
        enabled=data.enabled,
        priority=data.priority,
        supports_batch=data.supports_batch,
        test_symbols=data.test_symbols,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    logger.info(f"创建数据源: {source.name} ({source.provider})")
    return _to_response(source)


@router.put("/{source_id}")
def update_datasource(
    source_id: int, data: DataSourceUpdate, db: Session = Depends(get_db)
):
    """更新数据源"""
    _ensure_health_columns(db)
    source = db.query(DataSource).filter(DataSource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(source, key, value)

    db.commit()
    db.refresh(source)
    logger.info(f"更新数据源: {source.name}")
    return _to_response(source)


@router.delete("/{source_id}")
def delete_datasource(source_id: int, db: Session = Depends(get_db)):
    """删除数据源"""
    _ensure_health_columns(db)
    source = db.query(DataSource).filter(DataSource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")

    db.delete(source)
    db.commit()
    logger.info(f"删除数据源: {source.name}")
    return {"ok": True, "message": f"已删除 {source.name}"}


@router.post("/{source_id}/test")
async def test_datasource(source_id: int, db: Session = Depends(get_db)):
    """测试数据源连接"""
    source = db.query(DataSource).filter(DataSource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")

    from src.core.data_collector import get_collector_manager

    manager = get_collector_manager()
    manager.clear_logs()

    result = await manager.test_source(source)

    # 不用 success / data 作为顶层字段,避免被 ResponseWrapperMiddleware 当成业务响应
    # 拆解后导致 metadata 丢失(详见 src/web/response.py:59 的特殊分支)。
    return {
        "test_passed": result.success,
        "source_name": source.name,
        "source_type": source.type,
        "type_label": TYPE_LABELS.get(source.type, source.type),
        "provider": source.provider,
        "supports_batch": source.supports_batch or False,
        "test_symbols": source.test_symbols or [],
        "count": result.count,
        "duration_ms": result.duration_ms,
        "error": result.error,
        "items": result.data,
        "logs": manager.get_logs(),
    }
