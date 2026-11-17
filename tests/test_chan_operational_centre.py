"""Chan operational centre (操作中枢) prototype over externally supplied segments.

What these tests pin, and why:

* The input is a caller-supplied chain of **completed, confirmed, connected,
  alternating** segments.  Nothing here generates segments, so the strict 18th
  lesson centre (a completed sub-level 走势类型) is *not* what this computes:
  every snapshot says ``strictness='operational_not_strict'`` and
  ``segments_engine='none'``, i.e. a segment-a0 engine is not wired in.
* Geometry is the strict positive-width intersection of three consecutive
  ranges (``zd = max(lows)``, ``zg = min(highs)``).  Zero width is *no centre*,
  not a centre of width 0.
* The structural start day (first segment's start) is kept **separate** from the
  day the centre becomes knowable (third segment's ``confirmed_on``).  The raw
  time is never dressed up as a confirmation.
* Nothing foreign is trusted: identity digest, ordering, connection, alternation,
  confirmation timing (never before the evidence, never after ``as_of_day``),
  finite prices and non-zero length are re-verified, and a failure raises instead
  of being repaired.
* The lifecycle is conservative: ``candidate`` until something tests the zone,
  ``extended`` while price keeps touching it — a touch of the edge is a return,
  and crossing *through* the zone is not a departure — ``unknown`` when there is
  a departure but no reverse segment yet, and ``broken`` only when a departure is
  followed by a reverse segment that stays wholly outside.
* Prefix replay is checked against a hand-written oracle, so no later
  information can leak backwards, and a formed centre's numbers never move.
"""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.core.chan_operational_centre import (
    BASIS,
    CENTRE_KERNEL_VERSION,
    INPUT_KIND,
    SEGMENTS_ENGINE,
    STRICTNESS,
    CentreState,
    ExternalSegment,
    OperationalCentre,
    OperationalCentreSnapshot,
    SegmentChain,
    centre_from_chain,
    discover_centres,
    higher_level_centre,
    replay_prefixes,
    segments_from_pens,
    trend_from_centres,
)

_START = date(2026, 3, 2)


def _day(offset: int) -> str:
    return (_START + timedelta(days=offset)).isoformat()


def build(
    points,
    *,
    confs,
    span: int = 10,
    start: int = 0,
    instrument: str = 'SZ.300000',
    version: str = 'snap-1',
    basis: str = BASIS,
):
    """Connected alternating chain: one segment per (start_price, end_price) leg.

    Direction is derived from the prices, so a chain built here is always
    internally consistent; individual fields are mutated with ``replace`` in the
    fail-closed tests.  ``confs`` are day offsets, one per segment.
    """
    segments = []
    for i, (p0, p1) in enumerate(points):
        low, high = (p0, p1) if p0 <= p1 else (p1, p0)
        segments.append(
            ExternalSegment(
                index=i,
                direction='up' if p1 > p0 else 'down',
                start_day=_day(start + span * i),
                start_price=float(p0),
                end_day=_day(start + span * (i + 1)),
                end_price=float(p1),
                high=float(high),
                low=float(low),
                status='confirmed',
                confirmed_on=_day(confs[i]),
            )
        )
    return SegmentChain(
        instrument=instrument,
        snapshot_version=version,
        basis=basis,
        as_of_day=_day(confs[-1]),
        segments=tuple(segments),
    )


# Three segments whose ranges are [10,20] [12,20] [12,18] -> zd=12, zg=18.
_BASE = [(10, 20), (20, 12), (12, 18)]
_BASE_CONFS = [12, 22, 33]

# Zone [12,20] (third leg overshoots to 25), then two legs that only reach back
# to zg=20: a single-point intersection each time.
TOUCH_POINTS = [(10, 20), (20, 12), (12, 25), (25, 20), (20, 26)]
TOUCH_CONFS = [12, 22, 33, 42, 52]

# Zone [12,20] (third leg already ends above the zone), then fully above legs.
BREAK_POINTS = [(10, 20), (20, 12), (12, 25), (25, 22), (22, 28), (28, 21)]
BREAK_CONFS = [12, 22, 33, 42, 52, 62]

# Zone [12,18]; keeps returning into the zone, last leg crosses through it.
EXTEND_POINTS = _BASE + [(18, 13), (13, 19), (19, 11)]
EXTEND_CONFS = _BASE_CONFS + [42, 52, 62]

