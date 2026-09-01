# -*- coding: utf-8 -*-
"""第3块 两个空壳工具补实 单测:
   get_dark_flow_precise (.tck 主笔级暗盘) / get_order_book_queue (.img 托压单)
   + dark_split.find_tck_file / dark_flow_from_tck / orderbook_engine.to_ths_code
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import chat_tools as ct  # noqa: E402
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


# ---------------------------------------------------------------------------
# get_dark_flow_precise 工具
# ---------------------------------------------------------------------------


def _res(r):
    """ToolResult 是 dataclass(无 ok 字段), 统一转 dict 便于断言。"""
    return r.to_dict()


def _ok(r):
    d = _res(r)
    return d["error"] is None and d["data"] is not None


def test_tool_dark_flow_precise_no_file(monkeypatch):
    monkeypatch.delenv("PANWATCH_TCK_DIR", raising=False)
    r = ct.get_dark_flow_precise("000977", "2026-08-31", user_id="admin")
    assert _ok(r) is False
    assert "无 .tck" in (_res(r)["error"] or "")


def test_tool_dark_flow_precise_ok(monkeypatch, tmp_path):
    (tmp_path / "sz000977_20260831.tck").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))

    trades, orders, cancels = _trades(), [{"a28": 0, "a32": 1, "amt": 100000.0}], [{"seq": 9}]
    import src.core.tdx_tick_parser as ttp
    monkeypatch.setattr(ttp, "parse_tck", lambda p: (trades, orders, cancels))

    r = ct.get_dark_flow_precise("000977", "2026-08-31", user_id="admin")
    d = _res(r)
    assert _ok(r) is True
    assert d["data"]["net"] == 550000.0 + 100000.0
    assert d["data"]["cancel_count"] == 1
    assert d["data"]["partial"] is True
    assert d["units"]["net"] == "元"


def test_tool_dark_flow_precise_empty_trades(monkeypatch, tmp_path):
    (tmp_path / "sz000977.tck").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))
    import src.core.tdx_tick_parser as ttp
    monkeypatch.setattr(ttp, "parse_tck", lambda p: ([], [], []))

    r = ct.get_dark_flow_precise("000977", user_id="admin")
    assert _ok(r) is False
    assert "无成交记录" in (_res(r)["error"] or "")


def test_tool_dark_flow_precise_parse_error(monkeypatch, tmp_path):
    (tmp_path / "sz000977.tck").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))
    import src.core.tdx_tick_parser as ttp
    monkeypatch.setattr(ttp, "parse_tck",
                        lambda p: (_ for _ in ()).throw(ValueError("bad tck")))
    r = ct.get_dark_flow_precise("000977", user_id="admin")
    assert _ok(r) is False and "bad tck" in (_res(r)["error"] or "")


# ---------------------------------------------------------------------------
# get_order_book_queue 工具
# ---------------------------------------------------------------------------


def _img_frame(bid_vols, ask_vols, queue=None):
    from src.core.tdx_img_parser import ImgSnapshot
    n = len(bid_vols)
    return ImgSnapshot(
        t="14:30:00",
        bid_prices=[10.00 - 0.01 * i for i in range(n)],
        bid_vols=bid_vols,
        ask_prices=[10.01 + 0.01 * i for i in range(len(ask_vols))],
        ask_vols=ask_vols,
        queue=queue,
    )


def test_tool_order_book_queue_from_img(monkeypatch, tmp_path):
    (tmp_path / "sz000977_a.img").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_IMG_DIR", str(tmp_path))
    import src.core.tdx_img_parser as tip
    monkeypatch.setattr(tip, "frames_from_img",
                        lambda p: [_img_frame([90000] * 5, [10000] * 5, queue=[1000, 2000])])

    r = ct.get_order_book_queue("000977", user_id="admin")
    d = _res(r)
    assert _ok(r) is True
    assert d["data"]["shape"] == "托盘"
    assert d["data"]["img_path"].endswith("sz000977_a.img")
    assert d["units"]["queue_shares"] == "股"


def test_tool_order_book_queue_fallback_thsdk(monkeypatch):
    monkeypatch.delenv("PANWATCH_IMG_DIR", raising=False)
    monkeypatch.setattr(obe, "fetch_snapshot",
                        lambda code: obe.img_frame_to_snapshot(
                            _img_frame([10000] * 5, [90000] * 5), ts=0.0))
    r = ct.get_order_book_queue("000977", user_id="admin")
    assert _ok(r) is True
    assert _res(r)["data"]["shape"] == "压盘"


def test_tool_order_book_queue_all_fail_marks_no_data(monkeypatch):
    monkeypatch.delenv("PANWATCH_IMG_DIR", raising=False)
    monkeypatch.setattr(obe, "fetch_snapshot",
                        lambda code: (_ for _ in ()).throw(RuntimeError("thsdk 不通")))
    r = ct.get_order_book_queue("000977", user_id="admin")
    assert _ok(r) is False
    assert _res(r)["note"] == "无数据"


def test_tool_order_book_queue_passes_ths_code(monkeypatch):
    """fetch_snapshot 必须收到 USZA/USHA 代码, 不是腾讯 sz 风格。"""
    monkeypatch.delenv("PANWATCH_IMG_DIR", raising=False)
    seen = {}

    def fake(code):
        seen["code"] = code
        return obe.img_frame_to_snapshot(_img_frame([50000] * 5, [50000] * 5), ts=0.0)

    monkeypatch.setattr(obe, "fetch_snapshot", fake)
    ct.get_order_book_queue("000977", user_id="admin")
    assert seen["code"] == "USZA000977"
