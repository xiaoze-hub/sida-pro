"""A-1 现状钉住(2026-09-28, 审计任务交付前补的回归防护)。

现状说明: shareholders/dividend 的 TQ 备源(vendor + registry 注册 + seed 行)已于
2026-09-24 合入 main(见 tests/test_tq_shareholders_dividend.py 37 例)。
本文件钉住的是**备源接线本身**(seed 行存在 + 优先级排序), 即"A-1 真的接上了"
这个事实, 防止后续改动把 TQ seed 行删掉或把优先级排到付费源前面。

不重复 37 例的口径测试; 全离线, 不碰真实网关。
"""
from __future__ import annotations

from src.bootstrap.datasources import DATA_SOURCE_SEEDS


def _seeds(type_: str) -> dict[str, dict]:
    """type → {provider: seed 行}。"""
    return {s["provider"]: s for s in DATA_SOURCE_SEEDS if s.get("type") == type_}


class TestShareholdersTqSeed:
    def test_tq_seed行存在且enabled(self):
        seeds = _seeds("shareholders")
        assert "tq" in seeds, "TQ 股东备源 seed 行缺失(A-1 未接/被删)"
        assert seeds["tq"]["enabled"] is True

    def test_优先级_东财先于TQ先于智兔(self):
        seeds = _seeds("shareholders")
        assert seeds["eastmoney"]["priority"] < seeds["tq"]["priority"]
        assert seeds["tq"]["priority"] < seeds["zhitu"]["priority"]

    def test_主源是东财不是TQ(self):
        """TQ 是备源: 东财(0)仍排第一, TQ 不抢主源位。"""
        seeds = _seeds("shareholders")
        prios = {p: s["priority"] for p, s in seeds.items()}
        assert min(prios, key=lambda p: prios[p]) == "eastmoney"


class TestDividendTqSeed:
    def test_tq_seed行存在且enabled(self):
        seeds = _seeds("dividend")
        assert "tq" in seeds, "TQ 分红备源 seed 行缺失(A-1 未接/被删)"
        assert seeds["tq"]["enabled"] is True

    def test_优先级_东财先于TQ先于智兔(self):
        seeds = _seeds("dividend")
        assert seeds["eastmoney"]["priority"] < seeds["tq"]["priority"]
        assert seeds["tq"]["priority"] < seeds["zhitu"]["priority"]
        # 智兔是付费源且实测反复 429, 必须排在 TQ 之后
        assert seeds["zhitu"]["priority"] >= 5

    def test_主源是东财不是TQ(self):
        seeds = _seeds("dividend")
        prios = {p: s["priority"] for p, s in seeds.items()}
        assert min(prios, key=lambda p: prios[p]) == "eastmoney"


class TestRegistryHasTqVendors:
    """registry 必须已注册 TQ vendor(包层真相源), 否则 seed 行无 vendor 可路由。"""

    def test_shareholders注册tq(self):
        from marketdata.registry import PACKAGE_VENDORS_BY_TYPE

        assert "tq" in PACKAGE_VENDORS_BY_TYPE["shareholders"]

    def test_dividend注册tq(self):
        from marketdata.registry import PACKAGE_VENDORS_BY_TYPE

        assert "tq" in PACKAGE_VENDORS_BY_TYPE["dividend"]
