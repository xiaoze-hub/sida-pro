# -*- coding: utf-8 -*-
"""数智参谋 · 市场扫描只读适配器的**真实接线**验证（2026-11-07）。

`tools_scan_read.py` 一直是「有权限检查但没注册」的死代码：函数写得再对，模型也调不到。
本文件钉住接线后的四条硬约束：

1. **真的进注册表且带 schema**（模型看得见、_execute_tool 分得清名字）；
2. **配方路由可达**：`route_for_request("帮我复盘今天")` 命中 `postmarket_reviewer`
   后，两个工具的 schema 必须出现在本轮下发集里；
3. **无配方兜底可达**：不命中任何配方时，它们同样在兜底工具集里（fallback=True）；
4. **角色/时效/兜底口径**：member 无权 → 拒绝且不落审计；pro 可读；
   数据日未核实 → 只写「未核实」，绝不说「今日/实时」。

隔离：每个用例独立 tmp_path SQLite，不连仓库 data/ 与生产库，不触发 scan/refresh。
时间一律经模块私有钩子 `tsr._now_cst` 注入。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册全部 ORM 表
from src.agents.chat import tools_scan_read as tsr
from src.db.models import DarkFundTopSnapshot, HighValueApiLog, MarketScanRank
from src.web.database import Base
from tests.factories import make_user

RANKS_TOOL = "get_market_scan_ranks"
DARK_TOOL = "get_dark_fund_top"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'scan_wire.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(tsr, "_now_cst", lambda: datetime(2026, 11, 6, 16, 0))


def _user(db, role: str = "pro", **kw):
    u = make_user(role=role, **kw)
    db.add(u)
    db.commit()
    return u


RANK_PAYLOAD = {
    "generated_at": "2026-11-06T15:30:00",
    "universe": 5000,
    "computed": 4990,
    "skipped": 10,
    "dark_days": 1,
    "new_g_points": [{"symbol": "000001"}],
    "dark_top": [{"symbol": "000002", "dark_net_wan": 123.45, "approximation": True}],
    "activity_top": [{"symbol": "000003", "activity": 6.5, "level": "大牛线"}],
}

DARK_PAYLOAD = {
    "generated_at": "2026-11-06T15:31:00",
    "universe": 5000,
    "computed": 4800,
    "top": [{"symbol": "600000", "name": "浦发银行", "main_net_wan": 8800.0}],
}


def _add_rows(db):
    from datetime import date

    db.add(MarketScanRank(snapshot_date=date(2026, 11, 6), stock_market="CN", payload=RANK_PAYLOAD))
    db.add(DarkFundTopSnapshot(snapshot_date=date(2026, 11, 6), stock_market="CN", payload=DARK_PAYLOAD))
    db.commit()


async def _call(name: str, db, user) -> str:
    """走注册表 handler（= chat 里 _execute_tool 的真实路径），不直接调 read_*。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    tool = CHAT_TOOL_REGISTRY[name]
    return await tool.handler(db, {}, user)


# ─────────────────────── 1. 注册表 + schema（模型可见） ───────────────────────


def test_tools_registered_with_llm_schema():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY, chat_tool_schemas

    names = {s.get("function", {}).get("name") for s in chat_tool_schemas()}
    for name in (RANKS_TOOL, DARK_TOOL):
        assert name in CHAT_TOOL_REGISTRY, f"{name} 没进注册表"
        assert name in names, f"{name} 没有 LLM 可见 schema（等于调不到）"
        tool = CHAT_TOOL_REGISTRY[name]
        assert tool.caliber.strip(), f"{name} 缺口径标签"
        assert tool.fallback is True, f"{name} 进不了无配方兜底路径"


def test_schema_params_are_optional_only():
    """只读快照，不该要 symbol / date 之类必填参数 —— 否则兜底路径调用即失败。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    for name in (RANKS_TOOL, DARK_TOOL):
        schema = CHAT_TOOL_REGISTRY[name].schema
        assert schema["function"]["parameters"].get("required", []) == []


# ─────────────────────── 2. 配方路由可达 ───────────────────────


def test_recipe_route_exposes_scan_tools():
    from src.harness.routing import route_for_request
    from src.agents.chat.registry import chat_tool_schemas

    route = route_for_request("帮我复盘今天", chat_tool_schemas())
    assert route.recipe is not None, "「复盘」应命中 postmarket_reviewer"
    sent = {s["function"]["name"] for s in route.tools}
    assert RANKS_TOOL in sent and DARK_TOOL in sent, f"配方路由没下发扫描工具: {sorted(sent)}"


def test_recipe_whitelist_names_exist_in_registry():
    """配方里的工具名写错只会静默失效 —— 静态拦住。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY
    from src.harness.recipes import get_recipe

    recipe = get_recipe("postmarket_reviewer")
    assert recipe is not None
    for name in (RANKS_TOOL, DARK_TOOL):
        assert name in recipe.tools
        assert name in CHAT_TOOL_REGISTRY


# ─────────────────────── 3. 无配方兜底可达 ───────────────────────


