"""Chan v2 segment (线段) kernel: 第67课第一种情况 only — causal, conservative.

These tests pin the hard parts, not a happy path:

* a segment needs at least three confirmed pens and a **non-empty** overlap of
  their ranges; a zero-intersection trio is refused (never forced into a 线段);
* the segment ends only when the **反方向笔特征序列** forms a terminal fractal
  (顶分型 for an up segment, 底分型 for a down one), and only the *no-gap* case
  (`第一种情况`) is implemented: a gap between the fractal's first two elements
  is reported ``blocked`` (`第二种情况` needs the second characteristic sequence,
  which is deliberately not implemented and never guessed);
* 包含处理 of the characteristic sequence is sequential and **non-transitive**
  (a transitive closure would merge different elements), and its **direction**
  comes from the sequence itself — the trend of the two most recent normalised,
  non-contained elements — never from the segment's own direction.  Where that
  trend does not exist (the first pair is contained) or is undefined (the pair
  before it is ambiguous), the segment is ``blocked`` instead of guessing a side;
* the segment ends at the pen carrying the **fractal's own extreme** (第67课
  "该线段在该顶分型的高点处结束"): not at an earlier pen that merely has an equal
  price, and never at a pen outside the fractal;
* evidence (the three fractal elements: pens, range, confirmation day) is
  reported **separately** from the geometric span — the fractal's right element
  is necessarily *after* the segment's end;
* only confirmed pens are evidence: a candidate pen can never confirm a segment,
  and the earliest confirmation day is the day the third characteristic element
  was frozen, never the extreme's own day;
* endpoint drift lives on the open (candidate) segment only.  A confirmed
  segment is immutable: a later higher high is *not* back-filled, it belongs to
  the next structure — prefix replay is checked bar by bar;
* a tainted (``blocked``) pen in the span blocks the segment; nothing is
  published as fixed across it;
* **nothing shorter than three pens is ever a segment**: such a decision point is
  a ``SegmentPending`` (candidate or blocked, with the reason kept), never a
  one-pen "line" and never an internal exception;
* the result is explicitly bound to one source snapshot (symbol / adjustment /
  as-of day / input digest / pen kernel version).  Two results from different
  sources are never "the same line", and mixing them raises; the adjustment base
  may be declared ``unknown`` (never coerced to ``none``/``qfq``/``hfq``) because
  an unverifiable 复权 base must not be fabricated;
* ``confirmed_segments`` is the only consumer-safe view: it returns only frozen
  第67课 第一种情况 lines, never ``candidate`` or ``blocked``, and its membership is
  the pen span only — evidence pens reach past the end into the next span, so
  folding them in would double count;
* a ``blocked`` pending that is a decision *at an evidence pen* (undefined 包含处理
  direction) reports that pen in ``blocked_at_pen_index`` with no span, instead of
  pointing ``end_pen_index`` at the chain start and claiming a one-pen decision;
* every input inconsistency (non-contiguous pens, non-alternating directions,
  duplicated merged indices, a pen shorter than the pen kernel's merged-index
  gap, days that are not ISO or not strictly increasing, mixed kernel versions, a
  confirmation after ``as_of_day``/before its own position, out-of-order
  confirmations, a pen whose prices contradict its direction) raises
  ``ValueError`` instead of being repaired.

The hand-built pen series below are *explicit inputs* (the module's contract: a
pure function of a given pen sequence); one test additionally drives the whole
bar → fractal → pen → segment pipeline on generated bars and compares against a
hand-derived oracle, and another compares the production walk against a second
walk built only from the public normaliser.
"""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.core.chan_pen import (
    MIN_INDEX_GAP,
    PEN_KERNEL_VERSION,
    Pen,
    PenEndpoint,
    PenRules,
    PenSnapshot,
    build_chan_pen,
)
from src.core.chan_segment import (
    ADJUSTMENT_BASES,
    MIN_PENS,
    SEGMENT_KERNEL_VERSION,
    FractalEvidence,
    SegmentBinding,
    SegmentPending,
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
# characteristic sequence of down pens (8.5/4.5, 9.5/5.5, 10.5/6.5, 10.0/6.0)
# prints a top fractal at 10.5/6.5.  Its third element is the down pen
# 10.0 -> 6.0, which is frozen only when bar 39 arrives (see the oracle test).
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


def helper_oracle(prices, *, confirmed=None, first_kind='bottom'):
    """A *second* walk, built only from the public normaliser.

    It reads the characteristic ranges off the reverse pens, normalises them with
    :func:`normalize_characteristic_sequence` and applies the 第67课 rules (three
    element fractal, no gap, extreme carrier) itself.  It shares no code with the
    kernel's walk, so the two must agree — that is what keeps a single 包含处理
    implementation honest (v1 had two copies and they drifted).

    Returns ``[(start, end_pen, reason)]`` with ``reason`` in
    ``{'case1', 'case2', block reason..., 'tail'}``.
    """
    snapshot = make_pens(prices, confirmed=confirmed, first_kind=first_kind)
    pens = snapshot.pens
    out = []
    start = 0
    total = len(pens)
    while start < total:
        direction = pens[start].direction
        if start + 2 >= total:
            out.append((start, None, 'tail'))
            break
        trio = pens[start : start + 3]
        if max(min(p.start.price, p.end.price) for p in trio) >= min(
            max(p.start.price, p.end.price) for p in trio
        ):
            out.append((start, None, 'no_overlap'))
            break
        if any(p.status != 'confirmed' for p in trio):
            out.append((start, None, 'tail'))
            break

        decided = None
        for k in range(start + 1, total, 2):
            if pens[k].status != 'confirmed':
                break
            ranges = [
                (max(p.start.price, p.end.price), min(p.start.price, p.end.price))
                for p in pens[start + 1 : k + 1 : 2]
            ]
            normalised, blocked = normalize_characteristic_sequence(ranges)
            if blocked is not None:
                decided = (start, None, blocked)
                break
            if len(normalised) < 3:
                continue
            left, middle, right = normalised[-3], normalised[-2], normalised[-1]
            if direction == 'up':
                hit = (
                    middle[0] > left[0] and middle[1] > left[1]
                    and middle[0] > right[0] and middle[1] > right[1]
                )
                side = middle[0]
                gap = middle[1] > left[0]
                span_extreme = max(p.end.price for p in pens[start:k:2])
                moved = span_extreme > side
            else:
                hit = (
                    middle[1] < left[1] and middle[0] < left[0]
                    and middle[1] < right[1] and middle[0] < right[0]
                )
                side = middle[1]
                gap = middle[0] < left[1]
                span_extreme = min(p.end.price for p in pens[start:k:2])
                moved = span_extreme < side
            if not hit:
                continue
            carriers = [j for j in range(start, k, 2) if pens[j].end.price == side]
            if not carriers:  # pragma: no cover - the extreme always is a pen end
                decided = (start, None, 'no_carrier')
                break
            end_pen = carriers[-1]
            if moved:
                decided = (start, None, 'characteristic_fractal_below_prior_extreme')
            else:
                decided = (start, end_pen, 'case2' if gap else 'case1')
            break
        if decided is None:
            out.append((start, None, 'tail'))
            break
        out.append(decided)
        if decided[2] != 'case1':
            break
        start = decided[1] + 1
    return out


# ---------------------------------------------------------------------------
# versioning / rules
# ---------------------------------------------------------------------------

def test_rules_are_versioned_and_every_field_is_pinned():
    rules = SegmentRules()
    assert rules.kernel_version == SEGMENT_KERNEL_VERSION == 'chan-segment-v2'
    assert rules.min_pens == MIN_PENS == 3
    assert rules.min_index_gap == MIN_INDEX_GAP == 4
    assert rules.overlap == 'pen_endpoint_range_strict'
    assert rules.characteristic_sequence == 'reverse_pens'
    assert rules.inclusion == 'sequential_non_transitive'
    assert rules.merge_direction == 'recent_two_elements'
    assert rules.segment_end == 'fractal_middle_extreme_pen'
    assert rules.equal_fractal_extreme == 'keep_latest'
    assert rules.gap_case == 'blocked_case2_not_implemented'
    assert rules.initial_containment == 'blocked'
    assert rules.equal_extreme == 'keep_earlier'
    assert rules.evidence == 'confirmed_pens_only'
    assert rules.input_gate == 'iso_days_strict_and_pen_index_gap'
    # 'unknown' is a declared identity label, part of the v2 rule set: a caller
    # must be able to say "the 复权 base is not verified" instead of guessing one.
    assert rules.adjustment_labels == ADJUSTMENT_BASES == ('none', 'qfq', 'hfq', 'unknown')


@pytest.mark.parametrize(
    'field, value',
    [
        ('kernel_version', 'chan-segment-v1'),
        ('min_pens', 4),
        ('min_index_gap', 3),
        ('overlap', 'any_touch'),
        ('characteristic_sequence', 'all_pens'),
        ('inclusion', 'transitive_closure'),
        ('merge_direction', 'segment_direction'),
        ('segment_end', 'first_equal_extreme'),
        ('equal_fractal_extreme', 'keep_earlier'),
        ('gap_case', 'confirm_anyway'),
        ('initial_containment', 'guess'),
        ('equal_extreme', 'keep_later'),
        ('evidence', 'candidate_pens_too'),
        ('input_gate', 'trust_caller'),
        ('adjustment_labels', ('none', 'qfq', 'hfq')),
        ('adjustment_labels', ('none', 'qfq', 'hfq', 'unknown', 'auto')),
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


def test_digest_owns_every_pen_fact_and_binding_owns_the_sample():
    """The digest names the series; symbol/adjust are declarations outside it.

    Every pen field that can change the segments must change the digest, and the
    caller-declared sample identity must still separate two results that share a
    digest — otherwise two different lines could be compared as one.
    """
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0])
    base = segment_input_digest(snapshot)

    pen = snapshot.pens[0]
    mutations = {
        'price': replace(pen, end=replace(pen.end, price=pen.end.price + 0.25)),
        'day': replace(pen, end=replace(pen.end, position_day=_pd(9))),
        'merged index': replace(pen, end=replace(pen.end, merged_index=pen.end.merged_index + 4)),
        'status': replace(pen, status='candidate', confirmed_on=None),
        'confirmed_on': replace(pen, confirmed_on=_pd(9)),
    }
    for what, broken in mutations.items():
        forged = replace(snapshot, pens=(broken,) + snapshot.pens[1:])
        assert segment_input_digest(forged) != base, what

    assert segment_input_digest(replace(snapshot, as_of_day='2030-01-01')) != base

    # Two symbols / adjustment bases over one series: same digest, different source.
    a = segment_from_pens(snapshot, binding=binding_for(snapshot, symbol='SYM-A', adjust='qfq'))
    b = segment_from_pens(snapshot, binding=binding_for(snapshot, symbol='SYM-B', adjust='hfq'))
    assert a.input_digest == b.input_digest
    assert a.same_source(b) is False
    with pytest.raises(ValueError):
        a.assert_same_source(b)


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
        {'adjust': ''},
        {'adjust': 'unknown-ish'},
        {'as_of_day': '2026-01-05'},
        {'as_of_day': None},
        {'as_of_day': 20260105},
    ],
)
def test_invalid_binding_is_refused(overrides):
    pen_snapshot = build_chan_pen(SEGMENT_ORACLE_BARS)
    with pytest.raises(ValueError):
        segment_from_pens(pen_snapshot, binding=binding_for(pen_snapshot, **overrides))


