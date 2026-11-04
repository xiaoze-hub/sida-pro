"""Skill 接口/工具中文注释与官方名称映射（2026-11-04）。

用于管理后台用量监控、Top Skills 列表及开放 API 列表的中文平易化展示，
避免管理层和用户在报表中看到冰冷的英文函数名（如 get_main_intent, get_thsdk_dde）。

覆盖口径（2026-11-04 修复）：`OPEN_SKILLS` 的**每一个真实键**都必须有中文标签
—— 首版 25 条映射里只覆盖了 3 个真正在开放的键，其余 21 个在报表里仍是
`Get Stock Quote` 这种降级名，等于没做。结构钉子见
`tests/test_real_usage_analytics.py::test_所有开放skill都有中文标签`。

未收录的名字**保留原名并显式标注未收录**（不再是易误解的 title-case 降级名）：
报表要能一眼看出"这是没映射过的接口"，而不是把它当成官方名称。
"""

from __future__ import annotations

#: 未收录接口的显式标记后缀。
UNKNOWN_LABEL_SUFFIX = "（未收录）"

#: 名字本身为空时的兜底。
EMPTY_LABEL = "未知接口"

SKILL_CHINESE_LABELS: dict[str, str] = {
    # ── 行情 / 报价 ──
    "get_stock_quote": "个股实时行情快照",
    "get_kline_patterns": "K 线形态与组合识别",
    "get_orderbook": "盘口挂单与队列结构",
    "get_delta_series": "增量变化序列（分时差分）",
    # ── 主力与逐笔 ──
    "get_main_intent": "腾讯逐笔·主力意图分析",
    "get_intent_explain": "主力意图归因解读",
    "get_cancel_order_intent": "撤单意图与托盘探测",
    "get_order_granularity": "委托粒度与拆单特征",
    "get_fund_structure": "四档资金结构（筹码分布）",
    # ── 同花顺 THSDK / 扩展 ──
    "get_thsdk_dde": "DDE 大单动向与主力博弈",
    "get_thsdk_extended": "同花顺扩展指标及机构评级",
    "get_thsdk_quotes": "同花顺高速行情快照",
    # ── 资金 / 北向 ──
    "get_capital_flow": "资金流向与主力净额",
    "get_northbound": "北向资金与外资动向",
    "get_main_flow_compare": "多标的主力资金对比",
    # ── 市场与走势 ──
    "get_trend_profile": "多周期趋势与均线画像",
    "get_technical_analysis": "技术面指标与形态识别",
    "get_chip_accumulation": "CYQ 筹码分布与成本支撑",
    "get_decision_pioneer": "决策先锋多维信号",
    "get_intraday_profile": "盘中分时与逐笔追踪",
    "get_stock_factors": "量化因子全市场分位",
    "get_scenario_matrix": "情景矩阵与关键位动态模拟",
    "get_factor_winrate": "因子胜率与历史回测",
    "get_factor_ic_report": "因子 IC 与有效性报告",
    "get_stock_dossier": "个股全息档案（研报/公告）",
    # ── 盘面 / 情绪 / 异动 ──
    "market_overview": "市场全景复盘与风格轮动",
    "auction_hunter": "集合竞价异动与 M 标记",
    "get_auction_data": "集合竞价数据与竞价池",
    "get_sentiment_cycle": "市场情绪周期定位",
    "get_market_anomalies": "全市场异动扫描",
    "get_hot_stocks": "热股榜与热度排行",
    "get_rally_analysis": "拉升动能与承接分析",
    "get_opportunities": "机会挖掘与候选池",
    "get_strategy_signals": "策略信号与买卖点",
    # ── 基本面 / 新闻 / 事件 ──
    "get_fundamentals_detail": "财务基本面明细",
    "get_forecast": "业绩预测与一致预期",
    "get_market_news": "市场新闻与资讯速览",
    "get_event_catalyst": "事件催化与题材驱动",
    # ── 诊断 / 策略角色 ──
    "deep_diagnosis": "全景数智深度穿透诊断",
    "risk_judge": "红蓝对抗风险与防线预警",
    "devil_advocate": "逆向风险情报与可证伪反驳",
    "premarket_strategist": "盘前机会备料与埋伏池",
    "postmarket_reviewer": "盘后复盘与暗盘机构行为",
    "get_stock_report": "个股聚合体检报告",
    # ── 宏观与另类 ──
    "index_overview": "盘面宽基指数总览与四档资金",
    "macro_rates": "宏观利率与流动性监控",
}


def label_for(name: str) -> str:
    """获取 skill 或工具的中文注释名。

    - 已收录：返回官方中文名；
    - 未收录：**保留原名**并追加 ``（未收录）``，让人一眼看出没映射过；
    - 空名：``"未知接口"``。
    """
    if name is None:
        return EMPTY_LABEL
    key = str(name).strip()
    if not key:
        return EMPTY_LABEL
    known = SKILL_CHINESE_LABELS.get(key)
    if known:
        return known
    return f"{key}{UNKNOWN_LABEL_SUFFIX}"


def is_known_label(name: str) -> bool:
    """该名字是否有官方中文标签（报表里用来区分"已映射"和"未收录"）。"""
    if not name:
        return False
    return str(name).strip() in SKILL_CHINESE_LABELS
