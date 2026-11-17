"""Chan theory **operational** centre (操作中枢) over externally supplied segments.

Read this before using the output
---------------------------------
The 18th-lesson centre (第18课 中枢) is defined on **completed sub-level 走势类型**:
three consecutive completed sub-level trends/moves whose ranges overlap.  This
module does not compute that.  It computes the same *geometry* over a chain of
**segment-level (a0) legs that the caller supplies and vouches for**, and it says
so in every output:

``input_kind='externally_supplied_completed_segments'``, ``basis='segment_a0'``,
``strictness='operational_not_strict'``, ``segments_engine='none'``.

So: no real 线段 (segment) kernel is wired in — no fractal/pen → segment
recursion, no 特征序列 gap handling, no segment destruction.  Treating a segment
as a0 is an engineering choice, not a strict reading of the theory.  Anything
that displays this output must not call it a 缠论中枢 without the qualifier.

What is actually computed
-------------------------
One centre at most, from the **leading** three segments.  For each segment the
range is its own ``[low, high]`` (supplied, must cover both endpoints — a segment
with interior extremes must carry them):

    zd = max(low_1, low_2, low_3)      zg = min(high_1, high_2, high_3)

The centre exists only when the intersection is **strictly positive width**
(``zd < zg``).  ``zd == zg`` is one touch point, not a centre; a negative
intersection is no centre.  Later triples are never examined: scanning for them
would be centre discovery, which is explicitly not implemented.

Two clocks, never conflated
---------------------------
* ``structure_start_day`` / ``structure_start_price`` — where the structure
  begins (the first segment's start).  A *raw* time point; it says nothing about
  when anybody could have known.
* ``confirmed_on`` — the day the third segment's completion was confirmed.  This
  is the only day on which the centre is knowable, and it is what
  :attr:`OperationalCentreSnapshot.visible_on` and every state timestamp use.

A centre formed on 03-02 and confirmed on 04-04 is not visible on 03-02.

Input contract (all re-verified, never repaired)
------------------------------------------------
``SegmentChain`` carries the identity of the input snapshot (``instrument`` and a
caller-computed ``snapshot_version`` digest) plus ``basis`` and ``as_of_day``.
:func:`centre_from_chain` raises ``ValueError`` unless every one of these holds:

* ``basis == 'segment_a0'``, non-empty instrument and digest, ISO days;
* each segment is ``status='confirmed'`` with a confirmation day, its ``index``
  equals its position, prices are finite, ``end_price != start_price`` (non-zero
  length), ``high > low`` and ``low <= min(prices) <= max(prices) <= high``;
* ``start_day < end_day`` and direction agrees with the price move;
* consecutive segments share the anchor **exactly** (``start_day == prev.end_day``
  and ``start_price == prev.end_price``) and alternate direction — an approximate
  anchor would make the centre irreproducible, so it fails closed;
* ``confirmed_on >= end_day`` (a confirmation may never precede the evidence) and
  ``confirmed_on <= as_of_day`` (no look-ahead), and confirmations are
  non-decreasing along the chain.

Lifecycle: only provable states
-------------------------------
Measured on the segments *after* the third, left to right, so a return resets the
question:

* ``candidate`` — the centre has formed and nothing after it has tested the zone
  yet (``centre_formed_not_tested``);
* ``extended`` — a later segment re-enters the **closed** interval
  (``returned_to_zone``), touches an edge only (``touched_edge``), or crosses to
  the far side (``crossed_through``).  A touch is a return, and crossing through
  is not "leaving and not coming back": neither is a break;
* ``unknown`` — a segment lies wholly outside the zone but there is no reverse
  segment yet, so nothing proves price will not come back
  (``departure_without_reversal``);
* ``broken`` — a departure is followed by a *reverse* segment that stays wholly
  outside on the same side (``left_without_return``); quoted on that reverse
  segment's confirmation day and terminal afterwards.

Every state carries ``visible_on`` (the evidence day, never the raw time) and the
segment indices it rests on.

Not implemented (do not claim otherwise)
----------------------------------------
No real segment generation (:func:`segments_from_pens`), no automatic centre
discovery (:func:`discover_centres`), no multi-centre or complex/serial anchors,
no level upgrade (:func:`higher_level_centre`), no trend (:func:`trend_from_centres`),
no 走势类型, no buy/sell points, no multi-level recursion and no trading signal.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from math import isfinite
from typing import Literal, Sequence

__all__ = [
    'BASIS',
    'CENTRE_KERNEL_VERSION',
    'INPUT_KIND',
    'SEGMENTS_ENGINE',
    'STRICTNESS',
    'CentreState',
    'ExternalSegment',
    'OperationalCentre',
    'OperationalCentreSnapshot',
    'SegmentChain',
    'centre_from_chain',
    'discover_centres',
    'higher_level_centre',
    'replay_prefixes',
    'segments_from_pens',
    'trend_from_centres',
]

#: Version of this rule set.  Any behaviour change needs a new version.
CENTRE_KERNEL_VERSION = 'chan-operational-centre-v1'

#: Where the segments come from: the caller, already completed and confirmed.
INPUT_KIND = 'externally_supplied_completed_segments'

#: The a0 the geometry is measured on.
BASIS = 'segment_a0'

#: Honest label: operational geometry, not the strict 18th-lesson definition.
STRICTNESS = 'operational_not_strict'

#: No 线段 kernel is wired in; segments are never generated here.
SEGMENTS_ENGINE = 'none'

_Directions = ('up', 'down')
_Direction = Literal['up', 'down']
_State = Literal['candidate', 'extended', 'broken', 'unknown']
_StateReason = Literal[
    'centre_formed_not_tested',
    'returned_to_zone',
    'touched_edge',
    'crossed_through',
    'departure_without_reversal',
    'left_without_return',
    'departure_far_side_unproven',
]


@dataclass(frozen=True)
class ExternalSegment:
    """One completed segment supplied by the caller.

    ``high``/``low`` are the segment's real price range and must cover both
    endpoints; a segment with interior extremes has to carry them, because the
    centre is built from ranges, not from endpoint prices.

    ``status`` must be ``'confirmed'``: an unconfirmed segment can still change,
    so it may not be part of structure.  ``confirmed_on`` is the day the segment
    became final — the day its last piece of evidence was sealed — and must not
    precede ``end_day``.
    """

    index: int
    direction: _Direction
    start_day: str
    start_price: float
    end_day: str
    end_price: float
    high: float
    low: float
    status: Literal['confirmed'] = 'confirmed'
    confirmed_on: str | None = None


@dataclass(frozen=True)
class SegmentChain:
    """A caller-vouched segment chain plus the identity of its input snapshot.

    ``instrument`` + ``snapshot_version`` identify *which* snapshot produced the
    chain (the digest is the caller's; this module never computes one).  A centre
    built from one snapshot must never be merged with, or compared against, a
    centre from another — :func:`centre_from_chain` refuses a mismatch instead.
    ``as_of_day`` is the last day the caller claims to know: no confirmation may
    lie after it.
    """

    instrument: str
    snapshot_version: str
    basis: str
    as_of_day: str
    segments: tuple[ExternalSegment, ...]


@dataclass(frozen=True)
class OperationalCentre:
    """A fixed operational centre: ``[zd, zg]`` plus its two clocks.

    ``zd``/``zg`` are frozen at formation — later segments never widen or narrow
    them (widening would be 延伸 of a different kind; this kernel only reports
    whether the zone is still in force).  ``structure_start_day`` is a raw time
    point; ``confirmed_on`` is when the centre became knowable.  They are
    different fields on purpose.
    """

    kernel_version: str
    input_kind: str
    basis: str
    strictness: str
    instrument: str
    snapshot_version: str
    segment_indices: tuple[int, int, int]
    zd: float
    zg: float
    structure_start_day: str
    structure_start_price: float
    confirmed_on: str

    @property
    def width(self) -> float:
        return self.zg - self.zd


@dataclass(frozen=True)
class CentreState:
    """A lifecycle state that is only emitted when it is provable.

    ``visible_on`` is the day the deciding evidence was confirmed — never the raw
    time of the zone, never the day the centre's structure started.
    ``evidence_segment_indices`` names the segments the state rests on, so a
    reader can audit the call instead of trusting a label.
    """

    state: _State
    reason: _StateReason
    visible_on: str
    evidence_segment_indices: tuple[int, ...]
    detail: str = ''


@dataclass(frozen=True)
class OperationalCentreSnapshot:
    """Result for one input prefix.  ``centre is None`` is a real answer."""

    as_of_day: str
    instrument: str
    snapshot_version: str
    kernel_version: str
    input_kind: str
    basis: str
    strictness: str
    segments_engine: str
    segment_count: int
    centre: OperationalCentre | None
    state: CentreState | None
    absence_reason: str | None

    @property
    def visible_on(self) -> str | None:
        """The day this snapshot's structure is knowable, if there is any."""
        if self.centre is None:
            return None
        return self.state.visible_on if self.state is not None else self.centre.confirmed_on


def _day(value: str | None, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f'{field} must be an ISO date string, got {value!r}')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f'{field} is not an ISO date ({value!r})') from None


