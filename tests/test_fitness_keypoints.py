"""关键位兑现（F3）单测 —— 延迟真值层的口径必须钉死（2026-10-23）。

守的是三件事：
1. 关键位只从「阻力/支撑」标签后取数，没有就 None（不许拿裸数字当关键位）；
2. forward 行情没走完时 `available=False` 且原因明确 —— 这是常态，**不是未命中**；
3. 样本不够时 aggregate 不出现任何 `*_rate` 键（0% 与「不知道」必须能区分）。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from src.core import fitness_keypoints as fk
from _support_fake_db import FakeTraceDB  # tests/ 由 pytest 插入 sys.path（无 __init__.py）

SCEN_TEXT = (
    "[数据源: 均线 + CYQ 筹码带 + 区间高低 + 量能/因子]\n"
    "关键位：阻力 35.80（MA20/筹码带上沿/区间高取最近）｜支撑 32.80（MA60/筹码带下沿/区间低取最近）\n"
    "位置参考：日内区间位置 62%｜20 日相对强弱 +1.20pp｜20 日动量 +2.10%｜量比 1.3\n"
    "【走强】触发：放量站上 35.80｜目标：37.00｜应对：跟随\n"
)


def _run(run_id="run-1", created_at=datetime(2026, 10, 20, 6, 0), recipe="deep_diagnosis",
         tool_calls=5, status="ok", question="002050 怎么样", error=None):
    return SimpleNamespace(
        run_id=run_id, created_at=created_at, recipe=recipe, tool_calls=tool_calls,
        status=status, question=question, error=error, elapsed_ms=4000,
        conversation_id=1, user_id="u1", finished_at=None,
    )


def _step(run_id="run-1", seq=1, tool=fk.SCENARIO_TOOL, args='{"symbol": "002050"}',
          ok=True, digest=SCEN_TEXT, error=None, elapsed_ms=1200):
    return SimpleNamespace(run_id=run_id, seq=seq, tool=tool, args=args, ok=ok,
                           result_digest=digest, error=error, elapsed_ms=elapsed_ms)


def _bars(rows):
    return [{"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0}
            for d, o, h, lo, c in rows]


# ---------------------------------------------------------------- extract_levels

EXTRACT_CASES = [
    # (输入, 期望)
    ("关键位：阻力 35.80（MA20）｜支撑 32.80（筹码带下沿）", {"resist": 35.8, "support": 32.8}),
    ("关键位: 阻力 35.8 ｜ 支撑 32.8", {"resist": 35.8, "support": 32.8}),
    ("关键位：阻力：35.80（MA20/区间高取最近）｜支撑:32.80", {"resist": 35.8, "support": 32.8}),
    # 全角数字与全角冒号（机器人/前端有可能二次转码）
    ("关键位：阻力　３５．８０｜支撑　３２．８０", {"resist": 35.8, "support": 32.8}),
    # 截断文本：只剩阻力也要照实返回，另一端 None（不猜）
    ("关键位：阻力 41.20（MA20", {"resist": 41.2, "support": None}),
    # 只有支撑
    ("支撑 18.05（区间低）", {"resist": None, "support": 18.05}),
    # 拒绝：没有标签
    ("[情景矩阵] 关键位不足（缺均线/筹码带/区间高低）——不硬编情景。", None),
    ("位置参考：日内区间位置 62%｜量比 1.3｜20 日动量 +2.10%", None),
    ("", None),
    ("阻力位在均线上方", None),
]


@pytest.mark.parametrize("text,expected", EXTRACT_CASES)
def test_extract_levels_table(text, expected):
    assert fk.extract_levels(text) == expected


def test_extract_levels_never_guesses_from_bare_numbers():
    """矩阵文本里裸数字遍地都是 —— 没有「阻力/支撑」标签就不许产关键位。"""
    assert fk.extract_levels("均线 35.8 / 33.2 / 31.9，量比 1.3，62 分位") is None


# ------------------------------------------------------------------- score_run


def _patch_bars(monkeypatch, bars):
    calls = {}

    def _fake(symbol, market, days=250):
        calls["symbol"] = symbol
        calls["market"] = market
        calls["days"] = days
        return bars

    monkeypatch.setattr(fk, "load_qfq_bars_cached", _fake)
    return calls


def test_score_run_first_touch_resist_and_error_bp(monkeypatch):
    """阻力先触、支撑后触 → first_touch=resist；误差按窗口内极值算。"""
    bars = _bars([
        ("2026-10-20", 34.0, 34.6, 33.6, 34.2),   # 该轮当天，必须被排除
        ("2026-10-21", 34.2, 34.9, 33.8, 34.8),   # 都没触
        ("2026-10-22", 34.9, 36.1, 34.5, 35.9),   # 触阻力 35.8
        ("2026-10-23", 35.9, 36.4, 32.6, 33.0),   # 触支撑 32.8
    ])
    calls = _patch_bars(monkeypatch, bars)
    db = FakeTraceDB(runs=[_run()], steps=[_step()])

    out = fk.score_run(db, "run-1", horizon_td=3)

    assert out["available"] is True
    assert out["symbol"] == "002050"
    assert calls["symbol"] == "002050" and calls["market"] == "CN"
    assert out["levels"] == {"resist": 35.8, "support": 32.8}
    assert out["horizon_td"] == 3
    assert out["hit_resist"] is True and out["hit_support"] is True
    assert out["first_touch"] == "resist"
    # 窗口内最高 36.4：(36.4-35.8)/35.8*1e4 = 167.59… → 167.6
    assert out["error_bp"] == 167.6
    # 触阻力当天收 35.9，窗口末收 33.0 → down
    assert out["direction_after_touch"] == "down"
    assert out["as_of"] == "2026-10-23"
    assert out["soul"]["bars"] == ["2026-10-21", "2026-10-22", "2026-10-23"]
    # 不受当天 K 线影响：当日 high 34.6 不够阻力，若把当天算进去 first_touch 也会变
    assert out["soul"]["scored_on"] == "T+1..T+3 日K"


def test_score_run_error_bp_math_by_hand_on_two_row_fixture(monkeypatch):
    """两根 forward K：(36.2-35.8)/35.8*1e4 = 111.73 → 111.7（手算可复现）。"""
    bars = _bars([
        ("2026-10-20", 34.0, 34.5, 33.5, 34.1),
        ("2026-10-21", 34.1, 36.2, 33.0, 35.5),   # 触阻力，支撑差 0.2 未触
        ("2026-10-22", 35.5, 35.9, 33.2, 33.5),   # 仍不触支撑
    ])
    _patch_bars(monkeypatch, bars)
    db = FakeTraceDB(runs=[_run()], steps=[_step()])

    out = fk.score_run(db, "run-1", horizon_td=2)

    assert out["available"] is True
    assert out["first_touch"] == "resist"
    assert out["hit_support"] is False
    assert out["error_bp"] == 111.7
    assert out["as_of"] == "2026-10-22"


def test_score_run_support_only_touch(monkeypatch):
    bars = _bars([
        ("2026-10-20", 34.0, 34.2, 33.9, 34.0),
        ("2026-10-21", 33.9, 34.0, 32.5, 32.9),   # 触支撑 32.8
        ("2026-10-22", 32.9, 33.4, 32.7, 33.3),
    ])
    _patch_bars(monkeypatch, bars)
    db = FakeTraceDB(runs=[_run()], steps=[_step()])

    out = fk.score_run(db, "run-1", horizon_td=2)

    assert out["first_touch"] == "support"
    assert out["hit_resist"] is False
    # 最低 32.5：(32.8-32.5)/32.8*1e4 = 91.46 → 91.5
    assert out["error_bp"] == 91.5
    # 触支撑当天收 32.9，末日收 33.3 → up
    assert out["direction_after_touch"] == "up"


def test_score_run_too_early_is_unavailable_not_a_miss(monkeypatch):
    """T+3 没走完：available=False + 「行情尚未走完」，绝不算成未命中。"""
    bars = _bars([
        ("2026-10-20", 34.0, 34.5, 33.5, 34.1),
        ("2026-10-21", 34.1, 34.9, 33.9, 34.5),
    ])
    _patch_bars(monkeypatch, bars)
    db = FakeTraceDB(runs=[_run()], steps=[_step()])

    out = fk.score_run(db, "run-1", horizon_td=3)

    assert out["available"] is False
    assert "行情尚未走完" in out["reason"]
    assert "T+3" in out["reason"]
    assert out["have_bars"] == 1
    assert "hit_resist" not in out           # 不含任何命中字段，避免被上层当 0 统计


def test_score_run_without_scenario_step_is_unavailable():
    db = FakeTraceDB(runs=[_run()], steps=[_step(tool="get_trend_profile")])
    out = fk.score_run(db, "run-1")
    assert out == {"available": False, "reason": "该轮没有情景矩阵读数", "run_id": "run-1"}


def test_score_run_without_parseable_levels(monkeypatch):
    _patch_bars(monkeypatch, [])
    db = FakeTraceDB(runs=[_run()],
                     steps=[_step(digest="[情景矩阵] 关键位不足（缺均线/筹码带/区间高低）——不硬编情景。")])
    out = fk.score_run(db, "run-1")
    assert out["available"] is False
    assert "没有可解析的关键位" in out["reason"]


def test_score_run_without_symbol(monkeypatch):
    _patch_bars(monkeypatch, [])
    db = FakeTraceDB(runs=[_run()], steps=[_step(args="{}")])
    out = fk.score_run(db, "run-1")
    assert out["available"] is False
    assert "标的代码" in out["reason"]


# ------------------------------------------------------------------- aggregate


def test_aggregate_refuses_rates_below_min_samples(monkeypatch):
    """n < min_samples：只给 n 与原因，`*_rate` 键必须**不存在**（不是 0）。"""
    bars = _bars([
        ("2026-10-20", 34.0, 34.5, 33.5, 34.1),
        ("2026-10-21", 34.1, 36.2, 33.0, 35.5),
        ("2026-10-22", 35.5, 35.9, 33.2, 33.5),
        ("2026-10-23", 33.5, 34.0, 33.1, 33.8),
    ])
    _patch_bars(monkeypatch, bars)
    runs = [_run(run_id="run-1"), _run(run_id="run-2")]
    steps = [_step(run_id="run-1"), _step(run_id="run-2")]
    db = FakeTraceDB(runs=runs, steps=steps)

    out = fk.aggregate(db, days=30, min_samples=20)

    assert out["available"] is False
    assert out["n"] == 2
    assert out["reason"] == "样本不足"
    assert out["min_samples"] == 20
    for forbidden in ("hit_resist_rate", "hit_support_rate", "any_hit_rate",
                      "mean_error_bp", "by_recipe"):
        assert forbidden not in out, f"样本不足时不许出现 {forbidden}"


def test_aggregate_emits_rates_only_when_n_clears_threshold(monkeypatch):
    bars = _bars([
        ("2026-10-20", 34.0, 34.5, 33.5, 34.1),
        ("2026-10-21", 34.1, 36.2, 33.0, 35.5),
        ("2026-10-22", 35.5, 35.9, 33.2, 33.5),
        ("2026-10-23", 33.5, 34.0, 33.1, 33.8),
    ])
    _patch_bars(monkeypatch, bars)
    db = FakeTraceDB(
        runs=[_run(run_id="run-1"), _run(run_id="run-2")],
        steps=[_step(run_id="run-1"), _step(run_id="run-2")],
    )

    out = fk.aggregate(db, days=30, min_samples=2)

    assert out["available"] is True
    assert out["n"] == 2
    assert out["hit_resist_rate"] == 1.0
    assert out["hit_support_rate"] == 0.0
    assert out["by_recipe"]["deep_diagnosis"]["n"] == 2
    assert out["by_recipe"]["deep_diagnosis"]["mean_error_bp"] == 111.7
