"""
A 股短线情绪周期判别器

判断当前短线情绪所处的周期阶段:
冰点 → 修复 → 发酵 → 高潮 → 退潮 → 冰点 (循环)

纯函数模块, 无外部依赖, 全部字段可空降级。
"""

from __future__ import annotations

from typing import Any

# ── 阈值常量 — 经验值,后续可 IC 标定 ──────────────────────────────

# 冰点: 涨停少 + 连板低 + 炸板率高
FREEZING_LIMIT_UP_MAX = 30       # 涨停家数 ≤ 30
FREEZING_BOARD_HEIGHT_MAX = 3    # 最高连板 ≤ 3
FREEZING_BREAK_RATE_MIN = 40.0   # 炸板率 ≥ 40%

# 修复: 涨停回升 + 亏钱效应减弱
RECOVERY_LIMIT_UP_MIN = 25          # 涨停回升 ≥ 25
RECOVERY_LIMIT_UP_MAX = 60          # 涨停 ≤ 60
RECOVERY_BREAK_RATE_MAX = 35.0      # 炸板率 ≤ 35%
RECOVERY_LOSING_EFFECT_MAX = 0.35   # 亏钱效应 ≤ 0.35
RECOVERY_YESTERDAY_PERF_MIN = -1.5  # 昨日涨停表现 ≥ -1.5%

# 发酵: 涨停增多 + 连板抬升
FERMENT_LIMIT_UP_MIN = 50          # 涨停增多 ≥ 50
FERMENT_BOARD_HEIGHT_MIN = 4       # 连板抬升 ≥ 4
FERMENT_BREAK_RATE_MAX = 35.0      # 炸板率 ≤ 35%
FERMENT_YESTERDAY_PERF_MIN = 1.0   # 昨日涨停溢价 ≥ 1%

# 高潮: 涨停多 + 连板高 + 炸板率开始抬头
CLIMAX_LIMIT_UP_MIN = 80           # 涨停多 ≥ 80
CLIMAX_BOARD_HEIGHT_MIN = 6        # 连板高 ≥ 6
CLIMAX_BREAK_RATE_MIN = 20.0       # 炸板率开始升 ≥ 20%
CLIMAX_BREAK_RATE_MAX = 45.0       # 炸板率 ≤ 45%
CLIMAX_YESTERDAY_PERF_MIN = 2.0    # 溢价 ≥ 2%

# 退潮: 炸板率高 + 高标炸板 + 亏钱效应扩散
EBB_BREAK_RATE_MIN = 35.0          # 炸板率高 ≥ 35%
EBB_BOARD_HEIGHT_MAX = 5           # 连板降低 ≤ 5
EBB_YESTERDAY_PERF_MAX = -1.0      # 昨日涨停大面积亏钱 ≤ -1%
EBB_LOSING_EFFECT_MIN = 0.4        # 亏钱效应 ≥ 0.4

# ── 周期操作提示 ─────────────────────────────────────────────────

_CYCLE_HINTS: dict[str, list[str]] = {
    '冰点': [
        '空仓防守, 不抄底',
        '等待右侧信号出现',
        '关注首板试错, 轻仓为主',
    ],
    '修复': [
        '小仓试错, 关注首板和一进二',
        '观察题材持续性',
        '亏钱效应减弱可适当加仓',
    ],
    '发酵': [
        '积极做多, 聚焦核心题材',
        '龙头分歧转一致时加仓',
        '连板梯队完整可持股待涨',
    ],
    '高潮': [
        '持仓格局, 不盲目追高',
        '准备逐步减仓锁定利润',
        '关注炸板率变化, 警惕退潮信号',
    ],
    '退潮': [
        '减仓防守, 高标止盈',
        '不开新仓, 回避高位接力',
        '等待冰点确认后右侧机会',
    ],
}

# ── 周期评分权重 ─────────────────────────────────────────────────

