"""Chan v1 pen (笔) kernel: causal, versioned, conservative.

The pen is the *first* structure above fractals, so these tests pin the hard
parts rather than a happy path:

* a reverse fractal alone never confirms anything — an endpoint is frozen only
  when a **valid** opposite pen exists from it, so a later higher high can still
  extend the pen that a naive implementation would have called immutable;
* the same-direction rules are explicit: a more extreme fractal drifts the open
  endpoint (revision is recorded), an equal extreme keeps the earlier one;
* a too-short centre gap (``< 4`` merged bars) or a failed price gate rejects
  the pair instead of guessing;
* a window tainted by the unknown-direction envelope yields ``blocked``
  structure, never ``confirmed``;
* a *foreign* snapshot is re-verified rather than trusted: window completeness,
  strict three-bar geometry, extremum day/price, status vs seal state, and
  ``confirmed_on`` exactly equal to the right neighbour's ``sealed_on`` — so a
  caller cannot publish a pen confirmed before the bar that proves it;
* every prefix replays exactly, so no confirmed pen may ever change afterwards,
  and a fixture with real (non-empty) confirmed pens is pinned against a
  hand-derived earliest-confirmation-day oracle;
* the rule set is versioned and every field — including ``min_index_gap`` — is
  pinned to v1, so the version string can only ever mean one rule set.
"""
from datetime import date, timedelta

import pytest

from src.core.chan_pen import (
    MIN_INDEX_GAP,
    PEN_KERNEL_VERSION,
    PenRules,
    build_chan_pen,
    pen_from_snapshot,
)
from src.core.chan_structure import Fractal, RawBar, build_chan_structure

from dataclasses import replace


_START = date(2026, 1, 1)


def bars_of(ranges):
    """One raw bar per (high, low) tuple, starting 2026-01-01."""
    out = []
    for i, (high, low) in enumerate(ranges):
        day = (_START + timedelta(days=i)).isoformat()
        out.append(RawBar(day=day, open=low, high=high, low=low, close=high))
    return out


# A clean zig-zag where every raw bar becomes its own merged bar, so merged
# index == raw index and the fractal centres are easy to reason about:
# bottom@1, top@5, bottom@9, top@13 (four merged-bar centre gaps, >= 4).
ZIGZAG = [
    (6.0, 5.0),   # 0
    (5.0, 4.0),   # 1  bottom
    (5.5, 4.5),   # 2
    (6.5, 5.5),   # 3
    (7.5, 6.5),   # 4
    (8.5, 7.5),   # 5  top
    (8.0, 7.0),   # 6
    (7.0, 6.0),   # 7
    (6.5, 5.5),   # 8
    (6.0, 5.0),   # 9  bottom
    (7.0, 6.0),   # 10
    (7.5, 6.5),   # 11
    (8.0, 7.0),   # 12
    (9.0, 8.0),   # 13 top
    (8.5, 7.5),   # 14
    (9.5, 8.5),   # 15  seals merged bar 14
]


def test_zigzag_assumption_is_pinned():
    """The fixtures below rely on merged index == raw index."""
    snap = build_chan_structure(bars_of(ZIGZAG))
    assert [m.index for m in snap.merged_bars] == list(range(len(ZIGZAG)))
    assert [
        (f.kind, f.position_merged_index, f.position_day, f.position_price, f.status, f.confirmed_on)
        for f in snap.fractals
    ] == [
        ('bottom', 1, '2026-01-02', 4.0, 'confirmed', '2026-01-04'),
        ('top', 5, '2026-01-06', 8.5, 'confirmed', '2026-01-08'),
        ('bottom', 9, '2026-01-10', 5.0, 'confirmed', '2026-01-12'),
        ('top', 13, '2026-01-14', 9.0, 'confirmed', '2026-01-16'),
        # Still an open tail: only a candidate, so it must not become structure.
        ('bottom', 14, '2026-01-15', 7.5, 'candidate', None),
    ]


# ---------------------------------------------------------------------------
# versioning / fail-closed rules
# ---------------------------------------------------------------------------

def test_rules_are_versioned_and_unknown_rules_are_refused():
    rules = PenRules()
    assert rules.kernel_version == PEN_KERNEL_VERSION == 'chan-pen-v1'
    assert rules.min_index_gap == MIN_INDEX_GAP == 4
    assert rules.price_gate == 'top_high_above_bottom_high'
    assert rules.equal_extreme == 'keep_earlier'

    with pytest.raises(ValueError):
        PenRules(kernel_version='chan-pen-v2')
    # Fail-closed: a laxer spacing rule is not silently accepted.
    with pytest.raises(ValueError):
        PenRules(min_index_gap=3)
    # v1 spacing is *pinned*, not a floor: 5 is a different rule and would make a
    # snapshot labelled chan-pen-v1 mean two things at once.
    with pytest.raises(ValueError):
        PenRules(min_index_gap=5)
    with pytest.raises(ValueError):
        PenRules(min_index_gap=4, kernel_version='chan-pen-v1.1')
    with pytest.raises(ValueError):
        PenRules(price_gate='anything_goes')
    with pytest.raises(ValueError):
        PenRules(equal_extreme='keep_later')
    # The default stays exactly v1, so the pin cannot drift silently.
    assert PenRules(min_index_gap=4) == PenRules()


