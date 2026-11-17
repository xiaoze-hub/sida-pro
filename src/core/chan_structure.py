"""Experimental three-raw-bar extrema; not Chan-theory fractals.

No containment processing, pen, centre or trading signal is implemented here.
The middle bar is only confirmed when the next complete bar is available.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Literal, Sequence


@dataclass(frozen=True)
class RawBar:
    day: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class RawExtremum:
    kind: Literal['top', 'bottom']
    position_day: str
    confirmed_on: str
    price: float


def detect_raw_extrema(bars: Sequence[RawBar]) -> list[RawExtremum]:
    """Find strict non-contained local extrema in completed input bars only.

    Callers must exclude an unfinished last bar; this pure primitive does not
    know the market session or whether the supplied input is final.
    """
    previous = None
    for bar in bars:
        try:
            day = date.fromisoformat(bar.day)
            values = (bar.open, bar.high, bar.low, bar.close)
            valid = all(isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) for v in values)
        except (TypeError, ValueError):
            raise ValueError('invalid bar') from None
        if not valid or bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.low <= 0:
            raise ValueError('invalid OHLC')
        if previous is not None and day <= previous:
            raise ValueError('bar dates must be strictly increasing')
        previous = day
    out: list[RawExtremum] = []
    for left, middle, right in zip(bars, bars[1:], bars[2:]):
        if middle.high > left.high and middle.high > right.high and middle.low > left.low and middle.low > right.low:
            out.append(RawExtremum('top', middle.day, right.day, middle.high))
        elif middle.low < left.low and middle.low < right.low and middle.high < left.high and middle.high < right.high:
            out.append(RawExtremum('bottom', middle.day, right.day, middle.low))
    return out
