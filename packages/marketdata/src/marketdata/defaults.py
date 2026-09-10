"""端口的内置默认实现:静态配置 + 内存指标(供独立使用与测试)。"""

from __future__ import annotations

import collections
import threading
import time

from marketdata.ports import SourceConfig


class StaticConfigProvider:
    """从一个 {datatype: [SourceConfig]} 字典提供配置。"""

    def __init__(self, mapping: dict[str, list[SourceConfig]]):
        self._mapping = mapping

    def sources_for(self, datatype: str, market: str | None) -> list[SourceConfig]:
        srcs = [s for s in self._mapping.get(datatype, []) if s.enabled]
        return sorted(srcs, key=lambda s: s.priority)


# 延迟 EWMA 平滑系数(2026-09-10 C1): 越大越重近期样本;
# 0.3 兼顾"近期主导"与"不被单次抖动带偏"(对标 OpenTerminal tracked() 的滚动延迟)。
_EWMA_ALPHA = 0.3


class _Metrics:
    """单 vendor 的滚动统计(最近 100 次),对齐 orchestrator._Metrics。

    2026-09-10 (C1): 新增延迟 EWMA —— p50 对"持续变慢"不敏感(一半样本都慢才会动),
    EWMA 每次样本都按 alpha 递推, 更适合做心跳条上的"当前延迟"读数。失败样本一并计入
    (超时/报错耗时应体现在健康读数里)。
    """

    def __init__(self):
        self.window: collections.deque = collections.deque(maxlen=100)
        self.last_error = ""
        self.last_success_at = 0.0
        self.ewma_latency_ms: float | None = None

    def record(self, ok: bool, latency_ms: int, error: str = "") -> None:
        self.window.append((ok, latency_ms))
        if latency_ms is not None and latency_ms >= 0:
            if self.ewma_latency_ms is None:
                self.ewma_latency_ms = float(latency_ms)
            else:
                self.ewma_latency_ms = _EWMA_ALPHA * latency_ms + (1 - _EWMA_ALPHA) * self.ewma_latency_ms
        if ok:
            self.last_success_at = time.time()
        elif error:
            self.last_error = error

    def snapshot(self) -> dict:
        total = len(self.window)
        ewma = None if self.ewma_latency_ms is None else round(self.ewma_latency_ms)
        if total == 0:
            return {"count": 0, "success_rate": None, "p50_latency_ms": None,
                    "ewma_latency_ms": ewma,
                    "last_error": self.last_error, "last_success_at": self.last_success_at}
        success = sum(1 for ok, _ in self.window if ok)
        lat = sorted(v for _, v in self.window)
        return {
            "count": total,
            "success_rate": round(success / total, 3),
            "p50_latency_ms": lat[len(lat) // 2],
            "ewma_latency_ms": ewma,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
        }


class InMemoryMetricsSink:
    """内存指标沉淀(不落库)。health via snapshot()。"""

    def __init__(self):
        self._by_vendor: dict[str, _Metrics] = {}
        self._lock = threading.Lock()

    def record(self, *, vendor: str, datatype: str, market: str | None,
               ok: bool, count: int, latency_ms: int, error: str = "") -> None:
        with self._lock:
            self._by_vendor.setdefault(vendor, _Metrics()).record(ok, latency_ms, error)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {name: m.snapshot() for name, m in self._by_vendor.items()}
