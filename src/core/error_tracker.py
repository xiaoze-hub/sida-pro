"""轻量错误追踪 (2026-08-21)。

生产环境全局 try/except 包住请求, 未捕获异常只进本地日志, 用户报障要靠截图。
不引入 Sentry 等 SaaS(国内可达性差 + 需注册), 做自研轻量方案:

1. capture_exception(exc, context): 把结构化错误 JSONL 落盘到
   <DATA_DIR>/error_events.jsonl (默认 /app/data), 含 ts/type/message/traceback/context。
   带去重: 同一 (type, message) 指纹 5 分钟内只落一条。
2. install_error_tracker(app): FastAPI 中间件, 捕获未处理异常 → capture_exception
   + 原样 re-raise(不吞, 交给 Starlette 正常返回 500)。
3. 聚合告警: 同一指纹 10 分钟内出现 >=3 次 → 写一条 Notification
   (category=system, level=error)。防轰炸: 同指纹每小时最多发 1 条。
4. recent_errors(limit): 返回最近事件列表(供未来 UI 用)。

约束:
- 性能敏感路径零开销: 无异常时仅 middleware pass, capture_exception 只在 except 里调用。
- 文件写入 / 通知失败绝不抛异常, 不影响主流程(全 try/except)。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── 路径与阈值(可用 env / configure 覆盖) ─────────────────────────────
_DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
_FILE = os.environ.get("ERROR_TRACKER_FILE", os.path.join(_DATA_DIR, "error_events.jsonl"))

DEDUPE_SECONDS = 300          # 同指纹 JSONL 落盘去重窗口: 5 分钟
AGG_WINDOW_SECONDS = 600      # 聚合统计窗口: 10 分钟
AGG_THRESHOLD = 3             # 窗口内出现次数阈值
NOTIFY_COOLDOWN_SECONDS = 3600  # 同指纹告警防轰炸: 每小时最多 1 条

TRACEBACK_LIMIT = 2000        # traceback 截断长度
SAMPLE_TRACEBACK_LIMIT = 300  # 告警 body 里样例 traceback 长度

_lock = threading.Lock()
# fp -> 最近一次 JSONL 落盘 time.monotonic() (去重用)
_last_jsonl_write: dict[tuple[str, str], float] = {}
# fp -> 窗口内出现时间戳列表 time.monotonic() (聚合计数用)
_recent_occurrences: dict[tuple[str, str], list[float]] = {}
# fp -> 最近一次通知时间 time.monotonic() (防轰炸用)
_last_notify: dict[tuple[str, str], float] = {}


def _fingerprint(exc: BaseException) -> tuple[str, str]:
    """同 (type, message) 视为同一错误指纹。message 归一化(去掉首尾空白)。"""
    return (type(exc).__name__, (str(exc) or "").strip())


def _truncate(s: str | None, limit: int) -> str:
    s = s or ""
    if len(s) <= limit:
        return s
    return s[:limit] + "…(截断)"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _traceback_str(exc: BaseException) -> str:
    try:
        return "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    except Exception:
        return f"{type(exc).__name__}: {exc}"


def _log_file() -> str:
    return os.environ.get("ERROR_TRACKER_FILE", _FILE)


def _write_event(record: dict) -> bool:
    """JSONL 追加写入。任何失败都吞掉, 不影响主流程。"""
    try:
        path = Path(_log_file())
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        logger.warning("[error_tracker] JSONL 写入失败", exc_info=True)
        return False


def _build_record(exc: BaseException, context: dict | None) -> dict:
    return {
        "ts": _utc_iso(),
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": _truncate(_traceback_str(exc), TRACEBACK_LIMIT),
        "context": context or {},
    }


def _notify_high_frequency(
    fp: tuple[str, str], count: int, sample_tb: str
) -> None:
    """写一条站内 Notification。任何失败都吞掉。"""
    try:
        from src.core.notify_center import push_notification

        push_notification(
            title=f"[错误追踪] 高频异常: {fp[0]}",
            body=(
                f"错误类型: {fp[0]}\n"
                f"最近消息: {fp[1]}\n"
                f"10 分钟内出现 {count} 次\n"
                f"样例 traceback:\n{_truncate(sample_tb, SAMPLE_TRACEBACK_LIMIT)}"
            ),
            category="system",
            level="error",
            source="error_tracker",
            link="",
            also_push=False,  # 只写站内通知, 不触发外发渠道轰炸
        )
    except Exception:
        logger.warning("[error_tracker] 写告警通知失败", exc_info=True)


def capture_exception(exc: BaseException, context: dict | None = None) -> bool:
    """记录一条结构化错误事件(JSONL 落盘) + 触发聚合告警。

    返回是否实际落盘(去重后)。任何异常都不会向上抛。
    """
    try:
        fp = _fingerprint(exc)
        now = time.monotonic()

        with _lock:
            # 1) 记录本次出现(聚合计数用, 不因落盘去重而漏计)
            occs = _recent_occurrences.setdefault(fp, [])
            occs.append(now)
            _recent_occurrences[fp] = [t for t in occs if now - t <= AGG_WINDOW_SECONDS]
            count = len(_recent_occurrences[fp])

            # 2) JSONL 落盘去重: 同指纹 5 分钟内只写一次
            last_w = _last_jsonl_write.get(fp)
            if last_w is None or (now - last_w) > DEDUPE_SECONDS:
                _last_jsonl_write[fp] = now
                should_write = True
            else:
                should_write = False

            # 3) 聚合告警: 10 分钟内 >=3 次, 同指纹每小时最多一条
            should_notify = False
            if count >= AGG_THRESHOLD:
                last_n = _last_notify.get(fp)
                if last_n is None or (now - last_n) > NOTIFY_COOLDOWN_SECONDS:
                    _last_notify[fp] = now
                    should_notify = True

        wrote = False
        sample_tb = ""
        if should_write:
            sample_tb = _traceback_str(exc)
            wrote = _write_event(_build_record(exc, context))

        if should_notify:
            if not sample_tb:
                sample_tb = _traceback_str(exc)
            _notify_high_frequency(fp, count, sample_tb)

        return wrote
    except Exception:
        logger.warning("[error_tracker] capture_exception 处理异常失败", exc_info=True)
        return False


def recent_errors(limit: int = 50) -> list[dict]:
    """返回最近落盘的事件列表(按落盘顺序逆序, 最新在前)。供未来 UI 用。"""
    records: list[dict] = []
    try:
        path = Path(_log_file())
        if not path.exists():
            return records
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        logger.warning("[error_tracker] 读取事件文件失败", exc_info=True)
        return records
    return records[-limit:][::-1]


def install_error_tracker(app) -> None:
    """FastAPI 中间件: 捕获未处理异常 → capture_exception + 原样 re-raise(不吞)。

    放在中间件链合适位置。理想是尽可能贴近路由(内层)以捕获路由/处理器抛出的
    未处理异常, 并且原样向上抛, 由 Starlette 正常返回 500。
    """

    @app.middleware("http")
    async def _error_tracker_middleware(request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - 我们要捕获一切未处理异常
            try:
                capture_exception(
                    exc,
                    {
                        "path": request.url.path,
                        "method": request.method,
                    },
                )
            except Exception:
                pass
            raise  # 不吞, 继续原异常传播

    return app


def _clear_state() -> None:
    """清空进程内去重/聚合/防轰炸状态(测试隔离用)。"""
    with _lock:
        _last_jsonl_write.clear()
        _recent_occurrences.clear()
        _last_notify.clear()


def configure(
    *,
    file_path: str | None = None,
    data_dir: str | None = None,
    reset_state: bool = False,
) -> None:
    """测试/自定义: 覆盖落盘路径并可选清空进程内状态。"""
    global _FILE
    if file_path:
        _FILE = file_path
    elif data_dir:
        _FILE = os.path.join(data_dir, "error_events.jsonl")
    if reset_state:
        _clear_state()