def test_no_recipe_fallback_exposes_scan_tools():
    from src.agents.chat.registry import chat_tool_schemas
    from src.harness.routing import route_for_request

    route = route_for_request("你好，随便聊聊天气", chat_tool_schemas())
    assert route.recipe is None, f"这句话不该命中配方: {route.reason}"
    sent = {s["function"]["name"] for s in route.tools}
    assert RANKS_TOOL in sent and DARK_TOOL in sent, f"兜底路由没下发扫描工具: {sorted(sent)}"


# ─────────────────────── 4. 角色：pro 可读、member 拒绝 ───────────────────────


async def test_pro_can_read_both_tools(db):
    _add_rows(db)
    user = _user(db, role="pro")

    ranks = await _call(RANKS_TOOL, db, user)
    dark = await _call(DARK_TOOL, db, user)

    assert "000002" in ranks and "000003" in ranks
    assert "600000" in dark
    audits = db.query(HighValueApiLog).count()
    assert audits >= 2, "页面同源要求：成功读取必须落高价值审计"


async def test_member_without_trial_is_refused_and_not_audited(db, monkeypatch):
    """member 没有「机会/暗盘资金」权限 → 诚实拒绝，且**不落审计**（页面同规则）。"""
    from src.core import free_tier

    monkeypatch.setattr(free_tier, "trial_features", lambda db=None: {})
    _add_rows(db)
    member = _user(db, role="member")

    for name in (RANKS_TOOL, DARK_TOOL):
        out = await _call(name, db, member)
        assert "无权限" in out or "Pro" in out, f"{name} 没拒 member: {out[:120]}"
        assert "000002" not in out and "600000" not in out, f"{name} 把数据泄露给 member"
    assert db.query(HighValueApiLog).count() == 0


async def test_refusal_is_text_not_exception(db, monkeypatch):
    """权限失败必须在工具层转成一行拒绝文案 —— 抛 HTTPException 会炸整轮对话。"""
    from src.core import free_tier

    monkeypatch.setattr(free_tier, "trial_features", lambda db=None: {})
    _add_rows(db)
    member = _user(db, role="member")

    out = await _call(RANKS_TOOL, db, member)  # 不抛 = 通过
    assert isinstance(out, str) and out


# ─────────────────────── 5. 日期未知不得冒充实时/历史 ───────────────────────


async def test_unknown_data_date_never_claims_realtime(db):
    _add_rows(db)
    user = _user(db, role="pro")

    for name in (RANKS_TOOL, DARK_TOOL):
        out = await _call(name, db, user)
        assert "未核实" in out, f"{name} 没标数据日未核实: {out[:200]}"
        for banned in ("实时行情", "今日数据", "当日数据", "盘中实时"):
            assert banned not in out, f"{name} 冒充了实时: 出现 {banned}"
        assert "数据日 " not in out.split("写入日")[0], f"{name} 把写入日当数据日"


async def test_no_snapshot_is_unavailable_not_fabricated(db):
    user = _user(db, role="pro")

    for name in (RANKS_TOOL, DARK_TOOL):
        out = await _call(name, db, user)
        assert "暂无" in out or "不猜" in out, f"{name} 无快照没说实话: {out[:200]}"
        assert "实时" not in out


# ─────────────────────── 6. 口径红线：两榜不合并、DDE 不是暗盘拆单 ───────────────────────


async def test_dde_tool_does_not_claim_dark_split(db):
    _add_rows(db)
    user = _user(db, role="pro")
    out = await _call(DARK_TOOL, db, user)
    assert "主力净流入" in out
    assert "不是暗盘拆单" in out or "非暗盘拆单" in out


# ─────────────── 7. 真实执行入口：chat._execute_tool（不是只调 handler） ───────────────


async def test_execute_tool_real_dispatch_without_recipe(db):
    """无配方（REQ_TOOLS 未设）→ 模型真能通过 _execute_tool 调到本工具。"""
    from src.web.api.chat import _execute_tool

    _add_rows(db)
    user = _user(db, role="pro")
    out = await _execute_tool(db, RANKS_TOOL, {}, user=user)
    assert "扫描池" in out and "000002" in out, out[:200]

    out2 = await _execute_tool(db, DARK_TOOL, {}, user=user)
    assert "主力净流入" in out2 and "600000" in out2


async def test_execute_tool_blocked_when_recipe_lacks_tool(db):
    """配方白名单是运行时硬闸：配方没挂 = 执行层直接拒绝（所以必须挂进配方）。"""
    from src.web.api import chat as chat_api

    _add_rows(db)
    user = _user(db, role="pro")
    token = chat_api._REQ_TOOLS.set(frozenset({"get_market_replay"}))
    try:
        out = await chat_api._execute_tool(db, RANKS_TOOL, {}, user=user)
    finally:
        chat_api._REQ_TOOLS.reset(token)
    assert "不使用工具" in out, out[:200]

    # 挂进配方后同一路径即可执行（对照）
    token = chat_api._REQ_TOOLS.set(frozenset({"get_market_replay", RANKS_TOOL}))
    try:
        ok = await chat_api._execute_tool(db, RANKS_TOOL, {}, user=user)
    finally:
        chat_api._REQ_TOOLS.reset(token)
    assert "扫描池" in ok, ok[:200]