def test_unknown_adjustment_is_an_explicit_label_never_a_guess():
    """F: an unverifiable 复权 base must be *representable*, not invented.

    Historical samples often have no trustworthy adjustment metadata.  Forcing a
    caller to declare ``none``/``qfq``/``hfq`` makes it fabricate a fact; the
    kernel therefore accepts the explicit label ``unknown``.  It is an identity
    label only — same geometry, different source — so it can never be mixed with
    a claimed base, and nothing ever rewrites it to a "real" one.
    """
    pen_snapshot = build_chan_pen(SEGMENT_ORACLE_BARS)
    as_of = pen_snapshot.as_of_day

    unknown = segment_from_pens(
        pen_snapshot, binding=SegmentBinding(symbol='000001.SZ', adjust='unknown', as_of_day=as_of)
    )
    assert unknown.binding.adjust == 'unknown'  # never silently rewritten
    assert unknown.segments == build(pen_snapshot).segments  # rules ignore the label

    claimed = {
        base: segment_from_pens(
            pen_snapshot, binding=binding_for(pen_snapshot, adjust=base)
        )
        for base in ('none', 'qfq', 'hfq')
    }
    for base, other in claimed.items():
        # Same series, same digest, different sample identity.
        assert unknown.input_digest == other.input_digest
        assert unknown.same_source(other) is False, base
        with pytest.raises(ValueError):
            unknown.assert_same_source(other)
    # An unknown base is not "the same line" as another unknown from another symbol.
    other_symbol = segment_from_pens(
        pen_snapshot, binding=binding_for(pen_snapshot, symbol='600000.SH', adjust='unknown')
    )
    assert unknown.same_source(other_symbol) is False
    # Two reads that both declare unknown *are* the same sample.
    repeated = segment_from_pens(
        pen_snapshot, binding=SegmentBinding(symbol='000001.SZ', adjust='unknown', as_of_day=as_of)
    )
    assert unknown.same_source(repeated) is True

    assert 'unknown' in SegmentBinding.__doc__
    assert 'unknown' in mod_doc()


def mod_doc():
    import src.core.chan_segment as segment_module

    return segment_module.__doc__ or ''


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


def test_a_pen_shorter_than_the_pen_index_gap_is_refused():
    """A 笔 spans at least ``MIN_INDEX_GAP`` merged bars; a two-bar stub may not
    enter the segment kernel even though its endpoints chain perfectly."""
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0])
    stub_pen = replace(
        snapshot.pens[0],
        end=replace(snapshot.pens[0].end, merged_index=snapshot.pens[0].start.merged_index + 3),
    )
    chained = (
        stub_pen,
        replace(snapshot.pens[1], start=replace(snapshot.pens[1].start, merged_index=stub_pen.end.merged_index)),
    ) + snapshot.pens[2:]
    with pytest.raises(ValueError, match='merged indices'):
        build(replace(snapshot, pens=chained))

    # The same edit one gap up (4) is legal: the check is the pen kernel's rule,
    # not an off-by-one of this module.
    legal = (
        replace(
            snapshot.pens[0],
            end=replace(snapshot.pens[0].end, merged_index=snapshot.pens[0].start.merged_index + 4),
        ),
        replace(snapshot.pens[1], start=replace(snapshot.pens[1].start, merged_index=4)),
    ) + snapshot.pens[2:]
    assert build(replace(snapshot, pens=legal)).segments


def test_position_days_must_be_iso_and_strictly_increasing():
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5])

    # A pen whose end precedes its own start.
    with pytest.raises(ValueError, match='not after its start'):
        build(
            replace(
                snapshot,
                pens=(
                    replace(snapshot.pens[0], start=replace(snapshot.pens[0].start, position_day='2030-01-01')),
                )
                + snapshot.pens[1:],
            )
        )

    # A pen that does not move forward in time at all.
    with pytest.raises(ValueError, match='not after its start'):
        build(
            replace(
                snapshot,
                pens=(
                    replace(snapshot.pens[0], end=replace(snapshot.pens[0].end, position_day=snapshot.pens[0].start.position_day)),
                )
                + snapshot.pens[1:],
            )
        )

    # A non-ISO day is not a date (string compare would silently accept it).
    with pytest.raises(ValueError, match='ISO day'):
        build(
            replace(
                snapshot,
                pens=(
                    replace(snapshot.pens[0], start=replace(snapshot.pens[0].start, position_day='2026-3-2')),
                )
                + snapshot.pens[1:],
            )
        )


