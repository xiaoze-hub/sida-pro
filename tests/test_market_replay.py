"""盘后复盘数据完整性与输出模板回归测试（2026-10-23）。

覆盖三类真事故：
1. `signal_summary._block_limit_up` 键名不匹配 → 每天写「0家涨停，最高0板」
   （真实 2026-10-22 是 44 家、10-23 是 86 家）；
2. 跌停家数 / 炸板率 / 晋级率从未进入对话证据；
3. 复盘输出是散文式长段落，重点不突出。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core import market_replay, signal_summary
from src.db.models import Base, MarketReplayDaily

ROOT = Path(__file__).resolve().parents[1]


def _recipe() -> dict:
    return yaml.safe_load((ROOT / "src/harness/recipes/postmarket_reviewer.yaml").read_text(encoding="utf-8"))


def _zt_row(code="002724", name="海洋王", lbc=6, fbt=92500, zbc=0, fund=5e7, hybk="软件服务", m=0):
    return {
        "c": code, "m": m, "n": name, "p": 13280, "zdp": 10.02,
        "amount": 4.5e7, "ltsz": 1.7e9, "tshare": 2.7e9, "hs": 2.6,
        "lbc": lbc, "fbt": fbt, "lbt": fbt, "fund": fund, "zbc": zbc, "hybk": hybk,
        "zttj": {"days": lbc, "ct": lbc},
    }


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[MarketReplayDaily.__table__])
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()


# ─────────────── REPLAY-001: 必须挂 get_market_replay ───────────────


def test_postmarket_recipe_mounts_market_replay():
    """@spec REPLAY-001"""
    r = _recipe()
    assert "get_market_replay" in r["tools"], "复盘必须挂三池同源工具，不能从策略信号表取涨停"
    assert "get_opportunities" not in r["tools"], "长期返回空的工具不该占复盘预算"
    assert r["budget"]["max_tool_calls"] >= len(r["tools"])


def test_market_replay_tool_registered():
    """@spec REPLAY-001"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    tool = CHAT_TOOL_REGISTRY["get_market_replay"]
    params = tool.schema["function"]["parameters"]["properties"]
    assert "date" in params and "refresh" in params


# ─────────────── REPLAY-002: 不得用 0 冒充缺失 ───────────────


def test_signal_summary_limit_up_reads_real_keys(monkeypatch):
    """@spec REPLAY-002"""
    async def fake_summary():
        return {
            "limit_up_count": 86,
            "max_streak": 6,
            "ladder": {6: 2, 3: 1, 2: 10, 1: 73},
            "top_stocks": ["海洋王(002724)6板"],
            "limit_up_source": "tq",
        }

    monkeypatch.setattr("src.core.report_generator._collect_limit_up_summary", fake_summary)
    block = asyncio.run(signal_summary._block_limit_up())
    assert block["data_status"] == "ok"
    assert block["total"] == 86, "键名错配会把 86 读成 None→0"
    assert block["max_days"] == 6
    assert "86家涨停" in block["content"]
    assert "最高6板" in block["content"]
    assert "梯队" in block["content"]


def test_signal_summary_limit_up_marks_missing_not_zero(monkeypatch):
    """@spec REPLAY-002"""
    async def broken():
        return {"error": "无涨停池数据"}

    monkeypatch.setattr("src.core.report_generator._collect_limit_up_summary", broken)
    block = asyncio.run(signal_summary._block_limit_up())
    assert block["data_status"] == "missing"
    assert "0家涨停" not in block["content"], "取数失败不许写 0，0 是有效观测值"


def test_signal_summary_limit_up_marks_incomplete_fields(monkeypatch):
    """@spec REPLAY-002"""
    async def partial():
        return {"total": 0, "max_days": 0}  # 只有旧键、没有 limit_up_count

    monkeypatch.setattr("src.core.report_generator._collect_limit_up_summary", partial)
    block = asyncio.run(signal_summary._block_limit_up())
    assert block["data_status"] == "missing"


