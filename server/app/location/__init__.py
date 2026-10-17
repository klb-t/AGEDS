"""Location history: device/provider positions, private places and derived presence.

Layers (shared contract ``docs/plans/DIGITAL_TRACES_2026-10-17.md``):

* raw device/provider samples -> ``location_fixes`` / ``location_signals`` (bulk,
  append-only, deduplicated by content identity per stream; every export that
  contained a sample keeps its own observation row);
* provider inferences (visits, activity segments, frequent places) -> events
  ``location_visit`` / ``location_segment`` / ``location_profile`` with the
  provider's own confidence as a label, never as our probability;
* tracks from GPX/KML/GeoJSON/FIT -> ``location_track`` / ``location_route`` /
  ``location_waypoint`` events whose statistics are computed by code;
* our own derivation (geofence presence from a private places file) ->
  ``location_presence_runs`` / ``location_presence_intervals`` (versioned rule,
  append-only runs), optionally published as ``place_presence`` events.

Everything is local: no reverse geocoding, no IP geolocation and no map tiles
unless the operator opts in explicitly (tiles only, recorded). A position is a
device's or account's position, never proof of a person's presence.
"""