def test_days_going_backwards_are_refused():
    """Later pens dated before earlier ones: a shuffle, not a prefix."""
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5])
    new_pens = []
    for i, pen in enumerate(snapshot.pens):
        if i >= 4:
            start_day = (date(2025, 1, 1) + timedelta(days=i)).isoformat()
            end_day = (date(2025, 1, 1) + timedelta(days=i + 1)).isoformat()
            confirm = (date.fromisoformat(end_day) + timedelta(days=1)).isoformat()
            pen = replace(
                pen,
                confirmed_on=confirm,
                start=replace(pen.start, position_day=start_day, confirmed_on=new_pens[-1].end.confirmed_on),
                end=replace(pen.end, position_day=end_day, confirmed_on=confirm),
            )
        new_pens.append(pen)
    with pytest.raises(ValueError):
        build(replace(snapshot, pens=tuple(new_pens)))


def test_confirmations_must_not_go_backwards():
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5])
    # Pen 4 freezes only on the last day of the sample, so pen 5 (frozen the next
    # day) would have to be confirmed *before* its predecessor: impossible in a
    # replay, where later legs freeze later.
    late = snapshot.as_of_day
    forged = tuple(
        replace(p, confirmed_on=late, end=replace(p.end, confirmed_on=late)) if i == 4 else p
        for i, p in enumerate(snapshot.pens)
    )
    with pytest.raises(ValueError, match='freezes out of order'):
        build(replace(snapshot, pens=forged))


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


def test_pen_status_and_endpoint_status_must_agree():
    snapshot = make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0])
    # A "confirmed" pen whose end endpoint still says candidate.
    forged = tuple(
        replace(p, end=replace(p.end, status='candidate', confirmed_on=None)) if i == 0 else p
        for i, p in enumerate(snapshot.pens)
    )
    with pytest.raises(ValueError):
        build(replace(snapshot, pens=forged))


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


def test_every_pen_inconsistency_is_refused_with_its_own_reason():
    """One case per guard: a foreign snapshot is re-verified field by field.

    A single "raises ValueError" test would let one guard cover for another, so
    each case pins the message of the guard it targets.
    """
    base = make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5])

    def edit(index=None, pen_changes=None, endpoint_changes=None, **snapshot_changes):
        if index is None:
            return replace(base, **snapshot_changes)
        pens = list(base.pens)
        pen = pens[index]
        if endpoint_changes:
            for side, changes in endpoint_changes.items():
                pen = replace(pen, **{side: replace(getattr(pen, side), **changes)})
        if pen_changes:
            pen = replace(pen, **pen_changes)
        pens[index] = pen
        return replace(base, pens=tuple(pens), **snapshot_changes)

    cases = [
        ('unknown status', edit(0, {'status': 'maybe'}), 'unknown status'),
        ('end index not after start', edit(0, endpoint_changes={'end': {'merged_index': 0}}),
         'not after'),
        ('joins two bottoms', edit(0, endpoint_changes={'end': {'kind': 'bottom'}}), 'joins two'),
        ('direction contradicts kinds', edit(0, {'direction': 'down'}), 'is labelled'),
        ('non-finite price', edit(0, endpoint_changes={'end': {'price': float('inf')}}),
         'non-finite'),
        ('down pen not below its start',
         edit(1, endpoint_changes={'end': {'price': 9.0, 'bar_high': 10.0}}), 'not below'),
        ('end day after as_of', edit(0, endpoint_changes={'end': {'position_day': '2030-01-01'}}),
         'after as_of_day'),
        ('confirmed after as_of', edit(0, {'confirmed_on': '2030-01-01'},
                                       endpoint_changes={'end': {'confirmed_on': '2030-01-01'}}),
         'after as_of_day'),
        ('confirmed pen, endpoint open',
         edit(0, endpoint_changes={'end': {'status': 'candidate'}}),
         'its end endpoint is'),
        ('candidate pen carrying a day', edit(0, {'status': 'candidate'}), 'carries a confirmation day'),
        ('candidate not the tail',
         edit(0, {'status': 'candidate', 'confirmed_on': None},
              endpoint_changes={'end': {'status': 'candidate', 'confirmed_on': None}}),
         'not the tail'),
        ('endpoint confirmed without a day',
         edit(0, endpoint_changes={'start': {'confirmed_on': None, 'status': 'confirmed'}}),
         'without a confirmation day'),
        ('endpoint frozen before its own position',
         edit(1, endpoint_changes={'start': {'confirmed_on': '2026-01-01'}}), 'before its own'),
        ('endpoint frozen after as_of',
         edit(1, endpoint_changes={'start': {'confirmed_on': '2030-01-01'}}), 'after as_of_day'),
        ('endpoint frozen but marked open',
         edit(1, endpoint_changes={'start': {'status': 'candidate'}}), 'carries a confirmation day'),
        ('shared endpoint with another day',
         edit(1, endpoint_changes={'start': {'position_day': '2026-03-05'}}), 'does not meet'),
        ('non-ISO confirmation day', edit(0, {'confirmed_on': '2026-3-8'},
                                         endpoint_changes={'end': {'confirmed_on': '2026-3-8'}}),
         'ISO day'),
        ('non-ISO as_of', edit(as_of_day='2026-3-30'), 'ISO day'),
    ]
    for name, snapshot, pattern in cases:
        try:
            build(snapshot)
        except ValueError as error:
            assert pattern in str(error), (name, str(error))
        else:  # pragma: no cover - only on a regression
            raise AssertionError(f'{name}: the forged snapshot was accepted')

    # A shared endpoint has one identity, and the kind is part of it: pen 1 is
    # internally consistent (bottom -> top) but claims a bottom where pen 0
    # printed a top.
    second = replace(
        base.pens[1],
        start=replace(base.pens[1].start, kind='bottom'),
        end=replace(base.pens[1].end, kind='top', price=9.0, bar_high=10.0),
        direction='up',
    )
    with pytest.raises(ValueError, match='one identity'):
        build(replace(base, pens=(base.pens[0], second) + base.pens[2:]))

    # A binding that is not a SegmentBinding is refused before anything else.
    with pytest.raises(ValueError, match='must be a SegmentBinding'):
        segment_from_pens(base, binding={'symbol': 'X', 'adjust': 'none', 'as_of_day': base.as_of_day})


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

    * the segment starts at the first pen's start (bottom 3.5, bar 1);
    * the characteristic sequence (down pens) is ``8.5/4.5``, ``9.5/5.5``,
      ``10.5/6.5``, ``10.0/6.0``; no two of them are contained, so the
      normalisation of v2 is the identity here and the top fractal sits at
      ``10.5/6.5`` — the middle element is the down pen 10.5 -> 6.5, so the
      segment's end is the up pen *before* it: 5.5 -> 10.5 at bar 21
      (day 2026-01-22), high 10.5;
    * the fractal has **no gap** (its second element's low 6.5 sits below the
      first element's high 8.5), so 第一种情况 applies and the segment ends;
    * the earliest day it may be published is the day the *third* characteristic
      element (the down pen 10.0 -> 6.0) was frozen: that pen's end (bottom 6.0,
      bar 33) freezes only when the top at bar 37 is confirmed, whose 3-bar
      window is sealed by bar 38 — i.e. from the bar-39 prefix on, 2026-02-09;
    * the evidence pens are 3, 5 and 7 — the last of them is *after* the
      segment's end pen 4, which is exactly why evidence and span are separate;
    * the next down segment cannot be confirmed yet (two characteristic
      elements only), so it stays ``candidate`` with its end drifted to 6.0.
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

    # Evidence: the three fractal elements, with the right one outside the span.
    assert [element.role for element in segment.evidence] == ['left', 'middle', 'right']
    assert [element.pen_indices for element in segment.evidence] == [(3,), (5,), (7,)]
    assert [(element.high, element.low) for element in segment.evidence] == [
        (9.5, 5.5),
        (10.5, 6.5),
        (10.0, 6.0),
    ]
    assert [element.confirmed_on for element in segment.evidence] == [_d(23), _d(31), _d(39)]
    assert segment.evidence_pen_indices == (3, 5, 7)
    assert segment.evidence_confirmed_on == _d(39)
    assert all(pen > segment.pen_end_index for pen in (7,))
    assert segment.evidence_pen_indices[-1] > segment.pen_end_index

    # Only confirmed pens are evidence for a confirmed segment.
    evidence = pen_snapshot.pens[segment.pen_start_index : segment.pen_end_index + 1]
    assert all(p.status == 'confirmed' for p in evidence)
    assert all(
        pen_snapshot.pens[i].status == 'confirmed' for i in segment.evidence_pen_indices
    )

    # The next (down) segment is a candidate whose end already drifted once.
    candidate = snap.open_segment
    assert candidate is not None
    assert (candidate.status, candidate.direction) == ('candidate', 'down')
    assert (candidate.start.price, candidate.start.position_day) == (10.5, _d(21))
    assert (candidate.end.price, candidate.end.position_day) == (6.0, _d(33))
    assert (candidate.end.revisions, candidate.end.drift_days) == (1, (_d(25),))
    assert candidate.confirmed_on is None
    assert candidate.reason == 'awaiting_confirmed_characteristic_pen'
    assert candidate.evidence == ()  # no fractal yet: no evidence to report
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
            assert segment.pens_used >= MIN_PENS
            for pen in pen_snapshot.pens[segment.pen_start_index : segment.pen_end_index + 1]:
                assert pen.status == 'confirmed'
            for pen_index in segment.evidence_pen_indices:
                assert pen_snapshot.pens[pen_index].status == 'confirmed'
                assert pen_snapshot.pens[pen_index].confirmed_on <= segment.confirmed_on
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
    # The 12.0 belongs to the *next* structure, which is published on its own.
    assert [s.pen_start_index for s in longer.segments][:2] == [0, 5]
    assert longer.segments[1].direction == 'down'
    assert longer.segments[1].pen_end_index > 5