def _validate_segment(segment: ExternalSegment, position: int, as_of: date) -> None:
    """Re-derive every fact the centre will rely on; never repair a mismatch."""
    if segment.index != position:
        raise ValueError(
            f'segment at position {position} carries index {segment.index}; the '
            'index must equal its position in the chain'
        )
    if segment.direction not in _Directions:
        raise ValueError(f'segment {position} has unknown direction {segment.direction!r}')
    if segment.status != 'confirmed':
        raise ValueError(
            f'segment {position} is {segment.status!r}, not confirmed; only '
            'explicitly confirmed segments may enter an operational centre'
        )
    for name in ('start_price', 'end_price', 'high', 'low'):
        value = getattr(segment, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f'segment {position} {name} is not a finite price ({value!r})')
    if segment.start_price == segment.end_price:
        raise ValueError(
            f'segment {position} is zero length ({segment.start_price}); a segment '
            'must move price'
        )
    if segment.high <= segment.low:
        raise ValueError(
            f'segment {position} has a degenerate range '
            f'[{segment.low}, {segment.high}]'
        )
    lowest = min(segment.start_price, segment.end_price)
    highest = max(segment.start_price, segment.end_price)
    if segment.low > lowest or segment.high < highest:
        raise ValueError(
            f'segment {position} range [{segment.low}, {segment.high}] does not '
            f'cover both endpoints ({lowest}, {highest}); the centre is built from '
            'ranges, so interior extremes must be supplied'
        )
    if segment.direction == 'up' and segment.end_price <= segment.start_price:
        raise ValueError(
            f'segment {position} is labelled up but runs {segment.start_price} -> '
            f'{segment.end_price}'
        )
    if segment.direction == 'down' and segment.end_price >= segment.start_price:
        raise ValueError(
            f'segment {position} is labelled down but runs {segment.start_price} -> '
            f'{segment.end_price}'
        )

    start = _day(segment.start_day, f'segment {position} start_day')
    end = _day(segment.end_day, f'segment {position} end_day')
    if end <= start:
        raise ValueError(
            f'segment {position} ends on {segment.end_day}, not after its start '
            f'{segment.start_day}; time order is required'
        )
    if segment.confirmed_on is None:
        raise ValueError(
            f'segment {position} has no confirmation day; a segment without one '
            'is not confirmed in any usable sense'
        )
    confirmed = _day(segment.confirmed_on, f'segment {position} confirmed_on')
    if confirmed < end:
        raise ValueError(
            f'segment {position} claims confirmation on {segment.confirmed_on}, '
            f'before its own end day {segment.end_day}; a confirmation may never '
            'precede the evidence'
        )
    if confirmed > as_of:
        raise ValueError(
            f'segment {position} is confirmed on {segment.confirmed_on}, after '
            'as_of_day; that is look-ahead'
        )


