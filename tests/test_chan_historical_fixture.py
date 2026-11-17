"""Retrospective real-price geometry fixture; it does not prove PIT validity."""
import json
from pathlib import Path

from src.core.chan_structure import RawBar, build_chan_structure


def test_002361_candidate_and_confirmation_are_separate():
    fixture = json.loads((Path(__file__).parent / 'fixtures' / 'chan_002361_202609.json').read_text(encoding='utf-8'))
    bars = [RawBar(**row) for row in fixture['bars']]
    expectation = fixture['manual_expectation']
    earlier = build_chan_structure(bars[:-1])
    assert [(f.kind, f.position_day, f.position_price, f.confirmed_on) for f in earlier.candidates] == [
        (expectation['kind'], expectation['position_day'], expectation['position_price'], None)
    ]
    assert earlier.confirmed_fractals == ()
    assert (earlier.merged_bars[1].high, earlier.merged_bars[1].low) == (expectation['merged_high'], expectation['merged_low'])
    final = build_chan_structure(bars)
    assert [(f.kind, f.position_day, f.position_price, f.confirmed_on) for f in final.confirmed_fractals] == [
        (expectation['kind'], expectation['position_day'], expectation['position_price'], expectation['confirmed_on'])
    ]
