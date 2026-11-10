# -*- coding: utf-8 -*-
"""系统信号摘要(设计稿 §7.3) 单测: src/core/signal_summary.py

覆盖:
  - render_signal_summary: 5 块渲染成文本(含标题 + 各块 content)
  - build_signal_summary: 聚合 5 块(monkeypatch 各 _block_*)
  - _block_market_scan: 无快照 → missing; 有快照 → ok(取前3)
  - _block_indices_flow: 全ok / 仅指数(partial) / 全missing
  - read_latest_summary_text: 有快照 → text; 无 → None
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import signal_summary as ss  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ──────────────────────────── render ────────────────────────────

def test_render_all_blocks():
    blocks = {
        "sentiment": {"data_status": "ok", "content": "情绪周期：冰点"},
        "mainline": {"data_status": "ok", "content": "市场主线：机器人(12家)"},
        "market_scan": {"data_status": "ok", "content": "三榜：新G点[002361]"},
        "limit_up": {"data_status": "ok", "content": "涨停复盘：42家涨停"},
        "indices_flow": {"data_status": "ok", "content": "指数资金：上证-0.3%"},
    }
    text = ss.render_signal_summary(blocks)
    # 2026-11-10 时效修复：标题只写「系统信号摘要(基准日 ...)」，且带时效行；
    # 不再无条件写「今日」。
    assert "系统信号摘要" in text and "基准日" in text
    assert "今日系统信号摘要" not in text
    assert "时效：" in text
    for b in blocks.values():
        assert b["content"] in text
    # 5 块按 _BLOCK_ORDER 顺序出现
    idx = [text.index(b["content"]) for b in blocks.values()]
    assert idx == sorted(idx)


def test_render_missing_block_has_placeholder():
    text = ss.render_signal_summary({"sentiment": {"content": "情绪周期数据缺失"}})
    assert "情绪周期数据缺失" in text


# ──────────────────────────── build(聚合) ────────────────────────────

def test_build_aggregates_five_blocks(monkeypatch):
    async def fake_sentiment():
        return {"data_status": "ok", "content": "s"}
    async def fake_mainline():
        return {"data_status": "ok", "content": "m"}
    def fake_scan(db):
        return {"data_status": "ok", "content": "sc"}
    async def fake_limit():
        return {"data_status": "ok", "content": "l"}
    async def fake_flow():
        return {"data_status": "ok", "content": "f"}

    monkeypatch.setattr(ss, "_block_sentiment", fake_sentiment)
    monkeypatch.setattr(ss, "_block_mainline", fake_mainline)
    monkeypatch.setattr(ss, "_block_market_scan", fake_scan)
    monkeypatch.setattr(ss, "_block_limit_up", fake_limit)
    monkeypatch.setattr(ss, "_block_indices_flow", fake_flow)

    r = _run(ss.build_signal_summary(db=None))
    assert set(r["blocks"].keys()) == {"sentiment", "mainline", "market_scan", "limit_up", "indices_flow"}
    assert "s" in r["text"] and "m" in r["text"] and "f" in r["text"]


# ──────────────────────────── 三榜块 ────────────────────────────

class _Row:
    def __init__(self, payload):
        self.payload = payload

class _Query:
    def __init__(self, row):
        self._row = row
    def filter(self, *a, **k):
        # 2026-11-10: 三榜/摘要查询加了 `snapshot_date <= 今天` 过滤（未来脏行不当最新）
        return self
    def order_by(self, *a, **k):
        return self
    def first(self):
        return self._row

class _DB:
    def __init__(self, row):
        self._row = row
    def query(self, model):
        return _Query(self._row)


def test_block_scan_no_snapshot():
    b = ss._block_market_scan(_DB(None))
    assert b["data_status"] == "missing"
    assert "15:30" in b["content"] or "缺失" in b["content"]


def test_block_scan_with_snapshot():
    payload = {
        "new_g_points": [{"symbol": "a"}, {"symbol": "b"}, {"symbol": "c"}, {"symbol": "d"}],
        "dark_top": [{"symbol": "x", "dark_net_wan": 100}, {"symbol": "y", "dark_net_wan": 200}],
        "activity_top": [{"symbol": "p", "level": "强势"}],
    }
    b = ss._block_market_scan(_DB(_Row(payload)))
    assert b["data_status"] == "ok"
    assert len(b["new_g"]) == 3  # 只取前3
    assert b["new_g"] == ["a", "b", "c"]
    assert "a" in b["content"]


# ──────────────────────────── 指数资金块 ────────────────────────────

def test_block_flow_all_ok(monkeypatch):
    async def fake_indices():
        return [{"name": "上证", "change_pct": -0.3}, {"name": "深成", "change_pct": -0.5}]
    async def fake_flow():
        return {"total_main_flow": -85.0, "source": "eastmoney"}

    # 直接 patch report_generator 里的采集函数(block 内部 import)
    import src.core.report_generator as rg
    monkeypatch.setattr(rg, "_collect_indices", fake_indices)
    monkeypatch.setattr(rg, "_collect_market_flow", fake_flow)

    b = _run(ss._block_indices_flow())
    assert b["data_status"] == "ok"
    assert "主力净流入-85亿" in b["content"]


def test_block_flow_only_indices_partial(monkeypatch):
    import src.core.report_generator as rg

    async def fake_indices():
        return [{"name": "上证", "change_pct": 0.1}]
    async def fake_flow():
        return {"error": "数据获取失败"}

    monkeypatch.setattr(rg, "_collect_indices", fake_indices)
    monkeypatch.setattr(rg, "_collect_market_flow", fake_flow)

    b = _run(ss._block_indices_flow())
    assert b["data_status"] == "partial"
    assert "上证" in b["content"]


def test_block_flow_all_missing(monkeypatch):
    import src.core.report_generator as rg

    async def fake_indices():
        return []
    async def fake_flow():
        return {"error": "数据获取失败"}

    monkeypatch.setattr(rg, "_collect_indices", fake_indices)
    monkeypatch.setattr(rg, "_collect_market_flow", fake_flow)

    b = _run(ss._block_indices_flow())
    assert b["data_status"] == "missing"


# ──────────────────────────── 读快照 ────────────────────────────

def test_read_latest_summary_text_none():
    assert ss.read_latest_summary_text(_DB(None)) is None


def test_read_latest_summary_text_hit():
    """老快照（只有 text）：生成日只写「生成日」；标题里的生成日**不当**数据基准日。"""
    from datetime import datetime

    class Row:
        snapshot_date = "2026-11-05"
        text = "--- 今日系统信号摘要(2026-11-05) ---\n情绪周期：发酵"

    # 冻结到 2026-11-10 14:40（周二盘中）
    spent = ss.now_cn
    ss.now_cn = lambda now=None: now or datetime(2026, 11, 10, 14, 40, tzinfo=ss._CST)
    try:
        out = ss.read_latest_summary_text(_DB(Row()))
    finally:
        ss.now_cn = spent
    head = out.splitlines()[0]
    # 2026-11-10 时效审计：正文没有任何数据日期 → 基准日未知（不许拿标题里的生成日兜底）
    assert head == "--- 系统信号摘要（生成日 2026-11-05；基准日 未知）---"
    assert "时效：" in out
    assert "今日系统信号摘要" not in out
    assert "未标注数据基准日" in out
    assert "可用于今日结论" not in out
    assert "情绪周期：发酵" in out


def test_read_latest_summary_text_uses_body_date_not_header():
    """正文有数据日期时用正文；标题里的生成日（11-10）不得被当基准日。"""
    from datetime import datetime

    class Row:
        snapshot_date = "2026-11-10"
        text = (
            "--- 今日系统信号摘要(2026-11-10) ---\n"
            "情绪周期：发酵\n"
            "📊 市场广度·情绪温度（2026-11-05）"
        )

    spent = ss.now_cn
    ss.now_cn = lambda now=None: now or datetime(2026, 11, 10, 14, 40, tzinfo=ss._CST)
    try:
        out = ss.read_latest_summary_text(_DB(Row()))
    finally:
        ss.now_cn = spent
    head = out.splitlines()[0]
    assert "基准日 2026-11-05" in head and "生成日 2026-11-10" in head
    assert "历史" in out and "落后 2 个交易日" in out
    assert "可用于今日结论" not in out