def _validate_chain(chain: SegmentChain) -> None:
    if not isinstance(chain.instrument, str) or not chain.instrument.strip():
        raise ValueError('segment chain needs a non-empty instrument')
    if not isinstance(chain.snapshot_version, str) or not chain.snapshot_version.strip():
        raise ValueError(
            'segment chain needs a non-empty snapshot_version; the input digest is '
            'what binds a centre to the snapshot it came from'
        )
    if chain.basis != BASIS:
        raise ValueError(
            f'unsupported basis {chain.basis!r}; this kernel only measures '
            f'{BASIS!r} (segment a0)'
        )
    as_of = _day(chain.as_of_day, 'as_of_day')
    if not chain.segments:
        raise ValueError('segment chain is empty; at least one completed segment is required')

    previous: ExternalSegment | None = None
    for position, segment in enumerate(chain.segments):
        _validate_segment(segment, position, as_of)
        if previous is not None:
            if segment.start_day != previous.end_day:
                raise ValueError(
                    f'segment {position} starts on {segment.start_day} but segment '
                    f'{position - 1} ended on {previous.end_day}; segments must be '
                    'connected in time'
                )
            if segment.start_price != previous.end_price:
                raise ValueError(
                    f'segment {position} starts at {segment.start_price} but segment '
                    f'{position - 1} ended at {previous.end_price}; neighbouring '
                    'segments must share an anchor price exactly'
                )
            if segment.direction == previous.direction:
                raise ValueError(
                    f'segment {position} repeats direction {segment.direction!r} of '
                    f'segment {position - 1}; segments must alternate'
                )
            if _day(segment.confirmed_on, 'confirmed_on') < _day(
                previous.confirmed_on, 'confirmed_on'
            ):
                raise ValueError(
                    f'segment {position} is confirmed before the previous segment; '
                    'evidence cannot be known out of order'
                )
        previous = segment


