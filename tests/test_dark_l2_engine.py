"""tests/test_dark_l2_engine.py — thsdk L2 新模块集成测试(2026-08-19)。

覆盖:
- dark_l2.fetch_l2_ticks: thsdk 逐笔 → 腾讯同构 tick 列表(竞价保护/差分还原/单位)
- delta_engine.compute_delta_series: 秒级 Delta + 背离信号
- orderbook_engine.run: 盘口演变 + OB 失衡 + 幽灵单
- thsdk_alert.run: 尾盘突击 + 竞价快照 + wencai 候选池

风格对齐 test_dark_flow.py: 真实数据 + 弹性断言(>= 最低门槛), 兼容盘中/盘后。
注意: 依赖生产机 thsdk 游客账户, 非交易时段某些检测为空属正常, 用弹性断言。
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

THSDK_AVAILABLE = True
try:
    from src.core.dark_l2 import fetch_l2_ticks
    from src.core.delta_engine import compute_delta_series
    from src.core.orderbook_engine import run as orderbook_run
    from src.core.thsdk_alert import run as alert_run
except ImportError as e:  # 无 thsdk 环境下跳过(CI 等)
    THSDK_AVAILABLE = False
    pytest.skip(f"thsdk 未安装: {e}", allow_module_level=True)


pytestmark = pytest.mark.skipif(
    not THSDK_AVAILABLE, reason="thsdk 环境不可用"
)


class TestDarkL2:
    def test_fetch_ticks_shape(self):
        """thsdk 逐笔 → 腾讯同构列表, 字段齐全。"""
        ticks = fetch_l2_ticks("sz002361", "thsdk")
        assert len(ticks) > 100  # 弹性: 盘中/盘后至少百笔
        t0 = ticks[0]
        assert set(t0.keys()) == {"d", "amt", "vol", "price", "t"}
        assert t0["d"] in ("B", "S", "M")
        assert t0["amt"] > 0
        assert t0["vol"] > 0
        assert ":" in t0["t"]

    def test_auction_neutral(self):
        """竞价时段(09:15-09:30)必须标 M 中性, 不污染主动 Delta(dark_flow 教训)。"""
        ticks = fetch_l2_ticks("sz002361", "thsdk")
        pre = [t for t in ticks if t["t"] < "09:30"]
        for t in pre:
            assert t["d"] == "M", f"竞价行 {t} 应为中性"


class TestDeltaEngine:
    def test_delta_series(self):
        """秒级 Delta 序列结构完整。"""
        from src.core.dark_l2 import fetch_l2_ticks
        ticks = fetch_l2_ticks("sz002361", "thsdk")
        r = compute_delta_series(ticks)
        assert "ticks" in r and "seconds" in r and "stats" in r
        assert r["ticks"] == len(ticks)
        assert len(r["seconds"]) > 0
        s0 = r["seconds"][0]
        assert {"net", "buy", "sell", "delta30", "cum_net"} <= set(s0.keys())

    def test_stats_sane(self):
        """统计字段合理(金额>0, 价格区间有效)。"""
        from src.core.dark_l2 import fetch_l2_ticks
        ticks = fetch_l2_ticks("sz002361", "thsdk")
        r = compute_delta_series(ticks)
        st = r["stats"]
        assert st["total_buy_yuan"] > 0 and st["total_sell_yuan"] > 0
        assert st["hi_price"] >= st["lo_price"]


class TestOrderbookEngine:
    def test_run_shape(self):
        """盘口引擎返回结构化结果。"""
        r = orderbook_run("USZA002361", n_snapshots=2, interval=0.5)
        assert set(r.keys()) == {"events", "ob_series", "ghost_ratio", "summary"}
        assert isinstance(r["events"], list)
        assert isinstance(r["ob_series"], list)
        assert 0.0 <= r["ghost_ratio"] <= 1.0


class TestThsdkAlert:
    def test_run_shape(self):
        """预警模块返回结构化结果(竞价/尾盘/wencai)。"""
        r = alert_run("USZA002361")
        assert set(r.keys()) >= {"symbol", "close_surge", "auction", "wencai_pool"}
        assert "direction" in r["close_surge"]
        assert "direction" in r["auction"]
        assert isinstance(r["wencai_pool"], dict)