"""复审第 2 轮（2026-11-10）红绿用例：判据必须落在**生产真实 renderer 输出**上。

第 1 轮（5fffc2fc）把"猜"删了，但判据仍对着**自造文案**校准，于是生产里真实的
声明一条都没被认出来。本轮全部改成"由生产函数现算，再喂给管线"：

- 拒权：`tools_scan_read._denial_text`（真 handler `get_market_scan_ranks` 的返回）；
- 无快照 / 可用快照：`tools_scan_read._envelope` + `render_market_scan_ranks` /
  `render_dark_fund_top`（含 `_freshness` 的三种时效形态）；
- 盘后复盘：`market_replay.build_replay`（只桩掉外部取数）+ `market_replay.render`；
- 热榜 / 异动池空数据：registry 的 handler 本体（只桩掉 vendor 的 fetch）。

审计点（P0/P1）：

1. 真实拒权首行是 `[市场扫描榜] 无权限：…`（域名标签 + 声明词），旧实现只比对
   整行首字符 → 拒权被记成「已获可用证据」；
2. `[市场扫描榜] 暂无三榜快照…` / `[盘后复盘] 无数据。` / `…读取异常：KeyError`
   / `同花顺热榜暂无数据(…)` 同样是**显式失败**，不许算成功；
3. `【盘后复盘证据】<as_of>` 里的日期是 `build_replay` 的**取数/时钟日**，
   池子取空时它照样写今天 → 真实日期未知时绝不能被标今日；
4. 市场级工具全部返回人话字符串，没有 symbols JSON → 候选只能"未知"，
   不许写成"已有候选"或"没有候选"；
5. 代码只是**举例**（`有没有像 600111 这样的票`）时不许劫持市场问句；
6. `持仓 + 代码` 必须同时保住账户口径与指定股口径（`get_portfolio` 与
   `get_stock_quote@代码` 同时在车道里），不许只答其中一半。
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from src.agents.chat import tools_scan_read as tsr
from src.core import market_replay
from src.harness import pipeline_market as pm
from src.harness import pipeline_router
from src.harness.pipeline_market import MarketRun, compose_market_prompt, run_market_pipeline
from src.harness.recipes import get_recipe, match_recipe_for
from src.harness.routing import has_symbol_scope, route_for_request

CST = ZoneInfo("Asia/Shanghai")
#: 固定"今天"，让「今日 / 非今日 / 未知」三态可判定（管线时钟是注入点，
#: 真实 renderer 的行文不受影响 —— 它们由 `_freshness` 现算）。
TODAY = date(2026, 11, 10)


def _pin_today(monkeypatch, day: date = TODAY) -> None:
    monkeypatch.setattr(pm, "_cn_today", lambda: day)


def _now(day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, 16, 5, tzinfo=CST)


SCHEMAS = [{"type": "function", "function": {"name": n}} for n in (
    "get_market_scan_ranks", "get_hot_stocks", "get_sentiment_cycle",
    "get_strategy_signals", "get_market_anomalies", "get_dark_fund_top",
    "get_market_ranking", "get_market_news",
    "get_intraday_profile", "get_cancel_order_intent", "get_main_intent",
    "get_fund_structure", "get_portfolio", "get_stock_quote",
    "get_technical_analysis", "get_trend_profile",
)]

# 用户截图原句（会话里绑着 002361）
REPORTED = "今天尾盘有适合建仓的股票吗，可以有创业板"
HOLDINGS = "我的持仓尾盘要不要加仓"
SCAN_TOOLS = {"get_market_scan_ranks", "get_hot_stocks", "get_dark_fund_top"}


def _tool_names(route) -> set[str]:
    return {s["function"]["name"] for s in route.tools}


# ══════════════════════════════════════════════ 生产 renderer 输出（真函数现算）

_RANKS_PAYLOAD = {
    "universe": 5218, "computed": 5104, "skipped": 114, "dark_days": 12,
    "generated_at": "2026-11-10 15:35:12",
    "new_g_points": [{"symbol": "600111"}],
    # 审计反例种子：这条 3001234.56 万元里有一段 6 位数字，绝不许被当候选代码
    "dark_top": [{"symbol": "300750", "dark_net_wan": 1688.4, "approximation": True}],
    "activity_top": [{"symbol": "600519", "activity": 91.2, "level": "A"}],
    "zljc": {"600111": {"net": 1}},
}
_DARK_PAYLOAD = {
    "universe": 5218, "computed": 4803, "generated_at": "2026-11-10 15:36:01",
    "top": [{"symbol": "600111", "name": "北方稀土", "main_net_wan": 82311.5,
             "total_amount_wan": 3001234.56}],
}


def _envelope(monkeypatch, payload: dict, *, data_day: date | None = None,
              expected: date | None = None, source: str = tsr.RANKS_SOURCE) -> dict:
    """走生产 `_envelope` 造信封；`data_day` 非空 = 假定已接入可信数据日字段。

    生产现状（见 `tools_scan_read` 模块 docstring）是 `_resolve_data_date` 恒返回
    None：写入日 ≠ 数据日，所以 `data_day=None` 就是**真实形态**；非空只在
    "将来生成方落了可信字段"这一前提成立时才有意义，用来钉住管线的三态。
    """
    if data_day is not None:
        monkeypatch.setattr(tsr, "_resolve_data_date", lambda *a, **k: (data_day, ""))
        monkeypatch.setattr(tsr, "_expected_data_day", lambda *a, **k: expected or data_day)
        monkeypatch.setattr(tsr, "_trading_gap", lambda *a, **k: 1)
    row = SimpleNamespace(snapshot_date=data_day or TODAY, stock_market="CN",
                          updated_at=None, created_at=None)
    return tsr._envelope(
        row=row, payload=payload, source=source, market="CN",
        now=_now(), unavailable_note="三榜扫描未成功",
    )


def _ranks_text(monkeypatch, *, data_day=None, expected=None, payload=None) -> str:
    data = _envelope(monkeypatch, payload or _RANKS_PAYLOAD,
                     data_day=data_day, expected=expected)
    return tsr.render_market_scan_ranks(data)


def _dark_text(monkeypatch, *, data_day=None, expected=None) -> str:
    data = _envelope(monkeypatch, _DARK_PAYLOAD, data_day=data_day, expected=expected,
                     source=tsr.DARK_FUND_SOURCE)
    return tsr.render_dark_fund_top(data)


def _denial_text(status: int = 403, detail=None) -> str:
    """真 handler 的拒权文案（`_denial_text` 就是 handler 的 except 分支）。"""
    return tsr._denial_text(HTTPException(status, detail or {"pro_only": True}))


@pytest.fixture()
def replay_text(monkeypatch):
    """`market_replay.build_replay` + `render`：只桩外部取数，行文全是真的。"""
    def _make(zt_rows=None, zb_rows=None, dt_rows=None, zt_err=None, *, total=None,
              date_str=None):
        zt = list(zt_rows or [])
        zb = list(zb_rows or [])
        dt = list(dt_rows or [])
        pools = {"zt": (zt, zt_err, total if total is not None else len(zt)),
                 "zb": (zb, None, len(zb)), "dt": (dt, None, len(dt))}
        monkeypatch.setattr(market_replay, "_fetch_pool",
                            lambda kind, date_str: pools[kind])
        monkeypatch.setattr(market_replay, "_tq_crosscheck",
                            lambda date_str: {"available": False, "reason": "stub"})
        monkeypatch.setattr(market_replay, "_indices_flow", lambda: {"available": False})
        monkeypatch.setattr(market_replay, "_emotion",
                            lambda date_str, metrics: {"available": False})
        # `date_str=None` = 生产里 `get_market_replay` 的默认路径（as_of 取时钟当日）
        return market_replay.render(market_replay.build_replay(date_str))
    return _make


# ═══════════════════════════════════════════════════════ 1. 显式拒权 / 失败识别


def test_real_denial_first_line_is_recognised():
    """审计 P0：生产拒权首行 `[市场扫描榜] 无权限：…` 必须判 denied。"""
    for status, detail in (
        (403, {"pro_only": True, "message": "该榜为 Pro 专属功能"}),
        (403, {"pro_guide": True, "message": "试用次数已用完"}),
        (403, {"message": "当前账号不具备该数据权限"}),
        (401, {"message": "未登录"}),
    ):
        text = _denial_text(status, detail)
        assert text.startswith("[市场扫描榜] "), text[:40]
        assert pm.tool_status(text) == "denied", text[:60]


@pytest.mark.asyncio
async def test_real_denial_never_becomes_evidence(monkeypatch):
    """真 handler 拒权 → 不进证据；整轮全拒权 → 不产出结论（回落 loop）。"""
    monkeypatch.setattr(tsr, "read_market_scan_ranks",
                        lambda *a, **k: (_ for _ in ()).throw(
                            HTTPException(403, {"pro_only": True, "message": "Pro 专属"})))
    denied = await tsr._tool_get_market_scan_ranks(None, {})
    assert pm.tool_status(denied) == "denied"

    tools = list(get_recipe("entry_scanner").tools)
    ex = _Exec({t: denied for t in tools})
    run = await run_market_pipeline(get_recipe("entry_scanner"), ex)
    assert run is None, "全是拒权 → 不许产出结论"
    assert ex.calls, "工具真的被调过（不是空跑）"


@pytest.mark.asyncio
async def test_real_denial_is_counted_per_tool(monkeypatch):
    monkeypatch.setattr(tsr, "read_market_scan_ranks",
                        lambda *a, **k: (_ for _ in ()).throw(HTTPException(403, {"pro_only": True})))
    denied = await tsr._tool_get_market_scan_ranks(None, {})
    tools = list(get_recipe("entry_scanner").tools)
    out = {t: denied for t in tools}
    out[tools[0]] = _ranks_text(monkeypatch)
    run = await run_market_pipeline(get_recipe("entry_scanner"), _Exec(out))
    assert run is not None and run.ok == 1
    assert set(run.denied) == set(tools[1:]), run.denied
    note = run.missing_note()
    assert "拒权" in note and "未取到" not in note.split("拒权")[0]
    assert pm._missing_prefix(run).startswith("> 数据口径：")


def test_real_no_snapshot_is_declared_unavailable():
    """`available=False` 的真实行文：`[市场扫描榜] 暂无三榜快照…`。"""
    ranks = tsr.render_market_scan_ranks(
        {"available": False, "note": "暂无三榜快照", "expected_data_day": "2026-11-10"})
    dark = tsr.render_dark_fund_top({"available": False, "note": "暂无快照"})
    for text in (ranks, dark):
        assert pm.tool_status(text) == "unavailable", text[:60]
        assert pm.declared_data_day(text) == ("unknown", None), "无快照不声明数据日"


def test_real_replay_empty_and_error_lines_are_not_evidence(replay_text):
    """复盘的三条真实失败行文：无数据 / 取数失败 / 读取异常。"""
    assert pm.tool_status(market_replay.render({})) == "unavailable"
    empty = replay_text(zt_err="zt池取数失败:KeyError", total=0)
    assert empty.startswith("【盘后复盘证据】"), empty[:40]
    assert pm.tool_status(empty) == "ok", "有 as_of 的复盘报告本体是可用证据"
    assert pm.tool_status(
        "[盘后复盘] 取数失败：涨停池与跌停池均未返回数据——本维度如实标注无数据，不做推测。"
    ) == "unavailable"
    assert pm.tool_status(
        "[盘后复盘] 读取异常：KeyError——本维度如实标注无数据。"
    ) == "unavailable"


@pytest.mark.asyncio
async def test_replay_handler_failure_text_is_unavailable(monkeypatch):
    """registry 的 `get_market_replay` 本体：取数失败/抛错两种真实行文。"""
    from src.agents.chat import registry

    monkeypatch.setattr(market_replay, "read_latest", lambda *a, **k: None)
    monkeypatch.setattr(market_replay, "build_replay", lambda *a, **k: None)
    text = await registry._tool_get_market_replay(None, {})
    assert text.startswith("[盘后复盘] 取数失败"), text[:40]
    assert pm.tool_status(text) == "unavailable"

    def _boom(*_a, **_k):
        raise KeyError("pool")

    monkeypatch.setattr(market_replay, "build_replay", _boom)
    text2 = await registry._tool_get_market_replay(None, {})
    assert text2.startswith("[盘后复盘] 读取异常"), text2[:40]
    assert pm.tool_status(text2) == "unavailable"


@pytest.mark.asyncio
async def test_real_empty_vendor_handlers_are_unavailable(monkeypatch):
    """热榜 / 异动池「暂无数据」是工具自述，不是行情描述。"""
    from marketdata.vendors.em_anomaly import EmAnomalyVendor
    from marketdata.vendors.ths_hot import ThsHotListVendor
    from src.agents.chat import registry

    monkeypatch.setattr(ThsHotListVendor, "fetch", lambda self, *a, **k: [])
    monkeypatch.setattr(EmAnomalyVendor, "fetch", lambda self, *a, **k: [])
    hot = await registry._tool_get_hot_stocks(None, {})
    anomaly = await registry._tool_get_market_anomalies(None, {})
    for text in (hot, anomaly):
        assert "暂无数据" in text, text[:60]
        assert pm.tool_status(text) == "unavailable", text[:60]


@pytest.mark.asyncio
async def test_execution_layer_refusals_are_unavailable(monkeypatch):
    """执行层（`chat._execute_tool` / `registry.charge_tool_budget`）的真实行文。"""
    from src.agents.chat import registry
    from src.web.api import chat as chat_api

    # 1) 配方白名单拒绝
    tok = chat_api._REQ_TOOLS.set(frozenset({"get_trend_profile"}))
    lab = chat_api._REQ_RECIPE_LABEL.set("全景诊断")
    lbl = chat_api._REQ_RECIPE_LABEL.get()
    try:
        text = await chat_api._execute_tool(None, "get_stock_dossier", {})
    finally:
        chat_api._REQ_TOOLS.reset(tok)
        chat_api._REQ_RECIPE_LABEL.reset(lab)
    assert lbl == "全景诊断" and "本轮不使用工具" in text, text[:60]
    assert pm.tool_status(text) == "unavailable"

    # 2) 预算用尽
    budget_text = registry.charge_tool_budget({"used": 1, "max": 1}, "get_hot_stocks",
                                              "建仓候选筛查")
    assert budget_text is not None
    assert pm.tool_status(budget_text) == "unavailable", budget_text

    # 3) 未知工具
    unknown = await chat_api._execute_tool(None, "no_such_tool_xyz", {})
    assert pm.tool_status(unknown) == "unavailable", unknown

    # 4) 身份拒绝（requires="self" 的工具 + 无用户）
    anon = await chat_api._execute_tool(None, "get_portfolio", {})
    assert pm.tool_status(anon) == "denied", anon

    # 5) handler 抛错
    async def _boom(db, args, user=None):
        raise RuntimeError("boom")

    monkeypatch.setitem(
        registry.CHAT_TOOL_REGISTRY, "test_boom_tool",
        registry.ChatTool(name="test_boom_tool", schema=None, handler=_boom,
                          caliber="测试", requires="", fallback=True, charges_budget=False))
    err = await chat_api._execute_tool(None, "test_boom_tool", {})
    assert err.startswith("工具执行出错"), err[:40]
    assert pm.tool_status(err) == "unavailable"


def test_prose_permission_words_are_not_a_denial():
    """正文里的口径说明不许把可用证据判成拒权（第 1 轮的反例，继续守住）。"""
    body = ("=== 两融口径 ===\n融资融券未开通的标的不计入统计；"
            "未开通两融的营业部按 0 计。600111 净买入 1.2 亿")
    assert pm.tool_status(body) == "ok"


# ═══════════════════════════════════════════════════ 2. 数据日：真实输出不许标今日


def test_real_production_envelope_never_declares_a_data_day(monkeypatch):
    """生产形态（data_date 恒 None）→ 未知；行文里只有"数据日期未核实"。"""
    text = _ranks_text(monkeypatch)
    assert "数据日期未核实" in text
    assert pm.tool_status(text) == "ok", "快照可用：unknown 是时态未知，不是不可用"
    assert pm.declared_data_day(text) == ("unknown", None)


@pytest.mark.asyncio
async def test_real_replay_dated_first_line_is_not_today(replay_text, monkeypatch):
    """审计 P0：`【盘后复盘证据】<时钟当日>` 不许被当成"今日数据"。"""
    _pin_today(monkeypatch)
    text = replay_text(zt_rows=[{"symbol": "600111", "name": "北方稀土",
                                 "board_days": 3, "first_seal": "09:31:00",
                                 "seal_amount_wan": 12000, "open_times": 0,
                                 "industry": "稀土"}],
                       date_str=f"{TODAY:%Y%m%d}")
    assert text.startswith(f"【盘后复盘证据】{TODAY:%Y%m%d}"), text[:40]
    # 首行里就有"今天"，但那是取数日：池子取空时照样写今天
    assert pm.declared_data_day(text) == ("unknown", None), \
        "as_of 是取数/时钟日，池子取空时它照样写今天 → 只能算未知"

    run = await run_market_pipeline(get_recipe("postmarket_reviewer"),
                                    _Exec({t: text for t in
                                           get_recipe("postmarket_reviewer").tools}))
    assert run is not None
    assert run.data_date is None and run.data_date_status == "unverified"
    assert "get_market_replay" in run.undated
    assert not run.stale, "未知 ≠ 过期"
    system, user = compose_market_prompt(get_recipe("postmarket_reviewer"), run, "帮我复盘今天")
    assert "不得称今日" in system or "不得称今日" in user


def test_verified_data_day_renders_today_only_from_structured_field(monkeypatch):
    """只有结构化 verified 数据日才会渲染出 `数据日 X（…）`，管线据此判三态。"""
    _pin_today(monkeypatch)
    text = _ranks_text(monkeypatch, data_day=TODAY, expected=TODAY)
    assert text.startswith(f"数据日 {TODAY.isoformat()}"), text[:60]
    assert pm.declared_data_day(text) == ("today", TODAY.isoformat())


@pytest.mark.asyncio
async def test_verified_stale_day_is_not_today(monkeypatch):
    _pin_today(monkeypatch)
    yesterday = TODAY - timedelta(days=1)
    text = _ranks_text(monkeypatch, data_day=yesterday, expected=TODAY)
    assert text.startswith("⚠️ 快照已过期"), text[:60]
    assert pm.declared_data_day(text) == ("not_today", yesterday.isoformat())
    tools = list(get_recipe("entry_scanner").tools)
    run = await run_market_pipeline(get_recipe("entry_scanner"), _Exec({t: text for t in tools}))
    assert run is None, "整轮只有非今日 → 不许当今日证据写结论"


@pytest.mark.asyncio
async def test_partial_verified_day_does_not_speak_for_the_whole_run(monkeypatch):
    """只有一部分维度声明了数据日 → partial，提示词不许把它写成整轮今日。"""
    _pin_today(monkeypatch)
    tools = list(get_recipe("entry_scanner").tools)
    out = {t: "=== 热榜 ===\n600111 人气第 3" for t in tools}
    out[tools[0]] = _ranks_text(monkeypatch, data_day=TODAY, expected=TODAY)
    run = await run_market_pipeline(get_recipe("entry_scanner"), _Exec(out))
    assert run is not None
    assert run.data_date == TODAY.isoformat()
    assert run.data_date_status == "partial", run.data_date_status
    system, user = compose_market_prompt(get_recipe("entry_scanner"), run, REPORTED)
    assert "部分工具声明的数据日" in user
    assert "不得据此把整轮称今日" in user
    assert "时效未知，不得称今日" in run.missing_note()


@pytest.mark.asyncio
async def test_prose_dates_in_evidence_are_not_data_days(monkeypatch):
    _pin_today(monkeypatch)
    body = ("=== 公告 ===\n2027-01-01 限售解禁，2026-11-09 昨日收盘 12.30；"
            "本页生成于 2026-11-10 15:05")
    assert pm.declared_data_day(body) == ("unknown", None)
    tools = list(get_recipe("entry_scanner").tools)
    out = {t: body for t in tools}
    out[tools[0]] = _ranks_text(monkeypatch)
    run = await run_market_pipeline(get_recipe("entry_scanner"), _Exec(out))
    assert run is not None and run.data_date is None and not run.stale
    assert all(label in run.undated for label in tools), run.undated


# ═══════════════════════════════════════════════════════ 3. 候选：字符串没有名单


def test_real_renderer_text_has_no_symbols_payload(monkeypatch):
    """三榜/DDE 都是人话：候选只能"未知"，出站也不许带空数组。"""
    for text in (_ranks_text(monkeypatch),):
        assert not text.lstrip().startswith("{"), "渲染文本不是 JSON"
        assert pm.declared_candidates(text) == ("unknown", [])
    run = MarketRun(market="CN", ok=1, outputs=[("get_market_scan_ranks", _ranks_text(monkeypatch))])
    assert run.candidates == [] and run.candidates_status == "unknown"
    assert run.no_candidates is False, "未知 ≠ 没有候选"
    payload = pm.evidence_payload(run)
    assert "candidates" not in payload, "未声明名单时不许出站空数组冒充「没有候选」"
    assert payload["candidates_status"] == "unknown"
    assert "不是「没有候选」" in payload["missing_note"]


def test_only_structured_empty_list_means_no_candidates():
    declared_empty = json.dumps({"available": True, "symbols": []}, ensure_ascii=False)
    run = MarketRun(market="CN", ok=1, outputs=[("some_tool", declared_empty)])
    assert run.candidates_status == "declared" and run.no_candidates is True
    assert pm.evidence_payload(run)["candidates"] == []


def test_digits_inside_real_dde_render_are_not_candidates(monkeypatch):
    """`成交额 3,001,234.56 万元` 这种长数字不许被当候选代码。"""
    dark = _dark_text(monkeypatch)
    assert "3,001,234.56" in dark
    assert pm.declared_candidates(dark) == ("unknown", [])


# ═══════════════════════════════════════════════════════ 4. 路由：原句 / 代码 / 举例


@pytest.mark.parametrize("q", [
    REPORTED,
    "尾盘有适合建仓的股票吗",
    "今天有什么票可以埋伏",
    "今天尾盘买什么",
])
def test_reported_sentence_with_bound_still_scans_market(q):
    route = route_for_request(q, SCHEMAS, bound="002361")
    assert route.recipe is not None and route.recipe.id == "entry_scanner", q
    assert route.recipe.scope == "market"
    assert "get_market_scan_ranks" in _tool_names(route), "扫描工具必须真的下发"
    assert "get_intraday_profile" not in _tool_names(route), "单标的工具不许混进来"


@pytest.mark.parametrize("q,bound", [
    ("002361 适合建仓吗", None),
    ("002361 尾盘有适合建仓吗", "600111"),
    ("300750 放量了吗", "002361"),
    ("600111 现价多少，还能买吗", None),
    ("300750 这样的走势还能追吗", None),
])
def test_explicit_code_keeps_symbol_scope(q, bound):
    assert has_symbol_scope(q, bound) is True, q
    route = route_for_request(q, SCHEMAS, bound=bound)
    assert route.reason != "market_question", q
    assert route.recipe is None or route.recipe.scope == "symbol", q
    if route.recipe is not None:
        assert not (_tool_names(route) & SCAN_TOOLS), f"{q} 不该拿到全市场扫描工具"


@pytest.mark.parametrize("q", [
    "今天有没有像 600111 这样的票可以建仓",
    "有没有类似600111这种低位放量的票",
    "今天还有哪些票能买，比如 600111 这种",
])
def test_example_code_does_not_hijack_market_question(q):
    """审计 P1：代码只是例子 → 市场口径；不许被单标的管线代答。"""
    assert has_symbol_scope(q) is False, q
    assert has_symbol_scope(q, "002361") is False, f"{q} 不许被绑定拽走"
    route = route_for_request(q, SCHEMAS, bound="002361")
    assert route.recipe is None or route.recipe.scope == "market", \
        f"{q} → {getattr(route.recipe, 'id', None)}"
    assert route.reason != "recipe:intraday_analyst"
    if route.recipe is None:
        assert route.reason in ("market_question", "stock_question"), route.reason


def test_batch_question_without_code_is_not_dragged_into_symbol_pipeline():
    """没有任何代码的「要一批」问句：绑定不许把它变成单标的管线（也不许凭空升级口径）。"""
    q = "有没有低位放量的票"
    assert has_symbol_scope(q, "002361") is False
    route = route_for_request(q, SCHEMAS, bound="002361")
    assert route.recipe is None or route.recipe.scope == "market", \
        getattr(route.recipe, "id", None)
    assert route.reason != "recipe:intraday_analyst"


def test_fallback_stock_reason_honours_symbol_guard(monkeypatch):
    """兜底路径上「这是单股问句」的判据必须听护栏（`symbol_guard.is_stock_question`）。

    2026-11-10 复审把这一档换成 `has_symbol_scope`（只看正文代码/绑定这类字面证据），
    护栏说"是单股问句"的输入于是掉到 `default` —— 口径被记成闲聊、温度 0.5。
    正文里挑不出单股证据时，护栏就是唯一权威，故用桩替换它来钉这一档。
    """
    import src.core.symbol_guard as guard

    monkeypatch.setattr(guard, "is_stock_question", lambda _t: True)
    route = route_for_request("随便看看这个", SCHEMAS)
    assert route.reason == "stock_question", route.reason
    assert route.temperature == 0.2, "单股问句是数据题，温度必须降到 0.2"


@pytest.mark.parametrize("q", [
    "有没有类似600111这种低位放量的票",
    "今天还有哪些票能买，比如 600111 这种",
])
def test_guard_stock_detection_does_not_hijack_batch_question(monkeypatch, q):
    """护栏的 `is_stock_question` 对举例代码也返回 True，但口径是市场 —— 不许被劫持回去。"""
    import src.core.symbol_guard as guard

    monkeypatch.setattr(guard, "is_stock_question", lambda _t: True)
    route = route_for_request(q, SCHEMAS)
    assert route.recipe is None, getattr(route.recipe, "id", None)
    assert route.reason == "market_question", route.reason


def test_singular_reference_still_means_the_bound_stock():
    """"这票有没有放量" 是单数指代：绑定标的仍然是问句对象，不许被当成"要一批"。"""
    q = "这票有没有放量"
    assert has_symbol_scope(q, "002361") is True
    route = route_for_request(q, SCHEMAS, bound="002361")
    assert route.recipe is not None and route.recipe.id == "intraday_analyst", \
        getattr(route.recipe, "id", None)


def test_example_code_still_routes_to_scanner_when_verb_is_clear():
    route = route_for_request("今天有没有像 600111 这样的票可以建仓", SCHEMAS, bound="002361")
    assert route.recipe is not None and route.recipe.id == "entry_scanner"
    assert "get_market_scan_ranks" in _tool_names(route)


def test_industry_words_are_not_symbols():
    for q in ("创业板今天怎么样", "半导体板块今天怎么样", "今天大盘怎么样"):
        assert has_symbol_scope(q) is False, q
        assert has_symbol_scope(q, "002361") is False, q


@pytest.mark.parametrize("bound", ["002361", None])
@pytest.mark.parametrize("q", [HOLDINGS, "我的持仓尾盘要不要减", "我的持仓现在什么风险"])
def test_portfolio_question_keeps_get_portfolio(q, bound):
    route = route_for_request(q, SCHEMAS, bound=bound)
    assert route.recipe is not None, q
    assert "get_portfolio" in route.recipe.tools, f"{q}(bound={bound}) → {route.recipe.id}"


def test_portfolio_guard_holds_without_route_layer():
    r = match_recipe_for(HOLDINGS, has_symbol=False)
    assert r is not None and "get_portfolio" in r.tools


def test_routing_never_touches_network(monkeypatch):
    from src.core import symbol_guard

    calls: list[str] = []

    def _spy(word, *_a, **_kw):
        calls.append(str(word))
        return []

    monkeypatch.setattr(symbol_guard, "_tq_match", _spy)
    monkeypatch.setattr(symbol_guard, "_match_cached", _spy)
    monkeypatch.setattr(symbol_guard, "resolve_mentions", lambda *_a, **_kw: {})
    for q in (REPORTED, HOLDINGS, "002361 适合建仓吗", "三花智控这波怎么样",
              "创业板今天怎么样", "分时怎么样", "帮我复盘今天",
              "有没有类似600111这种低位放量的票"):
        has_symbol_scope(q)
        has_symbol_scope(q, "002361")
        route_for_request(q, SCHEMAS, bound="002361")
    assert calls == [], f"路由层同步联网（TQ 探测 3s 阻塞事件循环）: {calls}"


def test_unverified_name_is_not_guessed_and_not_market_answered(monkeypatch):
    from src.core import symbol_guard

    calls: list[str] = []
    monkeypatch.setattr(symbol_guard, "_match_cached",
                        lambda w, *a, **k: (calls.append(w), [])[1])
    q = "三花智控这波怎么样"
    route = route_for_request(q, SCHEMAS, bound=None)
    assert route.reason == "stock_question", route.reason
    assert route.recipe is None or getattr(route.recipe, "scope", "symbol") == "symbol"
    assert not str(route.reason).startswith("recipe:entry_scanner")
    assert calls == []


def test_name_like_mention_keeps_symbol_scope(monkeypatch):
    from src.core import symbol_guard

    calls: list[str] = []
    monkeypatch.setattr(symbol_guard, "_match_cached",
                        lambda w, *a, **k: (calls.append(w), [])[1])
    assert has_symbol_scope("三花智控这波怎么样") is True
    assert has_symbol_scope("三花智控所在的板块今天怎么样") is False
    assert calls == []


# ═══════════════════════════════════════════════════ 5. 持仓 + 代码：账户与指定股双语义


def test_named_code_enters_watchlist_first():
    wl = pipeline_router.watchlist_for("我的持仓里 600111 要不要减", None, None)
    assert wl[:1] == ["600111"], wl


def test_bound_alone_never_enters_watchlist():
    """`bound` 是会话状态：不许把上一轮的票塞进这一轮的市场问句。"""
    assert pipeline_router.watchlist_for("今天尾盘有适合建仓的股票吗", None, None) == []


def test_example_code_never_enters_watchlist():
    q = "有没有类似600111这种低位放量的票"
    assert pipeline_router.watchlist_for(q, None, None) == []


def test_portfolio_plus_code_keeps_both_lanes():
    """账户口径（get_portfolio）+ 指定股口径（get_stock_quote@600111）必须同时在。"""
    q = "我的持仓里 600111 要不要减"
    route = route_for_request(q, SCHEMAS, bound="002361")
    assert route.recipe is not None
    assert "get_portfolio" in route.recipe.tools, "账户维度不许丢"

    steps = pm.build_steps(route.recipe,
                           watchlist=pipeline_router.watchlist_for(q, None, None))
    labels = [s.label for s in steps]
    assert any(s.tool == "get_portfolio" and not s.skip for s in steps), labels
    assert "get_stock_quote@600111" in labels, f"指定股维度不许丢: {labels}"
    assert not any(l == "get_stock_quote@002361" for l in labels), \
        "绑定标的不是这句话的问句口径"


def test_account_question_without_code_stays_account_only():
    q = "我的持仓尾盘要不要加仓"
    wl = pipeline_router.watchlist_for(q, None, None)
    assert wl == [], "没有自选/没有点名 → symbol 车道如实空表（写「未取到」）"


# ═══════════════════════════════════════════════════════════════ 测试夹具


class _Exec:
    def __init__(self, out: dict[str, str]):
        self.out = out
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, tool, args):
        self.calls.append((tool, dict(args)))
        val = self.out.get(tool, "")
        if isinstance(val, BaseException):
            raise val
        return val


# ═══════════════════════════════════════════════════════════ 6. 出站结构


def test_evidence_payload_keeps_unknown_flags():
    run = MarketRun(market="CN", ok=1, outputs=[("get_hot_stocks", "=== 热榜 ===\n600111")])
    run.denied = ["get_market_news"]
    run.stale = [("get_market_scan_ranks", "2026-11-09")]
    run.undated = ["get_hot_stocks"]
    d = json.loads(json.dumps(pm.evidence_payload(run), ensure_ascii=False))
    assert d["denied"] == ["get_market_news"]
    assert d["stale"][0][0] == "get_market_scan_ranks"
    assert d["data_date"] is None and d["data_date_status"] == "unverified"
    assert d["candidates_status"] == "unknown"
    assert "candidates" not in d
    assert d["undated"] == ["get_hot_stocks"]


def test_pipeline_is_not_started_when_every_tool_declares_failure():
    async def go():
        ex = _Exec({t: "[市场扫描榜] 暂无三榜快照（最近应有数据日 2026-11-10）；只读快照。"
                    for t in get_recipe("entry_scanner").tools})
        return await run_market_pipeline(get_recipe("entry_scanner"), ex)

    assert asyncio.run(go()) is None