def _intersection(segments: Sequence[ExternalSegment]) -> tuple[float, float] | None:
    zd = max(segment.low for segment in segments)
    zg = min(segment.high for segment in segments)
    return None if zd >= zg else (zd, zg)


def _intersects(segment: ExternalSegment, centre: OperationalCentre) -> bool:
    """Closed-interval overlap: touching an edge is a return."""
    return segment.low <= centre.zg and segment.high >= centre.zd


def _intersection_reason(segment: ExternalSegment, centre: OperationalCentre) -> _StateReason:
    if (segment.start_price > centre.zg and segment.end_price < centre.zd) or (
        segment.start_price < centre.zd and segment.end_price > centre.zg
    ):
        return 'crossed_through'
    if segment.high == centre.zd or segment.low == centre.zg:
        return 'touched_edge'
    return 'returned_to_zone'


def _evaluate_state(
    segments: Sequence[ExternalSegment], centre: OperationalCentre
) -> CentreState:
    """Conservative left-to-right lifecycle; only provable states are emitted."""
    tail = list(segments)[3:]
    if not tail:
        return CentreState(
            state='candidate',
            reason='centre_formed_not_tested',
            visible_on=centre.confirmed_on,
            evidence_segment_indices=(),
            detail='no segment after the third has tested the zone',
        )

    departure: ExternalSegment | None = None
    departure_side: str | None = None
    last = CentreState(
        state='candidate',
        reason='centre_formed_not_tested',
        visible_on=centre.confirmed_on,
        evidence_segment_indices=(),
    )

    for segment in tail:
        if segment.confirmed_on is None:  # pragma: no cover - validated upstream
            raise ValueError('segment must be confirmed to be evaluated')
        if _intersects(segment, centre):
            # Re-entry resets the question: nothing is "left" any more.
            departure = None
            departure_side = None
            last = CentreState(
                state='extended',
                reason=_intersection_reason(segment, centre),
                visible_on=segment.confirmed_on,
                evidence_segment_indices=(segment.index,),
                detail=(
                    f'segment {segment.index} [{segment.low}, {segment.high}] touches '
                    f'the zone [{centre.zd}, {centre.zg}]'
                ),
            )
            continue

        side = 'above' if segment.low > centre.zg else 'below'
        if departure is None:
            # Outside, but nothing yet refuses to come back.
            departure, departure_side = segment, side
            last = CentreState(
                state='unknown',
                reason='departure_without_reversal',
                visible_on=segment.confirmed_on,
                evidence_segment_indices=(segment.index,),
                detail=(
                    f'segment {segment.index} lies wholly {side} the zone; no reverse '
                    'segment has shown that price stays away'
                ),
            )
            continue

        if side == departure_side:
            return CentreState(
                state='broken',
                reason='left_without_return',
                visible_on=segment.confirmed_on,
                evidence_segment_indices=(departure.index, segment.index),
                detail=(
                    f'segment {departure.index} left the zone {side} and segment '
                    f'{segment.index} never came back to it'
                ),
            )

        # A connected chain cannot jump the zone without touching it, so this is
        # only reachable from contradictory input; refuse to call it a break.
        departure, departure_side = segment, side
        last = CentreState(
            state='unknown',
            reason='departure_far_side_unproven',
            visible_on=segment.confirmed_on,
            evidence_segment_indices=(segment.index,),
            detail=f'segment {segment.index} is wholly {side} without traversing the zone',
        )

    return last


