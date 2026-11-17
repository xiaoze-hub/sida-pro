# -*- coding: utf-8 -*-
"""数智参谋 `get_chan_structure` 真实链路验收(2026-11-17)。

覆盖的是「真实路由 → 权限 → 工具 → 统一结果」, 不是 schema 快照:

  ① 真实 harness 路由: 单股缠论问句 / 「复盘 + 缠论」/ 无配方单股兜底, 三档都必须能把
     `get_chan_structure` 下发给模型; 「复盘」单独出现仍归盘后复盘配方(不被本工具劫持)。
  ② 权限: 无用户身份(定时任务/服务通道)、guest(无行情权限) 在**取数之前**被拒;
     member 才允许, 且走 core.permissions 同一口径。
  ③ 工具: 必须回传图表响应真摘要, 缺摘要 = 严格缺失态(不取数、不猜、不声称已读取);
     摘要来自**真实** `GET /api/klines/{symbol}` 响应(经 klines 真实函数计算), 不 mock 摘要。
  ④ 统一结果: 同一标的不同摘要(不同标的/换了一根 bar/days 不一致) → 409 如实返回,
     不返回半份结构; 无已完成K线 → 422; 上游异常 → 固定文案(不回显 DSN/异常原文)。
  ⑤ 隔离: 本轮 pytest 的实际 DB 绑定必须是 conftest 临时目录下的 SQLite。

只替换数据源边界(`_pg_klines`), 不 mock `get_klines`/摘要/分型/笔内核 —— 否则测的是
自己写的假链路。
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.collectors.kline_collector import KlineData
from src.core.chan_pen import PEN_KERNEL_VERSION
from src.web.api import chan_snapshot as chan
from src.web.api import klines as kl

NOW = datetime(2026, 11, 17, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TODAY = "2026-11-17"
COMPLETED = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16"]

USER_ID = "c0ffee00-2222-4333-8444-chana0000001"
GUEST_ID = "c0ffee00-2222-4333-8444-chang0000001"
assert len(USER_ID) == 36 and len(GUEST_ID) == 36, "users.id 是 VARCHAR(36)"

#: 单调上去再下来: 11-11 形成唯一确认顶分型(与既有端点契约测试同形)。
_OHLC = [
    (9.2, 9.8, 10.0, 9.0),
    (9.8, 10.8, 11.0, 9.5),
    (10.8, 11.8, 12.0, 10.5),
    (11.0, 10.4, 11.2, 10.0),
    (10.4, 11.3, 11.5, 9.8),
    (10.7, 9.4, 10.8, 9.2),
]
_TODAY_OHLC = (9.4, 10.5, 11.0, 9.3)


def _k(day: str, ohlc: tuple[float, float, float, float]) -> KlineData:
    open_, close, high, low = ohlc
    return KlineData(date=day, open=open_, close=close, high=high, low=low, volume=1000.0)


def _copy(bar: KlineData) -> KlineData:
    return KlineData(date=bar.date, open=bar.open, close=bar.close,
                     high=bar.high, low=bar.low, volume=bar.volume)


class _Feed:
    """可变的 `_pg_klines` 替身: 换 bars 即模拟"图表已经变了/换了数据源"。"""

    def __init__(self, bars: list[KlineData]):
        self.bars = bars
        self.calls = 0

    def __call__(self, symbol, market_code, days):
        self.calls += 1
        bars = list(self.bars)
        try:
            n = int(days)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            bars = bars[-n:]
        return ([_copy(bar) for bar in bars], bars[-1].date)


def _completed_feed() -> _Feed:
    return _Feed([_k(day, ohlc) for day, ohlc in zip(COMPLETED, _OHLC)])


#: 干净锯齿(每根各自成一根合并K线): 底@01-02 → 顶@01-06 → 底@01-10 → 顶@01-14,
#: 间距恰为 4 根(笔内核 MIN_INDEX_GAP); 前两笔已确认、第三笔候选。
_PEN_START = datetime(2026, 1, 1)
_PEN_HIGHS_LOWS = [
    (6.0, 5.0), (5.0, 4.0), (5.5, 4.5), (6.5, 5.5), (7.5, 6.5), (8.5, 7.5),
    (8.0, 7.0), (7.0, 6.0), (6.5, 5.5), (6.0, 5.0), (7.0, 6.0), (7.5, 6.5),
    (8.0, 7.0), (9.0, 8.0), (8.5, 7.5), (9.5, 8.5),
]
_PEN_DAYS = [(_PEN_START + timedelta(days=i)).date().isoformat()
             for i in range(len(_PEN_HIGHS_LOWS))]


def _pen_feed() -> _Feed:
    return _Feed([
        _k(day, (low, high, high, low)) for day, (high, low) in zip(_PEN_DAYS, _PEN_HIGHS_LOWS)
    ])


def _chart_client(monkeypatch, feed: _Feed) -> TestClient:
    monkeypatch.setattr(kl, "_pg_klines", feed)
    monkeypatch.setattr(kl, "_finalize_daily_bars", lambda bars, *a, **k: (bars, None))
    monkeypatch.setattr(kl, "_kl_cache", {})
    monkeypatch.setattr(chan, "_now", lambda: NOW)
    app = FastAPI()
    # 图表主接口本身是公开的(与生产一致); 结构端点的鉴权在工具层用真实用户核验。
    app.include_router(kl.router, prefix="/api/klines")
    return TestClient(app)


def _chart_digest(client: TestClient, *, symbol: str = "600000", days: int = 60,
                  instrument: str = "stock") -> str:
    r = client.get(
        f"/api/klines/{symbol}",
        params={"market": "CN", "days": days, "interval": "1d", "instrument": instrument},
    )
    assert r.status_code == 200, r.text
    block = r.json().get("chan_snapshot") or {}
    digest = block.get("digest")
    assert isinstance(digest, str) and len(digest) == 64, block
    return digest


def _loaded_conftest():
    for name in ("conftest", "tests.conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "purge_users"):
            return module
    raise RuntimeError("pytest 尚未加载 conftest")


@pytest.fixture()
def users():
    """真实 users 行(member + guest); 结构工具按 role 走 core.permissions。"""
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        created = []
        for uid, name, role in ((USER_ID, "chan-ai-member", "member"),
                                (GUEST_ID, "chan-ai-guest", "guest")):
            row = db.query(User).filter(User.id == uid).first()
            if row is None:
                row = User(id=uid, username=name, password_hash="x", role=role,
                           is_active=True, token_version=1)
                db.add(row)
                created.append(uid)
        db.commit()
    finally:
        db.close()
    yield {"member": USER_ID, "guest": GUEST_ID}
    db = SessionLocal()
    try:
        _loaded_conftest().purge_users(db, ids=[USER_ID, GUEST_ID])
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def db():
    from src.web.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _user(db, uid: str):
    from src.web.models import User

    return db.query(User).filter(User.id == uid).first()


def _call(db, user, **args) -> str:
    """走 chat 的真实分发函数(不是直接调 handler), 保留预算/白名单守卫语义。"""
    from src.web.api import chat as chat_api

    return asyncio.run(chat_api._execute_tool(db, "get_chan_structure", args, user=user))


@contextmanager
def _turn(*, symbol: str = "600000", instrument: str = "stock", days: int = 60,
          digest: str = "a" * 64, bound: str | None = None):
    """模拟消息入口: 把**本轮已复核**的快照装进请求上下文(2026-11-17 起唯一的信源)。

    工具不再接受调用方自带摘要, 所以凡是要真的取到结构的用例都必须走这个"本轮快照"。
    """
    from src.harness.context import REQ_BOUND, REQ_CHAN_SNAPSHOT

    tok_snap = REQ_CHAN_SNAPSHOT.set({
        "symbol": symbol, "market": "CN", "instrument": instrument,
        "interval": "1d", "days": days, "digest": digest,
    })
    tok_bound = REQ_BOUND.set(bound if bound is not None else symbol)
    try:
        yield
    finally:
        REQ_BOUND.reset(tok_bound)
        REQ_CHAN_SNAPSHOT.reset(tok_snap)


def _short(digest: str) -> str:
    return digest[:8] + "…"


def _route(text: str):
    from src.agents.chat.registry import chat_tool_schemas
    from src.harness.routing import route_for_request

    return route_for_request(text, chat_tool_schemas())


def _routed_names(route) -> set[str]:
    from src.agents.chat.registry import schema_tool_name

    return {schema_tool_name(s) for s in route.tools}


# ── ① 注册契约 ────────────────────────────────────────────────────────────

def test_tool_registered_with_snapshot_contract():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    tool = CHAT_TOOL_REGISTRY["get_chan_structure"]
    fn = tool.schema["function"]
    # 2026-11-17 二轮审计: 摘要不再由模型提供(模型只"点菜", 快照由服务端本轮持有),
    # 参数里也不再声明 snapshot_digest —— 免得提示词暗示"可以自己传摘要"。
    assert set(fn["parameters"]["required"]) == {"symbol", "instrument", "days"}
    assert "snapshot_digest" not in fn["parameters"]["properties"]
    assert "chan_snapshot.digest" in fn["description"]
    assert "不接受" in fn["description"] and "新快照" in fn["description"]
    assert "adjustment=unknown" in tool.caliber and "finality=unverified" in tool.caliber
    assert "中枢" in tool.caliber


# ── ② 真实路由: 单股 / 复盘+缠论 / 兜底 ────────────────────────────────────

def test_route_single_stock_chan_dispatches_tool(monkeypatch, db, users):
    route = _route("600000 缠论结构怎么看")
    assert route.recipe is not None and route.recipe.id == "chan_structure"
    assert "get_chan_structure" in _routed_names(route)

    # 真实派发: 把路由产出的白名单装进请求上下文, 再走 chat 的 _execute_tool。
    # 缺摘要 → 严格缺失态(证明白名单放行到了 handler, 而不是被"本轮不使用工具"挡掉)。
    from src.harness.context import REQ_BUDGET, REQ_TOOLS

    feed = _completed_feed()
    _chart_client(monkeypatch, feed)
    token = REQ_TOOLS.set(frozenset(_routed_names(route)))
    budget_token = REQ_BUDGET.set({"max": route.recipe.max_tool_calls, "used": 0})
    try:
        text = _call(db, _user(db, USER_ID), symbol="600000", instrument="stock", days=60)
    finally:
        REQ_BUDGET.reset(budget_token)
        REQ_TOOLS.reset(token)
    assert "缺少图表快照摘要" in text
    assert "本轮不使用工具" not in text
    assert feed.calls == 0


def test_route_review_plus_chan_dispatches_tool():
    """「复盘 + 缠论」带标的时不能被盘后复盘管线吞掉(那套配方没有这张图)。"""
    route = _route("600000 收盘复盘，缠论结构如何")
    assert route.recipe is not None and route.recipe.id == "chan_structure"
    assert "get_chan_structure" in _routed_names(route)


def test_route_plain_review_still_goes_to_postmarket():
    route = _route("今天收盘复盘，涨停梯队怎么样")
    assert route.recipe is not None and route.recipe.id == "postmarket_reviewer"
    assert "get_chan_structure" not in _routed_names(route)


def test_route_plain_single_stock_fallback_keeps_tool():
    route = _route("600000 这只票最近怎么样")
    assert route.recipe is None and route.reason == "stock_question"
    assert "get_chan_structure" in _routed_names(route)


def test_recipe_declares_snapshot_source_and_no_fabrication():
    from src.harness.recipes import get_recipe

    recipe = get_recipe("chan_structure")
    assert recipe is not None and recipe.pipeline is False and recipe.scope == "symbol"
    assert "chan_snapshot.digest" in recipe.prompt_fragment
    assert "未取到" in recipe.prompt_fragment
    assert "禁止" in recipe.prompt_fragment
    assert recipe.max_tool_calls >= len(recipe.tools)


# ── ③ 权限与缺失态: 取数之前就拒绝, 不声称已读取 ──────────────────────────

def test_missing_digest_is_strict_and_never_fetches(monkeypatch, db, users):
    feed = _completed_feed()
    _chart_client(monkeypatch, feed)          # 只装数据源替身, 不调用图表
    text = _call(db, _user(db, USER_ID), symbol="600000", instrument="stock", days=60)
    assert "缺少图表快照摘要" in text and "未读取任何结构数据" in text
    assert "chan_snapshot.digest" in text
    assert "分型 " not in text and "未实现" not in text
    assert feed.calls == 0, "缺摘要时不许自己去取一张新快照"


def test_no_identity_denied_before_fetch(monkeypatch, db):
    feed = _completed_feed()
    _chart_client(monkeypatch, feed)
    text = _call(db, None, symbol="600000", instrument="stock", days=60,
                 snapshot_digest="a" * 64)
    assert "需要已登录的用户身份" in text and "未读取任何结构数据" in text
    assert feed.calls == 0


def test_guest_without_quote_permission_denied_before_fetch(monkeypatch, db, users):
    feed = _completed_feed()
    _chart_client(monkeypatch, feed)
    text = _call(db, _user(db, GUEST_ID), symbol="600000", instrument="stock", days=60,
                 snapshot_digest="a" * 64)
    assert "无权限" in text
    assert "分型 " not in text
    assert feed.calls == 0


def test_model_supplied_digest_is_not_accepted(monkeypatch, db, users):
    """调用方(模型)自带摘要一律不接受: 没有本轮快照就没有结构, 且绝不取数。

    旧实现会把模型给的 64hex 当"用户这张图"直接算 —— 那可能是上一轮或别的票的摘要
    (2026-11-17 审计缺口 3)。
    """
    feed = _completed_feed()
    _chart_client(monkeypatch, feed)
    for supplied in ("A" * 64, "a" * 63, "z" * 64, "a" * 64):
        text = _call(db, _user(db, USER_ID), symbol="600000", instrument="stock",
                     days=60, snapshot_digest=supplied)
        assert "缺少图表快照摘要" in text
        assert "模型自带的 snapshot_digest 一律不接受" in text
        assert "分型 " not in text
    assert feed.calls == 0


def test_disabled_account_and_service_token_denied_before_fetch(monkeypatch, db):
    from types import SimpleNamespace

    feed = _completed_feed()
    _chart_client(monkeypatch, feed)
    args = dict(symbol="600000", instrument="stock", days=60)
    with _turn():
        disabled = SimpleNamespace(id="x", role="member", is_active=False)
        assert "已禁用或注销" in _call(db, disabled, **args)
        service = SimpleNamespace(id="x", role="member", is_active=True, is_service=True)
        assert "需要已登录的用户身份" in _call(db, service, **args)
    assert feed.calls == 0


def test_model_market_and_instrument_do_not_control_the_source(monkeypatch, db, users):
    """取数口径只看本轮复核结果: 模型给的 market=HK / instrument=auto 不能改变信源。

    旧实现会按模型参数臆测市场与标的类型; 现在这些参数只作对照, 真正生效的是
    消息入口复核过的那一份(symbol 不同才会被拒绝)。
    """
    feed = _completed_feed()
    client = _chart_client(monkeypatch, feed)
    digest = _chart_digest(client)
    user = _user(db, USER_ID)
    with _turn(symbol="600000", instrument="stock", days=60, digest=digest):
        text = _call(db, user, symbol="600000", market="HK", instrument="auto", days=0)
    assert "600000 CN stock" in text, text[:200]
    assert "分型 " in text

    # 没有本轮快照时, 参数再怎么"齐全"也不取数。
    calls_before = feed.calls
    text = _call(db, user, symbol="600000", market="CN", instrument="stock", days=60)
    assert "缺少图表快照摘要" in text
    assert feed.calls == calls_before


# ── ④ 真实图表摘要 → 只读端点 → 统一结果 ──────────────────────────────────

def test_real_chart_digest_returns_unified_structure(monkeypatch, db, users):
    feed = _pen_feed()
    client = _chart_client(monkeypatch, feed)
    digest = _chart_digest(client)

    with _turn(symbol="600000", instrument="stock", days=60, digest=digest):
        text = _call(db, _user(db, USER_ID), symbol="600000", market="CN",
                     instrument="stock", days=60)
    # 身份与口径
    assert "600000 CN stock" in text
    assert "身份依据:" in text
    assert "adjustment=unknown" in text and "finality=unverified" in text
    assert "分型规则 chan-fractal-inclusive-sealed-v1" in text
    assert f"笔规则 {PEN_KERNEL_VERSION}" in text
    # 结构内容: 分型含日期/状态/确认日, 笔含端点/状态/确认日/内核版本
    assert "2026-01-02 bottom" in text and "2026-01-06 top" in text
    assert "状态 confirmed" in text and "确认日 " in text
    assert "笔 3 笔" in text
    assert "up confirmed" in text and "→" in text
    assert f"内核 {PEN_KERNEL_VERSION}" in text
    assert "未实现(本工具不提供): segment, centre, buy_sell_point" in text
    # 只展示前 8 位(完整摘要属于本轮凭据, 不许随工具结果落库)
    assert _short(digest) in text
    assert digest not in text
    assert USER_ID not in text, "结果里不许回显用户内部 id"
    assert feed.calls >= 1


def test_other_symbol_rejected_without_fetch(monkeypatch, db, users):
    """本轮快照是 600000, 模型想算 600001 → 直接拒绝, 一次取数都不发。"""
    feed = _completed_feed()
    client = _chart_client(monkeypatch, feed)
    digest = _chart_digest(client, symbol="600000")

    calls_before = feed.calls
    with _turn(symbol="600000", instrument="stock", days=60, digest=digest):
        text = _call(db, _user(db, USER_ID), symbol="600001", instrument="stock", days=60)
    assert "不一致" in text and "未读取任何结构数据" in text
    assert "分型 " not in text
    assert feed.calls == calls_before, "跨标的必须在取数之前拒绝"


def test_stale_digest_after_chart_moves_is_rejected(monkeypatch, db, users):
    """本轮复核通过后图表又变了(开流到工具调用之间): 端点必须 409, 不许拿旧结构冒充当前图。"""
    feed = _completed_feed()
    client = _chart_client(monkeypatch, feed)
    digest = _chart_digest(client)

    # 图表已经换了数据(多一根未定型 bar): 旧摘要必须 409, 不许拿旧结构冒充当前图。
    # 同时清空进程内 60s 缓存 —— 等价于"图表请求落在另一个 worker", 这是真实漂移形态。
    feed.bars = feed.bars + [_k(TODAY, _TODAY_OHLC)]
    kl._kl_cache.clear()
    with _turn(symbol="600000", instrument="stock", days=60, digest=digest):
        text = _call(db, _user(db, USER_ID), symbol="600000", instrument="stock", days=60)
    assert "快照校验未通过" in text and "请刷新图表" in text
    assert "分型 " not in text


def test_turn_bound_symbol_must_match_snapshot(monkeypatch, db, users):
    """工具层兜底: 本轮请求目标(REQ_BOUND, 含"正文只写名字"解析出的代码)与快照不一致 → 拒绝。

    消息入口已按同一口径丢弃过一次; 这里证明即便上下文被别的路径装配(并发/旧入口),
    也不会出现"REQ_BOUND=600111 却按 000002 算结构"(2026-11-17 审计缺口 1 的第二层)。
    """
    feed = _completed_feed()
    client = _chart_client(monkeypatch, feed)
    digest = _chart_digest(client, symbol="000002")

    calls_before = feed.calls
    with _turn(symbol="000002", instrument="stock", days=60, digest=digest, bound="600111"):
        text = _call(db, _user(db, USER_ID), symbol="000002", instrument="stock", days=60)
    assert "本轮请求标的是 600111" in text and "不一致" in text
    assert "分型 " not in text
    assert feed.calls == calls_before


def test_no_completed_bars_reports_no_data(monkeypatch, db, users):
    feed = _Feed([_k(TODAY, _TODAY_OHLC)])
    client = _chart_client(monkeypatch, feed)
    digest = _chart_digest(client)

    with _turn(symbol="600000", instrument="stock", days=60, digest=digest):
        text = _call(db, _user(db, USER_ID), symbol="600000", instrument="stock", days=60)
    assert "没有已完成的K线" in text
    assert "分型 " not in text and "未返回任何结构" in text


def test_upstream_error_is_sanitized(monkeypatch, db, users):
    def _boom(*args, **kwargs):
        raise RuntimeError("dsn=postgresql://user:sup3rsecret@db.internal/sida")

    monkeypatch.setattr(kl, "_pg_klines", _boom)
    monkeypatch.setattr(kl, "_kl_cache", {})
    monkeypatch.setattr(chan, "_now", lambda: NOW)

    with _turn(symbol="600000", instrument="stock", days=60, digest="a" * 64):
        text = _call(db, _user(db, USER_ID), symbol="600000", instrument="stock", days=60)
    assert "K线数据源暂不可用" in text and "未返回任何结构" in text
    lowered = text.lower()
    for leaked in ("sup3rsecret", "postgresql", "db.internal", "runtimeerror", "traceback"):
        assert leaked not in lowered


# ── ⑤ 渲染契约(纯格式化, 不参与取数链) ───────────────────────────────────

def test_render_keeps_drift_and_truncation_counts():
    from src.agents.chat.tools_chan import render_chan_structure

    payload = {
        "symbol": "600000", "market": "CN", "instrument": "stock",
        "instrument_basis": "response_flag", "interval": "1d",
        "as_of_day": "2026-11-16", "snapshot_digest": "d" * 64,
        "rules_version": "chan-fractal-inclusive-sealed-v1",
        "pen_rules_version": PEN_KERNEL_VERSION, "source": "pg_klines_hypertable",
        "adjustment": "unknown", "finality": "unverified",
        "fractals": [{"kind": "bottom", "status": "confirmed",
                      "position_day": f"2026-01-{i + 1:02d}", "position_price": 1.0,
                      "confirmed_on": "2026-02-01"} for i in range(45)],
        "counts": {"total": 45, "confirmed": 45, "candidate": 0, "blocked": 0},
        "pens": [{"direction": "up", "status": "candidate", "confirmed_on": None,
                  "index_gap": 4, "kernel_version": PEN_KERNEL_VERSION,
                  "start": {"position_day": "2026-01-01", "price": 1.0, "revisions": 1,
                            "drift_days": ["2026-01-02"]},
                  "end": {"position_day": "2026-01-05", "price": 2.0, "revisions": 0}}],
        "pen_counts": {"total": 1, "confirmed": 0, "candidate": 1, "blocked": 0},
        "not_implemented": ["segment", "centre"],
    }
    text = render_chan_structure(payload)
    assert "共 45 条, 仅列最近 40 条" in text
    assert "起点修订 1 次(放弃日 2026-01-02)" in text
    assert "确认日 未确认" in text
    assert "未实现(本工具不提供): segment, centre" in text


# ── ⑥ 测试隔离: 实际 engine 必须是临时目录 SQLite ────────────────────────

def test_suite_uses_isolated_scratch_sqlite(users):
    from src.web.database import SessionLocal

    data_dir = Path(os.environ["DATA_DIR"]).resolve()
    assert data_dir.name.startswith("sida_test_data_"), f"DATA_DIR 不是临时目录: {data_dir}"
    session = SessionLocal()
    try:
        url = str(session.get_bind().url)
    finally:
        session.close()
    assert url.startswith("sqlite"), f"测试库必须是隔离 SQLite, 实际: {url.split('://')[0]}"
    db_path = Path(url.split("sqlite:///", 1)[-1]).resolve()
    assert any(p.name.startswith("sida_test_data_") for p in db_path.parents), db_path
    repo_data = (Path(__file__).resolve().parent.parent / "data").resolve()
    assert repo_data not in db_path.parents
