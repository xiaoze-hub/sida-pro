# -*- coding: utf-8 -*-
"""vendor 调用统计落库(MetricsSink 装饰器)。

## 为什么需要它

`data_sources.success_count/error_count` 三列早就存在(2026-09-01), 但**全仓库没有一处写入**,
于是唯一活着的健康读数是 vendor 的滚动 EWMA —— 而它在进程内存里, 每次重启归零。
后果: 刚发版去看「数据能力矩阵」, 18 类全是 `未测量`(口径诚实, 但等于没面板)。

本模块把每次 vendor 调用的成/败**累计**落到 DB(跨重启存活), 能力矩阵在 EWMA
样本不足时退回该累计值。`/health/data-sources`(同样依赖这两列)顺带从"永远 unknown"变可用。

## 口径与护栏

- **窗口 vs 累计**: EWMA 是"最近 100 次"的近期视角, 累计是"历史全部"的长期视角,
  两者都会出现在返回里, 由 `basis` 字段标明本次判定用的是哪一个。
- **限流**: 增量先在内存聚合, 距上次落库 ≥`FLUSH_SEC` 才真正 UPDATE, 且用
  `col = col + :delta` 的原子自增(多 worker 并发安全, 无读改写竞态)。
  代价是面板读数最多滞后 `FLUSH_SEC`(30s), 对"这个源此刻好不好"无影响;
  进程退出由 atexit 排空最后一段增量。
- **绝不影响主数据路径**: 落库异常只记 warning, 计数回灌到待写增量, 不丢数也不抛。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

FLUSH_SEC = 30.0          # 增量落库最小间隔(每次调用都 UPDATE 会把写放大到不可接受)


class DbCountingMetricsSink:
    """包一层 InMemoryMetricsSink: 内存快照照旧, 另外把成/败累计落 data_sources。"""

    def __init__(self, inner: Any | None = None, *, flush_sec: float = FLUSH_SEC,
                 session_factory=None) -> None:
        if inner is None:
            from marketdata.defaults import InMemoryMetricsSink

            inner = InMemoryMetricsSink()
        self._inner = inner
        self._flush_sec = flush_sec
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._pending: dict[str, list[int]] = {}   # provider -> [成功增量, 失败增量]
        self._drained = False
        # 纯"每窗口至多刷一次", 不给首条开特例: 面板容忍 ≤30s 的滞后, 而特例会让人无法
        # 在测试里关闭自动落库。monotonic 起点是开机时刻, 用它做基准才是确定性的。
        self._last_flush = time.monotonic()

    # --- MetricsSink 端口 -------------------------------------------------
    def record(self, *, vendor: str, datatype: str, market: str | None,
               ok: bool, count: int, latency_ms: int, error: str = "") -> None:
        self._inner.record(vendor=vendor, datatype=datatype, market=market, ok=ok,
                           count=count, latency_ms=latency_ms, error=error)
        prov = str(vendor or "").strip()
        if not prov:
            return
        due = False
        with self._lock:
            slot = self._pending.setdefault(prov, [0, 0])
            slot[0 if ok else 1] += 1
            due = (time.monotonic() - self._last_flush) >= self._flush_sec
        if due:
            self.flush()

    def snapshot(self) -> dict[str, dict]:
        return self._inner.snapshot()

    # --- 落库 -------------------------------------------------------------
    def flush(self) -> int:
        """把待写增量刷进 DB; 返回写入的源数。失败回灌, 不丢计数。"""
        with self._lock:
            if not self._pending:
                return 0
            batch, self._pending = self._pending, {}
            self._last_flush = time.monotonic()
        try:
            return self._write(batch)
        except Exception as e:  # noqa: BLE001 - 统计落库绝不能影响取数
            logger.warning("vendor 调用统计落库失败(%s), 计数回灌待下次", e)
            with self._lock:
                for prov, (s, f) in batch.items():
                    slot = self._pending.setdefault(prov, [0, 0])
                    slot[0] += s
                    slot[1] += f
            return 0

    def flush_at_exit(self) -> int:
        """进程退出时排空(幂等: 重复调用只刷一次, 防 shutdown 钩子被多次触发导致重复自增)。"""
        if self._drained:
            return 0
        self._drained = True
        return self.flush()

    def _write(self, batch: dict[str, list[int]]) -> int:
        """按 **provider** 自增(不是按行): 同一 provider 的多个 type 行会拿到同一份
        provider 级总量。这是刻意的 —— vendor 侧指标本来就只按 vendor 记账(EWMA 同理),
        拆到 (provider, type) 需要包内先按 datatype 分桶, 那是另一个口径工程。"""
        from sqlalchemy import text

        factory = self._session_factory
        if factory is None:
            from src.db.session import SessionLocal

            factory = SessionLocal
        n = 0
        db = factory()
        try:
            for prov, (succ, errs) in batch.items():
                db.execute(
                    text("UPDATE data_sources SET success_count = COALESCE(success_count, 0) + :s, "
                         "error_count = COALESCE(error_count, 0) + :e, "
                         "last_used_at = :ts WHERE provider = :p"),
                    {"s": succ, "e": errs, "p": prov, "ts": _now()},
                )
                n += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return n


def _now() -> Any:
    from datetime import datetime

    return datetime.now().replace(microsecond=0)


_sink: DbCountingMetricsSink | None = None


def get_metrics_sink() -> DbCountingMetricsSink:
    """进程级单例(与 get_market_data 同生命周期)。首建时挂 atexit 排空最后一段增量。"""
    global _sink
    if _sink is None:
        import atexit

        _sink = DbCountingMetricsSink()
        atexit.register(_sink.flush_at_exit)
    return _sink
