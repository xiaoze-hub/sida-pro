from datetime import datetime, timedelta, timezone

from src.agents.chat.stock_dossier_aggregator import (
    _fetch_news_by_sentiment,
    classify_news_sentiment,
    format_stock_dossier_text,
)


def test_classify_news_sentiment():
    # 利好：中标/签订/订单 类
    label, score, hits = classify_news_sentiment("公司中标5.2亿元大单，签订战略合作协议")
    assert label == "利好" and score > 0
    assert "中标" in hits

    # 利空：减持/立案 类
    label, score, hits = classify_news_sentiment("控股股东拟减持不超过3%股份，公司遭立案调查")
    assert label == "利空" and score < 0
    assert "减持" in hits

    # 中性：无关键词
    label, score, hits = classify_news_sentiment("公司召开2026年第三次临时股东大会")
    assert label == "中性" and score == 0 and hits == []

    # 混合：多利好压过单一利空 → 仍判利好但分数被冲减
    label, score, _ = classify_news_sentiment("签订重大合同并实现量产，同时小股东减持")
    assert label == "利好" and score == 2


def test_format_stock_dossier_text():
    mock_dossier = {
        "symbol": "600111",
        "market": "CN",
        "quote": "北方稀土 现价: 35.23 涨跌幅: -0.25%",
        "technical": "日K处于均线震荡收敛区间，量比 0.56",
        "kline_patterns": "【同花顺形态】早晨之星(偏多) 强度中",
        "main_intent": "[数据源: 腾讯逐笔·主力意图口径] 洗盘吸筹↑，大单净额 -552.05万",
        "chips": "[数据源: 自算CYQ·120日底仓沉淀] 底仓成本(COST50)：40.16 元 ｜ 筹码峰：39.06 元",
        "decision_pioneer": "[数智决策] GS区间: S区(偏空) | 机构活跃度: 2.14(强于生命线)",
        "broker_reports": [
            {
                "publish_date": "2026-10-07",
                "title": "北方稀土深度研究报告：轻稀土龙头",
                "org_name": "智研科信",
                "rating": "买入",
                "author": "张三",
            }
        ],
        "announcements": [
            {
                "title": "关于签订重大供货合同的公告",
                "event_type": "重大合同",
                "publish_time": datetime(2026, 10, 15, 18, 30),
                "importance": 3,
            }
        ],
        "positive_news": [
            {
                "title": "北方稀土前三季度净利润同比增长45%，超市场预期",
                "source": "eastmoney_news",
                "publish_time": datetime(2026, 10, 16, 9, 10),
                "score": 2,
                "hits": ["同比增长", "超预期"],
            }
        ],
        "negative_news": [
            {
                "title": "公司收到监管问询函",
                "source": "eastmoney_news",
                "publish_time": datetime(2026, 10, 17, 10, 0),
                "score": 1,
                "hits": ["问询函"],
            }
        ],
    }

    text = format_stock_dossier_text(mock_dossier)
    assert "SIDA-Pro 标的客观全景档案 · 600111" in text
    # 9 维齐备
    assert "第一维：实时行情与盘口态势" in text
    assert "第二维：均线与技术面状态" in text
    assert "第三维：K线形态识别" in text
    assert "第四维：主力意图与真金证据" in text
    assert "第五维：底仓筹码沉淀与获利带" in text
    assert "第六维：数智决策三指标" in text
    assert "第七维：券商机构研报" in text
    assert "第八维：近期公司公告" in text
    assert "第九维：利好面消息" in text
    assert "第十维：利空/风险消息" in text
    # 内容真实透传
    assert "北方稀土深度研究报告" in text
    assert "COST50" in text
    assert "早晨之星" in text
    assert "关于签订重大供货合同的公告" in text
    assert "净利润同比增长45%" in text
    assert "超预期" in text  # 命中词溯源
    assert "监管问询函" in text


def test_format_stock_dossier_empty_sections():
    """空数据必须显式「暂无」，不得留白或编造。"""
    text = format_stock_dossier_text({"symbol": "000001", "market": "CN"})
    assert "近 15 日暂无公司公告" in text
    assert "暂无规则筛出的利好条目" in text
    assert "近期暂无公开券商深度研报" in text
    assert "暂无形态数据" in text


class _StubItem:
    def __init__(self, title, content, publish_time, symbols=None, source="eastmoney_news"):
        self.title = title
        self.content = content
        self.publish_time = publish_time
        self.symbols = symbols or []
        self.source = source
        self.url = ""


class _StubMd:
    def __init__(self, items):
        self._items = items

    def news(self, symbols, *, market="CN", since_hours=72, names=None, now=None):
        return list(self._items)


def test_news_relevance_and_time_window(monkeypatch):
    """相关性（标题须点名本股）+ 时间窗口（剔除过期旧闻）双重过滤。"""
    import src.core.marketdata_client as mdc

    now = datetime.now(timezone.utc)
    items = [
        # 本股利好，窗口内 → 保留
        _StubItem("北方稀土：前三季度净利润同比预增135%", "", now - timedelta(hours=5)),
        # 全市场汇总稿（标题不含本股名，即便被打了本股 tag）→ 剔除
        _StubItem("55家公司前三季度业绩预增", "……600111……", now - timedelta(hours=5), symbols=["600111"]),
        # 本股利好但已过期（数月前）→ 剔除
        _StubItem("北方稀土：半年度业绩预增公告", "", now - timedelta(days=120)),
        # 本股但属利空 → 利好通道剔除
        _StubItem("北方稀土：控股股东拟减持3%股份", "", now - timedelta(hours=3)),
    ]
    monkeypatch.setattr(mdc, "get_market_data", lambda: _StubMd(items))

    import asyncio

    out = asyncio.run(_fetch_news_by_sentiment("600111", "CN", "利好", stock_name="北方稀土"))
    titles = [x["title"] for x in out]
    assert titles == ["北方稀土：前三季度净利润同比预增135%"]
    assert out[0]["score"] > 0 and out[0]["hits"]
