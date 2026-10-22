"""单轮取数缓存（2026-10-23）—— 同一轮里 (symbol, market, days) 只查一次 PG。

**问题（实测）**：一轮 `deep_diagnosis` 里 `get_trend_profile` / `get_chip_accumulation` /
`get_scenario_matrix` / `get_stock_dossier` 四个工具各自独立调
`src.db.klines_repo.load_qfq_bars(symbol, market, days=250)` —— 同一条 SQL 打 4 次 PG，
再各自对同一份 bars 跑 `compute_chips(bars[-120:])`。日线在**一轮问句内不可能变**，
这 4 次里 3 次是纯浪费（约占管线取数耗时的四成）。

**做法**：`contextvars.ContextVar` 记录"本轮有效"的缓存作用域，管线在 `run()` / `run_stream()`
两端 `begin_bar_scope()` / `end_bar_scope(token)` 收口。作用域内
`cached_load_bars(symbol, market, days, loader)` 按 `(symbol, market, days)` 命中即返回。

三条纪律（比命中率重要）：

1. **没有作用域就直通**：`cached_load_bars` 直接调 loader，与不装缓存逐字等价。
   非 chat 调用方（回测 / 因子回填 / 定时任务 …）永远碰不到缓存，不会读到陈旧数据。
2. **缓存出错不许影响取数**：键归一化、TTL 判断、容量淘汰、锁获取 … 任何一步抛异常都
   退化成直连 loader。缓存的失效模式必须是"慢一点"，不能是"没有报价"。
3. **不缓存空结果**：`load_qfq_bars` 查库失败返回 `[]`（不抛）。把 `[]` 记进缓存会让
   "第一次失败、第二次能成"变成"整轮全空"，那是**新增**的坏行为 —— 空结果按未命中处理。

TTL 默认 30s（env `HARNESS_BAR_CACHE_TTL` 可覆盖），容量默认 64 条（env
`HARNESS_BAR_CACHE_MAX_ENTRIES`）。TTL 挡"跨轮复用陈旧数据"，容量按插入序 FIFO 淘汰
挡"一轮里扫描全市场把内存撑爆"。

**并发**：`pipeline._run_symbol` 用 `asyncio.gather` 并发跑工具，同一 key 可能同时进来
两个——那就是两次 PG 查询，缓存等于没做。`cached_load_bars_async()` 用 per-key
`asyncio.Lock` 把 loader **含 await 的整段**罩住（single-flight）：第一个进锁的人查，
其余等锁的人拿到的是刚写进去的结果。

**工具侧迁移（下一步，本模块已就绪，工具在 `src/agents/chat/*` 由另一条线改）**：
工具把 `load_qfq_bars(symbol, market, days=250)` 换成
`load_qfq_bars_cached(symbol, market, days=250)`（`src/db/klines_repo.py`，同步薄封装）
即可自动吃到本缓存 —— 同步工具**不需要**改 async，也不需要知道缓存存在。为什么值得换：
同一轮 4 个工具从 4 次查库降到 1 次；`compute_chips` 仍是每个工具各算一次（那是 CPU，
不在本模块范围）。为什么必须是**新名字**而不是把 `load_qfq_bars` 本身改成缓存版：
回填 / 因子 / 回测这些调用方要的是**新鲜**数据，静默缓存会让它们读到上一轮的 K 线。
"""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import os
import time as _time

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 30.0
DEFAULT_MAX_ENTRIES = 64

_MISS = object()

# 无作用域 = None。用 ContextVar 而不是模块级 dict：并发请求（多个 asyncio 任务/线程）
# 各有各的作用域，绝不会看到别人那一轮的 bars。
_SCOPE: contextvars.ContextVar = contextvars.ContextVar("harness_bar_scope", default=None)


class _Scope:
    """一轮的缓存状态（entries 插入有序 → FIFO 淘汰不需要额外队列）。"""

    __slots__ = ("entries", "locks", "ttl", "max_entries")

    def __init__(self, ttl: float, max_entries: int):
        # key -> (expires_at, bars)
        self.entries: dict[tuple, tuple[float, list]] = {}
        # key -> asyncio.Lock（single-flight 用；与 entries 同生命周期）
        self.locks: dict[tuple, asyncio.Lock] = {}
        self.ttl = ttl
        self.max_entries = max_entries