def test_snapshot_carries_the_rule_set_it_was_built_with():
    rules = PenRules()
    snap = build_chan_pen(bars_of(ZIGZAG), rules=rules)
    assert snap.kernel_version == PEN_KERNEL_VERSION
    assert snap.rules == rules
    assert snap.rules.min_index_gap == MIN_INDEX_GAP
    # A version string alone must not be enough to claim a different rule set.
    with pytest.raises(ValueError):
        build_chan_pen(bars_of(ZIGZAG), rules=PenRules(min_index_gap=5))


# ---------------------------------------------------------------------------
# pen construction
# ---------------------------------------------------------------------------

def test_pens_are_built_only_on_confirmed_fractals_with_gap_at_least_four():
    snap = build_chan_pen(bars_of(ZIGZAG))

    assert [(p.start.merged_index, p.end.merged_index, p.status) for p in snap.pens] == [
        (1, 5, 'confirmed'),
        (5, 9, 'confirmed'),
        (9, 13, 'candidate'),
    ]
    first, second, third = snap.pens
    assert (first.direction, first.start.kind, first.end.kind) == ('up', 'bottom', 'top')
    assert (first.start_price, first.end_price) == (4.0, 8.5)
    assert (second.direction, second.start_price, second.end_price) == ('down', 8.5, 5.0)
    assert (third.direction, third.start_price, third.end_price) == ('up', 5.0, 9.0)
    assert [p.index_gap for p in snap.pens] == [4, 4, 4]
    assert [p.kernel_version for p in snap.pens] == [PEN_KERNEL_VERSION] * 3

    # The open tail is a candidate; the two earlier pens are frozen.
    assert [p.status for p in snap.confirmed_pens] == ['confirmed', 'confirmed']
    assert [p.status for p in snap.candidate_pens] == ['candidate']
    assert third.confirmed_on is None
    assert snap.open_endpoint.merged_index == 13
    assert snap.open_endpoint.status == 'candidate'


def test_a_reverse_fractal_alone_does_not_confirm_the_pen():
    """The pen 1->5 exists, but it is not immutable until a valid leg 5->9.

    This is the guard against treating the first reverse fractal as a fixed
    pen: the top may still drift to a higher high while nothing legal hangs
    below it.
    """
    up_to_top = bars_of(ZIGZAG[:14])  # top@13 is still only a candidate here
    snap = build_chan_pen(up_to_top)
    assert [(e.merged_index, e.status) for e in snap.endpoints] == [
        (1, 'confirmed'),
        (5, 'confirmed'),
        (9, 'candidate'),
    ]
    assert [(p.start.merged_index, p.end.merged_index, p.status) for p in snap.pens] == [
        (1, 5, 'confirmed'),
        (5, 9, 'candidate'),
    ]
    assert snap.pens[0].confirmed_on == '2026-01-12'  # the day 9's fractal froze

    # Only after the top@13 fractal is known does 5's pen neighbour become a
    # confirmed leg, and only then is 9 frozen.
    grown = build_chan_pen(bars_of(ZIGZAG))
    assert grown.pens[1].status == 'confirmed'
    assert grown.pens[1].confirmed_on == '2026-01-16'
    assert grown.pens[0] == snap.pens[0]


def test_confirmed_on_never_precedes_the_endpoint_position():
    snap = build_chan_pen(bars_of(ZIGZAG))
    for pen in snap.pens:
        for endpoint in (pen.start, pen.end):
            if endpoint.confirmed_on is not None:
                assert endpoint.confirmed_on >= endpoint.position_day
        if pen.confirmed_on is not None:
            assert pen.confirmed_on >= pen.end.position_day


# ---------------------------------------------------------------------------
# same-direction selection: more extreme / equal / less extreme
# ---------------------------------------------------------------------------

DRIFT = [
    (6.0, 5.0),   # 0
    (5.0, 4.0),   # 1  bottom
    (5.5, 4.5),   # 2
    (6.5, 5.5),   # 3
    (7.5, 6.5),   # 4
    (8.5, 7.5),   # 5  top
    (8.2, 7.2),   # 6
    (9.5, 8.5),   # 7  higher top: same direction, more extreme
    (9.0, 8.0),   # 8
    (8.6, 7.6),   # 9  seals merged bar 8, so the top@7 fractal is confirmed
]


def test_same_direction_more_extreme_moves_the_open_endpoint():
    snap = build_chan_pen(bars_of(DRIFT))
    assert len(snap.endpoints) == 2
    bottom, top = snap.endpoints
    assert (bottom.merged_index, bottom.status) == (1, 'confirmed')
    assert (top.kind, top.merged_index, top.price) == ('top', 7, 9.5)
    assert top.status == 'candidate'
    # The move is auditable, never silent.
    assert top.revisions == 1
    assert top.drift_days == ('2026-01-06',)
    assert [(p.start_price, p.end_price, p.status) for p in snap.pens] == [(4.0, 9.5, 'candidate')]
    assert snap.pens[0].index_gap == 6
    # The superseded fractal is reported, not deleted.
    superseded = [r for r in snap.rejections if r.reason == 'superseded_by_more_extreme']
    assert [(r.merged_index, r.position_day, r.price) for r in superseded] == [(5, '2026-01-06', 8.5)]


