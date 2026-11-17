"""Chan-theory v2: 线段 (segments) over an explicitly supplied 笔 (pen) sequence.

Scope and input contract
------------------------
Pure computation on top of :mod:`src.core.chan_pen`.  The caller supplies one
**already causal** pen snapshot; this kernel never inspects bars, sessions or
anything after ``as_of_day``, so every result is a deterministic replay of that
pen prefix.  No graph is fetched here and no snapshot is re-derived: pens are
the input, segments are the output.

Segment rules v2 (``SEGMENT_KERNEL_VERSION = 'chan-segment-v2'``)
-----------------------------------------------------------------
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
   neighbour, so a transitive closure would give a different range).

   包含处理 direction (v2)
   ~~~~~~~~~~~~~~~~~~~~~~~
   The merge direction is the **trend of the characteristic sequence itself**,
   exactly as ordinary K-lines are merged: when the new element is contained by
   its predecessor, the surviving extrema are decided by the move that led into
   that pair, i.e. by the two most recent **already normalised, non-contained**
   elements.  Rising (both high and low higher) keeps ``max`` high / ``max`` low,
   falling keeps ``min``/``min``.

   This replaces v1's "pin the merge to the segment direction" rule, which was a
   *choice*, not the lesson's rule, and which diverges on real series (v1 could
   collapse a sequence that 第67课 leaves at four elements, and then either stall
   the segment forever or confirm it at the wrong endpoint).

   **No prior trend, no merge.**  When the first pair is already contained there
   is no earlier element pair to read a direction from, so it is reported as
   ``blocked`` (``characteristic_initial_containment``) instead of silently
   defaulting to one direction.  A defaulted direction is indistinguishable from
   a real one after the fact, which is exactly the kind of invention this module
   refuses.  (Because containment is *inclusive*, two normalised neighbours are
   always strictly ordered — an equal high or equal low would itself be
   containment — so the first pair is the only reachable case; the second guard
   reason ``characteristic_merge_direction_unknown`` exists for a crossing pair
   that the normalisation must never produce and is kept fail-closed.)

Segment end (the important part)
--------------------------------
第67课: "该线段在该顶分型的高点处结束".  The segment therefore ends at the pen that
**carries the terminal fractal's own extreme**, not merely at "some earlier pen
with an equal price":

* the middle element of the fractal absorbs one or more reverse pens; its
  extreme side (``high`` for an up segment) is the start price of one of them;
* the segment's end pen is the opposite-direction pen immediately *before* that
  characteristic pen (``i - 1``), whose end price **is** that extreme;
* if several absorbed pens carry the same extreme (an equal high after
  normalisation), the **latest** one wins (``equal_fractal_extreme='keep_latest'``).
  Ending at an earlier equal top would leave a same-price top inside the next
  segment, which no later segment could ever terminate on, so the latest
  occurrence is the only self-consistent choice.

Note that this is *not* the open-end drift rule: an open (candidate) endpoint
still keeps the earlier position on an equal extreme (``equal_extreme``), because
moving an endpoint on an equal price is churn, not evidence.

Confirmation time
-----------------
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

Evidence is reported separately from the geometric span
-------------------------------------------------------
A :class:`Segment` carries ``evidence``: the three fractal elements (role,
absorbed pens, own range, own confirmation day).  Those pens are usually *not*
inside ``pen_start_index..pen_end_index`` — the fractal's right element is the
very pen that printed the rejection that ended the walk, so it sits **after** the
segment's end.  ``evidence_pen_indices`` / ``evidence_confirmed_on`` expose that
set, and ``pen_end_index`` never absorbs it: a confirmed segment's geometry and
the evidence that froze it are two different things.

Fail-closed behaviour
---------------------
``segment_from_pens`` accepts snapshots produced elsewhere, so nothing is
trusted.  It re-verifies pen order, endpoint continuity (shared merged index,
day and price), alternating directions, direction-vs-price, the pen kernel's own
minimum merged-index gap, strictly increasing **ISO position days** per pen and
across the series, kernel version, ``status``/``confirmed_on`` consistency,
``confirmed_on`` strictly after its own position day and never after
``as_of_day``, monotone confirmation days, and that only the tail pen may be a
candidate.  Any mismatch raises ``ValueError`` instead of being repaired.

Nothing is ever published as a line that cannot be one: when the decision point
is closer than :data:`MIN_PENS` pens to the chain start, the outcome is a
``SegmentPending`` (blocked or candidate, with the reason kept) — never a
one-pen "segment", and never an internal exception.

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
* ``characteristic_initial_containment`` — the first pair is contained: no prior
                trend exists, so the merge direction would be invented.
* ``characteristic_merge_direction_unknown`` — the pair before the contained one
                is ambiguous (equal high or low), same refusal.
* ``characteristic_fractal_below_prior_extreme`` — the fractal's middle is not
                the sequence's extreme (an earlier pen was more extreme), so
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
The digest covers the *series* only (symbol/adjust are caller declarations the
kernel cannot verify); :meth:`SegmentSnapshot.same_source` therefore requires
digest **and** binding to agree.  Two results with different digests, symbols,
adjustment bases or kernels describe different lines even when the numbers look
alike; use :meth:`SegmentSnapshot.same_source` / :meth:`SegmentSnapshot.assert_same_source`
and keep the digest next to anything you store or display.  :meth:`is_stale`
answers "was this computed before day X" — a stale result must not be presented
as today's structure.

The adjustment base is one of :data:`ADJUSTMENT_BASES`
(``none``/``qfq``/``hfq``/``unknown``).  ``unknown`` is a **declared** absence of
a verifiable base, not a synonym for ``none``: historical samples frequently
have no trustworthy adjustment metadata, and requiring a concrete base would
force a caller to fabricate one.  It is an identity label only — the rules never
read it — so an ``unknown`` result has the same geometry as the others but is a
*different sample* (:meth:`SegmentSnapshot.same_source` is ``False``), is never
rewritten to a "real" base, and must not be mixed with one.  Any other value is
refused: "I do not know" has one spelling, and a made-up base is not it.

Consumer contract: ``confirmed_segments`` is the only safe view
---------------------------------------------------------------
A downstream builder (中枢, 走势, multi-level recursion) must read
:attr:`SegmentSnapshot.confirmed_segments`, never ``segments``.  That property
returns **only** ``confirmed`` 第67课 第一种情况 lines and refuses ``candidate``
(its end may still drift) and ``blocked`` (evidence without a publishable line):
turning a refusal into geometry invents a pivot out of a decision the kernel
explicitly declined to take.  ``segments`` stays the full audit view, with
``blocked_segments`` / ``candidate_segments`` naming the rest.

The membership of a segment is its pen span ``pen_start_index..pen_end_index``
only.  ``evidence_pen_indices`` deliberately reaches past the end into the next
segment's span, so a consumer that unions evidence into the geometry would count
those pens **twice**; spans of consecutive confirmed segments are contiguous and
disjoint, which is what makes the view usable as a partition.  Nothing here
builds a 中枢 — that consumer is simply given a set it can trust.

Recall limits (documented, not bugs)
------------------------------------
These rules are conservative on purpose.  The price of never inventing a line is
lower recall, and the limits are enumerated here so no caller mistakes them for
temporary gaps:

* **initial containment blocks the chain.**  When the first characteristic pair
  is already contained there is no prior trend to read a merge direction from, so
  the start is reported as a ``blocked`` :class:`SegmentPending` (reason
  ``characteristic_initial_containment``, ``blocked_at_pen_index`` = the evidence
  pen) and everything after it is left undecided.  A defaulted direction would be
  indistinguishable from a real one, so it is refused.
* **no re-anchoring.**  A blocked or non-overlapping start is never dropped in
  favour of a later pen.  Re-anchoring is a rule v2 does not implement: it would
  make the result depend on how much future data the caller happens to hold, i.e.
  a lookahead that changes the answer.  ``pen_start_index`` is always the chain
  start, and extending the prefix cannot move it or unlock the refusal.
* **no future data.**  Only confirmed pens are evidence and ``confirmed_on`` is
  the latest freeze among the fractal's elements, so a prefix never asserts
  something a longer prefix disproves.  Endpoint drift belongs to the open tail.
* **only 第一种情况.**  A gap (第二种情况), 线段破坏后的重划 and 中枢 remain
  unimplemented; those inputs end in an explicit ``blocked`` state with a reason,
  not in a guessed continuation.

If a caller needs a line where v2 reports ``blocked``, the honest options are to
implement the missing case under a new kernel version or to show the refusal —
not to relax these limits here.

Not implemented (do not claim otherwise)
----------------------------------------
* 第67课 **第二种情况** (gap → wait for the second characteristic sequence).
* 线段破坏后的重划 / 复杂递归划分: a blocked boundary stops the chain, it is never
  patched with a guessed continuation.
* 中枢 (centres) — three overlapping pens are **not** a 中枢, and this module
  does not build one.  No 买卖点, no multi-level recursion, no volume/effort
  rules, no trading signals.
* No re-anchoring: if the first three pens do not overlap, the chain is blocked
  rather than restarted from a later pen (that rule is not part of v2).
* This is *not* a complete 线段 implementation: it is the verifiable no-gap
  case plus explicit blocked states for everything else.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Literal, Sequence

from src.core.chan_pen import MIN_INDEX_GAP, PEN_KERNEL_VERSION, PenSnapshot

__all__ = [
    'ADJUSTMENT_BASES',
    'MIN_PENS',
    'SEGMENT_KERNEL_VERSION',
    'CharacteristicElement',
    'FractalEvidence',
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
#:
#: v1 -> v2: 包含处理 direction is read from the characteristic sequence itself
#: (was: pinned to the segment direction); the segment end is the pen carrying the
#: fractal's own extreme (was: the first pen at an equal price, which could be a
#: pen that is not the fractal's extreme at all); evidence pens/days are reported
#: separately from the geometric span; and input validation now also enforces the
#: pen kernel's merged-index gap and a strictly increasing ISO day order.
#:
#: Still v2: the ``'unknown'`` adjustment label and
#: ``SegmentPending.blocked_at_pen_index`` are reporting fixes, not rule changes —
#: no segment is added, moved or confirmed differently, the labels are never read
#: by the rules, and the pending fields only stop pointing at the wrong pen.
SEGMENT_KERNEL_VERSION = 'chan-segment-v2'

#: A segment needs at least three pens, and their ranges must overlap.
MIN_PENS = 3

#: Accepted 复权 labels on :class:`SegmentBinding`.
#:
#: ``'unknown'`` is a first-class label, not a placeholder to be filled in: many
#: historical samples have no trustworthy adjustment metadata, and forcing a
#: caller to declare ``none``/``qfq``/``hfq`` would make it assert a fact it does
#: not have.  The label is *identity only* — the rules never read it — so two
#: results under different labels are different samples (different source) even
#: though their segment geometry is identical.  Nothing rewrites ``'unknown'``
#: into a "real" base; a caller that later verifies the sample publishes a new
#: result under the verified label.
ADJUSTMENT_BASES: tuple[str, ...] = ('none', 'qfq', 'hfq', 'unknown')

_OVERLAP = 'pen_endpoint_range_strict'
_CHARACTERISTIC = 'reverse_pens'
_INCLUSION = 'sequential_non_transitive'
_MERGE_DIRECTION = 'recent_two_elements'
_SEGMENT_END = 'fractal_middle_extreme_pen'
_EQUAL_FRACTAL_EXTREME = 'keep_latest'
_GAP_CASE = 'blocked_case2_not_implemented'
_INITIAL_CONTAINMENT = 'blocked'
_EQUAL_EXTREME = 'keep_earlier'
_EVIDENCE = 'confirmed_pens_only'
_INPUT_GATE = 'iso_days_strict_and_pen_index_gap'

_Direction = Literal['up', 'down']
_Status = Literal['candidate', 'confirmed', 'blocked']

_PendingReason = Literal[
    'awaiting_three_pens',
    'awaiting_three_confirmed_pens',
    'first_three_pens_no_overlap',
    'tainted_pen',
    'characteristic_gap_case2',
    'characteristic_initial_containment',
    'characteristic_merge_direction_unknown',
    'characteristic_fractal_below_prior_extreme',
    'fractal_extreme_pen_missing',
    'evidence_inconsistent',
]
_SegmentReason = Literal[
    'first_case_no_gap',
    'awaiting_confirmed_characteristic_pen',
    'characteristic_gap_case2',
    'characteristic_initial_containment',
    'characteristic_merge_direction_unknown',
    'characteristic_fractal_below_prior_extreme',
    'tainted_pen',
]
_RejectReason = Literal[
    'first_three_pens_no_overlap',
    'characteristic_gap_case2',
    'characteristic_initial_containment',
    'characteristic_merge_direction_unknown',
    'characteristic_fractal_below_prior_extreme',
    'superseded_by_more_extreme',
    'equal_extreme_kept_earlier',
    'tainted_pen',
]


@dataclass(frozen=True)
class SegmentRules:
    """Versioned rule set.  Every field is *pinned* to what v2 means.

    Changing any value while still reporting ``chan-segment-v2`` would make one
    version string describe two different rule sets, so unsupported values
    raise.  A new rule needs a new kernel version *and* a replacement module.

    ``adjustment_labels`` is the accepted :class:`SegmentBinding` vocabulary; it
    is pinned with the rest because an accepted label is part of the contract a
    consumer relies on.  ``'unknown'`` is a declared non-verification, not a
    default: dropping it would force callers to fabricate a base.
    """

    kernel_version: str = SEGMENT_KERNEL_VERSION
    min_pens: int = MIN_PENS
    min_index_gap: int = MIN_INDEX_GAP
    overlap: str = _OVERLAP
    characteristic_sequence: str = _CHARACTERISTIC
    inclusion: str = _INCLUSION
    merge_direction: str = _MERGE_DIRECTION
    segment_end: str = _SEGMENT_END
    equal_fractal_extreme: str = _EQUAL_FRACTAL_EXTREME
    gap_case: str = _GAP_CASE
    initial_containment: str = _INITIAL_CONTAINMENT
    equal_extreme: str = _EQUAL_EXTREME
    evidence: str = _EVIDENCE
    input_gate: str = _INPUT_GATE
    adjustment_labels: tuple[str, ...] = ADJUSTMENT_BASES

    def __post_init__(self) -> None:
        expected = {
            'kernel_version': SEGMENT_KERNEL_VERSION,
            'min_pens': MIN_PENS,
            'min_index_gap': MIN_INDEX_GAP,
            'overlap': _OVERLAP,
            'characteristic_sequence': _CHARACTERISTIC,
            'inclusion': _INCLUSION,
            'merge_direction': _MERGE_DIRECTION,
            'segment_end': _SEGMENT_END,
            'equal_fractal_extreme': _EQUAL_FRACTAL_EXTREME,
            'gap_case': _GAP_CASE,
            'initial_containment': _INITIAL_CONTAINMENT,
            'equal_extreme': _EQUAL_EXTREME,
            'evidence': _EVIDENCE,
            'input_gate': _INPUT_GATE,
            'adjustment_labels': ADJUSTMENT_BASES,
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

    ``adjust`` is one of :data:`ADJUSTMENT_BASES`.  ``'unknown'`` exists so an
    unverifiable historical sample can be *declared* as unknown instead of being
    coerced into ``'none'``/``'qfq'``/``'hfq'``: an invented base is a fabricated
    fact, and it would also make two genuinely different samples compare as one.
    It is therefore never treated as ``'none'`` — ``same_source`` requires the
    labels to be equal, and the label is never rewritten.  Anything else (an
    empty string, ``'adjusted'``, a made-up name) is refused: an unrecognised
    base is not the same thing as a base we know we do not know.
    """

    symbol: str
    adjust: str
    as_of_day: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError('symbol must be a non-empty string')
        if self.adjust not in ADJUSTMENT_BASES:
            raise ValueError(
                f'unsupported adjustment base {self.adjust!r}; v2 knows {ADJUSTMENT_BASES}. '
                "Declare 'unknown' when the sample's adjustment cannot be verified — "
                'never guess none/qfq/hfq on its behalf'
            )
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
class FractalEvidence:
    """One element of the terminal fractal a segment's decision rests on.

    ``pen_indices`` are the characteristic (reverse) pens the element absorbed,
    ``high``/``low`` its normalised range and ``confirmed_on`` the day it froze.
    The ``right`` element is by construction *after* the segment's end pen, so
    evidence pens and the segment's geometric span are not the same set.
    """

    role: Literal['left', 'middle', 'right']
    pen_indices: tuple[int, ...]
    high: float
    low: float
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
    third characteristic element was frozen.  ``pens_used`` is always at least
    :data:`MIN_PENS`: a shorter span is reported as a :class:`SegmentPending`.
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
    evidence: tuple[FractalEvidence, ...] = ()

    @property
    def pens_used(self) -> int:
        return self.pen_end_index - self.pen_start_index + 1

    @property
    def start_price(self) -> float:
        return self.start.price

    @property
    def end_price(self) -> float:
        return self.end.price

    @property
    def evidence_pen_indices(self) -> tuple[int, ...]:
        """Every characteristic pen the decision rests on, ascending.

        These need not lie inside ``pen_start_index..pen_end_index``: the fractal's
        right element comes after the end pen.  Keep the two sets apart when
        displaying or storing a segment.
        """
        return tuple(sorted({i for element in self.evidence for i in element.pen_indices}))

    @property
    def evidence_confirmed_on(self) -> str | None:
        """The day the last of the fractal's elements froze (== ``confirmed_on``)."""
        days = [element.confirmed_on for element in self.evidence if element.confirmed_on]
        return max(days) if days else None


