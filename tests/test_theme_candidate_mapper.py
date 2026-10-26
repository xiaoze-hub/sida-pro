import pytest
from src.core.theme_candidate_mapper import (
    extract_event_themes,
    resolve_theme_candidates,
    build_fullmarket_theme_candidates,
)


def test_extract_event_themes_robot():
    events = [
        {"content": "OpenAI 宣布成立具身智能硬件实验室，全面进军人形机器人大脑研发", "subjects": ["机器人", "AI大脑"]},
        {"content": "特斯拉人形机器人 Optimus 进厂实测执行器与减速器性能", "subjects": ["特斯拉", "减速器"]},
    ]
    themes = extract_event_themes(events)
    assert len(themes) >= 1
    assert themes[0]["theme"] == "人形机器人"
    assert themes[0]["hit_count"] >= 1


def test_resolve_theme_candidates_fallback():
    # 测试静态高辨识度龙头兜底机制
    cands = resolve_theme_candidates("人形机器人", top_n=3)
    assert len(cands) == 3
    symbols = [c["symbol"] for c in cands]
    # 包含了主板高辨识度机器人龙头
    assert any(sym in ["002046", "002896", "002050", "002472", "603728"] for sym in symbols)
    for c in cands:
        assert c["theme"] == "人形机器人"
        assert c["name"]


def test_build_fullmarket_theme_candidates_end_to_end():
    events = [
        {"content": "OpenAI 发力人形机器人研发，AI大脑为核心突破口", "subjects": ["机器人"]},
        {"content": "低空经济利好密集出台，多地发布eVTOL通航规划", "subjects": ["低空经济"]},
    ]
    res = build_fullmarket_theme_candidates(events)
    assert len(res) >= 2
    theme_names = [r["theme"] for r in res]
    assert "人形机器人" in theme_names
    assert "低空经济" in theme_names

    robot_item = next(r for r in res if r["theme"] == "人形机器人")
    assert len(robot_item["candidates"]) > 0
    first_c = robot_item["candidates"][0]
    assert "symbol" in first_c
    assert "name" in first_c
    assert "reason" in first_c
