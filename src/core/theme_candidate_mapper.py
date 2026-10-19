"""突发事件题材与全市场标的映射器 (Theme Candidate Mapper)

解决核心痛点:
盘前报告识别到了外部科技/大宗重磅利好(如 OpenAI 机器人、低空新政、芯片突破),
但因标的不在用户自选股内,导致盘前无法给出具体个股作战建议。

本模块能力:
1. 提取事件流中的关键题材词(机器人、AI大脑、低空经济、半导体、涨价品种等);
2. 检索对应概念/行业板块(依托本地 TDX / 静态概念映射 / 悟道题材等多层兜底);
3. 结合全市场妖股基因分(demon_factors)、市值与主板优先原则,
   自动筛选出 3~5 只高辨识度龙头候选标的;
4. 注入到盘前分析上下文的 `theme_candidates` 模块中,供 LLM 撰写全市场作战潜伏计划。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 预置高频科技与大宗主线关键词映射字典 (主题名 -> 板块关键词列表)
THEME_KEYWORD_RULES: dict[str, list[str]] = {
    "人形机器人": ["机器人", "减速器", "伺服", "电机", "传感器", "执行器", "AI大脑", "具身智能"],
    "低空经济": ["低空经济", "飞行汽车", "eVTOL", "无人机", "通航", "空管"],
    "人工智能/算力": ["算力", "光模块", "CPO", "PCB", "服务器", "大模型", "AI"],
    "半导体/芯片": ["半导体", "芯片", "光刻机", "先进封装", "EDA", "存储"],
    "大宗涨价/资源": ["稀土", "锂", "磷化工", "钛白粉", "黄金", "铜", "化肥", "农药"],
}

# 兜底核心龙头股票池（当网关网络瞬断或 TQ 离线时，秒级兜底对应题材的最具辨识度龙头）
STATIC_THEME_LEADERS: dict[str, list[dict[str, str]]] = {
    "人形机器人": [
        {"symbol": "002046", "name": "国机精工", "reason": "人形机器人轴承+主板龙头，高妖股记忆"},
        {"symbol": "002896", "name": "中大力德", "reason": "减速器+电机伺服一体化核心，主板高辨识度"},
        {"symbol": "002050", "name": "三花智控", "reason": "人形机器人旋转/直线执行器绝对核心"},
        {"symbol": "002472", "name": "双环传动", "reason": "精密减速器+RV/谐波双轮驱动龙头"},
        {"symbol": "603728", "name": "鸣志电器", "reason": "空心杯电机全球领先，机器人关节核心标的"},
    ],
    "低空经济": [
        {"symbol": "002361", "name": "神剑股份", "reason": "复合材料部件+航空低空整机配套，极妖先锋"},
        {"symbol": "002097", "name": "山河智能", "reason": "轻型飞机+低空无人装备，主板低位弹性"},
        {"symbol": "000099", "name": "中信海直", "reason": "低空通航运营第一股，国家队运营龙头"},
        {"symbol": "600372", "name": "中航机载", "reason": "机载航电核心，低空飞行器大脑"},
    ],
    "人工智能/算力": [
        {"symbol": "000977", "name": "浪潮信息", "reason": "AI服务器绝对龙头，算力基建中枢"},
        {"symbol": "002230", "name": "科大讯飞", "reason": "认知大模型+AI大脑+具身交互国家队"},
        {"symbol": "000938", "name": "紫光股份", "reason": "AI交换机+算力网络核心主板标的"},
        {"symbol": "002463", "name": "沪电股份", "reason": "AI服务器高多层PCB板全球主力供应商"},
    ],
    "半导体/芯片": [
        {"symbol": "002049", "name": "紫光国微", "reason": "特种芯片+安全芯片主板龙头"},
        {"symbol": "600584", "name": "长电科技", "reason": "先进封装Chiplet绝对龙头"},
        {"symbol": "002371", "name": "北方华创", "reason": "半导体设备平台型航母"},
        {"symbol": "300456", "name": "赛微电子", "reason": "MEMS代工全球领先，传感器核心"},
    ],
    "大宗涨价/资源": [
        {"symbol": "600111", "name": "北方稀土", "reason": "轻稀土绝对定价权，量价齐升"},
        {"symbol": "002460", "name": "赣锋锂业", "reason": "锂盐全产业链巨头，碳酸锂价格弹性"},
        {"symbol": "601899", "name": "紫金矿业", "reason": "全球金铜资源龙头，地缘与通胀避险中枢"},
        {"symbol": "002601", "name": "伯特利", "reason": "轻量化底盘+智能线控制动"},
    ],
}


def extract_event_themes(events: list[dict]) -> list[dict[str, Any]]:
    """从近 48 小时事件流中提取被强烈触发的科技与大宗主线题材。"""
    if not events:
        return []

    theme_hits: dict[str, list[str]] = {}
    for ev in events:
        content = (ev.get("content") or "") + " " + " ".join(ev.get("subjects") or [])
        if not content:
            continue
        for theme, kws in THEME_KEYWORD_RULES.items():
            for kw in kws:
                if kw.lower() in content.lower():
                    if theme not in theme_hits:
                        theme_hits[theme] = []
                    theme_hits[theme].append(content[:100])
                    break

    results = []
    for theme, matched_events in theme_hits.items():
        results.append({
            "theme": theme,
            "hit_count": len(matched_events),
            "sample_event": matched_events[0] if matched_events else "",
        })
    results.sort(key=lambda x: -x["hit_count"])
    return results[:3]  # 最多取前 3 个最具催化共振的突发主线


def resolve_theme_candidates(theme_name: str, top_n: int = 5) -> list[dict[str, Any]]:
    """根据题材名称, 从全市场中检索高辨识度主板候选标的 (优先妖股基因分、中低位、中小市值)。"""
    # 优先尝试从本地通达信网关拉取
    try:
        from src.core.tdx_boards import name_map, sector_items, constituents
        from src.core.demon_factors import top_demon_factors

        all_sectors = sector_items() or []
        matched_block_codes = []
        keywords = THEME_KEYWORD_RULES.get(theme_name, [theme_name])

        for s in all_sectors:
            s_name = s.get("name") or ""
            s_code = s.get("code") or ""
            for kw in keywords:
                if kw in s_name:
                    matched_block_codes.append((s_code, s_name))
                    break

        if matched_block_codes:
            candidate_symbols = set()
            for b_code, _ in matched_block_codes[:2]:
                stocks = constituents(b_code) or []
                for sym in stocks:
                    clean_sym = sym.split(".")[0]
                    if clean_sym.startswith(("00", "60", "30", "68")):
                        candidate_symbols.add(clean_sym)

            if candidate_symbols:
                all_names = name_map() or {}
                demon_map = top_demon_factors(100) or {}

                candidates = []
                for sym in candidate_symbols:
                    name = all_names.get(sym, sym)
                    demon_info = demon_map.get(sym) or {}
                    score = demon_info.get("score") or demon_info.get("demon_score") or 0.0
                    level = demon_info.get("level") or demon_info.get("demon_level") or "普通"

                    is_mainboard = sym.startswith(("00", "60"))
                    priority_score = score + (15.0 if is_mainboard else 0.0)

                    candidates.append({
                        "symbol": sym,
                        "name": name,
                        "theme": theme_name,
                        "is_mainboard": is_mainboard,
                        "demon_score": round(score, 1),
                        "demon_level": level,
                        "priority_score": priority_score,
                        "reason": f"命中【{theme_name}】核心成分股，历史妖股股性评分 {score}分({level})",
                    })

                candidates.sort(key=lambda x: -x["priority_score"])
                return candidates[:top_n]
    except Exception as e:
        logger.debug("动态检索板块成分股降级兜底: %s", e)

    # 兜底：使用静态高辨识度主线龙头库 + 动态妖股分补全
    static_cands = STATIC_THEME_LEADERS.get(theme_name) or []
    if not static_cands:
        return []

    try:
        from src.core.demon_factors import top_demon_factors
        demon_map = top_demon_factors(100) or {}
    except Exception:
        demon_map = {}

    out = []
    for sc in static_cands:
        sym = sc["symbol"]
        demon_info = demon_map.get(sym) or {}
        score = demon_info.get("score") or demon_info.get("demon_score") or 0.0
        level = demon_info.get("level") or demon_info.get("demon_level") or "普通"
        is_mainboard = sym.startswith(("00", "60"))
        out.append({
            "symbol": sym,
            "name": sc["name"],
            "theme": theme_name,
            "is_mainboard": is_mainboard,
            "demon_score": round(score, 1),
            "demon_level": level,
            "priority_score": score + (15.0 if is_mainboard else 0.0),
            "reason": sc["reason"],
        })

    out.sort(key=lambda x: -x["priority_score"])
    return out[:top_n]


def build_fullmarket_theme_candidates(events: list[dict]) -> list[dict[str, Any]]:
    """整合入口: 给定盘前事件流, 输出结构化的突发题材及全市场龙头候选名单。"""
    active_themes = extract_event_themes(events)
    if not active_themes:
        return []

    all_theme_candidates = []
    for t in active_themes:
        theme = t["theme"]
        cands = resolve_theme_candidates(theme, top_n=4)
        if cands:
            all_theme_candidates.append({
                "theme": theme,
                "sample_event": t["sample_event"],
                "candidates": cands,
            })

    return all_theme_candidates