# ---------------------------------------------------------------------------
# regression F1: a legal, fully confirmed pen series must not crash
# ---------------------------------------------------------------------------

#: The audit's counterexample.  Every pen is confirmed and legitimate (each up pen
#: rises, each down pen falls, endpoints chain, spacing is 4), yet v1 raised
#: ``ValueError: internal error: a segment shorter than three pens was confirmed``
#: at pen 7 because it kept the *first* top (4.00 at pen 0) as the segment end —
#: a pen that merely ties the fractal's high — and then found a span of one pen.
CRASH_PIVOTS = [1, 4, 2, 3, 1, 4, 2, 3, 1]


def test_a_fully_confirmed_series_never_raises_an_internal_error():
    snap = build(make_pens(CRASH_PIVOTS))
    assert snap.confirmed_segments, snap.rejections
    segment = snap.confirmed_segments[0]
    assert segment.status == 'confirmed'
    assert (segment.pen_start_index, segment.pen_end_index) == (0, 4)
    assert segment.end.price == 4.0
    assert segment.pens_used == 5
    # The end pen is the one carrying the fractal's high, not pen 0.
    assert segment.end.pen_index == 4
    # The two pens left over are one pen's worth of structure: a pending start,
    # not a second "segment".
    assert snap.open_segment is None
    assert snap.pending[0].pen_start_index == 5
    assert snap.pending[0].status == 'candidate'


def test_the_crash_case_survives_the_real_bar_pipeline():
    """Same counterexample through bars -> fractals -> pens -> segment."""
    bars = zigzag_bars([1.0, 4.0, 2.0, 3.0, 1.0, 4.0, 2.0, 3.0, 1.0, 4.0, 2.0])
    pen_snapshot = build_chan_pen(bars)
    assert len(pen_snapshot.pens) >= 9
    assert all(p.status == 'confirmed' for p in pen_snapshot.pens[:-1])
    snap = segment_from_pens(pen_snapshot, binding=binding_for(pen_snapshot))
    assert snap.confirmed_segments, snap.rejections
    for segment in snap.confirmed_segments:
        assert segment.pens_used >= MIN_PENS
        assert segment.confirmed_on is not None
        # Evidence carries the fractal's own extreme.
        middle = [e for e in segment.evidence if e.role == 'middle']
        assert len(middle) == 1
        side = middle[0].high if segment.direction == 'up' else middle[0].low
        assert side == segment.end.price


def test_every_exhaustive_short_series_is_answered_not_raised():
    """Small exhaustive sweep: the kernel answers (segment or pending), never
    raises, and never emits a line shorter than three pens."""
    import itertools

    checked = 0
    for length in (5, 6, 7):
        for pivots in itertools.product(range(1, 6), repeat=length):
            kinds = ['bottom' if i % 2 == 0 else 'top' for i in range(length)]
            if any(
                (pivots[i + 1] <= pivots[i]) if kinds[i] == 'bottom' else (pivots[i + 1] >= pivots[i])
                for i in range(length - 1)
            ):
                continue
            checked += 1
            snap = build(make_pens(list(pivots)))
            for segment in snap.segments:
                assert segment.pens_used >= MIN_PENS, (pivots, segment)
            assert snap.segments or snap.pending, pivots
    assert checked > 100


# ---------------------------------------------------------------------------
# regression F1b: an equal extreme must not pick a pen outside the fractal
# ---------------------------------------------------------------------------

def test_the_segment_ends_at_the_fractal_extreme_not_at_an_equal_earlier_top():
    """Pens 0 and 4 both top out at 4.00; only pen 4 is the fractal's high.

    v1 compared prices and kept the earlier equal top (pen 0), i.e. it ended the
    segment on a pen that the terminal fractal does not point at.
    """
    snapshot = make_pens(CRASH_PIVOTS)
    snap = build(snapshot)
    segment = snap.confirmed_segments[0]

    assert snapshot.pens[0].end_price == snapshot.pens[4].end_price == 4.0
    assert segment.end.pen_index == 4
    assert segment.end.position_day == _pd(5)
    assert segment.pen_end_index == 4
    # The fractal's middle element is pen 5 (down 4.00 -> 2.00): its high is the
    # 4.00 both tops share, and "keep_latest" makes pen 4 the end.
    assert [e.pen_indices for e in segment.evidence] == [(3,), (5,), (7,)]
    assert [e.high for e in segment.evidence] == [3.0, 4.0, 3.0]
    # The equal-price drift rejection is still recorded on the open end.
    assert [r.reason for r in snap.rejections if r.pen_index == 4] == ['equal_extreme_kept_earlier']


