# -*- coding: utf-8 -*-
"""通达信 `.img` 十档盘口解析器单测(**真实样本校准版**, 2026-10-02)。

覆盖:
  - 容器解码(24B 头 + zlib): 正常 / 短头 / 压缩长度越界 / zlib 损坏 / 声明长度不符
  - 帧与字段扫描(0x03..0x04 帧 / 0x02 字段): 正常 / 尾帧不完整丢弃 / 残片跳过
  - 十档装配: 十进制文本解析(元) / 缺失档显式 None(不补 0) / 04-05 兜底
  - 增量语义: 缺失档沿用上一帧; delta=False 时不沿用
  - 委托队列: 两段(买一 / 卖一)按标签 61 切分; 无 61 退回位置口径; 无队列 None
  - 委托笔数: 62 双值(前买后卖) / 单值 / 缺省
  - 时间 / 派生指标 / 同构输出「无数据」标注
  - 文件层: 缺文件 / 空文件 / 无帧 / 容器损坏 错误态
  - **样本驱动校准**: 用仓库内真实样本截取的 fixture 钉死实测结论(帧数/时间跨度/
    队列语义/增量单调性/队列截断), 逐条验证原 4 条假设被推翻

fixture 由 `scripts/gen_img_fixture.py` 从真实样本(逐帧原字节)截断重封装而来。
"""
import os
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import tdx_img_parser as ip  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
FIX_OPEN = FIXTURES / "sz002361_20260827_open.img"     # 08:36:27 → 09:32:18, 120 帧
FIX_0828 = FIXTURES / "sz002361_20260828_open.img"     # 09:30:03 → 09:33:00, 60 帧
SAMPLE = Path(os.environ.get("TDX_IMG_SAMPLE", "/app/data/tdx_img/sample.img"))
HAS_SAMPLE = SAMPLE.is_file()


# ---------------------------------------------------------------------------
# 造帧工具(按校准后的真实文本协议)
# ---------------------------------------------------------------------------


def _f(tag: str, value) -> bytes:
    return f"{tag}{value}".encode("ascii")


def _join(*fields: bytes) -> bytes:
    """字段用 0x02 连接(帧内容, 不含 0x03/0x04)。"""
    return b"\x02".join(fields)


def _record(content: bytes) -> bytes:
    """字段内容 → 一帧(0x03 起 0x04 止)。"""
    return b"\x03" + content + b"\x04"


def _container(*records: bytes, magic: bytes = b"TEST") -> bytes:
    """帧序列 → 合法 .img 容器(24B 头 + zlib)。"""
    body = b"".join(records)
    comp = zlib.compress(body)
    return (
        magic + b"\x00\x00\x00\x00"
        + len(comp).to_bytes(8, "little")
        + len(body).to_bytes(8, "little")
        + comp
    )


def _depth(bid_prices=(), bid_vols=(), ask_prices=(), ask_vols=(), t="93000.000"):
    out = []
    if t is not None:
        out.append(_f("0T", t))
    for i, (p, v) in enumerate(zip(bid_prices, bid_vols)):
        out.append(_f(f"{0x20 + i:02X}", f"{p:.6f}"))
        out.append(_f(f"{0x30 + i:02X}", int(v)))
    for i, (p, v) in enumerate(zip(ask_prices, ask_vols)):
        out.append(_f(f"{0x40 + i:02X}", f"{p:.6f}"))
        out.append(_f(f"{0x50 + i:02X}", int(v)))
    return out


def _book_content(
    bid_prices=(10.50, 10.49, 10.48),
    bid_vols=(1000, 2000, 3000),
    ask_prices=(10.51, 10.52, 10.53),
    ask_vols=(2000, 3000, 4000),
    bid_queue=None,
    ask_queue=None,
    bid_orders=None,
    ask_orders=None,
    t="93000.000",
) -> bytes:
    """含两段委托队列区块的完整帧内容。"""
    fields = _depth(bid_prices, bid_vols, ask_prices, ask_vols, t=t)
    fields.append(_f("60", "10"))
    if bid_orders is not None:
        fields.append(_f("62", int(bid_orders)))
    if bid_queue is not None:
        fields.append(_f("63", len(bid_queue)))
        fields += [_f("64", int(x)) for x in bid_queue]
    fields.append(_f("61", "10"))
    if ask_orders is not None:
        fields.append(_f("62", int(ask_orders)))
    if ask_queue is not None:
        fields.append(_f("63", len(ask_queue)))
        fields += [_f("64", int(x)) for x in ask_queue]
    return _join(*fields)