EQUAL = [
    (6.0, 5.0),   # 0
    (5.0, 4.0),   # 1  bottom
    (5.5, 4.5),   # 2
    (6.5, 5.5),   # 3
    (7.5, 6.5),   # 4
    (8.5, 7.5),   # 5  top
    (8.2, 7.2),   # 6
    (8.5, 7.6),   # 7  equal high: keep the earlier extreme
    (8.0, 7.0),   # 8
    (7.8, 6.8),   # 9  seals merged bar 8
]


def test_same_direction_equal_extreme_keeps_the_earlier_endpoint():
    snap = build_chan_pen(bars_of(EQUAL))
    top = snap.endpoints[-1]
    assert (top.merged_index, top.price, top.position_day) == (5, 8.5, '2026-01-06')
    assert top.revisions == 0
    assert top.drift_days == ()
    equal = [r for r in snap.rejections if r.reason == 'equal_extreme_kept_earlier']
    assert [(r.merged_index, r.price) for r in equal] == [(7, 8.5)]


LESS_EXTREME = [
    (6.0, 5.0),   # 0
    (5.0, 4.0),   # 1  bottom
    (5.5, 4.5),   # 2
    (6.5, 5.5),   # 3
    (7.5, 6.5),   # 4
    (8.5, 7.5),   # 5  top
    (7.5, 6.5),   # 6
    (8.0, 7.0),   # 7  lower high: kept out, endpoint unchanged
    (7.8, 6.8),   # 8
    (7.6, 6.6),   # 9  seals merged bar 8
]


def test_same_direction_less_extreme_is_rejected_without_moving_anything():
    snap = build_chan_pen(bars_of(LESS_EXTREME))
    top = snap.endpoints[-1]
    assert (top.merged_index, top.price, top.revisions) == (5, 8.5, 0)
    # bottom@6 is only one bar from the top@5: refused for spacing first, then
    # the lower high at 7 is refused as "not more extreme".
    assert [(r.merged_index, r.reason) for r in snap.rejections] == [
        (6, 'spacing'),
        (7, 'not_more_extreme'),
    ]


# A drifting endpoint that is frozen much later than it first appears:
# bottom@1, top@5, the top drifts to the higher high @7, then bottom@12, top@16.
# Every raw bar is its own merged bar, so merged index == raw index == day offset.
PEN_DRIFT_ORACLE = [
    (6.0, 5.0),   # 0
    (5.0, 4.0),   # 1  bottom, window (0,1,2) sealed by bar 3 -> confirmed 01-04
    (5.5, 4.5),   # 2
    (6.5, 5.5),   # 3
    (7.5, 6.5),   # 4
    (8.5, 7.5),   # 5  top, window (4,5,6) sealed by bar 7 -> confirmed 01-08
    (8.2, 7.2),   # 6  a genuine bottom between the two tops — too close to top@5,
                  #    so the kernel rejects it for spacing instead of re-anchoring
    (9.5, 8.5),   # 7  higher high: the open top endpoint drifts here
    (9.0, 8.0),   # 8
    (8.6, 7.6),   # 9  seals merged bar 8 -> the top@7 fractal is confirmed 01-10
    (7.5, 6.5),   # 10
    (6.5, 5.5),   # 11
    (6.0, 5.0),   # 12 bottom
    (6.5, 5.5),   # 13
    (7.0, 6.0),   # 14 seals merged bar 13 -> bottom@12 confirmed 01-15
    (7.5, 6.5),   # 15
    (8.5, 7.5),   # 16 top
    (8.0, 7.0),   # 17
    (7.9, 6.9),   # 18 seals merged bar 17 -> top@16 confirmed 01-19
]