def test_an_equal_fractal_high_keeps_the_latest_carrier():
    """Two reverse pens carry the same high inside one middle element.

    ``keep_latest`` is stated, not incidental: ending at the earlier one would
    leave a same-price top inside the next segment, which no later segment could
    terminate on.
    """
    # Pivots 1, 4, 2, 5, 3, 5, 2.5, 4, 2.5: the reverse pens 5->3 (pen 3) and
    # 5->2.5 (pen 5) share the high 5.00, and the third one is contained in the
    # second (5.00/3.00 holds 5.00/2.50), so they merge into one middle element
    # whose high is carried by *both* pens 2 and 4.
    prices = [1.0, 4.0, 2.0, 5.0, 3.0, 5.0, 2.5, 4.0, 2.5]
    snap = build(make_pens(prices))
    assert len(snap.confirmed_segments) == 1
    segment = snap.confirmed_segments[0]
    assert make_pens(prices).pens[2].end_price == make_pens(prices).pens[4].end_price == 5.0
    assert segment.evidence[1].pen_indices == (3, 5)  # the merged middle element
    # keep_latest: the end is pen 4 (the later carrier), not pen 2.
    assert segment.pen_end_index == 4
    assert segment.end.price == 5.0
    assert segment.evidence_pen_indices == (1, 3, 5, 7)
    assert segment.pens_used == 5


# ---------------------------------------------------------------------------
# regression F3/F5: 包含处理 direction comes from the sequence, one implementation
# ---------------------------------------------------------------------------

def test_inclusion_processing_is_sequential_and_non_transitive():
    # e0 [5,8], e1 [6,10], e2 [7,9.8]: e2 is contained in e1 only.  Sequential
    # processing merges e1+e2 into [7,10] and leaves e0 alone -> two elements.
    # The trend into the contained pair is e0 -> e1 (rising), so the merge is up.
    normalized, blocked = normalize_characteristic_sequence(
        [(8.0, 5.0), (10.0, 6.0), (9.8, 7.0)]
    )
    assert blocked is None
    assert normalized == ((8.0, 5.0), (10.0, 7.0))
    # A transitive closure over the three ranges would collapse them into one.
    assert len(normalized) == 2

    # The first pair being contained is the case with no prior trend at all:
    # reported, never guessed (and never defaulted to the segment direction).
    assert normalize_characteristic_sequence(
        [(10.0, 5.0), (9.0, 6.0)]
    ) == (((10.0, 5.0), (9.0, 6.0)), 'characteristic_initial_containment')

    # A rising pair keeps high-high / low-low ...
    assert normalize_characteristic_sequence(
        [(8.0, 5.0), (10.0, 6.0), (9.5, 7.0)]
    ) == (((8.0, 5.0), (10.0, 7.0)), None)
    # ... and a falling pair keeps low-low, whichever way the *segment* runs.
    assert normalize_characteristic_sequence(
        [(10.0, 5.0), (9.0, 4.0), (8.5, 4.5)]
    ) == (((10.0, 5.0), (8.5, 4.0)), None)

    # No containment -> untouched, order preserved.
    assert normalize_characteristic_sequence(
        [(8.0, 5.0), (9.0, 6.0), (10.0, 7.0)]
    ) == (((8.0, 5.0), (9.0, 6.0), (10.0, 7.0)), None)

    with pytest.raises(ValueError):
        normalize_characteristic_sequence([(4.0, 5.0)])
    with pytest.raises(ValueError):
        normalize_characteristic_sequence([(float('nan'), 5.0)])


def test_the_merge_direction_is_the_sequence_trend_not_the_segment_direction():
    """The audit's F3 counterexample, element by element.

    The sequence turns down in the middle, so a merge "pinned to the segment
    direction" (v1's choice) and a merge that reads the sequence (第67课, v2)
    disagree — and v1's collapse also destroys the fractal.
    """
    ranges = [(5.0, 2.0), (4.0, 1.0), (4.0, 2.0), (5.0, 2.0), (4.0, 1.0)]

    def pinned(merge_direction, sequence):
        out = []
        for high, low in sequence:
            out.append((high, low))
            while len(out) >= 2:
                (h1, l1), (h2, l2) = out[-2], out[-1]
                contained = (h1 >= h2 and l1 <= l2) or (h2 >= h1 and l2 <= l1)
                if not contained:
                    break
                if merge_direction == 'up':
                    out[-2:] = [(max(h1, h2), max(l1, l2))]
                else:
                    out[-2:] = [(min(h1, h2), min(l1, l2))]
        return tuple(out)

    # v2: (5,2) then (4,1); the third element is contained and the trend into it
    # is down (5,2 -> 4,1), so it merges down into (4,1) — no collapse at all.
    assert normalize_characteristic_sequence(ranges) == (
        ((5.0, 2.0), (4.0, 1.0), (5.0, 2.0), (4.0, 1.0)),
        None,
    )
    # v1's pinned directions both give a different sequence.
    assert pinned('up', ranges) == ((5.0, 2.0), (4.0, 1.0))
    assert pinned('down', ranges) == ((5.0, 2.0), (4.0, 1.0), (5.0, 2.0), (4.0, 1.0))
    # (down happens to coincide here; the flip case below separates them.)

    # A sequence that turns *both* ways: neither pinned direction can match it.
    mixed = [(5.0, 1.0), (7.0, 5.0), (10.0, 3.0), (8.0, 4.0), (9.0, 2.0)]
    assert normalize_characteristic_sequence(mixed) == (
        ((5.0, 1.0), (10.0, 5.0), (8.0, 2.0)),
        None,
    )
    assert pinned('up', mixed) == ((5.0, 1.0), (10.0, 5.0), (9.0, 4.0))
    assert pinned('down', mixed) == ((5.0, 1.0), (7.0, 2.0))


def test_only_the_first_pair_can_lack_a_merge_direction():
    """Two normalised, non-contained elements are always strictly ordered.

    Containment is inclusive, so an equal high or an equal low *is* containment:
    once the first pair is outside each other, the trend into every later pair is
    defined.  Therefore the only reachable "no direction" state is the first pair
    being contained — the second guard reason exists for a crossing pair that the
    normalisation should never produce, and is asserted to stay unreachable here.
    """
    import itertools

    ranges = [(t, b) for t in range(3, 9) for b in range(1, 6) if t > b]
    blocked_reasons = set()
    for sequence in itertools.product(ranges, repeat=3):
        _, blocked = normalize_characteristic_sequence(list(sequence))
        blocked_reasons.add(blocked)
    assert blocked_reasons == {None, 'characteristic_initial_containment'}


def test_the_production_walk_uses_the_sequence_direction():
    """F3 through the *real* entry point (v1 stalled here forever).

    The same pen series under a pinned-direction merge never prints the fractal,
    so v1 answered ``candidate`` at pen 0 for ever.  v2 must confirm at pen 6.
    """
    prices = [1.0, 5.0, 2.0, 4.0, 1.0, 4.0, 2.0, 5.0, 2.0, 4.0, 1.0]
    snap = build(make_pens(prices))
    assert len(snap.confirmed_segments) == 1
    segment = snap.confirmed_segments[0]
    assert (segment.pen_start_index, segment.pen_end_index) == (0, 6)
    assert segment.end.price == 5.0
    assert segment.reason == 'first_case_no_gap'
    # ... and its own characteristic sequence is what the helper produces from the
    # reverse pens that existed at that moment, not the two-element collapse v1
    # saw (v1 merged the contained pair "up" and lost the fractal).
    pens = make_pens(prices).pens
    ranges = [
        (max(p.start_price, p.end_price), min(p.start_price, p.end_price))
        for p in pens[1:10:2]
    ]
    assert normalize_characteristic_sequence(ranges) == (
        ((5.0, 2.0), (4.0, 1.0), (5.0, 2.0), (4.0, 1.0)),
        None,
    )
    assert segment.characteristic_elements == 4
    # The fractal's own pens are 3, 5, 7 and 9; the last of them lies *after* the
    # segment's end pen 6, which is why evidence and span are reported apart.
    assert segment.evidence_pen_indices == (3, 5, 7, 9)
    assert max(segment.evidence_pen_indices) > segment.pen_end_index


