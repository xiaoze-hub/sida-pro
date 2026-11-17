"""Read-only adapter: confirmed 线段 prefix (chan-segment v2) -> operational centre.

What this is
------------
A *pure, read-only* bridge between two already-versioned kernels:

* :mod:`src.core.chan_segment` — ``chan-segment-v2``, 第67课 **第一种情况** only,
  with an explicit consumer contract: :attr:`SegmentSnapshot.confirmed_segments`
  is the only view that may be used as geometry;
* :mod:`src.core.chan_operational_centre` — ``chan-operational-centre-v2``, which
  measures one centre on the **leading three** externally supplied segments and
  labels itself ``basis='segment_a0'`` / ``strictness='operational_not_strict'``.

This module does not compute bars, fractals, pens, segments or centres.  It reads
one segment snapshot, takes its **contiguous leading run of confirmed segments**,
turns exactly those lines into :class:`ExternalSegment` objects bound to the same
source, and calls :func:`centre_from_chain` only when that chain is long enough
and trustworthy.  Every limitation of both kernels is inherited and re-stated in
the result (``limitations``), never upgraded.

Why the pen snapshot is required
--------------------------------
A centre is built from **ranges**, not from endpoint prices
(:class:`ExternalSegment` documents this).  A confirmed 线段 may contain an
interior extreme beyond its own endpoints — the kernel checks the extreme in the
segment's direction, not the opposite one — so the endpoint pair
``[min(start,end), max(start,end)]`` is *not* the segment's range in general.
Substituting the endpoints would silently change ``zd``/``zg`` and could invent or
erase a centre.  The pen series that produced the segments is therefore a
mandatory input: the range is the extreme of the pen endpoints inside
``pen_start_index..pen_end_index``, the same set the segment kernel calls the
segment's membership.  ``evidence_pen_indices`` deliberately reach *past* the end
into the next segment's span, so they are **not** folded into the range (that
would double count and move the zone).

If a caller only has the segment snapshot, there is no honest way to know the
range; the adapter refuses (``ValueError``) instead of guessing.  ``'unknown'``
复权 stays ``'unknown'``: it is carried in the source identity and is a different
sample, never rewritten to ``none``/``qfq``/``hfq``.  The centre kernel has no
adjustment field, so the label is bound into ``snapshot_version`` — a centre
computed under one basis can then never be passed to, or compared with, another.

Causality: two clocks, one prefix
---------------------------------
The adapter invents no timestamps.  Each segment's ``confirmed_on`` is the day
the segment kernel sealed it, copied verbatim after re-verification against its
evidence; a centre is only knowable on the third segment's confirmation day, and
:attr:`CentreAdapterResult.visible_on` exposes that — never the raw structure
start day.  A prefix never learns from a later prefix: nothing is back-filled,
and a blocked refusal *after* a confirmed centre does **not** retract the centre
(the confirmation of the leading three segments is already final).  What it does
do is set ``chain_complete=False`` and add a blocking issue, so no reader can
mistake a partially blocked structure for a complete one.

Reported states, one refusal
----------------------------
``status`` is one of:

* ``centre`` — three or more confirmed segments were available and the leading
  three ranges have a strictly positive intersection;
* ``no_centre`` — three or more confirmed segments were available and their leading
  three ranges share no **strictly positive-width** interval.  That is a
  **measured** absence, not missing data
  (``absence_reason='zero_width_intersection'``, the centre kernel's own label,
  which covers an exact touch and a negative intersection alike);
* ``not_enough_confirmed_segments`` — fewer than three confirmed segments and the
  segment kernel has not refused.  It means "not enough yet"; it must never be
  displayed as "没有中枢";
* ``blocked_before_centre`` — the segment kernel refused (``blocked`` pending or
  segment) before three confirmed segments existed, so the confirmed prefix is
  empty or too short *and* incomplete.  No centre claim is made either way; the
  blocking reasons are returned in ``issues`` and the status is distinct from
  "not enough", which would wrongly suggest that waiting is enough.

``issues`` always separates a **blocking** refusal (a ``blocked`` segment or
pending carrying the segment kernel's own reason) from a non-blocking exclusion
(an open ``candidate`` tail, which may still drift and is therefore not
geometry).  ``chain_complete`` is ``False`` as soon as one blocking issue exists.

Fail-closed input checks (raise, never repair)
----------------------------------------------
:func:`centre_from_segment_snapshot` raises ``ValueError`` when

* the segment snapshot was not built by :data:`SEGMENT_KERNEL_VERSION`, or its
  ``as_of_day`` differs from its binding, or a non-empty confirmed prefix carries
  no ``as_of_day`` at all (nothing dated = nothing causal), or the binding's own
  ``adjust`` is not one of :data:`ADJUSTMENT_BASES`;
* the pen snapshot's kernel differs from the segment snapshot's
  ``source_pen_kernel_version``, its ``as_of_day`` differs, or its recomputed
  digest differs from ``SegmentSnapshot.input_digest`` — that digest is the only
  proof that the pens are the ones the segments were derived from;
* a ``confirmed`` segment carries a reason other than ``'first_case_no_gap'``
  (the kernel never emits that, so it is a doctored line), or a ``confirmed``
  segment appears after a non-confirmed one, or the confirmed run is not exactly
  :attr:`SegmentSnapshot.confirmed_segments`;
* pen spans are not the contiguous chain start (no re-anchoring), or they
  overlap/skip, or a span pen is not ``confirmed``, or the segment's endpoints do
  not reproduce the pens' own endpoints (merged index, day, price, kind);
* the derived range does not cover the endpoints, or the segment's end is not the
  span's extreme in the segment's direction (a label contradicting its own pens);
* ``confirmed_on`` is missing, precedes/equals the geometric end, is later than
  ``as_of_day``, or disagrees with the evidence pens' latest confirmation.

Those are contradictions, not recall limits: a recall limit is reported as a
blocking issue, a contradiction is refused.

Not implemented (do not claim otherwise)
----------------------------------------
* 第67课 **第二种情况** (gap case), 线段破坏后的重划 and initial-containment
  merging are still missing upstream; a chain that hits them is blocked, so the
  confirmed prefix stops there.  This adapter never fills the hole.
* One centre at most, from the leading three confirmed segments; no centre
  discovery, no 中枢延伸 geometry, no multi-centre, no level upgrade, no 走势类型,
  no 买卖点, no trading signal.  ``strictness='operational_not_strict'`` is not a
  synonym for the 18th-lesson definition.
* No HTTP, no DB, no UI: pure functions over values, deterministic for one input.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Literal

from src.core.chan_operational_centre import (
    BASIS as _CENTRE_BASIS,
    INPUT_KIND as CENTRE_INPUT_KIND,
    SEGMENTS_ENGINE,
    STRICTNESS as _CENTRE_STRICTNESS,
    ExternalSegment,
    OperationalCentreSnapshot,
    SegmentChain,
    centre_from_chain,
)
from src.core.chan_pen import PenSnapshot
from src.core.chan_segment import (
    ADJUSTMENT_BASES,
    SEGMENT_KERNEL_VERSION,
    Segment,
    SegmentSnapshot,
    segment_input_digest,
)

__all__ = [
    'ADAPTER_VERSION',
    'BASIS',
    'INPUT_KIND',
    'LIMITATIONS',
    'STRICTNESS',
    'AdapterIssue',
    'CentreAdapterResult',
    'CentreSource',
    'centre_from_segment_snapshot',
]

#: Version of this adapter.  Any behaviour change needs a new version.
ADAPTER_VERSION = 'chan-centre-adapter-v1'

#: The identity of this input: a confirmed prefix produced by the segment kernel.
INPUT_KIND = 'segment_kernel_confirmed_prefix'

#: Inherited from the centre kernel: geometry measured on segment legs as a0.
BASIS = _CENTRE_BASIS

#: Inherited honestly: operational geometry, **not** the strict 18th lesson.
STRICTNESS = _CENTRE_STRICTNESS

#: Everything this result may *not* be read as.  Copied to every result so a
#: consumer never has to guess which half of the theory is missing.
LIMITATIONS: tuple[str, ...] = (
    'segment_a0_geometry_only',
    'operational_not_strict_no_18th_lesson_definition',
    'case2_gap_segments_not_implemented_upstream',
    'segment_destruction_repartition_not_implemented_upstream',
    'characteristic_initial_containment_blocks_upstream',
    'one_centre_from_leading_three_confirmed_segments_only',
    'no_centre_discovery_no_level_upgrade_no_trend',
    'no_buy_sell_points_no_trading_signal',
)

_Status = Literal['centre', 'no_centre', 'not_enough_confirmed_segments', 'blocked_before_centre']

_FIRST_CASE = 'first_case_no_gap'


@dataclass(frozen=True)
class CentreSource:
    """Identity of the sample this result belongs to: never mix two of them.

    ``pen_digest`` is the segment snapshot's ``input_digest`` — a SHA-256 over the
    pen series (positions, prices, statuses, confirmation days, kernels, as-of
    day).  ``symbol``/``adjust`` are the segment binding's caller declarations and
    cannot be verified from the pens, so they are carried verbatim and compared
    exactly.  ``adjust='unknown'`` stays ``'unknown'``.

    ``snapshot_version`` is the string the centre kernel binds a centre to.  It
    folds in the adjustment label because the centre kernel has no field for it:
    two chains from different bases then carry different versions and
    :func:`centre_from_chain` refuses the cross-pairing instead of silently
    comparing incomparable zones.
    """

    symbol: str
    adjust: str
    as_of_day: str | None
    pen_digest: str
    segment_kernel: str
    pen_kernel: str
    adapter_version: str = ADAPTER_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError('symbol must be a non-empty string')
        if self.adjust not in ADJUSTMENT_BASES:
            raise ValueError(
                f'unsupported adjustment base {self.adjust!r}; declare '
                f"'unknown' when it cannot be verified, never guess one of {ADJUSTMENT_BASES}"
            )
        if self.as_of_day is not None:
            _day(self.as_of_day, 'as_of_day')
        if not isinstance(self.pen_digest, str) or not self.pen_digest:
            raise ValueError('a centre source needs the pen-series digest it was built from')

    @property
    def snapshot_version(self) -> str:
        """The centre chain's ``snapshot_version``: kernel + 复权 label + digest."""
        return f'{self.segment_kernel}|adjust={self.adjust}|pens={self.pen_digest}'

    def same_source(self, other: 'CentreSource') -> bool:
        """True only for the very same sample; ``unknown`` is not ``none``."""
        return self == other

    def assert_same_source(self, other: 'CentreSource') -> None:
        if not self.same_source(other):
            raise ValueError(
                'centre results come from different sources '
                f'(symbol={self.symbol!r}/{other.symbol!r}, adjust={self.adjust!r}/{other.adjust!r}, '
                f'as_of={self.as_of_day!r}/{other.as_of_day!r}, '
                f'pens={self.pen_digest[:12]}.../{other.pen_digest[:12]}...); never mix them'
            )


