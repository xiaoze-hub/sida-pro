# -*- coding: utf-8 -*-
"""SIDA-Pro 数据源定位表 (P0 骨架, 2026-08-31)
=================================================
按 Hermes 规格 D4 (msg_6xmrM17fpdAH... 派活) 实现:
"在 marketdata vendor 层固化一个指标一个权威源 + 降级链",
"src/core/marketdata_client.py 里每个指标的取数函数都明确走权威源 + 降级链, 注释写明口径"。

设计原则:
1. 一个指标只对应一个权威源 (避免双权威数据冲突)
2. 权威源失败时按降级链回退 (最相近口径)
3. 校准时口径 (盘后 vs 盘中) 决定用哪个权威源分支
4. 多用户隔离 (admin / 黄磊 / 娟姐 / demo) 与本文件无关, 由 engine/ConfigProvider 处理

用法 (示例, 待 marketdata_client.py 集成):
    from src.core.marketdata_authoritative_sources import (
        AUTHORITATIVE, FALLBACK_CHAIN, get_vendor_for,
    )
    vendor = get_vendor_for("暗盘资金", phase="盘中")
    if vendor is None:
        vendor = get_vendor_for("暗盘资金", phase="盘后")  # 降级
"""

from __future__ import annotations

# ===== P0 数据源定位表 =====
# 指标 -> (权威源, 盘后/盘中适配, 备注)
AUTHORITATIVE = {
    # ---- 主力资金 ----
    "明盘净额(万)":   ("ths_Zjl_HB", "盘中/盘后",
                       "TQ get_more_info Zjl_HB / 10000; 盘中实时, 盘后用当日累计"),
    "主力净额(手)":   ("ths_Zjl",     "盘中/盘后",
                       "TQ get_more_info Zjl; 口径同通达信"),

    # ---- 暗盘资金 (核心难点) ----
    # 官方口径 (Hermes 2026-08-31 钉死):
    #   明盘(主力大单)=单笔>30万, 可见, 但会骗人(拆单/对倒/散户30万+都污染) -> 不能代表主力
    #   暗盘(主力暗盘资金)=AI拆单识别(私募量化单+机构游资对倒拆单+大单拆小单) -> 真正主力意图
    #   选股铁律: 只选"暗盘资金持续流入"=主力底部吸筹
    #   OHLC分摊=同花顺对外公开简化近似(仅对齐APZJ数字, 妖股日误差23倍, 复刻不了真意图)
    #   主线: L2逐笔 -> 拆单识别 -> 暗盘净额 (必须 L2 逐笔委托号才能还原)
    "暗盘资金(主笔级)":   ("ths_tick_super_level1", "盘中",
                           "thsdk L2 逐笔(含被动侧委托号, 待明日9:30验证); 拆单识别真核; "
                           "盘中有效 / 盘后无效"),
    "暗盘资金(盘后近似)":  ("tck", "盘后",
                           ".tck 委托号级 a28/a32 逐笔; 主动侧 1:1; 被动侧 maker 未落盘, "
                           "Hermes 口径定夺: 暗盘分档 = 逐笔成交分档 (P0-3 校准 +939万)"),
    "暗盘资金(降级)":      ("tencent_tick", "全时段",
                           "腾讯逐笔推断主买主卖, 精度 ~92.6%; 兜底用"),

    # ---- 盘口 ----
    "盘口队列(十档)":     ("tq_depth", "盘中/盘后",
                           "TQ depth 或 thsdk.order_book_bid/ask; 盘中选 thsdk (20档更细)"),
    "盘口队列(降级)":      ("tencent_panel", "盘中/盘后",
                           "腾讯盘口 5档 + 委托笔数 (62/63字段)"),

    # ---- 全市场指标 ----
    "全市场K线(全量)":    ("ths_klines_count_5000", "全时段",
                            "thsdk klines(interval=day, count=5000); 回测基础"),
    "全市场K线(15年)":     ("ths_klines_max", "全时段",
                            "thsdk 单次最多 5000 行; 15年需分页或换 provider"),
    "全市场公式扫描":     ("formula_process_mul_zb", "盘中/盘后",
                            "thsdk 全市场指标批量计算; 降级: 单股循环 (慢)"),

    # ---- 行情 ----
    "实时快照(5档)":      ("ths_market_data_cn", "盘中/盘后",
                           "thsdk market_data_cn 基础+扩展1+扩展2"),
    "K线(日/周/月)":      ("ths_klines", "全时段",
                           "thsdk klines interval=day/week/month"),
    "分时数据":            ("ths_intraday_data", "盘中",
                           "thsdk intraday_data 每分钟价格/量; 仅盘中"),
    "5档盘口":             ("ths_depth", "全时段",
                           "thsdk depth; 20档请用 order_book_bid/ask"),

    # ---- 异动/选股 ----
    "涨停归因":           ("wencai_zhangting_guoyin", "盘后",
                           "wencai_nlp '今日涨停个股及涨停原因'; 仅盘后"),
    "板块资金流向":        ("wencai_bankuai_jingliuru_top20", "盘中/盘后",
                           "wencai_nlp '今日行业板块主力资金净流入排名前20'"),
    "板块涨幅":           ("wencai_bankuai_zhangfu", "盘中/盘后",
                           "wencai_nlp '今日行业板块涨幅排名前20'"),
    "北向持股":           ("wencai_xiangbei_chigushou", "全时段",
                           "wencai_nlp; 注: 实时净买入已停披露 (2024-08 起), 仅持股比例"),
    "龙虎榜":             ("wencai_longhuliang", "盘后",
                           "wencai_nlp '昨日龙虎榜个股, 显示上榜原因'; 仅盘后"),
    "主力连续N日流入":     ("wencai_lianxu3ri_jingliuru", "全时段",
                           "wencai_nlp '连续3日主力资金净流入的个股, 非ST'"),
    "连板个股":           ("wencai_lianban", "盘中",
                           "wencai_nlp '今日连板个股, 显示连板数'"),
    "创新高":             ("wencai_chuangxin_gao", "盘中",
                           "wencai_nlp '今日创历史新高的个股'"),
    "强势股(涨>7%)":      ("wencai_qiangshi_7pct", "盘中",
                           "wencai_nlp '今日涨幅超过7%的个股, 非ST'"),
    "涨停选股(一句话)":   ("wencai_nlp", "盘中/盘后",
                           "wencai_nlp 自然语言选股 (限频 250ms/次)"),

    # ---- L2 数据 (盘中) ----
    "逐笔大单+方向":      ("ths_big_order_flow", "盘中",
                           "thsdk big_order_flow 方向 1/2/-1/-2"),
    "L2 逐笔成交":        ("ths_tick_super_level1", "盘中",
                           "thsdk tick_super_level1; 盘后无效"),
    "20档委托队列":       ("ths_order_book_bid_ask", "盘中",
                           "thsdk order_book_bid/ask 各 20档"),

    # ---- 本地落盘 ----
    "本机 .tck 解析":       ("local_tck", "盘后",
                           ".tck 超盘回放落盘; 盘后逐笔"),
    "本机 .img 解析":      ("local_img", "盘中/盘后",
                           ".img 10档盘口 3 秒级快照; 已含委托笔数 62/63"),
    "本机 委托队列 (无委托号)": ("local_img", "盘后",
                           ".img 字段 64 委托队列仅有每笔挂单量, 无委托号 → 不能做主笔级暗盘还原"),
}

