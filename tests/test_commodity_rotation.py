"""commodity_rotation 模块测试(大宗商品轮动前瞻)。"""

from __future__ import annotations

import pytest

from src.core.commodity_rotation import (
    STAGE_AGRI,
    STAGE_ENERGY,
    STAGE_GOLD,
    STAGE_METAL,
    detect_rotation_stage,
    format_rotation,
)


def test_energy_stage():
    """原油/煤炭涨价 → 能源冲锋阶段,下一幕金属。"""
    r = detect_rotation_stage(["原油期货大涨3%", "煤炭价格创年内新高"])
    assert r["stage"] == STAGE_ENERGY
    assert r["next_stage"] == STAGE_METAL
    assert "铜" in "".join(r["next_sectors"]) or "铝" in "".join(r["next_sectors"])


def test_metal_stage():
    """铜铝钢铁涨 → 金属狂潮,下一幕农产品。"""
    r = detect_rotation_stage(["铜价突破新高", "铝锭现货涨价", "螺纹钢提价"])
    assert r["stage"] == STAGE_METAL
    assert r["next_stage"] == STAGE_AGRI


def test_agri_stage():
    """粮食棉花涨 → 农产压轴,下一幕黄金。"""
    r = detect_rotation_stage(["大豆期货上涨", "棉花价格走高"])
    assert r["stage"] == STAGE_AGRI
    assert r["next_stage"] == STAGE_GOLD


def test_gold_stage():
    """黄金启动 → 黄金返场,避险提示。"""
    r = detect_rotation_stage(["国际金价大涨", "避险情绪升温"])
    assert r["stage"] == STAGE_GOLD
    assert any("避险" in h for h in r["hints"])


def test_no_events():
    """无事件 → 未检测到。"""
    r = detect_rotation_stage([])
    assert "未检测" in r["stage"]


def test_mixed_multiple_stages():
    """多阶段命中取最高。"""
    r = detect_rotation_stage(["原油涨", "铜价涨", "铜价又涨"])
    assert r["stage"] == STAGE_METAL  # 金属命中 2 次 > 能源 1 次


def test_format():
    r = detect_rotation_stage(["铜价突破新高"])
    out = format_rotation(r)
    assert "大宗商品轮动" in out
    assert "金属狂潮" in out


def test_conflict_overrides_rotation():
    """地缘冲突优先于普通轮动(战争打断轮动剧本)。"""
    r = detect_rotation_stage(["以伊开战,中东局势升级", "原油暴涨5%", "黄金大涨"])
    assert r["stage"] == "地缘冲突"
    assert r.get("conflict") is True
    assert "石油" in "".join(r["sectors"])
    assert "黄金" in "".join(r["sectors"])
    assert any("五波传导" in h for h in r["hints"])


def test_conflict_energy_combined():
    """冲突+能源事件 → 仍判地缘冲突(优先级)。"""
    r = detect_rotation_stage(["铜价上涨", "伊朗遭导弹袭击"])
    assert r["stage"] == "地缘冲突"


def test_conflict_sector_cooccurrence():
    """避险板块同现(石油+军工+黄金 ≥2) → 地缘冲突; 单个不算。"""
    r = detect_rotation_stage(["石油板块涨停4家", "军工板块涨停3家"])
    assert r["stage"] == "地缘冲突" and r.get("conflict") is True
    r2 = detect_rotation_stage(["石油板块涨停4家"])
    assert r2["stage"] != "地缘冲突"
