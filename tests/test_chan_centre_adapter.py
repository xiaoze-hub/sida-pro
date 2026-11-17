"""Causal safety of the segment -> operational-centre adapter.

The adapter is a bridge, so most of what can go wrong is a *quiet* mix-up: a
candidate line used as geometry, evidence pens folded into a range, a centre
read a day too early, a 复权 label dropped, a blocked refusal papered over with
a guessed line.  These tests attack exactly those points.

What is pinned:

* the input is **one** ``SegmentSnapshot`` plus the pen snapshot that produced it
  (the digest must match).  Only the contiguous leading run of
  ``confirmed_segments`` becomes :class:`ExternalSegment`; ``candidate`` and
  ``blocked`` entries never do;
* the range of each segment is the **merged-bar interval** (``bar_low``/
  ``bar_high``) of every pen endpoint inside ``pen_start_index..pen_end_index`` —
  **not** the pivot prices (a wick beyond the pivot is real price action, and
  adapter v1 dropping it put ``zd``/``zg`` inside the K-line range) and **not**
  the evidence pens (they reach past the end and would double count);
* the v2 range convention is part of ``snapshot_version``
  (``range=pen_span_endpoint_merged_bar``), so a v1 pivot-price zone can never
  be mixed with a v2 merged-bar zone;
* ``confirmed_on`` is copied from the segment kernel and re-verified against the
  evidence's latest confirmation day, both endpoint confirmation days and the
  geometric end day; nothing in the adapter mints a timestamp;
* ``visible_on`` is the **formation** day (the third segment's confirmation
  day) and never moves while the centre stands; ``state_changed_on`` is the
  later evidence day of the lifecycle state, a separate clock;
* fewer than three confirmed segments is reported as
  ``not_enough_confirmed_segments`` — explicitly *not* "no centre" — and the
  centre kernel is not even called (checked with a monkeypatched spy);
* a ``blocked`` refusal before three confirmed lines yields
  ``blocked_before_centre`` with the kernel's own reason; a refusal *after* a
  confirmed centre does not retract or move that centre, it only makes the
  result non-``terminal`` — a prefix never contradicts an earlier prefix;
* ``terminal`` is strict: a 0-segment snapshot, an open candidate tail and a
  blocking refusal are all non-terminal, because none of them is a closed
  answer;
* three non-overlapping (or merely touching) ranges give ``no_centre`` with the
  centre kernel's own ``zero_width_intersection`` label: a measured absence;
* prefix replay over every bar prefix shows the centre appearing exactly on its
  evidence day, never earlier, and never changing once formed;
* the adjustment base is part of the source identity (``unknown`` included) and
  two bases can never be mixed into one centre;
* the reverse (price-mirrored) series produces the mirrored centre, so no rule
  is accidentally one-sided;
* every contradiction — doctored confirmation day, wrong span, flipped
  direction, digest mismatch, a confirmed line after a blocked one — raises
  instead of being repaired.

The bar fixtures are real ``bar -> fractal -> pen -> segment`` runs (the pen
kernel's 4-merged-index spacing and the segment kernel's 第67课 walk are what
produce the numbers), not hand-written segment objects.
"""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.core.chan_centre_adapter import (
    ADAPTER_VERSION,
    BASIS,
    INPUT_KIND,
    LIMITATIONS,
    RANGE_BASIS,
    STRICTNESS,
    CentreSource,
    centre_from_segment_snapshot,
)
from src.core.chan_operational_centre import (
    CENTRE_KERNEL_VERSION,
    SEGMENTS_ENGINE,
    centre_from_chain,
)
from src.core.chan_pen import (
    PEN_KERNEL_VERSION,
    Pen,
    PenEndpoint,
    PenRules,
    PenSnapshot,
    build_chan_pen,
)
from src.core.chan_segment import (
    FractalEvidence,
    MIN_PENS,
    SEGMENT_KERNEL_VERSION,
    Segment,
    SegmentBinding,
    SegmentEndpoint,
    segment_from_pens,
    segment_input_digest,
)
from src.core.chan_structure import RawBar

_SYMBOL = 'SZ.300000'
_START = date(2026, 1, 1)          # real-bar fixtures
_PEN_START = date(2026, 3, 2)      # explicit pen fixtures


def _d(index: int, start: date = _START) -> str:
    return (start + timedelta(days=index)).isoformat()


# ---------------------------------------------------------------------------
# real bars: every pivot becomes its own merged bar, so merged index == raw index
# ---------------------------------------------------------------------------

def zigzag_bars(pivots, *, step: int = 4, half: float = 0.5) -> list[RawBar]:
    mids = [float(pivots[0]) + 1.0, float(pivots[0])]
    for a, b in zip(pivots, pivots[1:]):
        for k in range(1, step + 1):
            mids.append(round(a + (b - a) * k / step, 6))
    return [
        RawBar(day=_d(i), open=m, high=round(m + half, 6), low=round(m - half, 6), close=m)
        for i, m in enumerate(mids)
    ]


# The pinned series: three confirmed segments and a fourth the kernel *blocks*
# (characteristic_initial_containment).  Its leading-three intersection is
# [20.92, 24.96], formed on the third segment's confirmation day 2026-03-05.
CENTRE_PIVOTS = [
    20.0, 22.72, 21.67, 24.32, 22.98, 24.46, 22.15, 24.44, 21.42, 23.61,
    22.68, 25.45, 23.4, 25.26, 23.12, 26.41, 23.45, 27.62, 25.35,
]
CENTRE_BARS = zigzag_bars(CENTRE_PIVOTS)