def test_render_never_fabricates_zero_for_missing_pools():
    """@spec REPLAY-002"""
    replay = {
        "as_of": "20261023",
        "source": "eastmoney_replay",
        "limit_up": {"caliber": "eastmoney_zt", "total": 86, "max_streak": 6, "ladder": {"6": 2, "1": 73},
                     "first_board": 73, "ge2": 12, "ge3": 3, "one_word": 2, "leaders": [], "rows": []},
        "broken": {"caliber": "eastmoney_zt", "total": 12, "break_rate_pct": 12.24, "seal_rate_pct": 87.76, "rows": []},
        "limit_down": {"caliber": "eastmoney_zt", "total": 6, "max_days": 2, "rows": []},
        "promotion": {"available": True, "prev_date": "20261022", "prev_count": 44, "promoted": 9,
                      "promo_rate_pct": 20.45},
        "mainline": {"top_sectors": [{"name": "软件服务", "count": 13}], "leaders": []},
        "emotion": {"available": True, "phase": "repair", "phase_label": "修复", "confidence": "ok", "basis_days": 30},
        "cross_check": {"eastmoney_zt": 86, "tq_pool": 86, "diff": 0, "note": "同口径", "tq_detail": {}},
        "quality": {"涨停池": "ok", "炸板池": "ok", "跌停池": "ok", "TQ交叉校验": "ok", "晋级率": "ok", "情绪周期": "ok"},
    }
    text = market_replay.render(replay)
    for kw in ("涨停 86 家", "炸板 12 家", "炸板率 12.24%", "封板率 87.76%", "跌停 6 家", "晋级率 20.45%", "修复"):
        assert kw in text, f"渲染缺 {kw}"

    # 炸板率缺失时不得输出 0%
    replay["broken"] = {"total": 0, "break_rate_pct": None, "rows": []}
    replay["quality"]["炸板池"] = "missing"
    text2 = market_replay.render(replay)
    assert "炸板率 0" not in text2
    assert "炸板率暂无" in text2


# ─────────────── REPLAY-003: 输出模板要点化 ───────────────


def test_recipe_prompt_enforces_decision_first_format():
    """@spec REPLAY-003"""
    r = _recipe()
    prompt = r["prompt_fragment"]
    sections = r["output_sections"]
    assert sections[0] == "一句话结论", "首段必须是结论"
    for sec in sections:
        assert sec in prompt, f"段落名 {sec} 必须在 prompt 里原文出现"
    for kw in ("≤50 字", "表格", "一行一条", "禁止流水段落"):
        assert kw in prompt, f"排版铁律缺 {kw}"
    assert "weekly" not in prompt.lower()


def test_recipe_has_acceptance_and_budget():
    """@spec REPLAY-003"""
    r = _recipe()
    assert len(r["acceptance"]) >= 3
    assert r["budget"]["max_tool_calls"] >= len(r["tools"])


# ─────────────── 三池计算与落库 ───────────────