def _decode(content: bytes, prev=None, delta=None, fmt=None):
    f = fmt or ip.ImgFormat()
    if delta is not None:
        f = f.with_delta(delta)
    return ip.decode_snapshot(list(ip.iter_fields(content)), f, prev=prev)


# ---------------------------------------------------------------------------
# 容器解码
# ---------------------------------------------------------------------------


def test_container_roundtrip():
    data = _container(_record(_book_content()))
    assert ip.decode_container(data) == _record(_book_content())


def test_container_too_short_raises():
    with pytest.raises(ip.ImgParseError):
        ip.decode_container(b"\x01\x02\x03")


def test_container_comp_len_out_of_range_raises():
    data = bytearray(_container(_record(_book_content())))
    data[8:16] = (10 ** 9).to_bytes(8, "little")  # 声明巨大压缩长度
    with pytest.raises(ip.ImgParseError):
        ip.decode_container(bytes(data))


def test_container_bad_zlib_raises():
    body = b"not-zlib-at-all"
    hdr = b"TEST" + b"\x00" * 4 + len(body).to_bytes(8, "little") + b"\x00" * 8
    with pytest.raises(ip.ImgParseError):
        ip.decode_container(hdr + body)


def test_container_decomp_len_mismatch_raises():
    """头声明的解压长度与实际不符 → 显式报错(样本漂移不静默)。"""
    data = bytearray(_container(_record(_book_content())))
    data[16:24] = (12345).to_bytes(8, "little")
    with pytest.raises(ip.ImgParseError):
        ip.decode_container(bytes(data))


# ---------------------------------------------------------------------------
# 帧 / 字段扫描
# ---------------------------------------------------------------------------


def test_iter_records_splits_frames():
    raw = _record(b"A") + _record(b"B") + _record(b"C")
    assert list(ip.iter_records(raw)) == [b"A", b"B", b"C"]


def test_iter_records_drops_incomplete_tail():
    raw = _record(b"A") + b"\x03B"  # 末帧缺 0x04
    assert list(ip.iter_records(raw)) == [b"A"]


def test_iter_fields_tag_value():
    content = _join(_f("0T", "93000.000"), _f("20", "10.500000"), _f("30", "1000"))
    assert list(ip.iter_fields(content)) == [
        ("0T", b"93000.000"), ("20", b"10.500000"), ("30", b"1000"),
    ]


def test_iter_fields_skips_short_chunk():
    content = _join(_f("20", "10.5"), b"X")  # b"X" 不足 tag 宽度
    assert list(ip.iter_fields(content)) == [("20", b"10.5")]


# ---------------------------------------------------------------------------
# 十档装配
# ---------------------------------------------------------------------------


def test_decode_full_depth_decimal_prices_and_share_vols():
    snap = _decode(_book_content())
    assert snap.t == "09:30:00"
    assert snap.bid_prices[:3] == [10.50, 10.49, 10.48]
    assert snap.bid_vols[:3] == [1000, 2000, 3000]
    assert snap.ask_prices[:3] == [10.51, 10.52, 10.53]
    assert snap.ask_vols[:3] == [2000, 3000, 4000]
    # 未出现的档位必须是 None, 不得补 0(0 是有意义的挂单量)
    assert snap.bid_prices[3:] == [None] * 7
    assert snap.ask_vols[3:] == [None] * 7


def test_decode_missing_levels_not_zero_filled():
    content = _join(*_depth(bid_prices=(10.5,), bid_vols=(0,), ask_prices=(10.6,), ask_vols=(0,)))
    snap = _decode(content)
    assert snap.bid_vols[0] == 0            # 显式 0 保留为 0
    assert snap.bid_vols[1] is None         # 未出现 ≠ 0
    assert snap.ask_prices[1] is None


def test_decode_bid1_fallback_from_tag_04_05():
    content = _join(_f("04", "9.990000"), _f("05", "1234"))
    snap = _decode(content)
    assert snap.bid_prices[0] == 9.99
    assert snap.bid_vols[0] == 1234


