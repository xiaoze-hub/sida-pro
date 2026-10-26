# -*- coding: utf-8 -*-
"""配方工具白名单的规格守卫。

对应 specs/harness/recipe_whitelist.md。
"""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.harness.recipes import filter_schemas


def test_whitelist_miss_fails_closed():
    """@spec RECIPE-010
    白名单一个都没命中时必须收紧到 0 工具(fail-closed), 不能回退全量。
    """
    schemas = [{"type": "function", "function": {"name": "a"}}]
    assert filter_schemas(schemas, frozenset({"nonexistent"})) == []


def test_empty_whitelist_means_no_filter():
    """@spec RECIPE-011
    白名单为 None / 空集时不过滤(返回全量) —— 这是显式的「不用配方」路径。
    """
    schemas = [{"type": "function", "function": {"name": "a"}},
               {"type": "function", "function": {"name": "b"}}]
    assert filter_schemas(schemas, None) == schemas
    assert filter_schemas(schemas, frozenset()) == schemas
    assert filter_schemas(schemas, set()) == schemas


def test_whitelist_miss_logs_unknown_names(caplog):
    """@spec RECIPE-012
    白名单一个都没命中时必须记 warning 并列出没对上的工具名。
    """
    schemas = [{"type": "function", "function": {"name": "a"}}]
    with caplog.at_level(logging.WARNING, logger="src.harness.recipes"):
        filter_schemas(schemas, frozenset({"nonexistent_tool"}))
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "白名单全 miss 必须留 warning"
    assert any("nonexistent_tool" in w for w in warnings), (
        f"warning 里要能看出是哪些工具名没对上: {warnings}"
    )
