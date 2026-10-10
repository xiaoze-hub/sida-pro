"""决策提胜率 A: 情绪周期条件化门槛 + 账本 regime 分桶的钉子(2026-10-10)。

钉什么:
  ① 条件化只改『动手』档, 且按态可解释 —— 至少覆盖 冰点/退潮/高潮(收紧)、修复(放宽)、
     发酵(标准) 五态; 『别碰』永不被 regime 改动;
  ② 无 regime(缺省)/ unknown → 与旧版逐字一致(向后兼容零破坏);
  ③ 缺数据显式: current_regime 读不到 → available=False + regime='unknown'(不猜状态);
  ④ 账本 regime 分桶聚合: 同 signal_kind 下按 regime 分桶命中率, 样本不足不给数字;
  ⑤ 迁移 v184 幂等 + regime 列存在; 老库(仅 v175 无 regime 列)stats 仍可用(regime_rows=[])。

禁真网络: 全部走纯函数 / 内存 sqlite。
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from src.core.decision import synthesize
from src.core import market_regime as mr
from src.core import decision_log as dl
from src.web.migrations import _m175_decision_log, _m184_decision_log_regime

# ── 造态: 三信号组合 → 7 行状态表特定行 ─────────────────────────────────────
# 行1 首次共振(向好→动手): G信号 + 活跃度≥强势线 + 资金流入
ROW1 = ("G信号", 3.5, 1.0, 1.0e8, 0.1e8)
# 行3 平稳(向好→动手): G区间 + 强势 + 流入, 无翻倍
ROW3 = ("G区间", 3.5, 3.4, 1.0e8, 0.9e8)
# 行2 再次共振(拐点→看看): G区间 + 双翻倍
ROW2 = ("G区间", 4.0, 1.5, 2.0e8, 0.5e8)
# 行7 全走坏(别碰): S信号 + 跌破强势线 + 流出
ROW7 = ("S信号", 1.0, 1.0, -1.0e8, -0.5e8)


def _verdict(row, regime):
    return synthesize(*row, regime=regime)


def test_regime_tighten_states_only_trim_touch():
    """收紧档(冰点/退潮/高潮): 行1 保留动手; 行3 收敛看看; 行2/行7 不动。"""
    for reg in ("ice", "ebb", "climax"):
        o1 = _verdict(ROW1, reg)
        assert o1["verdict"] == "动手", (reg, o1)
        o3 = _verdict(ROW3, reg)
        assert o3["verdict"] == "看看" and o3["regime_adjusted"] is True, (reg, o3)
        assert "条件化" in o3["reason"]
        o2 = _verdict(ROW2, reg)
        assert o2["verdict"] == "看看" and o2["regime_adjusted"] is False, (reg, o2)
        o7 = _verdict(ROW7, reg)
        assert o7["verdict"] == "别碰", (reg, o7)  # 『别碰』永不被 regime 改动


def test_regime_loosen_repair_opens_turning_point():
    """放宽档(修复): 拐点(再次共振 行2)放开为动手; 行1/行3 仍动手。"""
    o2 = _verdict(ROW2, "repair")
    assert o2["verdict"] == "动手" and o2["regime_adjusted"] is True, o2
    assert "放宽" in o2["reason"]
    assert _verdict(ROW1, "repair")["verdict"] == "动手"
    assert _verdict(ROW3, "repair")["verdict"] == "动手"


def test_regime_ferment_is_standard():
    """发酵档 = 状态表标准口径: 行1/行3 动手, 行2 看看(不调整)。"""
    assert _verdict(ROW1, "ferment")["verdict"] == "动手"
    assert _verdict(ROW3, "ferment")["verdict"] == "动手"
    o2 = _verdict(ROW2, "ferment")
    assert o2["verdict"] == "看看" and o2["regime_adjusted"] is False


def test_no_regime_backward_compatible():
    """缺 regime(缺省)/ unknown → 与基线逐字段一致(不调整), 且带显式 regime 字段。"""
    base1 = synthesize(*ROW1)
    base3 = synthesize(*ROW3)
    assert base1["regime"] == "unknown" and base1["regime_policy"] == "normal"
    assert base1["regime_adjusted"] is False and base3["verdict"] == "动手"
    # 显式 unknown 与缺省等价
    u1 = synthesize(*ROW1, regime="unknown")
    assert u1["verdict"] == base1["verdict"] and u1["reason"] == base1["reason"]


def test_pioneer_terms_align_to_decision_pioneer_vocab():
    """输出带决策先锋口径中文表述(情绪周期/资金博弈/主力动向), 只命名对齐不改判定。"""
    o = synthesize(*ROW1, regime="ebb")
    assert o["pioneer_terms"]["情绪周期"] == "退潮"
    assert o["pioneer_terms"]["资金博弈"] == "多方占优"
    assert o["pioneer_terms"]["主力动向"] == "未归集"  # A 阶段无七口诀 → 显式未归集


def test_normalize_regime_maps_cycle_and_phase():
    assert mr.normalize_regime("退潮") == "ebb"
    assert mr.normalize_regime("冰点") == "ice"
    assert mr.normalize_regime("ebb") == "ebb"
    assert mr.normalize_regime("ignite") == "ferment"
    assert mr.normalize_regime("accumulating") == "repair"
    assert mr.normalize_regime("不认识的状态") == "unknown"
    assert mr.normalize_regime(None) == "unknown"


# ── current_regime: 缺数据显式 + 两源读取 ───────────────────────────────────
def _regime_session():
    from src.db.models import MarketPhaseDaily, SignalSummaryDaily

    eng = sa.create_engine("sqlite://")
    SignalSummaryDaily.__table__.create(eng)
    MarketPhaseDaily.__table__.create(eng)
    return sessionmaker(bind=eng)()


def test_current_regime_missing_is_explicit_unknown():
    s = _regime_session()
    r = mr.current_regime(db=s, use_memo=False)
    assert r["available"] is False and r["regime"] == "unknown"
    assert r["source"] == "none" and "reason" in r


def test_current_regime_reads_signal_summary_then_phase():
    from src.db.models import MarketPhaseDaily, SignalSummaryDaily

    s = _regime_session()
    # 只有 market_phase → 兜底源
    s.add(MarketPhaseDaily(date=sa.func.date("2026-10-09"), phase="repair"))
    s.commit()
    r1 = mr.current_regime(db=s, use_memo=False)
    assert r1["regime"] == "repair" and r1["source"] == "market_phase", r1
    # 补 signal_summary → 主源优先
    s.add(SignalSummaryDaily(snapshot_date="2026-10-09", stock_market="CN",
                             blocks={"sentiment": {"cycle": "退潮", "confidence": "高"}}, text="x"))
    s.commit()
    r2 = mr.current_regime(db=s, use_memo=False)
    assert r2["regime"] == "ebb" and r2["source"] == "signal_summary", r2
    assert r2["confidence"] == "高"


# ── 账本 regime 分桶 ─────────────────────────────────────────────────────────
def _engine_with_regime():
    eng = sa.create_engine("sqlite://")
    with eng.connect() as conn:
        _m175_decision_log(conn)
        _m184_decision_log_regime(conn)
    return eng


def test_migration_regime_idempotent_and_column_present():
    eng = _engine_with_regime()
    with eng.connect() as conn:
        _m184_decision_log_regime(conn)  # 再跑一次不炸
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(decision_log)"))}
    assert "regime" in cols


def _provider(bars):
    def _p(_engine, symbol, start_day):
        return [b for b in bars.get(symbol, []) if b[0] >= start_day]
    return _p


def test_stats_buckets_hit_rate_by_regime():
    eng = _engine_with_regime()
    # ebb 桶: 2 只(1 命中); ferment 桶: 2 只(0 命中); 无 regime 行归 unknown
    dl.record_signal(eng, signal_kind="resonance3", symbol="E1", trade_date="2026-09-16", price=10.0, regime="退潮")
    dl.record_signal(eng, signal_kind="resonance3", symbol="E2", trade_date="2026-09-16", price=10.0, regime="ebb")
    dl.record_signal(eng, signal_kind="resonance3", symbol="F1", trade_date="2026-09-16", price=10.0, regime="发酵")
    dl.record_signal(eng, signal_kind="resonance3", symbol="U1", trade_date="2026-09-16", price=10.0)
    bars = {
        "E1": [("2026-09-16", 10.0), ("2026-09-17", 11.0)],   # 命中
        "E2": [("2026-09-16", 10.0), ("2026-09-17", 9.0)],    # 未命中
        "F1": [("2026-09-16", 10.0), ("2026-09-17", 9.5)],    # 未命中
        "U1": [("2026-09-16", 10.0), ("2026-09-17", 11.0)],   # 命中
    }
    dl.backfill_outcomes(eng, series_provider=_provider(bars))
    st = dl.stats(eng, min_sample=1)
    buckets = {(r["signal_kind"], r["regime"]): r for r in st["regime_rows"]}
    assert buckets[("resonance3", "ebb")]["horizons"]["t1"]["hit_rate"] == 0.5
    assert buckets[("resonance3", "ebb")]["n_total"] == 2
    assert buckets[("resonance3", "ferment")]["horizons"]["t1"]["hit_rate"] == 0.0
    assert buckets[("resonance3", "unknown")]["horizons"]["t1"]["hit_rate"] == 1.0
    # 顶层 rows(signal_kind 合计)不受分桶影响
    assert st["rows"][0]["signal_kind"] == "resonance3"
    assert st["rows"][0]["n_total"] == 4


def test_record_keeps_regime_when_second_write_omits_it():
    eng = _engine_with_regime()
    dl.record_signal(eng, signal_kind="resonance3", symbol="K1", trade_date="2026-09-16", price=10.0, regime="ebb")
    dl.record_signal(eng, signal_kind="resonance3", symbol="K1", trade_date="2026-09-16", context={"x": 1})
    with eng.connect() as conn:
        reg = conn.execute(sa.text("SELECT regime FROM decision_log WHERE symbol='K1'")).scalar()
    assert reg == "ebb"


def test_stats_backward_compatible_on_old_schema():
    """老库(仅 v175, 无 regime 列): stats 仍可用, regime_rows 为空, 不炸。"""
    eng = sa.create_engine("sqlite://")
    with eng.connect() as conn:
        _m175_decision_log(conn)
    dl.record_signal(eng, signal_kind="resonance3", symbol="O1", trade_date="2026-09-16", price=10.0, regime="ebb")
    st = dl.stats(eng, min_sample=1)
    assert st["rows"][0]["signal_kind"] == "resonance3"
    assert st["regime_rows"] == []