def test_decode_time_hhmmss_variants():
    assert _decode(_join(_f("0T", "83627.000"))).t == "08:36:27"
    assert _decode(_join(_f("0T", "91500.000"))).t == "09:15:00"
    assert _decode(_join(_f("0T", "155927.000"))).t == "15:59:27"


def test_decode_invalid_time_is_none():
    assert _decode(_join(_f("0T", "999999.000"))).t is None   # 分秒越界
    assert _decode(_join(_f("0T", "abcd"))).t is None         # 非数值
    assert _decode(_join(_f("20", "10.5"))).t is None         # 无时间标签


# ---------------------------------------------------------------------------
# 增量语义
# ---------------------------------------------------------------------------


def test_delta_missing_levels_carried_from_prev():
    full = _decode(_book_content())
    partial = _decode(_join(_f("0T", "93003.000"), _f("20", "10.510000"), _f("30", "1500")),
                      prev=full)
    # 本帧显式给了买一 → 用新值
    assert partial.bid_prices[0] == 10.51
    assert partial.bid_vols[0] == 1500
    # 本帧没给买二/买三 → 沿用上一帧
    assert partial.bid_prices[1] == 10.49 and partial.bid_vols[1] == 2000
    assert partial.ask_prices[0] == 10.51 and partial.ask_vols[0] == 2000


def test_delta_disabled_leaves_none():
    full = _decode(_book_content())
    partial = _decode(_join(_f("0T", "93003.000"), _f("20", "10.510000")), prev=full, delta=False)
    assert partial.bid_prices[0] == 10.51
    assert partial.bid_prices[1] is None
    assert partial.ask_vols[0] is None


def test_delta_carries_order_counts():
    full = _decode(_book_content(bid_orders=7, ask_orders=9))
    partial = _decode(_join(_f("0T", "93003.000")), prev=full)
    assert partial.bid_orders == 7 and partial.ask_orders == 9


# ---------------------------------------------------------------------------
# 委托队列 / 笔数
# ---------------------------------------------------------------------------


def test_queue_split_by_ask_section_tag():
    content = _book_content(bid_queue=[500, 300], ask_queue=[5500, 300, 600],
                            bid_orders=2, ask_orders=3)
    snap = _decode(content)
    assert snap.bid_queue == [500, 300]
    assert snap.queue == [5500, 300, 600]      # queue = 卖一委托队列(历史语义)
    assert snap.bid_orders == 2 and snap.ask_orders == 3


def test_queue_positional_fallback_without_section_tag():
    """无标签 61 → 退回位置口径: 第一段=买一, 第二段=卖一。"""
    content = _join(
        *_depth(bid_prices=(10.5,), bid_vols=(500,), ask_prices=(10.6,), ask_vols=(700,)),
        _f("64", "500"),
        _f("62", "1"),
        _f("64", "700"),
    )
    snap = _decode(content)
    assert snap.bid_queue == [500]
    assert snap.queue == [700]


def test_queue_absent_is_none():
    snap = _decode(_book_content())
    assert snap.bid_queue is None and snap.queue is None


def test_single_order_count_tag_only_bid():
    content = _join(*_depth(bid_prices=(10.5,), bid_vols=(1,), ask_prices=(10.6,), ask_vols=(1,)),
                    _f("62", "3"))
    snap = _decode(content)
    assert snap.bid_orders == 3 and snap.ask_orders is None


# ---------------------------------------------------------------------------
# 派生指标 / 同构输出
# ---------------------------------------------------------------------------


def test_derived_metrics():
    snap = _decode(_book_content(ask_queue=[9000], ask_orders=1))
    assert snap.best_bid() == 10.50 and snap.best_ask() == 10.51
    assert snap.spread() == pytest.approx(0.01)
    # 买量 6000 / (买 6000 + 卖 9000)
    assert snap.bid_pressure() == pytest.approx(6000 / 15000)
    # queue = 卖一委托队列 → 队列 9000 - 卖一量 2000
    assert snap.queue == [9000]
    assert snap.queue_imbalance() == 7000


def test_zero_volume_pressure_is_none_not_zero():
    content = _join(*_depth(bid_prices=(10.5,), bid_vols=(0,), ask_prices=(10.6,), ask_vols=(0,)))
    snap = _decode(content)
    assert snap.bid_pressure() is None      # 不允许 0.0 冒充「均衡」