def _now() -> float:
    """单调时钟。抽成函数是为了单测能 monkeypatch 时间（TTL 用例不 sleep）。"""
    return _time.monotonic()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r 非法，退回 %s", name, raw, default)
        return float(default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("%s=%r 非法，退回 %s", name, raw, default)
        return int(default)


def _market_key(market) -> str:
    """market 归一成键的一部分：枚举取 value，字符串统一大写（'cn' 与 'CN' 是同一条）。"""
    mc = market.value if hasattr(market, "value") else market
    return str(mc or "").strip().upper()


def _key(symbol, market, days) -> tuple:
    return (str(symbol or "").strip(), _market_key(market), int(days))


# ── 作用域 ────────────────────────────────────────────────────────────────


def begin_bar_scope(ttl: float | None = None, max_entries: int | None = None):
    """开一个本轮缓存作用域，返回 token（交给 `end_bar_scope`）。

    绝不抛：建作用域失败就返回 None（= 全程直通），宁可没缓存也不能让管线起不来。
    """
    try:
        scope = _Scope(
            float(ttl) if ttl is not None else _env_float("HARNESS_BAR_CACHE_TTL", DEFAULT_TTL_S),
            max(1, int(max_entries) if max_entries is not None
                else _env_int("HARNESS_BAR_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)),
        )
    except Exception as exc:  # noqa: BLE001 - 缓存是增强，不许挡路
        logger.warning("bar_cache 作用域创建失败(本轮不缓存): %s", type(exc).__name__)
        return None
    try:
        return _SCOPE.set(scope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bar_cache 作用域绑定失败(本轮不缓存): %s", type(exc).__name__)
        return None


def end_bar_scope(token) -> None:
    """关作用域（务必放 finally）。token 无效/跨 context 复位时静默吞掉。"""
    if token is None:
        return
    try:
        _SCOPE.reset(token)
    except Exception as exc:  # noqa: BLE001 - 例如 async generator 被别的 task 恢复
        logger.debug("bar_cache 作用域复位失败(忽略): %s", type(exc).__name__)


def scope_active() -> bool:
    """当前 context 里是否有活跃作用域（诊断/单测用）。"""
    try:
        return _SCOPE.get() is not None
    except Exception:  # noqa: BLE001
        return False


# ── 存取（全部内部函数都不抛，由调用方 try 包住再降级直连） ────────────────


def _get(scope: _Scope, key: tuple):
    item = scope.entries.get(key)
    if item is None:
        return _MISS
    expires_at, bars = item
    if expires_at <= _now():
        scope.entries.pop(key, None)     # 过期即删，别等淘汰
        return _MISS
    return list(bars)                    # 浅拷贝：调用方改列表顺序也污染不到缓存


def _put(scope: _Scope, key: tuple, bars) -> None:
    if not isinstance(bars, (list, tuple)) or len(bars) == 0:
        return                           # 空结果不入缓存(见模块 docstring 第 3 条)
    scope.entries.pop(key, None)         # 先删再插 → 插入序 = 最近写入
    scope.entries[key] = (_now() + scope.ttl, list(bars))
    while len(scope.entries) > scope.max_entries:
        oldest = next(iter(scope.entries))
        scope.entries.pop(oldest, None)  # FIFO 淘汰：长轮次不能无界增长


def _lock_for(scope: _Scope, key: tuple) -> asyncio.Lock | None:
    lock = scope.locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        scope.locks[key] = lock
    # 锁字典也跟着容量收口：只清"已释放且已有缓存结果"的键，等待者手里的旧锁对象
    # 最多导致一次重复查询，不会破坏正确性。
    if len(scope.locks) > scope.max_entries * 2:
        for k in [k for k, lk in scope.locks.items() if not lk.locked() and k in scope.entries]:
            scope.locks.pop(k, None)
    return lock


def _read(scope, key):
    try:
        return _get(scope, key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("bar_cache 读取失败(降级直连): %s", type(exc).__name__)
        return _MISS


def _write(scope, key, bars) -> None:
    try:
        _put(scope, key, bars)
    except Exception as exc:  # noqa: BLE001
        logger.debug("bar_cache 写入失败(忽略): %s", type(exc).__name__)


# ── 取数入口 ──────────────────────────────────────────────────────────────


def cached_load_bars(symbol, market, days, loader) -> list[dict]:
    """同步版：作用域内按 `(symbol, market, days)` 复用，作用域外直通 loader。

    `loader(symbol, market, days)` 的异常**原样上抛**（缓存层不包装、不吞），
    与不装缓存时的行为一致；只有缓存自身的簿记异常才降级成直连。
    """
    scope = _SCOPE.get()
    if scope is None:                    # ← 最重要的一条：无作用域 == 不缓存
        return loader(symbol, market, days)
    try:
        key = _key(symbol, market, days)
    except Exception:                    # noqa: BLE001
        return loader(symbol, market, days)
    hit = _read(scope, key)
    if hit is not _MISS:
        return hit            # type: ignore[return-value]  # 命中一定是 list
    bars = loader(symbol, market, days)  # 同步 loader 在事件循环里不会被抢占，天然 single-flight
    _write(scope, key, bars)
    return bars


async def cached_load_bars_async(symbol, market, days, loader):
    """异步版：同上，但用 per-key `asyncio.Lock` 把 await 段罩住（single-flight）。

    `loader` 可以是同步函数（`klines_repo.load_qfq_bars`）也可以是协程函数，两者都支持。
    """
    scope = _SCOPE.get()
    if scope is None:
        return await _maybe_await(loader(symbol, market, days))
    try:
        key = _key(symbol, market, days)
    except Exception:  # noqa: BLE001
        return await _maybe_await(loader(symbol, market, days))
    hit = _read(scope, key)
    if hit is not _MISS:
        return hit
    try:
        lock = _lock_for(scope, key)
    except Exception:  # noqa: BLE001 - 拿不到锁就直连，别等
        lock = None
    if lock is None:
        bars = await _maybe_await(loader(symbol, market, days))
        _write(scope, key, bars)
        return bars
    async with lock:                     # 并发同 key：只放一个人去查库
        hit = _read(scope, key)
        if hit is not _MISS:
            return hit
        bars = await _maybe_await(loader(symbol, market, days))
        _write(scope, key, bars)
        return bars


async def _maybe_await(result):
    return await result if inspect.isawaitable(result) else result