@dataclass(frozen=True)
class AdapterIssue:
    """A segment-kernel decision that kept a line out of the chain, or blocked it.

    ``blocking=True`` means the segment kernel *refused* (``blocked`` segment or
    pending) and the structure is therefore incomplete beyond this point;
    ``blocking=False`` means the entry is merely not eligible yet (an open
    ``candidate`` tail or an undecided pending), which is normal replay state.
    ``reason`` is the kernel's own reason string, copied, never translated.
    """

    reason: str
    detail: str
    blocking: bool
    segment_index: int | None = None
    pen_index: int | None = None


@dataclass(frozen=True)
class CentreAdapterResult:
    """One prefix's answer: the chain that entered, the centre if provable, the gaps.

    ``centre_snapshot`` is the untouched :class:`OperationalCentreSnapshot` from
    :func:`centre_from_chain` (or ``None`` when the kernel was not called), so its
    own labels, clocks and absence reason stay auditable.  ``segments`` is the
    confirmed prefix exactly as handed over; ``issues`` names everything excluded
    and why.
    """

    status: _Status
    absence_reason: str | None
    source: CentreSource
    chain: SegmentChain | None
    segments: tuple[ExternalSegment, ...]
    confirmed_prefix_length: int
    total_segments: int
    issues: tuple[AdapterIssue, ...]
    centre_snapshot: OperationalCentreSnapshot | None
    adapter_version: str = ADAPTER_VERSION
    input_kind: str = INPUT_KIND
    basis: str = BASIS
    strictness: str = STRICTNESS
    centre_input_kind: str = CENTRE_INPUT_KIND
    segments_engine: str = SEGMENTS_ENGINE
    limitations: tuple[str, ...] = LIMITATIONS

    @property
    def chain_complete(self) -> bool:
        """False once the segment kernel refused anywhere in the snapshot."""
        return not any(issue.blocking for issue in self.issues)

    @property
    def centre(self):
        """The :class:`OperationalCentre` itself, if one was provable."""
        return None if self.centre_snapshot is None else self.centre_snapshot.centre

    @property
    def lifecycle_status(self) -> str | None:
        return None if self.centre_snapshot is None else self.centre_snapshot.lifecycle_status

    @property
    def visible_on(self) -> str | None:
        """The day this structure is knowable — never the raw start day."""
        return None if self.centre_snapshot is None else self.centre_snapshot.visible_on

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        return tuple(issue.reason for issue in self.issues if issue.blocking)

    def same_source(self, other: 'CentreAdapterResult') -> bool:
        return self.source.same_source(other.source)

    def assert_same_source(self, other: 'CentreAdapterResult') -> None:
        self.source.assert_same_source(other.source)

    def describe(self) -> str:
        """One honest sentence: what is claimed, what is not, what is missing."""
        head = (
            f'{self.basis}/{self.strictness} centre adapter {self.adapter_version} '
            f'({self.source.symbol}, adjust={self.source.adjust}, as_of={self.source.as_of_day})'
        )
        if self.status == 'centre':
            centre = self.centre
            if centre is None:  # pragma: no cover - status and snapshot are set together
                raise RuntimeError('centre status without a centre snapshot')
            text = (
                f'{head}: operational centre [{centre.zd}, {centre.zg}] confirmed on '
                f'{centre.confirmed_on} from the leading confirmed segments '
                f'{centre.segment_indices} (not a strict 第18课 中枢)'
            )
            if not self.chain_complete:
                text += (
                    '; the structure after the confirmed prefix is INCOMPLETE: '
                    + '; '.join(self.blocked_reasons)
                )
            return text
        if self.status == 'no_centre':
            return (
                f'{head}: measured NO centre — the leading three confirmed segment ranges '
                'share no strictly positive-width interval; this is a measured absence, '
                'not missing data'
            )
        if self.status == 'blocked_before_centre':
            return (
                f'{head}: NO centre claim — the segment kernel blocked before three confirmed '
                f'segments existed ({", ".join(self.blocked_reasons)}); the data is incomplete '
                f'({self.confirmed_prefix_length} confirmed segment(s) available)'
            )
        return (
            f'{head}: NOT ENOUGH confirmed segments ({self.confirmed_prefix_length}); '
            'no centre claim either way — waiting for more confirmed lines is not the '
            'same as "no centre"'
        )


