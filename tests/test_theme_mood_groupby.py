# -*- coding: utf-8 -*-
"""PG 严格 GROUP BY: theme_mood board 轮动补全查询不得 GroupingError。

SQLite 宽松会静默过, PG 会 500 → 题材情绪页整页空白(2026-09-18 生产实测)。
"""
import inspect

from src.web.api import theme_mood as tm


def test_group_by_includes_non_agg_columns():
    src = inspect.getsource(tm._board_data)
    assert "GROUP BY block_code, block_name, block_type" in src
    # 旧写法(仅 GROUP BY block_code)不得回归
    assert "GROUP BY block_code\"" not in src.replace(" ", "")
    assert "GROUP BY block_code " not in src
    assert "GROUP BY block_code," in src
