"""两融（A股杠杆）工具：归因维度「杠杆资金在加还是在撤」的数据入口。

生产实测（2026-10-25 / v0.58.1 容器内）：
  wudao MCP margin_trading(format=json) 正常返回 SSE/SZSE/BSE 分区日频数据；
  2026-10-23 上证融资余额 12373.51亿、两融 12559.20亿。

这里钉三件事：
1. 同一天多交易所要**合并**（SSE+SZSE+BSE），不能只取一行当全市场；
2. 区间变化要用首尾两日算，不能拿最新一天跟 0 比；
3. 源挂/空必须如实说明（"数据源不可用" ≠ "没有两融数据"）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.agents.chat import registry as reg


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def call_tool(self, name, args=None):
        self.calls.append((name, args or {}))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _rows():
    # 单位：元。同一天三家交易所各一行。
    return {
        "rows": [
            {"tradeDate": "20261023", "exchangeId": "SSE", "marginBalance": 1.2375e12,
             "totalMarginShortBalance": 1.2559e12},
            {"tradeDate": "20261023", "exchangeId": "SZSE", "marginBalance": 1.1722e12,
             "totalMarginShortBalance": 1.1824e12},
            {"tradeDate": "20261023", "exchangeId": "BSE", "marginBalance": 7.813e9,
             "totalMarginShortBalance": 7.813e9},
            {"tradeDate": "20261020", "exchangeId": "SSE", "marginBalance": 1.20e12,
             "totalMarginShortBalance": 1.22e12},
            {"tradeDate": "20261020", "exchangeId": "SZSE", "marginBalance": 1.15e12,
             "totalMarginShortBalance": 1.16e12},
        ]
    }


def _run(args, payload, monkeypatch):
    fake = _FakeClient(payload)
    monkeypatch.setattr(
        "src.collectors.wudao_mcp_client.WudaoMCPClient", lambda *a, **k: fake
    )
    out = asyncio.run(reg._tool_get_margin_trading(None, args))
    return out, fake


def test_同日多交易所合并_不能只取一行(monkeypatch):
    out, _ = _run({"startDate": "2026-10-20", "endDate": "2026-10-23"}, _rows(), monkeypatch)
    # 融资: 1.2375 + 1.1722 + 0.0078 = 2.4175e12 → 24175亿
    # 两融: 1.2559 + 1.1824 + 0.0078 = 2.4461e12 → 24461亿
    assert "24175亿" in out, out
    assert "24461亿" in out, out
    assert "两融余额" in out
    for ex in ("SSE", "SZSE", "BSE"):
        assert ex in out, f"缺 {ex} 分区明细"


def test_区间变化用首尾两日算(monkeypatch):
    out, _ = _run({"startDate": "2026-10-20", "endDate": "2026-10-23"}, _rows(), monkeypatch)
    # 融资 2.35e12 → 2.4175e12 = +2.87%
    assert "区间变化" in out
    assert "+2.87%" in out, out


def test_源异常要说明是数据源不可用(monkeypatch):
    out, _ = _run({}, RuntimeError("connect timeout"), monkeypatch)
    assert "取数失败" in out
    assert "不是'没有两融数据'" in out


def test_空结果不装成0(monkeypatch):
    out, _ = _run({"startDate": "2026-10-20", "endDate": "2026-10-23"},
                  {"rows": [], "text": "无数据"}, monkeypatch)
    assert "无数据" in out
    assert "0亿" not in out


def test_传code时透传个股参数(monkeypatch):
    _, fake = _run({"code": "002361"}, _rows(), monkeypatch)
    assert fake.calls and fake.calls[0][0] == "margin_trading"
    assert fake.calls[0][1].get("code") == "002361"


def test_工具已在注册表末尾且带口径(monkeypatch):
    names = list(reg.CHAT_TOOL_REGISTRY)
    # 新增工具一律追加在末尾（保证 core 前缀导出顺序不变），只要求它在尾部区
    assert "get_margin_trading" in names[-3:], names[-3:]
    assert reg.CHAT_TOOL_REGISTRY["get_margin_trading"].caliber.strip()
    desc = reg.CHAT_TOOL_REGISTRY["get_margin_trading"].schema["function"]["description"]
    assert "杠杆" in desc