# Zero-width: [17,18] [16,18] [16,17] -> zd=17, zg=17.  A later triple
# ([16,18] [16,17] [12,17] -> width 1) would qualify, and must not be discovered.
ZERO_POINTS = [(17, 18), (18, 16), (16, 17), (17, 12)]
ZERO_CONFS = [12, 22, 33, 42]

# Hand-written per-prefix oracle: (length, (zd, zg) or None, state, visible offset).
PREFIX_ORACLE = {
    'touch': [
        (1, None, 'insufficient_segments', None),
        (2, None, 'insufficient_segments', None),
        (3, (12.0, 20.0), 'candidate', 33),
        # 25 -> 20 stops exactly on zg=20: one point, not a departure.
        (4, (12.0, 20.0), 'extended', 42),
        (5, (12.0, 20.0), 'extended', 52),
    ],
    'break': [
        (1, None, 'insufficient_segments', None),
        (2, None, 'insufficient_segments', None),
        (3, (12.0, 20.0), 'candidate', 33),
        (4, (12.0, 20.0), 'unknown', 42),
        (5, (12.0, 20.0), 'broken', 52),
        (6, (12.0, 20.0), 'broken', 52),
    ],
    'extend': [
        (1, None, 'insufficient_segments', None),
        (2, None, 'insufficient_segments', None),
        (3, (12.0, 18.0), 'candidate', 33),
        (4, (12.0, 18.0), 'extended', 42),
        (5, (12.0, 18.0), 'extended', 52),
        (6, (12.0, 18.0), 'extended', 62),
    ],
}

_CHAINS = [
    ('touch', TOUCH_POINTS, TOUCH_CONFS),
    ('break', BREAK_POINTS, BREAK_CONFS),
    ('extend', EXTEND_POINTS, EXTEND_CONFS),
]


# --------------------------------------------------------------------------
# labels: an operational prototype, never a strict Chan centre
# --------------------------------------------------------------------------
def test_labels_say_operational_and_no_segment_engine():
    assert CENTRE_KERNEL_VERSION == 'chan-operational-centre-v1'
    assert INPUT_KIND == 'externally_supplied_completed_segments'
    assert BASIS == 'segment_a0'
    assert STRICTNESS == 'operational_not_strict'
    assert SEGMENTS_ENGINE == 'none'

    snap = centre_from_chain(build(EXTEND_POINTS, confs=EXTEND_CONFS))
    assert isinstance(snap, OperationalCentreSnapshot)
    assert isinstance(snap.centre, OperationalCentre)
    assert snap.kernel_version == CENTRE_KERNEL_VERSION
    assert snap.input_kind == INPUT_KIND
    assert snap.basis == BASIS
    assert snap.strictness == STRICTNESS
    assert snap.segments_engine == 'none'
    assert snap.segment_count == len(EXTEND_POINTS)
    # The centre itself carries the labels too, so it cannot be detached from
    # the snapshot and then passed off as a strict centre.
    assert snap.centre.strictness == STRICTNESS
    assert snap.centre.input_kind == INPUT_KIND
    assert snap.centre.basis == BASIS


# --------------------------------------------------------------------------
# geometry and the two different clocks
# --------------------------------------------------------------------------
def test_geometry_is_strict_positive_width_intersection_of_three_ranges():
    snap = centre_from_chain(build(EXTEND_POINTS, confs=EXTEND_CONFS))
    centre = snap.centre
    # [10,20] n [12,20] n [12,18]
    assert centre.zd == 12.0
    assert centre.zg == 18.0
    assert centre.width == 6.0
    assert centre.segment_indices == (0, 1, 2)
    assert centre.instrument == 'SZ.300000'
    assert centre.snapshot_version == 'snap-1'


def test_structural_start_is_not_presented_as_confirmation():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    centre = centre_from_chain(chain).centre
    assert centre.structure_start_day == _day(0)
    assert centre.structure_start_price == 10.0
    # Knowable only when the third segment is confirmed, never earlier.
    assert centre.confirmed_on == chain.segments[2].confirmed_on == _day(33)
    assert centre.structure_start_day < centre.confirmed_on
    assert centre.confirmed_on != chain.segments[0].start_day


def test_centre_is_invisible_before_the_third_segment_is_confirmed():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    early = replace(chain, as_of_day=_day(32))  # one day before 33
    with pytest.raises(ValueError, match='after as_of_day'):
        centre_from_chain(early)


