# -*- coding: utf-8 -*-
"""第2块 .img 链路接通 单测: orderbook_engine ← tdx_img_parser。

覆盖:
  - 帧 → 快照同构转换(单位换算 股→手, 字段齐全)
  - load_snapshots_from_img 读文件 / 缺文件 / 解析失败 → []
  - order_book_queue 托压单形态判定 + 缺失显式 None
  - find_img_file 未配置目录 / 命中 / 未命中
  - summary 接口 orderbook 字段(有 .img 走 .img, 无则回退, 都无则 None)
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import orderbook_engine as obe  # noqa: E402
from src.core.tdx_img_parser import ImgSnapshot  # noqa: E402


def _frame(bid_vols=None, ask_vols=None, queue=None, t="14:30:00"):
    bid_vols = bid_vols if bid_vols is not None else [90000, 80000, 70000, 60000, 50000]
    ask_vols = ask_vols if ask_vols is not None else [10000, 20000, 30000, 40000, 50000]
    return ImgSnapshot(
        t=t,
        bid_prices=[10.00, 9.99, 9.98, 9.97, 9.96][: len(bid_vols)],
        bid_vols=bid_vols,
        ask_prices=[10.01, 10.02, 10.03, 10.04, 10.05][: len(ask_vols)],
        ask_vols=ask_vols,
        bid_orders=None, ask_orders=None, queue=queue,
    )


# ---------------------------------------------------------------------------
# 帧 → 快照
# ---------------------------------------------------------------------------


def test_frame_to_snapshot_converts_shares_to_hands():
    """量单位 股→手(1手=100股), 保持金额=元/量=股 的换算链。"""
    fr = _frame(bid_vols=[30000], ask_vols=[10000])
    snap = obe.img_frame_to_snapshot(fr, ts=0.0)
    assert snap["bid_levels"][0]["ordersque"] == [300]   # 30000股 → 300手
    assert snap["ask_levels"][0]["ordersque"] == [100]
    assert snap["bid"] == {10.00: 300}
    assert snap["source"] == "img"


def test_frame_to_snapshot_keeps_fields():
    snap = obe.img_frame_to_snapshot(_frame(), ts=1.0)
    for key in ("ts", "dt", "bid_levels", "ask_levels", "bid", "ask", "queue", "queue_shares"):
        assert key in snap
    assert snap["dt"] == "14:30:00"


def test_frame_to_snapshot_with_queue():
    """委托队列同时保留 手(引擎口径) 与 原始股(不丢信息)。"""
    snap = obe.img_frame_to_snapshot(_frame(queue=[1000, 2000, 3000]), ts=0.0)
    assert snap["queue"] == [10, 20, 30]
    assert snap["queue_shares"] == [1000, 2000, 3000]


# ---------------------------------------------------------------------------
# load_snapshots_from_img
# ---------------------------------------------------------------------------


def test_load_snapshots_missing_file_returns_empty():
    assert obe.load_snapshots_from_img("C:/nonexistent/xxx.img") == []


def test_load_snapshots_parse_failure_returns_empty(monkeypatch, tmp_path):
    """解析器抛错 → [] (不把异常泄漏到接口层)。"""
    import src.core.tdx_img_parser as tip

    monkeypatch.setattr(tip, "frames_from_img", lambda p: (_ for _ in ()).throw(ValueError("bad")))
    f = tmp_path / "x.img"
    f.write_bytes(b"junk")
    assert obe.load_snapshots_from_img(str(f)) == []


def test_load_snapshots_no_frames_returns_empty(monkeypatch, tmp_path):
    import src.core.tdx_img_parser as tip

    monkeypatch.setattr(tip, "frames_from_img", lambda p: [])
    f = tmp_path / "x.img"
    f.write_bytes(b"junk")
    assert obe.load_snapshots_from_img(str(f)) == []


def test_load_snapshots_converts_all_frames(monkeypatch, tmp_path):
    import src.core.tdx_img_parser as tip

    monkeypatch.setattr(tip, "frames_from_img", lambda p: [_frame(t="14:30:00"), _frame(t="14:30:01")])
    f = tmp_path / "x.img"
    f.write_bytes(b"junk")
    snaps = obe.load_snapshots_from_img(str(f))
    assert len(snaps) == 2
    assert snaps[0]["ts"] == 0.0 and snaps[1]["ts"] == 1.0
    assert obe.load_snapshots_from_img(str(f), limit=1) == snaps[:1]


# ---------------------------------------------------------------------------
# order_book_queue 形态识别
# ---------------------------------------------------------------------------


def test_queue_shape_none_when_no_snapshot():
    r = obe.order_book_queue(None)
    assert r["available"] is False and r["shape"] is None and "无数据" in r["note"]


def test_queue_shape_bid_heavy_is_tray():
    """买盘占比高 → 托盘。"""
    snap = obe.img_frame_to_snapshot(_frame(bid_vols=[90000] * 5, ask_vols=[10000] * 5), ts=0.0)
    r = obe.order_book_queue(snap)
    assert r["available"] is True
    assert r["bid_pressure"] == pytest.approx(0.9, abs=0.01)
    assert r["shape"] == "托盘"


def test_queue_shape_ask_heavy_is_press():
    snap = obe.img_frame_to_snapshot(_frame(bid_vols=[10000] * 5, ask_vols=[90000] * 5), ts=0.0)
    r = obe.order_book_queue(snap)
    assert r["bid_pressure"] == pytest.approx(0.1, abs=0.01)
    assert r["shape"] == "压盘"


def test_queue_shape_balanced():
    snap = obe.img_frame_to_snapshot(_frame(bid_vols=[50000] * 5, ask_vols=[50000] * 5), ts=0.0)
    assert obe.order_book_queue(snap)["shape"] == "均衡"


def test_queue_imbalance_computed():
    """队列失衡 = 队列总量 - 卖一量(股)。队列远大于卖一 → 疑似压单。"""
    snap = obe.img_frame_to_snapshot(_frame(ask_vols=[10000] * 5, queue=[50000, 50000]), ts=0.0)
    r = obe.order_book_queue(snap)
    assert r["queue_shares"] == 100000          # 50000+50000 股
    assert r["queue_imbalance"] == 100000 - 10000  # 卖一 10000 股


def test_queue_missing_fields_are_none():
    """无队列 / 无档位 → 显式 None, 不编造。"""
    snap = obe.img_frame_to_snapshot(_frame(), ts=0.0)
    r = obe.order_book_queue(snap)
    assert r["queue_shares"] is None and r["queue_imbalance"] is None


def test_queue_empty_book_gives_none_pressure():
    snap = {"bid": {}, "ask": {}, "bid_levels": [], "ask_levels": []}
    r = obe.order_book_queue(snap)
    assert r["bid_pressure"] is None and r["shape"] is None


# ---------------------------------------------------------------------------
# find_img_file
# ---------------------------------------------------------------------------


def test_find_img_file_no_dir_configured(monkeypatch):
    monkeypatch.delenv("PANWATCH_IMG_DIR", raising=False)
    assert obe.find_img_file("000977") is None


def test_find_img_file_hit(monkeypatch, tmp_path):
    (tmp_path / "sz000977_1.img").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_IMG_DIR", str(tmp_path))
    found = obe.find_img_file("000977")
    assert found and found.endswith("sz000977_1.img")


def test_find_img_file_miss(monkeypatch, tmp_path):
    (tmp_path / "sz600000_1.img").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_IMG_DIR", str(tmp_path))
    assert obe.find_img_file("000977") is None


def test_find_img_file_ignores_non_img(monkeypatch, tmp_path):
    (tmp_path / "000977.dat").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_IMG_DIR", str(tmp_path))
    assert obe.find_img_file("000977") is None


# ---------------------------------------------------------------------------
# summary 接口 orderbook 字段
# ---------------------------------------------------------------------------


def _bars(n=40):
    out = []
    price = 20.0
    for i in range(n):
        o = price
        price += 0.1
        out.append({"date": f"2026-07-{i+1:02d}", "open": o,
                    "high": max(o, price) + 0.05, "low": min(o, price) - 0.05,
                    "close": price, "volume": 100000})
    return out


def test_summary_orderbook_prefers_img(monkeypatch, tmp_path):
    """有 .img → 走 .img(带 img_path), 不调 thsdk。"""
    from src.models.market import MarketCode
    from src.core import decision_pioneer as dp
    from src.web.api import klines as kapi

    monkeypatch.setattr(dp, "fetch_bars", lambda s, m, days=120: _bars(40))
    (tmp_path / "sz000977_a.img").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_IMG_DIR", str(tmp_path))
    import src.core.tdx_img_parser as tip
    monkeypatch.setattr(tip, "frames_from_img",
                        lambda p: [_frame(bid_vols=[90000] * 5, ask_vols=[10000] * 5)])

    out = kapi._build_layer_data("000977", MarketCode.CN)
    assert out["orderbook"] is not None
    assert out["orderbook"]["shape"] == "托盘"
    assert out["orderbook"]["img_path"].endswith("sz000977_a.img")


def test_summary_orderbook_falls_back_to_thsdk(monkeypatch):
    """无 .img → 回退 thsdk 实时快照。"""
    from src.models.market import MarketCode
    from src.core import decision_pioneer as dp
    from src.web.api import klines as kapi

    monkeypatch.setattr(dp, "fetch_bars", lambda s, m, days=120: _bars(40))
    monkeypatch.delenv("PANWATCH_IMG_DIR", raising=False)
    monkeypatch.setattr(obe, "fetch_snapshot", lambda code: obe.img_frame_to_snapshot(
        _frame(bid_vols=[10000] * 5, ask_vols=[90000] * 5), ts=0.0))

    out = kapi._build_layer_data("000977", MarketCode.CN)
    assert out["orderbook"] is not None
    assert out["orderbook"]["shape"] == "压盘"


def test_summary_orderbook_none_when_all_fail(monkeypatch):
    """两路都失败 → 显式"无数据"(available=False), 不编造。"""
    from src.models.market import MarketCode
    from src.core import decision_pioneer as dp
    from src.web.api import klines as kapi

    monkeypatch.setattr(dp, "fetch_bars", lambda s, m, days=120: _bars(40))
    monkeypatch.delenv("PANWATCH_IMG_DIR", raising=False)
    monkeypatch.setattr(obe, "fetch_snapshot",
                        lambda code: (_ for _ in ()).throw(RuntimeError("thsdk 不通")))

    out = kapi._build_layer_data("000977", MarketCode.CN)
    ob = out["orderbook"]
    assert ob is None or ob.get("available") is False
    if ob is not None:
        assert ob.get("shape") is None and "无数据" in (ob.get("note") or "")
    # 其余字段不受影响
    assert out["fund_flow"] is not None
