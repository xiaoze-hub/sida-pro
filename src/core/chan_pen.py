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
2. **Spacing** — the two three-bar fractals must share no bar, i.e. their
   centre merged indices differ by at least ``MIN_INDEX_GAP = 4``
   (``j - i >= 4`` puts at least one merged bar in neither window).
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

Confirmation
------------
A reverse fractal alone never confirms anything: a top stays revisable while
price can still make a higher high, which is exactly the case where a naive
implementation publishes a pen and then retracts it.  An endpoint is frozen
(``status='confirmed'``) only when the chain grows a **valid opposite leg** from
it, because from then on any new extreme is a *new* pen rather than an extension
of this one.  ``confirmed_on`` is the day that evidence became known (the day
the opposite fractal's own right merged bar was sealed), never the extremum's
own day.  So:

* the last endpoint of the chain is always ``candidate`` and its price may move;
* every earlier endpoint is ``confirmed`` and immutable, therefore every pen but
  the last one is ``confirmed`` and the last is ``candidate``;
* ``confirmed_on`` is conservative (late), never early — a prefix can never
  learn something a later prefix contradicts.

Fail-closed behaviour
---------------------
* Fractals the structure kernel reports as ``blocked`` (unknown-direction
  envelope) are never endpoints; they are recorded and their merged index joins
  ``taint_indices``.  Any pen spanning a tainted index is reported ``blocked``
  with ``confirmed_on=None`` — readable for audit, never presented as fixed.
* Unconfirmed (``candidate``) fractals never become structure: they can still
  disappear from the next prefix.
* Unknown/inconsistent inputs raise ``ValueError`` instead of being repaired:
  unsupported rule versions, fractals out of merged-index order, a confirmed
  fractal without a confirmation day, a confirmation day after ``as_of_day``,
  or a fractal price that does not match the merged bar it points at.

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

#: Minimum merged-index distance between the two fractal centres: with a
#: distance of 4 at least one merged bar belongs to neither three-bar window.
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
    """Versioned rule set.  Unsupported values raise instead of being ignored."""

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
        if self.min_index_gap < MIN_INDEX_GAP:
            raise ValueError(
                f'min_index_gap {self.min_index_gap} is laxer than the v1 rule '
                f'({MIN_INDEX_GAP}); a different rule needs a new kernel version'
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


def _validate_snapshot(snapshot: ChanSnapshot) -> None:
    """Refuse an inconsistent structure snapshot instead of repairing it."""
    merged = snapshot.merged_bars
    previous = -1
    for fractal in snapshot.fractals:
        index = fractal.position_merged_index
        if index <= previous:
            raise ValueError(
                'fractals must be ordered by strictly increasing merged index'
            )
        previous = index
        if not 0 <= index < len(merged):
            raise ValueError(f'fractal merged index {index} is outside the merged bars')
        bar = merged[index]
        expected = bar.high if fractal.kind == 'top' else bar.low
        if fractal.position_price != expected:
            raise ValueError(
                f'fractal at merged index {index} has price {fractal.position_price} '
                f'but the merged bar extremum is {expected}'
            )
        if fractal.status == 'confirmed':
            if fractal.confirmed_on is None:
                raise ValueError(
                    f'fractal at merged index {index} is confirmed without a confirmation day'
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
        elif fractal.status == 'blocked':
            if fractal.confirmed_on is not None:
                raise ValueError(
                    f'blocked fractal at merged index {index} must not have a confirmation day'
                )
        else:  # pragma: no cover - structure kernel only emits the three states
            raise ValueError(f'unknown fractal status {fractal.status!r}')


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
