"""管理员状态拓扑：只读快照与显式主动自检分离，不回传连接串和凭据。"""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse

from src.web.api.auth import require_owner
from src.web.api._scope import scoped
from src.web.database import get_db
from src.web.models import AIModel, DataSource, NotifyChannel, User

router = APIRouter()


def _state(value: str | None) -> str:
    return {"ok": "ok", "connected": "ok", "down": "fail", "fail": "fail",
            "degraded": "slow", "slow": "slow", "disabled": "disabled"}.get(value or "", "unknown")


def build_status_snapshot(payload: dict[str, Any], sources: list[dict[str, Any]],
                          model_count: int, channel_count: int) -> dict[str, Any]:
    """只投影允许展示的字段；配置存在和历史成功均不等同于当前连通。"""
    components = payload.get("components") or {}
    nodes: list[dict[str, Any]] = []

    def node(key: str, label: str, kind: str, state: str, detail: str,
             connected: bool = False, latency_ms: int | None = None,
             items: list[dict[str, Any]] | None = None) -> None:
        nodes.append({"id": key, "label": label, "kind": kind, "status": state,
                      "detail": detail, "connected": connected, "latency_ms": latency_ms,
                      "items": items or []})

    node("api", "SIDA 应用服务", "api", "ok", "状态接口响应正常", True)
    for key, label in (("database", "数据库"), ("redis", "Redis 缓存"),
                       ("scheduler", "任务调度"), ("forecast_engine", "预测引擎")):
        value = components.get(key) or {}
        state = _state(value.get("status"))
        connected = state == "ok"
        detail = {"ok": "最近探测连通", "fail": "最近探测未连通", "slow": "服务降级或部分异常",
                  "disabled": "当前未启用", "unknown": "未取得检测结果"}[state]
        if key == "database" and value.get("dialect"):
            detail = str(value["dialect"]) + " · " + detail
        if key == "scheduler" and value.get("leader_state") == "standby":
            state, connected, detail = "standby", False, "当前进程待命；未验证主调度进程"
        if key == "scheduler" and isinstance(value.get("running"), int) and state == "ok":
            detail = f"{value['running']} 个调度器运行中"
        if key == "forecast_engine":
            detail = "可选模块 · " + detail
        node(key, label, key, state, detail, connected, value.get("latency_ms"))

    cache = components.get("biz_cache") or {}
    cache_state = "ok" if cache.get("redis") == "ok" else "slow" if "l1_entries" in cache else "unknown"
    node("biz_cache", "业务缓存", "cache", cache_state,
         f"L1 内存 {cache['l1_entries']} 项 · L2 " + ("连通" if cache_state == "ok" else "未连通")
         if "l1_entries" in cache else "未取得缓存状态", cache_state == "ok")
    # 累计统计仅作为详情，不作为流动连线的连通证据。
    source_items = [{"key": f"ds:{source['id']}", "name": source["name"],
                     "status": _state(source.get("status")), "note": "历史调用统计，未主动探测"}
                    for source in sources]
    node("datasource", "行情与数据源", "datasource", "unknown" if sources else "unconfigured",
         f"{len(sources)} 个已启用 · 点击查看历史状态" if sources else "尚未配置启用的数据源", items=source_items)
    node("ai", "AI 分析模型", "ai", "unknown" if model_count else "unconfigured",
         f"{model_count} 个平台模型 · 尚未检测连接" if model_count else "尚未配置平台模型")
    node("notify", "通知渠道", "notify", "unknown" if channel_count else "unconfigured",
         f"{channel_count} 个当前账号/共享渠道 · 尚未检测连接" if channel_count else "当前账号尚无启用渠道")
    links = [("datasource", "api"), ("api", "database"), ("api", "biz_cache"),
             ("biz_cache", "redis"), ("scheduler", "api"), ("api", "ai"),
             ("api", "forecast_engine"), ("api", "notify")]
    by_id = {item["id"]: item for item in nodes}
    edges = [{"id": f"{start}-{end}", "source": start, "target": end,
              "connected": by_id[start]["connected"] and by_id[end]["connected"]}
             for start, end in links]
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "version": payload.get("version", "unknown"),
            "nodes": nodes, "edges": edges}


