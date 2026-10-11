"""决策阈值统一配置层(数智决策 P1-3, 2026-10-10)。

审计 P1-3: 决策阈值(AI 机构活跃度的生命线/强势线/大牛线, 以及共振判定用的强势线)
原先分散硬编码在 `resonance_scan` / `ai_activity` / `resonance` 三处, 任何调整都要改代码
发版。本模块把它们收成**唯一只读配置层**:

优先级: 环境变量(`SIDA_THRESHOLD_*`) > 内置默认值。

**语义零变化**: 未设 env 时逐比特等于历史硬编码值(1.56 / 3.00 / 6.00)。
仅当显式设了 env 且值为合法数时才改变判定阈值。

安全口径(**绝不 crash**): env 缺失用默认; 值非数 / 非有限(inf/nan) / 越界 → **回默认
并 warn**, 不让一条坏配置把决策链路打挂。未知键视为编程错误, 直接抛 KeyError。

只读快照 `snapshot()` 供 API/UI 展示"当前生效值 + 来源(default/env)"; 本层**不提供写回**
(运行时改阈值需要权限设计, 本轮只做可配 + 可见)。
"""

from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)

# 阈值规格: 键 → {default 默认值, env 环境变量名, lo/hi 合法域(左开右闭), label 中文名}
# 默认值 = 历史硬编码现值(逐比特一致), 见审计 P1-3:
#   ai_activity.LIFE_LINE=1.56 / STRONG_LINE=3.00 / BULL_LINE=6.00
#   resonance.STRONG_LINE=3.00, resonance_scan._STRONG_LINE=3.0
#   top_line=12.00(2026-10-10 规格 §3 ">12 更佳", 大牛线之上的第五档)
_SPECS: dict[str, dict] = {
    "life_line": {
        "default": 1.56,
        "env": "SIDA_THRESHOLD_LIFE_LINE",
        "lo": 0.0,
        "hi": 1000.0,
        "label": "生命线",
    },
    "strong_line": {
        "default": 3.00,
        "env": "SIDA_THRESHOLD_STRONG_LINE",
        "lo": 0.0,
        "hi": 1000.0,
        "label": "强势线",
    },
    "bull_line": {
        "default": 6.00,
        "env": "SIDA_THRESHOLD_BULL_LINE",
        "lo": 0.0,
        "hi": 1000.0,
        "label": "大牛线",
    },
    # 更佳线(2026-10-10 规格 §3: AI 机构活跃度\"阈值 1.56生命线/3强势线/6大牛线, >12更佳\")。
    # 大牛线之上的第五档: 活跃度 >= 12 = \"更佳\"(顶格)。默认 12.0, env 可覆盖。
    "top_line": {
        "default": 12.00,
        "env": "SIDA_THRESHOLD_TOP_LINE",
        "lo": 0.0,
        "hi": 1000.0,
        "label": "更佳线",
    },
    # ── 决策先锋辅助指标参数(P3 补差, 2026-10-10) ──────────────────────────
    # 规格 §5/§6/§7; 官方精确参数未公开 → 均为**逆向近似默认值, 待截图/逆向校准**。
    # 周期类按整数存浮点, 消费方 int() 取整。
    "trend_pilot_red_period": {
        "default": 10.0,
        "env": "SIDA_THRESHOLD_TREND_PILOT_RED_PERIOD",
        "lo": 0.0,
        "hi": 250.0,
        "label": "操盘线-红线(快)周期",
    },
    "trend_pilot_yellow_period": {
        "default": 20.0,
        "env": "SIDA_THRESHOLD_TREND_PILOT_YELLOW_PERIOD",
        "lo": 0.0,
        "hi": 250.0,
        "label": "操盘线-黄线(慢)周期",
    },
    "trend_pilot_green_period": {
        "default": 60.0,
        "env": "SIDA_THRESHOLD_TREND_PILOT_GREEN_PERIOD",
        "lo": 0.0,
        "hi": 250.0,
        "label": "操盘线-绿线周期",
    },
    "trend_pilot_band_tol_pct": {
        "default": 1.0,
        "env": "SIDA_THRESHOLD_TREND_PILOT_BAND_TOL_PCT",
        "lo": 0.0,
        "hi": 20.0,
        "label": "操盘线-回踩容差%",
    },
    "niuxiong_bull_period": {
        "default": 20.0,
        "env": "SIDA_THRESHOLD_NIUXIONG_BULL_PERIOD",
        "lo": 0.0,
        "hi": 250.0,
        "label": "牛熊线-牛线(加权)周期",
    },
    "niuxiong_horse_period": {
        "default": 5.0,
        "env": "SIDA_THRESHOLD_NIUXIONG_HORSE_PERIOD",
        "lo": 0.0,
        "hi": 250.0,
        "label": "牛熊线-马线周期",
    },
    "niuxiong_trade_period": {
        "default": 30.0,
        "env": "SIDA_THRESHOLD_NIUXIONG_TRADE_PERIOD",
        "lo": 0.0,
        "hi": 250.0,
        "label": "牛熊线-买卖线周期",
    },
    "minute_consolidation_min": {
        "default": 15.0,
        "env": "SIDA_THRESHOLD_MINUTE_CONSOLIDATION_MIN",
        "lo": 0.0,
        "hi": 240.0,
        "label": "分时突破-盘整最少分钟",
    },
    "minute_consolidation_amp_pct": {
        "default": 0.6,
        "env": "SIDA_THRESHOLD_MINUTE_CONSOLIDATION_AMP_PCT",
        "lo": 0.0,
        "hi": 20.0,
        "label": "分时突破-盘整振幅上限%",
    },
    "minute_volume_spike_mult": {
        "default": 2.0,
        "env": "SIDA_THRESHOLD_MINUTE_VOLUME_SPIKE_MULT",
        "lo": 0.0,
        "hi": 50.0,
        "label": "分时突破-放量倍数",
    },
    "minute_dde_consecutive_min": {
        "default": 3.0,
        "env": "SIDA_THRESHOLD_MINUTE_DDE_CONSECUTIVE_MIN",
        "lo": 0.0,
        "hi": 60.0,
        "label": "分时突破-DDE连续流入分钟",
    },
    "minute_dde_min_wan": {
        "default": 50.0,
        "env": "SIDA_THRESHOLD_MINUTE_DDE_MIN_WAN",
        "lo": 0.0,
        "hi": 1_000_000.0,
        "label": "分时突破-DDE流入下限(万元)",
    },
    "minute_early_end_minutes": {
        "default": 60.0,
        "env": "SIDA_THRESHOLD_MINUTE_EARLY_END_MINUTES",
        "lo": 0.0,
        "hi": 240.0,
        "label": "分时突破-早盘窗口(开盘后分钟)",
    },
    "minute_early_inflow_ratio": {
        "default": 0.8,
        "env": "SIDA_THRESHOLD_MINUTE_EARLY_INFLOW_RATIO",
        "lo": 0.0,
        "hi": 1.0,
        "label": "分时突破-早盘正流入占比",
    },
    "minute_breakout_tol_pct": {
        "default": 0.2,
        "env": "SIDA_THRESHOLD_MINUTE_BREAKOUT_TOL_PCT",
        "lo": 0.0,
        "hi": 20.0,
        "label": "分时突破-突破容差%",
    },
    # 盘中 DDE 大单采样节拍(P3 补差 A, 2026-10-10)。采样范围=全市场, 值越小序列越密、
    # 数据源压力越大; 默认 5 分钟(与 1m 分时K 非逐分钟对齐 → 序列按样本判定"持续流入")。
    "minute_dde_sample_min": {
        "default": 5.0,
        "env": "SIDA_THRESHOLD_MINUTE_DDE_SAMPLE_MIN",
        "lo": 0.0,
        "hi": 60.0,
        "label": "分时突破-DDE采样节拍(分钟)",
    },
    # ── 情绪温度计条件化阈值(2026-10-11 胜率证据层 ①) ──────────────────────────
    # 供 `src.core.market_regime` 的『动手』门槛做**温度条件化**:
    #   封板成功率 < seal_rate_tighten → 收紧;  >= seal_rate_loosen → 放宽(冷热两侧);
    #   炸板比     >= blow_rate_tighten → 收紧(分歧加剧)。
    # 默认值为**启发式初值(占比 0-1)**, 待 5294 样本分桶验证后按需 env 覆盖
    # (docs/research/情绪温度计条件化与胜率证据_20261011.md)。
    "seal_rate_tighten": {
        "default": 0.50,
        "env": "SIDA_THRESHOLD_SEAL_RATE_TIGHTEN",
        "lo": 0.0,
        "hi": 1.0,
        "label": "封板成功率-收紧线",
    },
    "seal_rate_loosen": {
        "default": 0.70,
        "env": "SIDA_THRESHOLD_SEAL_RATE_LOOSEN",
        "lo": 0.0,
        "hi": 1.0,
        "label": "封板成功率-放宽线",
    },
    "blow_rate_tighten": {
        "default": 0.45,
        "env": "SIDA_THRESHOLD_BLOW_RATE_TIGHTEN",
        "lo": 0.0,
        "hi": 1.0,
        "label": "炸板比-收紧线",
    },
    # ── 首封时间 × 封成比 强度因子阈值(2026-10-11 胜率创新 P0-1, 影子运行) ──────────
    # 首封时间分桶边界(秒, 自 00:00 起算): 竞价 ≤ auc_end; 早盘 ≤ early_end;
    # 盘中 ≤ mid_end; 其余=尾盘。默认 09:30:00 / 10:00:00 / 14:30:00。
    # 供 src.core.limit_up_strength 消费; 纯展示/统计, 不改任何现有信号或决策。
    "first_seal_auc_end": {
        "default": 34200.0,  # 09:30:00
        "env": "SIDA_THRESHOLD_FIRST_SEAL_AUC_END",
        "lo": 0.0,
        "hi": 86400.0,
        "label": "首封-竞价桶上界(秒)",
    },
    "first_seal_early_end": {
        "default": 36000.0,  # 10:00:00
        "env": "SIDA_THRESHOLD_FIRST_SEAL_EARLY_END",
        "lo": 0.0,
        "hi": 86400.0,
        "label": "首封-早盘桶上界(秒)",
    },
    "first_seal_mid_end": {
        "default": 52200.0,  # 14:30:00
        "env": "SIDA_THRESHOLD_FIRST_SEAL_MID_END",
        "lo": 0.0,
        "hi": 86400.0,
        "label": "首封-盘中桶上界(秒)",
    },
    # 封成比(=封单额/流通市值)分档线: <b → C; [b,a) → B; [a,s) → A; >=s → S。
    "seal_ratio_b": {
        "default": 0.01,
        "env": "SIDA_THRESHOLD_SEAL_RATIO_B",
        "lo": 0.0,
        "hi": 10.0,
        "label": "封成比-B档线",
    },
    "seal_ratio_a": {
        "default": 0.03,
        "env": "SIDA_THRESHOLD_SEAL_RATIO_A",
        "lo": 0.0,
        "hi": 10.0,
        "label": "封成比-A档线",
    },
    "seal_ratio_s": {
        "default": 0.05,
        "env": "SIDA_THRESHOLD_SEAL_RATIO_S",
        "lo": 0.0,
        "hi": 10.0,
        "label": "封成比-S档线",
    },
    # 炸板次数降档阈值: OpenTimesZT >= 该值 → 强度标签降一档(炸板=封板质量受损)。
    "blow_downgrade_open_times": {
        "default": 1.0,
        "env": "SIDA_THRESHOLD_BLOW_DOWNGRADE_OPEN_TIMES",
        "lo": 0.0,
        "hi": 100.0,
        "label": "炸板降档-次数阈值",
    },
}

