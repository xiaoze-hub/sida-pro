"""GET /copilot/tools 工具目录（只读反射）回归。

钉子：注册表 77 个工具是门禁事实来源，目录必须**全量**列出且逐条带口径，
不允许聚合口径、不允许省略未挂配方的工具。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base
from src.web.api.copilot import router as copilot_router
from src.web.api.auth import get_current_user
from src.web.database import get_db


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    class _User:
        id = 1
        username = "tester"

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(copilot_router, prefix="/api")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: _User()
    return TestClient(app)


def test_catalog_lists_every_registered_tool(client):
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    body = client.get("/api/copilot/tools").json()
    assert body["total"] == len(CHAT_TOOL_REGISTRY) == 78
    assert len(body["tools"]) == 78
    assert {t["name"] for t in body["tools"]} == set(CHAT_TOOL_REGISTRY)


def test_every_tool_carries_its_own_caliber(client):
    body = client.get("/api/copilot/tools").json()
    # 口径必须逐条返回，不允许汇总成一句
    assert all("caliber" in t for t in body["tools"])
    portfolio = next(t for t in body["tools"] if t["name"] == "get_portfolio")
    assert "持仓" in portfolio["caliber"]


def test_recipe_back_index_is_correct(client):
    from src.harness.recipes import list_recipes

    body = client.get("/api/copilot/tools").json()
    by_name = {t["name"]: t for t in body["tools"]}

    for recipe in list_recipes():
        for tool_name in recipe.tools or []:
            assert recipe.id in by_name[tool_name]["recipes"]

    expected_mounted = {n for n, t in by_name.items() if t["recipes"]}
    assert body["mounted"] == len(expected_mounted)


def test_unmounted_tool_returns_empty_list_not_missing(client):
    body = client.get("/api/copilot/tools").json()
    unmounted = [t for t in body["tools"] if not t["recipes"]]
    # 门禁要求：未挂配方的工具必须能看出来（空数组，而不是省略 recipes 字段）
    assert unmounted, "注册表里必然有未挂配方的工具"
    assert all("recipes" in t and t["recipes"] == [] for t in unmounted)


def test_budget_flag_matches_registry(client):
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    body = client.get("/api/copilot/tools").json()
    for item in body["tools"]:
        assert item["charges_budget"] == bool(
            CHAT_TOOL_REGISTRY[item["name"]].charges_budget
        )