def _day(value, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f'{field} must be an ISO day string, got {value!r}')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f'{field} is not an ISO day ({value!r})') from None


def _check_binding(snapshot: SegmentSnapshot) -> CentreSource:
    """Verify the segment snapshot can name its own sample, then build the source."""
    if snapshot.kernel_version != SEGMENT_KERNEL_VERSION:
        raise ValueError(
            f'segment snapshot kernel {snapshot.kernel_version!r} is not '
            f'{SEGMENT_KERNEL_VERSION!r}; this adapter only reads that rule set'
        )
    if snapshot.as_of_day is None:
        # Only an empty prefix may be undated (there is nothing to date yet); a
        # confirmed line always carries its own days, checked in _external_segment.
        pass
    else:
        _day(snapshot.as_of_day, 'segment snapshot as_of_day')
    binding = snapshot.binding
    if binding.as_of_day != snapshot.as_of_day:
        raise ValueError(
            f'binding as_of_day {binding.as_of_day!r} does not match the snapshot '
            f'({snapshot.as_of_day!r}); the result is not bound to its own sample'
        )
    if binding.adjust not in ADJUSTMENT_BASES:
        raise ValueError(
            f"unsupported adjustment base {binding.adjust!r}; 'unknown' is a declared "
            'absence, never a synonym for none/qfq/hfq'
        )
    return CentreSource(
        symbol=binding.symbol,
        adjust=binding.adjust,
        as_of_day=snapshot.as_of_day,
        pen_digest=snapshot.input_digest,
        segment_kernel=snapshot.kernel_version,
        pen_kernel=snapshot.source_pen_kernel_version,
    )