def test_a_merge_that_flips_direction_mid_sequence_still_terminates():
    """Up segment whose sequence both rises and falls (down merge inside it)."""
    prices = [1.0, 5.0, 1.0, 7.0, 5.0, 10.0, 3.0, 8.0, 4.0, 9.0, 2.0]
    snap = build(make_pens(prices))
    assert len(snap.confirmed_segments) == 1
    segment = snap.confirmed_segments[0]
    assert (segment.pen_start_index, segment.pen_end_index) == (0, 4)
    assert segment.end.price == 10.0
    # Ranges [(5,1),(7,5),(10,3),(8,4),(9,2)]: the third and fifth elements are
    # contained and merged in *opposite* directions -> three elements remain.
    assert segment.characteristic_elements == 3
    assert segment.evidence_pen_indices == (1, 3, 5, 7)


@pytest.mark.parametrize(
    'prices',
    [
        [4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5, 9.0, 5.5],
        [1.0, 5.0, 2.0, 4.0, 1.0, 4.0, 2.0, 5.0, 2.0, 4.0, 1.0],
        [1.0, 5.0, 1.0, 7.0, 5.0, 10.0, 3.0, 8.0, 4.0, 9.0, 2.0],
        [10.0, 6.0, 8.5, 4.5, 7.0, 5.5, 7.8, 5.0],
        [5.0, 12.0, 6.0, 9.0, 5.0, 10.5, 7.0, 9.5, 6.5, 8.0],
        [5.0, 8.0, 5.0, 12.0, 9.0, 11.5, 8.5],
        [1.0, 4.0, 2.0, 3.0, 1.0, 4.0, 2.0, 3.0, 1.0],
        [3.0, 5.0, 3.0, 4.0, 1.0, 5.0, 3.0, 4.0, 1.0, 5.0],
    ],
)
def test_the_kernel_and_a_helper_based_walk_agree(prices):
    """One 包含处理 implementation: the production walk and a walk built from the
    public normaliser must produce the same confirmed segments and the same first
    refusal reason."""
    first_kind = 'bottom' if prices[1] > prices[0] else 'top'
    oracle = helper_oracle(prices, first_kind=first_kind)
    snap = build(make_pens(prices, first_kind=first_kind))

    expected = [(start, end) for start, end, reason in oracle if reason == 'case1']
    actual = [(s.pen_start_index, s.pen_end_index) for s in snap.confirmed_segments]
    assert actual == expected, (prices, oracle)

    stop = next((item for item in oracle if item[2] != 'case1'), None)
    if stop is not None and stop[2] not in ('tail', 'no_overlap'):
        # The production refusal must carry the same reason.
        reasons = {s.reason for s in snap.segments if s.status == 'blocked'}
        reasons |= {p.reason for p in snap.pending if p.status == 'blocked'}
        expected_reason = {
            'case2': 'characteristic_gap_case2',
            'characteristic_fractal_below_prior_extreme':
                'characteristic_fractal_below_prior_extreme',
            'characteristic_initial_containment': 'characteristic_initial_containment',
        }.get(stop[2], stop[2])
        assert expected_reason in reasons, (prices, stop, reasons)


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
    assert pending.end_pen_index == 2
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

    # The day the third pen freezes the trio is complete, but a *line* still needs
    # three pens up to its end: the start stays pending until the end can be that
    # far away, then it becomes a segment.
    third_frozen = build(make_pens([4.0, 8.0, 5.0, 9.0], confirmed=3))
    assert third_frozen.segments == ()
    assert third_frozen.pending[0].pen_start_index == 0
    assert third_frozen.pending[0].reason == 'awaiting_confirmed_characteristic_pen'
    assert third_frozen.pending[0].pens_used == 1

    one_more = build(make_pens([4.0, 8.0, 5.0, 9.0, 6.0], confirmed=4))
    assert one_more.pending == ()
    assert one_more.segments[0].pen_start_index == 0
    assert one_more.segments[0].pens_used == MIN_PENS


def test_a_decision_point_shorter_than_three_pens_is_never_a_segment():
    """F7: v1 published `blocked` Segments covering a single pen.

    A one-pen span is not a 线段 at all, so it is reported as a pending start
    with the reason kept — not as a Segment, and not as an internal error.
    """
    # Taint arrives before the segment could move off its start pen: v1 emitted a
    # 1-pen blocked Segment here.
    tainted = build(make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0], statuses={3: 'blocked'}))
    assert tainted.segments == ()
    assert len(tainted.pending) == 1
    assert tainted.pending[0].status == 'blocked'
    assert tainted.pending[0].reason == 'tainted_pen'
    assert tainted.pending[0].pens_used == 1
    assert 'fewer than the 3' in tainted.pending[0].detail

    # Same rule for a candidate that only has one pen behind it.
    short = build(make_pens([1.0, 5.0, 2.0, 4.0, 1.0]))
    assert short.segments == ()
    assert short.pending[0].status == 'candidate'
    assert short.pending[0].pens_used == 1
    assert short.open_segment is None

    # With three pens behind the taint the refusal *is* a Segment (>= 3 pens).
    late_taint = build(make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0], statuses={5: 'blocked'}))
    assert len(late_taint.segments) == 1
    assert late_taint.segments[0].status == 'blocked'
    assert late_taint.segments[0].reason == 'tainted_pen'
    assert late_taint.segments[0].pens_used == 3


# ---------------------------------------------------------------------------
# 特征序列 包含处理: initial containment (no prior trend)
# ---------------------------------------------------------------------------

def test_initial_containment_cannot_invent_a_merge_direction():
    """e0 [5,10], e1 [6,9]: e1 is inside e0 and no trend exists yet -> blocked.

    v2 reports it as a pending blocked start: the decision point is one pen away
    from the chain start, so there is no line to publish either.
    """
    snap = build(make_pens([5.0, 10.0, 5.0, 9.0, 6.0]))
    assert snap.confirmed_segments == ()
    assert snap.segments == ()
    assert len(snap.pending) == 1
    pending = snap.pending[0]
    assert pending.status == 'blocked'
    assert pending.reason == 'characteristic_initial_containment'
    # No end was ever a candidate: the refusal is a decision at a pen, so the
    # span fields stay empty and ``blocked_at_pen_index`` names the evidence.
    assert pending.end_pen_index is None
    assert pending.pens_used == 0
    assert pending.blocked_at_pen_index == 3
    assert [r.reason for r in snap.rejections] == ['characteristic_initial_containment']


def test_initial_containment_is_always_a_pending_start():
    """The first contained pair is only ever two elements wide, i.e. pen 3.

    A containment at the first pair cannot have moved the segment's end (the two
    candidate tops are equal or lower), so the refusal can never cover three
    pens — it is always reported as a pending start, never as a Segment.
    """
    for prices in ([5.0, 10.0, 5.0, 9.0, 6.0], [5.0, 10.0, 5.0, 9.0, 6.0, 9.5, 5.5]):
        snap = build(make_pens(prices))
        assert snap.segments == ()
        assert snap.pending[0].status == 'blocked'
        assert snap.pending[0].reason == 'characteristic_initial_containment'
        assert snap.pending[0].pens_used < MIN_PENS
        assert snap.pending[0].blocked_at_pen_index == 3


