"""情绪温度计条件化(2026-10-11 胜率证据层 ①)的钉子。

钉什么:
  ① SC15 封板成功率 / SC3 炸板比 派生(**纯函数**), 缺数据/分母 0 → None(不编造);
  ② sick 值(负/非有限) → suspicious=True 且**不硬算**该指标;
  ③ temperature_policy: 冷(<收紧线)→ 收紧(仅行1)、热(≥放宽线)→ 放宽(纳入行2)、
     炸板比高 → 收紧; 阈值走 thresholds(可 env 覆盖, 禁硬编码);
  ④ 缺数据 / suspicious → **不条件化**(按周期态放行), 向后兼容;
  ⑤ current_thermometer 读 market_sentiment_daily 最新行; 缺表 → available=False;
  ⑥ synthesize 透传温度计: regime_note 写明依据, temperature 字段回带。

禁真网络: 全走纯函数 / 内存 sqlite。
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from src.core import market_regime as mr
from src.core import sentiment_thermometer as st
from src.core import thresholds
from src.core.decision import synthesize
from src.web.migrations import _m178_market_sentiment_daily

# 三信号组合 → 状态表特定行(与 test_decision_regime 同口径)
ROW1 = ("G信号", 3.5, 1.0, 1.0e8, 0.1e8)   # 首次共振(向好→动手)
ROW3 = ("G区间", 3.5, 3.4, 1.0e8, 0.9e8)   # 平稳(向好→动手)
ROW2 = ("G区间", 4.0, 1.5, 2.0e8, 0.5e8)   # 再次共振(拐点→看看)


# ── ① 派生纯函数 ─────────────────────────────────────────────────────────────
def test_derive_normal():
    t = st.derive_thermometer({
        "trade_date": "20260918", "limit_up_count": 79, "limit_up_open_count": 28,
        "seal_success_money": 506.2, "seal_fail_money": 298.63,
    })
    assert t["available"] is True and t["suspicious"] is False
    assert abs(t["seal_success_rate"] - 506.2 / (506.2 + 298.63)) < 1e-9
    assert abs(t["blow_ratio"] - 28 / 79) < 1e-9


def test_derive_missing_and_zero_denominator():
    # 全缺 → available False, 不编造
    t = st.derive_thermometer({"trade_date": "x"})
    assert t["available"] is False and t["seal_success_rate"] is None and t["blow_ratio"] is None
    # 分母 0 → None(不拿 0/0 造数)
    t2 = st.derive_thermometer({"seal_success_money": 0, "seal_fail_money": 0,
                                "limit_up_count": 0, "limit_up_open_count": 0})
    assert t2["seal_success_rate"] is None and t2["blow_ratio"] is None


def test_derive_sick_flagged_not_computed():
    # 负值 = sick → suspicious, 不硬算
    t = st.derive_thermometer({
        "limit_up_count": 50, "limit_up_open_count": 10,
        "seal_success_money": -1, "seal_fail_money": 300,
    })
    assert t["suspicious"] is True
    assert t["seal_success_rate"] is None       # sick 维不硬算
    assert t["blow_ratio"] == 10 / 50           # 另一维正常照算


# ── ③ 温度条件化政策(纯函数) ────────────────────────────────────────────────
def test_policy_cold_tightens_only_row1():
    pol = mr.temperature_policy("ferment", {"available": True, "seal_success_rate": 0.40,
                                            "blow_ratio": 0.10, "trade_date": "x"})
    assert pol["policy"] == "tighten" and pol["allow_touch_rows"] == (1,)
    assert pol["thermo_applied"] is True
    assert "封板成功率 40%<阈值 50% → 收紧" in pol["note"]


def test_policy_hot_loosens_adds_row2():
    pol = mr.temperature_policy("ferment", {"available": True, "seal_success_rate": 0.75,
                                            "blow_ratio": 0.10, "trade_date": "x"})
    assert pol["policy"] == "loosen" and 2 in pol["allow_touch_rows"]
    assert "放宽" in pol["note"]


def test_policy_high_blow_tightens():
    pol = mr.temperature_policy("ferment", {"available": True, "seal_success_rate": 0.60,
                                            "blow_ratio": 0.55, "trade_date": "x"})
    assert pol["policy"] == "tighten" and "炸板比 55%≥阈值 45% → 收紧" in pol["note"]


def test_policy_no_data_or_sus_does_not_adjust():
    # 缺数据
    p1 = mr.temperature_policy("ferment", None)
    assert p1["thermo_applied"] is False and p1["allow_touch_rows"] == (1, 3)
    # sick
    p2 = mr.temperature_policy("ferment", {"available": True, "suspicious": True})
    assert p2["thermo_applied"] is False and "可疑" in p2["note"]


def test_condition_verdict_temperature_zero_break_without_thermo():
    # 无 thermo → 与纯周期条件化逐字一致
    v, adj, note = mr.condition_verdict("动手", 3, "ebb")
    assert v == "看看" and adj is True and "条件化" in note
    # 热(放宽)让 ferment 下的拐点行2 放开
    v2, adj2, _ = mr.condition_verdict("看看", 2, "ferment",
                                       thermo={"available": True, "seal_success_rate": 0.8,
                                               "blow_ratio": 0.1, "trade_date": "x"})
    assert v2 == "动手" and adj2 is True
    # 冷(收紧)让 ferment 下的平稳行3 收敛
    v3, adj3, note3 = mr.condition_verdict("动手", 3, "ferment",
                                           thermo={"available": True, "seal_success_rate": 0.3,
                                                   "blow_ratio": 0.1, "trade_date": "x"})
    assert v3 == "看看" and adj3 is True and "封板成功率" in note3


# ── ④ 阈值配置层(可 env 覆盖) ───────────────────────────────────────────────
def test_thresholds_defaults_and_env(monkeypatch):
    assert thresholds.value("seal_rate_tighten") == 0.50
    assert thresholds.value("seal_rate_loosen") == 0.70
    assert thresholds.value("blow_rate_tighten") == 0.45
    monkeypatch.setenv("SIDA_THRESHOLD_SEAL_RATE_TIGHTEN", "0.65")
    assert thresholds.value("seal_rate_tighten") == 0.65
    assert thresholds.source("seal_rate_tighten") == "env"


# ── ⑤ current_thermometer 读库 ───────────────────────────────────────────────
def _thermo_session():
    eng = sa.create_engine("sqlite://")
    with eng.connect() as conn:
        _m178_market_sentiment_daily(conn)
    return sessionmaker(bind=eng)()


def test_current_thermometer_missing_is_explicit():
    s = _thermo_session()
    t = st.current_thermometer(db=s, use_memo=False)
    assert t["available"] is False and "market_sentiment_daily" in t["reason"]


def test_current_thermometer_reads_latest():
    s = _thermo_session()
    with s.bind.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO market_sentiment_daily (trade_date, market, limit_up_count, "
            "limit_up_open_count, seal_success_money, seal_fail_money) VALUES "
            "('20260917','CN',49,23,360.35,278.92),"
            "('20260918','CN',79,28,506.2,298.63)"
        ))
    t = st.current_thermometer(db=s, use_memo=False)
    assert t["available"] is True and t["trade_date"] == "20260918"
    assert abs(t["seal_success_rate"] - 506.2 / (506.2 + 298.63)) < 1e-9


# ── ⑥ synthesize 透传: regime_note 写明依据 ─────────────────────────────────
def test_synthesize_with_thermo_notes_basis():
    o = synthesize(*ROW3, regime="ferment", thermo={"available": True, "seal_success_rate": 0.30,
                                                    "blow_ratio": 0.10, "trade_date": "20260918"})
    assert o["verdict"] == "看看" and o["regime_adjusted"] is True
    assert "封板成功率 30%<阈值 50% → 收紧" in o["reason"]
    assert o["temperature"]["seal_success_rate"] == 0.30
    # 无 thermo 时零破坏(不新增字段)
    base = synthesize(*ROW3, regime="ferment")
    assert base["verdict"] == "动手" and "temperature" not in base
