"""Bulk, append-only storage of location samples, provider events and import runs.

A *stream* is the device or account a sample belongs to. Its key is explicit and
conservative: ``--stream`` when the operator names one, else ``collection:<id>``
when the file came with a collection, else ``source:<source id>``; a provider's
device tag (``deviceTag``) is a sub-stream (``<stream>/device:<tag>``). Samples
are deduplicated by content identity *within* a stream only, so two exports of one
account in one collection share each sample while every export keeps its own
observation row (locator). Different streams are never merged by this module.

Content identity of a fix = SHA-256 of its stored representation (UTC instant or
the wall-clock text when the zone is unknown, latitude/longitude at the stored
precision, accuracy, altitude, speed, heading, kind, basis, provider source and
device tag). Raw extras of later observations (e.g. activity guesses attached to
a Records.json point) are not stored again; the first observing import keeps them
and every export stays in the preserved original. Rounding to a coarser stored
precision happens before hashing, so the hash never encodes the exact position.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

from ..db import session
from ..timeinfo import TimeCandidate
from . import geo

BATCH = 2000
_SQL_PARAMS = 900
FIX_COLUMNS = ('stream_key', 'fix_sha256', 'decimals', 'source_decimals', 't_us', 'time_raw', 'time_utc', 'time_local',
               'time_zone', 'tz_status', 'time_precision', 'lat', 'lon', 'accuracy_m', 'altitude_m',
               'vertical_accuracy_m', 'speed_mps', 'heading_deg', 'fix_kind', 'signal_basis', 'provider_source',
               'device_tag', 'extra_json', 'first_import_id')
SIGNAL_COLUMNS = ('stream_key', 'signal_sha256', 'signal_kind', 'signal_basis', 't_us', 'time_raw', 'time_utc',
                  'tz_status', 'summary_json', 'first_import_id')


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def utc_us(candidate: TimeCandidate | None) -> int | None:
    """UTC microseconds of a candidate the source fixed (explicit offset or epoch), else None."""
    if candidate is None or candidate.utc is None or candidate.tz_status not in ('explicit_offset', 'utc_epoch'):
        return None
    moment = datetime.fromisoformat(candidate.utc.replace('Z', '+00:00'))
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def us_to_iso(us: int | None) -> str | None:
    if us is None:
        return None
    moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(us))
    text = moment.strftime('%Y-%m-%dT%H:%M:%S')
    return text + (f'.{moment.microsecond:06d}' if moment.microsecond else '') + 'Z'


def stream_key(*, explicit: str | None, collection_id: int | None, source_id: int, device_tag: str | None = None) -> str:
    base = explicit.strip() if explicit and explicit.strip() else (
        f'collection:{collection_id}' if collection_id is not None else f'source:{source_id}')
    if device_tag:
        return f'{base}/device:{str(device_tag)[:128]}'
    return base


def _num(value) -> float | None:
    number = geo.finite(value)
    return number


def fix_row(fix: dict, *, stream: str, store_decimals: int | None, import_id: int) -> tuple:
    """Row tuple for ``location_fixes`` (see FIX_COLUMNS)."""
    coord: geo.Coordinate = fix['coord']
    lat, lon, decimals = geo.round_to(coord, store_decimals)
    candidate: TimeCandidate | None = fix.get('time')
    t_us = utc_us(candidate)
    values = {
        'accuracy_m': _num(fix.get('accuracy_m')), 'altitude_m': _num(fix.get('altitude_m')),
        'vertical_accuracy_m': _num(fix.get('vertical_accuracy_m')), 'speed_mps': _num(fix.get('speed_mps')),
        'heading_deg': _num(fix.get('heading_deg')),
    }
    time_identity = t_us if t_us is not None else (
        ['local', candidate.local, candidate.tz_status] if candidate is not None and candidate.local else None)
    identity = canonical([time_identity, f'{lat:.{decimals}f}', f'{lon:.{decimals}f}', decimals,
                          values['accuracy_m'], values['altitude_m'], values['vertical_accuracy_m'],
                          values['speed_mps'], values['heading_deg'], fix['fix_kind'], fix['signal_basis'],
                          fix.get('provider_source'), fix.get('device_tag')])
    extra = fix.get('extra') or {}
    return (stream, sha256_text(identity), decimals, coord.decimals, t_us,
            candidate.raw if candidate else fix.get('time_raw'), candidate.utc if candidate else None,
            candidate.local if candidate else None, candidate.tz if candidate else None,
            candidate.tz_status if candidate else 'unknown', candidate.precision if candidate else 'unknown',
            lat, lon, values['accuracy_m'], values['altitude_m'], values['vertical_accuracy_m'], values['speed_mps'],
            values['heading_deg'], fix['fix_kind'], fix['signal_basis'], fix.get('provider_source'), fix.get('device_tag'),
            canonical(extra), import_id)


def signal_row(signal: dict, *, stream: str, import_id: int) -> tuple:
    candidate: TimeCandidate | None = signal.get('time')
    t_us = utc_us(candidate)
    summary = signal.get('summary') or {}
    identity = canonical([t_us if t_us is not None else (candidate.raw if candidate else None),
                          signal['signal_kind'], signal['signal_basis'], summary])
    return (stream, sha256_text(identity), signal['signal_kind'], signal['signal_basis'], t_us,
            candidate.raw if candidate else None, candidate.utc if candidate else None,
            candidate.tz_status if candidate else 'unknown', canonical(summary), import_id)


class ConflictError(RuntimeError):
    """A locator of this import already points at a different sample (changed interpretation)."""


def ensure_import(db: sqlite3.Connection, *, artifact_id: int, importer: str, parser_version: str, fmt: str,
                  stream: str, params: dict) -> tuple[int, bool]:
    params_json = canonical(params)
    digest = sha256_text(params_json)
    row = db.execute('''SELECT id FROM location_imports WHERE artifact_id=? AND importer=? AND parser_version=?
                        AND params_sha256=?''', (artifact_id, importer, parser_version, digest)).fetchone()
    if row:
        return int(row[0]), True
    cur = db.execute('''INSERT INTO location_imports(artifact_id,importer,parser_version,format,stream_key,params_json,
                        params_sha256) VALUES(?,?,?,?,?,?,?)''',
                     (artifact_id, importer, parser_version, fmt, stream, params_json, digest))
    return int(cur.lastrowid), False


class SampleWriter:
    """Buffered writer: fixes and signals with their observations, committed per batch."""

    def __init__(self, import_id: int, *, store_decimals: int | None, batch: int = BATCH):
        self.import_id = import_id
        self.store_decimals = store_decimals
        self.batch = batch
        self.fixes: list[tuple[str, tuple]] = []
        self.signals: list[tuple[str, tuple]] = []
        self.counts = {'fixes_observed': 0, 'fixes_new': 0, 'fix_observations_new': 0,
                       'signals_observed': 0, 'signals_new': 0, 'signal_observations_new': 0}

    def add_fix(self, fix: dict, *, stream: str, locator: str) -> None:
        self.fixes.append((locator, fix_row(fix, stream=stream, store_decimals=self.store_decimals,
                                            import_id=self.import_id)))
        self.counts['fixes_observed'] += 1
        if len(self.fixes) >= self.batch:
            self.flush()

    def add_signal(self, signal: dict, *, stream: str, locator: str) -> None:
        self.signals.append((locator, signal_row(signal, stream=stream, import_id=self.import_id)))
        self.counts['signals_observed'] += 1
        if len(self.signals) >= self.batch:
            self.flush()

    def flush(self) -> None:
        if not self.fixes and not self.signals:
            return
        with session() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.fixes:
                self._write(db, 'location_fixes', 'location_fix_observations', 'fix_id', 'fix_sha256', FIX_COLUMNS,
                            self.fixes, 'fixes', with_decimals=True)
            if self.signals:
                self._write(db, 'location_signals', 'location_signal_observations', 'signal_id', 'signal_sha256',
                            SIGNAL_COLUMNS, self.signals, 'signals', with_decimals=False)
        self.fixes, self.signals = [], []

    def _write(self, db, table, obs_table, ref, sha_column, columns, pending, label, *, with_decimals: bool):
        before = db.total_changes
        db.executemany(f'INSERT OR IGNORE INTO {table}({",".join(columns)}) VALUES({",".join("?" * len(columns))})',
                       [row for _, row in pending])
        self.counts[f'{label}_new'] += db.total_changes - before
        key_index = columns.index(sha_column)
        ids: dict[tuple, int] = {}
        groups: dict[tuple, set] = {}
        for _, row in pending:
            group = (row[0], row[2]) if with_decimals else (row[0],)
            groups.setdefault(group, set()).add(row[key_index])
        for group, shas in groups.items():
            shas = sorted(shas)
            for offset in range(0, len(shas), _SQL_PARAMS):
                chunk = shas[offset:offset + _SQL_PARAMS]
                where = 'stream_key=? AND decimals=?' if with_decimals else 'stream_key=?'
                for rid, sha in db.execute(f'SELECT id, {sha_column} FROM {table} WHERE {where} AND {sha_column} IN '
                                           f'({",".join("?" * len(chunk))})', (*group, *chunk)):
                    ids[(*group, sha)] = int(rid)
        observations = []
        for locator, row in pending:
            group = (row[0], row[2]) if with_decimals else (row[0],)
            observations.append((self.import_id, locator, ids[(*group, row[key_index])]))
        before = db.total_changes
        db.executemany(f'INSERT OR IGNORE INTO {obs_table}(import_id,locator,{ref}) VALUES(?,?,?)', observations)
        inserted = db.total_changes - before
        self.counts[{'fixes': 'fix', 'signals': 'signal'}[label] + '_observations_new'] += inserted
        if inserted != len(observations):
            # Re-import (or a duplicate locator): the existing observation must name the same sample.
            wanted = {locator: rid for _, locator, rid in observations}
            locators = sorted(wanted)
            for offset in range(0, len(locators), _SQL_PARAMS):
                chunk = locators[offset:offset + _SQL_PARAMS]
                for locator, rid in db.execute(f'SELECT locator, {ref} FROM {obs_table} WHERE import_id=? AND locator IN '
                                               f'({",".join("?" * len(chunk))})', (self.import_id, *chunk)):
                    if int(rid) != wanted[locator]:
                        raise ConflictError(f'{obs_table}: locator {locator} of import {self.import_id} already names '
                                            f'another sample; use a new parser version')


def record_run(import_id: int, *, counts: dict, coverage: dict, issues: dict) -> int:
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        cur = db.execute('INSERT INTO location_import_runs(import_id,counts_json,coverage_json,issues_json) VALUES(?,?,?,?)',
                         (import_id, canonical(counts), canonical(coverage), canonical(issues)))
        return int(cur.lastrowid)


def iter_import_fixes(db: sqlite3.Connection, import_id: int, *, bases: tuple[str, ...] = ('raw_sample',)):
    """Fixes observed by one import with a UTC instant, ordered by stream and time (streaming cursor)."""
    marks = ','.join('?' * len(bases))
    yield from db.execute(f'''SELECT DISTINCT f.id, f.stream_key, f.t_us, f.lat, f.lon, f.decimals, f.accuracy_m,
                                     f.fix_kind, f.provider_source
                              FROM location_fix_observations o JOIN location_fixes f ON f.id=o.fix_id
                              WHERE o.import_id=? AND f.t_us IS NOT NULL AND f.signal_basis IN ({marks})
                              ORDER BY f.stream_key, f.t_us, f.id''', (import_id, *bases))


def sample_sessions(rows, *, gap_s: float, max_sessions: int):
    """Contiguous sampling sessions per stream: consecutive fixes at most ``gap_s`` apart.

    Returns ``(sessions, truncated)``; each session has first/last time, counts,
    a bounding box over stored coordinates and accuracy statistics.
    """
    sessions, current, truncated = [], None, False
    gap_us = int(gap_s * 1_000_000)
    for row in rows:
        fid, stream, t_us, lat, lon, decimals, accuracy, kind, source = tuple(row)
        if current is not None and (current['stream'] != stream or t_us - current['last_us'] > gap_us):
            sessions.append(current)
            current = None
            if len(sessions) >= max_sessions:
                truncated = True
                break
        if current is None:
            current = {'stream': stream, 'first_us': t_us, 'last_us': t_us, 'first_fix_id': fid, 'last_fix_id': fid,
                       'fixes': 0, 'points': [], 'decimals': decimals, 'accuracy': [], 'kinds': {}, 'sources': {},
                       'max_gap_us': 0}
        else:
            current['max_gap_us'] = max(current['max_gap_us'], t_us - current['last_us'])
        current['last_us'], current['last_fix_id'] = t_us, fid
        current['fixes'] += 1
        current['decimals'] = min(current['decimals'], decimals)
        _bbox_update(current, lat, lon)
        if accuracy is not None and math.isfinite(accuracy):
            current['accuracy'].append(accuracy)
        current['kinds'][kind] = current['kinds'].get(kind, 0) + 1
        if source:
            current['sources'][source] = current['sources'].get(source, 0) + 1
    if current is not None and not truncated:
        sessions.append(current)
    for s in sessions:
        acc = sorted(s.pop('accuracy'))
        s['accuracy_m'] = ({'min': acc[0], 'median': acc[len(acc) // 2], 'max': acc[-1], 'stated': len(acc)}
                           if acc else {'stated': 0})
        box = s.pop('points')
        s['bbox'] = box
    return sessions, truncated


def _bbox_update(session_: dict, lat: float, lon: float) -> None:
    box = session_['points']
    if not box:
        session_['points'] = {'south': lat, 'north': lat, 'west': lon, 'east': lon}
        return
    box['south'], box['north'] = min(box['south'], lat), max(box['north'], lat)
    box['west'], box['east'] = min(box['west'], lon), max(box['east'], lon)