def _check_pens(pen_snapshot: PenSnapshot, snapshot: SegmentSnapshot, source: CentreSource) -> None:
    """The pens must be provably the series the segments were derived from."""
    if not isinstance(pen_snapshot, PenSnapshot):
        raise ValueError('pen_snapshot must be a PenSnapshot: the segment range is read from it')
    if pen_snapshot.kernel_version != source.pen_kernel:
        raise ValueError(
            f'pen snapshot kernel {pen_snapshot.kernel_version!r} differs from the segment '
            f"snapshot's source_pen_kernel_version {source.pen_kernel!r}"
        )
    if pen_snapshot.as_of_day != source.as_of_day:
        raise ValueError(
            f'pen snapshot as_of_day {pen_snapshot.as_of_day!r} differs from the segment '
            f'snapshot ({source.as_of_day!r}); the two describe different prefixes'
        )
    digest = segment_input_digest(pen_snapshot)
    if digest != snapshot.input_digest:
        raise ValueError(
            'pen snapshot digest does not match SegmentSnapshot.input_digest '
            f'({digest[:12]}... vs {snapshot.input_digest[:12]}...); these pens are not '
            'provably the ones the segments were derived from, so no range may be taken from them'
        )


def _confirmed_prefix(snapshot: SegmentSnapshot) -> tuple[Segment, ...]:
    """The contiguous leading run of publishable lines — and nothing else.

    A ``candidate`` end may still drift and a ``blocked`` entry is a refusal: both
    are excluded here, which is why the prefix (not ``segments``) is the geometry.
    """
    prefix: list[Segment] = []
    for segment in snapshot.segments:
        if segment.status == 'confirmed' and segment.reason == _FIRST_CASE:
            prefix.append(segment)
            continue
        break
    for segment in snapshot.segments:
        if segment.status == 'confirmed' and segment.reason != _FIRST_CASE:
            raise ValueError(
                f'segment {segment.pen_start_index}..{segment.pen_end_index} claims '
                f'status=confirmed with reason {segment.reason!r}; the kernel only publishes '
                f'{_FIRST_CASE!r} as confirmed, so this line is not usable geometry'
            )
    trailing = tuple(s for s in snapshot.segments[len(prefix):] if s.status == 'confirmed')
    if trailing:
        raise ValueError(
            f'{len(trailing)} confirmed segment(s) appear after an unconfirmed one; the '
            'confirmed run must be a contiguous prefix, never a set with holes'
        )
    if tuple(prefix) != snapshot.confirmed_segments:
        raise ValueError(
            'the contiguous confirmed prefix differs from SegmentSnapshot.confirmed_segments; '
            'the snapshot contradicts its own consumer contract'
        )
    return tuple(prefix)


