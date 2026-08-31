"""拉升段分析测试: 用神剑 002361 真实数据验证判别逻辑。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.rally_analysis import (
    analyze_rallies,
    format_rally_report,
    _build_minutes,
    _segment_stats,
    _judge,
)


class TestRallyAnalysis:
    def test_real_analyze_002361(self):
        """真实数据: 神剑今日应识别出拉升段且输出完整结构。"""
        r = analyze_rallies("002361")
        if r is None:
            pytest.skip("盘前/无数据, 跳过真实数据测试")
        assert r["symbol"] == "002361"
        assert "rallies" in r and "summary" in r
        # 结构字段完整
        for rally in r["rallies"]:
            for k in ("start", "end", "price_up", "amt", "main_net",
                      "retail_net", "buy_ratio", "verdict", "score", "signals"):
                assert k in rally, f"缺字段 {k}"
        # 全天主力净额与 dark_flow 口径一致(±误差)
        assert abs(r["summary"]["main_net_total"]) < 2e8  # 不超过 2 亿, 防口径错乱

    def test_format_report(self):
        """摘要格式化: 不依赖 LLM, 含核心数字。"""
        r = analyze_rallies("002361")
        if r is None:
            pytest.skip("盘前/无数据")
        text = format_rally_report(r)
        assert "拉升段分析" in text
        assert "评分" in text
        assert "主力净" in text

    def test_build_minutes_filters_auction(self):
        """分钟聚合: 竞价单(09:25)剔除, 09:30 后保留。"""
        ticks = [
            {"d": "B", "amt": 1e6, "vol": 5000, "price": 11.9, "t": "09:25:00"},
            {"d": "B", "amt": 5e5, "vol": 3000, "price": 12.0, "t": "09:30:05"},
            {"d": "S", "amt": 3e5, "vol": 2000, "price": 12.1, "t": "09:31:00"},
        ]
        minutes, times = _build_minutes(ticks)
        assert times == ["09:30", "09:31"]
        assert "09:25" not in times

    def test_segment_stats_prefix_match(self):
        """段统计: 分钟起止 "09:48" 应匹配逐笔 "09:48:05"。"""
        ticks = [
            {"d": "B", "amt": 5e5, "vol": 3000, "price": 12.0, "t": "09:48:05"},
            {"d": "B", "amt": 4e5, "vol": 2500, "price": 12.0, "t": "09:48:30"},
            {"d": "S", "amt": 2e5, "vol": 1500, "price": 12.0, "t": "09:49:00"},
            {"d": "B", "amt": 1e5, "vol": 800, "price": 12.0, "t": "09:50:10"},
        ]
        stats = _segment_stats(ticks, "09:48", "09:48")
        assert stats["main_net"] == 9e5 - 0  # 两笔主动买(≥20万) 净 +90万
        assert stats["amt"] == 9e5

    def test_judge_true_rally(self):
        """真拉升: 买占>60% + 主力大额净买 + 拉升后主力留守 → 放量上涨。"""
        stats = {"buy_ratio": 78.0, "main_net": 40_000_000, "retail_net": -500_000}
        post = {"main_net": 5_000_000, "price_change": 0.01}
        j = _judge(stats, post, 0.15)
        assert "放量上涨" in j["verdict"] or "疑似真拉升" in j["verdict"]

    def test_judge_distribute(self):
        """假拉升: 买占低 + 主力净卖 + 拉升后离场 → 拉高出货。"""
        stats = {"buy_ratio": 42.0, "main_net": -1_500_000, "retail_net": 800_000}
        post = {"main_net": -5_000_000, "price_change": -0.08}
        j = _judge(stats, post, 0.10)
        assert "出货" in j["verdict"]
