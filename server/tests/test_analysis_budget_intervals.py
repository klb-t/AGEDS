import pytest

from server.app.analysis import intervals as iv


def test_union_merges_overlaps_and_touching_without_double_counting():
    parallel = [(0, 100), (50, 150), (150, 160), (200, 200), (300, 250)]
    assert iv.normalize(parallel) == [(0.0, 160.0)]
    assert iv.total(parallel) == 160
    assert iv.union([(0, 10)], [(5, 20)], [(30, 40)]) == [(0.0, 20.0), (30.0, 40.0)]


def test_complement_intersect_subtract_clip():
    busy = [(10, 20), (15, 30), (50, 60)]
    assert iv.complement(busy, 0, 100) == [(0, 10.0), (30.0, 50.0), (60.0, 100)]
    assert iv.complement([], 0, 5) == [(0, 5)]
    assert iv.intersect(busy, [(25, 55)]) == [(25.0, 30.0), (50.0, 55.0)]
    assert iv.subtract([(0, 100)], busy) == [(0.0, 10.0), (30.0, 50.0), (60.0, 100.0)]
    assert iv.clip(busy, 12, 52) == [(12.0, 30.0), (50.0, 52.0)]
    assert iv.subtract([], busy) == []


def test_sessionize_gap_is_strict_and_overlaps_join():
    items = [{'event_id': 3, 't_start': 0, 't_end': None},
             {'event_id': 1, 't_start': 60, 't_end': 100},
             {'event_id': 2, 't_start': 90, 't_end': 95},          # overlapping, joins
             {'event_id': 4, 't_start': 400, 't_end': None},       # gap exactly 300 -> new session
             {'event_id': 5, 't_start': 699.5, 't_end': None}]     # gap 299.5 -> joins
    sessions = iv.sessionize(items, 300)
    assert [(s['start'], s['end'], s['keys']) for s in sessions] == [(0, 100, [3, 1, 2]), (400, 699.5, [4, 5])]
    zero = iv.sessionize(items[:3], 0)
    assert [s['keys'] for s in zero] == [[3], [1, 2]]
    with pytest.raises(ValueError):
        iv.sessionize(items, -1)


def test_weighted_segments_union_by_max_within_key_and_breakpoints():
    segs = iv.weighted_segments([(0, 10, 'a', 1.0), (5, 15, 'a', 0.5), (5, 8, 'b', 0.3)], breakpoints=[12])
    assert segs == [(0.0, 5.0, {'a': 1.0}), (5.0, 8.0, {'a': 1.0, 'b': 0.3}), (8.0, 10.0, {'a': 1.0}),
                    (10.0, 12.0, {'a': 0.5}), (12.0, 15.0, {'a': 0.5})]
    # identical parallel intervals of one key never add up
    assert iv.weighted_segments([(0, 10, 'a', 1), (0, 10, 'a', 1)]) == [(0.0, 10.0, {'a': 1.0})]
