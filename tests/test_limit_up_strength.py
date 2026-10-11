# -*- coding: utf-8 -*-
"""首封时间×封成比 强度因子纯函数测试(2026-10-11 胜率创新 P0-1, 影子运行)。

全 mock/纯计算, 禁真网络。口径见 src/core/limit_up_strength.py。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import limit_up_strength as lus  # noqa: E402


# ─────────────── parse_clock ───────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("09:25:01", 9 * 3600 + 25 * 60 + 1),
        ("093105", 9 * 3600 + 31 * 60 + 5),
        ("93105", 9 * 3600 + 31 * 60 + 5),
        ("92501", 9 * 3600 + 25 * 60 + 1),
        ("14:30:00", 14 * 3600 + 30 * 60),
        ("", None),
        (None, None),
        ("abc", None),
        ("999999", None),  # 越界
        ("25:00:00", None),
    ],
)
def test_parse_clock(raw, expected):
    assert lus.parse_clock(raw) == expected


# ─────────────── first_seal_bucket ───────────────

def test_first_seal_bucket_defaults():
    assert lus.first_seal_bucket("09:25:01") == "竞价"
    assert lus.first_seal_bucket("09:30:00") == "竞价"   # 边界含
    assert lus.first_seal_bucket("09:30:01") == "早盘"
    assert lus.first_seal_bucket("10:00:00") == "早盘"   # 边界含
    assert lus.first_seal_bucket("10:00:01") == "盘中"
    assert lus.first_seal_bucket("14:30:00") == "盘中"   # 边界含
    assert lus.first_seal_bucket("14:30:01") == "尾盘"
    assert lus.first_seal_bucket("") is None


def test_first_seal_bucket_env_override(monkeypatch):
    # 阈值走配置层: 竞价上界改到 10:00 → 09:40 也归竞价
    monkeypatch.setenv("SIDA_THRESHOLD_FIRST_SEAL_AUC_END", "36000")
    assert lus.first_seal_bucket("09:40:00") == "竞价"


# ─────────────── compute_seal_ratio ───────────────

def test_compute_seal_ratio():
    assert lus.compute_seal_ratio(1e8, 2e9) == pytest.approx(0.05)
    assert lus.compute_seal_ratio(None, 2e9) is None
    assert lus.compute_seal_ratio(1e8, None) is None
    assert lus.compute_seal_ratio(1e8, 0) is None        # 分母 0 不猜
    assert lus.compute_seal_ratio(1e8, -1) is None
    assert lus.compute_seal_ratio("x", 2e9) is None
    assert lus.compute_seal_ratio(float("nan"), 2e9) is None  # nan 分子 → 不猜
    assert lus.compute_seal_ratio(1e8, float("inf")) == 0.0  # inf 分母 → 比值有限(0.0)


# ─────────────── seal_ratio_grade ───────────────

def test_seal_ratio_grade():
    assert lus.seal_ratio_grade(0.005) == "C"
    assert lus.seal_ratio_grade(0.01) == "B"   # 边界含
    assert lus.seal_ratio_grade(0.02) == "B"
    assert lus.seal_ratio_grade(0.03) == "A"   # 边界含
    assert lus.seal_ratio_grade(0.049) == "A"
    assert lus.seal_ratio_grade(0.05) == "S"   # 边界含
    assert lus.seal_ratio_grade(0.2) == "S"
    assert lus.seal_ratio_grade(None) is None


# ─────────────── strength_label: 矩阵 ───────────────

@pytest.mark.parametrize(
    "first_time,ratio,expected",
    [
        ("09:25:01", 0.06, "S"),  # 竞价 + S 档
        ("09:25:01", 0.01, "A"),  # 竞价 + B 档
        ("09:25:01", 0.005, "B"),  # 竞价 + C 档
        ("09:35:00", 0.06, "S"),  # 早盘 + S 档
        ("09:35:00", 0.04, "A"),  # 早盘 + A 档
        ("09:35:00", 0.02, "B"),  # 早盘 + B 档
        ("09:35:00", 0.005, "C"),  # 早盘 + C 档
        ("11:00:00", 0.06, "A"),  # 盘中 + S 档
        ("11:00:00", 0.02, "B"),  # 盘中 + B 档
        ("14:50:00", 0.06, "B"),  # 尾盘 + S 档
        ("14:50:00", 0.005, "C"),  # 尾盘 + C 档
    ],
)
def test_strength_label_matrix(first_time, ratio, expected):
    res = lus.strength_label(first_time=first_time, ratio=ratio)
    assert res["status"] == "ok"
    assert res["label"] == expected
    assert res["label_raw"] == expected  # 无炸板 → 不降档
    assert res["downgraded"] is False


def test_strength_label_from_amounts():
    # 由 封单额/流通市值 现算
    res = lus.strength_label(first_time="09:26:00", seal_amount=1e8, circulating_mktcap=2e9)
    assert res["label"] == "S"
    assert res["seal_ratio"] == pytest.approx(0.05)
    assert res["first_seal_bucket"] == "竞价"


# ─────────────── strength_label: 炸板降档 ───────────────

def test_strength_label_blow_downgrade():
    res = lus.strength_label(first_time="09:26:00", ratio=0.06, open_times=1)
    assert res["label_raw"] == "S" and res["label"] == "A" and res["downgraded"] is True
    # C 已是底, 不降
    res2 = lus.strength_label(first_time="14:50:00", ratio=0.005, open_times=3)
    assert res2["label"] == "C"


# ─────────────── strength_label: no_data 不编造 ───────────────

def test_strength_label_no_data_missing_first():
    res = lus.strength_label(first_time="", ratio=0.06)
    assert res["status"] == "no_data" and res["label"] is None
    assert "first_time" in res["missing"] and "seal_ratio" not in res["missing"]
    assert res["seal_ratio_grade"] == "S"  # 分量仍如实给出


def test_strength_label_no_data_missing_ratio():
    res = lus.strength_label(first_time="09:26:00", seal_amount=None, circulating_mktcap=None)
    assert res["status"] == "no_data" and res["label"] is None
    assert "seal_ratio" in res["missing"]
    assert res["first_seal_bucket"] == "竞价"


def test_strength_label_no_data_both():
    res = lus.strength_label()
    assert res["status"] == "no_data" and res["label"] is None
    assert set(res["missing"]) == {"first_time", "seal_ratio"}


def test_strength_label_note_never_advises():
    """信号是证据非建议: note 禁含『建议买入/加仓』类行动措辞。"""
    res = lus.strength_label(first_time="09:26:00", ratio=0.06)
    assert "买入" not in res["note"] and "加仓" not in res["note"]
    assert "影子运行" in res["note"] and "非交易建议" in res["note"]


def test_to_int_blow():
    assert lus.strength_label(first_time="09:26:00", ratio=0.06, open_times="2")["open_times"] == 2
    assert lus.strength_label(first_time="09:26:00", ratio=0.06, open_times=None)["open_times"] is None
    assert lus.strength_label(first_time="09:26:00", ratio=0.06, open_times=True)["open_times"] is None
