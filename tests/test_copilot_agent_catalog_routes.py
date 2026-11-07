# -*- coding: utf-8 -*-
"""P0 回归: `GET /copilot/agents` 与「专家目录」路由撞车。

**根因**（修复前，可直接复现）：`src/web/api/copilot.py` 里有两个 handler 同时注册在
`GET /agents` 上 ——
  · `get_copilot_agents` → `list[dict]`（前端 @ 面板 `api.ts::getCopilotAgents` 的契约）
  · `list_agents`        → `dict`（`available/items/count`，AgentCatalog 页的契约）

FastAPI 先注册者胜，于是：
  1. 目录对象永远拿不到 → `/copilot/agents` 只会回数组；
  2. AgentCatalog 页把数组当对象读 → `res.available === undefined` → 页面固定显示
     「服务端未给出原因」（`AgentCatalog.tsx` 的 error 分支），一屏智能体全看不见；
  3. 两个 handler 在 openapi 里互相覆盖，只有一条 operation 存活。

**钉子（本文件存在的理由）**：
  ① `/copilot/agents` 必须仍是**数组** —— @ 面板不能因为修目录而被打断；
  ② 专家目录走独立路径，返回对象，`count/items` 与配方数严格一致（不补 0、不吞空）；
  ③ 独立详情路径可用，未知 id 明确不可用而不是 500 或编造；
  ④ 两条路径在 openapi 里是两条**独立 operation**（防再次被同名覆盖）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.harness.recipes import list_recipes  # noqa: E402
from src.web.api import copilot as copilot_api  # noqa: E402
from src.web.api.auth import get_current_user  # noqa: E402
from src.web.database import Base, get_db  # noqa: E402

MENTION_ARRAY_PATH = "/copilot/agents"
CATALOG_PATH = "/copilot/agent-catalog"
CATALOG_DETAIL_PATH = "/copilot/agent-catalog/{recipe_id}"


class _User:
    id = "u-test"
    username = "tester"
    role = "owner"
    is_admin = True


def _make_client() -> TestClient:
    """最小 app：只挂 copilot router，鉴权/DB 全部走 dependency_overrides。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    app = FastAPI()
    app.include_router(copilot_api.router)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    async def _user():
        return _User()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app, raise_server_exceptions=False)
    client._app = app  # openapi 断言用
    return client


@pytest.fixture()
def client() -> TestClient:
    return _make_client()


def _recipe_ids() -> list[str]:
    return [r.id for r in list_recipes()]


# ───────────────── ① @ 面板契约：数组不能变 ─────────────────

def test_mention_panel_route_still_returns_bare_array(client: TestClient):
    res = client.get(MENTION_ARRAY_PATH)
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, list), f"@ 面板要数组，实际拿到 {type(body).__name__}: {str(body)[:200]}"
    assert [row["id"] for row in body] == _recipe_ids()
    # @ 面板行内要渲染 图标/标签/工具数；缺字段等于面板空白
    for row in body:
        assert {"id", "label", "icon", "desc", "when", "tool_count"} <= set(row)


# ───────────────── ② 专家目录：独立路径 + 对象 ─────────────────

def test_agent_catalog_has_its_own_route_and_object_shape(client: TestClient):
    res = client.get(CATALOG_PATH)
    assert res.status_code == 200, f"{CATALOG_PATH} 不可用({res.status_code})：目录仍被 /agents 覆盖"
    body = res.json()
    assert isinstance(body, dict), "目录必须是对象（available/items/count），不是 @ 面板的数组"
    assert body["available"] is True
    assert body["count"] == len(_recipe_ids())
    assert [item["id"] for item in body["items"]] == _recipe_ids()
    # 目录页要用：工具明细 + 内容指纹版本
    for item in body["items"]:
        assert isinstance(item["tools"], list)
        assert "version" in item


def test_catalog_count_matches_items_not_padded(client: TestClient):
    body = client.get(CATALOG_PATH).json()
    # 口径铁律：count 就是 len(items)，不补 0、不虚增
    assert body["count"] == len(body["items"])


# ───────────────── ③ 详情 ─────────────────

def test_agent_catalog_detail_route(client: TestClient):
    rid = _recipe_ids()[0]
    res = client.get(f"{CATALOG_PATH}/{rid}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["available"] is True
    assert body["item"]["id"] == rid
    assert isinstance(body["history"], list)


def test_agent_catalog_detail_unknown_id_is_explicit(client: TestClient):
    res = client.get(f"{CATALOG_PATH}/__no_such_recipe__")
    assert res.status_code == 200, "未知配方是「没有」，不是 500/404 崩"
    body = res.json()
    assert body["available"] is False
    assert body["item"] is None
    assert "__no_such_recipe__" in body["reason"]


# ───────────────── ④ 两条路径各自是独立 operation ─────────────────

def test_openapi_exposes_both_paths_as_distinct_operations(client: TestClient):
    paths = client._app.openapi()["paths"]
    assert MENTION_ARRAY_PATH in paths, "@ 面板路径被删了"
    assert CATALOG_PATH in paths, "目录独立路径没有注册"
    assert CATALOG_DETAIL_PATH in paths, "目录详情独立路径没有注册"

    mention_op = paths[MENTION_ARRAY_PATH]["get"]
    catalog_op = paths[CATALOG_PATH]["get"]
    # 同名路径被覆盖时会是同一个 operation（同 summary/handler 指纹），这里必须不同
    assert mention_op.get("operationId") != catalog_op.get("operationId")