# The v1 (pivot-price) no-centre fixture.  Under the v2 merged-bar convention it
# is a **centre**: the wick of the first line reaches 19 while the last one
# reaches 20.58, so zd/zg are [19, 20.58] instead of the pivot-only [20, 19.58].
# Kept as the regression for the convention change, not as a no-centre case.
NO_CENTRE_PIVOTS = [
    20.0, 20.97, 20.45, 23.95, 20.55, 21.13, 17.52, 19.24, 15.88, 18.96,
    15.96, 19.58, 18.68, 19.2, 15.41,
]

# A v2 no-centre fixture: even with every endpoint bar's wick included, the
# first line [86, 99] and the third [48, 72] stay apart (max low 86 > min high
# 72).  Segments span pens 0..2, 3..7, 8..10; a pending candidate follows.
NO_CENTRE_PIVOTS_V2 = [
    93, 97, 87, 98, 93, 96, 63, 65, 49, 64, 51, 71, 58, 68, 47,
]

# The same three lines followed by a blocked fourth segment (第67课 第二种情况,
# not implemented): a measured no-centre whose structure is incomplete.
NO_CENTRE_BLOCKED_PIVOTS = NO_CENTRE_PIVOTS_V2 + [55, 50, 60, 54]

# A confirmed up segment whose **interior** low (18) is below its start (20) and
# whose evidence pen 7 (down to 17) reaches past its end.  Endpoint-only ranges
# would say [20, 26] and folding evidence in would say [17, 26]; both are wrong.
INTERIOR_PIVOTS = [20, 24, 18, 25, 21, 26, 23, 25.5, 17]

# The first two characteristic pens are contained and there is no earlier pair,
# so the segment kernel blocks at pen 3 (initial containment): no line at all.
BLOCKED_PIVOTS = [20, 24, 20, 23, 21, 24.5, 22, 25]


def mirror_bars(bars, axis: float = 60.0) -> list[RawBar]:
    return [
        RawBar(
            day=b.day,
            open=round(axis - b.open, 6),
            high=round(axis - b.low, 6),
            low=round(axis - b.high, 6),
            close=round(axis - b.close, 6),
        )
        for b in bars
    ]


def real_chain(bars, *, adjust: str = 'none', symbol: str = _SYMBOL):
    pen = build_chan_pen(bars)
    segment = segment_from_pens(
        pen, binding=SegmentBinding(symbol=symbol, adjust=adjust, as_of_day=pen.as_of_day)
    )
    return pen, segment


def adapt(segment, pen, **kwargs):
    return centre_from_segment_snapshot(segment, pen_snapshot=pen, **kwargs)


# ---------------------------------------------------------------------------
# explicit pens: the segment kernel's documented input contract
# ---------------------------------------------------------------------------

def _point_endpoint(kind, index, day, price, confirmed_on):
    return PenEndpoint(
        kind=kind,
        merged_index=4 * index,
        position_day=day,
        price=price,
        bar_high=price + 1.0,
        bar_low=price - 1.0,
        revisions=0,
        drift_days=(),
        status='confirmed' if confirmed_on else 'candidate',
        confirmed_on=confirmed_on,
    )


def make_pens(prices) -> PenSnapshot:
    """Bottom/top alternating pen series, one pen per pivot pair, all frozen."""
    kinds = ['bottom' if i % 2 == 0 else 'top' for i in range(len(prices))]
    points = [
        (kinds[i], _d(4 * i, _PEN_START), float(prices[i])) for i in range(len(prices))
    ]
    pens = []
    for k in range(len(points) - 1):
        (ka, da, pa), (kb, db, pb) = points[k], points[k + 1]
        frozen_on = (date.fromisoformat(points[k + 1][1]) + timedelta(days=1)).isoformat()
        start_on = pens[-1].end.confirmed_on if pens else frozen_on
        pens.append(
            Pen(
                start=_point_endpoint(ka, k, da, pa, start_on),
                end=_point_endpoint(kb, k + 1, db, pb, frozen_on),
                direction='up' if ka == 'bottom' else 'down',
                status='confirmed',
                confirmed_on=frozen_on,
                kernel_version=PEN_KERNEL_VERSION,
            )
        )
    return PenSnapshot(
        as_of_day=max(p.confirmed_on for p in pens),
        kernel_version=PEN_KERNEL_VERSION,
        rules=PenRules(),
        endpoints=(),
        pens=tuple(pens),
        rejections=(),
        blocked_fractals=(),
        taint_indices=(),
    )


def empty_pens() -> PenSnapshot:
    return PenSnapshot(
        as_of_day=None,
        kernel_version=PEN_KERNEL_VERSION,
        rules=PenRules(),
        endpoints=(),
        pens=(),
        rejections=(),
        blocked_fractals=(),
        taint_indices=(),
    )


def explicit_chain(prices, *, adjust: str = 'none'):
    pen = make_pens(prices)
    segment = segment_from_pens(
        pen, binding=SegmentBinding(symbol=_SYMBOL, adjust=adjust, as_of_day=pen.as_of_day)
    )
    return pen, segment


# ---------------------------------------------------------------------------
# the fixture itself, pinned before any adapter claim
# ---------------------------------------------------------------------------

