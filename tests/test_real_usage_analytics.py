"""测试 API Key 用量监控增强、IP追踪、QPS、每日新增与中文接口标签（2026-11-04）。"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.skill_labels import label_for, SKILL_CHINESE_LABELS
from src.db.models import Base, SkillApiKey, SkillUsage
from src.web.api.skills_gateway import get_admin_usage_analytics


def test_skill_labels_mapping():
    assert label_for("get_main_intent") == "腾讯逐笔·主力意图分析"
    assert label_for("get_thsdk_dde") == "DDE 大单动向与主力博弈"
    assert label_for("unknown_custom_skill") == "Unknown Custom Skill"


@pytest.fixture()
def db_session():
    fd, path = tempfile.mkstemp(suffix=".db")
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    db = Session()
    yield db
    db.close()
    eng.dispose()


def test_admin_usage_analytics_endpoint_logic(db_session):
    # 模拟新增 API Key
    k1 = SkillApiKey(key_hash="hash1", key_prefix="sk_12345", owner_label="test1", tier="pro")
    db_session.add(k1)
    db_session.commit()

    # 模拟用量记录（带 client_ip）
    u1 = SkillUsage(api_key_id=k1.id, skill_name="get_main_intent", status_code=200, client_ip="1.2.3.4")
    u2 = SkillUsage(api_key_id=k1.id, skill_name="get_thsdk_dde", status_code=200, client_ip="5.6.7.8")
    db_session.add_all([u1, u2])
    db_session.commit()

    # 模拟 admin 用户对象
    class MockAdmin:
        id = "admin"
        role = "owner"

    res = get_admin_usage_analytics(days=7, db=db_session, user=MockAdmin())
    assert res["success"] is True
    assert res["total_calls_in_window"] == 2
    assert "qps" in res
    assert len(res["top_skills"]) == 2
    assert res["top_skills"][0]["display_name"] in SKILL_CHINESE_LABELS.values()
    assert len(res["top_ips"]) == 2
    assert res["top_ips"][0]["ip"] in ("1.2.3.4", "5.6.7.8")
