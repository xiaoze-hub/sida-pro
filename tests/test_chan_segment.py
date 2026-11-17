"""Chan v1 segment (线段) kernel: 第67课第一种情况 only — causal, conservative.

These tests pin the hard parts, not a happy path:

* a segment needs at least three confirmed pens and a **non-empty** overlap of
  their ranges; a zero-intersection trio is refused (never forced into a 线段);
* the segment ends only when the **反方向笔特征序列** forms a terminal fractal
  (顶分型 for an up segment, 底分型 for a down one), and only the *no-gap* case
  (`第一种情况`) is implemented: a gap between the fractal's first two elements
  is reported ``blocked`` (`第二种情况` needs the second characteristic sequence,
  which is deliberately not implemented and never guessed);
* 包含处理 of the characteristic sequence is sequential and **non-transitive**
  (a transitive closure would merge different elements), and an initial
  containment — where the merge direction would have to be invented — is
  ``blocked`` instead of assumed;
* only confirmed pens are evidence: a candidate pen can never confirm a segment,
  and the earliest confirmation day is the day the third characteristic element
  was frozen, never the extreme's own day;
* endpoint drift lives on the open (candidate) segment only.  A confirmed
  segment is immutable: a later higher high is *not* back-filled, it belongs to
  the next structure — prefix replay is checked bar by bar;
* a tainted (``blocked``) pen in the span blocks the segment; nothing is
  published as fixed across it;
* the result is explicitly bound to one source snapshot (symbol / adjustment /
  as-of day / input digest / pen kernel version).  Two results from different
  sources are never "the same line", and mixing them raises;
* every input inconsistency (non-contiguous pens, non-alternating directions,
  duplicated merged indices, mixed kernel versions, a confirmation after
  ``as_of_day``, a confirmation not after its own position, a pen whose prices
  contradict its direction) raises ``ValueError`` instead of being repaired.

The hand-built pen series below are *explicit inputs* (the module's contract: a
pure function of a given pen sequence); one test additionally drives the whole
bar → fractal → pen → segment pipeline on generated bars and compares against a
hand-derived oracle.
"""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.core.chan_pen import (
    PEN_KERNEL_VERSION,
    Pen,
    PenEndpoint,
    PenRules,
    PenSnapshot,
    build_chan_pen,
)
from src.core.chan_segment import (
    MIN_PENS,
    SEGMENT_KERNEL_VERSION,
    SegmentBinding,
    SegmentRules,
    normalize_characteristic_sequence,
    segment_from_pens,
    segment_input_digest,
)
from src.core.chan_structure import RawBar, build_chan_structure

_START = date(2026, 1, 1)   # bar fixtures (January, day index == raw index)
_PEN_START = date(2026, 3, 2)  # explicit pen fixtures


def _d(index, start=_START):
    return (start + timedelta(days=index)).isoformat()


def _pd(index):
    """Day of merged pivot ``index`` in the explicit pen fixtures (4 bars apart)."""
    return _d(4 * index, _PEN_START)


# ---------------------------------------------------------------------------
# fixtures: real bars (merged index == raw index, pivots every 4 bars)
# ---------------------------------------------------------------------------

def zigzag_bars(pivots, *, step=4, half=0.5, start=_START):
    """Piecewise-linear zig-zag: every raw bar is its own merged bar.

    The first bar is a lead-in one tick above the first pivot so pivot 0 is a
    strict bottom.  Only the pivots are extrema, so merged index == raw index
    and every fractal centre is a pivot index.
    """
    mids = [float(pivots[0]) + 1.0, float(pivots[0])]
    for a, b in zip(pivots, pivots[1:]):
        for k in range(1, step + 1):
            mids.append(round(a + (b - a) * k / step, 6))
    bars = []
    for i, mid in enumerate(mids):
        bars.append(
            RawBar(
                day=_d(i, start),
                open=mid,
                high=round(mid + half, 6),
                low=round(mid - half, 6),
                close=mid,
            )
        )
    return bars


# Hand-derived zig-zag: pivots at bars 1, 5, 9, ... 41 with prices
#   bottom 4.0, top 8.0, bottom 5.0, top 9.0, bottom 6.0, top 10.0,
#   bottom 7.0, top 9.5, bottom 6.5, top 9.0, bottom 5.5 (open tail)
# so the first up segment tops out at 10.5 (bar 21) and ends when the
# characteristic sequence of down pens (8.5/5.5, 9.5/6.5, 10.5/7.5, 10.0/7.0)
# prints a top fractal at 10.5/7.5.  Its third element is the down pen
# 10.0 -> 7.0, which is frozen only when bar 39 arrives (see the oracle test).
SEGMENT_ZIGZAG = [4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5, 9.0, 5.5]
SEGMENT_ORACLE_BARS = zigzag_bars(SEGMENT_ZIGZAG)

