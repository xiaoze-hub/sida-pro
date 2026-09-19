"""G/S 颜色验收线的**判据本身**要能拦住互换(2026-09-19 用户要求做成可测)。

产品侧的规则在 `packages/biz-ui/src/lib/stock-colors.ts`(前端 vitest 钉住), 这里钉**巡检脚本的判定**:
色相区间、互换必红、量不到不判(不猜)。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("terminal_audit", ROOT / "scripts" / "terminal_audit.py")
ta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ta)


def test_correct_colors_pass():
    assert ta.gs_color_violation("#E53935", "#43A047") is None


def test_swapped_colors_are_flagged():
    """红绿互换是这条验收线的**唯一目的** —— 必须红。"""
    msg = ta.gs_color_violation("#43A047", "#E53935")
    assert msg and "GS 颜色不符" in msg
    assert "G 应红系" in msg and "S 应绿系" in msg


def test_same_color_both_sides_flagged():
    assert ta.gs_color_violation("#E53935", "#E53935") is not None


def test_hue_family_boundaries():
    # 红系: hue<=25 或 >=330; 绿系: 90<hue<170
    assert ta.gs_color_violation("#FF0000", "#00FF00") is None        # 纯红/纯绿
    assert ta.gs_color_violation("#E91E63", "#00C853") is None        # 偏粉的红 / 偏深的绿 也算过
    assert ta.gs_color_violation("#FFC107", "#43A047") is not None    # 琥珀不是红
    assert ta.gs_color_violation("#E53935", "#2196F3") is not None    # 蓝不是绿


def test_unmeasurable_is_not_judged():
    """量不到(元素不存在/颜色解析失败)→ **不判**(返回 None), 不猜也不误报。"""
    assert ta.gs_color_violation(None, None) is None
    assert ta.gs_color_violation("#E53935", None) is None
    assert ta.gs_color_violation("rgb(229,57,53)", "#43A047") is None   # 非 hex 解析不了 → 不判


def test_hue_parsing_shorthand_hex():
    assert ta._hue("#f00") == 0
    assert ta._hue("#E53935") is not None and ta._hue("#E53935") < 25
