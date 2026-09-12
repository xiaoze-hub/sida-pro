"""后台任务 API(2026-09-12, 借鉴 tick-stock-panel 的作业面板)。

GET  /api/jobs            → 活跃任务 + 近期任务(含进度/阶段/结果摘要)
GET  /api/jobs/{id}       → 单任务(前端轮询进度用)
POST /api/jobs/{id}/cancel → 协作式取消(置 cancelled, 任务在分块边界自查退出)

口径与实现见 src/core/jobs.py(单飞复用 / 按进度停滞判卡死 / 落库可见)。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from src.core.jobs import jobs

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("")
def list_jobs(limit: int = Query(30, ge=1, le=200)) -> dict:
    """活跃任务 + 近期任务; 读取时顺带做一次停滞自愈。"""
    jobs.reap_stale()
    return {"active": jobs.active(), "recent": jobs.recent(limit)}


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    row = jobs.get(job_id)
    if not row:
        raise HTTPException(404, f"无此任务: {job_id}")
    return row


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    row = jobs.get(job_id)
    if not row:
        raise HTTPException(404, f"无此任务: {job_id}")
    ok = jobs.cancel(job_id)
    if not ok:
        raise HTTPException(409, f"任务已结束(status={row['status']}), 无法取消")
    logger.info("任务已请求取消: %s %s", job_id, row["kind"])
    return {"cancelled": True, "job": jobs.get(job_id)}
