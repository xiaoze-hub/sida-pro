"""Chan-theory v1: sequential merged bars (合并K线) and fractals (分型).

Scope and input contract
------------------------
Pure computation only.  Callers must pass **completed** bars: this module cannot
know the trading session or whether the final supplied bar is still forming.
It never inspects anything after the last input bar, so every returned snapshot
is a deterministic replay of exactly that prefix.

Sequential merge rules
----------------------
* Containment is inclusive: A contains B when ``A.high >= B.high`` and
  ``A.low <= B.low`` (or the reverse).  Equal highs/lows therefore merge.
* Direction is read only from the last two merged bars: both high and low
  strictly higher means ``up``, both lower means ``down``.  After inclusive
  containment processing adjacent merged bars can never be equal, so this is
  unambiguous.
* Known ``up``: keep ``max(high)`` and ``max(low)``.  Known ``down``: keep
  ``min(high)`` and ``min(low)``.
* When only one merged bar exists there is no direction.  The kernel keeps the
  envelope (``max(high)`` / ``min(low)``) and records
  ``merge_direction='unknown'``.  It does **not** guess ``up`` from bars that
  arrive later.  Such a bar is marked ``envelope_only=True``: its range is an
  enumeration envelope, not the result of a Chan merge rule.
* A single raw bar is also ``merge_direction='unknown'`` but ``envelope_only=False``:
  no merge decision was made, its high/low are the exact raw extremes, so it
  stays usable as a fractal neighbour.
* Every absorbed raw bar index is retained in ``sources``; the day of each
  extreme is tracked separately from the merged bar's start/end days.

Fractals
--------
* A strict top/bottom needs three consecutive merged bars, with the middle
  strictly above/below both neighbours on high *and* low.
* While the right merged bar is still open the middle is only a ``candidate``.
* It becomes ``confirmed`` at the first raw bar that creates the following
  merged bar: that seals the right bar, making the three-bar window final.
* A window containing an ``envelope_only`` bar is never confirmed: it is
  emitted as ``blocked`` with ``confirmed_on=None``.  Unknown direction must
  not produce deterministic structure.  Blocked is terminal, because sealed
  merged bars are immutable and the taint travels with the bar.  Confirmation
  resumes for windows built entirely from clean merged bars — in practice once
  three merged bars after the envelope exist.
* ``position_day`` is the extremum day; ``confirmed_on`` is the later day the
  right bar was sealed.  Position and confirmation time are deliberately
  separate fields.

Not implemented (do not claim otherwise)
----------------------------------------
No 笔 (pens), 线段 (segments), 中枢 (centres), buy/sell points, multi-level
recursion, volume/effort rules or trading signals.  Missing trading days are
neither detected nor filled; timestamps are only required to be strictly
increasing.  A date gap is reported honestly by keeping the real bar days,
never by inventing a bar.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from math import isfinite
from typing import Literal, Sequence

__all__ = [
    'ChanSnapshot',
    'Fractal',
    'MergedBar',
    'RawBar',
    'RawExtremum',
    'build_chan_structure',
    'detect_raw_extrema',
]

_Direction = Literal['up', 'down', 'unknown']
_FractalKind = Literal['top', 'bottom']
_FractalStatus = Literal['candidate', 'confirmed', 'blocked']


@dataclass(frozen=True)
class RawBar:
    """One completed raw bar.  ``day`` must be an ISO date string."""

    day: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class RawExtremum:
    """Legacy raw-bar extremum; not a Chan fractal."""

    kind: Literal['top', 'bottom']
    position_day: str
    confirmed_on: str
    price: float


@dataclass(frozen=True)
class MergedBar:
    """A containment-processed bar.

    ``high_day``/``low_day`` are position times of the kept extremes, while
    ``sealed_on`` is the day the bar became final (``None`` for the still-open
    tail bar).  ``start_day``/``end_day`` bound the raw bars it absorbed.

    ``envelope_only`` is ``True`` only when the bar absorbed bars while no
    direction was known, so ``high``/``low`` are ``max``/``min`` of an envelope
    rather than a Chan merge result.  Such a bar may not take part in a
    confirmed fractal.  A single raw bar also has ``merge_direction='unknown'``
    but is *not* an envelope and keeps ``envelope_only=False``.
    """

    index: int
    high: float
    low: float
    high_day: str
    low_day: str
    start_day: str
    end_day: str
    sources: tuple[int, ...]
    merge_direction: _Direction
    envelope_only: bool
    sealed_on: str | None


@dataclass(frozen=True)
class Fractal:
    """A three-merged-bar top/bottom, possibly still a candidate.

    ``position_merged_index``/``position_day``/``position_price`` locate the
    extremum.  ``confirmed_on`` is ``None`` until the right merged bar is
    sealed; it is never equal to a position time by construction.  ``blocked``
    means the window contains an ``envelope_only`` bar from an unknown-direction
    merge: the shape is reported for audit only and never seen as confirmed.
    """

    kind: _FractalKind
    status: _FractalStatus
    position_merged_index: int
    position_day: str
    position_price: float
    confirmed_on: str | None


@dataclass(frozen=True)
class ChanSnapshot:
    """Replayable result for one input prefix."""

    as_of_day: str | None
    merged_bars: tuple[MergedBar, ...]
    fractals: tuple[Fractal, ...]

    @property
    def confirmed_fractals(self) -> tuple[Fractal, ...]:
        return tuple(f for f in self.fractals if f.status == 'confirmed')

    @property
    def candidates(self) -> tuple[Fractal, ...]:
        return tuple(f for f in self.fractals if f.status == 'candidate')

    @property
    def blocked_fractals(self) -> tuple[Fractal, ...]:
        """Windows touching an unknown-direction envelope; audit only."""
        return tuple(f for f in self.fractals if f.status == 'blocked')


def _validate_bars(bars: Sequence[RawBar]) -> None:
    """Reject malformed OHLC and non-increasing ISO dates."""
    previous = None
    for bar in bars:
        try:
            day = date.fromisoformat(bar.day)
            values = (bar.open, bar.high, bar.low, bar.close)
            valid = all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)
                for v in values
            )
        except (TypeError, ValueError):
            raise ValueError('invalid bar') from None
        if (
            not valid
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
            or bar.low <= 0
        ):
            raise ValueError('invalid OHLC')
        if previous is not None and day <= previous:
            raise ValueError('bar dates must be strictly increasing')
        previous = day


def detect_raw_extrema(bars: Sequence[RawBar]) -> list[RawExtremum]:
    """Legacy: strict raw three-bar extrema with no containment processing."""
    _validate_bars(bars)
    out: list[RawExtremum] = []
    for left, middle, right in zip(bars, bars[1:], bars[2:]):
        if middle.high > left.high and middle.high > right.high and middle.low > left.low and middle.low > right.low:
            out.append(RawExtremum('top', middle.day, right.day, middle.high))
        elif middle.low < left.low and middle.low < right.low and middle.high < left.high and middle.high < right.high:
            out.append(RawExtremum('bottom', middle.day, right.day, middle.low))
    return out


def _contains(bar: MergedBar, high: float, low: float) -> bool:
    """Inclusive containment between a merged bar and a raw range."""
    return (bar.high >= high and bar.low <= low) or (high >= bar.high and low <= bar.low)


def _direction_of_pair(previous: MergedBar, last: MergedBar) -> _Direction:
    if last.high > previous.high and last.low > previous.low:
        return 'up'
    if last.high < previous.high and last.low < previous.low:
        return 'down'
    return 'unknown'


def _known_direction(merged: Sequence[MergedBar]) -> _Direction:
    """Direction available from bars already seen; never from later bars."""
    if len(merged) < 2:
        return 'unknown'
    return _direction_of_pair(merged[-2], merged[-1])


def _merge(last: MergedBar, raw: RawBar, index: int, direction: _Direction) -> MergedBar:
    """Absorb one raw bar into the open merged bar using ``direction``.

    Ties on an extreme keep the earlier extreme day, so a bar's recorded
    position never moves merely because an equal price arrives later.

    ``direction='unknown'`` is an envelope merge: the range is a bounding box,
    not a Chan merge result, so the bar is flagged ``envelope_only`` and can no
    longer support a confirmed fractal.
    """
    if direction == 'up':
        high = max(last.high, raw.high)
        low = max(last.low, raw.low)
        high_day = last.high_day if last.high >= raw.high else raw.day
        low_day = last.low_day if last.low >= raw.low else raw.day
    elif direction == 'down':
        high = min(last.high, raw.high)
        low = min(last.low, raw.low)
        high_day = last.high_day if last.high <= raw.high else raw.day
        low_day = last.low_day if last.low <= raw.low else raw.day
    else:
        # No direction is known yet: keep the envelope, do not pick a trend.
        high = max(last.high, raw.high)
        low = min(last.low, raw.low)
        high_day = last.high_day if last.high >= raw.high else raw.day
        low_day = last.low_day if last.low <= raw.low else raw.day
    return replace(
        last,
        high=high,
        low=low,
        high_day=high_day,
        low_day=low_day,
        end_day=raw.day,
        sources=last.sources + (index,),
        merge_direction=direction,
        envelope_only=last.envelope_only or direction == 'unknown',
    )


def _new_merged_bar(index: int, raw: RawBar, direction: _Direction) -> MergedBar:
    return MergedBar(
        index=index,
        high=raw.high,
        low=raw.low,
        high_day=raw.day,
        low_day=raw.day,
        start_day=raw.day,
        end_day=raw.day,
        sources=(index,),
        merge_direction=direction,
        envelope_only=False,  # one exact raw range: nothing was merged yet
        sealed_on=None,
    )


def _detect_fractals(merged: Sequence[MergedBar]) -> tuple[Fractal, ...]:
    out: list[Fractal] = []
    for middle_index in range(1, len(merged) - 1):
        left, middle, right = merged[middle_index - 1], merged[middle_index], merged[middle_index + 1]
        if (
            middle.high > left.high
            and middle.low > left.low
            and middle.high > right.high
            and middle.low > right.low
        ):
            kind: _FractalKind = 'top'
            position_day, position_price = middle.high_day, middle.high
        elif (
            middle.low < left.low
            and middle.high < left.high
            and middle.low < right.low
            and middle.high < right.high
        ):
            kind = 'bottom'
            position_day, position_price = middle.low_day, middle.low
        else:
            continue
        # Unknown direction must not produce deterministic structure: any
        # window touching an envelope from an unknown-direction merge is
        # reported as blocked and never as confirmed.
        blocked = left.envelope_only or middle.envelope_only or right.envelope_only
        confirmed_on = None if blocked else right.sealed_on
        if blocked:
            status: _FractalStatus = 'blocked'
        else:
            status = 'confirmed' if confirmed_on is not None else 'candidate'
        out.append(
            Fractal(
                kind=kind,
                status=status,
                position_merged_index=middle_index,
                position_day=position_day,
                position_price=position_price,
                confirmed_on=confirmed_on,
            )
        )
    return tuple(out)


def build_chan_structure(bars: Sequence[RawBar]) -> ChanSnapshot:
    """Process completed bars left to right and return the replayable snapshot.

    The input is treated as a full history prefix.  The last merged bar is
    necessarily still open (``sealed_on is None``); any fractal using it as the
    right bar is a candidate and may still disappear.
    """
    _validate_bars(bars)
    merged: list[MergedBar] = []
    for index, raw in enumerate(bars):
        if not merged:
            merged.append(_new_merged_bar(index, raw, 'unknown'))
            continue
        last = merged[-1]
        if _contains(last, raw.high, raw.low):
            direction = _known_direction(merged)
            merged[-1] = _merge(last, raw, index, direction)
        else:
            direction = 'up' if raw.high > last.high else 'down'
            merged[-1] = replace(last, sealed_on=raw.day)
            merged.append(_new_merged_bar(index, raw, direction))
    final = tuple(merged)
    return ChanSnapshot(
        as_of_day=bars[-1].day if bars else None,
        merged_bars=final,
        fractals=_detect_fractals(final),
    )
