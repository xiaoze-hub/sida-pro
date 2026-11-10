"""复审 P0（2026-11-10）红绿用例：绑定不许劫持、路由不许联网、证据只认「声明」。

上一版（ee0dd811）修的方向对，但引入了三处**新的推断**，审计以阻断退回：

1. `pipeline_market.payload_date()` 把证据正文里**最晚的日期**当数据日 —
   `「2027-01-01 限售解禁」` 会让未来日期冒充今日；页脚/报告期同理不可信。
2. `MarketRun.candidate_codes` 用 6 位数字正则**扫正文**当候选 —
   `「成交额 3001234.56 万元」` 会造出一只不存在的 300123。
3. `_verdict()` 用**子串**判权限/空库 — `「融资融券未开通的标的不计入」`
   是口径说明，却会把整条可用证据丢掉（比假证据更坏：可用数据被静默删除）。

4. `routing._lookup_name()` 在**路由链路里同步发网络请求**（TQ `match_stkinfo`，
   实测一次 3s 探测预算），既阻塞事件循环，又在源故障时给出不确定结论。

本轮的守则（不靠提示词，靠代码）：

- 路由：只有**正文明确写了 6 位代码**才算"问某只标的"；没有代码的市场问句不许被
  会话绑定改道；名字核不到就**不猜**，也不许换成市场配方**代答**；路由层一个网络
  请求都不发（名字解析在上游 `enter_request_ctx` 完成）。
- 管线：拒权/失败只认**首行声明前缀**与 JSON 显式字段；数据日只认**工具声明的
  数据日**（认不出＝未知，不许称今日/实时）；候选只认**结构化名单**（文本一律未知，
  不是"没有"）。缺口由 `missing_note()` / `_missing_prefix()` 确定性写出。
"""
from __future__ import annotations

import json
from datetime import date as _dt_date, timedelta

import pytest

from src.harness import pipeline_market
from src.harness.pipeline_market import (
    MarketRun,
    compose_market_prompt,
    run_market_pipeline,
)
from src.harness.recipes import get_recipe, match_recipe_for
from src.harness.routing import has_symbol_scope, route_for_request

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


# ═══════════════════════════════════════════════════════ 路由：图中原句
# 原句 + bound=002361 仍必须走市场扫描（`尾盘` 抓到的 intraday_analyst 不许赢）


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
    assert "get_intraday_profile" not in _tool_names(route), "单标的工具不许再混进来"


def test_bound_is_not_the_scope_of_this_sentence():
    assert has_symbol_scope(REPORTED, "002361") is False
    assert has_symbol_scope(HOLDINGS, "002361") is False


# ═══════════════════════════════════════════════════════ 路由：正文代码
# 只有**明确写了代码**才算问某只标的；此时 bound 与市场词都不许改道


@pytest.mark.parametrize("q,bound", [
    ("002361 适合建仓吗", None),
    ("002361 这个位置能建仓吗", None),
    ("002361 尾盘有适合建仓吗", "600111"),
    ("300750 放量了吗", "002361"),
])
def test_explicit_code_gets_symbol_scope_not_market_scan(q, bound):
    assert has_symbol_scope(q, bound) is True, q
    route = route_for_request(q, SCHEMAS, bound=bound)
    assert route.recipe is not None and route.recipe.scope == "symbol", q
    assert not (_tool_names(route) & SCAN_TOOLS), f"{q} 不该拿到全市场扫描工具"


def test_industry_words_are_not_symbols():
    """"创业板"/"板块"是市场口径词，不是标的（旧实现见后缀就猜）。"""
    for q in ("创业板今天怎么样", "半导体板块今天怎么样", "今天大盘怎么样"):
        assert has_symbol_scope(q) is False, q
        assert has_symbol_scope(q, "002361") is False, q


# ═══════════════════════════════════════════════════════ 路由：持仓优先


@pytest.mark.parametrize("bound", ["002361", None])
@pytest.mark.parametrize("q", [HOLDINGS, "我的持仓尾盘要不要减", "我的持仓现在什么风险"])
def test_portfolio_question_keeps_get_portfolio(q, bound):
    route = route_for_request(q, SCHEMAS, bound=bound)
    assert route.recipe is not None, q
    assert "get_portfolio" in route.recipe.tools, f"{q}(bound={bound}) → {route.recipe.id}"


def test_portfolio_guard_holds_without_route_layer():
    r = match_recipe_for(HOLDINGS, has_symbol=False)
    assert r is not None and "get_portfolio" in r.tools


# ═══════════════════════════════════════════════════════ 路由：禁止同步联网


def _spy_network(monkeypatch) -> list[str]:
    """记录路由链路上的名称检索：任何一次调用都是违规（返回空 = 核不到）。"""
    from src.core import symbol_guard

    calls: list[str] = []

    def _spy(word, *_a, **_kw):
        calls.append(str(word))
        return []

    monkeypatch.setattr(symbol_guard, "_tq_match", _spy)
    monkeypatch.setattr(symbol_guard, "_match_cached", _spy)
    monkeypatch.setattr(symbol_guard, "resolve_mentions", lambda *_a, **_kw: {})
    return calls