def test_the_real_fixture_is_pinned_before_any_adapter_claim():
    pen, segment = real_chain(CENTRE_BARS)
    assert len(pen.pens) == 17
    assert [
        (
            s.pen_start_index,
            s.pen_end_index,
            s.direction,
            s.start.price,
            s.end.price,
            s.start.position_day,
            s.end.position_day,
            s.confirmed_on,
            s.status,
            s.reason,
        )
        for s in segment.segments
    ] == [
        (0, 4, 'up', 19.5, 24.96, '2026-01-02', '2026-01-22', '2026-02-09', 'confirmed', 'first_case_no_gap'),
        (5, 7, 'down', 24.96, 20.92, '2026-01-22', '2026-02-03', '2026-02-21', 'confirmed', 'first_case_no_gap'),
        (8, 10, 'up', 20.92, 25.95, '2026-02-03', '2026-02-15', '2026-03-05', 'confirmed', 'first_case_no_gap'),
        (11, 13, 'down', 25.95, 22.62, '2026-02-15', '2026-02-27', None, 'blocked', 'characteristic_initial_containment'),
    ]
    assert segment.confirmed_segments == segment.segments[:3]


def test_explicit_pen_fixtures_are_what_the_adapter_tests_say():
    interior_pen, interior = explicit_chain(INTERIOR_PIVOTS)
    first = interior.confirmed_segments[0]
    assert (first.pen_start_index, first.pen_end_index) == (0, 4)
    assert (first.start.price, first.end.price) == (20.0, 26.0)
    assert first.evidence_pen_indices == (3, 5, 7)
    assert interior_pen.pens[1].end.price == 18.0  # interior low, below the start

    blocked_pen, blocked = explicit_chain(BLOCKED_PIVOTS)
    assert blocked.segments == ()
    assert [(p.status, p.reason, p.blocked_at_pen_index) for p in blocked.pending] == [
        ('blocked', 'characteristic_initial_containment', 3)
    ]

    _, flat = explicit_chain(NO_CENTRE_PIVOTS)
    assert len(flat.confirmed_segments) == 3


# ---------------------------------------------------------------------------
# what enters: the confirmed prefix, with real days and real anchors
# ---------------------------------------------------------------------------

def test_confirmed_prefix_becomes_bound_external_segments():
    pen, segment = real_chain(CENTRE_BARS)
    result = adapt(segment, pen)

    assert result.status == 'centre'
    assert result.total_segments == 4
    assert result.confirmed_prefix_length == 3
    assert [s.index for s in result.segments] == [0, 1, 2]
    assert [(s.direction, s.start_day, s.start_price, s.end_day, s.end_price) for s in result.segments] == [
        ('up', '2026-01-02', 19.5, '2026-01-22', 24.96),
        ('down', '2026-01-22', 24.96, '2026-02-03', 20.92),
        ('up', '2026-02-03', 20.92, '2026-02-15', 25.95),
    ]
    assert [(s.high, s.low) for s in result.segments] == [
        (24.96, 19.5),
        (24.96, 20.92),
        (25.95, 20.92),
    ]
    # Anchors are shared exactly and directions alternate: a reproducible chain.
    for previous, current in zip(result.segments, result.segments[1:]):
        assert (current.start_day, current.start_price) == (previous.end_day, previous.end_price)
        assert current.direction != previous.direction

    # confirmed_on is the kernel's own day, never minted here, and always after
    # the geometry it certifies.
    assert [s.confirmed_on for s in result.segments] == ['2026-02-09', '2026-02-21', '2026-03-05']
    for external, source in zip(result.segments, segment.confirmed_segments):
        assert external.confirmed_on == source.confirmed_on == source.evidence_confirmed_on
        assert external.confirmed_on > external.end_day
        assert external.status == 'confirmed'


def test_the_chain_is_bound_to_one_sample():
    pen, segment = real_chain(CENTRE_BARS, adjust='qfq')
    result = adapt(segment, pen)

    assert result.source.pen_digest == segment.input_digest == segment_input_digest(pen)
    assert result.source.pen_kernel == PEN_KERNEL_VERSION
    assert result.source.segment_kernel == SEGMENT_KERNEL_VERSION
    assert result.source.adjust == 'qfq'
    assert result.source.as_of_day == pen.as_of_day == segment.as_of_day
    assert result.chain.instrument == _SYMBOL
    assert result.chain.as_of_day == pen.as_of_day
    assert result.chain.snapshot_version == (
        f'chan-segment-v2|adjust=qfq|range={RANGE_BASIS}|pens={segment.input_digest}'
    )
    assert result.chain.basis == BASIS


# ---------------------------------------------------------------------------
# the centre: geometry, clocks, labels
# ---------------------------------------------------------------------------

def test_centre_geometry_and_two_clocks():
    pen, segment = real_chain(CENTRE_BARS)
    result = adapt(segment, pen)
    centre = result.centre

    assert (centre.zd, centre.zg) == (20.92, 24.96)
    assert centre.width == pytest.approx(4.04)
    assert centre.segment_indices == (0, 1, 2)
    assert centre.structure_start_day == '2026-01-02'      # raw time, not a confirmation
    assert centre.structure_start_price == 19.5
    assert centre.confirmed_on == '2026-03-05'             # third segment's evidence day
    assert result.visible_on == '2026-03-05'
    assert centre.confirmed_on != centre.structure_start_day
    assert result.lifecycle_status == 'formed'
    assert result.centre_snapshot.formation_confirmed is True
    assert result.centre_snapshot.segment_count == 3
    assert result.centre_snapshot.kernel_version == CENTRE_KERNEL_VERSION