# 每个指标命中时加分, 对应 cycle 的置信度
# 冰点
_FREEZING_RULES = [
    ('limit_up_count', lambda v: v is not None and v <= FREEZING_LIMIT_UP_MAX, 3,
     lambda v: f'涨停仅{v}家'),
    ('max_board_height', lambda v: v is not None and v <= FREEZING_BOARD_HEIGHT_MAX, 2,
     lambda v: f'最高连板仅{v}板'),
    ('break_rate', lambda v: v is not None and v >= FREEZING_BREAK_RATE_MIN, 2,
     lambda v: f'炸板率{v:.1f}%偏高'),
    ('yesterday_board_perf', lambda v: v is not None and v <= -2.0, 1,
     lambda v: '昨日涨停大面积亏损'),
    ('losing_effect', lambda v: v is not None and v >= 0.5, 1,
     lambda v: '亏钱效应显著'),
]

# 修复
_RECOVERY_RULES = [
    ('limit_up_count',
     lambda v: v is not None and RECOVERY_LIMIT_UP_MIN <= v <= RECOVERY_LIMIT_UP_MAX, 2,
     lambda v: f'涨停{v}家回升'),
    ('break_rate', lambda v: v is not None and v <= RECOVERY_BREAK_RATE_MAX, 2,
     lambda v: f'炸板率{v:.1f}%可控'),
    ('losing_effect', lambda v: v is not None and v <= RECOVERY_LOSING_EFFECT_MAX, 2,
     lambda _: '亏钱效应减弱'),
    ('yesterday_board_perf', lambda v: v is not None and v >= RECOVERY_YESTERDAY_PERF_MIN, 1,
     lambda _: '亏钱不再扩散'),
]

# 发酵
_FERMENT_RULES = [
    ('limit_up_count', lambda v: v is not None and v >= FERMENT_LIMIT_UP_MIN, 2,
     lambda v: f'涨停{v}家增多'),
    ('max_board_height', lambda v: v is not None and v >= FERMENT_BOARD_HEIGHT_MIN, 2,
     lambda v: f'连板高度{v}板抬升'),
    ('break_rate', lambda v: v is not None and v <= FERMENT_BREAK_RATE_MAX, 1,
     lambda _: '炸板率健康'),
    ('yesterday_board_perf', lambda v: v is not None and v >= FERMENT_YESTERDAY_PERF_MIN, 2,
     lambda _: '昨日涨停溢价为正'),
]

# 高潮
_CLIMAX_RULES = [
    ('limit_up_count', lambda v: v is not None and v >= CLIMAX_LIMIT_UP_MIN, 3,
     lambda v: f'涨停{v}家活跃'),
    ('max_board_height', lambda v: v is not None and v >= CLIMAX_BOARD_HEIGHT_MIN, 2,
     lambda v: f'连板高度{v}板'),
    ('break_rate',
     lambda v: v is not None and CLIMAX_BREAK_RATE_MIN <= v <= CLIMAX_BREAK_RATE_MAX, 1,
     lambda v: f'炸板率{v:.1f}%开始抬头'),
    ('yesterday_board_perf', lambda v: v is not None and v >= CLIMAX_YESTERDAY_PERF_MIN, 2,
     lambda _: '溢价充足'),
]

# 退潮
_EBB_RULES = [
    ('break_rate', lambda v: v is not None and v >= EBB_BREAK_RATE_MIN, 3,
     lambda v: f'炸板率{v:.1f}%偏高'),
    ('max_board_height', lambda v: v is not None and v <= EBB_BOARD_HEIGHT_MAX, 1,
     lambda _: '连板高度降低'),
    ('yesterday_board_perf', lambda v: v is not None and v <= EBB_YESTERDAY_PERF_MAX, 2,
     lambda _: '昨日涨停大面积亏钱'),
    ('losing_effect', lambda v: v is not None and v >= EBB_LOSING_EFFECT_MIN, 2,
     lambda _: '亏钱效应扩散'),
]

