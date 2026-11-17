"""Chan-theory v1: 线段 (segments) over an explicitly supplied 笔 (pen) sequence.

Scope and input contract
------------------------
Pure computation on top of :mod:`src.core.chan_pen`.  The caller supplies one
**already causal** pen snapshot; this kernel never inspects bars, sessions or
anything after ``as_of_day``, so every result is a deterministic replay of that
pen prefix.  No graph is fetched here and no snapshot is re-derived: pens are
the input, segments are the output.

Segment rules v1 (``SEGMENT_KERNEL_VERSION = 'chan-segment-v1'``)
--------------------------------------------------------------
A segment is a contiguous run of pens of one direction (pens alternate by
construction, so direction alternates too).  Following 第65/67课 as pinned by
:class:`SegmentRules`:

1. **前三笔必须重叠** — the first :data:`MIN_PENS` (=3) pens of a segment must
   share a non-empty price range.  Ranges are the pen's *endpoint* range
   (``[min(start,end), max(start,end)]``) and the test is strict: a zero-width
   touch (``max(low) == min(high)``) is **not** an overlap.  A trio that does not
   overlap is reported as a ``blocked`` :class:`SegmentPending` — no segment is
   forced, and nothing after it is invented.
2. **特征序列** — the segment's characteristic sequence is made of the *reverse*
   pens inside it (down pens for an up segment, up pens for a down one).  The
   segment ends when that sequence prints a terminal fractal of the segment's
   direction (顶分型 for up, 底分型 for down).
3. **包含处理** — the characteristic sequence is normalised with the same
   inclusive containment rule as merged bars, **sequentially and
   non-transitively** (each merge is compared with the already-normalised
   neighbour, so a transitive closure would give a different range).  The merge
   direction is **pinned to the segment direction** (``up`` keeps ``max`` high /
   ``max`` low, ``down`` keeps ``min``/``min``): that is the v1 choice, it is the
   only direction that can print the terminal fractal, and the alternative
   ("infer the direction from the last two elements") is *not* implemented.
   If the **first two** elements are contained, the first element's range would
   depend on a trend that does not exist yet: that is ``blocked``
   (``characteristic_initial_containment``) instead of invented.

Confirmation time (the important part)
--------------------------------------
Only **confirmed** pens are evidence.  A candidate pen may never confirm a
segment, and the segment's earliest publish day is the latest confirmation day
among its three fractal elements — i.e. the day the *third* characteristic
element was frozen, never the extremum's own day and never an earlier day a
caller might prefer.  Consequently ``confirmed_on`` is conservative (late) and
a prefix can never learn something a later prefix contradicts:

* the tail segment may still be ``candidate`` (its end may drift, and drift is
  recorded as ``revisions`` / ``drift_days`` on the endpoint);
* every earlier segment is ``confirmed`` and immutable.

Endpoint drift is **never back-filled**: a later, higher high (or lower low)
does not rewrite a confirmed segment — it belongs to the next segment or to the
open one.  A confirmed segment is a pure function of the prefix that produced
it, which is what makes bar-by-bar replay auditable.

Fail-closed behaviour
---------------------
``segment_from_pens`` accepts snapshots produced elsewhere, so nothing is
trusted.  It re-verifies pen order, endpoint continuity (shared merged index,
day and price), alternating directions, direction-vs-price, kernel version,
``status``/``confirmed_on`` consistency, ``confirmed_on`` strictly after its own
position day and never after ``as_of_day``, and that only the tail pen may be a
candidate.  Any mismatch raises ``ValueError`` instead of being repaired.

Reported states, never guesses
------------------------------
``candidate``   the end may still move (tail only).
``confirmed``   ended by 第67课 **第一种情况** (terminal fractal with no gap):
                ``reason='first_case_no_gap'``.
``blocked``     evidence exists but cannot be turned into a fixed line, with an
                explicit reason:

* ``characteristic_gap_case2`` — the fractal has a gap between its first two
                elements.  第二种情况 requires the *second* characteristic
                sequence to print a fractal as well; **not implemented**, so the
                segment is blocked, not "ended".
* ``characteristic_initial_containment`` — merge direction would be invented.
* ``characteristic_fractal_below_prior_extreme`` — the fractal's middle is not
                the sequence's extreme (an earlier element was more extreme), so
                using it as the end would silently move the segment's high/low.
* ``tainted_pen`` — a pen in the span is ``blocked`` (unknown-direction taint
                upstream).  The chain stops at a blocked boundary.

``SegmentRejection`` entries keep every conservative decision readable: nothing
is dropped silently.

Source binding (do not mix samples)
-----------------------------------
A result is bound to exactly one input: ``SegmentBinding`` carries the symbol,
the price-adjustment base and the as-of day, and the snapshot stores a
SHA-256 ``input_digest`` of the pen series plus ``source_pen_kernel_version``.
Two results with different digests, symbols, adjustment bases or kernels
describe different lines even when the numbers look alike; use
:meth:`SegmentSnapshot.same_source` / :meth:`SegmentSnapshot.assert_same_source`
and keep the digest next to anything you store or display.  :meth:`is_stale`
answers "was this computed before day X" — a stale result must not be presented
as today's structure.

Not implemented (do not claim otherwise)
----------------------------------------
* 第67课 **第二种情况** (gap → wait for the second characteristic sequence).
* 线段破坏后的重划 / 复杂递归划分: a blocked boundary stops the chain, it is never
  patched with a guessed continuation.
* 中枢 (centres) — three overlapping pens are **not** a 中枢, and this module
  does not build one.  No 买卖点, no multi-level recursion, no volume/effort
  rules, no trading signals.
* No re-anchoring: if the first three pens do not overlap, the chain is blocked
  rather than restarted from a later pen (that rule is not part of v1).
* This is *not* a complete 线段 implementation: it is the verifiable no-gap
  case plus explicit blocked states for everything else.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import isfinite
from typing import Literal, Sequence

from src.core.chan_pen import PEN_KERNEL_VERSION, PenSnapshot

__all__ = [
    'MIN_PENS',
    'SEGMENT_KERNEL_VERSION',
    'CharacteristicElement',
    'Segment',
    'SegmentBinding',
    'SegmentEndpoint',
    'SegmentPending',
    'SegmentRejection',
    'SegmentRules',
    'SegmentSnapshot',
    'normalize_characteristic_sequence',
    'segment_from_pens',
    'segment_input_digest',
]

#: Version of the segment rule set.  Any behaviour change needs a new version.
SEGMENT_KERNEL_VERSION = 'chan-segment-v1'

#: A segment needs at least three pens, and their ranges must overlap.
MIN_PENS = 3

_ADJUSTMENT_BASES = ('none', 'qfq', 'hfq')

_OVERLAP = 'pen_endpoint_range_strict'
_CHARACTERISTIC = 'reverse_pens'
_INCLUSION = 'sequential_non_transitive'
_MERGE_DIRECTION = 'segment_direction'
_GAP_CASE = 'blocked_case2_not_implemented'
_INITIAL_CONTAINMENT = 'blocked'
_EQUAL_EXTREME = 'keep_earlier'
_EVIDENCE = 'confirmed_pens_only'

_Direction = Literal['up', 'down']
_Status = Literal['candidate', 'confirmed', 'blocked']

_PendingReason = Literal[
    'awaiting_three_pens',
    'awaiting_three_confirmed_pens',
    'first_three_pens_no_overlap',
    'tainted_pen',
]
_SegmentReason = Literal[
    'first_case_no_gap',
    'awaiting_confirmed_characteristic_pen',
    'characteristic_gap_case2',
    'characteristic_initial_containment',
    'characteristic_fractal_below_prior_extreme',
    'tainted_pen',
]
_RejectReason = Literal[
    'first_three_pens_no_overlap',
    'characteristic_gap_case2',
    'characteristic_initial_containment',
    'characteristic_fractal_below_prior_extreme',
    'superseded_by_more_extreme',
    'equal_extreme_kept_earlier',
    'tainted_pen',
]


@dataclass(frozen=True)
class SegmentRules:
    """Versioned rule set.  Every field is *pinned* to what v1 means.

    Changing any value while still reporting ``chan-segment-v1`` would make one
    version string describe two different rule sets, so unsupported values
    raise.  A new rule needs a new kernel version *and* a replacement module.
    """

    kernel_version: str = SEGMENT_KERNEL_VERSION
    min_pens: int = MIN_PENS
    overlap: str = _OVERLAP
    characteristic_sequence: str = _CHARACTERISTIC
    inclusion: str = _INCLUSION
    merge_direction: str = _MERGE_DIRECTION
    gap_case: str = _GAP_CASE
    initial_containment: str = _INITIAL_CONTAINMENT
    equal_extreme: str = _EQUAL_EXTREME
    evidence: str = _EVIDENCE

    def __post_init__(self) -> None:
        expected = {
            'kernel_version': SEGMENT_KERNEL_VERSION,
            'min_pens': MIN_PENS,
            'overlap': _OVERLAP,
            'characteristic_sequence': _CHARACTERISTIC,
            'inclusion': _INCLUSION,
            'merge_direction': _MERGE_DIRECTION,
            'gap_case': _GAP_CASE,
            'initial_containment': _INITIAL_CONTAINMENT,
            'equal_extreme': _EQUAL_EXTREME,
            'evidence': _EVIDENCE,
        }
        for field, value in expected.items():
            actual = getattr(self, field)
            if actual != value:
                raise ValueError(
                    f'{field} {actual!r} is not the {SEGMENT_KERNEL_VERSION} rule '
                    f'({value!r}); the rule set is pinned per kernel version, so a '
                    'different value needs a new kernel version'
                )


@dataclass(frozen=True)
class SegmentBinding:
    """The one input a result belongs to: symbol, adjustment base, as-of day.

    Consumers must store this next to the result (together with
    ``SegmentSnapshot.input_digest``).  Nothing here can be inferred from the
    pen series, so a mismatch is the caller's to prevent — the kernel only
    refuses inconsistencies it can see (empty symbol, unknown base, an
    ``as_of_day`` that is not the snapshot's own).
    """

    symbol: str
    adjust: str
    as_of_day: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError('symbol must be a non-empty string')
        if self.adjust not in _ADJUSTMENT_BASES:
            raise ValueError(f'unsupported adjustment base {self.adjust!r}; v1 knows {_ADJUSTMENT_BASES}')
        if self.as_of_day is not None and not isinstance(self.as_of_day, str):
            raise ValueError('as_of_day must be an ISO day string or None')


@dataclass(frozen=True)
class CharacteristicElement:
    """One (possibly merged) element of the characteristic sequence.

    ``pen_indices`` are the reverse pens it absorbed, in order; ``first``/``last``
    locate its span so the segment's extreme can be read back onto a same
    direction pen.  ``confirmed_on`` is the latest confirmation day among the
    absorbed pens — never earlier than the evidence.
    """

    high: float
    low: float
    first_pen_index: int
    last_pen_index: int
    pen_indices: tuple[int, ...]
    confirmed_on: str | None


@dataclass(frozen=True)
class SegmentEndpoint:
    """One end of a segment: position, and (when frozen) its confirmation day.

    ``revisions``/``drift_days`` record an open end that moved before it froze,
    so a drifted endpoint never looks original.
    """

    kind: Literal['top', 'bottom']
    pen_index: int
    merged_index: int
    position_day: str
    price: float
    status: Literal['candidate', 'confirmed']
    confirmed_on: str | None
    revisions: int
    drift_days: tuple[str, ...]


@dataclass(frozen=True)
class Segment:
    """A 线段 built from pens ``pen_start_index..pen_end_index`` (inclusive).

    ``status='confirmed'`` is only ever the 第67课 第一种情况 (no gap).
    ``blocked`` keeps the audit-level end price but no ``confirmed_on``.
    ``confirmed_on`` is the earliest day both end prices are final — the day the
    third characteristic element was frozen.
    """

    start: SegmentEndpoint
    end: SegmentEndpoint
    direction: _Direction
    pen_start_index: int
    pen_end_index: int
    status: _Status
    reason: _SegmentReason
    confirmed_on: str | None
    characteristic_elements: int
    kernel_version: str

    @property
    def pens_used(self) -> int:
        return self.pen_end_index - self.pen_start_index + 1

    @property
    def start_price(self) -> float:
        return self.start.price

    @property
    def end_price(self) -> float:
        return self.end.price


@dataclass(frozen=True)
class SegmentPending:
    """A chain start that cannot become a segment yet (or at all).

    Reported instead of a guessed segment: not enough pens, not enough
    *confirmed* pens, a non-overlapping trio, or a tainted pen.  The position
    day and price of the would-be start are kept so the decision is auditable.
    """

    pen_start_index: int
    direction: _Direction
    status: Literal['candidate', 'blocked']
    reason: _PendingReason
    start_position_day: str
    start_price: float
    confirmed_pens: int
    detail: str = ''


@dataclass(frozen=True)
class SegmentRejection:
    """A conservative decision the kernel took, kept readable.

    ``pen_index`` is the pen whose end is the rejected *position* (the
    superseded extreme, the pen that caused a merge, the tainted pen...).
    """

    pen_index: int
    position_day: str
    price: float
    reason: _RejectReason
    detail: str = ''


@dataclass(frozen=True)
class SegmentSnapshot:
    """Replayable segment result for exactly one bound pen snapshot."""

    as_of_day: str | None
    kernel_version: str
    source_pen_kernel_version: str
    rules: SegmentRules
    binding: SegmentBinding
    input_digest: str
    segments: tuple[Segment, ...]
    pending: tuple[SegmentPending, ...]
    rejections: tuple[SegmentRejection, ...]

    @property
    def confirmed_segments(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.segments if s.status == 'confirmed')

    @property
    def blocked_segments(self) -> tuple[Segment, ...]:
        """Audit only: evidence exists, but no fixed line may be published."""
        return tuple(s for s in self.segments if s.status == 'blocked')

    @property
    def candidate_segments(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.segments if s.status == 'candidate')

    @property
    def open_segment(self) -> Segment | None:
        """The single segment whose end may still drift, if any."""
        if self.segments and self.segments[-1].status == 'candidate':
            return self.segments[-1]
        return None

    def same_source(self, other: 'SegmentSnapshot') -> bool:
        """True only when both results describe the same input sample."""
        return (
            self.binding == other.binding
            and self.input_digest == other.input_digest
            and self.kernel_version == other.kernel_version
            and self.source_pen_kernel_version == other.source_pen_kernel_version
        )

    def assert_same_source(self, other: 'SegmentSnapshot') -> None:
        if not self.same_source(other):
            raise ValueError(
                'segment results come from different sources '
                f'(symbol={self.binding.symbol!r}/{other.binding.symbol!r}, '
                f'adjust={self.binding.adjust!r}/{other.binding.adjust!r}, '
                f'as_of={self.binding.as_of_day!r}/{other.binding.as_of_day!r}, '
                f'digest={self.input_digest[:12]}.../{other.input_digest[:12]}...); '
                'never mix them'
            )

    def is_stale(self, day: str) -> bool:
        """True when this result was computed before ``day`` (or has no as-of)."""
        return self.as_of_day is None or day > self.as_of_day


def segment_input_digest(pen_snapshot: PenSnapshot) -> str:
    """SHA-256 over the pen series that produced the segments.

    Anything that changes a pen (position, price, status, confirmation day,
    direction, kernel version, as-of day) changes the digest, so two runs can be
    compared without trusting a label.
    """
    payload = {
        'as_of_day': pen_snapshot.as_of_day,
        'pen_kernel_version': pen_snapshot.kernel_version,
        'pens': [
            [
                pen.start.merged_index,
                pen.start.position_day,
                pen.start.price,
                pen.end.merged_index,
                pen.end.position_day,
                pen.end.price,
                pen.direction,
                pen.status,
                pen.confirmed_on,
            ]
            for pen in pen_snapshot.pens
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _contains(left: CharacteristicElement, right: CharacteristicElement) -> bool:
    """Inclusive containment, as for merged bars: equal extrema merge."""
    return (left.high >= right.high and left.low <= right.low) or (
        right.high >= left.high and right.low <= left.low
    )


def _merge_elements(
    left: CharacteristicElement, right: CharacteristicElement, direction: _Direction
) -> CharacteristicElement:
    """Merge two contained elements in the pinned (segment) direction.

    ``up`` keeps ``max`` high / ``max`` low, ``down`` keeps ``min``/``min``; the
    absorbed pen indices and the latest confirmation day are kept so the merge
    is never anonymous.
    """
    if direction == 'up':
        high, low = max(left.high, right.high), max(left.low, right.low)
    else:
        high, low = min(left.high, right.high), min(left.low, right.low)
    days = [d for d in (left.confirmed_on, right.confirmed_on) if d]
    return CharacteristicElement(
        high=high,
        low=low,
        first_pen_index=left.first_pen_index,
        last_pen_index=right.last_pen_index,
        pen_indices=left.pen_indices + right.pen_indices,
        confirmed_on=max(days) if days else None,
    )


def _merge_pass(
    elements: Sequence[CharacteristicElement], direction: _Direction
) -> tuple[tuple[CharacteristicElement, ...], str | None]:
    """Sequential, non-transitive 包含处理.  Returns (elements, blocked_reason)."""
    out: list[CharacteristicElement] = []
    merges = 0
    for element in elements:
        out.append(element)
        while len(out) >= 2 and _contains(out[-2], out[-1]):
            if merges == 0 and len(out) == 2:
                # The very first pair is contained: the first element's range
                # would depend on a trend that does not exist yet.  Fail closed.
                return tuple(out), 'initial_containment_direction_ambiguous'
            out[-2:] = (_merge_elements(out[-2], out[-1], direction),)
            merges += 1
    return tuple(out), None


def normalize_characteristic_sequence(
    ranges: Sequence[tuple[float, float]], *, direction: _Direction
) -> tuple[tuple[tuple[float, float], ...], str | None]:
    """Normalise characteristic-sequence ``(high, low)`` ranges for one direction.

    Exposed for audit: it is the exact 包含处理 used by the segment walk.  The
    merge is sequential and non-transitive (each merge compares with the
    already-normalised neighbour), so a transitive closure of the same ranges
    would give a different result.  Returns the normalised ranges plus a blocked
    reason when the first contained pair leaves the direction ambiguous.
    """
    if direction not in ('up', 'down'):
        raise ValueError(f'unsupported segment direction {direction!r}')
    elements = []
    for index, (high, low) in enumerate(ranges):
        if not (isfinite(high) and isfinite(low)) or high < low:
            raise ValueError(f'invalid characteristic range {(high, low)!r} at {index}')
        elements.append(
            CharacteristicElement(
                high=float(high),
                low=float(low),
                first_pen_index=index,
                last_pen_index=index,
                pen_indices=(index,),
                confirmed_on=None,
            )
        )
    normalised, blocked = _merge_pass(elements, direction)
    return tuple((e.high, e.low) for e in normalised), blocked


def _more_extreme(price: float, reference: float, direction: _Direction) -> bool:
    return price > reference if direction == 'up' else price < reference


def _terminal_fractal(
    left: CharacteristicElement,
    middle: CharacteristicElement,
    right: CharacteristicElement,
    direction: _Direction,
) -> bool:
    """Strict three-element terminal fractal of the segment's direction."""
    if direction == 'up':
        return (
            middle.high > left.high
            and middle.low > left.low
            and middle.high > right.high
            and middle.low > right.low
        )
    return (
        middle.low < left.low
        and middle.high < left.high
        and middle.low < right.low
        and middle.high < right.high
    )


def _has_gap(left: CharacteristicElement, middle: CharacteristicElement, direction: _Direction) -> bool:
    """第67课 缺口: the first two elements of the fractal do not overlap at all."""
    return middle.low > left.high if direction == 'up' else middle.high < left.low


def _validate_pens(snapshot: PenSnapshot) -> None:
    """Refuse an inconsistent pen series instead of repairing it."""
    if snapshot.kernel_version != PEN_KERNEL_VERSION:
        raise ValueError(
            f'pen snapshot kernel {snapshot.kernel_version!r} is not '
            f'{PEN_KERNEL_VERSION!r}; segments v1 only read that pen rule set'
        )
    as_of = snapshot.as_of_day
    previous = None
    last = len(snapshot.pens) - 1
    for index, pen in enumerate(snapshot.pens):
        if pen.kernel_version != PEN_KERNEL_VERSION:
            raise ValueError(
                f'pen {index} was built by {pen.kernel_version!r}, not {PEN_KERNEL_VERSION!r}; '
                'pens from different kernels may not be chained'
            )
        if pen.status not in ('confirmed', 'candidate', 'blocked'):
            raise ValueError(f'pen {index} has unknown status {pen.status!r}')
        if pen.start.merged_index >= pen.end.merged_index:
            raise ValueError(
                f'pen {index} ends at merged index {pen.end.merged_index}, not after '
                f'its start {pen.start.merged_index}'
            )
        if pen.start.kind == pen.end.kind:
            raise ValueError(f'pen {index} joins two {pen.start.kind}s')
        expected = 'up' if pen.start.kind == 'bottom' else 'down'
        if pen.direction != expected:
            raise ValueError(
                f'pen {index} is labelled {pen.direction!r} but joins '
                f'{pen.start.kind}->{pen.end.kind}'
            )
        if not (isfinite(pen.start.price) and isfinite(pen.end.price)):
            raise ValueError(f'pen {index} has a non-finite price')
        if pen.direction == 'up' and pen.end.price <= pen.start.price:
            raise ValueError(
                f'up pen {index} ends at {pen.end.price}, not above its start {pen.start.price}'
            )
        if pen.direction == 'down' and pen.end.price >= pen.start.price:
            raise ValueError(
                f'down pen {index} ends at {pen.end.price}, not below its start {pen.start.price}'
            )
        if pen.status == 'confirmed':
            if pen.confirmed_on is None:
                raise ValueError(f'pen {index} is confirmed without a confirmation day')
            if pen.end.confirmed_on is None:
                raise ValueError(
                    f'confirmed pen {index} has no confirmed end endpoint; a frozen pen '
                    'always carries the day its end froze'
                )
            if as_of is not None and pen.confirmed_on > as_of:
                raise ValueError(
                    f'pen {index} is confirmed on {pen.confirmed_on}, after as_of_day {as_of}'
                )
            if pen.confirmed_on <= pen.end.position_day:
                raise ValueError(
                    f'pen {index} is confirmed on {pen.confirmed_on}, not after its own '
                    f'end position day {pen.end.position_day}; a confirmation may never '
                    'precede the evidence'
                )
        else:
            if pen.confirmed_on is not None:
                raise ValueError(f'pen {index} is {pen.status} but carries a confirmation day')
            if pen.status == 'candidate' and index != last:
                raise ValueError(
                    f'pen {index} is a candidate but is not the tail; an open pen can only '
                    'be the last one of a prefix'
                )
        for endpoint in (pen.start, pen.end):
            if endpoint.confirmed_on is not None and endpoint.confirmed_on > (as_of or endpoint.confirmed_on):
                raise ValueError(
                    f'pen {index} endpoint is confirmed on {endpoint.confirmed_on}, after '
                    f'as_of_day {as_of}'
                )
        if previous is not None:
            if pen.start.merged_index != previous.end.merged_index:
                raise ValueError(
                    f'pen {index} starts at merged index {pen.start.merged_index} but pen '
                    f'{index - 1} ended at {previous.end.merged_index}; pens must share endpoints'
                )
            if pen.start.position_day != previous.end.position_day or pen.start.price != previous.end.price:
                raise ValueError(
                    f'pen {index} start ({pen.start.position_day}@{pen.start.price}) does not '
                    f'meet pen {index - 1} end ({previous.end.position_day}@{previous.end.price})'
                )
            if pen.direction == previous.direction:
                raise ValueError(f'pens {index - 1} and {index} share a direction; segments need alternation')
        previous = pen


def _range(pen) -> tuple[float, float]:
    """The pen's own price interval (endpoints, not the bar ranges)."""
    low, high = min(pen.start.price, pen.end.price), max(pen.start.price, pen.end.price)
    return low, high


def _segment_endpoint(pen, pen_index: int, endpoint, *, status, confirmed_on, revisions=0, drift_days=()):
    return SegmentEndpoint(
        kind=endpoint.kind,
        pen_index=pen_index,
        merged_index=endpoint.merged_index,
        position_day=endpoint.position_day,
        price=endpoint.price,
        status=status,
        confirmed_on=confirmed_on,
        revisions=revisions,
        drift_days=drift_days,
    )


def _rejection(
    pen, pen_index: int, reason: '_RejectReason', detail: str = ''
) -> SegmentRejection:
    return SegmentRejection(
        pen_index=pen_index,
        position_day=pen.end.position_day,
        price=pen.end.price,
        reason=reason,
        detail=detail,
    )


def _build_segment(
    pens, start: int, rules: SegmentRules
) -> tuple[Segment | SegmentPending, tuple[SegmentRejection, ...]]:
    """Build (or refuse) the single segment that starts at pen ``start``."""
    first = pens[start]
    direction: _Direction = first.direction  # type: ignore[assignment]
    trio = pens[start : start + rules.min_pens]
    start_ep = _segment_endpoint(
        first,
        start,
        first.start,
        status='confirmed' if first.start.confirmed_on else 'candidate',
        confirmed_on=first.start.confirmed_on,
    )

    def pending(status, reason, detail='') -> SegmentPending:
        return SegmentPending(
            pen_start_index=start,
            direction=direction,
            status=status,
            reason=reason,
            start_position_day=first.start.position_day,
            start_price=first.start.price,
            confirmed_pens=sum(1 for p in trio if p.status == 'confirmed'),
            detail=detail,
        )

    if len(trio) < rules.min_pens:
        return pending('candidate', 'awaiting_three_pens'), ()

    for pen_index, pen in enumerate(trio, start=start):
        if pen.status == 'blocked':
            return (
                pending('blocked', 'tainted_pen', f'pen {pen_index} is blocked'),
                (_rejection(pen, pen_index, 'tainted_pen'),),
            )
    for pen in trio:
        if pen.status != 'confirmed':
            return pending('candidate', 'awaiting_three_confirmed_pens'), ()

    lows = [_range(p)[0] for p in trio]
    highs = [_range(p)[1] for p in trio]
    if max(lows) >= min(highs):
        detail = f'max low {max(lows)} >= min high {min(highs)} over pens {start}..{start + 2}'
        return (
            pending('blocked', 'first_three_pens_no_overlap', detail),
            (_rejection(trio[-1], start + 2, 'first_three_pens_no_overlap', detail),),
        )

    best_pen = start  # pen whose end is the current extreme of this segment
    revisions = 0
    drift_days: tuple[str, ...] = ()
    elements: list[CharacteristicElement] = []
    merges = 0
    rejections: list[SegmentRejection] = []

    def make_segment(status, reason, confirmed_on=None, end_status='candidate'):
        end_pen = pens[best_pen]
        end_ep = _segment_endpoint(
            end_pen,
            best_pen,
            end_pen.end,
            status=end_status,
            confirmed_on=end_pen.end.confirmed_on if end_status == 'confirmed' else None,
            revisions=revisions,
            drift_days=drift_days,
        )
        return Segment(
            start=start_ep,
            end=end_ep,
            direction=direction,
            pen_start_index=start,
            pen_end_index=best_pen,
            status=status,
            reason=reason,
            confirmed_on=confirmed_on,
            characteristic_elements=len(elements),
            kernel_version=rules.kernel_version,
        )

    for pen_index in range(start + 1, len(pens), 2):
        pen = pens[pen_index]
        previous_same = pens[pen_index - 1]

        if pen.status == 'blocked' or previous_same.status == 'blocked':
            bad_index = pen_index if pen.status == 'blocked' else pen_index - 1
            rejections.append(_rejection(pens[bad_index], bad_index, 'tainted_pen'))
            return make_segment('blocked', 'tainted_pen'), tuple(rejections)

        # Endpoint drift: only the open extreme may move, and only forward.  The
        # same-direction pen before this element is frozen (a confirmed pen's
        # start endpoint is confirmed), so its extreme is legitimate evidence
        # even while the element itself is still open.
        if pen_index - 1 != start and previous_same.status == 'confirmed':
            price = previous_same.end.price
            best_price = pens[best_pen].end.price
            if _more_extreme(price, best_price, direction):
                rejections.append(
                    _rejection(
                        pens[best_pen],
                        best_pen,
                        'superseded_by_more_extreme',
                        f'open segment end moved to {previous_same.end.position_day}@{price}',
                    )
                )
                drift_days = drift_days + (pens[best_pen].end.position_day,)
                best_pen = pen_index - 1
                revisions += 1
            elif price == best_price:
                rejections.append(
                    _rejection(
                        previous_same,
                        pen_index - 1,
                        'equal_extreme_kept_earlier',
                        f'open segment end stays at pen {best_pen}',
                    )
                )

        if pen.status != 'confirmed':
            return make_segment('candidate', 'awaiting_confirmed_characteristic_pen'), tuple(rejections)

        elements.append(
            CharacteristicElement(
                high=max(pen.start.price, pen.end.price),
                low=min(pen.start.price, pen.end.price),
                first_pen_index=pen_index,
                last_pen_index=pen_index,
                pen_indices=(pen_index,),
                confirmed_on=pen.confirmed_on,
            )
        )

        merged_now = False
        while len(elements) >= 2 and _contains(elements[-2], elements[-1]):
            if merges == 0 and len(elements) == 2:
                rejections.append(
                    _rejection(
                        pen,
                        pen_index,
                        'characteristic_initial_containment',
                        'the first two characteristic elements are contained; the merge '
                        'direction cannot be established',
                    )
                )
                return (
                    make_segment('blocked', 'characteristic_initial_containment'),
                    tuple(rejections),
                )
            elements[-2:] = [_merge_elements(elements[-2], elements[-1], direction)]
            merges += 1
            merged_now = True

        if merged_now or len(elements) < 3:
            continue

        left, middle, right = elements[-3], elements[-2], elements[-1]
        if not _terminal_fractal(left, middle, right, direction):
            continue

        middle_price = middle.high if direction == 'up' else middle.low
        best_price = pens[best_pen].end.price
        if _more_extreme(best_price, middle_price, direction):
            rejections.append(
                _rejection(
                    pens[best_pen],
                    best_pen,
                    'characteristic_fractal_below_prior_extreme',
                    f'the terminal fractal is at {middle_price} but the extreme so far is '
                    f'{best_price}; using it would move the segment end',
                )
            )
            return (
                make_segment('blocked', 'characteristic_fractal_below_prior_extreme'),
                tuple(rejections),
            )
        if _has_gap(left, middle, direction):
            rejections.append(
                _rejection(
                    pens[best_pen],
                    best_pen,
                    'characteristic_gap_case2',
                    '第67课 第二种情况 (gap) needs the second characteristic sequence; '
                    'not implemented',
                )
            )
            return make_segment('blocked', 'characteristic_gap_case2'), tuple(rejections)

        confirmed_days = [e.confirmed_on for e in (left, middle, right)]
        if any(day is None for day in confirmed_days):  # pragma: no cover - elements are confirmed
            raise ValueError('internal error: a characteristic element lost its confirmation day')
        confirmed_on = max(confirmed_days)  # type: ignore[arg-type]
        if confirmed_on <= pens[best_pen].end.position_day:  # pragma: no cover - invariant guard
            raise ValueError('segment confirmation precedes its own end position')
        if best_pen - start + 1 < rules.min_pens:  # pragma: no cover - invariant guard
            raise ValueError('internal error: a segment shorter than three pens was confirmed')
        return make_segment('confirmed', 'first_case_no_gap', confirmed_on, end_status='confirmed'), tuple(
            rejections
        )

    return make_segment('candidate', 'awaiting_confirmed_characteristic_pen'), tuple(rejections)


def segment_from_pens(
    pen_snapshot: PenSnapshot,
    *,
    binding: SegmentBinding,
    rules: SegmentRules | None = None,
) -> SegmentSnapshot:
    """Derive segments from one explicitly bound, already causal pen snapshot."""
    rules = SegmentRules() if rules is None else rules
    if not isinstance(binding, SegmentBinding):
        raise ValueError('binding must be a SegmentBinding (symbol/adjust/as_of_day)')
    if binding.as_of_day != pen_snapshot.as_of_day:
        raise ValueError(
            f'binding as_of_day {binding.as_of_day!r} does not match the pen snapshot '
            f'({pen_snapshot.as_of_day!r}); bind the result to its own sample'
        )
    _validate_pens(pen_snapshot)

    segments: list[Segment] = []
    pending: list[SegmentPending] = []
    rejections: list[SegmentRejection] = []

    start = 0
    total = len(pen_snapshot.pens)
    while start < total:
        outcome, extra = _build_segment(pen_snapshot.pens, start, rules)
        rejections.extend(extra)
        if isinstance(outcome, SegmentPending):
            pending.append(outcome)
            break
        segments.append(outcome)
        if outcome.status != 'confirmed':
            break
        start = outcome.pen_end_index + 1

    return SegmentSnapshot(
        as_of_day=pen_snapshot.as_of_day,
        kernel_version=rules.kernel_version,
        source_pen_kernel_version=pen_snapshot.kernel_version,
        rules=rules,
        binding=binding,
        input_digest=segment_input_digest(pen_snapshot),
        segments=tuple(segments),
        pending=tuple(pending),
        rejections=tuple(rejections),
    )
