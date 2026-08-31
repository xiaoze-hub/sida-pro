# -*- coding: utf-8 -*-
"""通达信 .img 十档盘口解析器单测。

覆盖:
  - TLV 扫描(正常 / 越界)
  - 十档装配(价格标度还原 / 单位 / 缺失档位)
  - 派生指标(价差 / 买盘占比 / 队列净挂单)
  - 同构输出的"无数据"标注

真实 .img 样本校准测试默认 skip(仓库内无样本), 放入样本后自动启用。
"""
import os
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import tdx_img_parser as ip  # noqa: E402

SAMPLE = Path(os.environ.get("TDX_IMG_SAMPLE", "/app/data/tdx_img/sample.img"))
HAS_SAMPLE = SAMPLE.is_file()


# ---------------------------------------------------------------------------
# 工具: 造 TLV 帧
# ---------------------------------------------------------------------------


def _tlv(tag: int, payload: bytes) -> bytes:
    return bytes([tag, len(payload)]) + payload


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _price(v: float, scale: int = ip.PRICE_SCALE) -> bytes:
    return _u32(int(round(v * scale)))


def _frame(with_time: bool = True, levels: int = 10, with_queue: bool = True) -> bytes:
    """拼一帧完整的十档盘口 TLV(买一 10.50 / 卖一 10.51, 每档 1000*(i+1) 股)。"""
    buf = b""
    if with_time:
        buf += _tlv(ip.TAG_TIME, _u32(93_000_000))  # 09:30:00
    for i in range(levels):
        buf += _tlv(ip.BID_PRICE_TAGS[i], _price(10.50 - i * 0.01))
        buf += _tlv(ip.BID_VOL_TAGS[i], _u32(1000 * (i + 1)))
        buf += _tlv(ip.ASK_PRICE_TAGS[i], _price(10.51 + i * 0.01))
        buf += _tlv(ip.ASK_VOL_TAGS[i], _u32(2000 * (i + 1)))
    buf += _tlv(ip.TAG_BID_ORDERS, _u32(120))
    buf += _tlv(ip.TAG_ASK_ORDERS, _u32(80))
    if with_queue:
        buf += _tlv(ip.TAG_QUEUE, _u32(5000) + _u32(3000) + _u32(2000))
    return buf


# ---------------------------------------------------------------------------
# TLV 扫描
# ---------------------------------------------------------------------------


def test_iter_tlv_splits_tag_len_payload():
    data = _tlv(0x20, b"\x01\x02") + _tlv(0x30, b"\x03")
    got = list(ip.iter_tlv(data))
    assert got == [(0x20, b"\x01\x02"), (0x30, b"\x03")]


def test_iter_tlv_zero_length_payload():
    data = _tlv(0x64, b"")
    assert list(ip.iter_tlv(data)) == [(0x64, b"")]


def test_iter_tlv_oversized_raises():
    # 声明 8 字节但只给 2 字节 → 结构不匹配当前假设, 必须报错而不是静默截断
    data = bytes([0x20, 0x08]) + b"\x01\x02"
    with pytest.raises(ip.ImgParseError):
        list(ip.iter_tlv(data))


def test_iter_tlv_trailing_short_header_ignored():
    data = _tlv(0x20, b"\x01") + b"\x30"  # 只剩 1 字节, 凑不够 header
    assert list(ip.iter_tlv(data)) == [(0x20, b"\x01")]


# ---------------------------------------------------------------------------
# 十档装配
# ---------------------------------------------------------------------------


def test_decode_snapshot_full_depth():
    snap = ip.decode_snapshot(list(ip.iter_tlv(_frame())))
    assert snap.t == "09:30:00"
    assert len(snap.bid_prices) == 10 and len(snap.ask_prices) == 10
    # 价格定点还原(元)
    assert snap.bid_prices[0] == pytest.approx(10.50)
    assert snap.ask_prices[0] == pytest.approx(10.51)
    assert snap.bid_prices[9] == pytest.approx(10.41)
    # 量单位=股
    assert snap.bid_vols[0] == 1000
    assert snap.ask_vols[9] == 20000
    # 委托笔数
    assert snap.bid_orders == 120 and snap.ask_orders == 80
    # 委托队列(每笔挂单量, 股)
    assert snap.queue == [5000, 3000, 2000]


def test_decode_snapshot_missing_levels_is_none():
    snap = ip.decode_snapshot(list(ip.iter_tlv(_frame(levels=3))))
    assert len(snap.bid_prices) == 10
    assert snap.bid_prices[:3] == pytest.approx([10.50, 10.49, 10.48])
    # 未出现的档位必须是 None, 不能用 0 填充(0 是有意义的挂单量)
    assert snap.bid_prices[3:] == [None] * 7
    assert snap.bid_vols[3:] == [None] * 7


def test_decode_snapshot_duplicate_tag_keeps_first():
    data = _tlv(ip.BID_VOL_TAGS[0], _u32(1000)) + _tlv(ip.BID_VOL_TAGS[0], _u32(9999))
    snap = ip.decode_snapshot(list(ip.iter_tlv(data)))
    assert snap.bid_vols[0] == 1000


def test_decode_snapshot_no_queue_is_none():
    snap = ip.decode_snapshot(list(ip.iter_tlv(_frame(with_queue=False))))
    assert snap.queue is None