def test_snapshots_to_frames_marks_missing():
    snap = _decode(_join(*_depth(bid_prices=(10.5,), bid_vols=(1,), ask_prices=(10.6,), ask_vols=(1,))))
    f = ip.snapshots_to_frames([snap], top_n=3)[0]
    assert f["t"] == "09:30:00"
    assert f["bid"][0] == {"price": 10.5, "vol": 1}
    assert f["bid"][1] == {"price": ip.MISSING, "vol": ip.MISSING}
    assert f["queue"] == ip.MISSING
    assert f["queue_imb"] == ip.MISSING
    assert f["bid_orders"] == ip.MISSING


# ---------------------------------------------------------------------------
# 文件层 错误态
# ---------------------------------------------------------------------------


def test_parse_img_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ip.parse_img(tmp_path / "nope.img")


def test_parse_img_empty_file_raises(tmp_path):
    p = tmp_path / "empty.img"
    p.write_bytes(b"")
    with pytest.raises(ip.ImgParseError):
        ip.parse_img(p)


def test_parse_img_no_frames_raises(tmp_path):
    p = tmp_path / "noframes.img"
    p.write_bytes(_container())  # 头合法但无帧
    with pytest.raises(ip.ImgParseError):
        ip.parse_img(p)


def test_parse_img_corrupt_container_raises(tmp_path):
    p = tmp_path / "bad.img"
    p.write_bytes(b"TEST" + b"\x00" * 4 + (5).to_bytes(8, "little") + b"\x00" * 8 + b"junk!")
    with pytest.raises(ip.ImgParseError):
        ip.parse_img(p)


def test_parse_img_multiframe_and_frames_from_img_alias(tmp_path):
    content = _book_content()
    p = tmp_path / "two.img"
    p.write_bytes(_container(_record(content), _record(_book_content(t="93003.000"))))
    snaps = ip.parse_img(p)
    assert [s.t for s in snaps] == ["09:30:00", "09:30:03"]
    assert [s.t for s in ip.frames_from_img(p)] == ["09:30:00", "09:30:03"]


# ---------------------------------------------------------------------------
# 样本驱动校准(真实样本 fixture)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FIX_OPEN.is_file(), reason="缺真实样本 fixture")
class TestCalibrationOnRealFixture:
    def test_report_container_and_frames(self):
        r = ip.calibrate_from_sample(FIX_OPEN)
        assert r.n_frames == 120
        assert r.compressed_len_matches is True     # 压缩长度 == 文件长-24
        assert r.decomp_len_matches is True         # zlib 实得 == 头声明
        assert r.reserved_zero is True
        assert r.time_first == "08:36:27"
        assert r.time_last == "09:32:18"
        assert r.frame_gap_mode_s is not None       # 该窗口跨盘前/竞价, 间隔众数非 3s
        assert r.frame_with_all_depth >= 1          # 存在全量帧
        assert r.frame_with_queue > 0

    def test_continuous_session_cadence_is_3s(self):
        """连续竞价窗口的帧间隔众数 = 3s(与「约 3 秒一帧」一致)。"""
        r = ip.calibrate_from_sample(FIX_0828)
        assert r.time_first == "09:30:03" and r.time_last == "09:33:00"
        assert r.frame_gap_mode_s == 3.0

    def test_price_scale_is_decimal_not_fixedpoint(self):
        r = ip.calibrate_from_sample(FIX_OPEN)
        assert r.price_within_plausible_range is True
        # 定点 1e3 会把 "10.31" 解成 0.0103 或巨值; 实测价格在合理股价区间
        assert all(1.0 <= p <= 100.0 for p in r.price_samples)

    def test_queue_two_segments_and_exact_when_untruncated(self):
        """队列两段语义 + 非截断时 sum(队列) == 该档总量(100%)。"""
        r = ip.calibrate_from_sample(FIX_OPEN)
        assert r.queue_two_segments > 0
        assert r.queue_full_checked > 0
        assert r.queue_full_checked == r.queue_full_equal   # 非截断 100% 精确
        assert r.queue_truncated_frames > 0                 # 存在截断帧

    def test_second_sample_different_magic_still_parses(self):
        r = ip.calibrate_from_sample(FIX_0828)
        assert r.magic != ip.calibrate_from_sample(FIX_OPEN).magic  # magic 随文件变
        assert r.compressed_len_matches is True
        assert r.decomp_len_matches is True
        assert r.n_frames == 60
        assert r.queue_full_checked == r.queue_full_equal