@router.get("/system")
async def system_status(user: User = Depends(require_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
    from src.core.source_health import summarize_source
    from src.web.api.health import _check_components

    try:
        payload = await asyncio.wait_for(_check_components(), timeout=5.0)
    except Exception:
        payload = {"components": {}}
    try:
        sources = [summarize_source(row) for row in db.query(DataSource).filter(DataSource.enabled.is_(True)).all()]
        model_count = db.query(AIModel).count()
        channel_count = scoped(db.query(NotifyChannel), user).filter(NotifyChannel.enabled.is_(True)).count()
    except Exception:
        # 数据库故障不能把“未取到配置”报告为“尚未配置”。
        result = build_status_snapshot(payload, [], 0, 0)
        for item in result["nodes"]:
            if item["id"] in ("datasource", "ai", "notify"):
                item.update(status="unknown", detail="配置读取失败，未取得状态")
        return result
    return build_status_snapshot(payload, sources, model_count, channel_count)


@router.get("/system/stream")
async def system_status_stream(user: User = Depends(require_owner), db: Session = Depends(get_db)) -> StreamingResponse:
    from src.core.source_health import summarize_source
    from src.web.api.health import _check_components

    configuration_failed = False
    try:
        sources = [summarize_source(row) for row in db.query(DataSource).filter(DataSource.enabled.is_(True)).all()]
        models = db.query(AIModel).count()
        channels = scoped(db.query(NotifyChannel), user).filter(NotifyChannel.enabled.is_(True)).count()
    except Exception:
        sources, models, channels = [], 0, 0
        configuration_failed = True

    async def events() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        initial = build_status_snapshot({}, sources, models, channels)
        initial_nodes = [node for node in initial["nodes"] if node["id"] in ("api", "datasource", "ai", "notify")]
        if configuration_failed:
            for node in initial_nodes:
                if node["id"] != "api":
                    node.update(status="unknown", detail="配置读取失败，未取得状态")
        done_ids = {node["id"] for node in initial_nodes}

        def packet(nodes: list[dict[str, Any]]) -> dict[str, Any]:
            return {"nodes": nodes, "done_count": len(done_ids), "total": 9,
                    "checked_at": datetime.now(timezone.utc).isoformat()}

        def publish(key: str, component: dict[str, Any]) -> None:
            if key not in ("database", "redis", "biz_cache", "scheduler", "forecast_engine"):
                return
            nodes = build_status_snapshot({"components": {key: component}}, [], 0, 0)["nodes"]
            done_ids.add(key)
            queue.put_nowait(packet([node for node in nodes if node["id"] == key]))

        async def collect() -> None:
            try:
                payload = await asyncio.wait_for(_check_components(publish), timeout=5.0)
                queue.put_nowait({**packet([]), "done": True, "version": payload.get("version", "unknown")})
            except Exception:
                queue.put_nowait({"error": "部分模块状态读取失败；未更新项保留上次记录"})

        def encode(data: dict[str, Any]) -> str:
            return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"

        yield encode(packet(initial_nodes))
        task = asyncio.create_task(collect())
        try:
            while True:
                event = await queue.get()
                yield encode(event)
                if event.get("done") or event.get("error"):
                    break
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/selfcheck")
async def selfcheck_list(list_only: bool = Query(False, alias="list"), user: User = Depends(require_owner),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    from src.core.selfcheck import list_selfcheck_items

    if list_only:
        return {"items": list_selfcheck_items(db=db, user_id=user.id)}
    raise HTTPException(405, "主动自检请使用 POST")


class SelfCheckRequest(BaseModel):
    keys: list[str] | None = Field(default=None, max_length=80)
    notify_send: bool = False


@router.post("/selfcheck")
async def selfcheck(body: SelfCheckRequest, user: User = Depends(require_owner),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    from src.core.selfcheck import run_selfcheck

    if body.keys is not None and (not body.keys or not all(body.keys)):
        raise HTTPException(400, "自检项不能为空且最多 80 项")
    return await run_selfcheck(db=db, keys=body.keys, notify_send=body.notify_send, user_id=user.id)
