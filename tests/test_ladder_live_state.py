"""Redis 盘中态读写(v0.5.87): 键/TTL/fail-soft; cli 注入, 测试用 FakeRedis。"""
import json

import src.core.ladder_live_state as st


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        return True


class DeadRedis:
    def get(self, key):
        raise RuntimeError("connection closed")

    def set(self, key, value, ex=None):
        raise RuntimeError("connection closed")


def test_save_load_roundtrip():
    cli = FakeRedis()
    state = {"A": {"ever_sealed": True, "first_at": "09:31",
                   "last_sealed_at": "09:31", "opened_at": None}}
    meta = {"last_ok": "14:31", "stale": False, "rounds_failed": 0}
    assert st.save_state(cli, "20260912", state, meta) is True
    assert st.load_state(cli, "20260912") == state
    assert st.load_meta(cli, "20260912") == meta
    assert json.loads(cli.store["ladder_live:20260912"]) == state  # 键名固定


def test_cli_none_is_failsoft_not_raise():
    assert st.save_state(None, "20260912", {"A": {}}, {"stale": False}) is False
    assert st.load_state(None, "20260912") == {}
    assert st.load_meta(None, "20260912") == {"last_ok": None, "stale": False, "rounds_failed": 0}


def test_redis_error_is_failsoft_not_raise():
    assert st.save_state(DeadRedis(), "20260912", {"A": {}}, {"stale": False}) is False
    assert st.load_state(DeadRedis(), "20260912") == {}
    assert st.load_meta(DeadRedis(), "20260912") == {"last_ok": None, "stale": False, "rounds_failed": 0}


def test_load_meta_merges_defaults():
    cli = FakeRedis()
    cli.store["ladder_live:20260912:meta"] = json.dumps({"stale": True})
    assert st.load_meta(cli, "20260912") == {"last_ok": None, "stale": True, "rounds_failed": 0}