def test_a_drifting_endpoint_is_frozen_on_its_evidence_day_and_never_before():
    """Hand oracle for the earliest day each *confirmed* pen may exist.

    The evidence days are read off the raw list, never off the kernel:

    * merged bar 6 is sealed by raw bar 7, so the top@5 fractal is known 01-08;
    * merged bar 8 is sealed by raw bar 9, so the drifting top@7 is known 01-10;
    * merged bar 13 is sealed by raw bar 14, so bottom@12 — the first legal
      opposite leg below the drifting top — is known 01-15.  That is the first
      day the pen 1 -> 7 can be *confirmed*;
    * merged bar 17 is sealed by raw bar 18, so top@16 is known 01-19, which is
      the first day the pen 7 -> 12 can be confirmed.

    So the two confirmed pens are real, drift-stable, and one of them waits five
    days after its endpoint's own fractal before it may be published.
    """
    bars = bars_of(PEN_DRIFT_ORACLE)
    structure = build_chan_structure(bars)
    assert [m.index for m in structure.merged_bars] == list(range(len(PEN_DRIFT_ORACLE)))
    assert [
        (f.kind, f.position_merged_index, f.status, f.confirmed_on)
        for f in structure.fractals
    ] == [
        ('bottom', 1, 'confirmed', '2026-01-04'),
        ('top', 5, 'confirmed', '2026-01-08'),
        ('bottom', 6, 'confirmed', '2026-01-09'),  # too close: spacing-rejected
        ('top', 7, 'confirmed', '2026-01-10'),
        ('bottom', 12, 'confirmed', '2026-01-15'),
        ('top', 16, 'confirmed', '2026-01-19'),
    ]

    full = build_chan_pen(bars)
    assert [(p.start.merged_index, p.end.merged_index, p.status, p.confirmed_on) for p in full.pens] == [
        (1, 7, 'confirmed', '2026-01-15'),
        (7, 12, 'confirmed', '2026-01-19'),
        (12, 16, 'candidate', None),
    ]
    assert len(full.confirmed_pens) == 2

    # The drift itself stays auditable: the abandoned top@5 is not erased, and the
    # too-close bottom@6 is reported rather than silently used as a waypoint.
    drifted = full.endpoints[1]
    assert (drifted.kind, drifted.merged_index, drifted.price) == ('top', 7, 9.5)
    assert (drifted.revisions, drifted.drift_days) == (1, ('2026-01-06',))
    assert [(r.merged_index, r.reason) for r in full.rejections] == [
        (6, 'spacing'),
        (5, 'superseded_by_more_extreme'),
    ]

    oracle = {(1, 7): '2026-01-15', (7, 12): '2026-01-19'}

    # The tempting prefix: the drifting top is already at 9.5 and its own fractal
    # is confirmed, yet no pen may be published as confirmed.
    before_evidence = build_chan_pen(bars[:14])
    assert [(e.merged_index, e.status) for e in before_evidence.endpoints] == [
        (1, 'confirmed'),
        (7, 'candidate'),
    ]
    assert before_evidence.endpoints[-1].price == 9.5
    assert [p.confirmed_on for p in before_evidence.pens] == [None]

    first_seen: dict[tuple[int, int], str] = {}
    ever: set[tuple[int, int]] = set()
    for end in range(1, len(bars) + 1):
        day = bars[end - 1].day
        snap = build_chan_pen(bars[:end])
        keys = {(p.start.merged_index, p.end.merged_index) for p in snap.confirmed_pens}
        for key in keys:
            first_seen.setdefault(key, day)
        # A confirmed pen is never retracted by a later prefix.
        assert ever <= keys, f'{sorted(ever - keys)} disappeared on {day}'
        ever |= keys
        for key, confirmed_day in oracle.items():
            if day < confirmed_day:
                assert key not in keys, (
                    f'{key} confirmed on {day}, but its evidence is only {confirmed_day}'
                )
            else:
                assert key in keys, (
                    f'{key} must be confirmed from {confirmed_day} on, missing on {day}'
                )
    assert first_seen == oracle


# ---------------------------------------------------------------------------
# spacing / price gate
# ---------------------------------------------------------------------------

TOO_CLOSE = [
    (6.0, 5.0),   # 0
    (5.0, 4.0),   # 1  bottom
    (5.5, 4.5),   # 2
    (6.5, 5.5),   # 3
    (7.5, 6.5),   # 4  top: centre gap 3 < 4
    (7.0, 6.0),   # 5
    (6.6, 5.6),   # 6  seals merged bar 5 so the top@4 fractal is confirmed
]


def test_two_fractals_without_an_independent_bar_between_them_are_not_a_pen():
    snap = build_chan_pen(bars_of(TOO_CLOSE))
    assert [(f.kind, f.position_merged_index) for f in build_chan_structure(bars_of(TOO_CLOSE)).fractals] == [
        ('bottom', 1),
        ('top', 4),
    ]
    assert snap.pens == ()
    assert [r.reason for r in snap.rejections] == ['spacing']
    # The anchor is kept: a later legal top may still form a pen from it.
    assert [e.merged_index for e in snap.endpoints] == [1]
    assert snap.open_endpoint.merged_index == 1


def test_price_gate_requires_the_top_range_to_be_partly_above_the_bottom_range():
    from src.core.chan_pen import pen_price_gate_ok

    assert pen_price_gate_ok(top_high=10.0, bottom_high=9.0) is True
    assert pen_price_gate_ok(top_high=9.0, bottom_high=10.0) is False
    # Equality is not "above": fail-closed instead of guessing.
    assert pen_price_gate_ok(top_high=10.0, bottom_high=10.0) is False