# --------------------------------------------------------------------------
# no centre: zero width, and no auto-discovery of a later triple
# --------------------------------------------------------------------------
def test_zero_width_intersection_yields_no_centre():
    assert (ZERO_POINTS[0], ZERO_POINTS[1], ZERO_POINTS[2]) == ((17, 18), (18, 16), (16, 17))
    snap = centre_from_chain(build(ZERO_POINTS, confs=ZERO_CONFS))
    assert snap.centre is None
    assert snap.state is None
    assert snap.absence_reason == 'zero_width_intersection'


def test_later_qualifying_triple_is_not_discovered():
    # Segments 1..3 would give zd=16, zg=17 (width 1) but only the leading
    # triple forms a centre: searching further is centre discovery, not ours.
    snap = centre_from_chain(build(ZERO_POINTS, confs=ZERO_CONFS))
    assert snap.centre is None
    assert snap.absence_reason == 'zero_width_intersection'

    shorter = centre_from_chain(build(ZERO_POINTS[:2], confs=ZERO_CONFS[:2]))
    assert shorter.centre is None
    assert shorter.absence_reason == 'insufficient_segments'


# --------------------------------------------------------------------------
# fail-closed input validation
# --------------------------------------------------------------------------
def test_direction_must_alternate():
    chain = build([(10, 15), (15, 20), (20, 12)], confs=[12, 22, 33])
    with pytest.raises(ValueError, match='alternate'):
        centre_from_chain(chain)


def test_anchor_must_be_shared_by_neighbouring_segments():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    broken = list(chain.segments)
    broken[1] = replace(broken[1], start_price=21.0, high=21.0, low=12.0)
    with pytest.raises(ValueError, match='share an anchor price'):
        centre_from_chain(replace(chain, segments=tuple(broken)))


def test_segments_must_be_connected_in_time():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    broken = list(chain.segments)
    broken[1] = replace(broken[1], start_day=_day(11))
    with pytest.raises(ValueError, match='connected in time'):
        centre_from_chain(replace(chain, segments=tuple(broken)))


def test_confirmation_before_its_own_end_day_is_refused():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    broken = list(chain.segments)
    broken[2] = replace(broken[2], confirmed_on=_day(25))  # ends on 30
    with pytest.raises(ValueError, match='never precede the evidence'):
        centre_from_chain(replace(chain, segments=tuple(broken)))


def test_confirmation_order_cannot_go_backwards():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    broken = list(chain.segments)
    broken[1] = replace(broken[1], confirmed_on=_day(42))
    broken[2] = replace(broken[2], confirmed_on=_day(33))
    drifted = replace(chain, segments=tuple(broken), as_of_day=_day(60))
    with pytest.raises(ValueError, match='confirmed before the previous segment'):
        centre_from_chain(drifted)


def test_candidate_segment_is_refused():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    broken = list(chain.segments)
    broken[2] = replace(broken[2], status='candidate', confirmed_on=None)
    with pytest.raises(ValueError, match='not confirmed'):
        centre_from_chain(replace(chain, segments=tuple(broken)))


def test_zero_length_segment_is_refused():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    broken = list(chain.segments)
    broken[1] = replace(broken[1], end_price=20.0, high=20.0, low=20.0)
    with pytest.raises(ValueError, match='zero length'):
        centre_from_chain(replace(chain, segments=tuple(broken)))


def test_non_finite_price_is_refused():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    broken = list(chain.segments)
    broken[0] = replace(broken[0], high=float('inf'))
    with pytest.raises(ValueError, match='finite price'):
        centre_from_chain(replace(chain, segments=tuple(broken)))
    broken[0] = replace(broken[0], start_price=float('nan'))
    with pytest.raises(ValueError, match='finite price'):
        centre_from_chain(replace(chain, segments=tuple(broken)))


def test_range_must_cover_its_endpoints():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    broken = list(chain.segments)
    broken[0] = replace(broken[0], high=15.0)  # endpoint is 20
    with pytest.raises(ValueError, match='cover both endpoints'):
        centre_from_chain(replace(chain, segments=tuple(broken)))


