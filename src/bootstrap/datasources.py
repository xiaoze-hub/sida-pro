"""数据源/策略/示例股票种子与对账(原 server.py 原样搬移, W3.2/D1)。"""

from __future__ import annotations

import logging
import os

from src.core.strategy_catalog import ensure_strategy_catalog
from src.web.database import SessionLocal
from src.web.models import DataSource, Stock

logger = logging.getLogger("server")


def seed_sample_stocks():
    """首次启动时添加示例股票"""
    db = SessionLocal()
    try:
        # 只在没有任何股票时才添加示例
        if db.query(Stock).count() > 0:
            return

        samples = [
            {"symbol": "600519", "name": "贵州茅台", "market": "CN"},
            {"symbol": "002594", "name": "比亚迪", "market": "CN"},
            {"symbol": "300750", "name": "宁德时代", "market": "CN"},
            {"symbol": "00700", "name": "腾讯控股", "market": "HK"},
            {"symbol": "AAPL", "name": "苹果", "market": "US"},
        ]
        for s in samples:
            db.add(Stock(**s))
        db.commit()
        logger.info("已添加 5 只示例股票（首次启动）")
    finally:
        db.close()


# 预置数据源种子(供 seed_data_sources / reconcile_data_sources 复用)。
# 只增不删的 upsert 目标;删孤儿的对账逻辑见 reconcile_data_sources。
DATA_SOURCE_SEEDS: list[dict] = [
        # 新闻类数据源
        {
            "name": "雪球资讯",
            "type": "news",
            "provider": "xueqiu",
            "config": {
                "cookies": "",
                "description": "雪球个股新闻聚合，需要登录 cookie",
            },
            "enabled": False,
            "priority": 0,
            "supports_batch": True,
            "test_symbols": ["601127", "600519"],
        },
        {
            "name": "东方财富资讯",
            "type": "news",
            "provider": "eastmoney_news",
            "config": {},
            "enabled": True,
            "priority": 1,
            "supports_batch": False,  # 每只股票单独请求
            "test_symbols": ["601127", "600519"],
        },
        {
            "name": "东方财富公告",
            "type": "news",
            "provider": "eastmoney",
            "config": {},
            "enabled": True,
            "priority": 2,
            "supports_batch": True,  # 支持批量查询
            "test_symbols": ["601127", "600519"],
        },
        # K线数据源
        {
            "name": "通达信TQ K线",
            "type": "kline",
            "provider": "tq",
            "config": {
                "description": "通达信TQ本机网关(127.0.0.1:5100, 经frp隧道到小主机客户端), "
                "前复权日K。仅CN; 隧道断开自动降级腾讯/东财。",
            },
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": ["600519", "000001", "300750"],
        },
        {
            "name": "腾讯K线",
            "type": "kline",
            "provider": "tencent",
            "config": {},
            "enabled": True,
            "priority": 1,
            "supports_batch": False,
            "test_symbols": ["601127", "600519", "300750"],
        },
        {
            "name": "东方财富 K线",
            "type": "kline",
            "provider": "eastmoney",
            "config": {"description": "东方财富日线,A股/港股长历史兜底(免 key)。"},
            "enabled": True,
            "priority": 5,   # 腾讯(0)之后、Tushare(10)之前 → CN/HK 兜底
            "supports_batch": False,
            "test_symbols": ["600519", "00700"],
        },
        {
            "name": "Stooq K线",
            "type": "kline",
            "provider": "stooq",
            "config": {"description": "Stooq 美股日线兜底(免 key)。"},
            "enabled": True,
            "priority": 15,  # US 兜底(腾讯 0 之后)
            "supports_batch": False,
            "test_symbols": ["AAPL"],
        },
        {
            "name": "Yahoo K线",
            "type": "kline",
            "provider": "yahoo",
            "config": {
                "description": "Yahoo chart v8 日线(US/HK,免 key 免 crumb)。国内访问通常需代理,"
                "在 config.proxy 填写代理地址后启用,作港股 K线第二源/美股更稳兜底。",
                "proxy": "",
            },
            "enabled": False,  # 需代理,默认关(同 YFinance 口径),用户配好 proxy 再开
            "priority": 20,  # US/HK 最后兜底
            "supports_batch": False,
            "test_symbols": ["AAPL", "00700"],
        },
        # 资金流向数据源
        {
            "name": "东方财富资金流",
            "type": "capital_flow",
            "provider": "eastmoney",
            "config": {},
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": ["601127", "600519"],
        },
        {
            "name": "新浪资金流",
            "type": "capital_flow",
            "provider": "sina",
            "config": {
                "description": "新浪资金流入趋势(CN,免 key)。作东财之后的第二源,"
                "仅含主力/超大单净额(无大/中/小单细分)。",
            },
            "enabled": True,
            "priority": 5,  # 东财(0)之后的 CN 第二源
            "supports_batch": False,
            "test_symbols": ["601127", "600519"],
        },
        # 实时行情数据源
        {
            "name": "通达信TQ行情",
            "type": "quote",
            "provider": "tq",
            "config": {
                "description": "通达信TQ本机网关(127.0.0.1:5100, 经frp隧道到小主机客户端), "
                "实时快照含内外盘。仅CN; 实测延迟<30ms, 隧道断开自动降级腾讯。",
            },
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": ["600519", "000001", "300750"],
        },
        {
            "name": "腾讯行情",
            "type": "quote",
            "provider": "tencent",
            "config": {},
            "enabled": True,
            "priority": 1,
            "supports_batch": True,
            "test_symbols": ["601127", "600519", "300750"],
        },
        {
            "name": "东方财富行情",
            "type": "quote",
            "provider": "eastmoney",
            "config": {"description": "东方财富 push2 实时行情(CN,免 key)。作腾讯之后的 A 股第二源。"},
            "enabled": True,
            "priority": 3,  # 腾讯(0)之后的 CN 第二源(sina/yfinance 不支持 CN)
            "supports_batch": False,  # push2 stock/get 单只查询,逐只
            "test_symbols": ["601127", "600519", "300750"],
        },
        {
            "name": "Sina 行情",
            "type": "quote",
            "provider": "sina",
            "config": {"description": "新浪美股/港股实时行情,免 key 免代理,作腾讯之后的 US/HK 备源。"},
            "enabled": True,
            "priority": 5,   # 腾讯(0)之后
            "supports_batch": True,
            "test_symbols": ["AAPL", "00700"],
        },
        {
            "name": "YFinance 行情",
            "type": "quote",
            "provider": "yfinance",
            "config": {
                "description": "Yahoo Finance,需 pip install yfinance。适用 HK/US,A 股不可用。",
            },
            "enabled": False,
            "priority": 10,
            "supports_batch": True,
            "test_symbols": ["AAPL"],
        },
        # 事件日历数据源（基于公告结构化）
        {
            "name": "东方财富事件日历",
            "type": "events",
            "provider": "eastmoney",
            "config": {},
            "enabled": True,
            "priority": 0,
            "supports_batch": True,
            "test_symbols": ["601127", "600519"],
        },
        # 快讯数据源（7×24 电报，市场级，不按 symbols 过滤）
        {
            "name": "财联社快讯",
            "type": "flash_news",
            "provider": "cls",
            "config": {"description": "财联社 7×24 电报(免 key,本地签名)。"},
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": [],
        },
        {
            "name": "新浪7x24快讯",
            "type": "flash_news",
            "provider": "sina",
            "config": {"description": "新浪财经 7×24 直播,带关联个股。"},
            "enabled": True,
            "priority": 5,
            "supports_batch": False,
            "test_symbols": [],
        },
        {
            "name": "东方财富7x24快讯",
            "type": "flash_news",
            "provider": "eastmoney",
            "config": {"description": "东财 np-weblist 7×24 资讯,与财联社互备。"},
            "enabled": True,
            "priority": 10,
            "supports_batch": False,
            "test_symbols": [],
        },
        # 基本面数据源（按 symbol，估值/股本/财报指标）
        {
            "name": "腾讯基本面",
            "type": "fundamentals",
            "provider": "tencent",
            "config": {"description": "腾讯 qt.gtimg 估值快照(CN,免 key):PE/PB/市值。"},
            "enabled": True,
            "priority": 0,
            "supports_batch": True,
            "test_symbols": ["600519", "000001"],
        },
        {
            "name": "东方财富基本面",
            "type": "fundamentals",
            "provider": "eastmoney",
            "config": {
                "description": "东财基本面:CN 股本/市值(push2),US/HK 财报指标(GMAININDICATOR)。"
            },
            "enabled": True,
            "priority": 5,
            "supports_batch": True,
            "test_symbols": ["600519", "AAPL"],
        },
        # 市场资金面数据源（龙虎榜/融资融券/股东户数/分红/北向资金）
        {
            "name": "东财龙虎榜",
            "type": "dragon_tiger",
            "provider": "eastmoney",
            "config": {
                "description": "东财每日龙虎榜(市场级,需配 test_date 测试)。",
                "test_date": "",
            },
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": [],
        },
        {
            "name": "FTShare龙虎榜",
            "type": "dragon_tiger",
            "provider": "ftshare",
            "config": {
                "description": "FTShare MCP 龙虎榜备源(免 key,云服务器可直连)。东财断供时自动降级。",
            },
            "enabled": True,
            "priority": 5,
            "supports_batch": False,
            "test_symbols": [],
        },
        {
            "name": "东财融资融券",
            "type": "margin",
            "provider": "eastmoney",
            "config": {"description": "东财个股融资融券明细(按 symbol)。"},
            "enabled": True,
            "priority": 0,
            "supports_batch": True,
            "test_symbols": ["600519", "000001"],
        },
        {
            "name": "FTShare融资融券",
            "type": "margin",
            "provider": "ftshare",
            "config": {
                "description": "FTShare MCP 两融备源(免 key,云服务器可直连)。东财断供时自动降级。",
            },
            "enabled": True,
            "priority": 5,
            "supports_batch": False,
            "test_symbols": ["600519", "000001"],
        },
        {
            "name": "东财股东户数",
            "type": "shareholders",
            "provider": "eastmoney",
            "config": {"description": "东财股东户数变化(按 symbol,季度)。"},
            "enabled": True,
            "priority": 0,
            "supports_batch": True,
            "test_symbols": ["600519", "000001"],
        },
        {
            "name": "东财分红",
            "type": "dividend",
            "provider": "eastmoney",
            "config": {"description": "东财分红送转历史(按 symbol)。"},
            "enabled": True,
            "priority": 0,
            "supports_batch": True,
            "test_symbols": ["600519", "000001"],
        },
        {
            "name": "智兔分红",
            "type": "dividend",
            "provider": "zhitu",
            "config": {
                "api_keys": [],
                "description": "智兔数服分红送配备源(设置页 zhitu_token 或容器 env ZHITU_TOKEN 维护, 不进仓库)。",
            },
            "enabled": True,
            "priority": 5,
            "supports_batch": False,
            "test_symbols": ["600519", "000001"],
        },
        {
            "name": "智兔K线",
            "type": "kline",
            "provider": "zhitu",
            "config": {
                "api_keys": [],  # P1-15: 真实 key 不进仓库, 走设置页/容器 env
                "description": "智兔数服日线(双 key 池化)。东财在云服务器不稳定,智兔作优先稳定源。",
            },
            "enabled": True,
            "priority": 1,   # 腾讯(0)之后、东财(5)之前 → CN 优先稳定源
            "supports_batch": False,
            "test_symbols": ["600519", "000001"],
        },
        {
            "name": "智兔股东",

            "type": "shareholders",
            "provider": "zhitu",
            "config": {
                "api_keys": [],  # P1-15: 真实 key 不进仓库, 走设置页/容器 env
                "description": "智兔数服十大股东/股东变化(双 key 池化)。东财股东接口不稳定,智兔优先。",
            },
            "enabled": True,
            "priority": 0,   # 高于东财 → 股东优先源
            "supports_batch": False,
            "test_symbols": ["600519", "000001"],
        },
        {
            "name": "智兔基本面",
            "type": "fundamentals",
            "provider": "zhitu",
            "config": {
                "api_keys": [],  # P1-15: 真实 key 不进仓库, 走设置页/容器 env
                "description": "智兔数服财务主要(PE/PB/市值,双 key 池化)。东财财务接口不稳定,智兔优先。",
            },
            "enabled": True,
            "priority": 0,   # 高于腾讯/东财 → 基本面优先源
            "supports_batch": False,
            "test_symbols": ["600519", "000001"],
        },
        {
            "name": "同花顺北向资金",
            "type": "northbound",
            "provider": "ths",
            "config": {
                "description": "同花顺北向资金实时(东财已断供;深股通近期不可靠)。"
            },
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": [],
        },
        # 板块/大盘资金数据源(同花顺,免登录页面版)
        {
            "name": "同花顺板块资金",
            "type": "board_capital_flow",
            "provider": "ths_flow",
            "config": {
                "description": "同花顺行业/概念资金流向(页面版解析,免登录,海外可达)。",
            },
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": [],
        },
        {
            "name": "同花顺大盘资金",
            "type": "market_capital_flow",
            "provider": "ths_market_flow",
            "config": {
                "description": "全市场行业资金合计(大盘主力净额)。",
            },
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": [],
        },
        # 同花顺 Web(实时行情/K线/快讯,免登录,香港节点可达)
        {
            "name": "同花顺实时行情",
            "type": "quote",
            "provider": "ths",
            "config": {
                "description": "fuyao 统一行情聚合接口(免登录,实时快照)。生产IP常被403,降级档。",
            },
            "enabled": True,
            "priority": 2,  # tq(0)/tencent(1) 之后: 生产实测 fuyao 常年403
            "supports_batch": False,
            "test_symbols": ["600519", "000001"],
        },
        {
            "name": "同花顺K线",
            "type": "kline",
            "provider": "ths",
            "config": {
                "description": "d.10jqka.com.cn 日K线(免登录)。",
            },
            "enabled": True,
            "priority": 6,
            "supports_batch": False,
            "test_symbols": ["600519"],
        },
        {
            "name": "同花顺快讯",
            "type": "flash_news",
            "provider": "ths",
            "config": {
                "description": "news.10jqka.com.cn 7x24 快讯(免登录)。",
            },
            "enabled": True,
            "priority": 3,
            "supports_batch": False,
            "test_symbols": [],
        },
        # K线截图数据源
        {
            "name": "雪球K线截图",
            "type": "chart",
            "provider": "xueqiu",
            "config": {
                "viewport": {"width": 1280, "height": 900},
                "extra_wait_ms": 3000,
            },
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": ["601127"],
        },
        {
            "name": "东方财富K线截图",
            "type": "chart",
            "provider": "eastmoney",
            "config": {
                "viewport": {"width": 1280, "height": 900},
                "extra_wait_ms": 2000,
            },
            "enabled": False,
            "priority": 1,
            "supports_batch": False,
            "test_symbols": ["601127"],
        },
        {
            "name": "百度财经日历",
            "type": "macro_calendar",
            "provider": "ftshare",
            "config": {
                "description": "FTShare MCP 百度财经日历(免 key,云服务器可直连):经济数据/IPO/财报披露时间/交易提醒。市场级,按日期范围查询,不绑定个股。",
            },
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": [],
        },
        {
            "name": "通达信问小达",
            "type": "wenda",
            "provider": "tdx",
            "config": {
                "description": "通达信问小达 MCP(需 TDX_API_KEY):自然语言投研问答,覆盖个股行情/智能选股/板块排行/财务/技术/资金流向。市场级,不绑定个股 symbol 模型。",
            },
            "enabled": True,
            "priority": 0,
            "supports_batch": False,
            "test_symbols": [],
        },
        {
            "name": "Alpha Vantage 美股/全球",
            "type": "quote",
            "provider": "alphavantage",
            "config": {
                "api_keys": [k for k in (os.getenv("ALPHAVANTAGE_KEYS", "") or os.getenv("ALPHAVANTAGE_KEY", "")).split(",") if k],
                "description": "Alpha Vantage 美股/全球实时报价(免费 25 req/day/key, 多 key 池可放大额度)。",
            },
            "enabled": False,
            "priority": 10,
            "supports_batch": False,
            "test_symbols": ["AAPL", "MSFT"],
        },
        {
            "name": "Twelve Data 美股",
            "type": "quote",
            "provider": "twelvedata",
            "config": {
                "api_keys": [k for k in (os.getenv("TWELVEDATA_KEYS", "") or os.getenv("TWELVEDATA_KEY", "")).split(",") if k],
                "description": "Twelve Data 美股实时报价(免费 800 req/day/key, 多 key 池可放大额度)。",
            },
            "enabled": False,
            "priority": 11,
            "supports_batch": False,
            "test_symbols": ["AAPL", "MSFT"],
        },
]


