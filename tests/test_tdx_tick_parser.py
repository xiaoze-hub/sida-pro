"""通达信 .tck 解析器 + dark_l2 数据源接入层 单测。

样本文件(需手工超盘回放落盘): /home/ubuntu/tdx_data/sz002361_20260827.tck
无样本时解析类测试 skip, 纯函数测试(方向/过滤/回退)始终运行。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/marketdata/src"))

from src.core.tdx_tick_parser import parse_tck, ticks_from_tck, _tck_t_to_hms  # noqa: E402
from src.core import dark_l2  # noqa: E402

SAMPLE = Path("/home/ubuntu/tdx_data/sz002361_20260827.tck")
HAS_SAMPLE = SAMPLE.is_file()


def test_tck_t_to_hms():
    assert _tck_t_to_hms(93_000_000) == "09:30:00"
    assert _tck_t_to_hms(145_959_999) == "14:59:59"
    assert _tck_t_to_hms(100_000_000) == "10:00:00"


def test_ticks_from_tck_filters_auction_and_noise():
    trades = [
        # 集合竞价(9:25) + 正常价 → 应被剔除
        {"seq": 1, "t": 92_500_000, "price": 10.5, "vol": 100, "dir": "B", "amt": 1050},
        # 噪声行(price=1.0) → 剔除
        {"seq": 2, "t": 93_100_000, "price": 1.0, "vol": 100, "dir": "B", "amt": 100},
        # 修正行(price=0) → 剔除
        {"seq": 3, "t": 93_200_000, "price": 0.0, "vol": 100, "dir": "S", "amt": 0},
        # 连续竞价正常买
        {"seq": 4, "t": 93_300_000, "price": 10.6, "vol": 200, "dir": "B", "amt": 2120},
        # 连续竞价正常卖
        {"seq": 5, "t": 93_400_000, "price": 10.7, "vol": 300, "dir": "S", "amt": 3210},
    ]
    ticks = ticks_from_tck(trades)
    assert len(ticks) == 2
    assert [t["seq"] for t in ticks] == [4, 5]
    assert ticks[0]["d"] == "B" and ticks[1]["d"] == "S"
    assert ticks[0]["t"] == "09:33:00"


@pytest.mark.skipif(not HAS_SAMPLE, reason="无 .tck 样本文件")
def test_parse_tck_sample():
    trades, orders, cancels = parse_tck(str(SAMPLE))
    assert len(trades) > 1000
    assert len(orders) > 0
    # 逐笔方向只能是官方 2B/2S → 'B'/'S'
    assert all(t["dir"] in ("B", "S") for t in trades[:100])
    # 连续竞价过滤后有效
    ticks = ticks_from_tck(trades)
    assert len(ticks) > 1000
    assert all(t["t"] >= "09:30:00" for t in ticks)


def test_fetch_l2_ticks_tdx_hit(tmp_path, monkeypatch):
    # 造一个假 .tck 文件(最小合法: 头 + zlib 空) → 解析会失败, 但验证文件发现逻辑
    # 更直接: 用样本目录测命中
    if not HAS_SAMPLE:
        pytest.skip("无样本")
    monkeypatch.setenv("TDX_TCK_DIR", str(SAMPLE.parent))
    ticks = dark_l2.fetch_l2_ticks("sz002361", "tdx_tck")
    assert len(ticks) > 1000


def test_fetch_l2_ticks_miss_raises(monkeypatch):
    monkeypatch.setenv("TDX_TCK_DIR", "/tmp/不存在_tdx_tck目录")
    with pytest.raises(FileNotFoundError):
        dark_l2.fetch_l2_ticks("sz002361", "tdx_tck")


def test_fetch_l2_ticks_unknown_source():
    # 2026-09-01: dark_l2.fetch_l2_ticks 不支持未知 source 时抛 NotImplementedError(非 ValueError)
    with pytest.raises(NotImplementedError):
        dark_l2.fetch_l2_ticks("sz002361", "l2_xxx")
