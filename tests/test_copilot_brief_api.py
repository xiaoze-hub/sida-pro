"""tests/test_copilot_brief_api.py

验证 /api/copilot/brief/{symbol} 与 /api/copilot/presets 路由及确定性判定引擎。
"""

import pytest
from starlette.testclient import TestClient

from src.core.copilot_brief import build_copilot_brief
from src.web.app import app


@pytest.fixture
def client():
    # 绕过认证测试端点
    return TestClient(app, raise_server_exceptions=True)


def test_build_copilot_brief_structure():
    """测试 build_copilot_brief 返回数据结构符合 Schema 契约。"""
    res = build_copilot_brief("600111", market="CN")
    assert res["symbol"] == "600111"
    assert res["version"] == "copilot-brief/1"
    assert "evidence" in res
    assert "verdict" in res
    assert "session" in res

    verdict = res["verdict"]
    assert "intent_label" in verdict
    assert verdict["intent_tone"] in ("opportunity", "neutral", "caution", "risk")
    assert 0.20 <= verdict["confidence"] <= 0.95
    assert verdict["rule_id"] in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9")
    assert isinstance(verdict["defense_lines"], list)
    assert isinstance(verdict["action_plans"], list)


def test_copilot_openapi_paths():
    """确保 OpenAPI 中已挂载 /api/copilot/brief/{symbol} 和 /api/copilot/presets。"""
    schema = app.openapi()
    paths = schema.get("paths", {})
    assert "/api/copilot/brief/{symbol}" in paths
    assert "/api/copilot/presets" in paths