def test_price_gate_rejects_a_leg_whose_top_is_inside_the_bottom_range():
    """Same gate, exercised through the whole pipeline.

    A foreign snapshot may carry ranges this kernel would never merge itself;
    the rule must still refuse the pair rather than publish it.  The doctor keeps
    every window a *strict* three-bar shape (bar 1 is still a bottom: its high
    sits below both neighbours' highs) and only lifts the bottom's range above
    the top's — the case the gate exists for.
    """
    structure = build_chan_structure(bars_of(ZIGZAG))
    doctored = replace(
        structure,
        merged_bars=tuple(
            replace(m, high={0: 10.0, 1: 9.0, 2: 9.5}[m.index]) if m.index in (0, 1, 2) else m
            for m in structure.merged_bars
        ),
    )
    snap = pen_from_snapshot(doctored)
    assert snap.pens == ()
    assert [(r.merged_index, r.reason) for r in snap.rejections][0] == (5, 'price_gate')


LOWER_LOW = [
    (6.0, 5.0),   # 0
    (5.0, 4.0),   # 1  bottom
    (5.5, 4.5),   # 2
    (6.5, 5.5),   # 3
    (7.5, 6.5),   # 4
    (8.5, 7.5),   # 5  top
    (8.0, 7.0),   # 6
    (7.0, 6.0),   # 7
    (6.5, 5.5),   # 8
    (6.0, 5.0),   # 9  bottom
    (6.5, 5.5),   # 10
    (7.5, 6.5),   # 11
    (8.5, 7.5),   # 12
    (9.0, 8.0),   # 13 top
    (8.5, 7.5),   # 14
    (7.0, 6.0),   # 15
    (6.0, 5.0),   # 16
    (5.0, 4.0),   # 17 bottom
    (5.5, 4.5),   # 18 top: only one bar from 17, refused for spacing
    (5.0, 4.0),   # 19
    (4.5, 3.5),   # 20
    (4.2, 3.2),   # 21 bottom: lower than 17, so it tries to extend the tail
    (4.4, 3.4),   # 22
    (4.3, 3.3),   # 23 seals merged bar 22
]


def test_a_lower_low_extends_the_open_bottom_endpoint():
    snap = build_chan_pen(bars_of(LOWER_LOW))
    assert [e.merged_index for e in snap.endpoints] == [1, 5, 9, 13, 21]
    bottom = snap.endpoints[-1]
    assert (bottom.kind, bottom.price, bottom.revisions) == ('bottom', 3.2, 1)
    assert bottom.drift_days == ('2026-01-18',)
    # The abandoned position (17) is what gets recorded, and the still-open
    # top@22 is only a candidate so it never reaches the chain.
    assert [(r.merged_index, r.reason) for r in snap.rejections] == [
        (18, 'spacing'),
        (17, 'superseded_by_more_extreme'),
    ]


# A snapshot the kernel itself can produce in which a *lower* low may not take the
# open bottom endpoint over: bar 15's low (3.0) beats bottom@12 (5.0), but bar 15's
# own high (10.0) is above top@7's high (9.5), so it is not a legal leg partner of
# the previous endpoint.  Well-formed bars can get here by gapping a wide bar in a
# downtrend; the extension must be refused, not silently written into the chain.
EXTENSION_NOT_LEGAL = [
    (6.0, 5.0),    # 0
    (5.0, 4.0),    # 1  bottom
    (5.5, 4.5),    # 2
    (6.5, 5.5),    # 3
    (7.5, 6.5),    # 4
    (8.5, 7.5),    # 5  top
    (8.2, 7.2),    # 6  bottom, spacing-rejected
    (9.5, 8.5),    # 7  higher high: the open top drifts here
    (9.0, 8.0),    # 8
    (8.6, 7.6),    # 9  seals 8
    (7.5, 6.5),    # 10
    (6.5, 5.5),    # 11
    (6.0, 5.0),    # 12 bottom
    (6.5, 5.5),    # 13
    (10.5, 6.0),   # 14 top, spacing-rejected, widens the range
    (10.0, 3.0),   # 15 bottom: lower low, but its high is above top@7's high
    (11.0, 3.5),   # 16
    (11.5, 6.0),   # 17 seals 16 -> bottom@15 confirmed
]


def test_an_extension_that_breaks_the_price_gate_is_refused():
    """Defence in depth: a more extreme low may not take the endpoint over when
    its own range is not a legal partner of the previous endpoint.

    The extension gate re-checks exactly what the leg gate checks, so an
    over-wide bar cannot smuggle a rewrite in: the open endpoint stays at
    bottom@12 (price 5.0) and the move is reported as ``extension_not_legal``.
    """
    snap = build_chan_pen(bars_of(EXTENSION_NOT_LEGAL))
    assert [e.merged_index for e in snap.endpoints] == [1, 7, 12]
    assert [(r.merged_index, r.reason) for r in snap.rejections] == [
        (6, 'spacing'),
        (5, 'superseded_by_more_extreme'),
        (14, 'spacing'),
        (15, 'extension_not_legal'),
    ]
    assert snap.endpoints[-1].price == 5.0
    assert snap.endpoints[-1].revisions == 0
    assert snap.pens[-1].end_price == 5.0
    assert [p.status for p in snap.pens] == ['confirmed', 'candidate']


# ---------------------------------------------------------------------------
# blocked / tainted structure
# ---------------------------------------------------------------------------

