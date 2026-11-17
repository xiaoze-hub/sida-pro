"""Chan v1 merged bars + fractals: pure, causal and replayable.

These tests pin the execution model:

* containment is inclusive (``>=`` / ``<=``), so equal extremes merge;
* the very first containment has no known direction and must not guess one;
* a bar built by that unknown-direction merge is an *envelope*, never Chan
  merge output, so no confirmed fractal may use it (such windows are
  ``blocked``), while a lone first bar is direction-unknown but pristine;
* every merge is sequential and only uses bars available at that time;
* a fractal is confirmed only after its right merged bar is sealed, while a
  still-open tail can only be a candidate;
* every emitted fact carries a position day *and* a separate confirmation day.
"""
import pytest

from src.core.chan_structure import (
    RawBar,
    build_chan_structure,
    detect_raw_extrema,
)


def b(day, high, low):
    return RawBar(day=day, open=low, high=high, low=low, close=high)


# ---------------------------------------------------------------------------
# legacy raw extrema (kept for the previous experimental API)
# ---------------------------------------------------------------------------

def test_strict_top_has_separate_position_and_confirmation():
    out = detect_raw_extrema([b('2026-01-01', 3, 1), b('2026-01-02', 5, 2), b('2026-01-03', 4, 1)])
    assert len(out) == 1
    assert (out[0].kind, out[0].position_day, out[0].confirmed_on, out[0].price) == ('top', '2026-01-02', '2026-01-03', 5)


def test_equal_high_is_not_a_strict_top():
    assert not detect_raw_extrema([b('2026-01-01', 3, 1), b('2026-01-02', 5, 2), b('2026-01-03', 5, 1)])


def test_confirmation_is_stable_for_each_truncated_prefix():
    bars = [b('2026-01-01', 3, 1), b('2026-01-02', 5, 2), b('2026-01-03', 4, 1), b('2026-01-04', 6, 3)]
    full = detect_raw_extrema(bars)
    for end in range(1, len(bars) + 1):
        prefix = detect_raw_extrema(bars[:end])
        assert prefix == [x for x in full if x.confirmed_on <= bars[end - 1].day]


def test_bottom_and_containment_are_distinct():
    bars = [b('2026-01-01', 6, 3), b('2026-01-02', 4, 1), b('2026-01-03', 5, 2)]
    out = detect_raw_extrema(bars)
    assert [(x.kind, x.position_day, x.confirmed_on, x.price) for x in out] == [
        ('bottom', '2026-01-02', '2026-01-03', 1)
    ]
    assert detect_raw_extrema([b('2026-01-01', 4, 2), b('2026-01-02', 5, 1), b('2026-01-03', 4, 2)]) == []


def test_reject_bad_dates_and_ohlc():
    with pytest.raises(ValueError):
        detect_raw_extrema([b('2026-01-02', 2, 1), b('2026-01-01', 2, 1)])
    with pytest.raises(ValueError):
        detect_raw_extrema([RawBar(day='2026-01-01', open=3, high=2, low=1, close=2)])
    with pytest.raises(ValueError):
        detect_raw_extrema([b('2026-01-01', float('nan'), 1)])


# ---------------------------------------------------------------------------
# merged bars
# ---------------------------------------------------------------------------

def test_empty_and_single_bar_have_no_fractal():
    empty = build_chan_structure([])
    assert (empty.as_of_day, empty.merged_bars, empty.fractals) == (None, (), ())

    snap = build_chan_structure([b('2026-01-01', 5, 1)])
    assert len(snap.merged_bars) == 1
    merged = snap.merged_bars[0]
    assert (merged.high, merged.low) == (5, 1)
    assert merged.merge_direction == 'unknown'
    assert merged.sealed_on is None
    assert snap.fractals == ()


def test_initial_containment_has_unknown_direction_and_keeps_all_sources():
    snap = build_chan_structure([
        b('2026-01-01', 5, 1),
        b('2026-01-02', 4, 2),
        b('2026-01-03', 4.5, 1.5),
    ])
    assert len(snap.merged_bars) == 1
    merged = snap.merged_bars[0]
    assert (merged.high, merged.low) == (5, 1)
    assert (merged.high_day, merged.low_day) == ('2026-01-01', '2026-01-01')
    assert merged.sources == (0, 1, 2)
    assert merged.merge_direction == 'unknown'  # must not guess "up"
    assert merged.sealed_on is None
    assert (merged.start_day, merged.end_day) == ('2026-01-01', '2026-01-03')
    assert snap.fractals == ()
    assert snap.as_of_day == '2026-01-03'