def _check_span(segment: Segment, pens, position: int) -> None:
    """Re-verify the pen span without trusting the segment's own labels."""
    start, end = segment.pen_start_index, segment.pen_end_index
    if position == 0 and start != 0:
        raise ValueError(
            f'the first confirmed segment starts at pen {start}, not the chain start 0; '
            'a re-anchored chain has no causal meaning here'
        )
    if end < start or end >= len(pens):
        raise ValueError(
            f'segment {position} spans pens {start}..{end}, outside the '
            f'{len(pens)} supplied pens'
        )
    span = pens[start : end + 1]
    if any(pen.status != 'confirmed' for pen in span):
        raise ValueError(
            f'segment {position} spans a pen that is not confirmed; a confirmed line may '
            'not rest on unfrozen evidence'
        )
    for pen in span:
        if pen.status == 'confirmed' and pen.confirmed_on is None:  # pragma: no cover
            raise ValueError(f'segment {position} spans a confirmed pen without a confirmation day')
    for pen, following in zip(span, span[1:]):
        if pen.end.merged_index != following.start.merged_index:
            raise ValueError(
                f'segment {position} spans pens {start}..{end} that do not share a merged '
                f'index ({pen.end.merged_index} vs {following.start.merged_index})'
            )
        if (
            pen.end.position_day != following.start.position_day
            or pen.end.price != following.start.price
            or pen.end.kind != following.start.kind
        ):
            raise ValueError(
                f'segment {position} spans pens that do not meet at one anchor '
                f'({pen.end.position_day}@{pen.end.price} vs '
                f'{following.start.position_day}@{following.start.price})'
            )
        if pen.direction == following.direction:
            raise ValueError(f'segment {position} spans two pens of one direction; pens alternate')
    first, last = span[0], span[-1]
    for endpoint, pen_endpoint, where in (
        (segment.start, first.start, 'start'),
        (segment.end, last.end, 'end'),
    ):
        if (
            endpoint.kind != pen_endpoint.kind
            or endpoint.merged_index != pen_endpoint.merged_index
            or endpoint.position_day != pen_endpoint.position_day
            or endpoint.price != pen_endpoint.price
        ):
            raise ValueError(
                f'segment {position} {where} endpoint '
                f'({endpoint.kind} {endpoint.position_day}@{endpoint.price}, merged '
                f'{endpoint.merged_index}) does not reproduce its pen '
                f'({pen_endpoint.kind} {pen_endpoint.position_day}@{pen_endpoint.price}, merged '
                f'{pen_endpoint.merged_index})'
            )
        if endpoint.status != 'confirmed':
            raise ValueError(
                f'segment {position} {where} endpoint is {endpoint.status!r}; a confirmed '
                'segment needs both endpoints frozen'
            )