def test_labels_say_operational_segment_a0_and_never_strict():
    pen, segment = real_chain(CENTRE_BARS)
    result = adapt(segment, pen)

    assert (result.basis, result.strictness) == (BASIS, STRICTNESS) == ('segment_a0', 'operational_not_strict')
    assert result.adapter_version == ADAPTER_VERSION == 'chan-centre-adapter-v2'
    assert result.input_kind == INPUT_KIND == 'segment_kernel_confirmed_prefix'
    assert result.segments_engine == SEGMENTS_ENGINE == 'none'
    assert result.centre_input_kind == 'externally_supplied_completed_segments'
    assert result.centre_snapshot.basis == 'segment_a0'
    assert result.centre_snapshot.strictness == 'operational_not_strict'
    assert 'case2_gap_segments_not_implemented_upstream' in result.limitations
    assert 'characteristic_initial_containment_blocks_upstream' in result.limitations
    assert 'no_buy_sell_points_no_trading_signal' in result.limitations
    assert set(LIMITATIONS) <= set(result.limitations)

    text = result.describe()
    assert 'operational_not_strict' in text
    assert 'not a strict' in text
    assert 'INCOMPLETE' in text
    assert 'characteristic_initial_containment' in text


# ---------------------------------------------------------------------------
# blocked / candidate: excluded, reported, never geometry
# ---------------------------------------------------------------------------

def test_a_blocked_segment_is_never_geometry_but_never_hidden():
    pen, segment = real_chain(CENTRE_BARS)
    result = adapt(segment, pen)

    assert result.confirmed_prefix_length == 3
    assert result.total_segments == 4
    blocked = segment.segments[3]
    assert blocked.status == 'blocked'
    # its pen span (11..13) does not appear in any external segment
    assert all(not (s.index == 3) for s in result.segments)
    assert [i.reason for i in result.issues if i.blocking] == ['characteristic_initial_containment']
    issue = next(i for i in result.issues if i.blocking)
    assert issue.pen_index == 11 and issue.segment_index == 3
    assert result.blocked_reasons == ('characteristic_initial_containment',)
    assert result.terminal is False
    # and the centre was still built: the refusal is *after* it, not in it
    assert result.status == 'centre'
    assert result.centre.segment_indices == (0, 1, 2)


def test_a_candidate_tail_is_excluded_without_being_a_block():
    pen_all, segment_all = real_chain(CENTRE_BARS)
    pen, segment = real_chain(CENTRE_BARS[:64])
    assert segment.segments[-1].status == 'candidate'
    result = adapt(segment, pen)

    assert result.status == 'centre'
    assert result.confirmed_prefix_length == 3
    assert result.terminal is False      # an open candidate tail is not closed
    assert [i.reason for i in result.issues] == ['candidate_segment_excluded']
    assert all(not i.blocking for i in result.issues)
    assert [s.end_day for s in result.segments] == ['2026-01-22', '2026-02-03', '2026-02-15']
    assert segment.segments[-1].pen_start_index == 11  # candidate pens 11..13 absent


def test_a_blocked_tail_never_retracts_or_moves_a_confirmed_centre():
    pen_early, segment_early = real_chain(CENTRE_BARS[:64])   # 3 confirmed + candidate
    pen_late, segment_late = real_chain(CENTRE_BARS)          # 3 confirmed + blocked

    early = adapt(segment_early, pen_early)
    late = adapt(segment_late, pen_late)

    assert early.status == late.status == 'centre'
    assert early.segments == late.segments                     # no back-fill, no drift
    assert (early.centre.zd, early.centre.zg, early.centre.segment_indices) == (
        late.centre.zd,
        late.centre.zg,
        late.centre.segment_indices,
    )
    assert early.centre.confirmed_on == late.centre.confirmed_on == early.visible_on == late.visible_on
    assert (early.centre.zd, early.centre.zg, early.centre.confirmed_on) == (
        20.92,
        24.96,
        '2026-03-05',
    )
    assert early.terminal is False and late.terminal is False


# ---------------------------------------------------------------------------
# causality over every prefix
# ---------------------------------------------------------------------------

def test_prefix_replay_never_shows_the_centre_early_and_never_moves_it():
    final_pen, final_segment = real_chain(CENTRE_BARS)
    final = adapt(final_segment, final_pen)
    seen: dict[int, tuple] = {}
    last_prefix = -1
    first_centre_as_of = None

    for length in range(2, len(CENTRE_BARS) + 1):
        pen = build_chan_pen(CENTRE_BARS[:length])
        segment = segment_from_pens(
            pen, binding=SegmentBinding(symbol=_SYMBOL, adjust='none', as_of_day=pen.as_of_day)
        )
        result = adapt(segment, pen)

        # the confirmed prefix only ever grows, never shrinks (no retraction)
        assert result.confirmed_prefix_length >= last_prefix
        last_prefix = result.confirmed_prefix_length
        seen[length] = result.segments

        # geometry that entered a prefix still describes the same pens later
        assert result.segments == final.segments[: len(result.segments)]

        if result.status == 'centre':
            assert result.visible_on <= result.source.as_of_day   # no look-ahead
            assert result.centre.confirmed_on == '2026-03-05'
            assert (result.centre.zd, result.centre.zg) == (20.92, 24.96)
            first_centre_as_of = first_centre_as_of or result.source.as_of_day
        else:
            # no centre claim of any kind before its evidence day
            assert result.centre is None
            assert result.source.as_of_day < '2026-03-05'

    assert first_centre_as_of == '2026-03-05'
    assert seen[len(CENTRE_BARS)] == final.segments


