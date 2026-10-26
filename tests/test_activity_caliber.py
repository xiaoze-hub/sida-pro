# -*- coding: utf-8 -*-
"""AI 机构活跃度口径核对 + >12 更佳档 测试(2026-10-10, 规格 §3)。

钉死两件事:
  A. **因子口径结论**: 公开资料=6 指标, 我方=7 因子 —— 我方有内部依据(《数智决策 8 问 8 答》
     §2.3 + 逆向 TDX 公式 1_JGHYD), 故**保留 7 因子**并留差异注释(FACTOR_CALIBER_NOTE)。
     断言: 7 因子集合 + 差异注释存在性("6" 与 "8问8答"/"1_JGHYD" 关键字) + 内核仍是 7 因子 MAX×1.2。
  B. **>12 更佳档**: 规格 "阈值 1.56/3/6, >12 更佳" → 配置层新增 `top_line`(默认 12.0),
     `eval_activity`/`activity_of_value` 输出第五档 "更佳" + `above_top`, 支持 env 覆盖。

禁真网络: 纯计算用例直接喂合成 bar。
"""
from __future__ import annotations

from src.core import ai_activity as ai
from src.core import thresholds
from src.core.decision_pioneer import compute_institution_activity


def _bars(rows):
    return [{"open": o, "high": h, "low": l, "close": c} for o, h, l, c in rows]


# ══════════════════════════════════════════════════════════════════════
# A. 因子口径核对: 7 因子 + 差异注释
# ══════════════════════════════════════════════════════════════════════
def test_activity_factors_are_seven_and_named():
    """7 因子逐一点名(与内核一致), 顺序稳定。"""
    assert ai.ACTIVITY_FACTORS == (
        "上影", "下影", "实体+上影", "实体+下影", "上影+下影", "涨幅", "高开",
    )
    assert len(ai.ACTIVITY_FACTORS) == 7


def test_factor_caliber_note_pins_difference_and_basis():
    """差异注释存在且可机器校验: 提到公开 6 指标 + 内部 8问8答/逆向公式依据。"""
    note = ai.FACTOR_CALIBER_NOTE
    assert "6" in note              # 公开资料=6 指标
    assert "8问8答" in note         # 内部依据
    assert "1_JGHYD" in note        # 逆向 TDX 公式依据
    assert "7" in note              # 我方=7 因子


def test_kernel_still_seven_factor_max():
    """内核仍是 max(7 因子)×1.2: 用第 7 因子(全幅=上影+下影)主导的合成 bar 反向锁定。

    构造: 前日 10; 当日 开盘=收 10(实体 0, 涨幅 0, 高开 0) 但上下影各 ~5%
    → 只有"上影+下影"因子显著; 若砍成 6 因子(去全幅)结果会变小。
    """
    bars = _bars([(10, 10, 10, 10), (10, 10.5, 9.5, 10)])
    r = compute_institution_activity(bars)
    assert r is not None
    # 上影=(10.5-10)/10*100=5.0 ; 下影=(10-9.5)/9.5*100≈5.263 → 全幅=10.263
    upper, lower = 5.0, (10 - 9.5) / 9.5 * 100
    full_range = upper + lower
    other_max = max(upper, lower, 0.0)  # 若只剩单影/实体/涨幅/高开, 最大约 5.26
    assert r["activity"] == round(full_range * 1.2, 3)
    assert r["activity"] > round(other_max * 1.2, 3)


# ══════════════════════════════════════════════════════════════════════
# B. >12 更佳档(配置层 top_line + 语义层"更佳")
# ══════════════════════════════════════════════════════════════════════
def test_top_line_config_default(monkeypatch):
    monkeypatch.delenv("SIDA_THRESHOLD_TOP_LINE", raising=False)
    assert thresholds.top_line() == 12.00
    snap = thresholds.snapshot()
    assert "top_line" in snap
    assert snap["top_line"]["default"] == 12.00
    assert snap["top_line"]["label"] == "更佳线"
    assert snap["top_line"]["source"] == "default"


def test_top_line_env_override(monkeypatch):
    monkeypatch.setenv("SIDA_THRESHOLD_TOP_LINE", "9.5")
    assert thresholds.top_line() == 9.5
    assert thresholds.source("top_line") == "env"


def test_activity_of_value_top_tier(monkeypatch):
    monkeypatch.delenv("SIDA_THRESHOLD_TOP_LINE", raising=False)
    # >= 12 → 更佳(顶格)
    r = ai.activity_of_value(13.0)
    assert r["level"] == ai.LEVEL_TOP == "更佳"
    assert r["above_top"] is True
    assert r["above_bull"] is True
    # 12.0 恰在线上 → 更佳(与其他线同为 >=)
    assert ai.activity_of_value(12.0)["level"] == "更佳"
    # 11.99 未达 → 退回大牛
    r2 = ai.activity_of_value(11.99)
    assert r2["level"] == "大牛"
    assert r2["above_top"] is False
    # 缺值 → None, 不编造
    r3 = ai.activity_of_value(None)
    assert r3["level"] is None and r3["above_top"] is None


def test_activity_of_value_top_honors_env(monkeypatch):
    monkeypatch.setenv("SIDA_THRESHOLD_TOP_LINE", "9.0")
    assert ai.activity_of_value(9.5)["level"] == "更佳"
    assert ai.activity_of_value(9.5)["above_top"] is True
    assert ai.activity_of_value(8.0)["level"] == "大牛"


def test_eval_activity_top_tier_from_real_bars(monkeypatch):
    """真实合成 bar(内核活跃度 12.316) → 语义层档位=更佳 + above_top=True。"""
    monkeypatch.delenv("SIDA_THRESHOLD_TOP_LINE", raising=False)
    bars = _bars([(10, 10, 10, 10), (10, 10.5, 9.5, 10.5)])
    r = ai.eval_activity(bars)
    assert r["activity"] is not None and r["activity"] >= 12.0
    assert r["level"] == "更佳"
    assert r["above_top"] is True and r["above_bull"] is True


def test_eval_activity_no_data_has_above_top_none():
    r = ai.eval_activity([{"open": 1, "high": 1, "low": 1, "close": 1}])
    assert r["activity"] is None
    assert r["above_top"] is None and r["level"] is None