def test_unknown_direction_envelope_expands_without_guessing():
    snap = build_chan_structure([b('2026-01-01', 5, 1), b('2026-01-02', 6, 0.5)])
    assert len(snap.merged_bars) == 1
    merged = snap.merged_bars[0]
    assert (merged.high, merged.low) == (6, 0.5)
    assert (merged.high_day, merged.low_day) == ('2026-01-02', '2026-01-02')
    assert merged.sources == (0, 1)
    assert merged.merge_direction == 'unknown'


def test_equal_extremes_count_as_containment_and_keep_earliest_extreme_day():
    snap = build_chan_structure([
        b('2026-01-01', 5, 1),
        b('2026-01-02', 5, 2),   # equal high: still containment
        b('2026-01-03', 5, 2),   # identical bar
    ])
    assert len(snap.merged_bars) == 1
    merged = snap.merged_bars[0]
    assert (merged.high, merged.low) == (5, 1)
    assert (merged.high_day, merged.low_day) == ('2026-01-01', '2026-01-01')
    assert merged.sources == (0, 1, 2)
    assert merged.merge_direction == 'unknown'


def test_fully_equal_ranges_still_merge():
    snap = build_chan_structure([b('2026-01-01', 5, 1), b('2026-01-02', 5, 1)])
    assert len(snap.merged_bars) == 1
    merged = snap.merged_bars[0]
    assert merged.sources == (0, 1)
    assert (merged.high_day, merged.low_day) == ('2026-01-01', '2026-01-01')


def test_first_non_contained_bar_establishes_direction_and_seals_previous():
    snap = build_chan_structure([
        b('2026-01-01', 5, 1),
        b('2026-01-02', 4, 2),   # contained, direction still unknown
        b('2026-01-03', 6, 3),   # strictly above the merged envelope
    ])
    assert len(snap.merged_bars) == 2
    first, second = snap.merged_bars
    assert first.merge_direction == 'unknown'
    assert first.sealed_on == '2026-01-03'
    assert second.merge_direction == 'up'
    assert second.sealed_on is None
    assert snap.fractals == ()


def test_consecutive_containment_accumulates_sources_and_extreme_days():
    snap = build_chan_structure([
        b('2026-01-01', 5, 1),
        b('2026-01-02', 6, 2),    # up
        b('2026-01-03', 5.5, 3),  # contained in the second merged bar
        b('2026-01-04', 5.8, 3.5),  # contained again
        b('2026-01-05', 7, 4),    # seals the merged bar
    ])
    assert len(snap.merged_bars) == 3
    merged = snap.merged_bars[1]
    assert (merged.high, merged.low) == (6, 3.5)
    assert (merged.high_day, merged.low_day) == ('2026-01-02', '2026-01-04')
    assert merged.sources == (1, 2, 3)
    assert merged.merge_direction == 'up'
    assert merged.sealed_on == '2026-01-05'
    assert (merged.start_day, merged.end_day) == ('2026-01-02', '2026-01-04')


def test_direction_rule_is_symmetric_on_the_way_down():
    snap = build_chan_structure([
        b('2026-01-01', 8, 4),
        b('2026-01-02', 6, 2),    # down
        b('2026-01-03', 5, 3),    # contained
        b('2026-01-04', 4.5, 2.5),  # contained
    ])
    merged = snap.merged_bars[1]
    assert (merged.high, merged.low) == (4.5, 2)
    assert (merged.high_day, merged.low_day) == ('2026-01-04', '2026-01-02')
    assert merged.sources == (1, 2, 3)
    assert merged.merge_direction == 'down'


# ---------------------------------------------------------------------------
# fractals
# ---------------------------------------------------------------------------

def test_top_fractal_is_candidate_until_right_bar_is_sealed():
    bars = [
        b('2026-01-01', 3, 1),
        b('2026-01-02', 5, 2),
        b('2026-01-03', 4, 1.5),
        b('2026-01-04', 3, 1.2),
    ]

    before = build_chan_structure(bars[:3])
    assert len(before.fractals) == 1
    candidate = before.fractals[0]
    assert (candidate.kind, candidate.status, candidate.confirmed_on) == ('top', 'candidate', None)
    assert (candidate.position_day, candidate.position_price) == ('2026-01-02', 5)
    assert before.confirmed_fractals == ()

    after = build_chan_structure(bars)
    assert len(after.confirmed_fractals) == 1
    top = after.confirmed_fractals[0]
    assert (top.kind, top.position_day, top.position_price) == ('top', '2026-01-02', 5)
    assert top.confirmed_on == '2026-01-04'
    # confirmation is a time, not the extremum position
    assert top.confirmed_on != top.position_day
    assert after.merged_bars[top.position_merged_index + 2].start_day == top.confirmed_on