def seed_data_sources(db=None) -> list[dict]:
    """初始化预置数据源(按 name+provider 只增不删的 upsert)。

    db 为 None 时自建独立 session 并自行 commit/close(兼容旧调用方式);
    传入 db 时复用调用方 session,不 commit/close,交由调用方统一处理
    (供 reconcile_data_sources 在同一事务里接着做删孤儿)。

    返回本次新增(缺失被补齐)的种子记录摘要列表 [{"name","type","provider"}, ...]。
    """
    owns_session = db is None
    if owns_session:
        db = SessionLocal()

    seeded_missing: list[dict] = []
    for source_data in DATA_SOURCE_SEEDS:
        existing = (
            db.query(DataSource)
            .filter(
                DataSource.name == source_data["name"],
                DataSource.provider == source_data["provider"],
            )
            .first()
        )
        if existing:
            # 更新已存在记录的新字段（保留用户可能修改的配置）
            if existing.supports_batch != source_data.get("supports_batch", False):
                existing.supports_batch = source_data.get("supports_batch", False)
            if not existing.test_symbols:  # 只在空时更新
                existing.test_symbols = source_data.get("test_symbols", [])
        else:
            db.add(DataSource(**source_data))
            seeded_missing.append(
                {
                    "name": source_data["name"],
                    "type": source_data["type"],
                    "provider": source_data["provider"],
                }
            )

    if owns_session:
        db.commit()
        db.close()

    return seeded_missing