def test_initial_containment_block_names_the_pen_that_triggered_it():
    """F-A: pivots 2/6/3/6/3.5/5/2.5 — containment first lands on pen 3.

    The first characteristic pair is pen 1 [3,6] / pen 3 [3,6]: pen 3 is
    contained by pen 1 and no earlier element pair exists, so the merge
    direction is undefined and the chain blocks.  The report must say *that*:
    the pending start stays pen 0 (nothing is re-anchored), the decision points
    at pen 3, and no one-pen span is claimed for a decision that examined four
    pens.
    """
    snap = build(make_pens([2.0, 6.0, 3.0, 6.0, 3.5, 5.0, 2.5]))

    assert snap.segments == ()
    assert snap.confirmed_segments == ()
    assert len(snap.pending) == 1
    pending = snap.pending[0]
    assert pending.status == 'blocked'
    assert pending.reason == 'characteristic_initial_containment'
    assert (pending.pen_start_index, pending.start_price) == (0, 2.0)
    # The evidence pen, not the chain start: the block happened at pen 3.
    assert pending.blocked_at_pen_index == 3
    # No would-be end: the containment never produced a candidate end.
    assert pending.end_pen_index is None
    assert pending.pens_used == 0
    assert 'pen 3' in pending.detail and 'pen 1' in pending.detail
    # The detail must not claim the decision covered a single pen.
    assert 'fewer than the 3' not in pending.detail
    assert [(r.pen_index, r.reason) for r in snap.rejections] == [
        (2, 'equal_extreme_kept_earlier'),
        (3, 'characteristic_initial_containment'),
    ]
    # The no-short-line rule still holds: nothing was published as a 线段.
    assert snap.blocked_segments == ()
    assert snap.candidate_segments == ()


def test_a_blocked_pending_cannot_claim_the_chain_start_as_its_decision():
    """Guard the shape: a blocked pending either has a real span or a decision pen."""
    snap = build(make_pens([2.0, 6.0, 3.0, 6.0, 3.5, 5.0, 2.5]))
    pending = snap.pending[0]
    assert not (
        pending.end_pen_index == pending.pen_start_index
        and pending.pens_used == 1
        and 'fewer than the 3' in pending.detail
    )
    with pytest.raises(ValueError):
        # A decision pen belongs to a blocked refusal, and it is inside the chain.
        SegmentPending(
            pen_start_index=0,
            direction='up',
            status='candidate',
            reason='awaiting_three_pens',
            start_position_day=_pd(0),
            start_price=2.0,
            confirmed_pens=1,
            blocked_at_pen_index=3,
        )
    with pytest.raises(ValueError):
        SegmentPending(
            pen_start_index=4,
            direction='up',
            status='blocked',
            reason='characteristic_initial_containment',
            start_position_day=_pd(4),
            start_price=2.0,
            confirmed_pens=3,
            blocked_at_pen_index=1,
        )


def test_initial_containment_block_is_not_re_anchored_as_the_prefix_grows():
    """F-C: the conservative block is replay-stable — no future data unlocks it.

    v2 has no re-anchoring rule and no second characteristic sequence for this
    case, so appending pens may never move the chain start forward or turn the
    refusal into a line.  If this ever fails, recall was bought with lookahead.
    """
    base = [2.0, 6.0, 3.0, 6.0, 3.5, 5.0, 2.5]
    for extra in ([], [4.5], [4.5, 2.0], [4.5, 2.0, 5.5], [4.5, 2.0, 5.5, 2.2, 4.8]):
        snap = build(make_pens(base + extra))
        assert snap.segments == (), extra
        assert len(snap.pending) == 1
        pending = snap.pending[0]
        assert (pending.pen_start_index, pending.start_price) == (0, 2.0), extra
        assert pending.reason == 'characteristic_initial_containment'
        assert pending.blocked_at_pen_index == 3


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
    # The fractal is still reported as evidence, without a confirmation day.
    assert segment.evidence_pen_indices == (1, 3, 5)
    assert segment.evidence_confirmed_on is not None
    assert segment.confirmed_on is None
    gap_rejections = [r for r in snap.rejections if r.reason == 'characteristic_gap_case2']
    assert len(gap_rejections) == 1
    assert (gap_rejections[0].pen_index, gap_rejections[0].price) == (2, 12.0)
    # The chain stops at a blocked boundary: nothing downstream is invented.
    assert snap.pending == ()
    assert snap.open_segment is None


def test_a_fractal_below_a_prior_extreme_is_blocked_not_republished():
    """Elements 12 / 9 / 10.5 / 9.5: the fractal sits at 10.5 but 12 came first.

    The segment's real extreme is the earlier 12.0, so the fractal at 10.5 may
    not be used as the segment's end (that would silently move the high).  The
    end pen is the fractal's carrier (4), not the earlier 12.0 pen, so the
    blocked record is still a >= 3 pen span.
    """
    snap = build(make_pens([5.0, 12.0, 6.0, 9.0, 5.0, 10.5, 7.0, 9.5, 6.5, 8.0], confirmed=8))
    assert snap.confirmed_segments == ()
    assert snap.segments[0].status == 'blocked'
    assert snap.segments[0].reason == 'characteristic_fractal_below_prior_extreme'
    assert snap.segments[0].confirmed_on is None
    assert snap.segments[0].pen_end_index == 4
    assert snap.segments[0].end.price == 10.5
    assert snap.segments[0].pens_used == 5