# A 20-pivot series whose fourth segment is an edge touch: the centre extends
# while its zone and formation day stay put.
VISIBLE_PIVOTS = [
    19.82, 21.17, 19.72, 22.79, 21.67, 25.16, 22.1, 24.11, 21.0, 22.83, 21.34,
    24.51, 21.9, 23.52, 21.08, 23.2, 21.89, 24.08, 22.94, 24.3,
]


def test_visible_on_is_the_formation_day_and_never_drifts():
    bars = zigzag_bars(VISIBLE_PIVOTS)
    results = []
    for length in range(2, len(bars) + 1):
        pen, segment = real_chain(bars[:length])
        result = adapt(segment, pen)
        if result.centre is not None:
            results.append(result)

    assert results
    first, last = results[0], results[-1]
    # formation is knowable exactly once and never re-dated
    assert first.visible_on == last.visible_on == first.centre.confirmed_on == '2026-03-05'
    assert {result.visible_on for result in results} == {'2026-03-05'}

    # the lifecycle moved later, and that later fact has its own clock
    assert last.lifecycle_status == 'extended'
    assert last.state_changed_on == '2026-03-17'
    assert last.state_changed_on > last.visible_on
    assert all(result.visible_on <= result.source.as_of_day for result in results)
    assert 'lifecycle extended since 2026-03-17' in last.describe()
    assert 'visible_on stays the formation day' in last.describe()


def test_fewer_than_three_confirmed_is_not_a_no_centre_claim():
    for length in (2, 8, 20, 40, 52):
        pen, segment = real_chain(CENTRE_BARS[:length])
        result = adapt(segment, pen)
        assert result.status == 'not_enough_confirmed_segments'
        assert result.absence_reason == 'fewer_than_three_confirmed_segments'
        assert result.centre is None and result.visible_on is None
        assert result.confirmed_prefix_length == len(segment.confirmed_segments) < 3
        text = result.describe()
        assert 'NOT ENOUGH' in text
        assert 'measured NO centre' not in text
        assert 'no centre claim either way' in text


def test_empty_input_fabricates_nothing():
    pen = empty_pens()
    segment = segment_from_pens(
        pen, binding=SegmentBinding(symbol=_SYMBOL, adjust='none', as_of_day=None)
    )
    result = adapt(segment, pen)

    assert result.status == 'not_enough_confirmed_segments'
    assert result.confirmed_prefix_length == 0
    assert result.total_segments == 0
    assert result.segments == ()
    assert result.chain is None
    assert result.centre is None
    assert result.issues == ()
    assert result.source.as_of_day is None
    assert 'NOT ENOUGH' in result.describe()


def test_terminal_is_strict_not_merely_no_blocking_refusal():
    empty_pen = empty_pens()
    empty_segment = segment_from_pens(
        empty_pen, binding=SegmentBinding(symbol=_SYMBOL, adjust='none', as_of_day=None)
    )
    empty = adapt(empty_segment, empty_pen)
    assert empty.issues == ()
    assert empty.terminal is False       # nothing was measured, nothing is closed

    pen, segment = real_chain(CENTRE_BARS[:64])
    assert segment.segments[-1].status == 'candidate'
    candidate_tail = adapt(segment, pen)
    assert candidate_tail.issues and not candidate_tail.issues[0].blocking
    assert candidate_tail.terminal is False

    blocked_pen, blocked_segment = real_chain(CENTRE_BARS)
    blocked = adapt(blocked_segment, blocked_pen)
    assert blocked.blocked_reasons == ('characteristic_initial_containment',)
    assert blocked.terminal is False

    # a snapshot that ends exactly at the leading three (nothing left out) is
    # the only shape that may call itself terminal
    explicit_pen, explicit_segment = explicit_chain(NO_CENTRE_PIVOTS_V2)
    closed_segment = replace(
        explicit_segment, segments=explicit_segment.confirmed_segments, pending=()
    )
    closed = adapt(closed_segment, explicit_pen)
    assert closed.issues == ()
    assert closed.status == 'no_centre'
    assert closed.terminal is True


def test_no_positive_width_intersection_is_a_measured_absence():
    pen, segment = explicit_chain(NO_CENTRE_PIVOTS_V2)
    result = adapt(segment, pen)

    assert result.status == 'no_centre'
    assert result.absence_reason == 'zero_width_intersection'
    assert result.centre is None and result.visible_on is None
    assert result.confirmed_prefix_length == 3

    lows = [s.low for s in result.segments]
    highs = [s.high for s in result.segments]
    assert max(lows) >= min(highs)          # the measurement, re-derived here
    assert (lows, highs) == ([86.0, 48.0, 48.0], [99.0, 99.0, 72.0])

    text = result.describe()
    assert 'measured NO centre' in text
    assert 'not missing data' in text
    # a pending candidate follows the three lines: the interval is measured but
    # the structure is not closed
    assert result.terminal is False
    assert 'INCOMPLETE' in text


