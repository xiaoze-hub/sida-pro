"""网关单元测试：契约补参、禁用清单、缓存与串行闸门（全部离线，不打上游）。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tdx_gateway.contract import DENY, ContractError, build_body, load_schema, profile_for
from tdx_gateway.upstream import Upstream, UpstreamBusy

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "http_schema.json"
SCHEMA = load_schema(SCHEMA_PATH)


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class FakeClient:
    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay
        self.concurrent = 0
        self.max_concurrent = 0

    async def post(self, url, json=None, timeout=None):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.calls.append((json["method"], json["params"]))
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return FakeResp({"id": 1, "result": {"ErrorId": "0", "Value": {"ok": True}}})
        finally:
            self.concurrent -= 1

    async def aclose(self):
        return None


def test_contract_injects_hidden_fields():
    """漏了 table_list/id/type 是 ErrorId=10 的真因，网关必须补齐。"""
    body = build_body("get_financial_data", {"stock_list": ["600111.SH"], "field_list": ["FN1"]}, SCHEMA)
    assert body["id"] == 1
    assert body["type"] == 1
    assert body["table_list"] == ["FN1"]      # field_list → table_list 的映射
    assert body["stock_list"] == ["600111.SH"]


def test_contract_literal_type_cannot_be_overridden():
    body = build_body("get_market_snapshot", {"stock_code": "600111.SH", "type": 99}, SCHEMA)
    assert body["type"] == 3                  # 调用方改不动判别码，避免串道


def test_contract_reports_missing_required():
    with pytest.raises(ContractError) as e:
        build_body("get_market_snapshot", {}, SCHEMA)
    assert "stock_code" in str(e.value)


def test_deny_list_covers_trading():
    for m in ("order_stock", "cancel_order_stock", "exec_to_tdx", "query_stock_positions"):
        assert m in DENY


def test_profiles_pick_timeouts():
    assert profile_for("get_market_snapshot")["cache_ttl"] == 3
    assert profile_for("formula_process_mul_zb")["timeout"] == 90
    assert profile_for("get_financial_data")["cache_ttl"] == 300


def test_upstream_is_serial_and_caches():
    up = Upstream("http://127.0.0.1:1/", SCHEMA, concurrency=1)
    fake = FakeClient(delay=0.02)
    up._client = fake

    async def run():
        await asyncio.gather(*[up.call("get_market_snapshot", {"stock_code": "600111.SH"}) for _ in range(5)])
        # single-flight 合并后，再来一次同参调用 → 走缓存（3s TTL）
        await up.call("get_market_snapshot", {"stock_code": "600111.SH"})

    asyncio.run(run())
    assert fake.max_concurrent == 1            # 串行：客户端经不起并发
    assert len(fake.calls) == 1                # 5 并发合并成 1 次，缓存再省 1 次
    assert up.metrics["get_market_snapshot"]["cache_hit"] == 1


def test_queue_limit_rejects_when_full():
    up = Upstream("http://127.0.0.1:1/", SCHEMA, concurrency=1, queue_limit=1)
    fake = FakeClient(delay=0.05)
    up._client = fake

    async def run():
        t1 = asyncio.create_task(up.call("get_market_snapshot", {"stock_code": "600111.SH"}))
        await asyncio.sleep(0.01)
        with pytest.raises(UpstreamBusy):
            await up.call("get_market_snapshot", {"stock_code": "000001.SZ"})
        await t1

    asyncio.run(run())


def test_schema_file_is_shippable():
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert raw["count"] >= 50
    assert "get_market_snapshot" in raw["methods"]