def _external_segment(
    segment: Segment, position: int, pens, source: CentreSource
) -> ExternalSegment:
    """Turn one confirmed line into a centre-ready segment, range read from its pens."""
    _check_span(segment, pens, position)
    start, end = segment.pen_start_index, segment.pen_end_index
    first, last = pens[start], pens[end]
    direction = segment.direction
    expected = 'up' if first.start.kind == 'bottom' else 'down'
    if direction != expected or first.direction != expected:
        raise ValueError(
            f'segment {position} is labelled {direction!r} but joins '
            f'{first.start.kind}->{last.end.kind}; the label contradicts its pens'
        )
    if segment.kernel_version != SEGMENT_KERNEL_VERSION:  # pragma: no cover - snapshot-level gate
        raise ValueError(
            f'segment {position} was built by {segment.kernel_version!r}, not '
            f'{SEGMENT_KERNEL_VERSION!r}'
        )

    prices = [p.start.price for p in pens[start : end + 1]] + [
        p.end.price for p in pens[start : end + 1]
    ]
    if any(not isfinite(price) for price in prices):  # pragma: no cover - pen kernel gate
        raise ValueError(f'segment {position} spans a non-finite price')
    low, high = min(prices), max(prices)
    if high <= low:
        raise ValueError(f'segment {position} spans a degenerate range [{low}, {high}]')
    if segment.start_price == segment.end_price:
        raise ValueError(f'segment {position} has zero length ({segment.start_price})')
    if direction == 'up' and segment.end_price != high:
        raise ValueError(
            f'up segment {position} ends at {segment.end_price} but its pens reach {high}; '
            'the end must be the span extreme in the segment direction'
        )
    if direction == 'down' and segment.end_price != low:
        raise ValueError(
            f'down segment {position} ends at {segment.end_price} but its pens reach {low}; '
            'the end must be the span extreme in the segment direction'
        )
    if low > min(segment.start_price, segment.end_price) or high < max(
        segment.start_price, segment.end_price
    ):  # pragma: no cover - low/high are taken over all endpoints
        raise ValueError(f'segment {position} range [{low}, {high}] misses its own endpoints')

    start_day = _day(segment.start.position_day, f'segment {position} start day')
    end_day = _day(segment.end.position_day, f'segment {position} end day')
    if end_day <= start_day:
        raise ValueError(
            f'segment {position} ends on {segment.end.position_day}, not after its start '
            f'{segment.start.position_day}'
        )
    if segment.confirmed_on is None:
        raise ValueError(f'segment {position} is confirmed without a confirmation day')
    confirmed = _day(segment.confirmed_on, f'segment {position} confirmed_on')
    if confirmed <= end_day:
        raise ValueError(
            f'segment {position} claims confirmation on {segment.confirmed_on}, not after its '
            f'own end day {segment.end.position_day}; a confirmation may never precede the evidence'
        )
    if confirmed > _day(source.as_of_day, 'as_of_day'):
        raise ValueError(
            f'segment {position} is confirmed on {segment.confirmed_on}, after as_of_day '
            f'{source.as_of_day}; that is look-ahead'
        )
    if segment.confirmed_on != segment.evidence_confirmed_on:
        raise ValueError(
            f'segment {position} is confirmed on {segment.confirmed_on} but its evidence froze '
            f'on {segment.evidence_confirmed_on}; the confirmation must be the evidence day'
        )
    evidence_days = [e.confirmed_on for e in segment.evidence if e.confirmed_on]
    if evidence_days and max(evidence_days) != segment.confirmed_on:
        raise ValueError(
            f'segment {position} claims confirmation on {segment.confirmed_on}, but its latest '
            f'evidence is {max(evidence_days)}'
        )

    return ExternalSegment(
        index=position,
        direction=direction,
        start_day=segment.start.position_day,
        start_price=float(segment.start_price),
        end_day=segment.end.position_day,
        end_price=float(segment.end_price),
        high=float(high),
        low=float(low),
        status='confirmed',
        confirmed_on=segment.confirmed_on,
    )