def test_no_centre_with_a_blocking_tail_is_reported_as_incomplete():
    pen, segment = explicit_chain(NO_CENTRE_BLOCKED_PIVOTS)
    result = adapt(segment, pen)

    assert result.status == 'no_centre'
    assert result.terminal is False
    assert result.blocked_reasons == ('characteristic_gap_case2',)
    text = result.describe()
    assert 'measured NO centre' in text
    assert 'INCOMPLETE' in text
    assert 'characteristic_gap_case2' in text


def test_a_block_before_three_confirmed_lines_makes_no_claim_at_all():
    pen, segment = explicit_chain(BLOCKED_PIVOTS)
    result = adapt(segment, pen)

    assert segment.confirmed_segments == ()
    assert result.status == 'blocked_before_centre'
    assert result.absence_reason == 'segment_kernel_blocked'
    assert result.confirmed_prefix_length == 0
    assert result.chain is None
    assert result.centre is None
    assert result.terminal is False
    assert result.blocked_reasons == ('characteristic_initial_containment',)
    issue = result.issues[0]
    assert issue.blocking is True and issue.pen_index == 3

    text = result.describe()
    assert 'NO centre claim' in text
    assert 'incomplete' in text
    assert 'measured NO centre' not in text


# ---------------------------------------------------------------------------
# the range comes from the pens, and only from the span
# ---------------------------------------------------------------------------

def test_range_is_the_merged_bar_interval_of_the_span_pens():
    pen, segment = explicit_chain(INTERIOR_PIVOTS)
    result = adapt(segment, pen)
    external = result.segments[0]
    source = segment.confirmed_segments[0]

    span = pen.pens[source.pen_start_index:source.pen_end_index + 1]
    pivot_prices = [p.start.price for p in span] + [p.end.price for p in span]
    bar_lows = [e.bar_low for p in span for e in (p.start, p.end)]
    bar_highs = [e.bar_high for p in span for e in (p.start, p.end)]

    # v2: the merged K-line interval, not the pivots and not the endpoints
    assert (external.low, external.high) == (min(bar_lows), max(bar_highs))
    assert (external.low, external.high) == (17.0, 27.0)
    assert external.low < min(pivot_prices) and external.high > max(pivot_prices)
    assert external.low != external.start_price == 20.0      # endpoint pair would say 20
    assert external.start_price < external.end_price < external.high

    # the evidence pens reach past the end; they are not part of the span range
    assert max(source.evidence_pen_indices) > source.pen_end_index


def test_the_v1_pivot_range_fixture_becomes_a_centre_under_merged_bar_ranges():
    """The documented convention change: wicks are range, pivots are not."""
    pen, segment = explicit_chain(NO_CENTRE_PIVOTS)
    result = adapt(segment, pen)

    def pivot_range(source):
        span = pen.pens[source.pen_start_index:source.pen_end_index + 1]
        prices = [p.start.price for p in span] + [p.end.price for p in span]
        return min(prices), max(prices)

    pivot_ranges = [pivot_range(source) for source in segment.confirmed_segments[:3]]
    pivot_zd = max(low for low, _ in pivot_ranges)
    pivot_zg = min(high for _, high in pivot_ranges)
    # v1 pivot prices saw no positive width; v2 merged bars do
    assert pivot_zd >= pivot_zg
    assert result.status == 'centre'
    assert result.centre is not None
    assert (result.centre.zd, result.centre.zg) == (19.0, 20.58)
    assert result.centre.width == pytest.approx(1.58)
    assert result.centre.confirmed_on == segment.confirmed_segments[2].confirmed_on


def test_a_bar_interval_that_contradicts_its_own_pivot_is_refused():
    pen, segment = explicit_chain(NO_CENTRE_PIVOTS_V2)
    first = pen.pens[0]

    # a pivot above the bar high / below the bar low / a bar with low > high
    for endpoint in (
        replace(first.start, bar_high=first.start.price - 0.5),
        replace(first.start, bar_low=first.start.price + 0.5),
        replace(first.start, bar_low=first.start.price, bar_high=first.start.price - 1.0),
    ):
        bad_pen = replace(pen, pens=(replace(first, start=endpoint),) + pen.pens[1:])
        doctored = replace(segment, input_digest=segment_input_digest(bad_pen))
        with pytest.raises(ValueError, match='merged-bar|bar'):
            adapt(doctored, bad_pen)


# ---------------------------------------------------------------------------
# reverse symmetry
# ---------------------------------------------------------------------------

def test_the_mirrored_series_produces_the_mirrored_centre():
    pen, segment = real_chain(CENTRE_BARS)
    direct = adapt(segment, pen)
    mirrored_pen, mirrored_segment = real_chain(mirror_bars(CENTRE_BARS))
    mirrored = adapt(mirrored_segment, mirrored_pen)

    assert mirrored.status == 'centre'
    assert [s.direction for s in mirrored.segments] == ['down', 'up', 'down']
    assert [s.direction for s in direct.segments] == ['up', 'down', 'up']
    assert [s.confirmed_on for s in mirrored.segments] == [s.confirmed_on for s in direct.segments]
    assert [s.end_day for s in mirrored.segments] == [s.end_day for s in direct.segments]
    assert mirrored.centre.zd == pytest.approx(60.0 - direct.centre.zg)
    assert mirrored.centre.zg == pytest.approx(60.0 - direct.centre.zd)
    assert mirrored.centre.confirmed_on == direct.centre.confirmed_on
    assert mirrored.blocked_reasons == direct.blocked_reasons


