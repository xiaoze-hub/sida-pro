# -*- coding: utf-8 -*-
"""main_net_ratio → main_net_vol 语义/命名修正回归 (2026-09-30)。

背景: thsdk DDE「主力净量」**不是百分比/占比**(实测出现 2131 之类 >100 的值),
旧字段名 `main_net_ratio` 及注释「主力净量占比」会误导下游按百分比渲染。
其精确量纲(股/手)未在冻结口径矩阵 `docs/_frozen/caliber_matrix.md` 登记 →
本轮不臆测单位, 以中性名 `main_net_vol` 暴露, 旧名 `main_net_ratio` 保留为兼容别名(同值)。

本测试钉住生产者的字段契约与「无占比」措辞, 防止回退。
"""
from __future__ import annotations

import inspect

import pandas as pd

from src.core.dark_fund_scan import scan_dark_fund_top


class _FakeL2:
    """最小 thsdk 替身: 提供代码表 + 批量 DDE 大单流。"""

    def get_stock_cn_lists(self):
        return pd.DataFrame(
            [
                {"代码": "USHA600519", "名称": "贵州茅台"},
                {"代码": "USZA002361", "名称": "神剑股份"},
            ]
        )

    def get_dde_flow(self, codes, market="USHA", detail=False):
        rows = []
        for c in str(codes).split(","):
            rows.append(
                {
                    "代码": f"{market}{c}",
                    "主力净流入": 1.0e7,
                    "主力净量": 2131.0,  # >100: 证明它是净量原始值而非百分比
                    "总金额": 5.0e8,
                }
            )
        return pd.DataFrame(rows)


def test_scan_emits_canonical_main_net_vol_and_deprecated_alias():
    res = scan_dark_fund_top(top_n=5, markets=("USHA",), l2=_FakeL2())
    assert res["top"], "榜单不应为空"
    row = res["top"][0]
    # 规范字段名
    assert row["main_net_vol"] == 2131.0
    # 旧名保留为兼容别名(同值), 不破坏旧快照消费者
    assert row["main_net_ratio"] == row["main_net_vol"]
    # caliber 仍为 ths(非方向判定口径), 未受影响
    assert res["caliber"] == "ths"


def test_int32_sentinel_filtered_for_both_keys():
    class L2(_FakeL2):
        def get_dde_flow(self, codes, market="USHA", detail=False):
            return pd.DataFrame(
                [
                    {
                        "代码": f"{market}{codes}",
                        "主力净流入": 1.0e7,
                        "主力净量": 2_147_483_648,  # 哨兵
                        "总金额": 5.0e8,
                    }
                ]
            )

    res = scan_dark_fund_top(top_n=5, markets=("USHA",), l2=L2())
    row = res["top"][0]
    assert row["main_net_vol"] is None
    assert row["main_net_ratio"] is None


def test_producers_have_no_misleading_ratio_wording():
    """生产者源码不得再出现误导性「主力净量占比」措辞。"""
    from src.core import dark_fund_scan
    from src.web.api import thsdk_extended

    src = inspect.getsource(dark_fund_scan) + inspect.getsource(thsdk_extended)
    assert "主力净量占比" not in src
    assert "main_net_vol" in src


def test_thsdk_l2_producer_exposes_vol_and_alias():
    """thsdk_l2 源码: 规范键 main_net_vol + 兼容别名 main_net_ratio + 非百分比标注。

    注: data_source/thsdk_l2.py 在模块顶层 `from thsdk import THS`(可选私有依赖),
    未安装 thsdk 的环境无法 import ⇒ 本测试按源码文本断言, 不依赖运行时环境。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "data_source" / "thsdk_l2.py").read_text(
        encoding="utf-8"
    )
    assert '"main_net_vol": main_net_vol' in src
    assert '"main_net_ratio": main_net_vol' in src
    assert "非百分比" in src
    # 不再宣称「主力净量(占比)」
    assert "主力净量(占比)" not in src
