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
            {"tradeDate": "20261020", "exchangeId": "SSE", "marginBalance": 1.18e12,
             "totalMarginShortBalance": 1.20e12},
            {"tradeDate": "20261020", "exchangeId": "SZSE", "marginBalance": 1.13e12,
             "totalMarginShortBalance": 1.14e12},
            {"tradeDate": "20261020", "exchangeId": "BSE", "marginBalance": 7.5e9,
             "totalMarginShortBalance": 7.5e9},
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
    # 融资 (1.18+1.13+0.0075)=2.3175e12 → 23175亿；23175 → 24175 = +4.32%
    assert "区间变化" in out
    assert "+4.32%" in out, out


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
    # 2026-10-25: get_market_ranking 追加到末尾后，末尾窗口要放宽到 4
    assert "get_margin_trading" in names[-4:], names[-4:]
    assert reg.CHAT_TOOL_REGISTRY["get_margin_trading"].caliber.strip()
    desc = reg.CHAT_TOOL_REGISTRY["get_margin_trading"].schema["function"]["description"]
    assert "杠杆" in desc


def test_最新日缺交易所时不算假暴跌(monkeypatch):
    """生产实测：20261023 只回 SSE 一家(12374亿)，前一日三家齐全(24269亿)。
    直接比较会得出 -49% 的假腰斩 —— 必须识别"缺所日不可比"，
    区间只用两个完整日算，并在输出里显式警告。"""
    payload = {"rows": [
        {"tradeDate": "20261023", "exchangeId": "SSE", "marginBalance": 1.2374e12,
         "totalMarginShortBalance": 1.2559e12},
        {"tradeDate": "20261022", "exchangeId": "SSE", "marginBalance": 1.2469e12,
         "totalMarginShortBalance": 1.2654e12},
        {"tradeDate": "20261022", "exchangeId": "SZSE", "marginBalance": 1.1722e12,
         "totalMarginShortBalance": 1.1824e12},
        {"tradeDate": "20261022", "exchangeId": "BSE", "marginBalance": 7.8e9,
         "totalMarginShortBalance": 7.8e9},
        {"tradeDate": "20261021", "exchangeId": "SSE", "marginBalance": 1.2400e12,
         "totalMarginShortBalance": 1.2600e12},
        {"tradeDate": "20261021", "exchangeId": "SZSE", "marginBalance": 1.1600e12,
         "totalMarginShortBalance": 1.1700e12},
        {"tradeDate": "20261021", "exchangeId": "BSE", "marginBalance": 7.7e9,
         "totalMarginShortBalance": 7.7e9},
    ]}
    out, _ = _run({"startDate": "2026-10-21", "endDate": "2026-10-23"}, payload, monkeypatch)
    # 必须警告缺所
    assert "仅 1/3 家" in out, out
    assert "不可与前一日直接比较" in out
    # 区间用两个完整日：20261021 → 20261022
    assert "20261021→20261022" in out
    # 绝不允许出现腰斩级跌幅（假暴跌的判据：任何 >=10% 的跌幅都不该出现）
    import re as _re
    pcts = [float(x) for x in _re.findall(r"([+-]\d+\.\d+)%", out)]
    assert all(abs(v) < 10 for v in pcts), f"出现可疑大幅涨跌: {pcts}\n{out}"
    # 缺所日要标星
    assert "20261023" in out and "*" in out
