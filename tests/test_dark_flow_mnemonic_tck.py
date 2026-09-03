"""v0.4.79 口诀活代码化测试。

验证 _judge_mnemonic 的 tck_active_ratio 入参:
  - tck_active_ratio < 30% → ⑥ 双小单触发(原始主动率口诀)
  - tck_active_ratio > 85% → ⑦ 双大单对倒触发(原始主动率口诀)
  - tck_active_ratio 在 [30, 85] → 不触发 tck 路径, 走兜底
  - tck_active_ratio=None / 越界 → 兜底路径, 不破坏现有行为
"""
import pytest

from src.core.dark_flow import _judge_mnemonic


def _mk_quote(change_pct=0.0, volume_ratio=1.0, outer=100.0, inner=100.0,
              volume=200.0):
    """构造满足 _judge_mnemonic 最低数据要求的 quote。"""
    return {
        "volume_outer": outer,
        "volume_inner": inner,
        "change_pct": change_pct,
        "volume_ratio": volume_ratio,
        "volume": volume,
        "current_price": 10.0,
        "high_price": 10.0,
        "low_price": 10.0,
    }


def _mk_dark(data_status="ok", position="mid"):
    return {
        "data_status": data_status,
        "inner_outer": {"position": position},
        "signal": "",
    }


class TestTckPath:
    """有 .tck 主动率时, ⑥⑦ 走原始口诀(优先级最高)。"""

    def test_dual_large_triggers_when_ratio_above_85(self):
        """⑦ 双大单对倒: tck_active_ratio=90% → 命中, 无视兜底条件。"""
        # 兜底条件(对倒)需要 volume_ratio>1.2 + imbalance + no_move
        # 我们故意设成不触发兜底, 看 tck 路径是否独占
        q = _mk_quote(change_pct=0.0, volume_ratio=0.5, outer=200, inner=200, volume=400)
        d = _mk_dark()
        m = _judge_mnemonic(d, q, tck_active_ratio=90.0)
        assert m is not None
        assert m["mnemonic"] == "双大单对倒"
        assert m["direction"] == "警惕"
        assert "90%" in m["detail"]

    def test_dual_small_triggers_when_ratio_below_30(self):
        """⑥ 双小单: tck_active_ratio=25% → 命中, 走观望。"""
        q = _mk_quote(change_pct=0.0, volume_ratio=0.5, outer=200, inner=200, volume=400)
        d = _mk_dark()
        m = _judge_mnemonic(d, q, tck_active_ratio=25.0)
        assert m is not None
        assert m["mnemonic"] == "双小单"
        assert m["direction"] == "观望"
        assert "25%" in m["detail"]

    def test_ratio_in_mid_band_falls_through(self):
        """tck_active_ratio=50 (中间区间) → 不触发 tck 路径, 走兜底。"""
        # 设兜底条件: 缩量+震荡 → 控盘洗盘
        q = _mk_quote(change_pct=1.0, volume_ratio=0.5, outer=200, inner=200, volume=400)
        d = _mk_dark()
        m = _judge_mnemonic(d, q, tck_active_ratio=50.0)
        assert m is not None
        # 中间区间不命中 tck 路径, 应该走兜底
        assert m["mnemonic"] != "双大单对倒"
        assert m["mnemonic"] != "双小单"


class TestFallbackPath:
    """无 .tck 时, ⑥⑦ 走腾讯兜底(保持现有行为)。"""

    def test_none_ratio_uses_fallback(self):
        """tck_active_ratio=None → 完全不动行为, 走兜底。"""
        # 设兜底条件: 控盘洗盘(缩量+震荡)
        q = _mk_quote(change_pct=1.0, volume_ratio=0.5, outer=200, inner=200, volume=400)
        d = _mk_dark()
        m_none = _judge_mnemonic(d, q, tck_active_ratio=None)
        m_default = _judge_mnemonic(d, q)  # 默认参数
        assert m_none == m_default  # 完全等价
        assert m_none["mnemonic"] == "控盘洗盘"  # 兜底规则命中

    def test_out_of_range_ratio_treated_as_none(self):
        """越界值(负数/超100)→ 视同 None, 走兜底。"""
        q = _mk_quote(change_pct=1.0, volume_ratio=0.5, outer=200, inner=200, volume=400)
        d = _mk_dark()
        m_neg = _judge_mnemonic(d, q, tck_active_ratio=-5.0)
        m_high = _judge_mnemonic(d, q, tck_active_ratio=150.0)
        assert m_neg["mnemonic"] == "控盘洗盘"
        assert m_high["mnemonic"] == "控盘洗盘"

    def test_non_numeric_ratio_treated_as_none(self):
        """非数值 → 视同 None(防止崩溃)。"""
        q = _mk_quote(change_pct=1.0, volume_ratio=0.5, outer=200, inner=200, volume=400)
        d = _mk_dark()
        m = _judge_mnemonic(d, q, tck_active_ratio="abc")
        assert m["mnemonic"] == "控盘洗盘"  # 兜底

    def test_existing_mnemonics_still_work(self):
        """原有 ①~⑤ 兜底口诀仍正常触发。"""
        # ① 真金进攻: 外盘>55% + 涨 + 放量
        q = _mk_quote(change_pct=2.0, volume_ratio=2.0, outer=300, inner=100, volume=400)
        d = _mk_dark()
        m = _judge_mnemonic(d, q, tck_active_ratio=None)
        assert m["mnemonic"] == "真金进攻"
        assert m["direction"] == "看涨"


class TestTckPriority:
    """tck 路径优先级高于兜底: 即便兜底条件也命中, tck 路径先返回。"""

    def test_tck_dual_large_overrides_fallback_dual_large(self):
        """tck_active_ratio=90% 同时兜底 ⑦「内外失衡+不动+放量」也命中时, tck 路径胜出。
        (口诀名区分: tck 路径是"双大单对倒", 兜底是"对倒造假", 业务方可分辨数据来源。)
        """
        q = _mk_quote(change_pct=0.0, volume_ratio=2.0,
                      outer=160, inner=140, volume=300)  # imbalance=6.67 < 15, churn=2.0>1.2
        d = _mk_dark()
        m = _judge_mnemonic(d, q, tck_active_ratio=90.0)
        assert m["mnemonic"] == "双大单对倒"  # tck 路径, 不是兜底"对倒造假"
