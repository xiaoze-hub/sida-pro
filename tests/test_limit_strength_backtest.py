# -*- coding: utf-8 -*-
"""首封×封成比 历史回测脚本 纯逻辑测试(2026-10-11 P0-1)。只测纯函数, 不连库/不联网。"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# scripts/ 不是包 → 用文件方式加载
_spec = importlib.util.spec_from_file_location(
    "limit_strength_backtest", str(ROOT / "scripts" / "limit_strength_backtest.py")
)
assert _spec is not None and _spec.loader is not None
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)


def test_wilson_ci_bounds():
    lo, hi = bt.wilson_ci(0, 0)
    assert (lo, hi) == (0.0, 0.0)
    lo, hi = bt.wilson_ci(50, 100)
    assert lo < 50 < hi and 0 <= lo and hi <= 100
    # 命中率 0 时下界 0
    lo0, hi0 = bt.wilson_ci(0, 30)
    assert lo0 == 0.0 and hi0 > 0


def test_label_of_reuses_core_caliber():
    ok = bt.label_of({"first_time": "09:26:00", "max_seal_amount": 1e8, "circ_mv": 2e9,
                      "open_count": 0})
    assert ok["status"] == "ok" and ok["label"] == "S"
    nd = bt.label_of({"first_time": None, "max_seal_amount": None, "circ_mv": None})
    assert nd["status"] == "no_data" and nd["label"] is None


def test_forward_close_skips_gaps():
    cs = {"20260901": 10.0, "20260903": 11.0, "20260904": 12.0}
    # 无 0902 → T+1 顺延到 0903
    assert bt._forward_close(cs, "20260901", 1) == 11.0
    assert bt._forward_close(cs, "20260901", 2) == 12.0
    assert bt._forward_close(cs, "20260904", 1) is None