def centre_from_chain(
    chain: SegmentChain,
    *,
    expect_instrument: str | None = None,
    expect_snapshot_version: str | None = None,
) -> OperationalCentreSnapshot:
    """Build the operational centre for one segment chain.

    ``expect_instrument``/``expect_snapshot_version`` pin the identity the caller
    believes it is looking at; a mismatch raises instead of returning a centre
    computed from a different snapshot (adjustment basis, correction run,
    different instrument — nothing here can detect those after the fact).
    """
    _validate_chain(chain)

    if expect_instrument is not None and chain.instrument != expect_instrument:
        raise ValueError(
            f'instrument mismatch: chain is {chain.instrument!r}, expected '
            f'{expect_instrument!r}'
        )
    if expect_snapshot_version is not None and chain.snapshot_version != expect_snapshot_version:
        raise ValueError(
            f'snapshot digest mismatch: chain carries {chain.snapshot_version!r}, '
            f'expected {expect_snapshot_version!r}; a centre belongs to exactly one '
            'input snapshot'
        )

    def snapshot(
        centre: OperationalCentre | None,
        state: CentreState | None,
        absence: str | None,
    ) -> OperationalCentreSnapshot:
        return OperationalCentreSnapshot(
            as_of_day=chain.as_of_day,
            instrument=chain.instrument,
            snapshot_version=chain.snapshot_version,
            kernel_version=CENTRE_KERNEL_VERSION,
            input_kind=INPUT_KIND,
            basis=BASIS,
            strictness=STRICTNESS,
            segments_engine=SEGMENTS_ENGINE,
            segment_count=len(chain.segments),
            centre=centre,
            state=state,
            absence_reason=absence,
        )

    segments = chain.segments
    if len(segments) < 3:
        return snapshot(None, None, 'insufficient_segments')

    triple = segments[:3]
    zone = _intersection(triple)
    if zone is None:
        return snapshot(None, None, 'zero_width_intersection')

    first, third = triple[0], triple[2]
    third_confirmed = third.confirmed_on
    if third_confirmed is None:  # pragma: no cover - validated in _validate_chain
        raise ValueError('the third segment must be confirmed before a centre can exist')
    centre = OperationalCentre(
        kernel_version=CENTRE_KERNEL_VERSION,
        input_kind=INPUT_KIND,
        basis=BASIS,
        strictness=STRICTNESS,
        instrument=chain.instrument,
        snapshot_version=chain.snapshot_version,
        segment_indices=(first.index, triple[1].index, third.index),
        zd=zone[0],
        zg=zone[1],
        structure_start_day=first.start_day,
        structure_start_price=first.start_price,
        confirmed_on=third_confirmed,
    )
    return snapshot(centre, _evaluate_state(segments, centre), None)


def replay_prefixes(chain: SegmentChain) -> tuple[OperationalCentreSnapshot, ...]:
    """Replay every prefix, one snapshot per segment, each causally timestamped.

    Prefix ``k`` ends at segment ``k - 1`` and its ``as_of_day`` is that segment's
    confirmation day, so the prefix cannot know anything later.  A formed centre's
    numbers are identical in every prefix that contains it.
    """
    _validate_chain(chain)
    snapshots = []
    for length in range(1, len(chain.segments) + 1):
        prefix = chain.segments[:length]
        snapshots.append(
            centre_from_chain(
                replace(chain, segments=prefix, as_of_day=prefix[-1].confirmed_on)
            )
        )
    return tuple(snapshots)


def segments_from_pens(*args, **kwargs) -> None:
    """Not implemented: real 线段 generation from pens."""
    raise NotImplementedError(
        'real 线段 (segment) generation is not implemented: no fractal/pen -> '
        'segment recursion, no 特征序列 gap handling and no segment destruction in '
        'this kernel.  Segments must be supplied by the caller and labelled '
        f'{INPUT_KIND!r}'
    )


def discover_centres(*args, **kwargs) -> None:
    """Not implemented: scanning a chain for every qualifying triple."""
    raise NotImplementedError(
        'centre discovery is not implemented: this kernel reads exactly one centre '
        'from the leading three segments.  Multi-centre chains, serial/复杂 anchors '
        'and 中枢扩展 are out of scope'
    )


def higher_level_centre(*args, **kwargs) -> None:
    """Not implemented: promoting a centre to a higher level (中枢升级)."""
    raise NotImplementedError(
        'level upgrade is not implemented: there is no multi-level recursion here, '
        'so an operational centre can never be relabelled as a higher-level one'
    )


def trend_from_centres(*args, **kwargs) -> None:
    """Not implemented: deriving a trend (趋势) from centres."""
    raise NotImplementedError(
        'trend is not implemented: a trend needs at least two same-direction '
        'centres and a level contract, neither of which exists here'
    )