def test_bottom_fractal_is_symmetric():
    snap = build_chan_structure([
        b('2026-01-01', 6, 3),
        b('2026-01-02', 4, 1),
        b('2026-01-03', 5, 2),
        b('2026-01-04', 5.5, 2.5),
    ])
    assert len(snap.confirmed_fractals) == 1
    bottom = snap.confirmed_fractals[0]
    assert (bottom.kind, bottom.position_day, bottom.position_price) == ('bottom', '2026-01-02', 1)
    assert bottom.confirmed_on == '2026-01-04'


def test_tail_containment_defers_confirmation_without_early_confirm():
    bars = [
        b('2026-01-01', 3, 1),
        b('2026-01-02', 5, 2),
        b('2026-01-03', 4, 1.5),
    ]
    before = build_chan_structure(bars)
    assert [(f.kind, f.status) for f in before.fractals] == [('top', 'candidate')]

    # The fourth bar is contained by the right bar.  It is absorbed under the
    # down direction, so the right merged bar stays below the middle: the top
    # remains a candidate, but nothing may be confirmed yet.
    extended = build_chan_structure(bars + [b('2026-01-04', 5.5, 1.4)])
    assert [(f.kind, f.status) for f in extended.fractals] == [('top', 'candidate')]
    assert extended.confirmed_fractals == ()
    right = extended.merged_bars[-1]
    assert right.sources == (2, 3)
    assert right.sealed_on is None
    assert (right.high, right.low) == (4, 1.4)

    # Only the bar that seals the right bar confirms the top, so the
    # confirmation day drifts from the position day to that later day.
    sealed = build_chan_structure(bars + [b('2026-01-04', 5.5, 1.4), b('2026-01-05', 6, 2)])
    tops = [f for f in sealed.confirmed_fractals if f.kind == 'top']
    assert len(tops) == 1
    assert (tops[0].position_day, tops[0].confirmed_on) == ('2026-01-02', '2026-01-05')
    assert sealed.merged_bars[2].sealed_on == '2026-01-05'
    assert [(f.kind, f.status) for f in sealed.candidates] == [('bottom', 'candidate')]


def test_two_merged_bars_are_not_enough_for_a_fractal():
    snap = build_chan_structure([b('2026-01-01', 3, 1), b('2026-01-02', 5, 2)])
    assert snap.fractals == ()


# ---------------------------------------------------------------------------
# unknown-direction containment must not contaminate confirmed structure
# ---------------------------------------------------------------------------

def test_unknown_direction_envelope_bar_cannot_confirm_a_fractal():
    """Counterexample: a top whose left neighbour is an enumeration envelope.

    (5, 1) then (6, 0.5) contain each other, so no direction is known and the
    kernel keeps ``max(high)``/``min(low)``.  Because the user asked for the
    sequential rules, the following (7, 2), (6, 1.5), (5, 1.1) *look* like a
    top around (7, 2) — but that left neighbour was never produced by a Chan
    merge decision, only by an envelope.  Such a window may not be reported as
    ``confirmed``.
    """
    bars = [
        b('2026-01-01', 5, 1),
        b('2026-01-02', 6, 0.5),   # contains bar 0: envelope, direction unknown
        b('2026-01-03', 7, 2),
        b('2026-01-04', 6, 1.5),
        b('2026-01-05', 5, 1.1),
    ]
    snap = build_chan_structure(bars)

    # The contaminated bar is created from the envelope, not from a merge rule.
    assert snap.merged_bars[0].merge_direction == 'unknown'
    assert snap.merged_bars[0].sources == (0, 1)
    assert snap.merged_bars[0].envelope_only is True
    assert (snap.merged_bars[0].high, snap.merged_bars[0].low) == (6, 0.5)

    # The three-bar window (0, 1, 2) is shaped like a top, but it is blocked.
    assert len(snap.fractals) == 1
    blocked = snap.fractals[0]
    assert (blocked.kind, blocked.status) == ('top', 'blocked')
    assert (blocked.position_merged_index, blocked.position_day, blocked.position_price) == (
        1,
        '2026-01-03',
        7,
    )
    assert blocked.confirmed_on is None
    assert snap.confirmed_fractals == ()
    assert snap.candidates == ()
    assert snap.blocked_fractals == (blocked,)


