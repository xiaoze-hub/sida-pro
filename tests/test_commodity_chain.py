"""商品品种 → A 股行业通道 (`src/core/commodity_chain.py`) 与盘前快讯商品标注的验收单测。

覆盖两个环节：
A. `premarket_evidence._NEWS_THEME_KEYWORDS` 的「大宗/涨价」扩表：纸浆/木浆/浆价/原纸/瓦楞纸/
   箱板纸/白卡纸/双胶纸 + 纯碱/尿素/磷肥/钾肥/甲醇/PTA/涤纶/乙二醇/螺纹钢/铁矿石/焦煤/动力煤/
   锂/钴/镍/稀土/生猪/白糖/棉花/豆粕/棕榈油/天然橡胶 —— 语义仍是「大宗/涨价」，且不劫持原有主题。
B. 新增 `commodity_chain`：COMMODITY_CHAINS / match_commodity_news / industry_members / beneficiaries，
   以及 evidence.news 块的 `commodities` / `industries` / `commodity_chain` 接线和 premarket_outlook
   新闻行「（商品：纸浆 → 造纸/包装印刷）」注释。

口径约定（不是变通）：
- TQ 不可用 / 板块名不在真实目录 / 成分返回空 → `industry_members` 返回 `None`，绝不返回 `[]`
  冒充「板块无成分」；
- 本文件所有 TQ 调用走 `src.core.tdx_boards` 的全局函数替身（`sector_items` / `constituents`），
  不连真网关、不发网络。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.core.premarket_evidence as pe
import src.core.tdx_boards as tdx
from src.core import commodity_chain as cc
from src.agents.premarket_outlook import PremarketOutlookAgent

# 已核实的真实 TQ 二级行业名（128 个中的子集，勿自编）
VERIFIED_INDUSTRIES = {
    "造纸", "包装印刷", "化纤", "化学原料", "化学制品", "塑料", "橡胶", "农用化工",
    "普钢", "特钢", "冶钢原料", "工业金属", "贵金属", "能源金属", "稀有金属",
    "煤炭开采", "焦炭加工", "石油化工", "水泥", "玻璃玻纤", "养殖业", "饲料",
    "农产品加工", "食品加工", "纺织制造", "电池",
}

# 需求列出的商品品种关键词（原文字面口径）
REQUIRED_VARIETIES = (
    "纸浆", "木浆", "浆价", "原纸", "瓦楞纸", "箱板纸", "白卡纸", "双胶纸",
    "纯碱", "尿素", "磷肥", "钾肥", "甲醇", "PTA", "涤纶", "乙二醇",
    "螺纹钢", "铁矿石", "焦煤", "动力煤", "锂", "钴", "镍", "稀土",
    "生猪", "白糖", "棉花", "豆粕", "棕榈油", "天然橡胶",
)


# ──────────────────────────── TQ 替身 ────────────────────────────

# 造纸(881167.SH) 30 只、包装印刷(881171.SH) 49 只 为实测；其余代码仅作替身，不代表真实代码
_FAKE_SECTORS = [
    {"code": "881167.SH", "name": "造纸", "board_type": "industry"},
    {"code": "881171.SH", "name": "包装印刷", "board_type": "industry"},
    {"code": "881101.SH", "name": "化学原料", "board_type": "industry"},
    {"code": "881102.SH", "name": "养殖业", "board_type": "industry"},
    {"code": "881103.SH", "name": "煤炭开采", "board_type": "industry"},
]

_FAKE_MEMBERS = {
    "881167.SH": ["600103.SH", "600966.SH", "002078.SZ"],
    "881171.SH": ["002191.SZ", "600210.SH"],
    "881101.SH": ["600409.SH"],
    "881102.SH": ["002714.SZ"],
    "881103.SH": ["601088.SH"],
}


@pytest.fixture(autouse=True)
def _clear_chain_cache():
    cc._clear_cache()
    yield
    cc._clear_cache()


def _stub_tq(monkeypatch, *, members=None, sectors=None):
    """把 TQ 两个取数缝换成替身，返回调用记录。"""
    calls: list[tuple] = []
    sectors = _FAKE_SECTORS if sectors is None else sectors
    members = _FAKE_MEMBERS if members is None else members

    def fake_sector_items():
        calls.append(("sector_items",))
        return [dict(s) for s in sectors]

    def fake_constituents(block_code):
        calls.append(("constituents", block_code))
        return list(members.get(block_code, []))

    monkeypatch.setattr(tdx, "sector_items", fake_sector_items)
    monkeypatch.setattr(tdx, "constituents", fake_constituents)
    return calls


class _FakeArt:
    def __init__(self, dt, title, imp=1, symbols=None):
        self.publish_time = dt
        self.title = title
        self.source = "cls"
        self.importance = imp
        self.url = ""
        self.symbols = list(symbols or [])


class _FakeMD:
    def __init__(self, arts):
        self._arts = arts

    def flash_news(self, market="CN", limit=15):
        return list(self._arts)


# ──────────────────────────── A: 关键词扩表 ────────────────────────────


@pytest.mark.parametrize("variety", REQUIRED_VARIETIES)
def test_variety_keywords_route_to_commodity_theme(variety):
    """需求列出的 30 个品种词都归入「大宗/涨价」（语义仍是大宗/涨价）。"""
    assert pe._classify_news_theme(f"{variety}价格走强") == "大宗/涨价"


def test_real_production_pulp_headline_is_classified():
    """生产库真实快讯形态（期货综述里的纸浆）必须进「大宗/涨价」。"""
    title = "商品期货收盘涨跌不一 钯涨超4% 沪银涨超3% 纸浆涨超1%"
    assert pe._classify_news_theme(title) == "大宗/涨价"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("国产算力芯片订单大增", "算力/AI"),
        ("锂电池装机量同比增长", "新能源/电力"),
        ("央行开展逆回购操作", "宏观/政策"),
        ("白酒消费复苏超预期", "医药/消费"),
        ("低空经济产业支持政策发布", "低空/商业航天"),
    ],
)
def test_existing_themes_not_hijacked_by_commodity_keywords(title, expected):
    """扩表不得劫持原有主题路由（锂电仍是新能源、算力仍是算力）。"""
    assert pe._classify_news_theme(title) == expected


# ──────────────────────────── B1: COMMODITY_CHAINS 口径 ────────────────────────────


def test_chains_use_verified_tq_industry_names_only():
    """品种映射必须是已核实的真实 TQ 行业名，且方向字段合法。"""
    assert cc.COMMODITY_CHAINS, "COMMODITY_CHAINS 不能为空"
    for commodity, chain in cc.COMMODITY_CHAINS.items():
        assert chain["industries"], f"{commodity} 缺行业映射"
        unknown = set(chain["industries"]) - VERIFIED_INDUSTRIES
        assert not unknown, f"{commodity} 用了未核实的行业名: {unknown}"
        assert chain["direction"] in cc.DIRECTIONS, f"{commodity} 方向非法: {chain['direction']}"
        assert chain["keywords"], f"{commodity} 缺关键词"


def test_pulp_chain_maps_to_paper_and_packaging():
    chain = cc.COMMODITY_CHAINS["纸浆"]
    assert chain["industries"] == ("造纸", "包装印刷")


# ──────────────────────────── B2: match_commodity_news ────────────────────────────


def test_match_commodity_news_pulp_futures_review():
    items = [
        {"title": "商品期货收盘涨跌不一 钯涨超4% 纸浆涨超1%"},
        {"title": "工信部推进算力基础设施高质量发展"},
    ]
    hits = cc.match_commodity_news(items)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["index"] == 0
    assert hit["commodity"] == "纸浆"
    assert list(hit["industries"]) == ["造纸", "包装印刷"]
    assert hit["direction"] == "成本承压"


def test_match_commodity_news_multi_variety_in_one_item():
    items = [{"title": "碳酸锂与纯碱价格齐涨，下游成本承压"}]
    hits = cc.match_commodity_news(items)
    assert len(hits) == 1
    assert set(hits[0]["commodities"]) == {"锂", "纯碱"}


def test_match_commodity_news_ignores_lithium_battery_news():
    """「锂电池」是新能源题材，不是碳酸锂商品行情，不得误标商品。"""
    assert cc.match_commodity_news([{"title": "锂电池装机量同比增30%"}]) == []


def test_match_commodity_news_matches_content_too():
    items = [{"title": "早间要闻", "content": "木浆外盘报价连续两周上调"}]
    hits = cc.match_commodity_news(items)
    assert hits and hits[0]["commodity"] == "纸浆"


# ──────────────────────────── B3: industry_members ────────────────────────────


def test_industry_members_finds_600103_in_paper(monkeypatch):
    _stub_tq(monkeypatch)
    members = cc.industry_members("造纸")
    assert members is not None
    assert "600103.SH" in members


def test_industry_members_none_when_tq_unavailable(monkeypatch):
    """TQ 网关异常 → None（不是 []，不冒充「板块无成分」）。"""

    def boom():
        raise RuntimeError("tq gateway down")

    monkeypatch.setattr(tdx, "sector_items", boom)
    assert cc.industry_members("造纸") is None


def test_industry_members_none_when_constituents_fail(monkeypatch):
    def boom(block_code):
        raise RuntimeError("tq timeout")

    _stub_tq(monkeypatch)
    monkeypatch.setattr(tdx, "constituents", boom)
    assert cc.industry_members("造纸") is None


def test_industry_members_none_when_constituents_empty(monkeypatch):
    """板块必有成分：空列表按取数失败处理，返回 None，绝不返回 []。"""
    _stub_tq(monkeypatch, members={"881167.SH": []})
    assert cc.industry_members("造纸") is None


def test_industry_members_none_for_unknown_industry(monkeypatch):
    """不在真实目录里的行业名 → None（不自编代码、不猜）。"""
    _stub_tq(monkeypatch)
    assert cc.industry_members("纸浆制造") is None


def test_industry_members_uses_real_name_lookup(monkeypatch):
    calls = _stub_tq(monkeypatch)
    cc.industry_members("包装印刷")
    assert ("constituents", "881171.SH") in calls


# ──────────────────────────── B4: beneficiaries ────────────────────────────


def test_beneficiaries_pulp_news_returns_paper_members(monkeypatch):
    _stub_tq(monkeypatch)
    news = [{"title": "纸浆期货涨超1%，原纸价格上调"}]
    out = cc.beneficiaries(news)
    assert out is not None
    assert "造纸" in out and "包装印刷" in out
    assert "600103.SH" in out["造纸"]
    assert out["造纸"] == sorted(set(out["造纸"]))


def test_beneficiaries_dedupes_symbols_across_news(monkeypatch):
    _stub_tq(monkeypatch)
    news = [{"title": "纸浆涨超1%"}, {"title": "瓦楞纸提价"}]
    out = cc.beneficiaries(news)
    assert out is not None
    assert out["造纸"] == ["002078.SZ", "600103.SH", "600966.SH"]


def test_beneficiaries_none_when_tq_unavailable(monkeypatch):
    def boom():
        raise RuntimeError("tq gateway down")

    monkeypatch.setattr(tdx, "sector_items", boom)
    assert cc.beneficiaries([{"title": "纸浆涨超1%"}]) is None


def test_beneficiaries_empty_when_no_commodity_news(monkeypatch):
    _stub_tq(monkeypatch)
    assert cc.beneficiaries([{"title": "央行开展逆回购操作"}]) == {}


# ──────────────────────────── B5: evidence.news 块接线 ────────────────────────────


def _stub_flash(monkeypatch, arts):
    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _FakeMD(arts))


def test_evidence_news_block_annotates_hits_and_block_chain(monkeypatch):
    from datetime import datetime

    _stub_flash(
        monkeypatch,
        [
            _FakeArt(datetime(2026, 11, 11, 7, 10), "商品期货收盘涨跌不一 纸浆涨超1%", imp=2),
            _FakeArt(datetime(2026, 11, 11, 7, 25), "央行开展逆回购操作"),
        ],
    )
    _stub_tq(monkeypatch)
    blk = pe._collect_news_block("2026-11-11T08:30:00+08:00")
    assert blk["quality"] == "ok"
    hit, other = blk["items"]
    assert hit["commodities"] == ["纸浆"]
    assert hit["industries"] == ["造纸", "包装印刷"]
    assert "commodities" not in other and "industries" not in other
    chain = blk["commodity_chain"]
    assert "600103.SH" in chain["造纸"]
    assert chain["包装印刷"] == ["002191.SZ", "600210.SH"]


def test_evidence_news_block_omits_chain_when_tq_fails(monkeypatch):
    from datetime import datetime

    _stub_flash(
        monkeypatch,
        [
            _FakeArt(datetime(2026, 11, 11, 7, 10), "纸浆期货涨超1%", imp=2),
            _FakeArt(datetime(2026, 11, 11, 7, 25), "央行开展逆回购操作"),
        ],
    )

    def boom():
        raise RuntimeError("tq gateway down")

    monkeypatch.setattr(tdx, "sector_items", boom)
    blk = pe._collect_news_block("2026-11-11T08:30:00+08:00")
    assert "commodity_chain" not in blk
    # 商品标注是静态映射，不依赖 TQ → 命中条目仍然带 commodities/industries
    assert blk["items"][0]["commodities"] == ["纸浆"]
    assert blk["quality"] == "ok"


# ──────────────────────────── B6: premarket_outlook 新闻行 ────────────────────────────


def _agent() -> PremarketOutlookAgent:
    a = PremarketOutlookAgent()
    a.name = "premarket_outlook"
    a.display_name = "盘前分析"
    return a


def test_premarket_outlook_news_line_appends_commodity_tag():
    data = {
        "news": [
            {
                "time": "07:10",
                "title": "商品期货收盘涨跌不一 纸浆涨超1%",
                "source": "cls",
                "symbols": [],
                "importance": 2,
                "url": "",
            }
        ]
    }
    _, user_prompt = _agent().build_prompt(data, SimpleNamespace(watchlist=[], model_label=""))
    assert "纸浆涨超1% （商品：纸浆 → 造纸/包装印刷）" in user_prompt


def test_premarket_outlook_plain_news_line_unchanged():
    data = {
        "news": [
            {
                "time": "07:20",
                "title": "工信部推进算力基础设施高质量发展",
                "source": "cls",
                "symbols": [],
                "importance": 1,
                "url": "",
            }
        ]
    }
    _, user_prompt = _agent().build_prompt(data, SimpleNamespace(watchlist=[], model_label=""))
    assert "工信部推进算力基础设施高质量发展" in user_prompt
    assert "（商品：" not in user_prompt
