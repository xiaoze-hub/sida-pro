"""APM 轻量追踪 (P1 稳定性, 2026-09-18)。

目标: **低开销**, 不引入 OpenTelemetry 重依赖。能力:

1. 请求级 trace_id 生成/透传(与 ``src.core.log_context`` 共用 contextvar)
2. 耗时统计: DB 查询 / HTTP 外部调用 / LLM 调用
3. 慢操作告警: 默认 >1s 打 WARNING + 计入 metrics
4. 输出: 结构化日志 + Prometheus(复用 health.py 指标面, 未装 prometheus 时静默)

启用/集成点:
- ``install_db_tracing(engine)``: SQLAlchemy cursor 事件(每条 SQL 计时)
- ``install_httpx_tracing()``: 包装 httpx.Client/AsyncClient.send
- ``trace_llm()`` / ``record_op()``: 业务侧手工埋点(ai_client 已接)

开销控制:
- 采样率可配 ``APM_SAMPLE_RATE``(默认 1.0; 生产可降到 0.1)
- 未采样到的 span 只做一次随机数比较, 不计时不格式化
- 慢阈值 ``APM_SLOW_MS`` 默认 1000ms
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────────────
def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def apm_slow_threshold_ms() -> float:
    """慢操作阈值(毫秒), 默认 1000。"""
    return _env_float("APM_SLOW_MS", 1000.0)


def apm_sample_rate() -> float:
    """采样率 [0,1], 默认 1.0。"""
    r = _env_float("APM_SAMPLE_RATE", 1.0)
    return min(1.0, max(0.0, r))


def apm_enabled() -> bool:
    """总开关, ``APM_ENABLED=0`` 时全部跳过。默认开。"""
    return os.environ.get("APM_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


# ── trace_id ──────────────────────────────────────────────────────────────
_trace_id_var: ContextVar[str] = ContextVar("apm_trace_id", default="")


def new_trace_id() -> str:
    """生成请求级 trace_id (16 hex, 短、够用、低碰撞)。"""
    return uuid.uuid4().hex[:16]


def get_trace_id() -> str:
    return _trace_id_var.get() or ""


def set_trace_id(trace_id: str | None) -> str:
    """绑定当前上下文的 trace_id; 空则生成新的。返回实际值。"""
    tid = (trace_id or "").strip() or new_trace_id()
    _trace_id_var.set(tid)
    return tid


def clear_trace_id() -> None:
    _trace_id_var.set("")


@contextmanager
def trace_scope(trace_id: str | None = None) -> Iterator[str]:
    """绑定 trace_id 的作用域(请求入口/后台任务入口用)。"""
    tid = set_trace_id(trace_id)
    try:
        yield tid
    finally:
        clear_trace_id()


# ── 聚合计数(进程内, 供 /health 与日志) ─────────────────────────────────
class _Counters:
    __slots__ = ("lock", "by_kind")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_kind: dict[str, dict[str, float]] = {}

    def bump(self, kind: str, duration_ms: float, slow: bool, error: bool) -> None:
        with self.lock:
            c = self.by_kind.setdefault(
                kind, {"count": 0.0, "total_ms": 0.0, "slow": 0.0, "errors": 0.0}
            )
            c["count"] += 1
            c["total_ms"] += duration_ms
            if slow:
                c["slow"] += 1
            if error:
                c["errors"] += 1

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self.lock:
            out: dict[str, dict[str, float]] = {}
            for k, v in self.by_kind.items():
                count = v["count"] or 1.0
                out[k] = {
                    "count": v["count"],
                    "avg_ms": round(v["total_ms"] / count, 2),
                    "slow": v["slow"],
                    "errors": v["errors"],
                }
            return out


_counters = _Counters()


def get_apm_stats() -> dict[str, Any]:
    return {
        "enabled": apm_enabled(),
        "sample_rate": apm_sample_rate(),
        "slow_ms": apm_slow_threshold_ms(),
        "trace_id": get_trace_id(),
        "ops": _counters.snapshot(),
    }


# ── Prometheus 桥接(懒加载, 失败静默) ────────────────────────────────────
_op_histogram: Any = None
_op_counter: Any = None
_metric_lock = threading.Lock()


def _ensure_metrics() -> None:
    global _op_histogram, _op_counter
    if _op_histogram is not None or _op_counter is not None:
        return
    with _metric_lock:
        if _op_histogram is not None or _op_counter is not None:
            return
        try:
            from prometheus_client import Counter, Histogram

            _op_histogram = Histogram(
                "sida_apm_op_duration_seconds",
                "APM operation duration by kind (db/http/llm)",
                ["kind"],
                buckets=(0.005, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            )
            _op_counter = Counter(
                "sida_apm_ops_total",
                "APM operation count by kind and result",
                ["kind", "result"],  # result: ok|error|slow
            )
        except Exception:  # noqa: BLE001 — prometheus 未装
            pass


def _observe_metric(kind: str, duration_s: float, *, slow: bool, error: bool) -> None:
    try:
        _ensure_metrics()
        if _op_histogram is not None:
            _op_histogram.labels(kind=kind).observe(duration_s)
        if _op_counter is not None:
            result = "error" if error else ("slow" if slow else "ok")
            _op_counter.labels(kind=kind, result=result).inc()
    except Exception:  # noqa: BLE001
        pass


# ── 核心: record_op / span ────────────────────────────────────────────────
def record_op(
    kind: str,
    name: str,
    duration_ms: float,
    *,
    error: bool = False,
    extra: dict | None = None,
) -> None:
    """记录一次操作耗时。**必须极快**, 失败静默。

    Args:
        kind: db / http / llm / custom
        name: 操作名(表名/主机/模型), 仅用于日志, 不进 metric label(防高基数)
        duration_ms: 毫秒
        error: 是否失败
        extra: 附加字段(并入日志)
    """
    if not apm_enabled():
        return
    try:
        slow = duration_ms >= apm_slow_threshold_ms()
        _counters.bump(kind, duration_ms, slow, error)
        _observe_metric(kind, duration_ms / 1000.0, slow=slow, error=error)

        if slow or error:
            tid = get_trace_id()
            payload = {
                "apm_kind": kind,
                "apm_name": name,
                "duration_ms": round(duration_ms, 1),
                "trace_id": tid,
                "error": error,
            }
            if extra:
                payload.update(extra)
            level = logging.WARNING if (slow or error) else logging.INFO
            # 用独立 logger, 避免污染业务 logger 的 module 标签语义
            logger.log(
                level,
                "APM slow_op" if slow and not error else "APM op",
                extra={"tags": payload},
            )
            # 同时打一条可读中文(方便 Loki/控制台直接看)
            kind_cn = {"db": "DB查询", "http": "HTTP调用", "llm": "LLM调用"}.get(kind, kind)
            msg = (
                f"慢{'错误' if error else ''}操作: {kind_cn} {name} "
                f"耗时 {duration_ms:.0f}ms"
            )
            if error:
                logger.error(msg)
            elif slow:
                logger.warning(msg)
    except Exception:  # noqa: BLE001 — APM 绝不影响业务
        pass


@contextmanager
def span(kind: str, name: str = "", **extra: Any) -> Iterator[None]:
    """计时上下文管理器。

    用法::

        with span("db", "select users"):
            ...
    """
    if not apm_enabled() or apm_sample_rate() < 1.0 and random.random() >= apm_sample_rate():
        yield
        return
    t0 = time.perf_counter()
    err = False
    try:
        yield
    except Exception:
        err = True
        raise
    finally:
        record_op(kind, name, (time.perf_counter() - t0) * 1000.0, error=err, extra=extra or None)


# ── DB 追踪: SQLAlchemy 事件 ─────────────────────────────────────────────
_db_traced_engines: set[int] = set()


def _sql_op_kind(statement: str) -> str:
    """粗分 SQL 类型(仅用于日志 name, 不做精确解析)。"""
    s = (statement or "").lstrip()[:20].upper()
    for prefix in ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "BEGIN", "COMMIT"):
        if s.startswith(prefix):
            return prefix.lower()
    return "sql"


def install_db_tracing(engine) -> None:
    """给 engine 挂 before/after_cursor_execute 计时。

    低开销: 只在 conn.info 写一个 float; 采样外直接跳过记录。
    幂等: 同一 engine 只装一次。
    """
    if not apm_enabled():
        return
    eid = id(engine)
    if eid in _db_traced_engines:
        return
    try:
        from sqlalchemy import event
    except ImportError:
        return

    @event.listens_for(engine, "before_cursor_execute")
    def _apm_before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if apm_sample_rate() < 1.0 and random.random() >= apm_sample_rate():
            conn.info.pop("_apm_t0", None)
            return
        conn.info["_apm_t0"] = time.perf_counter()
        conn.info["_apm_sql"] = statement

    @event.listens_for(engine, "after_cursor_execute")
    def _apm_after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        t0 = conn.info.pop("_apm_t0", None)
        if t0 is None:
            return
        sql = conn.info.pop("_apm_sql", statement)
        duration_ms = (time.perf_counter() - t0) * 1000.0
        op = _sql_op_kind(sql)
        # 表名粗提取(第一个 FROM/INTO/UPDATE 后的 token), 失败用 op
        name = op
        try:
            up = sql.upper()
            for kw in ("FROM ", "INTO ", "UPDATE "):
                idx = up.find(kw)
                if idx >= 0:
                    name = sql[idx + len(kw):].split()[0].strip().strip('"`')
                    break
        except Exception:  # noqa: BLE001
            pass
        record_op("db", f"{op}:{name}"[:60], duration_ms)

    # 失败也要记(conn 异常时 after 不触发)
    @event.listens_for(engine, "handle_error")
    def _apm_error(exception_context):  # noqa: ANN001
        try:
            conn = exception_context.connection
            t0 = conn.info.pop("_apm_t0", None) if conn else None
            if t0 is not None:
                duration_ms = (time.perf_counter() - t0) * 1000.0
                record_op("db", "error", duration_ms, error=True)
        except Exception:  # noqa: BLE001
            pass

    _db_traced_engines.add(eid)


# ── HTTP 追踪: httpx 包装 ────────────────────────────────────────────────
_httpx_installed = False


def _http_host(url: Any) -> str:
    try:
        return (url.host or "unknown")[:60]
    except Exception:  # noqa: BLE001
        return "unknown"


def install_httpx_tracing() -> None:
    """包装 httpx.Client.send / AsyncClient.send 做耗时统计。

    幂等; 未装 httpx 时静默。采样在 span 内做。
    """
    global _httpx_installed
    if _httpx_installed or not apm_enabled():
        return
    try:
        import httpx
    except ImportError:
        return

    # ── sync ──
    if not getattr(httpx.Client, "_apm_wrapped", False):
        _orig_send = httpx.Client.send

        def _sync_send(self, request, **kwargs):  # noqa: ANN001
            host = _http_host(request.url)
            with span("http", host, method=request.method):
                return _orig_send(self, request, **kwargs)

        httpx.Client.send = _sync_send  # type: ignore[method-assign]
        httpx.Client._apm_wrapped = True  # type: ignore[attr-defined]

    # ── async ──
    if not getattr(httpx.AsyncClient, "_apm_wrapped", False):
        _orig_asend = httpx.AsyncClient.send

        async def _async_send(self, request, **kwargs):  # noqa: ANN001
            host = _http_host(request.url)
            with span("http", host, method=request.method):
                return await _orig_asend(self, request, **kwargs)

        httpx.AsyncClient.send = _async_send  # type: ignore[method-assign]
        httpx.AsyncClient._apm_wrapped = True  # type: ignore[attr-defined]

    _httpx_installed = True


# ── LLM 追踪: 显式埋点(ai_client 调用) ─────────────────────────────────
@contextmanager
def trace_llm(model: str = "", scene: str = "") -> Iterator[None]:
    """LLM 调用计时(供 ai_client._call_with_retry 使用)。"""
    name = f"{scene or 'llm'}:{model or '?'}"[:80]
    with span("llm", name, model=model or "", scene=scene or ""):
        yield


def install_all(engines: list | None = None) -> None:
    """一次性安装: httpx + 指定 engines 的 DB 追踪。启动时调用。"""
    if not apm_enabled():
        return
    install_httpx_tracing()
    if engines:
        for eng in engines:
            try:
                install_db_tracing(eng)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"APM DB 追踪挂载失败(忽略): {e}")