def test_multiple_initial_containments_are_all_envelope_only():
    bars = [
        b('2026-01-01', 5, 1),
        b('2026-01-02', 6, 0.5),
        b('2026-01-03', 5.5, 0.6),   # contained in the envelope
        b('2026-01-04', 7, 2),
        b('2026-01-05', 6, 1.5),
        b('2026-01-06', 5, 1.1),
    ]
    snap = build_chan_structure(bars)
    assert len(snap.merged_bars) == 4
    first = snap.merged_bars[0]
    assert first.sources == (0, 1, 2)
    assert (first.high, first.low) == (6, 0.5)
    assert first.envelope_only is True
    assert [m.envelope_only for m in snap.merged_bars[1:]] == [False, False, False]
    assert snap.confirmed_fractals == ()
    assert [(f.kind, f.status) for f in snap.fractals] == [('top', 'blocked')]


def test_single_bar_unknown_direction_is_pristine_and_usable():
    """A lone first bar has no direction either, but no merge decision was made.

    Its high/low are the raw values, so a fractal may legitimately use it as a
    neighbour.  Only an *envelope* (direction-unknown bar that absorbed other
    bars) is contaminated.
    """
    bars = [
        b('2026-01-01', 3, 1),
        b('2026-01-02', 5, 2),
        b('2026-01-03', 4, 1.5),
        b('2026-01-04', 3, 1.2),
    ]
    snap = build_chan_structure(bars)
    first = snap.merged_bars[0]
    assert (first.merge_direction, first.sources, first.envelope_only) == ('unknown', (0,), False)
    assert snap.blocked_fractals == ()
    assert [(f.kind, f.status, f.position_day, f.confirmed_on) for f in snap.confirmed_fractals] == [
        ('top', 'confirmed', '2026-01-02', '2026-01-04')
    ]


def test_confirmation_recovers_after_three_clean_merged_bars():
    """Blocked is terminal, but later clean windows confirm again."""
    polluted = [
        b('2026-01-01', 5, 1),
        b('2026-01-02', 6, 0.5),
        b('2026-01-03', 7, 2),
        b('2026-01-04', 6, 1.5),
        b('2026-01-05', 5, 1.1),
    ]
    assert build_chan_structure(polluted).confirmed_fractals == ()

    # Merged bars 2, 3, 4 all descend from the post-envelope sequence and are
    # clean; a bottom at merged bar 3 is therefore confirmable.
    bars = polluted + [
        b('2026-01-06', 5.5, 3),
        b('2026-01-07', 6, 4),
    ]
    snap = build_chan_structure(bars)
    assert [m.envelope_only for m in snap.merged_bars] == [True, False, False, False, False, False]

    blocked = [f for f in snap.fractals if f.status == 'blocked']
    assert [(f.kind, f.position_day) for f in blocked] == [('top', '2026-01-03')]
    assert [(f.kind, f.status, f.position_day, f.confirmed_on) for f in snap.confirmed_fractals] == [
        ('bottom', 'confirmed', '2026-01-05', '2026-01-07')
    ]


def test_confirmation_resumes_at_the_first_window_of_three_clean_merged_bars():
    """The earliest recoverable window is merged bars 1, 2, 3 (middle index 2).

    Merged bar 0 is the envelope, so any window touching it is blocked (no
    shape here, hence no blocked entry).  The window 1/2/3 never touches it and
    confirms normally once its right bar is sealed — no blanket suppression of
    the rest of the series.
    """
    snap = build_chan_structure([
        b('2026-01-01', 5, 1),
        b('2026-01-02', 6, 0.5),   # envelope: merged 0
        b('2026-01-03', 8, 3),     # merged 1
        b('2026-01-04', 9, 4),     # merged 2
        b('2026-01-05', 6, 1.5),   # merged 3
        b('2026-01-06', 5, 1),     # merges 4; seals merged 3
    ])
    assert [m.envelope_only for m in snap.merged_bars] == [True, False, False, False, False]
    assert snap.blocked_fractals == ()
    assert [(f.kind, f.status, f.position_merged_index, f.position_day, f.confirmed_on) for f in snap.fractals] == [
        ('top', 'confirmed', 2, '2026-01-04', '2026-01-06'),
    ]
    assert [(f.position_merged_index, f.confirmed_on) for f in snap.confirmed_fractals] == [(2, '2026-01-06')]