# Only used to prove the input digest is price-sensitive.
OTHER_BARS = zigzag_bars([4.0, 8.0, 5.0, 9.0, 6.0, 10.5, 7.5, 9.5, 6.5, 9.0, 5.5])


# ---------------------------------------------------------------------------
# fixtures: explicit pen series (the module's documented input contract)
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


def zigzag_points(prices, *, first_kind='bottom'):
    second = 'top' if first_kind == 'bottom' else 'bottom'
    kinds = [first_kind if i % 2 == 0 else second for i in range(len(prices))]
    return [(kinds[i], _pd(i), float(prices[i])) for i in range(len(prices))]


def make_pens(
    prices,
    *,
    confirmed=None,
    statuses=None,
    as_of=None,
    kernel_version=PEN_KERNEL_VERSION,
    first_kind='bottom',
):
    """Build a pen snapshot from pivot prices (bottom/top alternating).

    Pen ``k`` joins pivot ``k`` to pivot ``k + 1`` (merged indices ``4k`` /
    ``4k + 4``, i.e. the v1 spacing is visibly respected).  ``confirmed`` is the
    number of leading pens that are frozen; the rest are candidates.  Each
    frozen pen is dated one day after its end pivot, so "evidence after
    position" holds.  ``statuses`` overrides individual pen statuses
    (``blocked``).
    """
    points = zigzag_points(prices, first_kind=first_kind)
    total = len(points) - 1
    confirmed = total if confirmed is None else confirmed
    pens = []
    for k in range(total):
        (ka, da, pa), (kb, db, pb) = points[k], points[k + 1]
        frozen_on = _pd(k + 1) if k < confirmed else None
        if frozen_on is not None:
            frozen_on = (date.fromisoformat(frozen_on) + timedelta(days=1)).isoformat()
        status = 'confirmed' if frozen_on else 'candidate'
        if statuses and k in statuses:
            status = statuses[k]
            if status != 'confirmed':
                frozen_on = None
        start_on = pens[-1].end.confirmed_on if pens else frozen_on
        pens.append(
            Pen(
                start=_point_endpoint(ka, k, da, pa, start_on),
                end=_point_endpoint(kb, k + 1, db, pb, frozen_on),
                direction='up' if ka == 'bottom' else 'down',
                status=status,
                confirmed_on=frozen_on,
                kernel_version=kernel_version,
            )
        )
    known_days = [p[1] for p in points] + [p.confirmed_on for p in pens if p.confirmed_on]
    return PenSnapshot(
        as_of_day=as_of or max(known_days),
        kernel_version=kernel_version,
        rules=PenRules(),
        endpoints=(),
        pens=tuple(pens),
        rejections=(),
        blocked_fractals=(),
        taint_indices=(),
    )


def binding_for(snapshot, **overrides):
    kwargs = dict(symbol='000001.SZ', adjust='none', as_of_day=snapshot.as_of_day)
    kwargs.update(overrides)
    return SegmentBinding(**kwargs)


def build(snapshot, **overrides):
    return segment_from_pens(snapshot, binding=binding_for(snapshot, **overrides))


# ---------------------------------------------------------------------------
# versioning / rules
# ---------------------------------------------------------------------------

def test_rules_are_versioned_and_every_field_is_pinned():
    rules = SegmentRules()
    assert rules.kernel_version == SEGMENT_KERNEL_VERSION == 'chan-segment-v1'
    assert rules.min_pens == MIN_PENS == 3
    assert rules.overlap == 'pen_endpoint_range_strict'
    assert rules.characteristic_sequence == 'reverse_pens'
    assert rules.inclusion == 'sequential_non_transitive'
    assert rules.merge_direction == 'segment_direction'
    assert rules.gap_case == 'blocked_case2_not_implemented'
    assert rules.initial_containment == 'blocked'
    assert rules.equal_extreme == 'keep_earlier'
    assert rules.evidence == 'confirmed_pens_only'


@pytest.mark.parametrize(
    'field, value',
    [
        ('kernel_version', 'chan-segment-v2'),
        ('min_pens', 4),
        ('overlap', 'any_touch'),
        ('characteristic_sequence', 'all_pens'),
        ('inclusion', 'transitive_closure'),
        ('merge_direction', 'inferred_from_elements'),
        ('gap_case', 'confirm_anyway'),
        ('initial_containment', 'guess'),
        ('equal_extreme', 'keep_later'),
        ('evidence', 'candidate_pens_too'),
    ],
)
def test_unsupported_rule_values_are_refused(field, value):
    with pytest.raises(ValueError):
        SegmentRules(**{field: value})


