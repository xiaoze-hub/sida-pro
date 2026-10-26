"""宏观序列工具（外部环境维度）——美债/油价/美元/VIX。

为什么有这个模块：参谋被问「为什么跌」时，先前只有内部数据（K线/资金/两融），
外部环境（美债收益率、油价、美元指数）完全没有来源，于是第二轮就编了
「北向 -9.28 亿 / 44 股解禁 1060 亿 / 氪气 / 胡塞」这些无出处的叙事。

口径红线（实测踩过）：
1. Yahoo 的 `^TNX` 在 2026 年从「×10」改成「直接给百分数」——写死 0.1 会静默差 10 倍，
   于是「美债 4.99%」会变成「0.4988%」。必须自适应归一。
2. 一个指标有数据 ≠ 因果成立。工具输出里要显式写「不能单独当因果」。
3. 取不到要列 reason，不插值不补 0。
"""

from __future__ import annotations

import asyncio

import pytest

from src.agents.chat import registry as reg
from src.core import macro_free_sources as mfs


# ── 纯函数：归一与缩放 ────────────────────────────────────────────────


def test_收益率归一_两种格式都对():
    # 新格式：直接给百分数
    assert mfs._norm_yield(4.988) == 4.988
    # 旧格式：×10 报价（49.88 表示 4.988%）
    assert mfs._norm_yield(49.88) == 4.988
    # 阈值边界：>20 才判旧格式，20 以内不动
    assert mfs._norm_yield(20.0) == 20.0
    assert mfs._norm_yield(20.1) == 2.01


def test_不支持指标要列出可用项而不是静默空(monkeypatch):
    out = mfs.get_series("not_a_key")
    assert out["points"] == []
    assert "不支持的指标" in out["reason"]
    assert "us10y" in out["supported"]


def test_抓取失败要带reason不编数(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("network down")

    mfs._CACHE.clear()
    monkeypatch.setattr(mfs, "_fetch_raw", boom)
    s = mfs.get_series("brent")
    assert s["points"] == []
    assert "取数失败" in s["reason"]


# ── 工具层：输出纪律 ──────────────────────────────────────────────────


def _fake_summarize(key, range_="3mo"):
    if key == "us10y":
        return {"key": "us10y", "name": "美国10年期国债收益率", "unit": "%",
                "first_date": "2026-07-23", "last_date": "2026-10-23",
                "first": 4.703, "last": 4.988, "change_pct": 6.06,
                "high": 5.12, "low": 4.55, "points": [{"date": "x", "value": 1}],
                "source": "Yahoo"}
    return {"key": key, "name": key, "unit": "%", "points": [], "reason": "源无返回"}


def test_工具输出带口径且禁止单指标下因果结论(monkeypatch):
    monkeypatch.setattr(mfs, "summarize", _fake_summarize)
    out = asyncio.run(reg._tool_get_macro_series(None, {"keys": ["us10y", "brent"]}))
    assert "美债" in out or "国债收益率" in out
    assert "+6.06%" in out
    assert "非国内宏观" in out
    # 只有 1 个指标有数 -> 必须警告不能当因果
    assert "不能据此推出" in out
    assert "未取到" in out


def test_工具默认取三个指标(monkeypatch):
    seen = []

    def spy(key, range_="3mo"):
        seen.append(key)
        return _fake_summarize(key, range_)

    monkeypatch.setattr(mfs, "summarize", spy)
    asyncio.run(reg._tool_get_macro_series(None, {}))
    assert seen == ["us10y", "brent", "dxy"]


def test_工具已注册且带口径():
    names = list(reg.CHAT_TOOL_REGISTRY)
    # 新增工具都在尾部区（顺序：两融 → 宏观 → 区间走势 → 体检报告 → 市场排行）
    # 2026-10-26: get_stock_report 归位末尾后窗口放宽到 6
    assert {"get_margin_trading", "get_macro_series"} <= set(names[-6:]), names[-6:]
    t = reg.CHAT_TOOL_REGISTRY["get_macro_series"]
    assert t.caliber.strip()
    assert "Yahoo" in t.caliber


def test_宏观指标表覆盖归因需要的关键维度():
    for k in ("us10y", "brent", "dxy", "vix"):
        assert k in mfs.SERIES, f"{k} 缺"