def test_blocked_fractal_never_upgrades_and_sealed_merged_bars_are_stable():
    bars = [
        b('2026-01-01', 5, 1),
        b('2026-01-02', 6, 0.5),
        b('2026-01-03', 7, 2),
        b('2026-01-04', 6, 1.5),
        b('2026-01-05', 5, 1.1),
        b('2026-01-06', 5.5, 3),
        b('2026-01-07', 6, 4),
        b('2026-01-08', 4, 2),
    ]
    seen_blocked = []
    for end in range(1, len(bars) + 1):
        snap = build_chan_structure(bars[:end])
        statuses = {(f.kind, f.position_day): f.status for f in snap.fractals}
        assert statuses.get(('top', '2026-01-03')) in (None, 'blocked')
        for key in seen_blocked:
            assert statuses.get(key) == 'blocked'
        if statuses.get(('top', '2026-01-03')) == 'blocked':
            seen_blocked.append(('top', '2026-01-03'))

    full = build_chan_structure(bars)
    assert [f.status for f in full.fractals if f.position_day == '2026-01-03'] == ['blocked']
    # A blocked fractal is never exposed through the confirmed/candidate views.
    assert {f.status for f in full.confirmed_fractals} <= {'confirmed'}
    assert {f.status for f in full.candidates} <= {'candidate'}



# ---------------------------------------------------------------------------
# truncation replay / no look-ahead
# ---------------------------------------------------------------------------

def test_every_prefix_replays_without_lookahead():
    bars = [
        b('2026-01-01', 6, 3),
        b('2026-01-02', 4, 1),
        b('2026-01-03', 5, 2),
        b('2026-01-04', 11, 9),
        b('2026-01-05', 10, 8),
        b('2026-01-06', 9, 7),
    ]
    full = build_chan_structure(bars)
    assert [(f.kind, f.position_day, f.position_price, f.confirmed_on) for f in full.confirmed_fractals] == [
        ('bottom', '2026-01-02', 1, '2026-01-04'),
        ('top', '2026-01-04', 11, '2026-01-06'),
    ]

    for end in range(1, len(bars) + 1):
        day = bars[end - 1].day
        snap = build_chan_structure(bars[:end])

        assert snap.as_of_day == day
        assert snap.merged_bars[-1].sealed_on is None
        assert all(m.sealed_on is not None for m in snap.merged_bars[:-1])

        for merged in snap.merged_bars:
            assert merged.start_day <= merged.end_day <= day
            assert merged.high_day <= day and merged.low_day <= day
            assert max(merged.sources) < end

        for fractal in snap.fractals:
            assert fractal.position_day <= day
            if fractal.status == 'confirmed':
                assert fractal.confirmed_on is not None
                assert fractal.confirmed_on <= day
            else:
                assert fractal.status == 'candidate' and fractal.confirmed_on is None
                assert fractal.position_merged_index == len(snap.merged_bars) - 2

        # Facts already confirmed in the full run and known at this day must
        # match this prefix exactly; nothing later may leak in.
        assert snap.confirmed_fractals == tuple(
            f for f in full.confirmed_fractals if f.confirmed_on <= day
        )
        for merged in snap.merged_bars:
            if merged.sealed_on is not None:
                assert merged == full.merged_bars[merged.index]


def test_date_gaps_are_accepted_and_not_filled():
    # A weekend gap is not a trading-calendar error; this kernel neither
    # invents bars nor validates exchange holidays.
    snap = build_chan_structure([b('2026-01-02', 5, 1), b('2026-01-05', 6, 2)])
    assert snap.as_of_day == '2026-01-05'
    assert len(snap.merged_bars) == 2


def test_structure_rejects_bad_dates_and_ohlc():
    with pytest.raises(ValueError):
        build_chan_structure([b('2026-01-02', 2, 1), b('2026-01-01', 2, 1)])
    with pytest.raises(ValueError):
        build_chan_structure([RawBar(day='2026-01-01', open=3, high=2, low=1, close=2)])
    with pytest.raises(ValueError):
        build_chan_structure([b('2026-01-01', float('nan'), 1)])
    with pytest.raises(ValueError):
        build_chan_structure([b('2026-01-01', 2, 0)])
