"""磁盘持久化缓存层(2026-08-12): 解决"容器重启后内存缓存全丢, 首次请求慢"。

把高频/昂贵的缓存(逐笔/筹码/K线/指数)定期落盘到 /app/data/cache/,
进程启动时加载回内存。落盘策略:
- 写时: 缓存变更时标记 dirty, 由 flush 定时(默认 60s)或进程退出时写盘
- 读时: 内存优先, 内存未命中才查磁盘(启动时一次性 load 到内存, 之后纯内存)
- 安全: JSON 序列化失败/损坏静默跳过(缓存是可再生的, 不能因缓存拖垮服务)

用法:
    from src.core.disk_cache import DiskCache
    cache = DiskCache("dark_flow_ticks", ttl=30)
    cache.set("sz002361", value)   # 内存+标记dirty
    v = cache.get("sz002361")      # 内存优先
    cache.flush()                  # 手动刷盘(退出钩子/定时器)
"""
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# 容器数据卷 /app/data/cache; 本地开发回退到项目内 .cache/
_CACHE_DIR = os.environ.get("PANWATCH_CACHE_DIR") or os.path.join(
    os.environ.get("PANWATCH_DATA_DIR", "/app/data"), "cache"
)
try:
    os.makedirs(_CACHE_DIR, exist_ok=True)
except OSError:
    _CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
    os.makedirs(_CACHE_DIR, exist_ok=True)


class DiskCache:
    """TTL 内存缓存 + 定期落盘的 JSON 磁盘缓存。"""

    def __init__(self, name: str, ttl: float = 30.0, flush_interval: float = 60.0):
        self.name = name
        self.ttl = ttl
        self._path = os.path.join(_CACHE_DIR, f"{name}.json")
        self._mem: dict[str, tuple[float, object]] = {}
        self._dirty = False
        self._lock = threading.Lock()
        self._flush_interval = flush_interval
        self._last_flush = 0.0
        self._load_from_disk()

    def _load_from_disk(self):
        """启动时从磁盘加载(纯内存之后, 磁盘只在启动时读一次)。"""
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                now = time.time()
                for k, (ts, v) in data.items():
                    if now - ts < self.ttl:
                        self._mem[k] = (ts, v)
                logger.info(f"DiskCache[{self.name}] 加载 {len(self._mem)} 条")
        except Exception as e:
            logger.warning(f"DiskCache[{self.name}] 磁盘加载失败(忽略): {e}")

    def get(self, key: str):
        with self._lock:
            hit = self._mem.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            return None

    def set(self, key: str, value):
        with self._lock:
            self._mem[key] = (time.time(), value)
            self._dirty = True
            # 简单节流: 距上次刷盘超过 interval 则刷一次
            if time.time() - self._last_flush >= self._flush_interval:
                self._flush_locked()

    def delete(self, key: str):
        with self._lock:
            self._mem.pop(key, None)
            self._dirty = True

    def clear(self):
        with self._lock:
            self._mem.clear()
            self._dirty = True

    def _flush_locked(self):
        """调用方必须持有 _lock。写盘失败静默(缓存可再生)。"""
        try:
            payload = {k: [ts, v] for k, (ts, v) in self._mem.items()}
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self._path)  # 原子替换, 防写一半损坏
            self._dirty = False
            self._last_flush = time.time()
        except Exception as e:
            logger.warning(f"DiskCache[{self.name}] 写盘失败: {e}")

    def flush(self):
        """手动刷盘(供退出钩子/定时器调用)。"""
        with self._lock:
            if self._dirty:
                self._flush_locked()

    def snapshot(self) -> dict:
        """当前内存缓存快照(供调试/统计)。"""
        with self._lock:
            return dict(self._mem)


# 全局退出刷盘钩子: atexit 保证进程退出时把 dirty 缓存写盘
_flush_registry: list[DiskCache] = []
_registry_lock = threading.Lock()


def register(cache: DiskCache):
    with _registry_lock:
        _flush_registry.append(cache)


def flush_all():
    for c in list(_flush_registry):
        try:
            c.flush()
        except Exception:
            pass


import atexit

atexit.register(flush_all)