POLLUTED = [
    (5.0, 1.0),   # 0  envelope: no direction known, absorbs bar 1
    (6.0, 0.5),   # 1
    (7.0, 2.0),   # 2
    (6.0, 1.5),   # 3
    (5.0, 1.1),   # 4
    (5.5, 3.0),   # 5
    (6.0, 4.0),   # 6
    (7.0, 5.0),   # 7
    (8.0, 6.0),   # 8
    (7.0, 5.5),   # 9
    (7.5, 5.8),   # 10  seals merged bar 8 so the top@7 fractal is confirmed
]


def test_initial_envelope_blocks_a_fractal_and_it_never_becomes_an_endpoint():
    structure = build_chan_structure(bars_of(POLLUTED))
    blocked = structure.blocked_fractals
    assert [(f.kind, f.position_merged_index, f.confirmed_on) for f in blocked] == [('top', 1, None)]

    snap = build_chan_pen(bars_of(POLLUTED))
    assert snap.blocked_fractals == blocked
    assert snap.taint_indices == (1,)
    assert all(p.status != 'blocked' for p in snap.pens)
    # Nothing may claim the blocked window as a pen endpoint.
    used = {e.merged_index for e in snap.endpoints}
    assert 1 not in used
    assert [r.reason for r in snap.rejections if r.reason == 'blocked_fractal'] == ['blocked_fractal']
    assert all(p.status != 'confirmed' for p in snap.pens[:1])


def test_a_pen_spanning_a_blocked_window_is_reported_blocked():
    """Fail-closed: a tainted window inside a pen's span cannot be confirmed.

    The live kernel only ever produces an envelope at merged bar 0, so the
    span guard is exercised on a hand-built snapshot: the same rules must
    still refuse to publish a fixed pen across a blocked fractal.  The taint is
    built the way the kernel builds it — an ``envelope_only`` neighbour inside
    the window — instead of relabelling a clean window, so the fixture stays a
    snapshot the validator accepts.
    """
    structure = build_chan_structure(bars_of(ZIGZAG))
    tainted = replace(
        structure,
        merged_bars=tuple(
            replace(m, envelope_only=True) if m.index == 6 else m
            for m in structure.merged_bars
        ),
        fractals=tuple(
            replace(f, status='blocked', confirmed_on=None) if f.position_merged_index == 5 else f
            for f in structure.fractals
        ),
    )
    snap = pen_from_snapshot(tainted)
    assert snap.taint_indices == (5,)
    assert snap.confirmed_pens == ()
    # With the top@5 window tainted the chain falls back to 1 -> 13, and that
    # pen covers the tainted index, so it may not be published as confirmed.
    assert [(p.start.merged_index, p.end.merged_index, p.status, p.confirmed_on) for p in snap.pens] == [
        (1, 13, 'blocked', None),
    ]
    assert snap.blocked_pens == snap.pens
    # The pen is still readable for audit, but no price is presented as fixed.
    assert (snap.pens[0].start_price, snap.pens[0].end_price) == (4.0, 9.0)


# ---------------------------------------------------------------------------
# fail-closed on an inconsistent input snapshot
# ---------------------------------------------------------------------------

def test_inconsistent_snapshots_are_refused_instead_of_guessed():
    structure = build_chan_structure(bars_of(ZIGZAG))

    # A "confirmed" fractal without a confirmation time.
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(
                structure,
                fractals=tuple(
                    replace(f, status='confirmed', confirmed_on=None) if f.position_merged_index == 5 else f
                    for f in structure.fractals
                ),
            )
        )

    # Out-of-order fractals.
    with pytest.raises(ValueError):
        pen_from_snapshot(replace(structure, fractals=tuple(reversed(structure.fractals))))

    # A confirmation time after the snapshot's own as-of day.
    with pytest.raises(ValueError):
        pen_from_snapshot(replace(structure, as_of_day='2026-01-05'))

    # A fractal price that does not match the merged bar it points at.
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(
                structure,
                fractals=tuple(
                    replace(f, position_price=99.0) if f.position_merged_index == 5 else f
                    for f in structure.fractals
                ),
            )
        )

    # A fractal index outside the merged bar list.
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(
                structure,
                fractals=structure.fractals
                + (replace(structure.fractals[1], position_merged_index=99),),
            )
        )


def test_a_confirmed_fractal_cannot_be_confirmed_before_its_right_bar_sealed():
    """A foreign ``confirmed_on`` earlier than the evidence is a look-ahead leak.

    The top@5 window is (4, 5, 6) and merged bar 6 is sealed only on 2026-01-08,
    the day raw bar 7 arrives.  A snapshot claiming 2026-01-05 must be refused
    instead of having that claim copied into a pen's ``confirmed_on`` — otherwise
    a caller can publish a pen "confirmed" before the bar that proves it.
    """
    structure = build_chan_structure(bars_of(ZIGZAG))
    assert structure.merged_bars[6].sealed_on == '2026-01-08'
    assert structure.merged_bars[10].sealed_on == '2026-01-12'

    # Before the fix both of these were accepted silently: the first leaked into
    # the start endpoint's ``confirmed_on``, the second into the *pen's* own
    # ``confirmed_on`` (2026-01-07 for a window that only sealed on 01-12).
    for index, early in ((5, '2026-01-05'), (5, '2026-01-07'), (9, '2026-01-07')):
        forged = replace(
            structure,
            fractals=tuple(
                replace(f, confirmed_on=early) if f.position_merged_index == index else f
                for f in structure.fractals
            ),
        )
        with pytest.raises(ValueError):
            pen_from_snapshot(forged)


