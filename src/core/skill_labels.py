"""Skill 接口/工具中文注释与官方名称映射（2026-11-04）。

用于管理后台用量监控、Top Skills 列表及开放 API 列表的中文平易化展示，
避免管理层和用户在报表中看到冰冷的英文函数名（如 get_main_intent, get_thsdk_dde）。
"""

from __future__ import annotations

SKILL_CHINESE_LABELS: dict[str, str] = {
    # ── 主力与逐笔 ──
    "get_main_intent": "腾讯逐笔·主力意图分析",
    "get_cancel_order_intent": "撤单意图与托盘探测",
    "get_order_granularity": "委托粒度与拆单特征",
    "get_fund_structure": "四档资金结构（筹码分布）",
    # ── 同花顺 THSDK / 扩展 ──
    "get_thsdk_dde": "DDE 大单动向与主力博弈",
    "get_thsdk_extended": "同花顺扩展指标及机构评级",
    "get_thsdk_quotes": "同花顺高速行情快照",
    # ── 市场与走势 ──
    "get_trend_profile": "多周期趋势与均线画像",
    "get_technical_analysis": "技术面指标与形态识别",
    "get_chip_accumulation": "CYQ 筹码分布与成本支撑",
    "get_decision_pioneer": "决策先锋多维信号",
    "get_intraday_profile": "盘中分时与逐笔追踪",
    "get_stock_factors": "量化因子全市场分位",
    "get_scenario_matrix": "情景矩阵与关键位动态模拟",
    "get_factor_winrate": "因子胜率与历史回测",
    "get_stock_dossier": "个股全息档案（研报/公告）",
    # ── 盘面与策略 ──
    "market_overview": "市场全景复盘与风格轮动",
    "auction_hunter": "集合竞价异动与 M 标记",
    "deep_diagnosis": "全景数智深度穿透诊断",
    "risk_judge": "红蓝对抗风险与防线预警",
    "devil_advocate": "逆向风险情报与可证伪反驳",
    "premarket_strategist": "盘前机会备料与埋伏池",
    "postmarket_reviewer": "盘后复盘与暗盘机构行为",
    # ── 宏观与另类 ──
    "index_overview": "盘面宽基指数总览与四档资金",
    "macro_rates": "宏观利率与流动性监控",
}


def label_for(name: str) -> str:
    """获取 skill 或工具的中文注释名；未收录时返回规范化的可读名称。"""
    if not name:
        return "未知接口"
    key = name.strip()
    if key in SKILL_CHINESE_LABELS:
        return SKILL_CHINESE_LABELS[key]
    # 降级：下划线转空格并首字母大写
    cleaned = key.replace("_", " ").strip()
    return cleaned.title() if cleaned else key