def test_snapshot_carries_the_rule_set_it_was_built_with():
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5])
    snap = segment_from_pens(snapshot, binding=binding_for(snapshot), rules=SegmentRules())
    assert snap.rules == SegmentRules()
    assert snap.kernel_version == SEGMENT_KERNEL_VERSION
    with pytest.raises(ValueError):
        SegmentRules(min_pens=4)


# ---------------------------------------------------------------------------
# explicit source binding: symbol / adjustment / as_of / digest
# ---------------------------------------------------------------------------

def test_binding_and_input_digest_are_explicit():
    pen_snapshot = build_chan_pen(SEGMENT_ORACLE_BARS)
    binding = SegmentBinding(symbol='000001.SZ', adjust='qfq', as_of_day=pen_snapshot.as_of_day)
    snap = segment_from_pens(pen_snapshot, binding=binding)

    assert snap.binding == binding
    assert snap.source_pen_kernel_version == PEN_KERNEL_VERSION
    assert snap.input_digest == segment_input_digest(pen_snapshot)
    assert len(snap.input_digest) == 64

    # Same input series -> same digest, regardless of object identity.
    assert segment_input_digest(build_chan_pen(SEGMENT_ORACLE_BARS)) == snap.input_digest
    # A different price series must not look like the same sample.
    assert segment_input_digest(build_chan_pen(OTHER_BARS)) != snap.input_digest


def test_results_from_different_sources_are_never_mixed():
    pen_a = build_chan_pen(SEGMENT_ORACLE_BARS)
    pen_b = build_chan_pen(OTHER_BARS)
    a = segment_from_pens(pen_a, binding=binding_for(pen_a))
    b = segment_from_pens(pen_b, binding=binding_for(pen_b))

    assert a.same_source(a) is True
    assert a.same_source(b) is False
    with pytest.raises(ValueError):
        a.assert_same_source(b)

    # The same snapshot under another adjustment base is a different line.
    c = segment_from_pens(pen_a, binding=binding_for(pen_a, adjust='hfq'))
    assert a.same_source(c) is False
    with pytest.raises(ValueError):
        a.assert_same_source(c)

    # ... and a result computed from an older prefix is stale for a later day.
    assert a.is_stale(a.as_of_day) is False
    assert a.is_stale('2999-01-01') is True
    assert segment_from_pens(pen_a, binding=binding_for(pen_a)).same_source(a) is True

    # An older prefix is not the same sample as a longer one.
    older = build_chan_pen(SEGMENT_ORACLE_BARS[:20])
    d = segment_from_pens(older, binding=binding_for(older))
    assert d.same_source(a) is False


@pytest.mark.parametrize(
    'overrides',
    [
        {'symbol': ''},
        {'symbol': '   '},
        {'adjust': 'adjusted'},
        {'as_of_day': '2026-01-05'},
        {'as_of_day': None},
    ],
)
def test_invalid_binding_is_refused(overrides):
    pen_snapshot = build_chan_pen(SEGMENT_ORACLE_BARS)
    with pytest.raises(ValueError):
        segment_from_pens(pen_snapshot, binding=binding_for(pen_snapshot, **overrides))


# ---------------------------------------------------------------------------
# input validation: a foreign pen series is re-verified, never trusted
# ---------------------------------------------------------------------------

def test_pens_must_be_contiguous_and_alternating():
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5])

    # A gap in the shared endpoint breaks "pens chain by endpoints".
    broken = tuple(
        replace(p, start=replace(p.start, merged_index=p.start.merged_index - 2))
        if i == 2
        else p
        for i, p in enumerate(snapshot.pens)
    )
    with pytest.raises(ValueError):
        build(replace(snapshot, pens=broken))

    # Two pens in the same direction cannot form a segment chain.
    same_dir = tuple(
        replace(p, direction='up') if i == 1 else p for i, p in enumerate(snapshot.pens)
    )
    with pytest.raises(ValueError):
        build(replace(snapshot, pens=same_dir))

    # An up pen whose end price is not above its start contradicts itself.
    with pytest.raises(ValueError):
        build(
            replace(
                snapshot,
                pens=(
                    replace(snapshot.pens[0], end=replace(snapshot.pens[0].end, price=3.0)),
                )
                + snapshot.pens[1:],
            )
        )

    # A duplicated pen is not a prefix replay.
    with pytest.raises(ValueError):
        build(replace(snapshot, pens=(snapshot.pens[0], snapshot.pens[0]) + snapshot.pens[1:]))

    # A confirmation day after as_of_day is look-ahead.
    with pytest.raises(ValueError):
        build(replace(snapshot, as_of_day='2026-03-02'))