def test_a_fractal_label_alone_does_not_make_a_three_bar_shape():
    """Merged bar 2 sits inside a rising leg: it is neither a top nor a bottom.

    Relabelling it must be refused, otherwise a foreign snapshot can mint a pen
    out of bars that never formed a fractal.  The claimed ``confirmed_on`` here
    *is* the real seal day of bar 3, so only the geometry can reject it.
    """
    structure = build_chan_structure(bars_of(ZIGZAG))
    assert structure.merged_bars[3].sealed_on == '2026-01-05'

    fake_top = Fractal(
        kind='top',
        status='confirmed',
        position_merged_index=2,
        position_day='2026-01-03',
        position_price=5.5,
        confirmed_on='2026-01-05',
    )
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(structure, fractals=(structure.fractals[0], fake_top) + structure.fractals[1:])
        )

    fake_bottom = replace(fake_top, kind='bottom', position_price=4.5)
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(structure, fractals=(structure.fractals[0], fake_bottom) + structure.fractals[1:])
        )


def test_fractal_windows_must_have_both_neighbours():
    """A three-bar window needs bars ``i-1`` and ``i+1``, not just ``i``."""
    structure = build_chan_structure(bars_of(ZIGZAG))
    merged = structure.merged_bars
    first = structure.fractals[0]

    forged_head = replace(
        first,
        position_merged_index=0,
        position_price=merged[0].low,
        position_day=merged[0].low_day,
        confirmed_on=merged[0].sealed_on,
    )
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(structure, fractals=(forged_head,) + structure.fractals[1:])
        )

    tail = structure.fractals[-1]
    forged_tail = replace(
        tail,
        position_merged_index=len(merged) - 1,
        position_price=merged[-1].low,
        position_day=merged[-1].low_day,
        confirmed_on=None,
    )
    assert forged_tail.status == 'candidate'
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(structure, fractals=structure.fractals[:-1] + (forged_tail,))
        )


def test_fractal_position_day_must_match_the_merged_extremum_day():
    structure = build_chan_structure(bars_of(ZIGZAG))
    assert structure.fractals[1].position_day == '2026-01-06'
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(
                structure,
                fractals=tuple(
                    replace(f, position_day='2026-01-01') if f.position_merged_index == 5 else f
                    for f in structure.fractals
                ),
            )
        )


def test_fractal_status_must_match_the_window_seal_and_taint():
    """``confirmed`` / ``candidate`` / ``blocked`` are derived, not free labels."""
    structure = build_chan_structure(bars_of(ZIGZAG))
    tail = structure.fractals[-1]
    assert (tail.position_merged_index, tail.status) == (14, 'candidate')

    # The tail's right bar (15) is still open, so nothing may call it confirmed.
    sealed_claim = structure.fractals[:-1] + (
        replace(tail, status='confirmed', confirmed_on='2026-01-16'),
    )
    with pytest.raises(ValueError):
        pen_from_snapshot(replace(structure, fractals=sealed_claim))

    # A window whose right bar *is* sealed is final: it cannot stay a candidate.
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(
                structure,
                fractals=tuple(
                    replace(f, status='candidate', confirmed_on=None)
                    if f.position_merged_index == 5
                    else f
                    for f in structure.fractals
                ),
            )
        )

    # ... and it cannot be called blocked without an unknown-direction bar.
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(
                structure,
                fractals=tuple(
                    replace(f, status='blocked', confirmed_on=None)
                    if f.position_merged_index == 5
                    else f
                    for f in structure.fractals
                ),
            )
        )


def test_snapshot_bar_series_and_tail_seal_are_reverified():
    """Merged bars must be ordered, and only the last one may still be open."""
    structure = build_chan_structure(bars_of(ZIGZAG))
    merged = structure.merged_bars
    assert merged[-1].sealed_on is None

    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(
                structure,
                merged_bars=merged[:-1] + (replace(merged[-1], sealed_on='2026-01-16'),),
            )
        )
    # Duplicating a source index means the series is no longer a prefix replay.
    with pytest.raises(ValueError):
        pen_from_snapshot(
            replace(structure, merged_bars=(replace(merged[0], index=1),) + merged[1:])
        )


def test_empty_snapshot_yields_no_structure():
    snap = build_chan_pen([])
    assert (snap.as_of_day, snap.pens, snap.endpoints, snap.rejections, snap.taint_indices) == (
        None,
        (),
        (),
        (),
        (),
    )
    assert snap.open_endpoint is None


# ---------------------------------------------------------------------------
# prefix replay / no look-ahead
# ---------------------------------------------------------------------------