def test_a_down_segment_confirms_on_a_bottom_fractal_without_gap():
    """The symmetric path: 底分型 of an up-pen characteristic sequence.

    Points: top 10.0, bottom 6.0, top 8.5, bottom 4.5, top 7.0, bottom 5.5,
    top 7.8, bottom 5.0.  The down segment's characteristic sequence (up pens)
    is ``8.5/6.0``, ``7.0/4.5``, ``7.8/5.5``: a 底分型 at ``7.0/4.5`` with no
    gap (7.0 is not below the first element's low 6.0), so it ends at the
    bottom 4.5 (pivot 3, i.e. the carrier's own pen) and waits for the third
    element (pen 5) to freeze.
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
    assert [e.pen_indices for e in segment.evidence] == [(1,), (3,), (5,)]
    assert segment.evidence_pen_indices == (1, 3, 5)
    assert segment.pen_end_index == 2  # the evidence pen 5 is after the end

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
        [4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5], statuses={5: 'blocked'}
    )
    snap = build(snapshot)
    assert snap.confirmed_segments == ()
    blocked = [s for s in snap.segments if s.status == 'blocked']
    assert len(blocked) == 1
    assert blocked[0].reason == 'tainted_pen'
    assert blocked[0].confirmed_on is None
    assert blocked[0].pens_used == MIN_PENS
    assert any(r.reason == 'tainted_pen' for r in snap.rejections)
    # Nothing after the taint is published.
    assert snap.pending == ()

    # A taint that lands before the end could leave the start at all is a pending
    # refusal, not a one-pen "segment".
    early = build(make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0], statuses={3: 'blocked'}))
    assert early.segments == ()
    assert early.pending[0].status == 'blocked'
    assert early.pending[0].reason == 'tainted_pen'


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


def test_evidence_is_a_first_class_field_not_a_span_extension():
    """Evidence is reported, typed and separate from ``pen_end_index``."""
    segment = build(make_pens(CRASH_PIVOTS)).confirmed_segments[0]
    assert isinstance(segment.evidence[0], FractalEvidence)
    # pen_end_index never absorbs evidence pens: the right element is later.
    assert max(segment.evidence_pen_indices) > segment.pen_end_index
    # A blocked segment keeps its evidence but no confirmation day.
    blocked = build(
        make_pens([5.0, 8.0, 5.0, 12.0, 9.0, 11.5, 8.5])
    ).segments[0]
    assert blocked.evidence and blocked.confirmed_on is None
    assert blocked.evidence_confirmed_on is not None


def test_pending_records_the_refused_span():
    pending = build(make_pens([5.0, 10.0, 4.0, 4.5])).pending[0]
    assert isinstance(pending, SegmentPending)
    assert pending.end_pen_index == 2
    assert pending.pens_used == 3
    assert pending.direction == 'up'

    # When not even an end is known (fewer than three pens) the span is unknown,
    # not zero-pens-of-evidence.
    unknown = build(make_pens([4.0, 8.0, 5.0])).pending[0]
    assert unknown.end_pen_index is None
    assert unknown.pens_used == 0


def test_snapshot_properties_expose_only_confirmed_and_flagged_segments():
    confirmed = build(make_pens(CRASH_PIVOTS))
    assert confirmed.blocked_segments == ()
    segment = confirmed.confirmed_segments[0]
    assert (segment.start_price, segment.end_price) == (segment.start.price, segment.end.price)
    assert confirmed.candidate_segments == ()

    open_tail = build(make_pens([4.0, 8.0, 5.0, 9.0, 6.0, 10.0, 7.0, 9.5, 6.5], confirmed=7))
    assert [s.status for s in open_tail.candidate_segments] == ['candidate']
    assert open_tail.open_segment is open_tail.candidate_segments[0]

    blocked = build(make_pens([5.0, 8.0, 5.0, 12.0, 9.0, 11.5, 8.5]))
    assert [s.reason for s in blocked.blocked_segments] == ['characteristic_gap_case2']
    assert blocked.candidate_segments == ()


# ---------------------------------------------------------------------------
# confirmed_segments: the only consumer-safe view (anti-misuse regressions)
# ---------------------------------------------------------------------------

def test_confirmed_segments_returns_confirmed_lines_and_nothing_else():
    """``confirmed_segments`` is the consumer view: winner-takes-confirmed-only.

    A downstream 中枢/higher-level consumer must read this property, never
    ``segments`` (or the geometry holes from ``blocked`` become pivots, and a
    drifting ``candidate`` tail becomes a fixed line).  Verified at the source:
    it filters on status *and* on the one reason that may carry ``confirmed``.
    """
    open_tail = build(make_pens(SEGMENT_ZIGZAG))
    assert [s.status for s in open_tail.segments] == ['confirmed', 'candidate']
    assert [s.status for s in open_tail.confirmed_segments] == ['confirmed']
    assert all(s.status == 'confirmed' for s in open_tail.confirmed_segments)
    assert all(s.reason == 'first_case_no_gap' for s in open_tail.confirmed_segments)
    # The candidate tail is still reachable through its own property.
    assert open_tail.open_segment is open_tail.segments[-1]

    blocked = build(make_pens([5.0, 8.0, 5.0, 12.0, 9.0, 11.5, 8.5]))
    assert blocked.blocked_segments
    assert blocked.confirmed_segments == ()


def test_confirmed_segments_survives_a_spliced_blocked_or_candidate_entry():
    """Hostile/misused call sites can hand a doctored snapshot downstream.

    The kernel stops the chain at a ``blocked`` refusal and only the tail may be
    a ``candidate``, so these tuples cannot come out of a real run.  A consumer
    view that simply forwarded ``segments`` would publish them as lines; the
    property must refuse them even when the tuple is forged.
    """
    open_tail = build(make_pens(SEGMENT_ZIGZAG))
    confirmed_seg, candidate_seg = open_tail.segments
    blocked_seg = build(make_pens([5.0, 8.0, 5.0, 12.0, 9.0, 11.5, 8.5])).segments[0]
    assert (candidate_seg.status, blocked_seg.status) == ('candidate', 'blocked')

    spliced = replace(open_tail, segments=(blocked_seg, confirmed_seg, candidate_seg))
    assert spliced.confirmed_segments == (confirmed_seg,)
    assert len(spliced.blocked_segments) == 1 and len(spliced.candidate_segments) == 1

    # A blocked/candidate entry that has been edited to look frozen is still not
    # a line: ``confirmed`` is only 第67课 第一种情况 without a gap.
    forged = replace(
        blocked_seg,
        status='confirmed',
        confirmed_on=max(d for d in (blocked_seg.evidence_confirmed_on, _pd(6)) if d),
    )
    assert forged.status == 'confirmed' and forged.reason == 'characteristic_gap_case2'
    doctored = replace(open_tail, segments=(forged, confirmed_seg))
    assert doctored.confirmed_segments == (confirmed_seg,)
    # Nothing about the forgery is hidden: the audit view still carries it, and it
    # is not promoted into either of the honest status buckets.
    assert doctored.segments[0] is forged
    assert doctored.blocked_segments == ()
    assert doctored.candidate_segments == ()
    # The unedited blocked entry *is* still visible in the audit bucket.
    assert spliced.blocked_segments == (blocked_seg,)


def test_confirmed_segments_is_a_pen_partition_nothing_double_counted():
    """``pen_start..pen_end`` is the geometry; evidence pens are *not* in it.

    Evidence (the terminal fractal's elements) deliberately reaches past the
    segment's end, into the next segment's span.  A consumer that unions spans
    with ``evidence_pen_indices`` would count those pens twice, so the partition
    is defined on the span only — pinned here on a real two-segment chain.
    """
    prices = [10.0, 11.0, 10.0, 12.0, 11.5, 13.0, 12.0, 12.5, 10.5, 12.0, 11.0, 12.0, 11.5, 12.5, 11.0, 12.0, 11.0]
    snap = build(make_pens(prices))
    confirmed = snap.confirmed_segments
    assert len(confirmed) >= 2

    covered = [i for s in confirmed for i in range(s.pen_start_index, s.pen_end_index + 1)]
    assert covered == list(range(covered[-1] + 1))  # contiguous, no gap, no overlap
    assert len(covered) == len(set(covered))
    assert sum(s.pens_used for s in confirmed) == len(covered)

    # At least one evidence pen sits beyond the frozen end, inside the next span:
    # unioning the two sets would count that pen twice.
    beyond = [
        (segment.pen_end_index, index, nxt.pen_start_index, nxt.pen_end_index)
        for segment, nxt in zip(confirmed, confirmed[1:])
        for index in segment.evidence_pen_indices
        if index > segment.pen_end_index and nxt.pen_start_index <= index <= nxt.pen_end_index
    ]
    assert beyond, [(s.pen_end_index, s.evidence_pen_indices) for s in confirmed]
    # ...and the property itself never folds evidence into a span.
    assert all(max(s.evidence_pen_indices) > s.pen_end_index or s.pen_end_index < s.pen_start_index
               for s in confirmed)


def test_the_confirmed_view_is_documented_as_the_consumer_contract():
    """The docstrings must say which view is safe, not just which is complete."""
    import src.core.chan_segment as mod

    doc = mod.__doc__ or ''
    assert 'confirmed_segments' in doc
    property_doc = mod.SegmentSnapshot.confirmed_segments.__doc__ or ''
    assert 'confirmed' in property_doc
    assert 'candidate' in property_doc and 'blocked' in property_doc


def test_the_trend_helper_refuses_to_invent_a_direction():
    """``_trend`` is the one place a merge direction is read; it never defaults."""
    from src.core.chan_segment import CharacteristicElement, _trend

    def element(high, low):
        return CharacteristicElement(high, low, 0, 0, (0,), None)

    assert _trend(element(5.0, 1.0), element(6.0, 2.0)) == 'up'
    assert _trend(element(6.0, 2.0), element(5.0, 1.0)) == 'down'
    # Equal high, equal low and crossing pairs all define no direction.
    assert _trend(element(5.0, 1.0), element(5.0, 2.0)) is None
    assert _trend(element(5.0, 1.0), element(6.0, 1.0)) is None
    assert _trend(element(5.0, 1.0), element(6.0, 0.5)) is None


def test_module_scope_is_honest():
    import src.core.chan_segment as mod

    doc = mod.__doc__ or ''
    assert 'Not implemented' in doc
    assert '第二种情况' in doc
    assert '中枢' in doc  # explicitly out of scope, not accidentally claimed
    # F-C: the recall limits of the conservative rules are stated, not implied.
    assert 'Recall limits' in doc
    assert 're-anchoring' in doc
    assert 'characteristic_initial_containment' in doc
    for forbidden in ('zhongshu', 'centre', 'center', 'pivot', 'buy_sell'):
        assert not any(forbidden in name.lower() for name in dir(mod)), forbidden
    assert mod.SEGMENT_KERNEL_VERSION == 'chan-segment-v2'