def test_build_replay_computes_break_and_seal_rate(monkeypatch):
    zt = [_zt_row("002724", "海洋王", 6), _zt_row("603278", "大业股份", 6),
          _zt_row("600111", "北方稀土", 1, fbt=100000, hybk="稀有金属"),
          _zt_row("300750", "宁德时代", 2, fbt=93500, hybk="电池")]
    zb = [{"c": "001360", "m": 0, "n": "南矿集团", "p": 17880, "ztp": 20440, "zdp": -3.77,
           "amount": 4.7e8, "ltsz": 2.2e9, "tshare": 3.6e9, "hs": 20.8, "fbt": 92500, "zbc": 1,
           "hybk": "专用设备", "zttj": {"days": 2, "ct": 1}},
          {"c": "603600", "m": 1, "n": "永艺股份", "p": 10790, "ztp": 11130, "zdp": 6.62,
           "amount": 2.7e8, "ltsz": 3.5e9, "tshare": 3.5e9, "hs": 8.0, "fbt": 94000, "zbc": 2,
           "hybk": "家居用品", "zttj": {"days": 1, "ct": 1}}]
    dt = [{"c": "001368", "m": 0, "n": "通达创智", "p": 28940, "zdp": -10.01, "amount": 6.3e7,
           "ltsz": 3.1e9, "tshare": 3.2e9, "hs": 1.98, "fund": 5.7e6, "lbt": 93721, "days": 2,
           "oc": 0, "hybk": "文娱用品"}]

    def fake_pool(kind, date_str):
        # 走真实归一化路径（生产 _fetch_pool 返回的就是 _norm_row 归一后的行）
        raw = {"zt": zt, "zb": zb, "dt": dt}[kind]
        rows = [market_replay._norm_row(it) for it in raw]
        return rows, None, len(rows)

    monkeypatch.setattr(market_replay, "_fetch_pool", fake_pool)
    monkeypatch.setattr(market_replay, "_tq_crosscheck", lambda d: {"available": True, "total": 4, "max_streak": 6, "ladder": {}})
    monkeypatch.setattr(market_replay, "_promotion", lambda d, rows: {"available": True, "promo_rate_pct": 25.0,
                                                                    "prev_count": 8, "promoted": 2, "prev_date": "20261022"})
    monkeypatch.setattr(market_replay, "_emotion", lambda d, m: {"available": True, "phase": "repair",
                                                                "phase_label": "修复", "confidence": "ok", "basis_days": 20})

    rp = market_replay.build_replay("20261023")
    assert rp["limit_up"]["total"] == 4
    assert rp["limit_up"]["max_streak"] == 6
    assert rp["limit_up"]["first_board"] == 1
    assert rp["limit_up"]["ge2"] == 3
    assert rp["broken"]["total"] == 2
    # 炸板率 = 2/(4+2) = 33.33%，封板率 = 66.67%
    assert rp["broken"]["break_rate_pct"] == pytest.approx(33.33, abs=0.01)
    assert rp["broken"]["seal_rate_pct"] == pytest.approx(66.67, abs=0.01)
    assert rp["limit_down"]["total"] == 1
    assert rp["limit_down"]["max_days"] == 2
    # 一字板：09:25:00 首封 + 0 炸板
    assert rp["limit_up"]["one_word"] == 2
    assert rp["mainline"]["top_sectors"][0] == {"name": "软件服务", "count": 2}


def test_persist_and_read_roundtrip(db):
    replay = {
        "as_of": "20261023", "source": "eastmoney_replay",
        "limit_up": {"caliber": "eastmoney_zt", "total": 86, "max_streak": 6, "ladder": {"6": 2},
                     "first_board": 73, "ge2": 12, "ge3": 3, "one_word": 2, "leaders": [], "rows": []},
        "broken": {"total": 12, "break_rate_pct": 12.24, "seal_rate_pct": 87.76, "rows": []},
        "limit_down": {"total": 6, "max_days": 2, "rows": []},
        "promotion": {"available": True, "promo_rate_pct": 20.45, "prev_count": 44, "promoted": 9},
        "mainline": {"top_sectors": [], "leaders": []},
        "emotion": {"available": True, "phase": "repair", "phase_label": "修复"},
        "cross_check": {"eastmoney_zt": 86, "tq_pool": 86, "diff": 0, "note": "同口径"},
        "quality": {"涨停池": "ok", "炸板池": "ok", "跌停池": "ok", "TQ交叉校验": "ok", "晋级率": "ok", "情绪周期": "ok"},
    }
    assert market_replay.persist(db, replay) == 1
    row = db.query(MarketReplayDaily).filter(MarketReplayDaily.trade_date == "20261023").first()
    assert row is not None
    assert row.limit_up_count == 86 and row.broken_count == 12 and row.limit_down_count == 6
    assert row.break_rate_pct == pytest.approx(12.24)
    assert row.quality == "ok"
    # 幂等：再写一次不新增行
    assert market_replay.persist(db, replay) == 1
    assert db.query(MarketReplayDaily).count() == 1

    latest = market_replay.read_latest(db)
    assert latest and latest["as_of"] == "20261023"

    # 部分缺失 → quality=partial，且 missing_dims 记录缺口
    replay["quality"]["炸板池"] = "missing"
    market_replay.persist(db, replay)
    row = db.query(MarketReplayDaily).filter(MarketReplayDaily.trade_date == "20261023").first()
    assert row.quality == "partial"
    assert "炸板池" in (row.missing_dims or [])