def test_every_prefix_replays_and_confirmed_pens_never_change():
    bars = bars_of(ZIGZAG)
    full = build_chan_pen(bars)
    assert len(full.confirmed_pens) == 2

    seen = {}
    for end in range(1, len(bars) + 1):
        day = bars[end - 1].day
        snap = build_chan_pen(bars[:end])

        structure = build_chan_structure(bars[:end])
        assert snap.as_of_day == day
        # Structural bounds: nothing may point past the prefix.
        for pen in snap.pens:
            assert pen.end.merged_index < len(structure.merged_bars)
            assert pen.start.merged_index < pen.end.merged_index
            for endpoint in (pen.start, pen.end):
                assert endpoint.position_day <= day
                if endpoint.confirmed_on is not None:
                    assert endpoint.confirmed_on <= day
        for endpoint in snap.endpoints:
            assert endpoint.merged_index < len(structure.merged_bars)

        # Exactly one open (driftable) endpoint, and it is the chain tail.
        assert sum(1 for e in snap.endpoints if e.status == 'candidate') <= 1
        if snap.endpoints:
            assert snap.endpoints[-1].status == 'candidate'

        # Confirmed facts are a pure function of the prefix: no later bar and
        # no later fractal may leak into them.
        assert snap.confirmed_pens == tuple(p for p in full.confirmed_pens if p.confirmed_on <= day)
        assert snap.taint_indices == tuple(
            t for t in full.taint_indices if t < len(structure.merged_bars)
        )

        for pen in snap.pens:
            key = (pen.start.merged_index, pen.end.merged_index)
            if key in seen and seen[key].status == 'confirmed':
                assert pen == seen[key], f'confirmed pen {key} changed at {day}'
            seen[key] = pen

    # The full run must still hold every pen that some prefix confirmed.
    for pen in seen.values():
        if pen.status == 'confirmed':
            assert pen in full.confirmed_pens


def test_candidate_endpoint_moves_do_not_disturb_confirmed_pens():
    """Drift is allowed only on the open tail, never behind a confirmed pen."""
    seen_confirmed = ()
    for end in range(1, len(DRIFT) + 1):
        snap = build_chan_pen(bars_of(DRIFT)[:end])
        assert snap.confirmed_pens[: len(seen_confirmed)] == seen_confirmed
        seen_confirmed = snap.confirmed_pens
        assert all(p.status == 'candidate' for p in snap.pens) or not snap.pens


# ---------------------------------------------------------------------------
# scope honesty: no 中枢
# ---------------------------------------------------------------------------

def test_module_claims_no_center_or_segment():
    import src.core.chan_pen as mod

    for forbidden in ('center', 'zhongshu', 'segment', 'pivot', 'buy_sell'):
        assert not any(forbidden in name.lower() for name in dir(mod)), forbidden
    doc = mod.__doc__ or ''
    assert 'Not implemented' in doc
    assert '中枢' in doc
    assert '前三笔重叠' in doc


def test_a_long_walk_replays_without_lookahead():
    """A deliberately messy series: absorbed bars, drift candidates, rejections.

    The point is not a particular shape but the invariants: every prefix must
    agree with the full run on everything already confirmed, and a confirmed
    pen must never be rewritten by later bars.
    """
    import random

    rng = random.Random(7)
    price = 10.0
    ranges = []
    for _ in range(60):
        price += rng.uniform(-1.2, 1.2)
        half = rng.uniform(0.2, 1.1)
        ranges.append((round(price + half, 2), round(price - half, 2)))
    bars = bars_of(ranges)
    full = build_chan_pen(bars)

    # The fixture must actually exercise the kernel.
    assert len(full.pens) >= 3
    assert len(full.endpoints) >= 4

    seen = {}
    for end in range(1, len(bars) + 1):
        day = bars[end - 1].day
        snap = build_chan_pen(bars[:end])
        structure = build_chan_structure(bars[:end])

        assert snap.confirmed_pens == tuple(p for p in full.confirmed_pens if p.confirmed_on <= day)
        for pen in snap.pens:
            assert pen.start.kind != pen.end.kind
            assert pen.index_gap >= MIN_INDEX_GAP
            assert pen.start.merged_index > -1
            assert pen.end.merged_index < len(structure.merged_bars)
            assert pen.confirmed_on is None or pen.confirmed_on <= day
            key = (pen.start.merged_index, pen.end.merged_index)
            if key in seen and seen[key].status == 'confirmed':
                assert pen == seen[key], f'confirmed pen {key} changed on {day}'
            seen[key] = pen
        assert [e.status for e in snap.endpoints].count('candidate') <= 1
        for endpoint in snap.endpoints:
            assert endpoint.merged_index < len(structure.merged_bars)
            assert endpoint.revisions == len(endpoint.drift_days)
            assert endpoint.position_day <= day

    for pen in full.confirmed_pens:
        assert pen.confirmed_on is not None
        assert pen.confirmed_on >= pen.end.position_day


def test_the_kernel_is_pure_and_deterministic():
    bars = bars_of(ZIGZAG)
    before = list(bars)
    first = build_chan_pen(bars)
    second = build_chan_pen(bars)
    assert first == second
    assert bars == before  # nothing mutated, no new bars invented
