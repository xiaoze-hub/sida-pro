"""B1.1(KI-040): K 线入库前校验 —— 硬错误拒绝, 可疑跳变标记。"""

from __future__ import annotations

from src.collectors.kline_collector import KlineData
from src.collectors.klines_ingestor import _HARD_REJECT_REASONS, validate_bar


def _k(o, h, l, c, v=1_000_000, date="2026-09-01"):
    return KlineData(date=date, open=o, high=h, low=l, close=c, volume=v)


def test_valid_bar_passes():
    assert validate_bar("600000", _k(10.0, 10.5, 9.8, 10.2), prev_close=10.0) is None


def test_ohlc_relation_rejected():
    assert validate_bar("600000", _k(10.0, 9.5, 9.0, 9.8), prev_close=None) == "ohlc_relation"
    # low 高于 open/close
    assert validate_bar("600000", _k(10.0, 10.5, 10.1, 10.2), prev_close=None) == "ohlc_relation"


def test_non_positive_price_rejected():
    assert validate_bar("600000", _k(0.0, 10.5, 0.0, 10.2), prev_close=None) == "non_positive_price"
    assert validate_bar("600000", _k(10.0, 10.5, -1.0, 10.2), prev_close=None) == "non_positive_price"


def test_unparsable_and_negative_volume():
    class Bad:
        date = "2026-09-01"
        open = "abc"
        high = 1
        low = 1
        close = 1
        volume = 1

    assert validate_bar("600000", Bad(), prev_close=None) == "unparsable"
    assert validate_bar("600000", _k(10, 10.5, 9.8, 10.2, v=-5), prev_close=None) == "negative_volume"


def test_jump_beyond_limit_flagged_not_rejected():
    """主板 10%: 昨收 10 → 收 11.5 超限 → 标记(不进硬错误名单)。"""
    reason = validate_bar("600000", _k(10.0, 11.6, 10.0, 11.5), prev_close=10.0)
    assert reason == "jump_beyond_limit"
    assert reason not in _HARD_REJECT_REASONS
    # 显式非 ST(主板 10%): 涨停 11.00 恰好合法
    assert validate_bar("600000", _k(10.5, 11.0, 10.4, 11.0), prev_close=10.0, is_st=False) is None
    # ST 未知 → 保守 5%: 同一条(10→11, +10%)判为可疑跳变
    assert validate_bar("600000", _k(10.5, 11.0, 10.4, 11.0), prev_close=10.0) == "jump_beyond_limit"


def test_jump_uses_board_limit():
    """创业板 20%: 10 → 11.5 合法; 13.0 超限。"""
    assert validate_bar("300001", _k(10.5, 11.6, 10.4, 11.5), prev_close=10.0) is None
    assert validate_bar("300001", _k(12.0, 13.1, 11.9, 13.0), prev_close=10.0) == "jump_beyond_limit"


def test_jump_unknown_st_is_conservative():
    """ST 未知 → 按 5% 判: 主板 10 → 10.8 即超限。"""
    assert validate_bar("600000", _k(10.0, 10.9, 10.0, 10.8), prev_close=10.0) == "jump_beyond_limit"
    # 显式非 ST → 10% 内合法
    assert validate_bar("600000", _k(10.0, 10.9, 10.0, 10.8), prev_close=10.0, is_st=False) is None
