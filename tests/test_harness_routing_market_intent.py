"""无标的市场级问题不许被单标的配方抓走（2026-11-10）。

现场：用户问「今天尾盘有适合建仓的股票吗，可以有创业板」，
`尾盘` 命中 `intraday_analyst`（scope=symbol），管线 `symbols_of()` 解析不到标的就
`return None`，但 `_REQ_TOOLS` 已收窄成 4 个只吃 symbol 的工具 ——
模型只能回一句「我是分时解读员，看不了全市场」。

这里守三件事：
1. 无标的的市场级问句 → `entry_scanner`（market 口径），工具里必须真的带扫描工具；
2. 指定股 / 会话绑定 / 盘中 / 持仓 / 复盘 / 竞价 一条都不许被改道；
3. 没有同口径候选时保持原赢家（空头司令这类"无标的也要跑"的老口径不变）。
"""
from __future__ import annotations

import pytest

from src.harness import pipeline_market
from src.harness.recipes import get_recipe, match_recipe_for
from src.harness.routing import has_symbol_scope, route_for_request

SCHEMAS = [{"type": "function", "function": {"name": n}} for n in (
    "get_market_scan_ranks", "get_hot_stocks", "get_sentiment_cycle",
    "get_strategy_signals", "get_market_anomalies", "get_dark_fund_top",
    "get_market_ranking", "get_market_news",
    "get_intraday_profile", "get_cancel_order_intent", "get_main_intent",
    "get_fund_structure", "get_portfolio", "get_stock_quote",
)]

REPORTED = "今天尾盘有适合建仓的股票吗，可以有创业板"

CASES = [
    # ── 本次修复：无标的、要一批候选 ───────────────────────────────
    (REPORTED, None, "entry_scanner"),
    ("尾盘有适合建仓的股票吗", None, "entry_scanner"),
    ("今天尾盘买什么", None, "entry_scanner"),
    ("有什么票可以埋伏什么位置", None, "entry_scanner"),
    # ── 不许误伤：指定股照旧走单标的配方 ───────────────────────────
    ("002361 尾盘怎么样", None, "intraday_analyst"),
    ("002361 尾盘有适合建仓吗", None, "intraday_analyst"),
    ("002361 这半小时撤单有没有变化", None, "intraday_analyst"),
    ("300750 放量了吗", None, "intraday_analyst"),
    ("600111 深度诊断 龙虎榜", None, "deep_diagnosis"),
    ("分时怎么样", "002361", "intraday_analyst"),
    ("全景数智深度诊断002050.SZ", None, "deep_diagnosis"),
    ("002050 筹码怎么样", None, "chip_analyst"),
    # ── 不许误伤：持仓 / 盘中 / 复盘 / 竞价 ───────────────────────
    ("持仓要不要减，看看席位", None, "intraday_watcher"),
    ("我的持仓现在什么风险", None, "intraday_watcher"),
    ("盘中002361要不要减", None, "intraday_watcher"),
    # 风控裁判 pipeline=False、get_portfolio 不需要代码 —— 不许被「持仓」二字换走（例外 1）
    ("持仓体检", None, "risk_judge"),
    ("帮我复盘今天", None, "postmarket_reviewer"),
    ("复盘今天的龙虎榜", None, "lhb_reviewer"),
    ("涨停梯队怎么样", None, "postmarket_reviewer"),
    ("今天竞价谁爆量", None, "auction_hunter"),
    ("题材主线在哪", None, "premarket_strategist"),
    # ── 老口径不变：没有同口径候选就保持原赢家 ───────────────────
    ("帮我做红蓝对抗压力测试", None, "devil_advocate"),
    ("用一句话说明你能做什么", None, None),
]


@pytest.mark.parametrize("q,bound,expected", CASES)
def test_route_scope_split(q, bound, expected):
    r = route_for_request(q, SCHEMAS, bound=bound).recipe
    assert (r.id if r else None) == expected, f"{q} → {r.id if r else None}"


def test_has_symbol_scope_is_pure_regex(monkeypatch):
    """口径判定全是本地正则：不发网络请求（2026-11-10 复审阻断）。"""
    from src.core import symbol_guard

    calls: list[str] = []
    monkeypatch.setattr(symbol_guard, "_tq_match",
                        lambda w, *a, **k: (calls.append(w), [])[1])
    monkeypatch.setattr(symbol_guard, "_match_cached",
                        lambda w, *a, **k: (calls.append(w), [])[1])
    assert has_symbol_scope("002361 怎么样") is True
    # 正文只写名字 = 像在问某只票，但**不在这里核**（上游 bound 才是核对结果）
    assert has_symbol_scope("三花智控这波怎么样") is True
    assert has_symbol_scope("分时怎么样", "002361") is True
    assert has_symbol_scope(REPORTED) is False
    assert has_symbol_scope("创业板今天怎么样") is False, "板块词不是标的"
    assert has_symbol_scope("创业板今天怎么样", "002361") is False, "绑定不许劫持板块问句"
    assert calls == [], f"路由层不该联网: {calls}"


def test_reported_question_gets_scan_tools():
    route = route_for_request(REPORTED, SCHEMAS, bound=None)
    names = [s["function"]["name"] for s in route.tools]
    assert "get_market_scan_ranks" in names, "市场扫描工具必须在白名单里"
    assert "get_hot_stocks" in names
    assert "get_intraday_profile" not in names, "单标的工具不该再被下发"
    assert route.temperature == 0.2
    assert route.reason == "recipe:entry_scanner"


def test_reported_question_pipeline_runs_without_symbol():
    """market 口径的管线不需要标的：8 个工具全在 market lane，一个都不许被 skip。"""
    steps = pipeline_market.build_steps(get_recipe("entry_scanner"), watchlist=[])
    assert [s.tool for s in steps] == list(get_recipe("entry_scanner").tools)
    assert all(not s.skip for s in steps), [ (s.tool, s.skip) for s in steps if s.skip ]
    assert all(s.args == {} for s in steps), "market lane 必须传 {}"


def test_bound_stock_does_not_block_market_intent():
    """用户绑着票也要能问"今天有什么能建仓的"：没有同口径候选时不强抢。"""
    r = match_recipe_for("今天有什么能建仓的", has_symbol=True)
    assert r is not None and r.id == "entry_scanner"


def test_entry_scanner_has_no_recommendation_tool():
    """合规：荐股口径的 get_opportunities 是 handler-only（schema=None），不许被配方复活。"""
    assert "get_opportunities" not in get_recipe("entry_scanner").tools