def test_a_confirmed_pen_must_carry_its_confirmation_day():
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0])
    forged = replace(
        snapshot,
        pens=tuple(
            replace(p, status='confirmed', confirmed_on=None) if i == 0 else p
            for i, p in enumerate(snapshot.pens)
        ),
    )
    with pytest.raises(ValueError):
        build(forged)

    # ... and a candidate must not pretend to be frozen.
    with pytest.raises(ValueError):
        build(
            replace(
                snapshot,
                pens=tuple(
                    replace(p, status='candidate', confirmed_on=_pd(9)) if i == 1 else p
                    for i, p in enumerate(snapshot.pens)
                ),
            )
        )

    # A confirmation may never precede the position it certifies.
    with pytest.raises(ValueError):
        build(
            replace(
                snapshot,
                pens=tuple(
                    replace(p, confirmed_on=_pd(0), end=replace(p.end, confirmed_on=_pd(0)))
                    if i == 2
                    else p
                    for i, p in enumerate(snapshot.pens)
                ),
            )
        )


def test_pens_from_another_kernel_version_are_refused():
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0])
    mixed = replace(
        snapshot,
        pens=(replace(snapshot.pens[0], kernel_version='chan-pen-v0'),) + snapshot.pens[1:],
    )
    with pytest.raises(ValueError):
        build(mixed)
    with pytest.raises(ValueError):
        build(replace(snapshot, kernel_version='chan-pen-v2'))


def test_a_candidate_pen_may_only_be_the_tail():
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0, 6.0], confirmed=5)
    forged = replace(
        snapshot,
        pens=(
            replace(snapshot.pens[0], status='candidate', confirmed_on=None),
        )
        + snapshot.pens[1:],
    )
    with pytest.raises(ValueError):
        build(forged)


def test_empty_input_yields_no_structure():
    snapshot = PenSnapshot(
        as_of_day=None,
        kernel_version=PEN_KERNEL_VERSION,
        rules=PenRules(),
        endpoints=(),
        pens=(),
        rejections=(),
        blocked_fractals=(),
        taint_indices=(),
    )
    snap = segment_from_pens(
        snapshot, binding=SegmentBinding(symbol='000001.SZ', adjust='none', as_of_day=None)
    )
    assert (snap.as_of_day, snap.segments, snap.pending, snap.rejections) == (None, (), (), ())
    assert snap.confirmed_segments == ()
    assert snap.open_segment is None


# ---------------------------------------------------------------------------
# the real pipeline: bars -> fractals -> pens -> segment, against a hand oracle
# ---------------------------------------------------------------------------

def test_the_zigzag_fixture_is_pinned_before_any_segment_claim():
    """Every number the segment oracle uses is pinned here, from the bars."""
    structure = build_chan_structure(SEGMENT_ORACLE_BARS)
    assert [m.index for m in structure.merged_bars] == list(range(len(SEGMENT_ORACLE_BARS)))
    assert [
        (f.kind, f.position_merged_index, f.position_day, f.position_price, f.status)
        for f in structure.fractals
    ] == [
        ('bottom', 1, _d(1), 3.5, 'confirmed'),
        ('top', 5, _d(5), 8.5, 'confirmed'),
        ('bottom', 9, _d(9), 4.5, 'confirmed'),
        ('top', 13, _d(13), 9.5, 'confirmed'),
        ('bottom', 17, _d(17), 5.5, 'confirmed'),
        ('top', 21, _d(21), 10.5, 'confirmed'),
        ('bottom', 25, _d(25), 6.5, 'confirmed'),
        ('top', 29, _d(29), 10.0, 'confirmed'),
        ('bottom', 33, _d(33), 6.0, 'confirmed'),
        ('top', 37, _d(37), 9.5, 'confirmed'),
    ]


