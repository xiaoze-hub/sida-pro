"""Experimental raw-bar extrema, not Chan-theory fractals."""
import pytest

from src.core.chan_structure import RawBar, detect_raw_extrema


def b(day, high, low):
    return RawBar(day=day, open=low, high=high, low=low, close=high)


def test_strict_top_has_separate_position_and_confirmation():
    out = detect_raw_extrema([b('2026-01-01', 3, 1), b('2026-01-02', 5, 2), b('2026-01-03', 4, 1)])
    assert len(out) == 1
    assert (out[0].kind, out[0].position_day, out[0].confirmed_on, out[0].price) == ('top', '2026-01-02', '2026-01-03', 5)


def test_equal_high_is_not_a_strict_top():
    assert not detect_raw_extrema([b('2026-01-01', 3, 1), b('2026-01-02', 5, 2), b('2026-01-03', 5, 1)])


def test_confirmation_is_stable_for_each_truncated_prefix():
    bars = [b('2026-01-01', 3, 1), b('2026-01-02', 5, 2), b('2026-01-03', 4, 1), b('2026-01-04', 6, 3)]
    full = detect_raw_extrema(bars)
    for end in range(1, len(bars) + 1):
        prefix = detect_raw_extrema(bars[:end])
        assert prefix == [x for x in full if x.confirmed_on <= bars[end - 1].day]


def test_bottom_and_containment_are_distinct():
    bars = [b('2026-01-01', 6, 3), b('2026-01-02', 4, 1), b('2026-01-03', 5, 2)]
    out = detect_raw_extrema(bars)
    assert [(x.kind, x.position_day, x.confirmed_on, x.price) for x in out] == [
        ('bottom', '2026-01-02', '2026-01-03', 1)
    ]
    assert detect_raw_extrema([b('2026-01-01', 4, 2), b('2026-01-02', 5, 1), b('2026-01-03', 4, 2)]) == []


def test_reject_bad_dates_and_ohlc():
    with pytest.raises(ValueError):
        detect_raw_extrema([b('2026-01-02', 2, 1), b('2026-01-01', 2, 1)])
    with pytest.raises(ValueError):
        detect_raw_extrema([RawBar(day='2026-01-01', open=3, high=2, low=1, close=2)])
    with pytest.raises(ValueError):
        detect_raw_extrema([b('2026-01-01', float('nan'), 1)])
