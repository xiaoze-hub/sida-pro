"""暗盘资金计算器测试 v5(2026-08-11): 三分类 + 大单/暗盘分层 + 分价表价位维度。"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent  # /tmp/PanWatch
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/marketdata/src"))

import pytest
from marketdata import Symbol
from src.core.dark_flow import compute_dark_flow, _judge_signal, _fetch_all_ticks, _tencent_code
from src.core import dark_flow as df_module


class TestTickCacheCrossDay:
    """2026-08-13 跨日缓存回归: 腾讯逐笔页码按天重置, 昨天残留缓存必须触发全量重拉,
    否则增量续拉从旧 last_page 往后拉 → 拉不到今天 0 页起的数据(曾实测返回 2 条残留)。"""

    def test_old_4tuple_cache_is_stale(self):
        """旧格式 4 元组缓存(无 day) → stale, 必须重拉。"""
        assert df_module._cache_stale("x", (1.0, [], 68, 4760)) is True

    def test_yesterday_cache_is_stale(self):
        """昨天日期的 5 元组缓存 → stale, 必须重拉。"""
        import datetime
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        assert df_module._cache_stale("x", (1.0, [], 68, 4760, yesterday)) is True

    def test_today_cache_is_fresh(self):
        """今天日期的缓存 → 不 stale, 可走增量续拉。"""
        assert df_module._cache_stale("x", (1.0, [], 68, 4760, df_module._cache_day())) is False

    def test_fetch_after_stale_cache_returns_full_data(self):
        """真实链路: 塞入昨天残留缓存后调用, 应返回全天数据(>1000 条)而非残留。"""
        import time
        code = _tencent_code(Symbol.parse("002361", "CN"))
        assert code is not None
        yesterday = (__import__("datetime").date.today() - __import__("datetime").timedelta(days=1)).isoformat()
        df_module._TICKS_CACHE[code] = (
            time.time() - 100, [{"d": "B", "amt": 100, "t": "15:18:00"}], 68, 4760, yesterday,
        )
        ticks = _fetch_all_ticks(code)
        assert len(ticks) > 1000  # 全天数据, 而非 1 条残留
        # 缓存应已更新为今天
        assert df_module._TICKS_CACHE[code][4] == df_module._cache_day()


class TestDarkFlowV5:
    def test_real_compute(self):
        """真实数据: 002361 应返回完整结构(盘中/盘后均可)。"""
        r = compute_dark_flow(Symbol.parse("002361", "CN"))
        assert r is not None
        assert "dark_net" in r and "signal" in r
        assert "big_net" in r and "small_net" in r
        assert "segments" in r and "strong_buy_zones" in r
        # 2026-08-12: 弹性断言 —— 盘中刚开盘可能只有几十笔, 盘后才是全天量
        assert r["tick_count"] > 0

    def test_tick_full_coverage(self):
        """逐笔应覆盖交易时段(盘后含尾盘, 盘中至少非空)。"""
        code = _tencent_code(Symbol.parse("002361", "CN"))
        assert code is not None
        ticks = _fetch_all_ticks(code)
        assert len(ticks) > 0
        # 盘后(15:30+)才要求全天覆盖; 盘中只要求有数据
        import datetime
        now = datetime.datetime.now().strftime("%H:%M")
        if now >= "15:30" or now < "09:25":
            assert len(ticks) > 1000
            assert any(tk["t"] >= "14:30" for tk in ticks)

    def test_amount_matches_daily(self):
        """逐笔总金额应≈全天成交额。"""
        from marketdata.vendors.tencent import TencentQuoteVendor
        code = _tencent_code(Symbol.parse("002361", "CN"))
        assert code is not None
        ticks = _fetch_all_ticks(code)
        total = sum(tk["amt"] for tk in ticks)
        q = TencentQuoteVendor().fetch([Symbol.parse("002361", "CN")], {})[0]
        turnover = q.turnover or 0
        if turnover > 0:
            assert abs(total - turnover) / turnover < 0.05

    def test_price_zones(self):
        """分价表吸筹/抛压区应存在(神剑: 低位强买区+开盘抛压)。"""
        r = compute_dark_flow(Symbol.parse("002361", "CN"))
        assert r is not None
        assert "strong_buy_zones" in r
        # 竞买率应合理(0-100)
        for z in r["strong_buy_zones"]:
            assert 0 <= z["ratio"] <= 100


class TestClassifySplit:
    """2026-08-31 拆单分类修复: 位置(套牢/获利)为主判据, 价格方向降级辅助。
    原逻辑"涨中卖+获利区=主力派发"要求 price_dir=up, 主力高位出货时价格横盘/微跌
    被误判散户, 暗盘只剩散户没有主力。修复后获利区卖=主力派发、套牢区买=主力抄底。"""

    def _seq(self, d, p0, p1, amt=50e4):
        return [
            {"d": d, "amt": amt, "price": p0, "t": "10:00:00"},
            {"d": d, "amt": amt, "price": p1, "t": "10:00:03"},
        ]

    def test_profit_zone_sell_flat_is_distribution(self):
        """获利区横盘卖出 = 主力派发(contrarian=True), 不再要求涨中卖。"""
        g = df_module._classify_split(self._seq("S", 10.52, 10.52), 10.45)
        assert g["contrarian"] is True
        assert g["reason"] == "主力派发"

    def test_profit_zone_sell_down_is_distribution(self):
        """获利区微跌卖出 = 主力派发(高位压着卖)。"""
        g = df_module._classify_split(self._seq("S", 10.51, 10.50), 10.45)
        assert g["contrarian"] is True
        assert g["reason"] == "主力派发"

    def test_trapped_zone_buy_is_accumulation(self):
        """套牢区买入 = 主力抄底(contrarian=True)。"""
        g = df_module._classify_split(self._seq("B", 10.40, 10.39), 10.45)
        assert g["contrarian"] is True
        assert g["reason"] == "主力抄底"

    def test_trapped_zone_sell_up_is_retail_unwind(self):
        """套牢区涨中卖出 = 散户解套(contrarian=False)。"""
        g = df_module._classify_split(self._seq("S", 10.40, 10.41), 10.45)
        assert g["contrarian"] is False
        assert g["reason"] == "散户解套"

    def test_trapped_zone_sell_down_is_retail_cut(self):
        """套牢区跌中卖出 = 散户割肉(contrarian=False)。"""
        g = df_module._classify_split(self._seq("S", 10.40, 10.39), 10.45)
        assert g["contrarian"] is False
        assert g["reason"] == "散户割肉"

    def test_profit_zone_buy_is_retail_chase(self):
        """获利区买入 = 散户追涨(contrarian=False)。"""
        g = df_module._classify_split(self._seq("B", 10.52, 10.53), 10.45)
        assert g["contrarian"] is False
        assert g["reason"] == "散户追涨"


class TestJudgeSignal:
    def test_inflow_tail(self):
        assert "吸筹" in _judge_signal(8000e4, 8000e4, 3000e4, 5000e4, -5000e4,
                                       {"tail": 2000e4, "morning": 0, "mid": 0, "afternoon": 0}, 0.4, [{"price": 11.0}], [])

    def test_outflow(self):
        assert "流出" in _judge_signal(-9000e4, -9000e4, -5000e4, -4000e4, 5000e4,
                                       {"tail": -1000e4, "morning": 0, "mid": 0, "afternoon": 0}, 0.3, [], [])
        # 净流出但参与度高 = 洗盘吸筹
        assert "吸筹" in _judge_signal(-9000e4, -9000e4, -5000e4, -4000e4, 5000e4,
                                       {"tail": -1000e4, "morning": 0, "mid": 0, "afternoon": 0}, 0.3, [], [],
                                       0, 0, 40, 50)

    def test_watch(self):
        assert "平衡" in _judge_signal(100e4, 100e4, 50e4, 50e4, 0,
                                       {"tail": 0, "morning": 0, "mid": 0, "afternoon": 0}, 0.3, [], [])