def test_segment_end_and_earliest_confirmation_are_hand_derived():
    """Hand oracle for the single confirmed up segment.

    * the segment starts at the first pen's start (bottom 4.0, bar 1);
    * the characteristic sequence (down pens) is ``8.5/5.5``, ``9.5/6.5``,
      ``10.5/7.5``, ``10.0/7.0`` — the top fractal sits at ``10.5/7.5``, i.e.
      the segment's high is 10.5 at bar 21 (day 2026-01-22);
    * that fractal has **no gap** (its second element's low 7.5 sits below the
      first element's high 9.5), so 第一种情况 applies and the segment ends;
    * the earliest day it may be published is the day the *third* characteristic
      element (the down pen 10.0 -> 7.0) was frozen: that pen's end (bottom 7.0,
      bar 33) freezes only when the top at bar 37 is confirmed, whose 3-bar
      window is sealed by bar 38 — i.e. from the bar-39 prefix on, 2026-02-09;
    * the next down segment cannot be confirmed yet (two characteristic
      elements only), so it stays ``candidate`` with its end drifted to 7.0.
    """
    bars = SEGMENT_ORACLE_BARS
    pen_snapshot = build_chan_pen(bars)
    assert [
        (
            p.start.merged_index,
            p.end.merged_index,
            p.start_price,
            p.end_price,
            p.status,
            p.confirmed_on,
        )
        for p in pen_snapshot.pens
    ] == [
        (1, 5, 3.5, 8.5, 'confirmed', _d(11)),
        (5, 9, 8.5, 4.5, 'confirmed', _d(15)),
        (9, 13, 4.5, 9.5, 'confirmed', _d(19)),
        (13, 17, 9.5, 5.5, 'confirmed', _d(23)),
        (17, 21, 5.5, 10.5, 'confirmed', _d(27)),
        (21, 25, 10.5, 6.5, 'confirmed', _d(31)),
        (25, 29, 6.5, 10.0, 'confirmed', _d(35)),
        (29, 33, 10.0, 6.0, 'confirmed', _d(39)),
        (33, 37, 6.0, 9.5, 'candidate', None),
    ]

    snap = build(pen_snapshot)
    assert len(snap.confirmed_segments) == 1
    segment = snap.confirmed_segments[0]

    assert segment.status == 'confirmed'
    assert segment.reason == 'first_case_no_gap'
    assert segment.direction == 'up'
    assert (segment.pen_start_index, segment.pen_end_index) == (0, 4)
    assert (segment.start.position_day, segment.start.price) == (_d(1), 3.5)
    assert (segment.end.position_day, segment.end.price) == (_d(21), 10.5)
    assert segment.confirmed_on == _d(39) == '2026-02-09'
    assert segment.end.confirmed_on == _d(27)
    assert segment.characteristic_elements == 4
    assert segment.pens_used == 5

    # Only confirmed pens are evidence for a confirmed segment.
    evidence = pen_snapshot.pens[segment.pen_start_index : segment.pen_end_index + 1]
    assert all(p.status == 'confirmed' for p in evidence)

    # The next (down) segment is a candidate whose end already drifted once.
    candidate = snap.open_segment
    assert candidate is not None
    assert (candidate.status, candidate.direction) == ('candidate', 'down')
    assert (candidate.start.price, candidate.start.position_day) == (10.5, _d(21))
    assert (candidate.end.price, candidate.end.position_day) == (6.0, _d(33))
    assert (candidate.end.revisions, candidate.end.drift_days) == (1, (_d(25),))
    assert candidate.confirmed_on is None
    assert candidate.reason == 'awaiting_confirmed_characteristic_pen'
    assert snap.pending == ()


def test_a_confirmed_segment_never_appears_before_its_evidence_day():
    bars = SEGMENT_ORACLE_BARS
    oracle = {(0, 4): _d(39)}
    first_seen: dict[tuple[int, int], str] = {}
    ever: set[tuple[int, int]] = set()
    frozen: dict[tuple[int, int], object] = {}

    for end in range(1, len(bars) + 1):
        day = bars[end - 1].day
        pen_snapshot = build_chan_pen(bars[:end])
        snap = segment_from_pens(pen_snapshot, binding=binding_for(pen_snapshot))
        keys = {(s.pen_start_index, s.pen_end_index) for s in snap.confirmed_segments}
        for key in keys:
            first_seen.setdefault(key, day)
        assert ever <= keys, f'{sorted(ever - keys)} disappeared on {day}'
        ever |= keys

        for key, confirmed_day in oracle.items():
            if day < confirmed_day:
                assert key not in keys, f'{key} confirmed on {day}, evidence is {confirmed_day}'
            else:
                assert key in keys, f'{key} must be confirmed from {confirmed_day} on'

        # A confirmed segment is a pure function of the prefix: no later bar and
        # no later pen may leak into it, and it is never rewritten.
        for segment in snap.confirmed_segments:
            assert segment.confirmed_on <= day
            assert segment.end.position_day <= day
            for pen in pen_snapshot.pens[segment.pen_start_index : segment.pen_end_index + 1]:
                assert pen.status == 'confirmed'
            key = (segment.pen_start_index, segment.pen_end_index)
            if key in frozen:
                assert segment == frozen[key], f'confirmed segment {key} changed on {day}'
            frozen[key] = segment

    assert first_seen == oracle


