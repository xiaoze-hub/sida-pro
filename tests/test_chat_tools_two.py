# -*- coding: utf-8 -*-
"""dark_split / orderbook_engine 底层单测:
   dark_split.find_tck_file / dark_flow_from_tck / orderbook_engine.to_ths_code
(原空壳工具包装层测试随该包装层于 W3.3 删除一并移除)
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import dark_split as ds  # noqa: E402
from src.core import orderbook_engine as obe  # noqa: E402


# ---------------------------------------------------------------------------
# to_ths_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("000977", "USZA000977"),
    ("600103", "USHA600103"),
    ("603893", "USHA603893"),
    ("688981", "USHA688981"),
    ("002361", "USZA002361"),
    ("USZA002361", "USZA002361"),   # 已是 thsdk 格式 → 原样
    ("usza002361", "USZA002361"),   # 大小写归一
    ("abc", None),
    ("", None),
])
def test_to_ths_code(raw, expected):
    assert obe.to_ths_code(raw) == expected


def test_to_ths_code_differs_from_tencent_style():
    """两套代码体系不能混用(thsdk USZA vs 腾讯 sz)。"""
    assert obe.to_ths_code("000977") == "USZA000977"
    assert obe.to_ths_code("000977") != "sz000977"


# ---------------------------------------------------------------------------
# find_tck_file
# ---------------------------------------------------------------------------


def test_find_tck_file_no_dir(monkeypatch):
    monkeypatch.delenv("PANWATCH_TCK_DIR", raising=False)
    assert ds.find_tck_file("000977") is None


def test_find_tck_file_hit(monkeypatch, tmp_path):
    (tmp_path / "sz000977_20260831.tck").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))
    assert ds.find_tck_file("000977").endswith("sz000977_20260831.tck")


def test_find_tck_file_filters_by_date(monkeypatch, tmp_path):
    (tmp_path / "sz000977_20260830.tck").write_bytes(b"x")
    (tmp_path / "sz000977_20260831.tck").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))
    assert ds.find_tck_file("000977", "2026-08-31").endswith("20260831.tck")
    assert ds.find_tck_file("000977", "20260830").endswith("20260830.tck")


def test_find_tck_file_date_miss(monkeypatch, tmp_path):
    (tmp_path / "sz000977_20260830.tck").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))
    assert ds.find_tck_file("000977", "2026-09-01") is None


def test_find_tck_file_ignores_other_ext(monkeypatch, tmp_path):
    (tmp_path / "000977.dat").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))
    assert ds.find_tck_file("000977") is None


# ---------------------------------------------------------------------------
# dark_flow_from_tck
# ---------------------------------------------------------------------------


def _trades():
    """主动买 3 笔 / 主动卖 1 笔。金额: 买 30万+40万+5万, 卖 20万。"""
    return [
        {"dir": "B", "amt": 300000.0},   # 明盘(>30万)
        {"dir": "B", "amt": 400000.0},   # 明盘(>30万)
        {"dir": "B", "amt": 50000.0},    # 小单
        {"dir": "S", "amt": 200000.0},   # 小单(<30万)
    ]


def test_dark_flow_from_tck_active_side():
    r = ds.dark_flow_from_tck(_trades(), [])
    assert r["active_buy"] == 750000.0
    assert r["active_sell"] == 200000.0
    assert r["active_net"] == 550000.0


def test_dark_flow_from_tck_passive_side():
    """a32 指向主动卖成交 → 该委托被主动卖吃掉 → 挂买单(被动买)。"""
    orders = [
        {"a28": 0, "a32": 123, "amt": 100000.0},   # 被动买
        {"a28": 456, "a32": 0, "amt": 60000.0},    # 被动卖
    ]
    r = ds.dark_flow_from_tck(_trades(), orders)
    assert r["passive_buy"] == 100000.0
    assert r["passive_sell"] == 60000.0
    assert r["passive_net"] == 40000.0
    # 总净额 = 主动 + 被动
    assert r["net"] == 550000.0 + 40000.0


def test_dark_flow_from_tck_marks_partial():
    """maker 未落盘 → partial=True, 明示仅主笔级还原。"""
    r = ds.dark_flow_from_tck(_trades(), [])
    assert r["partial"] is True
    assert "主笔级还原" in r["note"]


def test_dark_flow_from_tck_dark_basis_is_small_orders():
    """暗盘此处是"小单口径", 用 dark_basis 明示, 不做等价冒充。"""
    r = ds.dark_flow_from_tck(_trades(), [])
    assert r["dark_basis"] == "small_orders"
    # 明盘 = >30万 两笔买; 小单 = 5万买 - 20万卖
    assert r["ming_net"] == 700000.0
    assert r["small_net"] == -150000.0


def test_dark_flow_from_tck_empty_marks_no_data():
    r = ds.dark_flow_from_tck([], [])
    assert r["net"] is None and r["active_net"] is None
    assert r["note"] == "无数据"

