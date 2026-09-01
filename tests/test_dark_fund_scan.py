# -*- coding: utf-8 -*-
""".tck/thsdk 全市场暗盘资金 TOP 扫描(A6) 单测: src/core/dark_fund_scan.py

覆盖:
  - 空代码表 → universe=0, top=[]
  - 正常扫描 → 按主力净流入降序取 TOP N, 单位换算(元→万元)
  - 批量分页(>batch_size 时分批)
  - attach_tck_dark: 有 .tck → 附加 tck_dark_net_wan; 无 → tck_available=False
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import dark_fund_scan as ds  # noqa: E402


class FakeL2:
    """mock THSDKL2: 代码表 + 批量 DDE。"""

    def __init__(self, codes=None):
        self._codes = codes or []  # [(ths_code, name)]
        self.batches = []  # 记录每批调用, 验证分页

    def get_stock_cn_lists(self):
        return pd.DataFrame(
            {"代码": [c for c, _ in self._codes], "名称": [n for _, n in self._codes]}
        )

    def get_dde_flow(self, codelist, market="USZA", detail=False):
        self.batches.append((codelist, market))
        codes = codelist.split(",")
        rows = []
        for i, c in enumerate(codes):
            rows.append(
                {
                    "代码": f"{market}{c}",
                    "主力净流入": (100 - i) * 10000.0,  # 元, 降序
                    "主力净量": 0.01 * (i + 1),
                    "总金额": 10000000.0,
                }
            )
        return pd.DataFrame(rows)


def test_empty_codes():
    l2 = FakeL2([])
    r = ds.scan_dark_fund_top(top_n=5, l2=l2)
    assert r["universe"] == 0
    assert r["computed"] == 0
    assert r["top"] == []


def test_scan_sorts_desc_and_converts():
    l2 = FakeL2(
        [
            ("USZA000001", "平安银行"),
            ("USZA000002", "万科A"),
            ("USHA600000", "浦发银行"),
        ]
    )
    r = ds.scan_dark_fund_top(top_n=3, l2=l2)
    assert r["universe"] == 3
    assert r["computed"] == 3
    assert len(r["top"]) == 3
    # 降序: 每批第一只(主力净流入最大)
    first = r["top"][0]
    assert first["main_net_wan"] == pytest.approx(1000000.0 / 1e4)  # 100万 → 100万? 见下
    # 单位: 主力净流入 1000000.0 元 → 100.0 万元
    assert first["main_net_wan"] == pytest.approx(100.0)
    assert first["source"] == "thsdk_dde"
    # 名称从代码表 join
    assert first["name"] in ("平安银行", "万科A", "浦发银行")


def test_scan_batches_pagination():
    codes = [(f"USZA{i:06d}", f"股{i}") for i in range(1, 21)]  # 20 只
    l2 = FakeL2(codes)
    r = ds.scan_dark_fund_top(top_n=5, batch_size=10, l2=l2)
    assert r["universe"] == 20
    assert r["computed"] == 20
    assert len(l2.batches) == 2  # 20 只 / 10 每批 = 2 批
    assert len(r["top"]) == 5


def test_scan_filters_int32_overflow():
    """int32 溢出哨兵值(2^31-1/2^31)应被过滤, 不进榜。"""
    import src.core.dark_fund_scan as m

    class FakeL2Overflow:
        def get_stock_cn_lists(self):
            return pd.DataFrame(
                {
                    "代码": ["USZA000001", "USZA000002", "USZA000003"],
                    "名称": ["正常", "溢出", "正常2"],
                }
            )

        def get_dde_flow(self, codelist, market="USZA", detail=False):
            rows = []
            for c in codelist.split(","):
                if c == "000002":
                    main_net = 2147483648.0  # 溢出哨兵
                else:
                    main_net = 50000.0
                rows.append(
                    {
                        "代码": f"USZA{c}",
                        "主力净流入": main_net,
                        "主力净量": 2147483648.0 if c == "000002" else 0.01,
                        "总金额": 10000000.0,
                    }
                )
            return pd.DataFrame(rows)

    r = m.scan_dark_fund_top(top_n=5, l2=FakeL2Overflow())
    syms = [x["symbol"] for x in r["top"]]
    assert "000002" not in syms  # 溢出值被过滤
    assert len(r["top"]) == 2     # 只剩两只正常
    assert r["computed"] == 2


def test_scan_one_batch_fails_does_not_break(monkeypatch):
    l2 = FakeL2([("USZA000001", "平安银行")])

    def bad_dde(codelist, market="USZA", detail=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(l2, "get_dde_flow", bad_dde)
    r = ds.scan_dark_fund_top(top_n=5, l2=l2)
    assert r["universe"] == 1
    assert r["computed"] == 0
    assert r["top"] == []


# ──────────────────────────── .tck 融合 ────────────────────────────

def test_attach_tck_dark(monkeypatch):
    top = [
        {"symbol": "002361", "name": "神剑", "main_net_wan": 100.0, "source": "thsdk_dde"},
        {"symbol": "600519", "name": "茅台", "main_net_wan": 50.0, "source": "thsdk_dde"},
    ]
    monkeypatch.setattr(
        "src.core.postmarket_review.dark_review_from_tck",
        lambda sym: {
            "symbol": sym,
            "available": sym == "002361",
            "main_net": 20000.0,  # 元
        },
    )
    out = ds.attach_tck_dark(top, ["002361"])
    assert out[0]["tck_available"] is True
    assert out[0]["tck_dark_net_wan"] == pytest.approx(2.0)  # 20000元 → 2万
    # 600519 不在持仓, 不附加 tck 字段
    assert "tck_available" not in out[1]