def test_routing_never_touches_network(monkeypatch):
    calls = _spy_network(monkeypatch)
    for q in (REPORTED, HOLDINGS, "002361 适合建仓吗", "三花智控这波怎么样",
              "创业板今天怎么样", "分时怎么样", "帮我复盘今天"):
        has_symbol_scope(q)
        has_symbol_scope(q, "002361")
        route_for_request(q, SCHEMAS, bound="002361")
    assert calls == [], f"路由层同步联网（TQ 探测 3s 阻塞事件循环）: {calls}"


def test_unverified_name_is_not_guessed_and_not_market_answered(monkeypatch):
    """名字核不到 → 不猜是哪只，也不许把它绑到市场/扫描配方上**代答**。"""
    calls = _spy_network(monkeypatch)
    q = "三花智控这波怎么样"
    route = route_for_request(q, SCHEMAS, bound=None)
    assert route.reason == "stock_question", route.reason
    assert route.recipe is None or getattr(route.recipe, "scope", "symbol") == "symbol"
    # 也不许悄悄绑到"全市场候选"那类配方上
    assert not str(route.reason).startswith("recipe:entry_scanner")
    assert calls == [], "路由层不该为核名字发请求"


def test_name_question_without_resolution_keeps_symbol_scope(monkeypatch):
    """「像股票名但核不到」按单标的处置：交上游核对/追问，不许当市场问题答。"""
    calls = _spy_network(monkeypatch)
    assert has_symbol_scope("三花智控这波怎么样") is True
    # 有市场意图词时仍按市场口径（板块类问句不能被一个后缀词抓走）
    assert has_symbol_scope("三花智控所在的板块今天怎么样") is False
    assert calls == []


# ═══════════════════════════════════════════════════════ 管线：权限只认声明


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


def _scan_recipe():
    return get_recipe("entry_scanner")


def _scan_tools() -> list[str]:
    return list(_scan_recipe().tools)


GOOD = "=== 龙虎榜（净买入 1.2 亿）===\n600111 净买入 1.2 亿｜300750 净卖出 0.3 亿"
DENIED = "无权限（HTTP 403）：当前账号不具备该数据权限"


@pytest.mark.asyncio
async def test_denial_is_read_from_declared_first_line_only():
    ex = _Exec({t: DENIED for t in _scan_tools()})
    run = await run_market_pipeline(_scan_recipe(), ex)
    assert run is None, "全是拒权 → 不许产出结论（fail-closed，回落 tool loop）"


@pytest.mark.asyncio
async def test_permission_word_inside_body_is_not_a_denial():
    """审计反例：正文里的「未开通」是口径说明，不许把整条证据判成拒权。"""
    tools = _scan_tools()
    out = {t: DENIED for t in tools}
    out[tools[0]] = ("=== 两融口径 ===\n融资融券未开通的标的不计入统计；"
                     "未开通两融的营业部按 0 计。600111 净买入 1.2 亿")
    run = await run_market_pipeline(_scan_recipe(), _Exec(out))
    assert run is not None
    assert run.ok == 1 and tools[0] not in run.denied
    # 其余确实拒权
    assert set(run.denied) == set(tools[1:])


@pytest.mark.asyncio
async def test_denied_counts_are_stated_deterministically():
    tools = _scan_tools()
    out = {t: DENIED for t in tools}
    out[tools[0]] = GOOD
    run = await run_market_pipeline(_scan_recipe(), _Exec(out))
    assert run is not None and run.ok == 1
    assert len(run.denied) == len(tools) - 1
    assert "拒权" in run.missing_note()
    prefix = pipeline_market._missing_prefix(run)
    assert prefix.startswith("> 数据口径：") and "拒权" in prefix, "缺口必须由代码写进正文"


@pytest.mark.asyncio
async def test_handler_failure_first_line_is_not_evidence():
    """handlers 自己的失败开场（首行带冒号）也不算证据；正文里的"失败"二字不算。"""
    tools = _scan_tools()
    out = {t: "同花顺热榜查询失败: timeout" for t in tools}
    out[tools[0]] = GOOD
    run = await run_market_pipeline(_scan_recipe(), _Exec(out))
    assert run is not None and run.ok == 1
    assert set(run.unavailable) == set(tools[1:])

    # 正文（非首行/非声明）里有"失败"不影响判定
    body = "=== 两融口径 ===\n昨日两融折算失败案例 3 起；600111 净买入 1.2 亿"
    assert pipeline_market.tool_status(body) == "ok"


# ═══════════════════════════════════════════════════════ 管线：日期不许从正文猜


