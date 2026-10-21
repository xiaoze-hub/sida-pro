"""标的画像判定器单测(2026-10-21)。

不连真库/不联网: `_load_demon` / `_load_bars` / `_load_theme_rank` 三个数据入口
全部 monkeypatch 成构造数据, detect() 内部不会碰 SessionLocal / klines / TDX RPC。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core import stock_persona as sp
from src.core.stock_persona import KINDS, Persona, detect, render


# ── 构造数据 ────────────────────────────────────────────────────────────

def _bars(closes: list[float], highs: list[float] | None = None) -> list[dict]:
    highs = highs or closes
    return [
        {"date": f"2026-{i:04d}", "open": c, "high": h, "low": c, "close": c}
        for i, (c, h) in enumerate(zip(closes, highs))
    ]


def _demon(total=78.0, grade="妖", streak=5, n_events=21):
    return {
        "factor_date": "20261020",
        "total": total,
        "grade": grade,
        "grade_raw": grade,
        "max_streak": streak,
        "n_events": n_events,
        "n_sealed": 8,
        "participation": "封板",
        "dims": {"freq": 80, "lianban": 100},
        "flags": [],
    }


@pytest.fixture
def no_data(monkeypatch):
    """默认全缺数据(各用例按需覆盖)。"""
    monkeypatch.setattr(sp, "_load_demon", lambda db, symbol: None)
    monkeypatch.setattr(sp, "_load_bars", lambda db, symbol, n=250, market="CN": [])
    monkeypatch.setattr(sp, "_load_theme_rank", lambda db, symbol, top_n=5: None)


# ── ① demon 命中 ───────────────────────────────────────────────────────

def test_demon_hits_with_real_numbers(monkeypatch):
    monkeypatch.setattr(sp, "_load_demon", lambda db, s: _demon(total=78.0, grade="妖", streak=5))
    monkeypatch.setattr(sp, "_load_bars", lambda db, s, n=250, market="CN": [])
    monkeypatch.setattr(sp, "_load_theme_rank", lambda db, s, top_n=5: None)

    p = detect(db=object(), symbol="600519")

    assert p.kind == "demon"
    assert p.recipe == "demon_diagnosis"
    assert p.label == "情绪妖股"
    assert p.score == 78.0
    assert p.grade == "A"          # 中文档「妖」→ A
    assert p.dims["demon"]["max_streak"] == 5
    assert any("78" in r and "最高连板 5 板" in r for r in p.reasons)


def test_demon_hits_by_grade_S_even_low_total(monkeypatch):
    monkeypatch.setattr(sp, "_load_demon", lambda db, s: _demon(total=52.0, grade="极妖", streak=6))
    monkeypatch.setattr(sp, "_load_bars", lambda db, s, n=250, market="CN": [])
    monkeypatch.setattr(sp, "_load_theme_rank", lambda db, s, top_n=5: None)

    p = detect(db=None, symbol="000001")
    assert p.kind == "demon" and p.grade == "S" and p.score == 52.0


# ── ② left 命中(伪造 40%+ 回撤)────────────────────────────────────────

def test_left_hits_on_drawdown_and_below_ma60(monkeypatch, no_data):
    # 前 190 根 100 元平台, 后 60 根一路跌到 58 → 回撤 42%, 现价在 MA60 下方
    closes = [100.0] * 190 + [99.0 - i * (41.0 / 59) for i in range(60)]
    monkeypatch.setattr(sp, "_load_bars", lambda db, s, n=250, market="CN": _bars(closes))

    p = detect(db=object(), symbol="300750")

    assert p.kind == "left"
    assert p.recipe == "left_diagnosis"
    assert p.label == "左侧超跌"
    assert p.dims["bars"]["drawdown_pct"] >= 35
    assert any("回撤" in r and "%" in r and "MA60" in r for r in p.reasons)
    # 现价确实是构造的 58.x, 依据里要能读到真实数字
    assert f"{p.dims['bars']['last_close']:.2f}" in " ".join(p.reasons)


# ── ③ trend 命中 ───────────────────────────────────────────────────────

def test_trend_hits_when_above_rising_ma60(monkeypatch, no_data):
    closes = [50.0 + i * (50.0 / 249) for i in range(250)]  # 稳步上行, 回撤 0
    monkeypatch.setattr(sp, "_load_bars", lambda db, s, n=250, market="CN": _bars(closes))

    p = detect(db=object(), symbol="600036")

    assert p.kind == "trend"
    assert p.recipe == "deep_diagnosis"
    assert p.label == "趋势白马"
    assert p.dims["ma60_slope_5d"] > 0
    assert any("站上 MA60" in r and "斜率" in r for r in p.reasons)


def test_low_grade_demon_does_not_short_circuit(monkeypatch, no_data):
    """grade=活跃(B) 且总分 45 → 不该判妖股, 继续走趋势判据。"""
    monkeypatch.setattr(sp, "_load_demon", lambda db, s: _demon(total=45.0, grade="活跃", streak=2))
    closes = [50.0 + i * (50.0 / 249) for i in range(250)]
    monkeypatch.setattr(sp, "_load_bars", lambda db, s, n=250, market="CN": _bars(closes))

    p = detect(db=object(), symbol="600036")
    assert p.kind == "trend"
    assert p.grade == "B" and p.score == 45.0
    assert any("未达妖股门槛" in r and "45" in r for r in p.reasons)


# ── ④ 全数据缺失 → normal 且 reasons 有说明 ────────────────────────────

def test_all_data_missing_falls_back_to_normal_with_explanation(no_data):
    p = detect(db=object(), symbol="002594")

    assert p.kind == "normal"
    assert p.recipe == "deep_diagnosis"
    assert p.score is None and p.grade is None
    joined = " ".join(p.reasons)
    assert "画像数据不足" in joined
    assert "取不到" in joined          # 每步缺数据都写明, 不静默
    assert any("跳过" in r for r in p.reasons)


def test_normal_with_data_still_reports_numbers(monkeypatch, no_data):
    """横盘票: 有数据、四条判据都不中 → normal, 依据里仍有回撤/MA60 真实数值。"""
    closes = [100.0 + (0.4 if i % 2 else -0.4) for i in range(250)]  # 横盘, 回撤 <1%
    monkeypatch.setattr(sp, "_load_bars", lambda db, s, n=250, market="CN": _bars(closes))

    p = detect(db=object(), symbol="601988")

    assert p.kind == "normal"
    joined = " ".join(p.reasons)
    assert "未触发妖股/左侧/题材龙头/趋势任一判据" in joined
    assert re.search(r"MA60 \d+\.\d+", joined)      # 有真实均线数值
    assert p.dims["bars"]["drawdown_pct"] is not None


def test_fewer_than_60_bars_skips_ma_rules(monkeypatch, no_data):
    monkeypatch.setattr(sp, "_load_bars", lambda db, s, n=250, market="CN": _bars([10.0] * 20))
    p = detect(db=object(), symbol="002594")
    assert p.kind == "normal"
    assert any("不足 60 根算 MA60" in r for r in p.reasons)


# ── ③' leader 命中(题材快照)────────────────────────────────────────────

def test_leader_hits_on_top_theme(monkeypatch, no_data):
    monkeypatch.setattr(
        sp, "_load_theme_rank",
        lambda db, s, top_n=5: {"group": "算力", "rank": 2, "score": 71.4, "is_leader": True, "days": 5},
    )
    p = detect(db=object(), symbol="601138")

    assert p.kind == "leader"
    assert p.recipe == "theme_leader"
    assert p.label == "题材龙头"
    assert any("算力" in r and "第 2" in r and "71.4" in r for r in p.reasons)


def test_theme_rank_matches_leader_code_from_payload(monkeypatch):
    """_load_theme_rank 自身逻辑: 从 payload 里按代码前缀归一匹配龙头。"""
    payload = {
        "ranked_groups": [
            {"name": "机器人", "score": 80.0, "leader": {"code": "300024", "days": 4}, "constituents": []},
            {"name": "算力", "score": 60.0, "leader": {"code": "SH601138", "days": 5},
             "constituents": [{"code": "000977", "days": 2}]},
        ]
    }

    class _Row:
        def __init__(self, v):
            self._mapping = {"payload": v}

    class _Sess:
        def execute(self, *a, **k):
            class _R:
                def fetchone(self_inner):
                    return _Row(payload)
            return _R()

    got = sp._load_theme_rank(_Sess(), "601138")
    assert got and got["group"] == "算力" and got["rank"] == 2 and got["is_leader"] is True
    # 非龙头但在前 2 名题材成分里 → 仍算题材靠前, is_leader=False
    got2 = sp._load_theme_rank(_Sess(), "000977")
    assert got2 and got2["is_leader"] is False and got2["rank"] == 2
    # 完全不在前列 → None(不编造)
    assert sp._load_theme_rank(_Sess(), "600000") is None


# ── 日 K 口径: 交易日根数 vs 自然日窗口 ─────────────────────────────────

def test_load_bars_converts_trading_days_to_calendar_window(monkeypatch):
    """load_qfq_bars(days=) 是自然日窗口, 250 个交易日必须换更大的自然日窗口。"""
    from src.db import klines_repo

    seen = {}

    def _fake(symbol, market, days=250):
        seen["days"] = days
        return [{"date": f"d{i}", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}
                for i in range(300)]

    monkeypatch.setattr(klines_repo, "load_qfq_bars", _fake)
    bars = sp._load_bars(None, "600519", 250, "CN")

    assert seen["days"] >= 360          # 自然日 > 交易日
    assert len(bars) == 250             # 只留最后 250 根
    assert bars[-1]["close"] == 1.5


# ── 耗时预算保护: 超预算跳过题材查询, 但不静默改判 ────────────────────

def test_budget_guard_skips_theme_lookup(monkeypatch, no_data):
    ticks = {"n": 0}

    def _clock():
        ticks["n"] += 1
        return 0.0 if ticks["n"] == 1 else 5.0

    monkeypatch.setattr(sp.time, "monotonic", _clock)
    called = {"hit": False}

    def _theme(db, symbol, top_n=5):
        called["hit"] = True
        return None

    monkeypatch.setattr(sp, "_load_theme_rank", _theme)

    p = detect(db=object(), symbol="600036")

    assert called["hit"] is False                       # 没超预算查询
    assert any("跳过题材龙头判据(保" in r for r in p.reasons)  # 但写明为什么没查
    assert p.kind in KINDS


# ── ⑤ render 输出 ──────────────────────────────────────────────────────

def test_render_contains_label_and_numeric_reason(monkeypatch, no_data):
    monkeypatch.setattr(sp, "_load_bars",
                        lambda db, s, n=250, market="CN": _bars([50.0 + i * (50.0 / 249) for i in range(250)]))
    p = detect(db=object(), symbol="600036")
    text = render(p)

    lines = text.splitlines()
    assert p.label in lines[0]
    assert p.kind in lines[0] and p.recipe in lines[0]
    assert len(lines) > 1 and lines[1].startswith("·")
    assert re.search(r"\d+\.\d+", "\n".join(lines[1:]))  # 至少一条依据带真实数值


def test_render_works_for_missing_data(no_data):
    text = render(detect(db=object(), symbol="002594"))
    assert "常规个股" in text and "画像数据不足" in text


# ── ⑥ 架构约束: core 不 import src.web / 不联网 ────────────────────────

def test_module_does_not_import_web_layer_or_network():
    src = Path(sp.__file__).read_text(encoding="utf-8")
    code_lines = [
        ln for ln in src.splitlines()
        if ln.strip().startswith(("import ", "from ")) and not ln.strip().startswith("#")
    ]
    joined = "\n".join(code_lines)
    assert "src.web" not in joined
    for banned in ("requests", "httpx", "aiohttp", "urllib"):
        assert banned not in joined, f"core 画像判定不许联网: {banned}"
    assert "src.db.session" in src  # 会话走中立层


def test_persona_contract_fields():
    p = detect(db=object(), symbol="")  # 空代码也不炸
    assert isinstance(p, Persona)
    assert p.kind in KINDS
    assert isinstance(p.reasons, list) and isinstance(p.dims, dict)
    assert p.label and p.recipe