def test_a_candidate_pen_can_never_confirm_a_segment():
    """The third characteristic element is open: the geometry is not evidence."""
    prices = [4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5]
    open_tail = make_pens(prices, confirmed=7)
    snap = build(open_tail)
    assert snap.confirmed_segments == ()
    assert snap.open_segment.status == 'candidate'
    assert snap.open_segment.reason == 'awaiting_confirmed_characteristic_pen'
    # The open pen (9.5 -> 6.5) is not evidence, so the end stays on the frozen
    # same-direction pen (the 10.0 top), not on the open pen's low.
    assert (snap.open_segment.end.price, snap.open_segment.pen_end_index) == (10.0, 4)

    # Freeze the third element and the same geometry confirms, dated by that pen.
    frozen = make_pens(prices, confirmed=8)
    snap2 = build(frozen)
    assert len(snap2.confirmed_segments) == 1
    segment = snap2.confirmed_segments[0]
    assert segment.pen_end_index == 4
    assert segment.end.price == 10.0
    assert segment.confirmed_on == frozen.pens[7].confirmed_on
    assert segment.characteristic_elements == 4


def test_more_extreme_end_moves_only_the_open_segment_end():
    # Candidate up segment: tops at 8.0, 9.0, 10.0 and no third element yet.
    prices = [4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0]
    snap = build(make_pens(prices, confirmed=6))
    segment = snap.segments[0]
    assert segment.status == 'candidate'
    assert (segment.end.price, segment.pen_end_index) == (10.0, 4)
    assert (segment.end.revisions, segment.end.drift_days) == (2, (_pd(1), _pd(3)))
    superseded = [r for r in snap.rejections if r.reason == 'superseded_by_more_extreme']
    assert [(r.pen_index, r.price) for r in superseded] == [(0, 8.0), (2, 9.0)]

    # An equal extreme keeps the earlier position instead of moving the end.
    equal = build(make_pens([4.0, 8.0, 5.0, 10.0, 6.0, 10.0, 7.0], confirmed=6))
    assert equal.segments[0].status == 'candidate'
    assert (equal.segments[0].end.price, equal.segments[0].pen_end_index) == (10.0, 2)
    assert equal.segments[0].end.revisions == 1
    assert [r.reason for r in equal.rejections] == [
        'superseded_by_more_extreme',
        'equal_extreme_kept_earlier',
    ]


def test_a_higher_high_after_confirmation_is_not_backfilled():
    """The confirmed segment is frozen; the new extreme starts the next structure."""
    prices = [4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5, 9.0]
    before = build(make_pens(prices, confirmed=8))
    segment = before.confirmed_segments[0]
    assert (segment.pen_end_index, segment.end.price) == (4, 10.0)
    assert segment.confirmed_on is not None
    assert segment.confirmed_on > segment.end.position_day

    # Extend with a 12.0 high; the frozen segment must not absorb it.
    longer = build(
        make_pens(prices + [5.0, 12.0, 6.0, 10.0, 7.0], confirmed=13)
    )
    assert longer.confirmed_segments[0] == segment
    assert longer.confirmed_segments[0].end.price == 10.0
    assert all(s.end.price != 12.0 for s in longer.confirmed_segments)
    assert longer.open_segment is not None
    assert longer.open_segment.status == 'candidate'


# ---------------------------------------------------------------------------
# 前三笔重叠 / pending
# ---------------------------------------------------------------------------

def test_three_pens_with_zero_intersection_are_not_a_segment():
    """p0 [5,10], p1 [4,10], p2 [4,4.5]: max low 5 > min high 4.5 — no overlap."""
    snap = build(make_pens([5.0, 10.0, 4.0, 4.5]))

    assert snap.segments == ()
    assert len(snap.pending) == 1
    pending = snap.pending[0]
    assert pending.status == 'blocked'
    assert pending.reason == 'first_three_pens_no_overlap'
    assert (pending.pen_start_index, pending.start_price) == (0, 5.0)
    assert pending.start_position_day == _pd(0)
    assert [r.reason for r in snap.rejections] == ['first_three_pens_no_overlap']
    # Nothing after the refusal is fabricated either.
    assert snap.open_segment is None

    # A zero-width touch (max low == min high) is not an overlap either.
    touching = build(make_pens([5.0, 10.0, 4.0, 5.0]))
    assert touching.segments == ()
    assert touching.pending[0].reason == 'first_three_pens_no_overlap'


def test_pending_until_three_pens_are_available_and_confirmed():
    two = build(make_pens([4.0, 8.0, 5.0]))
    assert two.pending[0].reason == 'awaiting_three_pens'
    assert two.pending[0].status == 'candidate'
    assert two.segments == ()

    third_open = build(make_pens([4.0, 8.0, 5.0, 9.0], confirmed=2))
    assert third_open.pending[0].reason == 'awaiting_three_confirmed_pens'
    assert third_open.pending[0].status == 'candidate'
    assert third_open.segments == ()

    # The day the third pen freezes, the same pens do form a segment start.
    third_frozen = build(make_pens([4.0, 8.0, 5.0, 9.0], confirmed=3))
    assert third_frozen.pending == ()
    assert third_frozen.segments[0].pen_start_index == 0