def test_decode_snapshot_bid1_fallback_from_tag_04_05():
    # 只有 04/05 旧标签, 没有 20/30 档 → 买一仍应可读
    data = _tlv(ip.TAG_BID1_PRICE, _price(9.99)) + _tlv(ip.TAG_BID1_VOL, _u32(1234))
    snap = ip.decode_snapshot(list(ip.iter_tlv(data)))
    assert snap.bid_prices[0] == pytest.approx(9.99)
    assert snap.bid_vols[0] == 1234


def test_decode_snapshot_time_epoch_seconds():
    import time

    epoch = int(time.mktime(time.strptime("2026-08-31 14:30:00", "%Y-%m-%d %H:%M:%S")))
    data = _tlv(ip.TAG_TIME, _u32(epoch))
    snap = ip.decode_snapshot(list(ip.iter_tlv(data)))
    assert snap.t == "14:30:00"


def test_decode_snapshot_invalid_time_is_none():
    data = _tlv(ip.TAG_TIME, _u32(999_999_999))  # 既非 HHMMSSmmm 也非 epoch
    snap = ip.decode_snapshot(list(ip.iter_tlv(data)))
    assert snap.t is None


# ---------------------------------------------------------------------------
# 派生指标
# ---------------------------------------------------------------------------


def test_spread():
    snap = ip.decode_snapshot(list(ip.iter_tlv(_frame())))
    assert snap.spread() == pytest.approx(0.01)


def test_spread_missing_side_is_none():
    data = _tlv(ip.BID_PRICE_TAGS[0], _price(10.50))
    snap = ip.decode_snapshot(list(ip.iter_tlv(data)))
    assert snap.spread() is None


def test_bid_pressure_ratio():
    snap = ip.decode_snapshot(list(ip.iter_tlv(_frame())))
    # 买总量 = 1000*(1+..+10) = 55000, 卖总量 = 2000*55 = 110000
    assert snap.bid_pressure() == pytest.approx(55000 / 165000)


def test_bid_pressure_zero_volume_is_none():
    data = _tlv(ip.BID_VOL_TAGS[0], _u32(0)) + _tlv(ip.ASK_VOL_TAGS[0], _u32(0))
    snap = ip.decode_snapshot(list(ip.iter_tlv(data)))
    # 总量为 0 → 不允许返回 0.0 冒充"均衡", 必须显式 None
    assert snap.bid_pressure() is None


def test_queue_imbalance():
    snap = ip.decode_snapshot(list(ip.iter_tlv(_frame())))
    # 队列合计 10000 - 卖一量 2000 = 8000
    assert snap.queue_imbalance() == 8000


def test_queue_imbalance_without_queue_is_none():
    snap = ip.decode_snapshot(list(ip.iter_tlv(_frame(with_queue=False))))
    assert snap.queue_imbalance() is None


# ---------------------------------------------------------------------------
# 同构输出
# ---------------------------------------------------------------------------


def test_frames_from_img_shape_and_units():
    snap = ip.decode_snapshot(list(ip.iter_tlv(_frame())))
    frames = ip.frames_from_img([snap])
    assert len(frames) == 1
    f = frames[0]
    assert f["t"] == "09:30:00"
    assert len(f["bid"]) == 10 and len(f["ask"]) == 10
    assert f["bid"][0] == {"price": 10.50, "vol": 1000}
    assert f["bid_orders"] == 120
    assert f["queue"] == [5000, 3000, 2000]


def test_frames_from_img_marks_missing_as_no_data():
    snap = ip.decode_snapshot(list(ip.iter_tlv(_frame(levels=1, with_queue=False))))
    f = ip.frames_from_img([snap])[0]
    assert f["spread"] == ip.MISSING or isinstance(f["spread"], float)
    # 缺失档位显式标注"无数据", 不得补 0
    assert f["bid"][5] == {"price": ip.MISSING, "vol": ip.MISSING}
    assert f["queue"] == ip.MISSING
    assert f["queue_imb"] == ip.MISSING


def test_frames_from_img_respects_top_n():
    snap = ip.decode_snapshot(list(ip.iter_tlv(_frame())))
    f = ip.frames_from_img([snap], top_n=5)[0]
    assert len(f["bid"]) == 5 and len(f["ask"]) == 5


# ---------------------------------------------------------------------------
# 文件层
# ---------------------------------------------------------------------------


def test_parse_img_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ip.parse_img(tmp_path / "nope.img")


def test_parse_img_empty_file_raises(tmp_path):
    p = tmp_path / "empty.img"
    p.write_bytes(b"")
    with pytest.raises(ip.ImgParseError):
        ip.parse_img(p)


@pytest.mark.skipif(not HAS_SAMPLE, reason="无真实 .img 样本, 字节层假设未校准")
def test_parse_img_real_sample():
    """真实样本校准入口。

    ⚠️ 通过该测试前, 模块顶部的"待校准"项必须逐条确认并固化到 ImgFormat。
    """
    snaps = ip.parse_img(str(SAMPLE))
    assert len(snaps) > 0
    s = snaps[0]
    assert s.t is not None
    assert s.bid_prices[0] is not None and s.ask_prices[0] is not None
    assert s.bid_prices[0] < s.ask_prices[0]  # 卖一必须高于买一