# ---------------------------------------------------------------------------
# source identity: symbol, adjustment, digest, expectation
# ---------------------------------------------------------------------------

def test_adjustment_base_is_part_of_the_source_identity():
    pen, none_segment = real_chain(CENTRE_BARS)
    _, qfq_segment = real_chain(CENTRE_BARS, adjust='qfq')
    _, unknown_segment = real_chain(CENTRE_BARS, adjust='unknown')

    none_result = adapt(none_segment, pen)
    qfq_result = adapt(qfq_segment, pen)
    unknown_result = adapt(unknown_segment, pen)

    assert none_result.segments == qfq_result.segments == unknown_result.segments
    assert len({none_result.chain.snapshot_version, qfq_result.chain.snapshot_version,
                unknown_result.chain.snapshot_version}) == 3
    assert qfq_result.source.same_source(none_result.source) is False
    assert unknown_result.source.adjust == 'unknown'               # never coerced
    with pytest.raises(ValueError, match='different sources'):
        qfq_result.assert_same_source(none_result)

    # a centre from one base cannot be re-read under another base's version
    with pytest.raises(ValueError, match='snapshot digest mismatch'):
        centre_from_chain(
            qfq_result.chain, expect_snapshot_version=none_result.chain.snapshot_version
        )


def test_expect_source_pins_the_sample():
    pen, segment = real_chain(CENTRE_BARS)
    _, qfq_segment = real_chain(CENTRE_BARS, adjust='qfq')
    expected = adapt(segment, pen).source

    with pytest.raises(ValueError, match='different sources'):
        adapt(qfq_segment, pen, expect_source=expected)

    same = adapt(segment, pen, expect_source=expected)
    assert same.source.same_source(expected)


def test_the_centre_kernel_is_only_called_with_three_confirmed_segments(monkeypatch):
    import src.core.chan_centre_adapter as adapter_module

    calls = []

    def spy(*args, **kwargs):  # pragma: no cover - must not run on short prefixes
        calls.append(args)
        raise AssertionError('centre_from_chain must not be called below three segments')

    monkeypatch.setattr(adapter_module, 'centre_from_chain', spy)

    pen, segment = real_chain(CENTRE_BARS[:52])
    result = adapt(segment, pen)
    assert result.status == 'not_enough_confirmed_segments'
    assert calls == []


def test_the_result_is_deterministic():
    pen, segment = real_chain(CENTRE_BARS)
    assert adapt(segment, pen) == adapt(segment, pen)


# ---------------------------------------------------------------------------
# fail-closed: contradictions raise, they are never repaired
# ---------------------------------------------------------------------------

def test_a_pen_snapshot_from_another_series_is_refused():
    _, segment = real_chain(CENTRE_BARS)
    other_pen = build_chan_pen(zigzag_bars([20.0, 23.0, 21.0, 24.0, 22.5, 25.0]))
    # same as-of day, different series: only the digest can tell them apart
    with pytest.raises(ValueError, match='digest'):
        adapt(segment, replace(other_pen, as_of_day=segment.as_of_day))


def test_a_pen_snapshot_with_another_kernel_or_as_of_is_refused():
    pen, segment = real_chain(CENTRE_BARS)
    with pytest.raises(ValueError, match='kernel'):
        adapt(segment, replace(pen, kernel_version='chan-pen-v0'))
    with pytest.raises(ValueError, match='as_of_day'):
        adapt(segment, replace(pen, as_of_day='2026-03-16'))


def test_a_binding_as_of_mismatch_is_refused():
    pen, segment = real_chain(CENTRE_BARS)
    with pytest.raises(ValueError, match='binding as_of_day'):
        adapt(replace(segment, as_of_day='2026-03-16'), pen)


def _one_pen_segment(index, pen):
    """A forged 'segment' over a single pen: the shape the adapter must refuse."""
    source = pen.pens[index]
    return Segment(
        start=SegmentEndpoint(
            kind=source.start.kind,
            pen_index=index,
            merged_index=source.start.merged_index,
            position_day=source.start.position_day,
            price=source.start.price,
            status='confirmed',
            confirmed_on=source.start.confirmed_on,
            revisions=0,
            drift_days=(),
        ),
        end=SegmentEndpoint(
            kind=source.end.kind,
            pen_index=index,
            merged_index=source.end.merged_index,
            position_day=source.end.position_day,
            price=source.end.price,
            status='confirmed',
            confirmed_on=source.end.confirmed_on,
            revisions=0,
            drift_days=(),
        ),
        direction=source.direction,
        pen_start_index=index,
        pen_end_index=index,
        status='confirmed',
        reason='first_case_no_gap',
        confirmed_on=source.confirmed_on,
        characteristic_elements=MIN_PENS,
        kernel_version=SEGMENT_KERNEL_VERSION,
    )


def test_a_forged_one_pen_segment_cannot_mint_a_centre():
    pen = make_pens([20.0, 24.0, 22.0, 26.0])
    base = segment_from_pens(
        pen, binding=SegmentBinding(symbol=_SYMBOL, adjust='none', as_of_day=pen.as_of_day)
    )
    fake = tuple(_one_pen_segment(index, pen) for index in (0, 1, 2))
    doctored = replace(base, segments=fake, pending=(), input_digest=segment_input_digest(pen))

    with pytest.raises(ValueError, match='MIN_PENS'):
        adapt(doctored, pen)