# ---------------------------------------------------------------------------
# 特征序列 包含处理
# ---------------------------------------------------------------------------

def test_inclusion_processing_is_sequential_and_non_transitive():
    # e0 [5,8], e1 [6,10], e2 [7,9.8]: e2 is contained in e1 only.  Sequential
    # processing merges e1+e2 into [7,10] and leaves e0 alone -> two elements.
    normalized, blocked = normalize_characteristic_sequence(
        [(8.0, 5.0), (10.0, 6.0), (9.8, 7.0)], direction='up'
    )
    assert blocked is None
    assert normalized == ((8.0, 5.0), (10.0, 7.0))
    # A transitive closure over the three ranges would collapse them into one.
    assert len(normalized) == 2
    # The first pair being contained is the ambiguous case, reported not guessed.
    assert normalize_characteristic_sequence(
        [(10.0, 5.0), (9.0, 6.0)], direction='up'
    ) == (((10.0, 5.0), (9.0, 6.0)), 'initial_containment_direction_ambiguous')

    # The same contained pair merges differently per segment direction:
    # an up segment keeps high-high / low-low ...
    assert normalize_characteristic_sequence(
        [(8.0, 5.0), (10.0, 6.0), (9.5, 7.0)], direction='up'
    ) == (((8.0, 5.0), (10.0, 7.0)), None)
    # ... while a down segment keeps low-low / high-high.
    assert normalize_characteristic_sequence(
        [(8.0, 5.0), (10.0, 6.0), (9.5, 7.0)], direction='down'
    ) == (((8.0, 5.0), (9.5, 6.0)), None)

    # No containment -> untouched, order preserved.
    assert normalize_characteristic_sequence(
        [(8.0, 5.0), (9.0, 6.0), (10.0, 7.0)], direction='up'
    ) == (((8.0, 5.0), (9.0, 6.0), (10.0, 7.0)), None)

    with pytest.raises(ValueError):
        normalize_characteristic_sequence([(10.0, 5.0)], direction='sideways')
    with pytest.raises(ValueError):
        normalize_characteristic_sequence([(4.0, 5.0)], direction='up')


def test_initial_containment_cannot_invent_a_merge_direction():
    """e0 [5,10], e1 [6,9]: e1 is inside e0 and no trend exists yet -> blocked."""
    snap = build(make_pens([5.0, 10.0, 5.0, 9.0, 6.0]))
    assert snap.confirmed_segments == ()
    assert len(snap.segments) == 1
    segment = snap.segments[0]
    assert segment.status == 'blocked'
    assert segment.reason == 'characteristic_initial_containment'
    assert segment.confirmed_on is None
    assert (segment.end.price, segment.end.position_day) == (10.0, _pd(1))
    assert [r.reason for r in snap.rejections] == ['characteristic_initial_containment']


# ---------------------------------------------------------------------------
# 缺口 (第67课第二种情况): blocked, never guessed
# ---------------------------------------------------------------------------

def test_a_gap_between_the_first_two_elements_blocks_the_segment():
    """e0 [5,8], e1 [9,12], e2 [8.5,11.5]: 顶分型 with a gap up above e0.

    第二种情况 needs the *second* characteristic sequence to print a fractal too;
    that is not implemented, so the segment must be reported blocked with no
    confirmation day rather than published as ended.
    """
    snap = build(make_pens([5.0, 8.0, 5.0, 12.0, 9.0, 11.5, 8.5]))

    assert snap.confirmed_segments == ()
    assert len(snap.segments) == 1
    segment = snap.segments[0]
    assert segment.status == 'blocked'
    assert segment.reason == 'characteristic_gap_case2'
    assert segment.confirmed_on is None
    assert (segment.end.price, segment.end.position_day) == (12.0, _pd(3))
    assert segment.pen_end_index == 2
    gap_rejections = [r for r in snap.rejections if r.reason == 'characteristic_gap_case2']
    assert len(gap_rejections) == 1
    assert (gap_rejections[0].pen_index, gap_rejections[0].price) == (2, 12.0)
    # The chain stops at a blocked boundary: nothing downstream is invented.
    assert snap.pending == ()
    assert snap.open_segment is None


def test_a_fractal_below_a_prior_extreme_is_blocked_not_republished():
    """Elements 12 / 9 / 10.5 / 9.5: the fractal sits at 10.5 but 12 came first.

    The segment's real extreme is the earlier 12.0, so the fractal at 10.5 may
    not be used as the segment's end (that would silently move the high).
    """
    snap = build(make_pens([5.0, 12.0, 6.0, 9.0, 5.0, 10.5, 7.0, 9.5, 6.5, 8.0], confirmed=8))
    assert snap.confirmed_segments == ()
    assert snap.segments[0].status == 'blocked'
    assert snap.segments[0].reason == 'characteristic_fractal_below_prior_extreme'
    assert snap.segments[0].confirmed_on is None


