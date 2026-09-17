"""Loki 日志聚合推送 (P1 稳定性, 2026-09-18)。

设计:
- 从环境变量 ``LOKI_URL`` 读取推送地址; **未配置时本模块完全不影响现有日志**
  (handler 不挂载 / 全部 no-op), 控制台与 DB handler 行为不变。
- 批量推送: 攒满 ``BATCH_SIZE`` 条 或 距上次推送超过 ``FLUSH_INTERVAL_S`` 秒。
- 标签固定 ``job=sida`` + ``level`` + ``module``(logger name 截断, 防高基数)。
- 推送走独立 daemon 线程 + 同步 httpx, **绝不阻塞业务线程**(emit 只入队)。
- 失败静默丢弃(最多计数), 不重试风暴、不递归打日志。

Loki Push API:
    POST {LOKI_URL}/loki/api/v1/push
    {"streams":[{"stream":{"job":"sida","level":"INFO","module":"src.web"},
                 "values":[["<ns>", "line"], ...]}]}
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

# 批量阈值
BATCH_SIZE = 100
FLUSH_INTERVAL_S = 10.0
# 单次推送超时(秒) —— Loki 挂了也不能拖垮日志线程
PUSH_TIMEOUT_S = 3.0
# 队列上限, 满了丢最老的(宁丢日志不涨内存)
MAX_QUEUE = 5_000
# module 标签最长长度(防 label 基数爆炸)
_MODULE_LABEL_MAX = 40


def loki_enabled() -> bool:
    """是否配置了 LOKI_URL。"""
    return bool(os.environ.get("LOKI_URL", "").strip())


def _loki_url() -> str:
    raw = os.environ.get("LOKI_URL", "").strip().rstrip("/")
    if not raw:
        return ""
    # 允许直接给完整 push 端点, 也允许只给 base
    if raw.endswith("/push"):
        return raw
    return f"{raw}/loki/api/v1/push"


def _module_label(name: str) -> str:
    """logger name → Loki module 标签(截断 + 空值兜底)。"""
    n = (name or "unknown").strip() or "unknown"
    if len(n) > _MODULE_LABEL_MAX:
        n = n[:_MODULE_LABEL_MAX]
    return n


class LokiLogHandler(logging.Handler):
    """批量缓冲 Loki handler: emit 只入队, 后台线程推送。

    挂到 root logger 上与现有 console/DB handler 并存; 未配置 LOKI_URL 时
    ``setup_logging`` 不会创建本 handler。
    """

    def __init__(self, level: int = logging.INFO):
        super().__init__(level)
        self._queue: list[tuple[int, dict[str, str], str]] = []  # (ns, labels, line)
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._pushed = 0
        self._dropped = 0
        self._errors = 0
        self._last_error = ""
        self._closed = False
        self._start_timer()

    # ── 入队(业务线程, 必须极快) ──
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            labels = {
                "job": "sida",
                "level": record.levelname,
                "module": _module_label(getattr(record, "name", "")),
            }
            # Loki 时间戳: 纳秒字符串
            ns = int(record.created * 1_000_000_000)
            with self._lock:
                if len(self._queue) >= MAX_QUEUE:
                    overflow = len(self._queue) - MAX_QUEUE + 1
                    del self._queue[:overflow]
                    self._dropped += overflow
                self._queue.append((ns, labels, msg))
                should_flush = len(self._queue) >= BATCH_SIZE
            if should_flush:
                # 后台刷, 不在调用线程做网络 IO
                threading.Thread(
                    target=self._flush_now, name="loki-flush", daemon=True
                ).start()
        except Exception:
            pass  # 日志路径绝不抛

    # ── 定时刷 ──
    def _start_timer(self) -> None:
        if self._closed:
            return
        self._timer = threading.Timer(FLUSH_INTERVAL_S, self._timed_flush)
        self._timer.daemon = True
        self._timer.start()

    def _timed_flush(self) -> None:
        try:
            self._flush_now()
        finally:
            self._start_timer()

    def _flush_now(self) -> None:
        with self._lock:
            if not self._queue:
                return
            batch = self._queue[:]
            self._queue.clear()
        self._push(batch)

    # ── 推送 ──
    def _push(self, batch: list[tuple[int, dict[str, str], str]]) -> None:
        url = _loki_url()
        if not url or not batch:
            return
        # 按 labels 分组成 streams
        streams: dict[str, dict] = {}
        for ns, labels, line in batch:
            key = json.dumps(labels, sort_keys=True, ensure_ascii=False)
            entry = streams.setdefault(key, {"stream": labels, "values": []})
            entry["values"].append([str(ns), line])
        payload = {"streams": list(streams.values())}
        try:
            import httpx

            resp = httpx.post(
                url,
                json=payload,
                timeout=PUSH_TIMEOUT_S,
                headers={"Content-Type": "application/json"},
            )
            # Loki 对部分成功返回 204; 4xx/5xx 记错误但不重试(避免风暴)
            if resp.status_code >= 400:
                self._errors += 1
                self._last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            else:
                self._pushed += len(batch)
        except Exception as e:  # noqa: BLE001 — 网络失败静默
            self._errors += 1
            self._last_error = str(e)[:200]

    def close(self) -> None:
        self._closed = True
        if self._timer:
            self._timer.cancel()
        try:
            self._flush_now()  # 退出前尽量把残留推出去
        except Exception:
            pass
        super().close()

    def stats(self) -> dict:
        with self._lock:
            pending = len(self._queue)
        return {
            "enabled": True,
            "url": _loki_url(),
            "pending": pending,
            "pushed": self._pushed,
            "dropped": self._dropped,
            "errors": self._errors,
            "last_error": self._last_error,
        }


_ACTIVE: LokiLogHandler | None = None


def get_loki_handler() -> LokiLogHandler | None:
    return _ACTIVE


def get_loki_stats() -> dict:
    """供 /health 暴露; 未配置时返回 enabled=False。"""
    h = _ACTIVE
    if h is None or not loki_enabled():
        return {"enabled": False, "reason": "LOKI_URL not set"}
    return h.stats()


def ensure_loki_handler(root_logger: logging.Logger | None = None) -> LokiLogHandler | None:
    """幂等挂载 Loki handler 到 root logger。

    - 未配置 LOKI_URL → 返回 None, 不挂任何东西。
    - 已挂载 → 复用现有 handler, 不重复挂。
    - setup_logging 里 reload 时会先 close 旧的再调本函数。
    """
    global _ACTIVE
    if not loki_enabled():
        _ACTIVE = None
        return None

    root = root_logger or logging.getLogger()

    # 已挂载则复用
    for h in root.handlers:
        if isinstance(h, LokiLogHandler):
            _ACTIVE = h
            return h

    handler = LokiLogHandler(level=logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s")
    )
    root.addHandler(handler)
    _ACTIVE = handler
    try:
        logging.getLogger(__name__).info(
            f"Loki 日志聚合已启用: {_loki_url()} (batch={BATCH_SIZE}/{FLUSH_INTERVAL_S}s)"
        )
    except Exception:
        pass
    return handler


def shutdown_loki() -> None:
    """进程退出前刷盘(可选调用)。"""
    global _ACTIVE
    h = _ACTIVE
    if h is not None:
        try:
            h.close()
        except Exception:
            pass
        _ACTIVE = None