def test_a_segment_with_an_impossible_element_count_is_refused():
    pen, segment = real_chain(CENTRE_BARS)
    first = segment.segments[0]
    for count in (2, 99):
        doctored = replace(
            segment, segments=(replace(first, characteristic_elements=count),) + segment.segments[1:]
        )
        with pytest.raises(ValueError, match='characteristic'):
            adapt(doctored, pen)


def test_evidence_must_name_real_supplied_pens_and_their_real_days():
    pen, segment = real_chain(CENTRE_BARS)
    first = segment.segments[0]
    middle = first.evidence[1]

    doctored_evidence = {
        'pen index outside the series': (
            FractalEvidence(role='left', pen_indices=first.evidence[0].pen_indices,
                            high=first.evidence[0].high, low=first.evidence[0].low,
                            confirmed_on=first.evidence[0].confirmed_on),
            FractalEvidence(role='middle', pen_indices=(999,), high=middle.high, low=middle.low,
                            confirmed_on=middle.confirmed_on),
            FractalEvidence(role='right', pen_indices=first.evidence[2].pen_indices,
                            high=first.evidence[2].high, low=first.evidence[2].low,
                            confirmed_on=first.evidence[2].confirmed_on),
        ),
        'invented element day': (
            first.evidence[0],
            replace(middle, confirmed_on='2026-12-31'),
            first.evidence[2],
        ),
        'evidence pen repeated': (
            first.evidence[0],
            replace(middle, pen_indices=(middle.pen_indices[0], middle.pen_indices[0])),
            first.evidence[2],
        ),
    }
    for evidence in doctored_evidence.values():
        doctored = replace(
            segment,
            segments=(replace(first, evidence=evidence),) + segment.segments[1:],
        )
        with pytest.raises(ValueError, match='evidence|pen|froze|element'):
            adapt(doctored, pen)


def test_an_endpoint_confirmation_day_is_rederived_not_trusted():
    pen, segment = real_chain(CENTRE_BARS)
    first = segment.segments[0]
    cases = (
        replace(first, end=replace(first.end, confirmed_on='2026-12-31')),
        replace(first, start=replace(first.start, confirmed_on='2026-12-31')),
    )
    for doctored_first in cases:
        doctored = replace(segment, segments=(doctored_first,) + segment.segments[1:])
        with pytest.raises(ValueError, match='endpoint clock|after the segment'):
            adapt(doctored, pen)


def test_a_doctored_confirmed_segment_is_refused():
    pen, segment = real_chain(CENTRE_BARS)
    first = segment.segments[0]

    cases = {
        'wrong reason': replace(segment, segments=(replace(first, reason='characteristic_gap_case2'),) + segment.segments[1:]),
        'confirmation before the end': replace(segment, segments=(replace(first, confirmed_on='2026-01-10'),) + segment.segments[1:]),
        'span shifted onto another pen': replace(segment, segments=(replace(first, pen_start_index=1),) + segment.segments[1:]),
        'direction flipped': replace(segment, segments=(replace(first, direction='down'),) + segment.segments[1:]),
        'unfrozen endpoint': replace(
            segment,
            segments=(replace(first, start=replace(first.start, status='candidate', confirmed_on=None)),)
            + segment.segments[1:],
        ),
    }
    for label, doctored in cases.items():
        with pytest.raises(ValueError):
            adapt(doctored, pen)


def test_a_confirmed_segment_after_a_blocked_one_is_refused():
    pen, segment = real_chain(CENTRE_BARS)
    first, second, third, blocked = segment.segments
    fake_block = replace(
        second,
        status='blocked',
        reason='characteristic_initial_containment',
        confirmed_on=None,
        evidence=(),
        end=replace(second.end, status='candidate', confirmed_on=None),
    )
    doctored = replace(segment, segments=(first, fake_block, third), pending=())
    with pytest.raises(ValueError, match='contiguous prefix|after an unconfirmed'):
        adapt(doctored, pen)


def test_a_segment_from_another_kernel_is_refused():
    pen, segment = real_chain(CENTRE_BARS)
    with pytest.raises(ValueError, match='kernel'):
        adapt(replace(segment, kernel_version='chan-segment-v1'), pen)


def test_an_undated_prefixed_snapshot_is_refused():
    pen, segment = real_chain(CENTRE_BARS)
    doctored = replace(segment, as_of_day=None, binding=replace(segment.binding, as_of_day=None))
    with pytest.raises(ValueError, match='undated|as_of_day'):
        adapt(doctored, replace(pen, as_of_day=None))


def test_the_public_helpers_require_their_arguments():
    pen, segment = real_chain(CENTRE_BARS)
    with pytest.raises(ValueError, match='PenSnapshot'):
        adapt(segment, object())
    with pytest.raises(ValueError, match='SegmentSnapshot'):
        centre_from_segment_snapshot(object(), pen_snapshot=pen)
    with pytest.raises(ValueError, match='adjustment base'):
        CentreSource(
            symbol=_SYMBOL,
            adjust='adjusted',
            as_of_day='2026-03-15',
            pen_digest=segment.input_digest,
            segment_kernel=SEGMENT_KERNEL_VERSION,
            pen_kernel=PEN_KERNEL_VERSION,
        )
