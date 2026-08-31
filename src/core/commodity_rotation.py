"""大宗商品轮动前瞻(2026-08-10 同花顺学习文《大宗商品的轮动顺序》)。

轮动剧本: 能源冲锋(石油/煤炭) → 金属狂潮(铜/铝/钢铁) → 农产压轴(粮食/棉花/大豆) → 黄金返场(避险)

用途: 盘前事件驱动联动——输入当日商品/板块异动信号,输出轮动阶段 + 下一幕关注题材。
仅作研究参考,不构成投资建议。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 四大轮动阶段
STAGE_ENERGY = "能源冲锋"
STAGE_METAL = "金属狂潮"
STAGE_AGRI = "农产压轴"
STAGE_GOLD = "黄金返场"

# 各阶段关联的 A 股板块/题材
STAGE_SECTORS = {
    STAGE_ENERGY: ["石油", "煤炭", "天然气", "油服"],
    STAGE_METAL: ["有色金属", "铜", "铝", "钢铁", "稀土"],
    STAGE_AGRI: ["农业", "种业", "棉花", "大豆", "油脂", "糖"],
    STAGE_GOLD: ["黄金", "贵金属", "军工", "高股息"],
}

# 地缘冲突阶段(2026-08-10 学习文《以伊开战五波冲击》,优先级高于普通轮动)
STAGE_CONFLICT = "地缘冲突"
STAGE_SECTORS[STAGE_CONFLICT] = ["石油", "油气", "黄金", "军工", "国防"]

# 冲突关键词(命中即认为进入地缘冲突模式)
CONFLICT_KEYWORDS = [
    "开战", "战争", "冲突", "袭击", "导弹", "轰炸", "制裁", "战火",
    "军事行动", "地缘", "中东", "以伊", "伊朗", "以色列", "俄乌", "巴以",
]

# 避险板块同现检测(2026-08-10 题材启动接入): 石油+军工+黄金 同时活跃 = 冲突信号
CONFLICT_SECTOR_KWS = ["石油", "油气", "军工", "国防", "黄金", "避险", "原油"]

# 商品关键词 → 所属阶段
COMMODITY_KEYWORDS = {
    "原油": STAGE_ENERGY, "石油": STAGE_ENERGY, "煤炭": STAGE_ENERGY, "天然气": STAGE_ENERGY,
    "铜": STAGE_METAL, "铝": STAGE_METAL, "钢铁": STAGE_METAL, "螺纹钢": STAGE_METAL, "稀土": STAGE_METAL,
    "粮食": STAGE_AGRI, "棉花": STAGE_AGRI, "大豆": STAGE_AGRI, "玉米": STAGE_AGRI, "糖": STAGE_AGRI,
    "黄金": STAGE_GOLD, "白银": STAGE_GOLD, "金价": STAGE_GOLD,
}

# 轮动顺序(用于"下一幕"推演)
_ROTATION_ORDER = [STAGE_ENERGY, STAGE_METAL, STAGE_AGRI, STAGE_GOLD]


def _next_stage(stage: str) -> str:
    try:
        idx = _ROTATION_ORDER.index(stage)
        return _ROTATION_ORDER[(idx + 1) % len(_ROTATION_ORDER)]
    except ValueError:
        return STAGE_METAL


def detect_rotation_stage(events: list[str]) -> dict:
    """根据盘前事件流(涨价/异动关键词)判断当前轮动阶段。

    events: 如 ["原油期货大涨3%", "铜价创年内新高", ...]
    返回: {stage, sectors, next_stage, next_sectors, hints}
    """
    if not events:
        return {
            "stage": "未检测到商品异动",
            "sectors": [],
            "next_stage": STAGE_METAL,
            "next_sectors": STAGE_SECTORS[STAGE_METAL],
            "hints": [],
        }

    # 地缘冲突优先(战争冲击可打断正常轮动,直接进入避险/能源模式)
    for ev in events:
        for kw in CONFLICT_KEYWORDS:
            if kw in ev:
                return {
                    "stage": STAGE_CONFLICT,
                    "sectors": STAGE_SECTORS[STAGE_CONFLICT],
                    "next_stage": STAGE_GOLD,
                    "next_sectors": STAGE_SECTORS[STAGE_GOLD],
                    "hints": [
                        "⚠️ 地缘冲突爆发 → 五波传导: 能源(油气/油服)→大宗(有色/化工)→通胀→货币→避险(黄金/军工)",
                        "冲突当天能源+避险先动,持续性看供给是否真中断",
                        "与商品轮动联动: 冲突优先级高于轮动剧本,防御思维为主",
                    ],
                    "conflict": True,
                }

    # 避险板块同现(2026-08-10): 石油/军工/黄金 等 ≥2 个不同避险板块同时活跃 = 冲突信号
    hit_sectors = set()
    for ev in events:
        for kw in CONFLICT_SECTOR_KWS:
            if kw in ev:
                hit_sectors.add(kw)
    if len(hit_sectors) >= 2:
        return {
            "stage": STAGE_CONFLICT,
            "sectors": STAGE_SECTORS[STAGE_CONFLICT],
            "next_stage": STAGE_GOLD,
            "next_sectors": STAGE_SECTORS[STAGE_GOLD],
            "hints": [
                f"⚠️ 避险板块同现({len(hit_sectors)}个: {'/'.join(sorted(hit_sectors))}) → 地缘冲突模式",
                "石油+军工+黄金同时活跃 = 市场避险情绪,题材方向转向防御",
            ],
            "conflict": True,
        }

    # 统计各阶段命中次数
    stage_hits: dict[str, int] = {}
    for ev in events:
        for kw, stage in COMMODITY_KEYWORDS.items():
            if kw in ev:
                stage_hits[stage] = stage_hits.get(stage, 0) + 1
                break

    if not stage_hits:
        return {
            "stage": "未检测到商品异动",
            "sectors": [],
            "next_stage": STAGE_METAL,
            "next_sectors": STAGE_SECTORS[STAGE_METAL],
            "hints": [],
        }

    # 最高命中阶段 = 当前阶段
    stage = max(stage_hits.items(), key=lambda kv: kv[1])[0]
    next_stage = _next_stage(stage)

    hints = []
    if stage == STAGE_ENERGY:
        hints.append("能源在涨 → 下一幕关注工业金属(铜/铝/钢铁),联动有色/钢铁/基建题材")
    elif stage == STAGE_METAL:
        hints.append("金属在涨 → 轮动进入中后段,关注农产品补涨(粮食/棉花/大豆/种业)")
    elif stage == STAGE_AGRI:
        hints.append("农产品也涨 → 轮动近尾声,注意黄金/避险启动,防御思维")
    elif stage == STAGE_GOLD:
        hints.append("黄金启动 → 不确定性上升,避险为主,警惕大盘回调风险")

    return {
        "stage": stage,
        "stage_hits": stage_hits,
        "sectors": STAGE_SECTORS[stage],
        "next_stage": next_stage,
        "next_sectors": STAGE_SECTORS[next_stage],
        "hints": hints,
    }


def format_rotation(rotation: dict) -> str:
    """格式化轮动判断(供盘前报告/AI 助手)。"""
    stage = rotation.get("stage", "")
    if "未检测" in stage:
        return "今日未检测到大宗商品明显异动,轮动阶段不明。"
    lines = [f"【大宗商品轮动】当前阶段: {stage}"]
    secs = rotation.get("sectors") or []
    if secs:
        lines.append(f"关联板块: {'/'.join(secs)}")
    nxt = rotation.get("next_stage", "")
    nxt_secs = rotation.get("next_sectors") or []
    if nxt:
        lines.append(f"下一幕预判: {nxt}(关注 {'/'.join(nxt_secs)})")
    for h in rotation.get("hints") or []:
        lines.append(f"⚠️ {h}")
    return "\n".join(lines)