# 对外暴露的阈值键(顺序稳定, 供 API/UI 展示)
KEYS: tuple[str, ...] = tuple(_SPECS.keys())


def _read(name: str) -> tuple[float, str]:
    """读取单阈值 → (生效值, 来源 'default' | 'env')。未知键抛 KeyError。"""
    spec = _SPECS[name]
    default: float = spec["default"]
    env_name: str = spec["env"]
    raw = os.environ.get(env_name)
    if raw is None or raw.strip() == "":
        return default, "default"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "决策阈值 %s: 环境变量 %s=%r 非数值, 回默认 %s", name, env_name, raw, default
        )
        return default, "default"
    if not (math.isfinite(value) and spec["lo"] < value <= spec["hi"]):
        logger.warning(
            "决策阈值 %s: 环境变量 %s=%r 越界(合法域 (%s, %s]), 回默认 %s",
            name, env_name, raw, spec["lo"], spec["hi"], default,
        )
        return default, "default"
    return value, "env"


def value(name: str) -> float:
    """生效阈值(env 覆盖优先; 缺失/非法一律回默认 + warn)。"""
    return _read(name)[0]


def source(name: str) -> str:
    """生效来源: 'default' | 'env'。"""
    return _read(name)[1]


def life_line() -> float:
    return value("life_line")


def strong_line() -> float:
    return value("strong_line")


def bull_line() -> float:
    return value("bull_line")


def top_line() -> float:
    """更佳线(活跃度 >= 12 = "更佳", 规格 §3 ">12 更佳")。"""
    return value("top_line")


def snapshot() -> dict:
    """只读快照: {key: {value, source, default, env, label}}。

    供 API/UI 展示"当前生效值 + 来源"; 不含写入口。
    """
    out: dict[str, dict] = {}
    for name, spec in _SPECS.items():
        v, src = _read(name)
        out[name] = {
            "value": v,
            "source": src,
            "default": spec["default"],
            "env": spec["env"],
            "label": spec["label"],
        }
    return out