def _seed_providers_by_type() -> dict[str, set[str]]:
    """从 DATA_SOURCE_SEEDS 推导每个 type 当前合法的 provider 集合。"""
    result: dict[str, set[str]] = {}
    for source_data in DATA_SOURCE_SEEDS:
        result.setdefault(source_data["type"], set()).add(source_data["provider"])
    return result


def reconcile_data_sources(db) -> dict:
    """数据源表温和对账:补缺失默认 + 删孤儿,保留用户有效自定义/凭证。

    孤儿判定: legal(type) = PACKAGE_VENDORS_BY_TYPE.get(type, frozenset()) | seed 内该 type 的 provider 集合;
    DB 行 (type, provider) 不在 legal(type) 内即孤儿。news/chart 等非引擎类型(包内集合为空)的合法性完全由 seed 决定。

    只删孤儿行,其余行(含用户改过 config/priority/enabled 的自定义行)原样保留。
    """
    from marketdata import PACKAGE_VENDORS_BY_TYPE

    seeded_missing = seed_data_sources(db)
    seed_providers_by_type = _seed_providers_by_type()

    deleted: list[dict] = []
    for row in db.query(DataSource).all():
        legal = PACKAGE_VENDORS_BY_TYPE.get(row.type, frozenset()) | seed_providers_by_type.get(row.type, set())
        if row.provider not in legal:
            deleted.append(
                {"id": row.id, "type": row.type, "provider": row.provider, "name": row.name}
            )
            db.delete(row)

    if seeded_missing:
        logger.info(f"数据源对账: 补齐缺失默认 {len(seeded_missing)} 条: {seeded_missing}")
    if deleted:
        logger.info(f"数据源对账: 删除孤儿数据源 {len(deleted)} 条: {deleted}")

    db.commit()
    return {"deleted": deleted, "seeded_missing": seeded_missing}


def seed_strategies():
    """初始化策略目录。"""
    ensure_strategy_catalog()
    logger.info("策略目录初始化完成")
