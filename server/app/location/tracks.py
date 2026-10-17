"""Streaming statistics of tracks and routes, computed by code (never copied from the file).

Distance is the sum of haversine legs between consecutive points **inside one
segment** (a gap between ``<trkseg>`` elements is not travelled distance); the
duration is ``last - first`` of the points' fixed UTC instants in file order; the
time span ``[min, max]`` is reported separately when the times are not monotonic.
"""
from __future__ import annotations

from ..timeinfo import TimeCandidate
from . import geo, store


class TrackStats:
    def __init__(self, store_decimals: int | None):
        self.store_decimals = store_decimals
        self.points = 0
        self.timed = 0
        self.segments = 0
        self.distance_m = 0.0
        self.previous = None
        self.first_time: TimeCandidate | None = None
        self.last_time: TimeCandidate | None = None
        self.min_us = self.max_us = None
        self.last_us = None
        self.backwards = 0
        self.unknown_zone_times = 0
        self.ele_min = self.ele_max = None
        self.box = None
        self.decimals = None

    def new_segment(self) -> None:
        self.segments += 1
        self.previous = None

    def add(self, coord: geo.Coordinate, time: TimeCandidate | None, altitude=None) -> None:
        if self.segments == 0:
            self.segments = 1
        self.points += 1
        if self.previous is not None:
            self.distance_m += geo.haversine_m(self.previous[0], self.previous[1], coord.lat, coord.lon)
        self.previous = (coord.lat, coord.lon)
        lat, lon, decimals = geo.round_to(coord, self.store_decimals)
        self.decimals = decimals if self.decimals is None else min(self.decimals, decimals)
        if self.box is None:
            self.box = {'south': lat, 'north': lat, 'west': lon, 'east': lon}
        else:
            self.box = {'south': min(self.box['south'], lat), 'north': max(self.box['north'], lat),
                        'west': min(self.box['west'], lon), 'east': max(self.box['east'], lon)}
        altitude = geo.finite(altitude)
        if altitude is not None:
            self.ele_min = altitude if self.ele_min is None else min(self.ele_min, altitude)
            self.ele_max = altitude if self.ele_max is None else max(self.ele_max, altitude)
        if time is None:
            return
        us = store.utc_us(time)
        if us is None:
            self.unknown_zone_times += 1
            return
        self.timed += 1
        if self.first_time is None:
            self.first_time = time
        if self.last_us is not None and us < self.last_us:
            self.backwards += 1
        self.last_us, self.last_time = us, time
        self.min_us = us if self.min_us is None else min(self.min_us, us)
        self.max_us = us if self.max_us is None else max(self.max_us, us)

    def summary(self) -> dict:
        elevation = None if self.ele_min is None else {'min_m': self.ele_min, 'max_m': self.ele_max}
        duration = None
        if self.first_time is not None and self.last_time is not None:
            duration = (store.utc_us(self.last_time) - store.utc_us(self.first_time)) / 1e6
        return {'points': self.points, 'timed_points': self.timed, 'segments': self.segments,
                'distance_m': round(self.distance_m, 3), 'distance_model': geo.DISTANCE_MODEL,
                'distance_scope': 'sum of legs inside each segment at source precision',
                'duration_s': duration, 'span': {'min_utc': store.us_to_iso(self.min_us),
                                                 'max_utc': store.us_to_iso(self.max_us)},
                'time_order_violations': self.backwards, 'unknown_zone_times': self.unknown_zone_times,
                'elevation': elevation, 'bbox_stored_precision': self.box, 'decimals': self.decimals,
                'computed_by': 'code'}

    def times(self, kind_start: str = 'start', kind_end: str = 'end') -> tuple[TimeCandidate | None, TimeCandidate | None]:
        """Start/end candidates: the first and last timed point in file order (kinds rewritten)."""
        def as_kind(candidate, kind):
            if candidate is None:
                return None
            return TimeCandidate(kind, candidate.raw, candidate.source, utc=candidate.utc, local=candidate.local,
                                 tz_status=candidate.tz_status, tz=candidate.tz, precision=candidate.precision,
                                 metadata={**candidate.metadata,
                                           'derived_from': ('first' if kind == kind_start else 'last')
                                           + '_timed_point_in_file_order'})
        start = as_kind(self.first_time, kind_start)
        end = as_kind(self.last_time, kind_end) if self.timed > 1 else None
        return start, end
