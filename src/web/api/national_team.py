"""国家队 / 机构持仓 API（2026-11-18）：汇金 · 证金 · 社保 · 险资 · 外汇局三平台的季度持仓与环比。

端点
----
    GET  /api/national-team/overview?report_date=&kind=float|total
    GET  /api/national-team/holdings?report_date=&kind=&holder=&q=&sort=&desc=&limit=&offset=
    GET  /api/national-team/changes?report_date=&kind=&limit=
    POST /api/national-team/refresh?mode=targeted|scan   （owner 触发采集，幂等）

`mode=targeted`（默认）按 `national_team_holder_registry` 名单 `in (...)` 刷新最近一期，秒级；
`mode=scan` 走全市场扫描（一期两表各约 5.6 万行，3-6 分钟）—— 社保各组合 / 险资各产品账户
这类事先不可枚举的名字只有扫描才能认全，通常由调度器在新报告期出现时自动触发。

数据来源与口径见 `src/core/national_team.py` 模块 docstring（东财十大股东 / 十大流通股东，
按股东全称精确过滤；季报口径；只含进入前十大的持仓；**不含宽基 ETF**）。

红线
----
- 表为空 → `available=false` + reason，**不编造**任何一行；
- 上游失败 → 采集 receipt 里 `ok=false` + errors，页面照实展示「部分失败」；
- 缓存只存成功结果，读库失败不写缓存。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from src.web.cache.biz_cache import biz_cache
from src.web.api.auth import require_owner
from src.web.database import engine

logger = logging.getLogger(__name__)

router = APIRouter()

_CACHE_TTL = 600  # 季报口径（一年四更），10 分钟缓存足够；refresh 后由 file 层重建


def _cached(key: str, producer) -> dict[str, Any]:
    hit = biz_cache.get_json(key)
    if isinstance(hit, dict):
        return hit
    out = producer()
    try:
        biz_cache.set_json(key, out, ttl=_CACHE_TTL)
    except Exception as exc:  # noqa: BLE001 — 缓存故障不影响读数
        logger.warning("[national-team] 缓存写入失败: %s", exc)
    return out


@router.get("")
def national_team_usage():
    """裸路径自说明。"""
    return {
        "endpoint": "/api/national-team",
        "usage": {
            "GET /overview": "机构组汇总（汇金系/证金/社保/险资/外汇局：只数·账户数·市值·环比变动只数）",
            "GET /holdings": "持仓明细（holder 传组 key：huijin/csf/ssb/insurance/safe，或具体 holder_key）",
            "GET /changes": "环比变动（按机构组+股票聚合：新进/加仓/减仓/退出）",
            "POST /refresh": "手动触发采集（owner）：mode=targeted(默认,秒级) / scan(全市场,数分钟)",
        },
        "caliber": {
            "source": "eastmoney_datacenter RPT_F10_EH_FREEHOLDERS / RPT_F10_EH_HOLDERS",
            "coverage": "只含进入十大股东/十大流通股东的持仓；不含宽基 ETF；A+H 未拆分；险资含控股股东自有账户",
        },
    }


@router.get("/overview")
def national_team_overview(
    report_date: str | None = Query(None, description="YYYY-MM-DD；缺省最新报告期"),
    kind: str = Query("float", description="float=十大流通股东, total=十大股东"),
):
    from src.core import national_team as nt

    k = (kind or "float").strip().lower()
    if k not in nt.REPORT_TABLES:
        raise HTTPException(400, f"未知 kind: {kind}（float|total）")

    def _load() -> dict[str, Any]:
        return nt.overview(engine, report_date=report_date, kind=k)

    try:
        return _cached(f"national_team:overview:{report_date or 'latest'}:{k}", _load)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[national-team] overview 失败: %s", exc)
        raise HTTPException(500, f"国家队持仓汇总查询失败: {exc}") from exc


@router.get("/holdings")
def national_team_holdings(
    report_date: str | None = Query(None, description="YYYY-MM-DD；缺省最新报告期"),
    kind: str = Query("float"),
    holder: str | None = Query(None, description="主体 key，all/缺省=全部"),
    q: str | None = Query(None, description="代码或名称关键字"),
    sort: str = Query("market_cap", description="market_cap|hold_num|hold_ratio|stock_code"),
    desc: bool = Query(True),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    from src.core import national_team as nt

    k = (kind or "float").strip().lower()
    if k not in nt.REPORT_TABLES:
        raise HTTPException(400, f"未知 kind: {kind}（float|total）")

    def _load() -> dict[str, Any]:
        return nt.holdings(
            engine,
            report_date=report_date,
            kind=k,
            holder=holder,
            q=q,
            sort=sort,
            desc=desc,
            limit=limit,
            offset=offset,
        )

    key = f"national_team:holdings:{report_date or 'latest'}:{k}:{holder or 'all'}:{q or ''}:{sort}:{int(desc)}:{limit}:{offset}"
    try:
        return _cached(key, _load)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[national-team] holdings 失败: %s", exc)
        raise HTTPException(500, f"国家队持仓明细查询失败: {exc}") from exc


@router.get("/changes")
def national_team_changes(
    report_date: str | None = Query(None, description="YYYY-MM-DD；缺省最新报告期"),
    kind: str = Query("float"),
    limit: int = Query(100, ge=1, le=500),
):
    from src.core import national_team as nt

    k = (kind or "float").strip().lower()
    if k not in nt.REPORT_TABLES:
        raise HTTPException(400, f"未知 kind: {kind}（float|total）")

    def _load() -> dict[str, Any]:
        return nt.changes(engine, report_date=report_date, kind=k, limit=limit)

    try:
        return _cached(f"national_team:changes:{report_date or 'latest'}:{k}:{limit}", _load)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[national-team] changes 失败: %s", exc)
        raise HTTPException(500, f"国家队环比变动查询失败: {exc}") from exc


@router.post("/refresh")
def national_team_refresh(
    periods: str | None = Query(None, description="逗号分隔的 YYYY-MM-DD；缺省最近一期"),
    mode: str = Query("targeted", description="targeted=按名单定向刷新(秒级)；scan=全市场扫描(数分钟)"),
    _owner=Depends(require_owner),
):
    """手动触发一次采集（幂等 upsert）。owner 专属 —— 采集会打上游网络。

    `scan` 期间请求会阻塞数分钟（一期两表各约 5.6 万行），适合补数/新报告期首采；
    日常刷新用默认的 `targeted`。
    """
    from src.core import national_team as nt

    m = (mode or "targeted").strip().lower()
    if m not in {"targeted", "scan"}:
        raise HTTPException(400, f"未知 mode: {mode}（targeted|scan）")
    period_list = [p.strip() for p in (periods or "").split(",") if p.strip()] or None
    try:
        receipt = nt.sync(engine, periods=period_list, mode=m)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[national-team] 采集失败: %s", exc)
        raise HTTPException(502, f"国家队持仓采集失败: {exc}") from exc
    # 采集成功后清掉读缓存（只清本模块的确定性 key，不 clear() 整池）
    for k in nt.REPORT_TABLES:
        for prefix in (
            f"national_team:overview:latest:{k}",
            f"national_team:changes:latest:{k}:100",
        ):
            try:
                biz_cache.delete(prefix)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[national-team] 缓存清理失败(%s): %s", prefix, exc)
    return receipt


__all__ = ["router"]