def test_calibration_raises_on_missing_sample(tmp_path):
    """样本缺失必须抛错(不返回假报告)。"""
    with pytest.raises(FileNotFoundError):
        ip.calibrate_from_sample(tmp_path / "nope.img")


class TestRealFixtureDepth:
    """真实 fixture 上的十档 / 增量 / 队列钉死值。"""

    def test_open_fixture_known_frames(self):
        snaps = ip.parse_img(FIX_OPEN)
        assert len(snaps) == 120
        # 首帧盘前全 0(显式 0, 非缺失)
        assert snaps[0].t == "08:36:27"
        assert snaps[0].bid_prices[0] == 0.0 and snaps[0].bid_vols[0] == 0
        # 竞价帧
        assert snaps[3].t == "09:15:09"
        assert snaps[3].bid_prices[0] == 10.35 and snaps[3].bid_vols[0] == 9300
        # 开盘后: 十档 + 两段队列 + 笔数
        s = snaps[73]
        assert s.t == "09:30:00"
        assert s.bid_prices[:3] == [10.27, 10.26, 10.25]
        assert s.ask_prices[:3] == [10.29, 10.30, 10.31]
        assert s.bid_queue == [500]
        assert s.queue == [5500, 300, 600, 300]
        assert s.bid_orders == 1 and s.ask_orders == 4
        assert s.spread() == pytest.approx(0.02)

    def test_carry_forward_yields_monotonic_ladder(self):
        """增量沿用后, 连续竞价帧的买卖十档严格单调(买降 / 卖升)—— 证明沿用语义正确。"""
        for fixture, want in ((FIX_OPEN, 56), (FIX_0828, 59)):
            checked = bad = 0
            for s in ip.parse_img(fixture):
                bp = [p for p in s.bid_prices if p]
                ap = [p for p in s.ask_prices if p]
                if len(bp) == 10 and len(ap) == 10:
                    checked += 1
                    if any(bp[i] < bp[i + 1] for i in range(9)):
                        bad += 1
                    if any(ap[i] > ap[i + 1] for i in range(9)):
                        bad += 1
            assert checked == want, f"{fixture.name}: 完整档位帧数变了"
            assert bad == 0, f"{fixture.name}: 出现非单调档位"

    def test_without_carry_forward_ladder_is_incomplete(self):
        """关掉增量沿用后完整档位帧骤减 —— 反证「缺失=未变」。"""
        def _full(snaps):
            return sum(
                1 for s in snaps
                if len([p for p in s.bid_prices if p]) == 10
                and len([p for p in s.ask_prices if p]) == 10
            )

        carried = _full(ip.parse_img(FIX_OPEN))
        raw = _full(ip.parse_img(FIX_OPEN, delta=False))
        assert carried > raw

    def test_truncated_queue_smaller_than_level_volume(self):
        """截断帧(委托笔数 > 队列条数): sum(队列) < 该档总量 —— 档位总量须用标签 30/50。"""
        snaps = ip.parse_img(FIX_OPEN)
        trunc = [s for s in snaps if s.bid_queue and len(s.bid_queue) == 50
                 and (s.bid_orders or 0) > 50]
        assert trunc, "fixture 应含截断帧"
        s = trunc[0]
        assert sum(s.bid_queue) < s.bid_vols[0]

    def test_untruncated_queue_equals_level_volume(self):
        """非截断帧(委托笔数 == 队列条数): sum(队列) == 该档总量。"""
        snaps = ip.parse_img(FIX_OPEN)
        exact = [s for s in snaps if s.bid_queue and s.bid_orders == len(s.bid_queue)]
        assert exact, "fixture 应含非截断帧"
        for s in exact:
            assert sum(s.bid_queue) == s.bid_vols[0]


@pytest.mark.skipif(not HAS_SAMPLE, reason="未提供真实 .img 样本(TDX_IMG_SAMPLE)")
def test_parse_img_real_sample():
    """真实样本校准入口(设 TDX_IMG_SAMPLE 指向一份真实 .img 即启用)。"""
    snaps = ip.parse_img(str(SAMPLE))
    assert len(snaps) > 1
    s = next((x for x in snaps if x.bid_prices[0] and x.ask_prices[0]), None)
    assert s is not None
    assert s.bid_prices[0] < s.ask_prices[0]     # 卖一必须高于买一
