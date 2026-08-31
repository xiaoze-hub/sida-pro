"""DiskCache 单元测试: 落盘/加载/TTL/损坏容错。"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("PANWATCH_CACHE_DIR", "/tmp/panwatch_test_cache")

from src.core.disk_cache import DiskCache


@pytest.fixture()
def clean_cache_dir():
    import shutil
    d = "/tmp/panwatch_test_cache"
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestDiskCache:
    def test_set_get_roundtrip(self, clean_cache_dir):
        c = DiskCache("test1", ttl=60)
        c.set("a", {"x": 1})
        assert c.get("a") == {"x": 1}

    def test_flush_and_reload(self, clean_cache_dir):
        c = DiskCache("test2", ttl=60)
        c.set("sz002361", {"ticks": [1, 2, 3], "last_page": 19})
        c.flush()
        # 新实例 = 模拟重启
        c2 = DiskCache("test2", ttl=60)
        assert c2.get("sz002361") == {"ticks": [1, 2, 3], "last_page": 19}

    def test_ttl_expiry(self, clean_cache_dir):
        c = DiskCache("test3", ttl=0.1)
        c.set("k", "v")
        time.sleep(0.2)
        assert c.get("k") is None

    def test_corrupt_file_ignored(self, clean_cache_dir):
        path = os.path.join(clean_cache_dir, "test4.json")
        with open(path, "w") as f:
            f.write("{corrupt json!!!")
        c = DiskCache("test4", ttl=60)  # 不应抛异常
        assert c.get("anything") is None

    def test_expired_disk_not_loaded(self, clean_cache_dir):
        c = DiskCache("test5", ttl=1)
        c.set("k", "v")
        c.flush()
        time.sleep(1.2)
        c2 = DiskCache("test5", ttl=1)
        assert c2.get("k") is None