@pytest.mark.asyncio
async def test_prose_date_is_not_used_as_data_date():
    """审计反例：未来日期/页脚日期出现在正文里，不许当数据日。"""
    tools = _scan_tools()
    out = {t: GOOD for t in tools}
    out[tools[0]] = ("=== 公告 ===\n2027-01-01 限售解禁，2026-11-09 昨日收盘 12.30；"
                     "本页生成于 2026-11-10 15:05")
    run = await run_market_pipeline(_scan_recipe(), _Exec(out))
    assert run is not None and run.ok == len(tools)
    assert run.data_date is None, "正文里的日期不是数据日声明"
    assert run.data_date_status == "unverified"
    assert not run.stale, "不许用正文日期判过期"
    assert tools[0] in run.undated
    prefix = pipeline_market._missing_prefix(run)
    assert "数据日未声明" in prefix and "不得" in prefix


@pytest.mark.asyncio
async def test_declared_stale_day_is_not_today():
    """声明行说数据日是昨天 → 只能算非今日，不进今日证据。"""
    stale_day = (_dt_date.today() - timedelta(days=1)).isoformat()
    tools = _scan_tools()
    out = {t: f"⚠️ 快照已过期：数据日 {stale_day}（最近应有数据日 {_dt_date.today().isoformat()}）\n{GOOD}"
           for t in tools}
    run = await run_market_pipeline(_scan_recipe(), _Exec(out))
    assert run is None, "整轮只有非今日数据 → 不许当今日证据写结论"


@pytest.mark.asyncio
async def test_declared_today_is_verified_and_counted():
    today = _dt_date.today().isoformat()
    tools = _scan_tools()
    out = {t: f"数据日 {today}（当前系统日期 {today}；最近应有数据日 {today}，未过期）\n{GOOD}"
           for t in tools}
    run = await run_market_pipeline(_scan_recipe(), _Exec(out))
    assert run is not None and run.ok == len(tools)
    assert run.data_date == today and run.data_date_status == "verified"
    assert not run.stale and not run.undated


@pytest.mark.asyncio
async def test_mixed_stale_is_listed_but_not_today():
    today = _dt_date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    tools = _scan_tools()
    out = {t: f"⚠️ 快照已过期：数据日 {yesterday}（最近应有数据日 {today.isoformat()}）\n{GOOD}"
           for t in tools}
    out[tools[0]] = f"数据日 {today.isoformat()}（当前系统日期 {today.isoformat()}）\n{GOOD}"
    run = await run_market_pipeline(_scan_recipe(), _Exec(out))
    assert run is not None and run.ok == 1
    assert any(label == tools[1] and day == yesterday for label, day in run.stale)
    system, user = compose_market_prompt(_scan_recipe(), run, REPORTED)
    assert "非今日" in system + user


# ═══════════════════════════════════════════════════════ 管线：候选不许扫数字


def test_digits_in_prose_are_not_candidates():
    """审计反例：正文里的长数字/代码片段不许当候选名单。"""
    run = MarketRun(market="CN", ok=1,
                    outputs=[("get_hot_stocks", GOOD)])
    assert run.candidates == []
    assert run.candidates_status == "unknown", "文本证据没声明名单 → 未知，不是「没有」"
    assert run.no_candidates is False, "未知 ≠ 没有候选"
    note = run.missing_note()
    assert "未知" in note and "未取到候选" not in note


def test_candidates_only_from_structured_list():
    declared_empty = json.dumps({"available": True, "symbols": []}, ensure_ascii=False)
    run = MarketRun(market="CN", ok=1, outputs=[("get_market_scan_ranks", declared_empty)])
    assert run.candidates_status == "declared"
    assert run.no_candidates is True
    assert "无候选" in run.missing_note()

    declared = json.dumps({"available": True, "symbols": ["600111", "300750"]},
                          ensure_ascii=False)
    run2 = MarketRun(market="CN", ok=1, outputs=[("get_market_scan_ranks", declared)])
    assert run2.candidates == ["600111", "300750"]
    assert run2.no_candidates is False


def test_structured_payload_date_requires_verified_status():
    """payload 自带 data_date 不算（同 tools_scan_read 口径：无 verified 声明即未知）。"""
    today = _dt_date.today().isoformat()
    payload = json.dumps({"available": True, "data_date": today, "symbols": ["600111"]},
                         ensure_ascii=False)
    assert pipeline_market.declared_data_day(payload) == ("unknown", None)

    payload_ok = json.dumps({"available": True, "data_date": today,
                             "data_date_status": "verified"}, ensure_ascii=False)
    assert pipeline_market.declared_data_day(payload_ok) == ("today", today)


# ═══════════════════════════════════════════════════════ 出站证据


def test_evidence_payload_carries_unknown_flags():
    run = MarketRun(market="CN", ok=1, outputs=[("get_hot_stocks", GOOD)])
    run.denied = ["get_market_news"]
    run.stale = [("get_market_scan_ranks", "2026-11-09")]
    run.undated = ["get_hot_stocks"]
    d = json.loads(json.dumps(pipeline_market.evidence_payload(run), ensure_ascii=False))
    assert d["denied"] == ["get_market_news"]
    assert d["stale"][0][0] == "get_market_scan_ranks"
    assert d["data_date"] is None and d["data_date_status"] == "unverified"
    assert d["candidates_status"] == "unknown"
    assert d["undated"] == ["get_hot_stocks"]