def _tail_issues(snapshot: SegmentSnapshot, prefix_length: int) -> tuple[AdapterIssue, ...]:
    """Everything excluded from the prefix, with the kernel's own reason."""
    issues: list[AdapterIssue] = []
    for position, segment in enumerate(snapshot.segments[prefix_length:], start=prefix_length):
        if segment.status == 'blocked':
            issues.append(
                AdapterIssue(
                    reason=segment.reason,
                    detail=(
                        f'the segment kernel refused a line over pens '
                        f'{segment.pen_start_index}..{segment.pen_end_index}: {segment.reason}'
                    ),
                    blocking=True,
                    segment_index=position,
                    pen_index=segment.pen_start_index,
                )
            )
        elif segment.status == 'candidate':
            issues.append(
                AdapterIssue(
                    reason='candidate_segment_excluded',
                    detail=(
                        f'pens {segment.pen_start_index}..{segment.pen_end_index} form a '
                        'candidate segment whose end may still drift; it is not geometry'
                    ),
                    blocking=False,
                    segment_index=position,
                    pen_index=segment.pen_start_index,
                )
            )
    for pending in snapshot.pending:
        blocking = pending.status == 'blocked'
        issues.append(
            AdapterIssue(
                reason=pending.reason if blocking else 'pending_candidate',
                detail=(
                    f'pending start at pen {pending.pen_start_index} is {pending.status} '
                    f'({pending.reason}){": " + pending.detail if pending.detail else ""}'
                ),
                blocking=blocking,
                pen_index=(
                    pending.blocked_at_pen_index
                    if pending.blocked_at_pen_index is not None
                    else pending.end_pen_index
                ),
            )
        )
    return tuple(issues)