def test_a_down_segment_confirms_on_a_bottom_fractal_without_gap():
    """The symmetric path: 底分型 of an up-pen characteristic sequence.

    Points: top 10.0, bottom 6.0, top 8.5, bottom 4.5, top 7.0, bottom 5.5,
    top 7.8, bottom 5.0.  The down segment's characteristic sequence (up pens)
    is ``6.0/8.5``, ``4.5/7.0``, ``5.5/7.8``: a 底分型 at ``4.5/7.0`` with no
    gap (7.0 is not below the first element's low 6.0), so it ends at the
    bottom 4.5 (pivot 3) and waits for the third element (pen 5) to freeze.
    """
    snapshot = make_pens(
        [10.0, 6.0, 8.5, 4.5, 7.0, 5.5, 7.8, 5.0], confirmed=6, first_kind='top'
    )
    snap = build(snapshot)

    assert len(snap.confirmed_segments) == 1
    segment = snap.confirmed_segments[0]
    assert (segment.direction, segment.status, segment.reason) == (
        'down',
        'confirmed',
        'first_case_no_gap',
    )
    assert (segment.pen_start_index, segment.pen_end_index) == (0, 2)
    assert (segment.start.price, segment.start.position_day) == (10.0, _pd(0))
    assert (segment.end.price, segment.end.position_day) == (4.5, _pd(3))
    assert segment.confirmed_on == snapshot.pens[5].confirmed_on
    assert segment.end.kind == 'bottom'
    assert segment.characteristic_elements == 3

    # The following up segment exists but is not confirmed yet.
    follow = snap.open_segment
    assert follow is not None
    assert (follow.direction, follow.status) == ('up', 'candidate')
    assert (follow.start.price, follow.pen_start_index) == (4.5, 3)
    assert follow.end.price == 7.8


# ---------------------------------------------------------------------------
# tainted pens
# ---------------------------------------------------------------------------

def test_a_tainted_pen_in_the_span_blocks_the_segment():
    snapshot = make_pens(
        [4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5], statuses={3: 'blocked'}
    )
    snap = build(snapshot)
    assert snap.confirmed_segments == ()
    blocked = [s for s in snap.segments if s.status == 'blocked']
    assert len(blocked) == 1
    assert blocked[0].reason == 'tainted_pen'
    assert blocked[0].confirmed_on is None
    assert any(r.reason == 'tainted_pen' for r in snap.rejections)
    # Nothing after the taint is published.
    assert snap.pending == ()


def test_a_tainted_pen_inside_the_first_three_is_a_pending_block():
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0], statuses={1: 'blocked'})
    snap = build(snapshot)
    assert snap.segments == ()
    assert snap.pending[0].status == 'blocked'
    assert snap.pending[0].reason == 'tainted_pen'


# ---------------------------------------------------------------------------
# pen ownership
# ---------------------------------------------------------------------------

def test_segments_never_share_a_pen_and_join_at_endpoints():
    prices = [4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 8.5, 5.5, 9.0, 6.5]
    snap = build(make_pens(prices, confirmed=10))
    assert len(snap.segments) >= 2

    first, second = snap.segments[0], snap.segments[1]
    assert first.status == 'confirmed'
    assert first.pen_end_index + 1 == second.pen_start_index
    assert (first.end.position_day, first.end.price) == (
        second.start.position_day,
        second.start.price,
    )
    assert second.pen_end_index > second.pen_start_index
    # No pen is claimed by two segments.
    spans = [(s.pen_start_index, s.pen_end_index) for s in snap.segments]
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        assert a1 < b0


# ---------------------------------------------------------------------------
# purity / scope honesty
# ---------------------------------------------------------------------------

def test_the_kernel_is_pure_and_deterministic():
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5])
    pens_before = snapshot.pens
    first = build(snapshot)
    second = build(snapshot)
    assert first == second
    assert snapshot.pens == pens_before


def test_module_scope_is_honest():
    import src.core.chan_segment as mod

    doc = mod.__doc__ or ''
    assert 'Not implemented' in doc
    assert '第二种情况' in doc
    assert '中枢' in doc  # explicitly out of scope, not accidentally claimed
    for forbidden in ('zhongshu', 'centre', 'center', 'pivot', 'buy_sell'):
        assert not any(forbidden in name.lower() for name in dir(mod)), forbidden
    assert mod.SEGMENT_KERNEL_VERSION == 'chan-segment-v1'