def test_snapshot_digest_mismatch_is_refused():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    # Matching identity is fine.
    centre_from_chain(
        chain,
        expect_instrument=chain.instrument,
        expect_snapshot_version=chain.snapshot_version,
    )
    with pytest.raises(ValueError, match='digest'):
        centre_from_chain(chain, expect_snapshot_version='snap-2')


def test_instrument_mismatch_is_refused():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    with pytest.raises(ValueError, match='instrument'):
        centre_from_chain(chain, expect_instrument='SH.600000')


def test_basis_must_be_segment_a0():
    with pytest.raises(ValueError, match='basis'):
        centre_from_chain(build(EXTEND_POINTS, confs=EXTEND_CONFS, basis='pen_a0'))


def test_segment_index_must_match_its_position():
    chain = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    broken = list(chain.segments)
    broken[1] = replace(broken[1], index=7)
    with pytest.raises(ValueError, match='index'):
        centre_from_chain(replace(chain, segments=tuple(broken)))


# --------------------------------------------------------------------------
# lifecycle: candidate / extended / unknown / broken
# --------------------------------------------------------------------------
def _centre_and_state(snapshot):
    """Narrow the optional fields: a lifecycle state is meaningless without a centre."""
    assert snapshot.centre is not None
    assert snapshot.state is not None
    return snapshot.centre, snapshot.state


def test_candidate_until_something_tests_the_zone():
    snap = replay_prefixes(build(EXTEND_POINTS, confs=EXTEND_CONFS))[2]  # 3 segments
    _centre, state = _centre_and_state(snap)
    assert isinstance(state, CentreState)
    assert state.state == 'candidate'
    assert state.reason == 'centre_formed_not_tested'
    assert state.visible_on == _day(33)
    assert state.evidence_segment_indices == ()


def test_touching_the_edge_is_a_return_not_a_break():
    # 25 -> 20 stops exactly on zg=20: the intersection is the single point 20,
    # and a touch still counts as being in the zone.
    snap = replay_prefixes(build(TOUCH_POINTS, confs=TOUCH_CONFS))[3]
    centre, state = _centre_and_state(snap)
    assert (centre.zd, centre.zg) == (12.0, 20.0)
    assert state.state == 'extended'
    assert state.reason == 'touched_edge'
    assert state.visible_on == _day(42)
    assert state.state != 'broken'


def test_departure_without_a_reverse_segment_is_unknown():
    # 25 -> 22 lies wholly above zg=20, but nothing has yet refused to return.
    snap = replay_prefixes(build(BREAK_POINTS, confs=BREAK_CONFS))[3]
    _centre, state = _centre_and_state(snap)
    assert state.state == 'unknown'
    assert state.reason == 'departure_without_reversal'
    assert state.visible_on == _day(42)
    assert state.evidence_segment_indices == (3,)


def test_break_needs_a_departure_and_a_reverse_segment_wholly_outside():
    # 22 -> 28 reverses with its low at 22 > zg=20: no pullback at all.
    snapshots = replay_prefixes(build(BREAK_POINTS, confs=BREAK_CONFS))
    snap = snapshots[4]  # five segments: departure (3) + reversal (4)
    _centre, state = _centre_and_state(snap)
    assert state.state == 'broken'
    assert state.reason == 'left_without_return'
    assert state.visible_on == _day(52)
    assert state.evidence_segment_indices == (3, 4)
    # Broken is terminal: the later prefix must not resurrect or re-time it.
    _centre, later = _centre_and_state(snapshots[-1])
    assert later.state == 'broken'
    assert later.visible_on == _day(52)
    _centre, earlier = _centre_and_state(snapshots[3])
    assert earlier.state == 'unknown'
    assert earlier.visible_on == _day(42)


def test_return_into_the_zone_interior_is_an_extension():
    # 18 -> 13 comes back inside [12,18]: a real overlap, not a departure.
    snap = replay_prefixes(build(EXTEND_POINTS, confs=EXTEND_CONFS))[3]
    centre, state = _centre_and_state(snap)
    assert state.state == 'extended'
    assert state.reason == 'returned_to_zone'
    assert state.visible_on == _day(42)
    assert centre.zd < 13.0 < centre.zg
    # The snapshot's visibility is the evidence date, never the raw start.
    assert snap.visible_on == state.visible_on > centre.structure_start_day


