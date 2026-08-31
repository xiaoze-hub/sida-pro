"""策略库 scan 批量选股测试。"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from src.web.api.strategies import _load_strategies, _evaluate_strategy

def _quote_dict(symbol, name, price, change_pct, volume_ratio, turnover_rate,
                amount, pe_ratio, pb_ratio, total_market_value):
    return {
        "symbol": symbol, "market": "CN", "name": name,
        "current_price": price, "change_pct": change_pct,
        "volume_ratio": volume_ratio, "turnover_rate": turnover_rate,
        "amount": amount, "pe_ratio": pe_ratio, "pb_ratio": pb_ratio,
        "total_market_value": total_market_value,
    }

class TestEvaluateStrategy:
    def setup_method(self):
        self.strategies = _load_strategies()

    def test_dual_low_bank_passes(self):
        """低 PE 低 PB 银行股应通过 dual_low 硬过滤。"""
        cfg = self.strategies["dual_low"]
        q = _quote_dict("601166", "兴业银行", 18.0, 1.0, 1.2, 0.8,
                        2e8, 5.0, 0.5, 2500)
        r = _evaluate_strategy(cfg, q, "dual_low", "601166", "CN")
        assert r["passed"] is True
        assert r["score"] > 50
        assert "pe_ttm" not in r["missing_fields"]
        assert "pb_ratio" not in r["missing_fields"]

    def test_dual_low_expensive_stock_fails(self):
        """高 PE 高 PB(茅台)应被 dual_low 过滤掉。"""
        cfg = self.strategies["dual_low"]
        q = _quote_dict("600519", "贵州茅台", 1348.0, 3.0, 1.8, 0.5,
                        5e9, 20.0, 7.0, 16000)
        r = _evaluate_strategy(cfg, q, "dual_low", "600519", "CN")
        assert r["passed"] is False
        # 价格超 80 上限也应记为失败过滤
        assert any(f["field"] == "current_price" for f in r["failed_filters"])

    def test_capital_heat_momentum_passes(self):
        """放量上涨股应通过 capital_heat。"""
        cfg = self.strategies["capital_heat"]
        q = _quote_dict("000001", "平安银行", 12.0, 4.0, 2.5, 3.0,
                        8e8, 6.0, 0.6, 2200)
        r = _evaluate_strategy(cfg, q, "capital_heat", "000001", "CN")
        assert r["passed"] is True

    def test_capital_heat_dead_stock_fails(self):
        """缩量下跌股应被 capital_heat 过滤。"""
        cfg = self.strategies["capital_heat"]
        q = _quote_dict("600000", "浦发银行", 10.0, -2.0, 0.6, 0.3,
                        5e7, 5.0, 0.5, 2800)
        r = _evaluate_strategy(cfg, q, "capital_heat", "600000", "CN")
        assert r["passed"] is False

    def test_pe_ratio_alias_normalized(self):
        """腾讯 pe_ratio 字段应被规范化为 pe_ttm。"""
        cfg = self.strategies["dual_low"]
        q = _quote_dict("601988", "中国银行", 5.8, 0.5, 1.0, 0.4,
                        1e8, 6.0, 0.6, 18000)
        # 只传 pe_ratio(腾讯口径), 不传 pe_ttm
        assert "pe_ratio" in q and "pe_ttm" not in q
        r = _evaluate_strategy(cfg, q, "dual_low", "601988", "CN")
        assert "pe_ttm" not in r["missing_fields"]
        # 高分说明 low_pe 因子生效
        assert any(b["factor"] == "low_pe" for b in r["score_breakdown"])

    def test_dual_low_negative_pe_excluded(self):
        """负 PE(亏损公司)不应通过 dual_low——pe_ttm_min 约束。"""
        cfg = self.strategies["dual_low"]
        q = _quote_dict("688707", "振华新材", 10.86, 1.0, 1.0, 0.8,
                        3e7, -12.84, 1.42, 55)
        r = _evaluate_strategy(cfg, q, "dual_low", "688707", "CN")
        assert r["passed"] is False
        assert any(f["field"] == "pe_ttm" for f in r["failed_filters"])

    def test_missing_eod_fields_marked_and_fail(self):
        """盘后字段缺失 = 无法验证 = 不通过(2026-08-23 P3 保守语义反转)。

        旧语义"缺失仅标注不致命"会让双低策略在无估值数据时变成裸筛,
        已在判断准确性大修中反转; 缺失项仍标注在 missing_fields。
        """
        cfg = self.strategies["dual_low"]
        q = _quote_dict("000002", "万科A", 10.0, 1.0, 1.1, 0.9,
                        3e8, None, None, None)
        r = _evaluate_strategy(cfg, q, "dual_low", "000002", "CN")
        # 估值字段缺失 → 不通过 + missing 标注
        assert r["passed"] is False
        assert "pe_ttm" in r["missing_fields"] or "pb_ratio" in r["missing_fields"]
