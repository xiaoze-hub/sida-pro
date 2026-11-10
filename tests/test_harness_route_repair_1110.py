"""路由复审 P0（2026-11-10）：bound 不许劫持市场选股 / 单标的诊断 / 持仓问题。

现场原句（用户截图）：
    「今天尾盘有适合建仓的股票吗，可以有创业板」
用户会话里绑着 002361，于是 `has_symbol_scope` 见到 bound 就判「有标的」，
`尾盘` 抓到的 `intraday_analyst`（scope=symbol）赢了 `建仓` 的 `entry_scanner`。
管线 `symbols_of()` 拿到的是「今天有什么能建仓的」→ 解析不到 → 回落，用户看到
「我是分时解读员，只能看个股分时」。

四条守则：
1. 会话绑定不是问句口径 —— 绑定只在一个「没有任何市场/持仓意图」的问句里才算标的；
2. 正文写了代码（002361 适合建仓吗）→ 单标的诊断，不许跑全市场扫描；
3. 「我的持仓…尾盘/加仓」必须落到带 `get_portfolio` 的配方，bound 不许换走；
4. 简称必须核对过才算标的，核对不上就不猜（`创业板`/`科技股` 不是标的）。
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from src.core import symbol_guard
from src.harness import pipeline_market
from src.harness.pipeline_market import MarketRun, compose_market_prompt, run_market_pipeline
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

REPORTED = "今天尾盘有适合建仓的股票吗，可以有创业板"
HOLDINGS = "我的持仓尾盘要不要加仓"


# ---------------------------------------------------------------- 路线：P0-1 / P0-2


@pytest.mark.parametrize("q,bound,expected", [
    # 截图原句 + 绑定 002361 仍必须市场选股
    (REPORTED, "002361", "entry_scanner"),
    ("尾盘有适合建仓的股票吗", "002361", "entry_scanner"),
    ("今天有什么票可以埋伏", "002361", "entry_scanner"),
])
def test_bound_must_not_hijack_market_scan(q, bound, expected):
    r = route_for_request(q, SCHEMAS, bound=bound).recipe
    assert (r.id if r else None) == expected
    names = [s["function"]["name"] for s in route_for_request(q, SCHEMAS, bound=bound).tools]
    assert "get_market_scan_ranks" in names


@pytest.mark.parametrize("q,expected", [
    ("002361 适合建仓吗", "deep_diagnosis"),
    ("002361 这个位置能建仓吗", "deep_diagnosis"),
])
def test_explicit_code_gets_single_symbol_diagnosis(q, expected):
    r = route_for_request(q, SCHEMAS).recipe
    assert (r.id if r else None) == expected
    assert r.scope == "symbol"
    assert "get_market_scan_ranks" not in [s["function"]["name"] for s in
                                           route_for_request(q, SCHEMAS).tools]


def test_explicit_code_wins_over_bound_and_scan_words():
    """"002361 尾盘有适合建仓吗" 有代码 → 单标的，bound 与市场词都不许改道。"""
    r = route_for_request("002361 尾盘有适合建仓吗", SCHEMAS, bound="600111").recipe
    assert r is not None and r.scope == "symbol"


# ---------------------------------------------------------------- 路线：P0-3 持仓


@pytest.mark.parametrize("bound", ["002361", None])
@pytest.mark.parametrize("q", [
    HOLDINGS,
    "我的持仓尾盘要不要减",
    "我的持仓现在什么风险",
])
def test_portfolio_question_keeps_get_portfolio(q, bound):
    r = route_for_request(q, SCHEMAS, bound=bound).recipe
    assert r is not None, q
    assert "get_portfolio" in r.tools, f"{q}(bound={bound}) → {r.id} 丢了 get_portfolio"


def test_portfolio_guard_via_match_recipe_for():
    r = match_recipe_for(HOLDINGS, has_symbol=False)
    assert r is not None and "get_portfolio" in r.tools


# ---------------------------------------------------------------- 路线：P0-4 简称


def test_bound_is_not_symbol_scope_for_market_question():
    assert has_symbol_scope(REPORTED, "002361") is False
    assert has_symbol_scope(HOLDINGS, "002361") is False


def test_sector_and_common_nouns_are_not_symbols():
    assert has_symbol_scope("创业板今天怎么样") is False
    assert has_symbol_scope("半导体板块今天怎么样") is False
    assert has_symbol_scope(REPORTED) is False


def test_name_must_be_verified_not_guessed(monkeypatch):
    """「三花智控」核上有这只票才算标的；查不到（源不可用/无此票）一律不猜。"""
    from src.harness import routing as routing_mod

    monkeypatch.setattr(symbol_guard, "_tq_match", lambda w: [])
    symbol_guard._TQ_MATCH_CACHE.clear()
    routing_mod._NAME_MISS.clear()
    assert has_symbol_scope("三花智控这波怎么样") is False

    monkeypatch.setattr(symbol_guard, "_tq_match", lambda w: [("002050", "三花智控")])
    symbol_guard._TQ_MATCH_CACHE.clear()
    routing_mod._NAME_MISS.clear()
    assert has_symbol_scope("三花智控这波怎么样") is True
    symbol_guard._TQ_MATCH_CACHE.clear()
    routing_mod._NAME_MISS.clear()


# ---------------------------------------------------------------- 市场管线：证据


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


@pytest.mark.asyncio
async def test_denied_and_empty_are_not_usable_evidence():
    """拒权/空数据 = 没取到，不许算成「已获可用证据」。"""
    ex = _Exec({
        "get_market_scan_ranks": "无权限访问该接口，请先开通数据服务",
        "get_hot_stocks": "",
        "get_sentiment_cycle": "暂无数据",
    })
    run = await run_market_pipeline(_scan_recipe(), ex)
    assert run is None, "只剩拒权/空数据的管线必须回落，不能拿它写结论"


@pytest.mark.asyncio
async def test_denied_is_traced_and_counted_not_ok():
    ex = _Exec({"get_market_ranks": "无权限访问该接口"})
    run = await pipeline_market.run_market_pipeline(_scan_recipe(), ex)
    # entry_scanner 的工具名以真实配方为准：全部拒权 → 不产出 run
    assert run is None


@pytest.mark.asyncio
async def test_yesterday_board_is_not_today(monkeypatch):
    """昨日榜单不许当今日证据：返回 2026-11-09 的榜在 11-10 只算「昨日」。"""
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    ex = _Exec({t: f"{yesterday} 龙虎榜数据：净买入 1.2 亿" for t in _scan_recipe().tools})
    run = await run_market_pipeline(_scan_recipe(), ex)
    assert run is None, "整轮只有昨日数据 → 不算今天可用证据"


@pytest.mark.asyncio
async def test_stale_rows_are_listed_when_mixed():
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    today = _dt.date.today().isoformat()
    tools = list(_scan_recipe().tools)
    out = {t: f"{yesterday} 净买入 1.2 亿" for t in tools}
    out[tools[0]] = f"{today} 主力净流入 3.4 亿"
    run = await run_market_pipeline(_scan_recipe(), _Exec(out))
    assert run is not None and run.ok == 1
    assert any(label == tools[1] and day == yesterday for label, day in run.stale)
    assert run.data_date == today
    system, user = compose_market_prompt(_scan_recipe(), run, "今天尾盘有适合建仓的股票吗")
    assert yesterday in system + user
    assert "昨日" in system + user or "不是今天" in system + user


@pytest.mark.asyncio
async def test_no_candidates_is_stated_not_left_to_prompt():
    """取到了数据但没有任何候选代码 → 结构上标「缺候选」，不是只改提示词。"""
    run = MarketRun(market="CN", outputs=[("get_hot_stocks", "情绪回暖，赚钱效应一般")], ok=1)
    assert run.no_candidates is True
    assert "候选" in run.missing_note()
    system, _user = compose_market_prompt(_scan_recipe(), run, "今天有什么能建仓的")
    assert "未取到候选" in system or "没有候选" in system


@pytest.mark.asyncio
async def test_denied_counts_reported_in_run():
    tools = list(_scan_recipe().tools)
    out = {t: "无权限访问该接口" for t in tools}
    out[tools[0]] = "2026-11-10 主力净流入 3.4 亿，600111 领涨"
    run = await run_market_pipeline(_scan_recipe(), _Exec(out))
    assert run is not None and run.ok == 1
    assert set(run.denied) and all(t in tools for t in run.denied)
    system, user = compose_market_prompt(_scan_recipe(), run, "今天有什么能建仓的")
    assert "拒权" in system + user


def test_evidence_payload_marks_stale_and_missing():
    """证据出站也必须带「哪些没取到」，不许只剩成功条数。"""
    run = MarketRun(market="CN", outputs=[("get_hot_stocks", "600111 涨停")], ok=1)
    run.denied = ["get_market_news"]
    run.stale = [("get_market_scan_ranks", "2026-11-09")]
    d = json.loads(json.dumps(pipeline_market.evidence_payload(run)))
    assert d["denied"] == ["get_market_news"]
    assert d["stale"][0][0] == "get_market_scan_ranks"
