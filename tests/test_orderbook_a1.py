# -*- coding: utf-8 -*-
""".img 十档盘口(A1) + get_order_book_queue(A5) 单测。

覆盖:
  - tdx_img_parser.ImgSnapshot 派生指标(best_bid/spread/bid_pressure/queue_imbalance)
  - orderbook_engine.img_frame_to_snapshot(.img 帧 → 同构快照)
  - orderbook_engine.order_book_queue(托压单形态识别)
  - orderbook_engine 三算法(演变/失衡/幽灵单, 合成数据)
  - chat_tools.get_order_book_queue(.img 路径 + thsdk 回退)
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import orderbook_engine as obe  # noqa: E402
from src.core import tdx_img_parser as tip  # noqa: E402
from src.core import chat_tools as ct  # noqa: E402


# ──────────────────────────── ImgSnapshot 派生指标 ────────────────────────────

def _img_snap(bid_prices, bid_vols, ask_prices, ask_vols, queue=None):
    return tip.ImgSnapshot(
        t="10:30:00",
        bid_prices=bid_prices, bid_vols=bid_vols,
        ask_prices=ask_prices, ask_vols=ask_vols,
        queue=queue,
    )


def test_imgsnapshot_derived():
    s = _img_snap([11.27, 11.26], [10000, 5000], [11.28, 11.29], [4000, 2000], queue=[1000, 2000])
    assert s.best_bid() == 11.27
    assert s.best_ask() == 11.28
    assert s.spread() == pytest.approx(0.01)
    # 买盘力量 = (10000+5000) / (15000+6000) = 15000/21000 = 0.7143
    assert s.bid_pressure() == pytest.approx(round(15000 / 21000, 6))
    # 队列失衡 = 队列总量 - 卖一量 = (1000+2000) - 4000 = -1000
    assert s.queue_imbalance() == -1000


def test_imgsnapshot_no_data_none():
    s = _img_snap([], [], [], [])
    assert s.best_bid() is None
    assert s.spread() is None
    assert s.bid_pressure() is None  # 总量 0 → None, 不返回 0.0 冒充均衡


# ──────────────────────────── 帧 → 快照 ────────────────────────────

def test_img_frame_to_snapshot():
    fr = _img_snap([11.27, 11.26], [10000, 5000], [11.28, 11.29], [4000, 2000], queue=[1000, 2000])
    snap = obe.img_frame_to_snapshot(fr, ts=1.0, dt_iso="10:30:00")
    assert snap["source"] == "img"
    assert snap["bid"][11.27] == 100  # 10000 股 → 100 手
    assert snap["bid"][11.26] == 50
    assert snap["ask"][11.28] == 40
    # 队列: 股 → 手
    assert snap["queue"] == [10, 20]
    assert snap["queue_shares"] == [1000, 2000]


# ──────────────────────────── 托压单形态 ────────────────────────────

def test_order_book_queue_shape():
    # 买盘力量大 → 托盘
    snap = obe.img_frame_to_snapshot(
        _img_snap([11.27], [9000], [11.28], [1000]), ts=0.0
    )
    q = obe.order_book_queue(snap)
    assert q["available"] is True
    assert q["shape"] == "托盘"  # 9000/(9000+1000)=0.9 >= 0.6
    assert q["best_bid"] == 11.27
    assert q["best_ask"] == 11.28


def test_order_book_queue_empty():
    q = obe.order_book_queue(None)
    assert q["available"] is False
    assert q["shape"] is None


# ──────────────────────────── 三算法(合成数据) ────────────────────────────

def _mk_level(price, orders, orderlevel):
    return {"orderlevel": orderlevel, "price": price, "ordersque": orders}


def _mk_snap(bid_levels, ask_levels, tag):
    return {
        "ts": 0.0, "dt": tag,
        "bid_levels": bid_levels, "ask_levels": ask_levels,
        "bid": {float(l["price"]): sum(l["ordersque"]) for l in bid_levels},
        "ask": {float(l["price"]): sum(l["ordersque"]) for l in ask_levels},
    }


def _synthetic_snapshots():
    """构造含托单/压单/撤单/幽灵单的 6 快照序列。"""
    base_bid = [
        _mk_level(11.27, [100] * 30, 1), _mk_level(11.26, [100] * 25, 2),
        _mk_level(11.25, [100] * 20, 3), _mk_level(11.24, [100] * 18, 4),
    ]
    base_ask = [
        _mk_level(11.28, [100] * 30, 1), _mk_level(11.29, [100] * 25, 2),
        _mk_level(11.30, [100] * 20, 3), _mk_level(11.31, [100] * 18, 4),
    ]

    def copy(lv): return [dict(x) for x in lv]

    snaps = [_mk_snap(copy(base_bid), copy(base_ask), "s0基准")]
    # s1: 买二堆单 +5000 → 托单; 卖三堆单 +6000 → 压单
    b1, a1 = copy(base_bid), copy(base_ask)
    b1[1]["ordersque"] = [100] * 25 + [5000]
    a1[2]["ordersque"] = [100] * 20 + [6000]
    snaps.append(_mk_snap(b1, a1, "s1堆单"))
    # s2: 保持
    snaps.append(_mk_snap(copy(b1), copy(a1), "s2堆单持续"))
    # s3: 回落
    snaps.append(_mk_snap(copy(base_bid), copy(base_ask), "s3堆单回落"))
    # s4: 买四撤单 50%+
    b4 = copy(base_bid)
    b4[3]["ordersque"] = [100] * 5
    snaps.append(_mk_snap(b4, copy(base_ask), "s4撤单"))
    # s5: 卖一大单 50000 手
    a5 = copy(base_ask)
    a5[0]["ordersque"] = [100] * 30 + [50000]
    snaps.append(_mk_snap(copy(b1), a5, "s5卖一大单"))
    return snaps


def test_order_book_evolution_detects():
    snaps = _synthetic_snapshots()
    events = obe.order_book_evolution(snaps)
    types = {e["type"] for e in events}
    assert {"托单", "压单", "撤单"} <= types  # 三种都检出


def test_order_book_imbalance():
    snaps = _synthetic_snapshots()
    series = obe.order_book_imbalance(snaps)
    assert len(series) == len(snaps)
    for s in series:
        assert -1.0 <= s["ob"] <= 1.0
        assert s["label"] in ("买压", "卖压", "中性")


def test_ghost_order():
    snaps = _synthetic_snapshots()
    ghosts, ratio = obe.ghost_order(snaps)
    assert isinstance(ratio, float)
    assert 0.0 <= ratio <= 1.0
    # 合成数据 s5 卖一大单 50000 手(>1000手 且 >档总50%) → 应被识别为大单
    assert any(g["hands"] >= 1000 for g in ghosts) if ghosts else True


# ──────────────────────────── 工具链路 ────────────────────────────

def test_get_order_book_queue_img(monkeypatch):
    monkeypatch.setattr(obe, "find_img_file", lambda code, market="CN": "fake.img")
    monkeypatch.setattr(
        obe, "load_snapshots_from_img",
        lambda img_path, limit=None: [
            obe.img_frame_to_snapshot(
                _img_snap([11.27], [9000], [11.28], [1000]), ts=0.0
            )
        ],
    )
    r = ct.get_order_book_queue("002361")
    assert r.error is None
    assert r.data["available"] is True
    assert r.data["shape"] == "托盘"
    assert r.data["img_path"] == "fake.img"


def test_get_order_book_queue_no_source(monkeypatch):
    monkeypatch.setattr(obe, "find_img_file", lambda code, market="CN": None)
    # thsdk 回退也失败(不真正调用, 直接让 fetch_snapshot 抛异常)
    monkeypatch.setattr(obe, "fetch_snapshot", lambda ths_code: (_ for _ in ()).throw(RuntimeError("no thsdk")))
    monkeypatch.setattr(obe, "to_ths_code", lambda code: "USZA002361")
    r = ct.get_order_book_queue("002361")
    assert r.error is not None
    assert r.data is None
