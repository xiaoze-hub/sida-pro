"""Chan-theory v1: pens (笔) over confirmed fractals — read-only, causal, versioned.

Scope and input contract
------------------------
Pure computation on top of :mod:`src.core.chan_structure`.  Callers pass
**completed** bars; the kernel never inspects anything after the last supplied
bar, so every snapshot is a deterministic replay of exactly that prefix.

Pen rules v1 (``PEN_KERNEL_VERSION = 'chan-pen-v1'``)
----------------------------------------------------
A pen joins two **confirmed** fractals of opposite kind.  All three conditions
are required; any failure is reported as a :class:`PenRejection` and never
guessed around:

1. **Alternation** — the endpoints are a top and a bottom.
2. **Spacing** — the two three-bar windows must be separated by a merged bar
   that belongs to *neither* window: ``j - i >= MIN_INDEX_GAP = 4``.  Sharing no
   bar at all only needs ``j - i >= 3``; v1 is deliberately one bar stricter, so
   the leg is never a mere abutting of windows.  ``MIN_INDEX_GAP`` is **pinned**
   by :class:`PenRules` — it is not a tunable floor, because a snapshot labelled
   ``chan-pen-v1`` must never mean two different rule sets.
3. **Price** — the top merged bar's range must be at least partly *above* the
   bottom merged bar's range: ``top.high > bottom.high``.  Equality is not
   "above", so it fails closed.

Rule 3 is deliberately the weakest reading of "at least partly above" and is
named (``price_gate='top_high_above_bottom_high'``) so a stricter variant can be
versioned later instead of silently replacing this one.

Endpoint drift (the important part)
-----------------------------------
Endpoint identities are **not** frozen on first sight.  The pen kernel keeps one
open (``candidate``) endpoint at the tail of the alternating chain:

* **Same direction, more extreme** — e.g. a higher high after a top.  The open
  endpoint *moves* to the new fractal, ``revisions`` is incremented and the
  abandoned position day is appended to ``drift_days``; the superseded fractal
  is still listed as a rejection so the move is auditable.  The move is refused
  (``extension_not_legal``) when the new fractal is not a legal leg partner of
  the previous endpoint — that is what keeps a short, non-pen bounce from
  hijacking the endpoint.  While the chain is a single anchor there is no leg
  to violate, so the anchor simply takes the more extreme value.
* **Same direction, equal extreme** — the earlier endpoint is kept
  (``equal_extreme_kept_earlier``).  An equal extreme is not new information and
  may not move a position.
* **Same direction, less extreme** — rejected (``not_more_extreme``).

Confirmation — why, and exactly when
------------------------------------
A reverse fractal alone never confirms anything: a top stays revisable while
price can still make a higher high, which is exactly the case where a naive
implementation publishes a pen and then retracts it.

An endpoint freezes because the chain **grew a valid opposite leg out of it**,
and that leg's evidence is the one and only clock: the opposite fractal's
three-bar window becomes final only when its right merged bar is sealed.  So
``confirmed_on`` is exactly ``merged_bars[end_index + 1].sealed_on`` — the day
the opposite fractal became known — never the extremum's own day and never an
earlier day a caller might prefer.  Freezing is one-way inside a replay: once a
valid leg exists below a top, any later same-direction extreme starts the *next*
pen instead of extending this one, which is why a confirmed pen can never be
rewritten, moved or retracted by a later prefix.

Consequences:

* the last endpoint of the chain is always ``candidate`` and its price may move;
* every earlier endpoint is ``confirmed`` and immutable, therefore every pen but
  the last one is ``confirmed`` and the last is ``candidate``;
* ``confirmed_on`` is conservative (late), never early — a prefix can never
  learn something a later prefix contradicts.

Fail-closed behaviour
---------------------
``pen_from_snapshot`` accepts snapshots produced *elsewhere*, so nothing is
trusted: every fact is re-verified and a mismatch raises ``ValueError`` instead
of being repaired.  For each fractal the kernel re-checks

* merged-index order, and that the centre has both neighbours
  (``1 <= i <= len(merged_bars) - 2``);
* the strict three-bar top/bottom geometry itself, so a label alone cannot mint
  a pen out of bars that never formed a fractal;
* ``position_day`` **and** ``position_price`` against the merged bar's own
  extremum;
* that no bar of the window is an ``envelope_only`` (unknown-direction) bar when
  the fractal claims ``confirmed``;
* ``confirmed_on == merged_bars[i + 1].sealed_on`` and
  ``<= as_of_day`` — a confirmation may never precede the bar that proves it;
* that ``candidate`` really has an open right bar, and ``blocked`` really has an
  unknown-direction bar: the status is derived, not a free label.

The snapshot's merged bars are re-verified too: their source ``index`` must be
strictly increasing, and only the tail bar may be open (``sealed_on is None``).
A sealed tail would describe structure past ``as_of_day``.

Fractals the structure kernel reports as ``blocked`` are never endpoints; they
are recorded and their merged index joins ``taint_indices``.  Any pen spanning a
tainted index is reported ``blocked`` with ``confirmed_on=None`` — readable for
audit, never presented as fixed.  Unconfirmed (``candidate``) fractals never
become structure: they can still disappear from the next prefix.

Consumer contract (API responses, frontend)
-------------------------------------------
Anything that stores or displays this output must keep the **input snapshot
digest** next to it — at minimum ``as_of_day`` plus the merged-bar series that
produced the pens — and must never mix results computed from different price
adjustment bases (不复权 / 前复权 / 后复权).  The same merged index means a
different bar under another basis, so a "matching" pen can describe different
prices; nothing in this module can detect that, the caller owns it.

Not implemented (do not claim otherwise)
----------------------------------------
No 线段 (segments), no 中枢 (centres), no buy/sell points, no multi-level
recursion, no volume/effort rules and no trading signals.  In particular three
overlapping pens are **not** a 前三笔重叠 中枢 here — that structure needs a
separate, explicitly versioned kernel with its own causal contract.  Do not
describe this module's output as a 中枢.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Sequence

from src.core.chan_structure import (
    ChanSnapshot,
    Fractal,
    MergedBar,
    RawBar,
    build_chan_structure,
)

__all__ = [
    'MIN_INDEX_GAP',
    'PEN_KERNEL_VERSION',
    'Pen',
    'PenEndpoint',
    'PenRejection',
    'PenRules',
    'PenSnapshot',
    'build_chan_pen',
    'pen_from_snapshot',
    'pen_price_gate_ok',
]

#: Version of the pen rule set.  Any behaviour change needs a new version.
PEN_KERNEL_VERSION = 'chan-pen-v1'

#: Minimum merged-index distance between the two fractal centres.  Pinned by
#: :class:`PenRules`: at 4 at least one merged bar belongs to neither three-bar
#: window (sharing no bar would only need 3), and v1 is exactly this value.
MIN_INDEX_GAP = 4

_PRICE_GATE = 'top_high_above_bottom_high'
_EQUAL_EXTREME = 'keep_earlier'

_Kind = Literal['top', 'bottom']
_EndpointStatus = Literal['candidate', 'confirmed']
_PenStatus = Literal['candidate', 'confirmed', 'blocked']
_RejectReason = Literal[
    'blocked_fractal',
    'spacing',
    'price_gate',
    'not_more_extreme',
    'equal_extreme_kept_earlier',
    'superseded_by_more_extreme',
    'extension_not_legal',
]


@dataclass(frozen=True)
class PenRules:
    """Versioned rule set.  Unsupported values raise instead of being ignored.

    Every field is *pinned* to what v1 means.  ``min_index_gap`` in particular
    is not a tunable floor: accepting e.g. 5 while still reporting
    ``kernel_version='chan-pen-v1'`` would make the same version string describe
    two different rule sets, so anything but the v1 value is refused.  A new
    rule needs a new kernel version *and* to replace this module.
    """

    kernel_version: str = PEN_KERNEL_VERSION
    min_index_gap: int = MIN_INDEX_GAP
    price_gate: str = _PRICE_GATE
    equal_extreme: str = _EQUAL_EXTREME

    def __post_init__(self) -> None:
        if self.kernel_version != PEN_KERNEL_VERSION:
            raise ValueError(
                f'unsupported pen kernel version {self.kernel_version!r}; '
                f'this module implements {PEN_KERNEL_VERSION!r}'
            )
        if self.min_index_gap != MIN_INDEX_GAP:
            raise ValueError(
                f'min_index_gap {self.min_index_gap} is not the v1 rule '
                f'({MIN_INDEX_GAP}); the gap is pinned per kernel version, so a '
                'different value needs a new kernel version'
            )
        if self.price_gate != _PRICE_GATE:
            raise ValueError(f'unsupported price_gate {self.price_gate!r}')
        if self.equal_extreme != _EQUAL_EXTREME:
            raise ValueError(f'unsupported equal_extreme rule {self.equal_extreme!r}')


@dataclass(frozen=True)
class PenEndpoint:
    """One end of a pen, with its position *and* its confirmation time.

    ``bar_high``/``bar_low`` are the merged bar's range at ``merged_index``
    (the price gate is defined on ranges, not on single prices), while ``price``
    is the extremum actually used by the pen.

    ``revisions``/``drift_days`` record how the endpoint moved: every
    superseded position day is kept, so a drifting endpoint never looks
    original.  ``status='candidate'`` means the price may still move.
    """

    kind: _Kind
    merged_index: int
    position_day: str
    price: float
    bar_high: float
    bar_low: float
    revisions: int
    drift_days: tuple[str, ...]
    status: _EndpointStatus
    confirmed_on: str | None


@dataclass(frozen=True)
class Pen:
    """A pen between two endpoints.

    ``status`` is ``candidate`` while its end can still drift, ``confirmed``
    once frozen, ``blocked`` when a tainted window lies inside its span (audit
    only, never a fixed price).  ``confirmed_on`` is the day the *end* endpoint
    was frozen — the earliest day on which both prices are final.  A confirmed
    pen is never rewritten, moved or retracted by a later prefix.
    """

    start: PenEndpoint
    end: PenEndpoint
    direction: Literal['up', 'down']
    status: _PenStatus
    confirmed_on: str | None
    kernel_version: str

    @property
    def index_gap(self) -> int:
        return self.end.merged_index - self.start.merged_index

    @property
    def start_price(self) -> float:
        return self.start.price

    @property
    def end_price(self) -> float:
        return self.end.price

    @property
    def start_day(self) -> str:
        return self.start.position_day

    @property
    def end_day(self) -> str:
        return self.end.position_day


@dataclass(frozen=True)
class PenRejection:
    """A confirmed fractal that did not become (or stopped being) an endpoint.

    Kept so every conservative decision is visible: nothing is silently dropped.
    """

    kind: _Kind
    merged_index: int
    position_day: str
    price: float
    reason: _RejectReason
    detail: str = ''


@dataclass(frozen=True)
class PenSnapshot:
    """Replayable pen result for one input prefix."""

    as_of_day: str | None
    kernel_version: str
    rules: PenRules
    endpoints: tuple[PenEndpoint, ...]
    pens: tuple[Pen, ...]
    rejections: tuple[PenRejection, ...]
    blocked_fractals: tuple[Fractal, ...]
    taint_indices: tuple[int, ...]

    @property
    def confirmed_pens(self) -> tuple[Pen, ...]:
        return tuple(p for p in self.pens if p.status == 'confirmed')

    @property
    def candidate_pens(self) -> tuple[Pen, ...]:
        return tuple(p for p in self.pens if p.status == 'candidate')

    @property
    def blocked_pens(self) -> tuple[Pen, ...]:
        """Pens whose span covers a tainted window; audit only."""
        return tuple(p for p in self.pens if p.status == 'blocked')

    @property
    def open_endpoint(self) -> PenEndpoint | None:
        """The single endpoint whose price may still move, if any."""
        for endpoint in reversed(self.endpoints):
            if endpoint.status == 'candidate':
                return endpoint
        return None


def pen_price_gate_ok(*, top_high: float, bottom_high: float) -> bool:
    """v1 price gate: the top merged bar is at least partly above the bottom one.

    Equality does not count as "above": returning ``False`` keeps the kernel
    from publishing an ambiguous pen.
    """
    return top_high > bottom_high


def _validate_merged_bars(merged: Sequence[MergedBar]) -> None:
    """Merged bars must be ordered source bars and only the tail may be open.

    ``MergedBar.index`` is the *raw* bar that created the merged bar, so it is
    strictly increasing across the series (a merged bar absorbs several raw
    bars), while ``Fractal.position_merged_index`` counts positions in this
    tuple.  Both are checked against the tuple they belong to.
    """
    tail = len(merged) - 1
    previous_source = -1
    for position, bar in enumerate(merged):
        if bar.index <= previous_source:
            raise ValueError(
                f'merged bar at position {position} has source index {bar.index}, '
                'which is not strictly after the previous merged bar; the series '
                'is out of order or duplicated'
            )
        previous_source = bar.index
        if position < tail and bar.sealed_on is None:
            raise ValueError(
                f'merged bar {position} is not the tail but has no sealed_on; '
                'only the last supplied bar may still be forming'
            )
        if position == tail and bar.sealed_on is not None:
            raise ValueError(
                f'the tail merged bar is sealed on {bar.sealed_on}, which would '
                'describe structure after as_of_day; the tail must stay open'
            )


def _validate_fractal(
    snapshot: ChanSnapshot, merged: Sequence[MergedBar], fractal: Fractal
) -> None:
    """Re-derive one fractal from the bars instead of trusting its labels.

    Everything a pen would later rely on is checked here: window completeness,
    strict geometry, extremum day/price, the unknown-direction taint, and the
    exact confirmation day (the right neighbour's ``sealed_on``).  A failure
    raises rather than being repaired, because a foreign snapshot that lies
    about any of it would leak into published pens.
    """
    index = fractal.position_merged_index
    last_centre = len(merged) - 2
    if not 1 <= index <= last_centre:
        raise ValueError(
            f'fractal at merged index {index} needs both neighbouring merged bars; '
            f'valid centres are 1..{last_centre}'
        )

    left, middle, right = merged[index - 1], merged[index], merged[index + 1]
    if fractal.kind == 'top':
        expected_price, expected_day = middle.high, middle.high_day
        shaped = (
            middle.high > left.high
            and middle.low > left.low
            and middle.high > right.high
            and middle.low > right.low
        )
    elif fractal.kind == 'bottom':
        expected_price, expected_day = middle.low, middle.low_day
        shaped = (
            middle.low < left.low
            and middle.high < left.high
            and middle.low < right.low
            and middle.high < right.high
        )
    else:
        raise ValueError(f'unknown fractal kind {fractal.kind!r}')

    if not shaped:
        raise ValueError(
            f'{fractal.kind} fractal at merged index {index} is not a strict '
            f'three-bar {fractal.kind} of merged bars '
            f'{index - 1}, {index}, {index + 1}'
        )
    if fractal.position_price != expected_price:
        raise ValueError(
            f'fractal at merged index {index} has price {fractal.position_price} '
            f'but the merged bar extremum is {expected_price}'
        )
    if fractal.position_day != expected_day:
        raise ValueError(
            f'fractal at merged index {index} has position day '
            f'{fractal.position_day} but the merged bar extremum day is '
            f'{expected_day}'
        )

    tainted = left.envelope_only or middle.envelope_only or right.envelope_only
    sealed_on = right.sealed_on

    if fractal.status == 'confirmed':
        if fractal.confirmed_on is None:
            raise ValueError(
                f'fractal at merged index {index} is confirmed without a confirmation day'
            )
        if tainted:
            raise ValueError(
                f'fractal at merged index {index} cannot be confirmed: its window '
                'contains an unknown-direction envelope bar'
            )
        if sealed_on is None:
            raise ValueError(
                f'fractal at merged index {index} cannot be confirmed: its right '
                'merged bar is the still-open tail'
            )
        if fractal.confirmed_on != sealed_on:
            raise ValueError(
                f'fractal at merged index {index} claims confirmation on '
                f'{fractal.confirmed_on} but its right merged bar was sealed on '
                f'{sealed_on}; a confirmation may never precede its evidence'
            )
        if fractal.confirmed_on <= fractal.position_day:
            raise ValueError(
                f'fractal at merged index {index} is confirmed on '
                f'{fractal.confirmed_on}, not after its own position day '
                f'{fractal.position_day}'
            )
        if snapshot.as_of_day is None or fractal.confirmed_on > snapshot.as_of_day:
            raise ValueError(
                f'fractal at merged index {index} is confirmed after as_of_day'
            )
    elif fractal.status == 'candidate':
        if fractal.confirmed_on is not None:
            raise ValueError(
                f'candidate fractal at merged index {index} must not have a confirmation day'
            )
        if tainted:
            raise ValueError(
                f'fractal at merged index {index} touches an unknown-direction '
                'envelope, so it must be blocked, not candidate'
            )
        if sealed_on is not None:
            raise ValueError(
                f'fractal at merged index {index} is a candidate although its right '
                f'merged bar was sealed on {sealed_on}; a final window is confirmed '
                'or blocked, never a candidate'
            )
    elif fractal.status == 'blocked':
        if fractal.confirmed_on is not None:
            raise ValueError(
                f'blocked fractal at merged index {index} must not have a confirmation day'
            )
        if not tainted:
            raise ValueError(
                f'fractal at merged index {index} is blocked although no bar of its '
                'window is an unknown-direction envelope'
            )
    else:  # pragma: no cover - structure kernel only emits the three states
        raise ValueError(f'unknown fractal status {fractal.status!r}')


def _validate_snapshot(snapshot: ChanSnapshot) -> None:
    """Refuse an inconsistent structure snapshot instead of repairing it."""
    merged = snapshot.merged_bars
    _validate_merged_bars(merged)
    previous = -1
    for fractal in snapshot.fractals:
        index = fractal.position_merged_index
        if index <= previous:
            raise ValueError(
                'fractals must be ordered by strictly increasing merged index'
            )
        previous = index
        _validate_fractal(snapshot, merged, fractal)


def _endpoint(fractal: Fractal, bar: MergedBar) -> PenEndpoint:
    return PenEndpoint(
        kind=fractal.kind,
        merged_index=fractal.position_merged_index,
        position_day=fractal.position_day,
        price=fractal.position_price,
        bar_high=bar.high,
        bar_low=bar.low,
        revisions=0,
        drift_days=(),
        status='candidate',
        confirmed_on=None,
    )


def _leg_gate(
    start: PenEndpoint,
    kind: _Kind,
    index: int,
    bar_high: float,
    bar_low: float,
    rules: PenRules,
) -> _RejectReason | None:
    """``None`` when ``start`` and the candidate form a legal pen, else why not."""
    if index - start.merged_index < rules.min_index_gap:
        return 'spacing'
    if start.kind == kind:  # pragma: no cover - callers guarantee alternation
        raise ValueError('pen endpoints must alternate')
    if start.kind == 'bottom':
        ok = pen_price_gate_ok(top_high=bar_high, bottom_high=start.bar_high)
    else:
        ok = pen_price_gate_ok(top_high=start.bar_high, bottom_high=bar_high)
    return None if ok else 'price_gate'


def _more_extreme(fractal: Fractal, endpoint: PenEndpoint) -> bool:
    if fractal.kind == 'top':
        return fractal.position_price > endpoint.price
    return fractal.position_price < endpoint.price


def pen_from_snapshot(
    snapshot: ChanSnapshot, *, rules: PenRules | None = None
) -> PenSnapshot:
    """Derive pens from an existing (already causal) structure snapshot."""
    rules = PenRules() if rules is None else rules
    _validate_snapshot(snapshot)

    merged = snapshot.merged_bars
    chain: list[PenEndpoint] = []
    rejections: list[PenRejection] = []
    blocked: list[Fractal] = []

    def reject(fractal: Fractal, reason: _RejectReason, detail: str = '') -> None:
        rejections.append(
            PenRejection(
                kind=fractal.kind,
                merged_index=fractal.position_merged_index,
                position_day=fractal.position_day,
                price=fractal.position_price,
                reason=reason,
                detail=detail,
            )
        )

    for fractal in snapshot.fractals:
        bar = merged[fractal.position_merged_index]
        if fractal.status == 'blocked':
            blocked.append(fractal)
            reject(fractal, 'blocked_fractal', 'unknown-direction envelope in the window')
            continue
        if fractal.status != 'confirmed':
            # A candidate can vanish from the next prefix: never structure.
            continue

        if not chain:
            chain.append(_endpoint(fractal, bar))
            continue

        last = chain[-1]
        if last.status != 'candidate':  # pragma: no cover - invariant guard
            raise ValueError('internal error: only the chain tail may be revisable')

        if fractal.kind == last.kind:
            if not _more_extreme(fractal, last):
                reason: _RejectReason = (
                    'equal_extreme_kept_earlier'
                    if fractal.position_price == last.price
                    else 'not_more_extreme'
                )
                reject(fractal, reason, f'open endpoint stays at merged index {last.merged_index}')
                continue
            # A new extreme may only take the endpoint over when it is itself a
            # legal leg partner of the previous endpoint; otherwise a short
            # bounce would silently rewrite the chain.
            gate: _RejectReason | None = None
            if len(chain) >= 2:
                gate = _leg_gate(
                    chain[-2],
                    fractal.kind,
                    fractal.position_merged_index,
                    bar.high,
                    bar.low,
                    rules,
                )
            if gate is not None:
                reject(
                    fractal,
                    'extension_not_legal',
                    f'more extreme than the open endpoint but not a valid leg ({gate})',
                )
                continue
            # The *abandoned* position is the auditable fact here.
            rejections.append(
                PenRejection(
                    kind=last.kind,
                    merged_index=last.merged_index,
                    position_day=last.position_day,
                    price=last.price,
                    reason='superseded_by_more_extreme',
                    detail=(
                        'open endpoint moved to '
                        f'{fractal.position_day}@{fractal.position_price}'
                    ),
                )
            )
            chain[-1] = replace(
                last,
                merged_index=fractal.position_merged_index,
                position_day=fractal.position_day,
                price=fractal.position_price,
                bar_high=bar.high,
                bar_low=bar.low,
                revisions=last.revisions + 1,
                drift_days=last.drift_days + (last.position_day,),
                status='candidate',
                confirmed_on=None,
            )
            continue

        # Opposite kind: the only event that can freeze the previous endpoint.
        gate = _leg_gate(
            last,
            fractal.kind,
            fractal.position_merged_index,
            bar.high,
            bar.low,
            rules,
        )
        if gate is not None:
            reject(fractal, gate, 'cannot form a pen with the open endpoint')
            continue
        if fractal.confirmed_on is None:  # pragma: no cover - validated above
            raise ValueError('a pen evidence fractal must be confirmed')
        if fractal.confirmed_on < last.position_day:  # pragma: no cover - invariant guard
            raise ValueError('confirmation precedes the confirmed endpoint position')
        chain[-1] = replace(last, status='confirmed', confirmed_on=fractal.confirmed_on)
        chain.append(_endpoint(fractal, bar))

    taint = tuple(sorted({f.position_merged_index for f in blocked}))
    pens: list[Pen] = []
    for start, end in zip(chain, chain[1:]):
        status: _PenStatus = 'confirmed' if end.status == 'confirmed' else 'candidate'
        confirmed_on = end.confirmed_on if status == 'confirmed' else None
        if any(start.merged_index <= t <= end.merged_index for t in taint):
            status, confirmed_on = 'blocked', None
        pens.append(
            Pen(
                start=start,
                end=end,
                direction='up' if start.kind == 'bottom' else 'down',
                status=status,
                confirmed_on=confirmed_on,
                kernel_version=rules.kernel_version,
            )
        )

    return PenSnapshot(
        as_of_day=snapshot.as_of_day,
        kernel_version=rules.kernel_version,
        rules=rules,
        endpoints=tuple(chain),
        pens=tuple(pens),
        rejections=tuple(rejections),
        blocked_fractals=tuple(blocked),
        taint_indices=taint,
    )


def build_chan_pen(
    bars: Sequence[RawBar], *, rules: PenRules | None = None
) -> PenSnapshot:
    """Build the structure for ``bars`` and derive pens from it."""
    return pen_from_snapshot(build_chan_structure(bars), rules=rules)