# ===== 降级链 (按指标) =====
# 顺序: 权威源 → 备选权威源 → 降级源 → 最后兜底
FALLBACK_CHAIN = {
    "明盘净额(万)":       ["ths_Zjl_HB", "ths_Zjl", "tq_zjl", "tencent_capital_flow"],
    "暗盘资金(主笔级)":   ["ths_tick_super_level1", "tck", "tencent_tick"],
    "暗盘资金(盘后近似)":  ["tck", "ths_tick_super_level1", "tencent_tick"],
    "盘口队列(十档)":     ["ths_order_book_bid_ask", "tq_depth", "tencent_panel", "ths_depth"],
    "实时快照(5档)":      ["ths_market_data_cn", "tq_snapshot", "tencent_quote"],
    "涨停归因":           ["wencai_zhangting_guoyin", "ths_zt_list"],
    "龙虎榜":             ["wencai_longhuliang", "ths_dragon_tiger"],
}


# ===== 硬约束 (Hermes 红线 + 手册 §10) =====
# 1. 金额 = 元 (所有净额/成交额/封单字段统一口径, 手册 §3.2 §10)
# 2. 成交量 = 股 (vol 字段统一, 转换到"手"在展示层做)
# 3. 缺失数据显式标 "无数据" (None + note), 禁止推测编造 (手册 §10 风险 4)
# 4. 多用户隔离 (admin / 黄磊 / 娟姐 / demo, 手册 §5.3), 所有查询自动注入 user_id 过滤
# 5. thsdk 走云端 L2 通道 (凭据 THS_USERNAME/THS_PASSWORD, 手册 §3.2 已实测登录成功)
# 6. 不主张"我们的 AI 方向预测" (手册 §8.2 风险), thsdk 直出是数据, 明盘+暗盘是同花顺官方口径
# 7. 盘后门 vs 盘中门 严格区分 (tick_super_level1 仅盘中, 手册 §4.6)


def get_vendor_for(metric: str, phase: str = "盘中"):
    """根据指标 + 时段, 返回 (权威源vendor, 降级链) 元组。
    phase: '盘中' | '盘后' | '全时段'
    """
    entry = AUTHORITATIVE.get(metric)
    if entry is None:
        raise KeyError(f"指标 '{metric}' 不在数据源定位表中")
    vendor, valid_phases, _note = entry
    if phase not in valid_phases.split("/"):
        return None
    fallback = FALLBACK_CHAIN.get(metric, [])
    return {"authoritative": vendor, "fallback": fallback, "phase": phase, "metric": metric}