def centre_from_segment_snapshot(
    segment_snapshot: SegmentSnapshot,
    *,
    pen_snapshot: PenSnapshot,
    expect_source: CentreSource | None = None,
) -> CentreAdapterResult:
    """Adapt one segment prefix and build the operational centre when it is provable.

    ``pen_snapshot`` is mandatory: it is where the segments' *ranges* come from
    (endpoint pairs are not ranges).  ``expect_source`` pins the sample the caller
    believes it is reading; a mismatch raises instead of returning a centre for
    another symbol/adjustment/prefix.

    The centre kernel is called only with three or more confirmed segments; with
    fewer the result is ``not_enough_confirmed_segments`` (or
    ``blocked_before_centre`` when the segment kernel refused), never a
    "no centre" claim.
    """
    if not isinstance(segment_snapshot, SegmentSnapshot):
        raise ValueError('segment_snapshot must be a SegmentSnapshot')
    source = _check_binding(segment_snapshot)
    if expect_source is not None:
        source.assert_same_source(expect_source)

    prefix = _confirmed_prefix(segment_snapshot)
    if prefix and source.as_of_day is None:
        raise ValueError(
            'the snapshot contains confirmed segments but no as_of_day; an undated result '
            'cannot be placed in time, so no causal centre may be built from it'
        )
    _check_pens(pen_snapshot, segment_snapshot, source)
    pens = pen_snapshot.pens
    segments: list[ExternalSegment] = []
    previous: ExternalSegment | None = None
    for position, segment in enumerate(prefix):
        external = _external_segment(segment, position, pens, source)
        if previous is not None:
            if segment.pen_start_index != prefix[position - 1].pen_end_index + 1:
                raise ValueError(
                    f'segment {position} starts at pen {segment.pen_start_index} but the '
                    f'previous confirmed segment ends at pen {prefix[position - 1].pen_end_index}; '
                    'the confirmed spans must be contiguous'
                )
            if external.start_day != previous.end_day or external.start_price != previous.end_price:
                raise ValueError(
                    f'segment {position} starts at {external.start_day}@{external.start_price} '
                    f'but segment {position - 1} ended at {previous.end_day}@{previous.end_price}'
                )
            if external.direction == previous.direction:
                raise ValueError(f'segment {position} repeats the direction of segment {position - 1}')
            if _day(external.confirmed_on, 'confirmed_on') < _day(
                previous.confirmed_on, 'confirmed_on'
            ):
                raise ValueError(
                    f'segment {position} is confirmed before segment {position - 1}; evidence '
                    'cannot be known out of order'
                )
        segments.append(external)
        previous = external

    issues = _tail_issues(segment_snapshot, len(segments))
    blocking = any(issue.blocking for issue in issues)
    chain: SegmentChain | None = None
    if segments:
        as_of = source.as_of_day
        if as_of is None:  # pragma: no cover - guarded right after the prefix
            raise ValueError('a chain of confirmed segments cannot be undated')
        chain = SegmentChain(
            instrument=source.symbol,
            snapshot_version=source.snapshot_version,
            basis=BASIS,
            as_of_day=as_of,
            segments=tuple(segments),
        )

    def result(
        status: _Status,
        absence: str | None,
        centre_snapshot: OperationalCentreSnapshot | None,
    ) -> CentreAdapterResult:
        return CentreAdapterResult(
            status=status,
            absence_reason=absence,
            source=source,
            chain=chain,
            segments=tuple(segments),
            confirmed_prefix_length=len(segments),
            total_segments=len(segment_snapshot.segments),
            issues=issues,
            centre_snapshot=centre_snapshot,
        )

    if len(segments) < 3:
        if blocking:
            return result('blocked_before_centre', 'segment_kernel_blocked', None)
        return result('not_enough_confirmed_segments', 'fewer_than_three_confirmed_segments', None)

    if chain is None:  # pragma: no cover - three segments always build a chain
        raise RuntimeError('three confirmed segments must build a chain')
    centre_snapshot = centre_from_chain(
        chain,
        expect_instrument=source.symbol,
        expect_snapshot_version=source.snapshot_version,
    )
    if (
        centre_snapshot.snapshot_version != source.snapshot_version
        or centre_snapshot.instrument != source.symbol
        or centre_snapshot.as_of_day != source.as_of_day
        or centre_snapshot.basis != BASIS
        or centre_snapshot.strictness != STRICTNESS
        or centre_snapshot.input_kind != CENTRE_INPUT_KIND
        or centre_snapshot.segments_engine != SEGMENTS_ENGINE
        or centre_snapshot.segment_count != len(segments)
    ):  # pragma: no cover - the kernel copies these from the chain it was given
        raise ValueError(
            'the centre snapshot does not describe the chain it was built from; refusing to '
            'return a result whose labels disagree with its own input'
        )
    if centre_snapshot.centre is None:
        return result('no_centre', centre_snapshot.absence_reason, centre_snapshot)
    return result('centre', None, centre_snapshot)