def test_crossing_through_the_zone_is_not_a_break():
    # 19 -> 11 straddles the zone (19 > zg, 11 < zd): it went through, it did
    # not stay away, so the centre is still in force.
    snap = centre_from_chain(build(EXTEND_POINTS, confs=EXTEND_CONFS))
    _centre, state = _centre_and_state(snap)
    assert state.state == 'extended'
    assert state.reason == 'crossed_through'
    assert state.state != 'broken'


def test_state_is_timestamped_by_its_evidence_not_by_the_raw_time():
    chain = build(BREAK_POINTS, confs=BREAK_CONFS)
    snap = centre_from_chain(chain)
    centre, state = _centre_and_state(snap)
    assert state.visible_on == chain.segments[4].confirmed_on
    assert state.visible_on > centre.structure_start_day
    assert state.visible_on <= snap.as_of_day


# --------------------------------------------------------------------------
# prefix replay: hand oracle, stability, drift
# --------------------------------------------------------------------------
@pytest.mark.parametrize('name,points,confs', _CHAINS)
def test_prefix_replay_matches_hand_oracle(name, points, confs):
    chain = build(points, confs=confs)
    snapshots = replay_prefixes(chain)
    assert len(snapshots) == len(points)
    for snap, (length, zone, state, visible) in zip(snapshots, PREFIX_ORACLE[name]):
        assert snap.segment_count == length
        assert snap.as_of_day == _day(confs[length - 1])
        if zone is None:
            assert snap.centre is None
            assert snap.state is None
            assert snap.absence_reason == state
            continue
        assert snap.absence_reason is None
        assert (snap.centre.zd, snap.centre.zg) == zone
        assert snap.state.state == state
        assert snap.state.visible_on == _day(visible)
        assert snap.centre.confirmed_on <= snap.state.visible_on
        assert snap.state.visible_on <= snap.as_of_day


@pytest.mark.parametrize('name,points,confs', _CHAINS)
def test_prefix_geometry_never_changes_once_formed(name, points, confs):
    snapshots = replay_prefixes(build(points, confs=confs))
    formed = [s for s in snapshots if s.centre is not None]
    assert len(formed) == len(points) - 2
    first = formed[0].centre
    for snap in formed[1:]:
        assert snap.centre == first  # frozen dataclass equality, all fields


def test_confirmation_drift_moves_visibility_but_not_geometry():
    base = build(EXTEND_POINTS, confs=EXTEND_CONFS)
    shifted = replace(
        base,
        segments=tuple(
            replace(s, confirmed_on=_day(EXTEND_CONFS[i] + 7)) for i, s in enumerate(base.segments)
        ),
        as_of_day=_day(EXTEND_CONFS[-1] + 7),
    )
    a = centre_from_chain(base)
    b = centre_from_chain(shifted)
    assert (a.centre.zd, a.centre.zg) == (b.centre.zd, b.centre.zg)
    assert a.centre.structure_start_day == b.centre.structure_start_day
    assert a.centre.structure_start_price == b.centre.structure_start_price
    assert a.centre.segment_indices == b.centre.segment_indices
    assert b.centre.confirmed_on == _day(33 + 7)
    assert b.state.visible_on == _day(62 + 7)
    # ... and the drifted chain is still invisible before its own evidence.
    with pytest.raises(ValueError, match='after as_of_day'):
        centre_from_chain(replace(shifted, as_of_day=_day(33 + 6)))


def test_replay_prefixes_ignores_later_segments():
    chain = build(BREAK_POINTS, confs=BREAK_CONFS)
    truncated = replace(chain, segments=chain.segments[:4], as_of_day=chain.segments[3].confirmed_on)
    from_full = replay_prefixes(chain)[3]
    from_truncated = replay_prefixes(truncated)[3]
    assert from_full.centre == from_truncated.centre
    assert from_full.state == from_truncated.state
    assert from_full.as_of_day == from_truncated.as_of_day


# --------------------------------------------------------------------------
# explicitly not implemented
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    'call,match',
    [
        (lambda: segments_from_pens(()), 'segment'),
        (lambda: discover_centres(build(EXTEND_POINTS, confs=EXTEND_CONFS)), 'discover'),
        (lambda: higher_level_centre(centre_from_chain(build(EXTEND_POINTS, confs=EXTEND_CONFS)).centre), 'level'),
        (lambda: trend_from_centres(()), 'trend'),
    ],
)
def test_not_implemented_surfaces_are_explicit(call, match):
    with pytest.raises(NotImplementedError, match=match):
        call()
