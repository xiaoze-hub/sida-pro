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
