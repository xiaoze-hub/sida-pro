"""交易所异常波动异动监控测试 (任务 C, 2026-08-24)。

覆盖:
- 板判定 (主板/创业板/科创板/北交所/未知)
- 基准指数映射 (沪 vs 深 / 创业板 vs 主板)
- 异动规则表 (3 日板异阈值 + 严重异动阈值)
- 累计涨跌幅计算 (normal / 数据不足 / 边界)
- 偏离值 = 个股累计 - 指数累计
- 接近度分级 (triggered / edge / watch / normal / unknown)
- 全套 analyze_abnormal_moves + 边界 (klines 缺失/异常隔离)
- 批量 analyze_for_symbols + 倒序 + 阈值过滤
- 单股分析 mock klines 注入 (避免联网)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import pytest

from src.core import abnormal_moves as am


# ---- mock klines -------------------------------------------------------

@dataclass
class _FakeBar:
    date: str = "2026-08-01"
    open: float = 10.0
    close: float = 10.0
    high: float = 10.5
    low: float = 9.8
    volume: float = 1000.0


def _fake_kline(close: float, date: str = "2026-08-01") -> _FakeBar:
    return _FakeBar(date=date, open=close, close=close, high=close, low=close)


def _scale(closes: Iterable[float], start: float = 10.0) -> list[float]:
    """把等差 closes 转成想要的走势 (整体抬高到 start 附近)."""
    base = start / closes[0] if closes else 1.0
    return [round(c * base, 4) for c in closes]


def _inject_fake_kline_collector(
    monkeypatch,
    *,
    stock_map: dict[str, list[float]],
    index_map: dict[str, list[float]],
):
    """替换 KlineCollector.get_klines 与 get_index_klines, 返回 FakeBar 列表."""
    from src.collectors import kline_collector as kc

    def _build(closes):
        out = []
        for i, c in enumerate(closes):
            d = f"2026-08-{(i % 28) + 1:02d}"
            out.append(_FakeBar(date=d, open=c, close=c, high=c, low=c))
        return out

    class _FakeColl:
        def __init__(self, market=None):
            self.market = market

        def get_klines(self, symbol, days=60):
            return _build(stock_map.get(symbol, []))

    # 同时 patch 模块绑定 (am.KlineCollector) 与原始模块 (kc.KlineCollector)
    # 否则 abnormal_moves 模块顶部 import 的 KlineCollector 不被覆盖.
    monkeypatch.setattr(kc, "KlineCollector", _FakeColl)
    monkeypatch.setattr(am, "KlineCollector", _FakeColl)
    monkeypatch.setattr(
        kc,
        "get_index_klines",
        lambda code, market, days=60: _build(index_map.get(code, [])),
    )
    monkeypatch.setattr(
        am,
        "get_index_klines",
        lambda code, market, days=60: _build(index_map.get(code, [])),
    )


# ---- 1. 板判定 ----------------------------------------------------------

class TestBoardOf:
    @pytest.mark.parametrize("code,expected", [
        ("600519", "main"),   # 沪主板
        ("601318", "main"),
        ("000001", "main"),   # 深主板
        ("002361", "main"),   # 深主板
        ("300750", "cyb"),    # 创业板
        ("688981", "star"),   # 科创板
        ("688111", "star"),
        ("830799", "bse"),    # 北交所 8 开头
        ("430047", "bse"),    # 北交所 4 开头
        ("920566", "bse"),    # 北交所 92 段
        ("", "unknown"),
        ("12345", "unknown"),
        ("abc", "unknown"),
        ("999999", "unknown"),  # 999 开头属于 99 (老 B 股), 不是 90, 不算沪主板
    ])
    def test_board_classification(self, code, expected):
        assert am.board_of(code) == expected


# ---- 2. 基准指数 ------------------------------------------------------

class TestBenchmarkFor:
    def test_sh_main_uses_shanghai(self):
        bm = am.benchmark_for("600519")
        assert bm["code"] == "000001"
        assert bm["name"] == "上证指数"
        assert bm["board"] == "main"

    def test_sz_main_uses_shenzhen_component(self):
        bm = am.benchmark_for("002361")
        assert bm["code"] == "399001"
        assert bm["name"] == "深证成指"

    def test_chinext_uses_chinext_index(self):
        bm = am.benchmark_for("300750")
        assert bm["code"] == "399006"

    def test_star_uses_shanghai(self):
        bm = am.benchmark_for("688981")
        assert bm["code"] == "000001"
        assert bm["board"] == "star"

    def test_bse_uses_bj_50(self):
        bm = am.benchmark_for("830799")
        assert bm["code"] == "899050"
        assert bm["board"] == "bse"


# ---- 3. 规则表 ---------------------------------------------------------

class TestRulesFor:
    @pytest.mark.parametrize("board,expected_3d_key", [
        ("main", "3d_main"),
        ("cyb", "3d_cyb"),
        ("star", "3d_star"),
        ("bse", "3d_bse"),
    ])
    def test_3d_thresholds_per_board(self, board, expected_3d_key):
        rules = am.rules_for(board)
        r3 = next(r for r in rules if r.window == 3)
        assert r3.key == expected_3d_key
        # 一致性: 主±20, 创/科±30, 北±40
        assert r3.up_threshold == r3.down_threshold  # 都用绝对值
        if board == "main":
            assert r3.up_threshold == 20.0
        elif board in ("cyb", "star"):
            assert r3.up_threshold == 30.0
        elif board == "bse":
            assert r3.up_threshold == 40.0

    def test_severe_rules_shared_across_boards(self):
        for board in ("main", "cyb", "star", "bse"):
            rules = am.rules_for(board)
            severe = [r for r in rules if r.severity == "severe"]
            assert len(severe) == 2
            keys = sorted([r.key for r in severe])
            assert keys == ["10d_all", "30d_all"]

    def test_severe_thresholds(self):
        rules = am.rules_for("main")
        rule10 = next(r for r in rules if r.key == "10d_all")
        assert rule10.up_threshold == 100.0
        assert rule10.down_threshold == 50.0  # 负向更严
        rule30 = next(r for r in rules if r.key == "30d_all")
        assert rule30.up_threshold == 200.0
        assert rule30.down_threshold == 70.0  # 负向更严

    def test_unknown_board_returns_only_severe(self):
        rules = am.rules_for("unknown")
        assert all(r.severity == "severe" for r in rules)
        assert len(rules) == 2


# ---- 4. 累计涨跌幅 ----------------------------------------------------

class TestCumulativeChange:
    def test_normal_window(self):
        # 10 -> 13 (4 步), 涨幅 +30% (用 -need=-4 锚定)
        pct, used = am.cumulative_change([10, 11, 12, 12, 13], window=3)
        # need=4; closes[-4]=11; closes[-1]=13; (13/11-1)*100 ≈ 18.18
        assert pct == 18.18
        assert used == 3

    def test_window_zero(self):
        pct, used = am.cumulative_change([10, 11], window=0)
        assert pct == 0.0
        assert used == 0

    def test_window_negative_returns_zero(self):
        pct, used = am.cumulative_change([10, 11], window=-1)
        assert pct == 0.0

    def test_insufficient_data_uses_partial(self):
        # 需要 4 根; 只有 3 根 → 用 [0..-1] 作近似, used = len-1
        pct, used = am.cumulative_change([10, 11, 12], window=3)
        # anchor=10, end=12 -> +20%
        assert pct == 20.0
        assert used == 2

    def test_empty_closes_returns_none(self):
        assert am.cumulative_change([], 3) == (None, 0)

    def test_single_close_returns_none(self):
        assert am.cumulative_change([10.0], 3) == (None, 1)

    def test_anchor_zero_returns_none(self):
        # 全部 close 为 0, 锚点非正 -> pct=None, used 为已有长度
        assert am.cumulative_change([0.0, 0.0, 0.0], 3) == (None, 3)
        # 单元素 0 也返回 None
        assert am.cumulative_change([0.0], 3) == (None, 1)


# ---- 5. 偏离 + 接近度 -------------------------------------------------

class TestDeviationAndProximity:
    def test_deviation_subtracts_index(self):
        # 个股 +20% 指数 +10% -> 偏离 +10%
        s = [10, 11, 12, 12.5, 13]
        # need=4 -> anchor=11, end=13, pct=18.18
        idx = [100, 102, 105, 108, 110]
        # need=4 -> anchor=102, end=110, pct=(110/102-1)*100 = 7.84
        dev = am.compute_deviation(s, idx, window=3)
        assert dev["available"] is True
        assert dev["deviation_pct"] == round(dev["stock_pct"] - dev["index_pct"], 2)
        assert dev["deviation_pct"] == pytest.approx(round(18.18 - 7.84, 2), abs=0.01)

    def test_deviation_insufficient_returns_none(self):
        dev = am.compute_deviation([10.0], [100.0], window=3)
        assert dev["available"] is False
        assert dev["deviation_pct"] is None

    def test_proximity_up_uses_up_threshold(self):
        rule = am.AbnormalRule("t", 3, "main", 20.0, 20.0, "normal")
        assert am.proximity_of(15.0, rule) == 0.75
        assert am.proximity_of(20.0, rule) == 1.0
        assert am.proximity_of(25.0, rule) == 1.25

    def test_proximity_down_uses_down_threshold(self):
        rule = am.AbnormalRule("t", 3, "main", 20.0, 50.0, "severe")  # 负向更严
        assert am.proximity_of(-40.0, rule) == 0.8
        assert am.proximity_of(-50.0, rule) == 1.0

    def test_proximity_zero_returns_zero(self):
        rule = am.AbnormalRule("t", 3, "main", 20.0, 20.0, "normal")
        assert am.proximity_of(0.0, rule) == 0.0

    def test_proximity_none_returns_none(self):
        rule = am.AbnormalRule("t", 3, "main", 20.0, 20.0, "normal")
        assert am.proximity_of(None, rule) is None

    @pytest.mark.parametrize("prox,expected", [
        (0.0, "normal"),
        (0.49, "normal"),
        (0.5, "watch"),
        (0.69, "watch"),
        (0.7, "edge"),
        (0.99, "edge"),
        (1.0, "triggered"),
        (1.5, "triggered"),
        (None, "unknown"),
    ])
    def test_status_classification(self, prox, expected):
        assert am.status_of(prox) == expected


# ---- 6. worst 选取 ----------------------------------------------------

class TestWorstWindow:
    def test_selects_max_proximity(self):
        rules = [
            {"rule_key": "a", "window": 3, "proximity": 0.6, "status": "watch"},
            {"rule_key": "b", "window": 10, "proximity": 1.2, "status": "triggered"},
            {"rule_key": "c", "window": 30, "proximity": 0.95, "status": "edge"},
        ]
        worst = am.worst_window(rules)
        assert worst is not None
        assert worst["rule_key"] == "b"

    def test_returns_none_when_all_unavailable(self):
        rules = [
            {"rule_key": "a", "window": 3, "proximity": None, "status": "unknown"},
        ]
        assert am.worst_window(rules) is None

    def test_empty_list_returns_none(self):
        assert am.worst_window([]) is None


# ---- 7. 全套 analyze (mock klines) -------------------------------------

class TestAnalyzeAbnormalMoves:
    def test_main_no_move(self, monkeypatch):
        # 主+0%, 指数+0% -> 偏离 0 -> 全部 normal
        closes = [10.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"600519": closes},
            index_map={"000001": closes[:]},
        )
        r = am.analyze_abnormal_moves("600519")
        assert r["board"] == "main"
        assert r["available"] is True
        # 全部 proximity = 0/阈值 = 0, status=normal
        for w in r["windows"]:
            if w["window"] == 3:
                assert w["deviation_pct"] == 0.0
                assert w["status"] == "normal"

    def test_main_3d_almost_triggered(self, monkeypatch):
        # 主 +19, 指数 +0 -> 偏离 +19, 阈值 20 -> proximity = 0.95 (edge)
        # 35 根个股: 前 30 根 10, 后 5 根 11.9 (累计 +19%)
        # closes[-4]=10, closes[-1]=11.9 -> +19%
        stock = [10.0] * 32 + [11.9] * 3
        index = [1000.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"002361": stock},
            index_map={"399001": index},
        )
        r = am.analyze_abnormal_moves("002361")
        w3 = next(w for w in r["windows"] if w["window"] == 3)
        assert w3["direction"] == "up"
        assert w3["deviation_pct"] == 19.0
        assert w3["proximity"] == pytest.approx(0.95, abs=0.01)
        assert w3["status"] == "edge"
        assert r["status"] == "edge"

    def test_main_3d_triggered(self, monkeypatch):
        # 主 +25, 指数 +0 -> 偏离 +25, 阈值 20 -> 1.25 (triggered)
        stock = [10.0] * 32 + [12.5] * 3
        index = [1000.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"002361": stock},
            index_map={"399001": index},
        )
        r = am.analyze_abnormal_moves("002361")
        assert r["status"] == "triggered"
        # worst 应该是 3d_main
        assert r["worst"]["rule_key"] == "3d_main"
        assert r["worst"]["deviation_pct"] == 25.0

    def test_chinext_3d_thresholds_30pct(self, monkeypatch):
        stock = [10.0] * 32 + [12.5] * 3
        index = [2000.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"300750": stock},
            index_map={"399006": index},
        )
        r = am.analyze_abnormal_moves("300750")
        w3 = next(w for w in r["windows"] if w["window"] == 3)
        assert w3["up_threshold"] == 30.0
        assert w3["deviation_pct"] == 25.0
        assert w3["proximity"] == pytest.approx(25.0 / 30.0, abs=0.01)
        assert w3["status"] == "edge"

    def test_bse_3d_thresholds_40pct(self, monkeypatch):
        # closes[-4]=10, closes[-1]=13 -> +30% over 3-bar
        stock = [10.0] * 32 + [13.0] * 3
        index = [1000.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"830799": stock},
            index_map={"899050": index},
        )
        r = am.analyze_abnormal_moves("830799")
        assert r["board"] == "bse"
        w3 = next(w for w in r["windows"] if w["window"] == 3)
        assert w3["up_threshold"] == 40.0
        assert w3["deviation_pct"] == 30.0
        assert w3["proximity"] == pytest.approx(0.75, abs=0.01)
        assert w3["status"] == "edge"

    def test_severe_10d_triggered_first(self, monkeypatch):
        # 10 日累计 +209, 远超 +100 阈值
        closes = [10.0] * 25 + [11.2, 12.5, 14.0, 15.7, 17.6, 19.7, 22.0, 24.6, 27.6, 30.9]
        index = [1000.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"002361": closes},
            index_map={"399001": index},
        )
        r = am.analyze_abnormal_moves("002361")
        # 主最接近的是 10d_all (proximity ~= 209/100 = 2.09)
        # 30d_all 也超 200% 阈值
        severe_10 = next(w for w in r["windows"] if w["window"] == 10)
        assert severe_10["severity"] == "severe"
        assert severe_10["status"] == "triggered"

    def test_returns_unknown_when_no_data(self, monkeypatch):
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"600519": []},
            index_map={"000001": []},
        )
        r = am.analyze_abnormal_moves("600519")
        assert r["available"] is False
        assert r["status"] == "unknown"
        assert r["proximity"] is None
        for w in r["windows"]:
            assert w["available"] is False

    def test_index_failure_isolated(self, monkeypatch):
        # 个股有数据, 指数返回空 -> 全部窗口 dev_pct=None (available=False)
        stock = [10.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"002361": stock},
            index_map={"399001": []},
        )
        r = am.analyze_abnormal_moves("002361")
        assert r["available"] is False
        for w in r["windows"]:
            assert w["deviation_pct"] is None
            assert w["available"] is False

    def test_star_uses_shanghai_index(self, monkeypatch):
        # 科创板 688981, 指数用 000001 (上证)
        stock = [10.0] * 32 + [13.0] * 3
        index = [3000.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"688981": stock},
            index_map={"000001": index},
        )
        r = am.analyze_abnormal_moves("688981")
        assert r["benchmark"]["code"] == "000001"
        assert r["benchmark"]["name"] == "上证指数"
        # +30% 偏离, 阈值 30, proximity=1.0 -> triggered
        w3 = next(w for w in r["windows"] if w["window"] == 3)
        assert w3["status"] == "triggered"


# ---- 8. 批量 + 排序 ---------------------------------------------------

class TestAnalyzeForSymbols:
    def test_filters_below_threshold(self, monkeypatch):
        stock_high = [10.0] * 32 + [15.0] * 3  # 偏离 +50 (triggered)
        stock_low = [10.0] * 32 + [10.5] * 3   # 偏离 +5 (normal, 0.25)
        index = [1000.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"600000": stock_high, "600001": stock_low},
            index_map={"000001": index},
        )
        results = am.analyze_for_symbols(["600000", "600001"], min_proximity=0.5)
        assert len(results) == 1
        assert results[0]["symbol"] == "600000"
        assert results[0]["status"] == "triggered"

    def test_sorted_by_proximity_desc(self, monkeypatch):
        stock1 = [10.0] * 32 + [12.0] * 3   # 偏离 +20 (1.0)
        stock2 = [10.0] * 32 + [13.0] * 3   # 偏离 +30 (1.5)
        index = [1000.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"600000": stock1, "600001": stock2},
            index_map={"000001": index},
        )
        results = am.analyze_for_symbols(["600000", "600001"], min_proximity=0.5)
        assert [r["symbol"] for r in results] == ["600001", "600000"]

    def test_analyzer_failure_isolated(self, monkeypatch):
        # 一只成功, 一只 raise, 整体不应崩溃
        stock = [10.0] * 32 + [15.0] * 3
        index = [1000.0] * 35

        def fake_analyze(symbol):
            if symbol == "600002":
                raise RuntimeError("boom")
            # 走真实 analyze, 但注入 klines
            monkeypatch.setattr(am, "_klines_to_closes", lambda ks: [10.0, 15.0])
            return am.analyze_abnormal_moves("600000")

        # 简化: 直接 monkeypatch analyzer
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"600000": stock, "600001": stock},
            index_map={"000001": index},
        )

        def fake_an(symbol):
            if symbol == "600000":
                raise RuntimeError("boom")
            return am.analyze_abnormal_moves(symbol)

        results = am.analyze_for_symbols(["600000", "600001"], min_proximity=0.5, analyzer=fake_an)
        assert len(results) == 1
        assert results[0]["symbol"] == "600001"

    def test_skips_unknown_results(self, monkeypatch):
        # 数据都不可用, 应返回空
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"600000": []},
            index_map={"000001": []},
        )
        results = am.analyze_for_symbols(["600000"], min_proximity=0.5)
        assert results == []


# ---- 9. collect_idempotency / 内部不变量 ------------------------------

class TestIntegrityChecks:
    def test_windows_always_has_three_entries_per_main(self):
        # 主板总是 1 条 3 日 + 2 条严重
        rules = am.rules_for("main")
        assert len(rules) == 3
        assert [r.severity for r in rules] == ["normal", "severe", "severe"]

    def test_worst_is_subset_of_windows(self, monkeypatch):
        stock = [10.0] * 32 + [12.0] * 3
        index = [1000.0] * 35
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"002361": stock},
            index_map={"399001": index},
        )
        r = am.analyze_abnormal_moves("002361")
        worst_keys = [w["rule_key"] for w in r["windows"]]
        assert r["worst"]["rule_key"] in worst_keys

    def test_benchmark_always_present(self, monkeypatch):
        # 即使无数据也要给基准指数
        _inject_fake_kline_collector(
            monkeypatch,
            stock_map={"600000": []},
            index_map={"000001": []},
        )
        r = am.analyze_abnormal_moves("600000")
        assert r["benchmark"]["code"]
        assert r["benchmark"]["name"]
