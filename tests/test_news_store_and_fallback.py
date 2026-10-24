"""news 沉淀/检索 + 指数降级 + 联网兜底（2026-10-24 新增能力）。

三件事各自的最重要断言：
1. news_cache 此前 **0 行**（只拉不存）→ 现在落库必须**幂等**（同一批拉两次不重复）。
2. 指数行情 thsdk 空时必须**降级腾讯**，且 note 里写明口径，不冒充 thsdk。
3. 联网搜索是**兜底**：没配密钥要如实说"未启用"，不能装作搜不到。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from src.core import news_store
from src.db.session import SessionLocal


def _db():
    return SessionLocal()


def _clear(db):
    db.execute(text("DELETE FROM news_cache WHERE external_id LIKE 'test-%'"))
    db.commit()


class _Item:
    def __init__(self, title, source="cls", ts=None, content="", ext=None):
        self.title = title
        self.source = source
        self.publish_time = ts or datetime.now()
        self.content = content
        self.external_id = ext


def test_保存幂等_同一批拉两次不重复入库():
    db = _db()
    try:
        _clear(db)
        items = [
            _Item("工信部：加快人工智能+专项行动", ext="test-1"),
            _Item("某公司发布三季度业绩预告", ext="test-2"),
        ]
        assert news_store.save_flash_news(db, items) == 2
        # 再灌一次同样的（模拟 30s 轮询重复拉取）
        assert news_store.save_flash_news(db, items) == 0
        n = db.execute(text("SELECT COUNT(*) FROM news_cache WHERE external_id LIKE 'test-%'")).scalar()
        assert n == 2
    finally:
        _clear(db)
        db.close()


def test_政策分类只按关键词_不做情绪判断():
    assert news_store.is_policy("工信部：加快推进人工智能+软件专项行动")
    assert news_store.is_policy("央行宣布降准0.5个百分点")
    assert not news_store.is_policy("某公司股东减持计划实施完毕")


def test_检索按关键词与政策过滤():
    db = _db()
    try:
        _clear(db)
        now = datetime.now()
        news_store.save_flash_news(db, [
            _Item("发改委：出台专项债支持设备更新", ts=now, ext="test-p1"),
            _Item("半导体设备订单回暖", ts=now - timedelta(days=1), ext="test-p2", content="半导体"),
        ])
        rows = news_store.search_news(db, keyword="半导体", days=7, policy_only=False, limit=10)
        assert any("半导体" in r["title"] or "半导体" in r["content"] for r in rows)
        pol = news_store.search_news(db, policy_only=True, days=7, limit=20)
        assert pol and all(r["policy"] for r in pol)
    finally:
        _clear(db)
        db.close()


def test_覆盖区间如实返回_空库不假装有历史():
    db = _db()
    try:
        cov = news_store.coverage(db)
        assert set(cov) == {"rows", "from", "to"}
        # 空库必须是 rows=0 + from/to=None（不填今天冒充有数据）
        if cov["rows"] == 0:
            assert cov["from"] is None and cov["to"] is None
    finally:
        db.close()


def test_指数工具_thsdk空时降级腾讯且标注口径(monkeypatch):
    from src.agents.chat import tools_thsdk

    # thsdk 恒空（模拟生产：ths_username/ths_sdk_password 未配 → 游客受限）
    monkeypatch.setattr(tools_thsdk, "_safe_call", lambda *a, **k: tools_thsdk._degraded("thsdk 返回空数据"))
    monkeypatch.setattr(
        tools_thsdk, "_tencent_index_fallback",
        lambda sym: [{"symbol": sym, "name": "上证指数", "price": 3690.48,
                      "change_pct": 0.89, "change": 32.45, "prev_close": 3658.03, "source": "腾讯"}],
    )
    r = tools_thsdk.get_thsdk_market_data_index("000001")
    assert r["available"] is True
    assert r["source"] == "腾讯"
    assert "口径=腾讯" in r["note"] and "th sdk" not in r["note"].lower()
    assert r["data"][0]["price"] == 3690.48


def test_指数工具_两路都没有时如实报空(monkeypatch):
    from src.agents.chat import tools_thsdk

    monkeypatch.setattr(tools_thsdk, "_safe_call", lambda *a, **k: tools_thsdk._degraded("thsdk 返回空数据"))
    monkeypatch.setattr(tools_thsdk, "_tencent_index_fallback", lambda sym: [])
    r = tools_thsdk.get_thsdk_market_data_index("000001")
    assert r["available"] is False
    assert "腾讯兜底亦无数据" in r["note"]


def test_联网兜底_未配密钥时明确说未启用而不是搜不到(monkeypatch):
    from src.agents.chat import registry as reg

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(reg, "_tavily_key", lambda db=None: "")
    db = _db()
    try:
        out = asyncio.run(reg._tool_web_search_fallback(db, {"query": "半导体 政策"}))
    finally:
        db.close()
    assert "未启用" in out and "不代表" in out


def test_联网兜底_工具描述强制兜底语义():
    """约束写在 schema 里，模型才看得到 —— 缺这条就等于没有约束。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    desc = CHAT_TOOL_REGISTRY["web_search_fallback"].schema["function"]["description"]
    assert "兜底" in desc
    assert "系统数据源有数据时" in desc
    assert "非系统数据源" in desc


def test_新增两个工具都已进规格表():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY, chat_tool_schemas

    names = {s["function"]["name"] for s in chat_tool_schemas()}
    assert {"search_news", "web_search_fallback"} <= names
    assert len(CHAT_TOOL_REGISTRY) == 75
