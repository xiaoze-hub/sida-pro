"""盘前结构化证据层 (`src/core/premarket_evidence.py`) 与报告生成器接线单测。

覆盖：
1. 涨停家数超全市场股票上限（如 5580 家）→ `ladder.quality == "suspect"` + reason
2. 连板高度与梯队字典最高板矛盾 → `ladder.quality == "suspect"` + reason
3. 连板梯队各档家数之和与总涨停家数矛盾 → `ladder.quality == "suspect"` + reason
4. 涨停家数为 0 但最高连板 > 0 → `ladder.quality == "suspect"` + reason
5. 快讯全部同一时间戳 → `news.quality == "suspect"` + reason
6. 快讯正常时主题聚类(`theme`)与热度(`heat`)计算且保留原字段
7. 全源抛异常时 `collect()` 绝不向外抛异常，各块 `quality == "missing"` 且带 `reason`
8. 局部源失败互不拖累，成功块 `quality == "ok"`、失败块 `quality == "missing"`
9. 持仓行情全部失败时 `portfolio.quality == "suspect"` 且带原因
10. `report_generator._build_template_report("premarket", ...)` 在 `quality == "ok"` 时与旧版逐字节一致（固定 fixture）
11. `report_generator._build_template_report("premarket", ...)` 在 `quality != "ok"` 时输出 `⚠️ 数据待核` 角标
12. `report_generator._collect_data("premarket", db)` 委托调用 `premarket_evidence.collect`
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from src.core import premarket_evidence as pe
from src.core import report_generator as rg


# ──────────────────────────── 1-4: 涨停梯队失真判定 ────────────────────────────


def test_ladder_limit_up_count_exceeds_market_cap_is_suspect(monkeypatch):
    """涨停 5580 家（超过全市场股票数上限）必须被取数层标为 suspect。"""
    dirty_summary = {
        "limit_up_count": 5580,
        "max_streak": 4,
        "ladder": {4: 2, 3: 5, 2: 13, 1: 5560},
        "top_stocks": ["测试股(000001)4板"],
        "top_sectors": [{"name": "半导体", "count": 120}],
        "limit_up_source": "eastmoney",
    }
    monkeypatch.setattr(
        "src.collectors.market_sentiment_collector.MarketSentimentCollector.get_sentiment_summary",
        lambda self: dirty_summary,
    )
    blk = pe._collect_ladder_block("2026-10-23T08:30:00+08:00")
    assert blk["quality"] == "suspect"
    assert "5580" in blk["reason"]
    assert "超过全市场股票数上限" in blk["reason"]
    assert blk["items"][0]["limit_up_count"] == 5580


def test_ladder_streak_contradicts_ladder_dict_is_suspect():
    """最高连板(6板)与梯队字典最大键(3板)不一致 → suspect。"""
    summary = {
        "limit_up_count": 20,
        "max_streak": 6,
        "ladder": {3: 2, 2: 5, 1: 13},
    }
    q, reason = pe.audit_ladder_summary(summary)
    assert q == "suspect"
    assert "不一致" in reason


def test_ladder_sum_contradicts_limit_up_count_is_suspect():
    """梯队各档家数合计(15) != 涨停总家数(40) → suspect。"""
    summary = {
        "limit_up_count": 40,
        "max_streak": 3,
        "ladder": {3: 1, 2: 4, 1: 10},
    }
    q, reason = pe.audit_ladder_summary(summary)
    assert q == "suspect"
    assert "矛盾" in reason


def test_ladder_zero_count_positive_streak_is_suspect():
    """涨停家数为 0 但最高连板 > 0 → suspect。"""
    q, reason = pe.audit_ladder_summary({"limit_up_count": 0, "max_streak": 2, "ladder": {}})
    assert q == "suspect"
    assert "矛盾" in reason


# ──────────────────────────── 5-6: 快讯失真判定与主题聚类 ────────────────────────────


def test_news_all_identical_timestamps_is_suspect(monkeypatch):
    """多条快讯全部为同一时间戳 → suspect + reason。"""

    class FakeArt:
        def __init__(self, title: str):
            self.publish_time = datetime(2026, 10, 23, 8, 0)
            self.title = title
            self.source = "cls"
            self.importance = 1
            self.url = "https://example.com"
            self.symbols = ["000001"]

    class FakeMD:
        def flash_news(self, market="CN", limit=15):
            return [FakeArt("快讯A：算力芯片突破"), FakeArt("快讯B：大模型发布"), FakeArt("快讯C：特高压建设")]

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: FakeMD())
    blk = pe._collect_news_block("2026-10-23T08:30:00+08:00")
    assert blk["quality"] == "suspect"
    assert "同一时间戳" in blk["reason"]
    assert "10-23 08:00" in blk["reason"]


def test_news_clustering_and_heat_enrichment(monkeypatch):
    """快讯时间戳不同时为 ok，且自动聚类主题并计算热度。"""

    class FakeArt:
        def __init__(self, dt: datetime, title: str, imp: int = 1):
            self.publish_time = dt
            self.title = title
            self.source = "cls"
            self.importance = imp
            self.url = ""
            self.symbols = []

    class FakeMD:
        def flash_news(self, market="CN", limit=15):
            return [
                FakeArt(datetime(2026, 10, 23, 7, 10), "国产算力芯片订单大增", imp=2),
                FakeArt(datetime(2026, 10, 23, 7, 25), "智算中心大模型招标提速", imp=1),
                FakeArt(datetime(2026, 10, 23, 7, 40), "央行开展逆回购操作", imp=1),
            ]

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: FakeMD())
    blk = pe._collect_news_block("2026-10-23T08:30:00+08:00")
    assert blk["quality"] == "ok"
    assert blk["reason"] == ""
    items = blk["items"]
    assert len(items) == 3
    assert items[0]["theme"] == "算力/AI"
    assert items[1]["theme"] == "算力/AI"
    # 算力/AI 出现 2 次，第一条 importance=2 → heat = 2 + 2 = 4
    assert items[0]["heat"] == 4
    assert items[2]["theme"] == "宏观/政策"


# ──────────────────────────── 7-9: 异常隔离与 missing/suspect 标注 ────────────────────────────


def test_collect_never_raises_when_all_sources_fail(monkeypatch):
    """所有外部数据源与数据库查询均抛异常时，collect() 绝不抛异常，全部标 missing 且带原因。"""

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", boom)
    monkeypatch.setattr("src.core.global_indices.fetch_global_indices", boom)
    monkeypatch.setattr("src.core.commodity_quotes.fetch_snapshot", boom)
    monkeypatch.setattr("src.collectors.market_http.market_get", boom)
    monkeypatch.setattr(
        "src.collectors.market_sentiment_collector.MarketSentimentCollector.get_sentiment_summary",
        boom,
    )
    monkeypatch.setattr("src.core.mood_cycle.current_mood", boom)

    class BrokenDB:
        def query(self, *args, **kwargs):
            raise RuntimeError("db disconnected")

    ev = pe.collect(date="2026-10-23", db=BrokenDB(), user_id="u-123")
    assert ev["date"] == "2026-10-23"
    assert ev["user_id"] == "u-123"
    for key in ("overnight", "ladder", "news", "sentiment", "watchlist", "portfolio"):
        blk = ev[key]
        assert blk["quality"] == "missing", f"{key} should be missing"
        assert blk["items"] == []
        assert blk["reason"], f"{key} must carry non-empty reason"
        assert blk["as_of"]


def test_collect_partial_failure_isolates_missing_blocks(monkeypatch):
    """单块失败不影响其余正常块。"""
    monkeypatch.setattr(
        pe,
        "_collect_overnight_block",
        lambda as_of: pe._make_block([], "missing", reason="外盘超时", source="none", as_of=as_of),
    )
    monkeypatch.setattr(
        pe,
        "_collect_ladder_block",
        lambda as_of: pe._make_block(
            [{"limit_up_count": 45, "max_streak": 3, "ladder": {3: 2, 2: 8, 1: 35}}],
            "ok",
            source="tq",
            as_of=as_of,
        ),
    )
    monkeypatch.setattr(
        pe,
        "_collect_news_block",
        lambda as_of: pe._make_block(
            [{"time": "08:00", "title": "快讯1", "source": "cls", "importance": 1}],
            "ok",
            source="cls/ths",
            as_of=as_of,
        ),
    )
    monkeypatch.setattr(
        pe,
        "_collect_sentiment_block",
        lambda as_of: pe._make_block(
            [{"available": True, "phase": "ignite", "label": "发酵期", "allowance": 10}],
            "ok",
            source="market_phase",
            as_of=as_of,
        ),
    )
    monkeypatch.setattr(
        pe,
        "_collect_watchlist_block",
        lambda db, uid, as_of: pe._make_block([], "missing", reason="空表", source="entry_candidates", as_of=as_of),
    )
    monkeypatch.setattr(
        pe,
        "_collect_portfolio_block",
        lambda db, uid, as_of: pe._make_block([], "missing", reason="无持仓", source="positions", as_of=as_of),
    )

    ev = pe.collect(date="2026-10-23", db=object())
    assert ev["overnight"]["quality"] == "missing"
    assert ev["overnight"]["reason"] == "外盘超时"
    assert ev["ladder"]["quality"] == "ok"
    assert ev["news"]["quality"] == "ok"
    assert ev["sentiment"]["quality"] == "ok"


def test_portfolio_quote_failure_marks_suspect(monkeypatch):
    """持仓记录存在但行情接口失败时，portfolio 块标记为 suspect。"""

    class FakePos:
        cost_price = 10.0
        quantity = 100
        trading_style = "swing"
        user_id = "u1"

    class FakeStock:
        symbol = "000001"
        name = "平安银行"
        market = "CN"

    class FakeAccount:
        id = 1
        name = "测试账户"

    class FakeQuery:
        def join(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def all(self):
            return [(FakePos(), FakeStock(), FakeAccount())]

    class FakeDB:
        def query(self, *a, **k):
            return FakeQuery()

    def broken_quotes(*a, **k):
        raise RuntimeError("quote vendor timeout")

    monkeypatch.setattr("src.core.marketdata_client.md_quote_rows", broken_quotes)
    blk = pe._collect_portfolio_block(FakeDB(), "u1", "2026-10-23T08:30:00+08:00")
    assert blk["quality"] == "suspect"
    assert "持仓行情获取失败" in blk["reason"]
    assert blk["items"][0]["quote_error"] == "数据获取失败"


# ──────────────────────────── 10-12: report_generator 逐字节兼容与角标 ────────────────────────────


FIXED_PREMARKET_FIXTURE = {
    "market_date": "2026-10-23",
    "generated_at": "2026-10-23 08:30:00",
    "yesterday_limit_up": {
        "limit_up_count": 52,
        "max_streak": 4,
        "ladder": {4: 1, 3: 3, 2: 8, 1: 40},
        "top_stocks": ["华立股份(600605)4板", "川大智胜(002253)3板"],
        "top_sectors": [{"name": "算力", "count": 12}, {"name": "低空经济", "count": 8}],
    },
    "news": [
        {
            "time": "10-23 07:45",
            "title": "工信部推进算力基础设施高质量发展",
            "source": "cls",
            "importance": 2,
        },
        {
            "time": "10-23 08:10",
            "title": "多地发布低空经济产业支持政策",
            "source": "ths",
            "importance": 1,
        },
    ],
    "candidates": [
        {
            "symbol": "300308",
            "name": "中际旭创",
            "action": "准备建仓",
            "score": 85.5,
            "reason": "光模块龙头景气延续",
            "entry_low": 120.0,
            "entry_high": 123.5,
        }
    ],
    "positions": [
        {
            "symbol": "000001",
            "name": "平安银行",
            "quantity": 1000,
            "cost_price": 11.2,
            "current_price": 11.5,
            "change_pct": 1.32,
            "prev_close": 11.35,
        }
    ],
}

EXPECTED_LEGACY_TEMPLATE_OUTPUT = (
    "# A股盘前报告 2026-10-23\n"
    "\n"
    "**生成时间:** 2026-10-23 08:30:00\n"
    "**生成方式:** 数据模板(LLM 不可用, 纯数据不解读)\n"
    "\n"
    "## 一、昨日市场回顾(涨停复盘)\n"
    "- 涨停家数: 52 家, 最高连板: 4 板\n"
    "- 连板梯队: 4板×1, 3板×3, 2板×8, 1板×40\n"
    "- 高位股: 华立股份(600605)4板, 川大智胜(002253)3板\n"
    "- 涨停集中板块: 算力×12, 低空经济×8\n"
    "\n"
    "## 二、隔夜重要消息\n"
    "- [10-23 07:45] ⭐⭐工信部推进算力基础设施高质量发展(cls)\n"
    "- [10-23 08:10] ⭐多地发布低空经济产业支持政策(ths)\n"
    "\n"
    "## 三、今日关注候选\n"
    "- 中际旭创(300308) 动作=准备建仓 分=85.5 区间[120.00,123.50] 理由: 光模块龙头景气延续\n"
    "\n"
    "## 四、当前持仓(参考前收盘)\n"
    "- 平安银行(000001) 1000股 成本 11.2 现价 11.5 (+1.32%) 前收 11.35\n"
    "\n"
    "## 五、风险提示\n"
    "- 本报告由 SIDA 自动生成, 数据源可能存在延迟或缺失, 仅供参考。\n"
    "- 涨停/情绪数据为最近交易日口径, 竞价情况以开盘后实际为准。\n"
    "- 不构成投资建议, 入市需谨慎。"
)


def test_report_generator_byte_for_byte_identical_when_quality_ok():
    """当各块 quality == 'ok' 时，模板输出与旧版逐字节完全一致。"""
    data = dict(FIXED_PREMARKET_FIXTURE)
    out = rg._build_template_report("premarket", data)
    assert out.encode("utf-8") == EXPECTED_LEGACY_TEMPLATE_OUTPUT.encode("utf-8")


def test_report_generator_adds_warning_badge_when_quality_not_ok():
    """当涨停数据失真(如 5580 家)或快讯时间戳重复时，模板对应块必须打 `⚠️ 数据待核` 角标。"""
    import copy

    data = copy.deepcopy(FIXED_PREMARKET_FIXTURE)
    data["yesterday_limit_up"]["limit_up_count"] = 5580
    data["news"][1]["time"] = data["news"][0]["time"]  # 制造时间戳完全重复
    out = rg._build_template_report("premarket", data)
    assert "- 涨停家数: 5580 家, 最高连板: 4 板 ⚠️ 数据待核" in out
    assert "## 二、隔夜重要消息 ⚠️ 数据待核" in out


def test_report_generator_collect_data_delegates_to_premarket_evidence(monkeypatch):
    """`report_generator._collect_data('premarket', db)` 必须调用 `premarket_evidence.collect`。"""
    called = {}

    def fake_collect(date=None, db=None, user_id=None):
        called["date"] = date
        called["db"] = db
        as_of = "2026-10-23T08:30:00+08:00"
        return {
            "date": date or "2026-10-23",
            "as_of": as_of,
            "user_id": user_id,
            "overnight": pe._make_block([], "ok", source="tencent", as_of=as_of),
            "ladder": pe._make_block(
                [FIXED_PREMARKET_FIXTURE["yesterday_limit_up"]],
                "ok",
                source="tq",
                as_of=as_of,
            ),
            "news": pe._make_block(
                FIXED_PREMARKET_FIXTURE["news"],
                "ok",
                source="cls/ths",
                as_of=as_of,
            ),
            "sentiment": pe._make_block([], "ok", source="market_phase", as_of=as_of),
            "watchlist": pe._make_block(
                FIXED_PREMARKET_FIXTURE["candidates"],
                "ok",
                source="entry_candidates",
                as_of=as_of,
            ),
            "portfolio": pe._make_block(
                FIXED_PREMARKET_FIXTURE["positions"],
                "ok",
                source="positions",
                as_of=as_of,
            ),
        }

    monkeypatch.setattr(pe, "collect", fake_collect)
    sentinel_db = object()
    data = asyncio.run(rg._collect_data("premarket", sentinel_db))
    assert called["db"] is sentinel_db
    assert data["yesterday_limit_up"]["limit_up_count"] == 52
    assert len(data["news"]) == 2
    assert len(data["candidates"]) == 1
    assert len(data["positions"]) == 1
    assert data["qualities"]["ladder"] == "ok"