# 周期名称与规则映射
_CYCLE_CONFIGS: list[tuple[str, list[tuple[str, Any, int, Any]], int]] = [
    ('冰点', _FREEZING_RULES, 9),
    ('修复', _RECOVERY_RULES, 7),
    ('发酵', _FERMENT_RULES, 7),
    ('高潮', _CLIMAX_RULES, 8),
    ('退潮', _EBB_RULES, 8),
]


def _score_cycle(
    metrics: dict,
    rules: list,
) -> tuple[int, list[str]]:
    """对一个周期打分, 返回 (分数, 证据列表)."""
    score = 0
    evidence: list[str] = []
    for field, condition, weight, fmt in rules:
        val = metrics.get(field)
        if condition(val):
            score += weight
            evidence.append(fmt(val))
    return score, evidence


def classify_sentiment_cycle(metrics: dict) -> dict:
    """
    判断短线情绪周期。

    Parameters
    ----------
    metrics : dict
        结构化指标字典，字段全部可空 (None 表示缺失):
        - limit_up_count (int|None): 涨停家数
        - max_board_height (int|None): 最高连板数
        - break_rate (float|None): 炸板率 %
        - yesterday_board_perf (float|None): 昨日涨停今日平均表现 %
        - losing_effect (float|None): 亏钱效应 0-1

    Returns
    -------
    dict
        {
            'cycle': str,        # 冰点/修复/发酵/高潮/退潮/数据不足
            'confidence': str,   # 低/中/高
            'evidence': str,     # 中文一句话证据
            'hints': list[str],  # 操作提示
        }
    """
    # 核心字段缺失检查 (涨停数 / 连板 / 炸板率三个核心)
    core_fields = [metrics.get('limit_up_count'),
                   metrics.get('max_board_height'),
                   metrics.get('break_rate')]
    core_missing = sum(1 for x in core_fields if x is None)
    if core_missing >= 3:
        return {
            'cycle': '数据不足',
            'confidence': '低',
            'evidence': '情绪指标缺失',
            'hints': [],
        }

    # 对每个周期打分
    best_cycle = '数据不足'
    best_score = 0
    best_evidence: list[str] = []

    for cycle_name, rules, _ in _CYCLE_CONFIGS:
        score, evidence = _score_cycle(metrics, rules)
        if score > best_score:
            best_score = score
            best_cycle = cycle_name
            best_evidence = evidence

    if best_score == 0:
        return {
            'cycle': '数据不足',
            'confidence': '低',
            'evidence': '多项指标缺失，无法判断周期',
            'hints': [],
        }

    # 置信度: 根据得分占「命中周期」满分比例(不是全局最大, 否则满分只有 7 的
    # 修复/发酵周期即使拿满也到不了 0.8 的"高"门槛)。
    best_max = next((cfg[2] for cfg in _CYCLE_CONFIGS if cfg[0] == best_cycle), 9)
    ratio = best_score / best_max if best_max > 0 else 0.0
    if ratio >= 0.8:
        confidence = '高'
    elif ratio >= 0.5:
        confidence = '中'
    else:
        confidence = '低'

    return {
        'cycle': best_cycle,
        'confidence': confidence,
        'evidence': '；'.join(best_evidence) if best_evidence else '无明显信号',
        'hints': _CYCLE_HINTS.get(best_cycle, []),
    }


def format_cycle(result: dict) -> str:
    """
    将 classify_sentiment_cycle 的结果格式化为可读文本。

    Parameters
    ----------
    result : dict
        classify_sentiment_cycle 的返回值。

    Returns
    -------
    str
        格式化后的文本（供 AI 助手 / 报告使用）。
    """
    cycle = result.get('cycle', '?')
    confidence = result.get('confidence', '?')
    evidence = result.get('evidence', '')
    hints = result.get('hints', [])

    lines = [
        f'📊 短线情绪周期: {cycle}',
        f'📈 置信度: {confidence}',
        f'📝 依据: {evidence}',
    ]
    if hints:
        lines.append('')
        lines.append('💡 操作提示:')
        for i, h in enumerate(hints, 1):
            lines.append(f'  {i}. {h}')

    return '\n'.join(lines)