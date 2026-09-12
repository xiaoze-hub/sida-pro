"""长任务作业框架(2026-09-12, 借鉴 tick-stock-panel 的 pipeline job 设计)。

我们原来的样子: 扫描/AI 批量/回填都是"cron 或手工 POST 起一个后台线程", 跑没跑成、
跑到哪一步、有没有第二次点击又起一个 —— 界面上全都看不见。

借来的四条(每条都对应我们踩过的坑):
- **单飞复用**: 同类任务已有活跃(pending/running)作业时, 再触发直接返回**同一个 job_id**,
  不并发起第二个(并发扫描会撞数据源限流, 也会互踩落库)。
- **按"进度停滞"判卡死, 不按总时长**: 慢网络下长任务不该被误杀; 超过 STALL_SECONDS
  没有任何进度更新才判 failed(孤儿线程/重启遗留的 running 行也会被这条自愈)。
- **落库**: 状态存 `app_jobs` 表, 重启后仍能看到"上一次跑成没有", 不是只在内存里。
- **可取消(协作式)**: 置 cancelled 并由任务在分块边界自查; 不假装能中断阻塞中的 IO。
"""
from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

ACTIVE = ("pending", "running")
TERMINAL = ("succeeded", "failed", "cancelled")
STALL_SECONDS = 900          # 15 分钟无进度更新 → 判卡死(回填/扫描单步都远快于此)
KEEP_RECENT = 40             # 列表默认回看的近期任务数

_UPSERT = """
INSERT INTO app_jobs (id, kind, label, status, progress, stage, message, error,
                      created_at, updated_at, finished_at)
VALUES (:id, :kind, :label, :status, :progress, :stage, :message, :error,
        :created_at, :updated_at, :finished_at)
ON CONFLICT(id) DO UPDATE SET
  status = EXCLUDED.status, progress = EXCLUDED.progress, stage = EXCLUDED.stage,
  message = EXCLUDED.message, error = EXCLUDED.error,
  updated_at = EXCLUDED.updated_at, finished_at = EXCLUDED.finished_at
"""


def _now() -> datetime:
    return datetime.now()


def _row_to_dict(r: Any) -> dict:
    return {
        "id": r[0], "kind": r[1], "label": r[2], "status": r[3], "progress": r[4],
        "stage": r[5] or "", "message": r[6] or "", "error": r[7] or "",
        "created_at": str(r[8] or ""), "updated_at": str(r[9] or ""),
        "finished_at": str(r[10] or "") if r[10] else None,
    }


_COLS = ("id, kind, label, status, progress, stage, message, error, "
         "created_at, updated_at, finished_at")


class JobStore:
    """`app_jobs` 表的薄封装。线程安全靠单条 SQL upsert + 进程内锁兜底。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ── 写 ──
    def _save(self, row: dict) -> None:
        from sqlalchemy import text

        from src.db.session import engine

        with engine.begin() as conn:
            conn.execute(text(_UPSERT), row)

    def create(self, kind: str, label: str = "", *, single_flight: bool = True) -> tuple[str, bool]:
        """建任务; 同类已有活跃任务时**复用其 id**(返回 is_new=False)。"""
        with self._lock:
            self.reap_stale()
            if single_flight:
                found = self.active(kind=kind)
                if found:
                    return found[0]["id"], False
            now = _now()
            row = {"id": secrets.token_hex(5), "kind": kind, "label": label or kind,
                   "status": "pending", "progress": 0, "stage": "queued", "message": "",
                   "error": "", "created_at": now, "updated_at": now, "finished_at": None}
            self._save(row)
            return row["id"], True

    def update(self, job_id: str, **fields: Any) -> None:
        from sqlalchemy import text

        from src.db.session import engine

        with engine.begin() as conn:
            cur = conn.execute(text(f"SELECT {_COLS} FROM app_jobs WHERE id = :i"), {"i": job_id}).fetchone()  # noqa: S608 列名为本模块常量
            if cur is None:
                return
            row = _row_to_dict(cur)
            row.update({k: v for k, v in fields.items() if k in row})
            row["updated_at"] = _now()
            self._save(row)

    def start(self, job_id: str, stage: str = "running") -> None:
        self.update(job_id, status="running", stage=stage, progress=0)

    def progress(self, job_id: str, pct: int, stage: str = "", message: str = "") -> None:
        fields: dict[str, Any] = {"progress": max(0, min(100, int(pct)))}
        if stage:
            fields["stage"] = stage
        if message:
            fields["message"] = message[:500]
        self.update(job_id, **fields)

    def succeed(self, job_id: str, message: str = "") -> None:
        self.update(job_id, status="succeeded", progress=100, message=message[:500],
                    finished_at=_now())

    def fail(self, job_id: str, error: str) -> None:
        self.update(job_id, status="failed", error=str(error)[:1000], finished_at=_now())

    def cancel(self, job_id: str, reason: str = "已取消") -> bool:
        """协作式取消: 置 cancelled, 任务在下一个分块边界自查后退出。"""
        from sqlalchemy import text

        from src.db.session import engine

        with engine.begin() as conn:
            cur = conn.execute(text("SELECT status FROM app_jobs WHERE id = :i"), {"i": job_id}).fetchone()
            if not cur or cur[0] not in ACTIVE:
                return False
        self.update(job_id, status="cancelled", message=reason, finished_at=_now())
        return True

    def reap_stale(self, stall_seconds: int = STALL_SECONDS) -> int:
        """running 但长时间无进度更新 → 判失败(重启遗留的孤儿行也走这条自愈)。"""
        from sqlalchemy import text

        from src.db.session import engine

        cutoff = _now() - timedelta(seconds=stall_seconds)
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id FROM app_jobs WHERE status = 'running' AND updated_at < :c"),
                {"c": cutoff},
            ).fetchall()
        for (jid,) in rows:
            self.fail(jid, f"超过 {stall_seconds // 60} 分钟无进度更新, 判定为卡死")
        return len(rows)

    # ── 读 ──
    def _query(self, where: str, params: dict, order: str, limit: int) -> list[dict]:
        from sqlalchemy import text

        from src.db.session import engine

        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text(f"SELECT {_COLS} FROM app_jobs {where} {order} LIMIT :lim"),  # noqa: S608
                    {**params, "lim": limit},
                ).fetchall()
        except Exception as e:  # noqa: BLE001 表尚未创建(新库/未迁移) → 空列表, 不抛
            logger.warning("app_jobs 查询失败: %s", e)
            return []
        return [_row_to_dict(r) for r in rows]

    def get(self, job_id: str) -> dict | None:
        rows = self._query("WHERE id = :i", {"i": job_id}, "", 1)
        return rows[0] if rows else None

    def active(self, kind: str | None = None) -> list[dict]:
        where, params = ("WHERE status IN ('pending','running')", {})
        if kind:
            where += " AND kind = :k"
            params["k"] = kind
        return self._query(where, params, "ORDER BY created_at", 20)

    def recent(self, limit: int = KEEP_RECENT) -> list[dict]:
        return list(reversed(self._query("", {}, "ORDER BY created_at", max(1, min(limit, 200)))))

    def is_cancelled(self, job_id: str) -> bool:
        row = self.get(job_id)
        return bool(row and row["status"] == "cancelled")


jobs = JobStore()