@dataclass(frozen=True)
class SegmentPending:
    """A chain start that cannot become a segment yet (or at all).

    Reported instead of a guessed segment: not enough pens, not enough
    *confirmed* pens, a non-overlapping trio, a tainted pen, or a decision point
    that is closer than :data:`MIN_PENS` pens to the chain start.  The position
    day and price of the would-be start are kept so the decision is auditable.

    Where the refusal happened is reported in one of two fields, never both:

    * ``end_pen_index`` — the end the refused span *would* have had, for
      refusals decided on a span (non-overlapping trio, shorter than
      :data:`MIN_PENS`, an open candidate awaiting its characteristic pen).
    * ``blocked_at_pen_index`` — the **evidence pen** whose own value made the
      decision undefined, for refusals that are a decision at a pen rather than
      about an end (特征序列 initial containment, ambiguous merge direction).
      There is no would-be end in those cases, so ``end_pen_index`` stays
      ``None`` and ``pens_used`` is ``0``: claiming the chain start as the
      "end" would point the reader at the wrong pen and invent a one-pen span.

    ``pen_start_index`` is always the chain start — a refusal never re-anchors
    the chain forward (see "Recall limits" in the module docstring).
    """

    pen_start_index: int
    direction: _Direction
    status: Literal['candidate', 'blocked']
    reason: _PendingReason
    start_position_day: str
    start_price: float
    confirmed_pens: int
    detail: str = ''
    end_pen_index: int | None = None
    blocked_at_pen_index: int | None = None

    def __post_init__(self) -> None:
        if self.blocked_at_pen_index is not None:
            if self.status != 'blocked':
                raise ValueError(
                    f'blocked_at_pen_index={self.blocked_at_pen_index} on a '
                    f'{self.status!r} pending; only a blocked refusal has a decision pen'
                )
            if self.blocked_at_pen_index < self.pen_start_index:
                raise ValueError(
                    f'blocked_at_pen_index={self.blocked_at_pen_index} is before the chain '
                    f'start {self.pen_start_index}; the decision pen belongs to this chain'
                )
        if self.end_pen_index is not None and self.end_pen_index < self.pen_start_index:
            raise ValueError(
                f'end_pen_index={self.end_pen_index} is before the chain start '
                f'{self.pen_start_index}'
            )

    @property
    def pens_used(self) -> int:
        """How many pens the refused end span covered (0 when there is no end).

        A decision-pen refusal (``blocked_at_pen_index``) never had a would-be
        end, so this stays ``0`` — it is not the number of pens examined.
        """
        if self.end_pen_index is None:
            return 0
        return self.end_pen_index - self.pen_start_index + 1


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
        """The **consumer contract**: only lines that are frozen 第67课 第一种情况.

        This is the view a downstream structure builder (中枢, 走势, anything
        higher level) may consume.  It deliberately excludes:

        * ``candidate`` — the tail's end may still drift, so it is not a line yet;
        * ``blocked`` — evidence exists but no line may be published (a gap case,
          an undefined 包含处理 direction, a tainted pen).  Using a blocked entry
          as geometry would invent a pivot out of a refusal.

        Filtering is on ``status`` **and** on ``reason == 'first_case_no_gap'``:
        the kernel never emits a ``confirmed`` segment for any other reason, so a
        doctored entry that sets ``status='confirmed'`` but keeps a refusal
        reason is refused here rather than downstream.  ``segments`` remains the
        full audit view; ``blocked_segments`` / ``candidate_segments`` expose the
        rest by name.

        The membership is the pen span ``pen_start_index..pen_end_index`` only:
        ``evidence_pen_indices`` reaches past a segment's end into the next
        segment's span, so a caller that unions evidence into the geometry would
        count those pens twice.  Spans of consecutive confirmed segments are
        contiguous and disjoint.
        """
        return tuple(
            s
            for s in self.segments
            if s.status == 'confirmed' and s.reason == 'first_case_no_gap'
        )

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
    compared without trusting a label.  The digest names the *series*: the
    symbol/adjustment in :class:`SegmentBinding` are caller declarations that the
    kernel cannot verify, so they are deliberately not folded in here and
    :meth:`SegmentSnapshot.same_source` requires the binding to match too.
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
    """Merge two contained elements in ``direction`` (the K-line rule).

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


def _trend(previous: CharacteristicElement, current: CharacteristicElement) -> _Direction | None:
    """Direction of the move ``previous -> current``; ``None`` when undefined.

    Only a *strict* two-extrema move defines a merge direction.  An equal high or
    equal low means the pair is (still) a flat/ambiguous step, and defaulting it
    would invent a direction that nothing in the sequence supports.
    """
    if current.high > previous.high and current.low > previous.low:
        return 'up'
    if current.high < previous.high and current.low < previous.low:
        return 'down'
    return None


class _CharacteristicNormalizer:
    """The single 包含处理 implementation.

    Both the segment walk (on real characteristic pens) and the public
    :func:`normalize_characteristic_sequence` (on bare ranges) push elements
    through *this* object, so an audit of the helper audits production and vice
    versa: there is no second copy of the merge rule to drift.

    The merge direction of a contained pair is the trend of the two most recent
    already-normalised, non-contained elements — the sequence's own direction, as
    for K-lines.  When that trend does not exist (first pair) or is not defined
    (equal high/low), the normaliser blocks instead of picking a side.
    """

    __slots__ = ('elements', 'merges', 'blocked')

    def __init__(self) -> None:
        self.elements: list[CharacteristicElement] = []
        self.merges = 0
        self.blocked: str | None = None

    def push(self, element: CharacteristicElement) -> str | None:
        """Add one element; returns a blocked reason if the sequence is undefined."""
        if self.blocked is not None:  # pragma: no cover - callers stop on a block
            raise ValueError(f'characteristic sequence is already blocked: {self.blocked}')
        self.elements.append(element)
        while len(self.elements) >= 2 and _contains(self.elements[-2], self.elements[-1]):
            direction = self._merge_direction()
            if direction is None:
                if len(self.elements) == 2:
                    self.blocked = 'characteristic_initial_containment'
                else:  # pragma: no cover - a normalised pair is always strictly ordered
                    self.blocked = 'characteristic_merge_direction_unknown'
                return self.blocked
            self.elements[-2:] = [_merge_elements(self.elements[-2], self.elements[-1], direction)]
            self.merges += 1
        return None

    def _merge_direction(self) -> _Direction | None:
        """Trend leading into the contained pair: ``elements[-3] -> elements[-2]``.

        ``elements[-1]`` is the element that is contained by ``elements[-2]``, so
        the move that must decide the merge is the one that produced
        ``elements[-2]``: that is exactly the K-line rule ("包含关系之前的两根").
        With fewer than three elements there is no such move.
        """
        if len(self.elements) < 3:
            return None
        return _trend(self.elements[-3], self.elements[-2])

    @property
    def ranges(self) -> tuple[tuple[float, float], ...]:
        return tuple((element.high, element.low) for element in self.elements)


def normalize_characteristic_sequence(
    ranges: Sequence[tuple[float, float]],
) -> tuple[tuple[tuple[float, float], ...], str | None]:
    """Normalise characteristic-sequence ``(high, low)`` ranges.

    Exposed for audit: it drives the very same :class:`_CharacteristicNormalizer`
    the segment walk uses, so this is the production 包含处理, not a parallel
    reimplementation.  The merge is sequential and non-transitive (each merge
    compares with the already-normalised neighbour), and its direction is read
    from the sequence itself (see the module docstring), never from the segment
    direction.  Returns the normalised ranges plus a blocked reason when the
    direction is undefined: ``characteristic_initial_containment`` when the first
    pair is contained, ``characteristic_merge_direction_unknown`` when the pair
    before a contained one is ambiguous.
    """
    normalizer = _CharacteristicNormalizer()
    for index, (high, low) in enumerate(ranges):
        if not (isfinite(high) and isfinite(low)) or high < low:
            raise ValueError(f'invalid characteristic range {(high, low)!r} at {index}')
        blocked = normalizer.push(
            CharacteristicElement(
                high=float(high),
                low=float(low),
                first_pen_index=index,
                last_pen_index=index,
                pen_indices=(index,),
                confirmed_on=None,
            )
        )
        if blocked is not None:
            break
    return normalizer.ranges, normalizer.blocked


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


def _fractal_end_pen(
    pens, middle: CharacteristicElement, direction: _Direction
) -> int | None:
    """The pen that carries the fractal's own extreme (第67课 高点/低点).

    The middle element's extreme side is the *start* price of one of the reverse
    pens it absorbed (a down pen's high, an up pen's low), and the segment's end
    is the opposite-direction pen immediately before it — its end price is that
    extreme.  On an equal extreme the **latest** carrier wins
    (``equal_fractal_extreme='keep_latest'``).
    """
    side = middle.high if direction == 'up' else middle.low
    carriers = [
        pen_index - 1
        for pen_index in middle.pen_indices
        if pen_index > 0 and pens[pen_index].start.price == side
    ]
    return max(carriers) if carriers else None


def _check_iso_day(value: str, what: str) -> None:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f'{what} {value!r} is not an ISO day (YYYY-MM-DD)') from None


def _validate_pens(snapshot: PenSnapshot, rules: SegmentRules) -> None:
    """Refuse an inconsistent pen series instead of repairing it."""
    if snapshot.kernel_version != PEN_KERNEL_VERSION:
        raise ValueError(
            f'pen snapshot kernel {snapshot.kernel_version!r} is not '
            f'{PEN_KERNEL_VERSION!r}; segments v2 only read that pen rule set'
        )
    as_of = snapshot.as_of_day
    if as_of is not None:
        _check_iso_day(as_of, 'as_of_day')
    previous = None
    previous_confirm: str | None = None
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
        gap = pen.end.merged_index - pen.start.merged_index
        if gap < rules.min_index_gap:
            raise ValueError(
                f'pen {index} spans {gap} merged indices, not the {rules.min_index_gap} '
                'the pen kernel requires; a pen shorter than that is not a leg'
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
        _check_iso_day(pen.start.position_day, f'pen {index} start position day')
        _check_iso_day(pen.end.position_day, f'pen {index} end position day')
        if pen.end.position_day <= pen.start.position_day:
            raise ValueError(
                f'pen {index} ends on {pen.end.position_day}, not after its start '
                f'{pen.start.position_day}; a leg must move forward in time'
            )
        if as_of is not None and pen.end.position_day > as_of:
            raise ValueError(
                f'pen {index} ends on {pen.end.position_day}, after as_of_day {as_of}'
            )
        if pen.status == 'confirmed':
            if pen.confirmed_on is None:
                raise ValueError(f'pen {index} is confirmed without a confirmation day')
            if pen.end.confirmed_on is None:
                raise ValueError(
                    f'confirmed pen {index} has no confirmed end endpoint; a frozen pen '
                    'always carries the day its end froze'
                )
            _check_iso_day(pen.confirmed_on, f'pen {index} confirmation day')
            if pen.end.status != 'confirmed':
                raise ValueError(
                    f'pen {index} is confirmed but its end endpoint is '
                    f'{pen.end.status!r}'
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
            if previous_confirm is not None and pen.confirmed_on < previous_confirm:
                raise ValueError(
                    f'pen {index} is confirmed on {pen.confirmed_on}, before the previous '
                    f'pen froze ({previous_confirm}); a prefix never freezes out of order'
                )
            previous_confirm = pen.confirmed_on
        else:
            if pen.confirmed_on is not None:
                raise ValueError(f'pen {index} is {pen.status} but carries a confirmation day')
            if pen.end.status == 'confirmed':
                raise ValueError(
                    f'pen {index} is {pen.status} but its end endpoint claims to be frozen'
                )
            if pen.status == 'candidate' and index != last:
                raise ValueError(
                    f'pen {index} is a candidate but is not the tail; an open pen can only '
                    'be the last one of a prefix'
                )
        for endpoint in (pen.start, pen.end):
            _check_iso_day(endpoint.position_day, f'pen {index} endpoint day')
            if endpoint.confirmed_on is not None:
                _check_iso_day(endpoint.confirmed_on, f'pen {index} endpoint confirmation')
                if as_of is not None and endpoint.confirmed_on > as_of:
                    raise ValueError(
                        f'pen {index} endpoint is confirmed on {endpoint.confirmed_on}, after '
                        f'as_of_day {as_of}'
                    )
                if endpoint.confirmed_on < endpoint.position_day:
                    raise ValueError(
                        f'pen {index} endpoint is confirmed on {endpoint.confirmed_on}, before '
                        f'its own position day {endpoint.position_day}'
                    )
                if endpoint.status != 'confirmed':
                    raise ValueError(
                        f'pen {index} endpoint is {endpoint.status} but carries a '
                        'confirmation day'
                    )
            elif endpoint.status == 'confirmed':
                raise ValueError(
                    f'pen {index} endpoint is confirmed without a confirmation day'
                )
        if previous is not None:
            if pen.start.merged_index != previous.end.merged_index:
                raise ValueError(
                    f'pen {index} starts at merged index {pen.start.merged_index} but pen '
                    f'{index - 1} ended at {previous.end.merged_index}; pens must share endpoints'
                )
            if pen.start.kind != previous.end.kind:
                raise ValueError(
                    f'pen {index} starts at a {pen.start.kind} but pen {index - 1} ended at a '
                    f'{previous.end.kind}; a shared endpoint has one identity'
                )
            if pen.start.position_day != previous.end.position_day or pen.start.price != previous.end.price:
                raise ValueError(
                    f'pen {index} start ({pen.start.position_day}@{pen.start.price}) does not '
                    f'meet pen {index - 1} end ({previous.end.position_day}@{previous.end.price})'
                )
            if pen.direction == previous.direction:  # pragma: no cover - kinds alternate above
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

    def pending(status, reason, detail='', end_pen=None, blocked_at=None) -> SegmentPending:
        return SegmentPending(
            pen_start_index=start,
            direction=direction,
            status=status,
            reason=reason,
            start_position_day=first.start.position_day,
            start_price=first.start.price,
            confirmed_pens=sum(1 for p in trio if p.status == 'confirmed'),
            detail=detail,
            end_pen_index=end_pen,
            blocked_at_pen_index=blocked_at,
        )

    if len(trio) < rules.min_pens:
        return pending('candidate', 'awaiting_three_pens'), ()

    for pen_index, pen in enumerate(trio, start=start):
        if pen.status == 'blocked':
            return (
                pending('blocked', 'tainted_pen', f'pen {pen_index} is blocked', end_pen=pen_index),
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
            pending('blocked', 'first_three_pens_no_overlap', detail, end_pen=start + 2),
            (_rejection(trio[-1], start + 2, 'first_three_pens_no_overlap', detail),),
        )

    best_pen = start  # pen whose end is the current extreme of this segment
    revisions = 0
    drift_days: tuple[str, ...] = ()
    normalizer = _CharacteristicNormalizer()
    rejections: list[SegmentRejection] = []

    def close(
        status,
        reason,
        confirmed_on=None,
        *,
        end_pen=None,
        end_status='candidate',
        evidence: tuple[FractalEvidence, ...] = (),
        blocked_at: int | None = None,
        detail: str = '',
    ):
        """Publish a segment, or a pending refusal when the span cannot be one.

        A span shorter than :data:`MIN_PENS` is never turned into a
        :class:`Segment`: one pen is not a 线段, and an internal exception would
        hide a legal input behind a crash.

        ``blocked_at`` is passed for refusals that are a *decision at an evidence
        pen* (undefined 包含处理 direction).  When no line can be published
        (fewer than :data:`MIN_PENS` pens behind the decision) the refusal is a
        pending that names that pen in ``blocked_at_pen_index`` and claims no
        span — instead of pointing ``end_pen_index`` at the chain start and
        claiming a one-pen decision that never happened.  With three or more pens
        behind it the refusal is still a ``blocked`` :class:`Segment` covering
        ``start..best_pen``, exactly as before (audit only, never a line).
        """
        pen_index = best_pen if end_pen is None else end_pen
        used = pen_index - start + 1
        if blocked_at is not None and used < rules.min_pens:
            return pending(
                'blocked',
                reason,
                detail or f'blocked at pen {blocked_at}',
                end_pen=None,
                blocked_at=blocked_at,
            )
        if used < rules.min_pens:
            verdict = 'blocked' if status == 'blocked' else 'candidate'
            return pending(
                verdict,
                reason,
                f'the decision point covers {used} pen(s), fewer than the {rules.min_pens} '
                'a segment needs; reported as a pending start, not a line',
                end_pen=pen_index,
            )
        end_pen_obj = pens[pen_index]
        end_ep = _segment_endpoint(
            end_pen_obj,
            pen_index,
            end_pen_obj.end,
            status=end_status,
            confirmed_on=end_pen_obj.end.confirmed_on if end_status == 'confirmed' else None,
            revisions=revisions,
            drift_days=drift_days,
        )
        return Segment(
            start=start_ep,
            end=end_ep,
            direction=direction,
            pen_start_index=start,
            pen_end_index=pen_index,
            status=status,
            reason=reason,
            confirmed_on=confirmed_on,
            characteristic_elements=len(normalizer.elements),
            kernel_version=rules.kernel_version,
            evidence=evidence,
        )

    def evidence_of(left, middle, right) -> tuple[FractalEvidence, ...]:
        return tuple(
            FractalEvidence(
                role=role,
                pen_indices=element.pen_indices,
                high=element.high,
                low=element.low,
                confirmed_on=element.confirmed_on,
            )
            for role, element in (('left', left), ('middle', middle), ('right', right))
        )

    for pen_index in range(start + 1, len(pens), 2):
        pen = pens[pen_index]
        previous_same = pens[pen_index - 1]

        if pen.status == 'blocked' or previous_same.status == 'blocked':
            bad_index = pen_index if pen.status == 'blocked' else pen_index - 1
            rejections.append(_rejection(pens[bad_index], bad_index, 'tainted_pen'))
            return close('blocked', 'tainted_pen', end_pen=best_pen), tuple(rejections)

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
            return close('candidate', 'awaiting_confirmed_characteristic_pen'), tuple(rejections)

        before = len(normalizer.elements)
        blocked = normalizer.push(
            CharacteristicElement(
                high=max(pen.start.price, pen.end.price),
                low=min(pen.start.price, pen.end.price),
                first_pen_index=pen_index,
                last_pen_index=pen_index,
                pen_indices=(pen_index,),
                confirmed_on=pen.confirmed_on,
            )
        )
        if blocked is not None:
            # The refusal is a decision *at this pen*: report the evidence pen and
            # the element that contains it, never a would-be end or a pen count.
            container = normalizer.elements[-2] if len(normalizer.elements) >= 2 else None
            container_pen = container.first_pen_index if container is not None else pen_index
            if blocked == 'characteristic_initial_containment':
                detail = (
                    f'pen {pen_index} is contained by the first characteristic element '
                    f'(pen {container_pen}) and no earlier element pair exists, so the '
                    '包含处理 direction is undefined; nothing is merged or guessed'
                )
            else:
                detail = (
                    f'pen {pen_index} is contained by pen {container_pen}, but the pair '
                    'before it defines no direction (equal high/low); nothing is merged'
                )
            rejections.append(
                _rejection(
                    pen,
                    pen_index,
                    blocked,  # type: ignore[arg-type]
                    'the merge direction is undefined here; the sequence is not normalised '
                    'by guessing one',
                )
            )
            return (
                close('blocked', blocked, blocked_at=pen_index, detail=detail),
                tuple(rejections),
            )  # type: ignore[arg-type]

        if len(normalizer.elements) <= before:
            continue  # this element was absorbed: defer the fractal check
        if len(normalizer.elements) < 3:
            continue

        elements = normalizer.elements
        left, middle, right = elements[-3], elements[-2], elements[-1]
        if not _terminal_fractal(left, middle, right, direction):
            continue

        middle_price = middle.high if direction == 'up' else middle.low
        # The segment's extreme can only come from a same-direction pen up to the
        # last element; anything more extreme means this fractal is not the top of
        # this segment and using it would silently move the high/low.
        span_price = (
            max(pens[j].end.price for j in range(start, pen_index, 2))
            if direction == 'up'
            else min(pens[j].end.price for j in range(start, pen_index, 2))
        )
        end_pen = _fractal_end_pen(pens, middle, direction)
        evidence = evidence_of(left, middle, right)
        if _more_extreme(span_price, middle_price, direction):
            rejections.append(
                _rejection(
                    pens[best_pen],
                    best_pen,
                    'characteristic_fractal_below_prior_extreme',
                    f'the terminal fractal is at {middle_price} but the extreme so far is '
                    f'{span_price}; using it would move the segment end',
                )
            )
            return (
                close(
                    'blocked',
                    'characteristic_fractal_below_prior_extreme',
                    end_pen=end_pen if end_pen is not None else best_pen,
                    evidence=evidence,
                ),
                tuple(rejections),
            )
        if end_pen is None:  # pragma: no cover - an element extreme is always a pen end
            return (
                close('blocked', 'fractal_extreme_pen_missing', end_pen=best_pen, evidence=evidence),
                tuple(rejections),
            )
        if _has_gap(left, middle, direction):
            rejections.append(
                _rejection(
                    pens[end_pen],
                    end_pen,
                    'characteristic_gap_case2',
                    '第67课 第二种情况 (gap) needs the second characteristic sequence; '
                    'not implemented',
                )
            )
            return (
                close('blocked', 'characteristic_gap_case2', end_pen=end_pen, evidence=evidence),
                tuple(rejections),
            )

        confirmed_days = [element.confirmed_on for element in (left, middle, right)]
        if any(day is None for day in confirmed_days):  # pragma: no cover - elements are confirmed
            return (
                close('blocked', 'evidence_inconsistent', end_pen=end_pen, evidence=evidence),
                tuple(rejections),
            )
        confirmed_on = max(confirmed_days)  # type: ignore[arg-type]
        if confirmed_on <= pens[end_pen].end.position_day:  # pragma: no cover - guarded above
            # The fractal's third element froze before the pen it certifies even
            # existed: the evidence and the geometry contradict each other.
            return (
                close('blocked', 'evidence_inconsistent', end_pen=end_pen, evidence=evidence),
                tuple(rejections),
            )
        if any(p.status != 'confirmed' for p in pens[start : end_pen + 1]):  # pragma: no cover
            # Only the tail pen may be a candidate, and the fractal is decided on
            # confirmed elements, so the span is always confirmed evidence.
            return (
                close('blocked', 'evidence_inconsistent', end_pen=end_pen, evidence=evidence),
                tuple(rejections),
            )
        return (
            close(
                'confirmed',
                'first_case_no_gap',
                confirmed_on,
                end_pen=end_pen,
                end_status='confirmed',
                evidence=evidence,
            ),
            tuple(rejections),
        )

    return close('candidate', 'awaiting_confirmed_characteristic_pen'), tuple(rejections)


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
    _validate_pens(pen_snapshot, rules)

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
