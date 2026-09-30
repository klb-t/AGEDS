from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..db import session
from ..evidence import add_event, ensure_source, ingest_file

PARSER_NAME = 'sms_backup'
PARSER_VERSION = '2'
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ms_to_iso(value: str | None) -> str | None:
    """Read Unix milliseconds without assuming a date for bad data."""
    if value is None or not value.strip():
        return None
    try:
        return (EPOCH + timedelta(milliseconds=int(value))).isoformat()
    except (ValueError, OverflowError):
        return None


def _timestamp(value: str | None) -> tuple[str | None, dict]:
    ts = ms_to_iso(value)
    status = 'parsed' if ts else ('missing' if value is None or not value.strip() else 'invalid')
    return ts, {
        'raw': value, 'status': status, 'format': 'unix_milliseconds',
        'timezone': 'UTC' if ts else None,
        'timezone_status': 'defined_by_unix_epoch' if ts else 'unavailable',
    }


def _stored_path(artifact_id: int) -> Path:
    # Event rows must describe the hashed snapshot, not a subsequently edited file.
    with session() as db:
        row = db.execute('SELECT stored_path FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
        if not row or not row['stored_path']:
            raise ValueError('Imported artifact has no stored snapshot')
        return Path(row['stored_path'])


def import_sms_backup(
    path: Path, *, source_locator: str | None = None, original_name: str | None = None,
) -> dict:
    locator = source_locator if source_locator is not None else str(path)
    source_id = ensure_source('android_backup', 'SMS Backup & Restore', locator)
    artifact_id = ingest_file(
        path, source_id=source_id, source_locator=locator, original_name=original_name,
        metadata={'importer': PARSER_NAME, 'parser_version': PARSER_VERSION},
    )
    count = {'calls': 0, 'sms': 0, 'mms': 0}
    issues = []
    root = ET.parse(_stored_path(artifact_id)).getroot()
    tag = root.tag.lower()
    if tag not in {'calls', 'smses'}:
        raise ValueError(f'Unsupported SMS Backup root: {root.tag}')

    for row_index, child in enumerate(root, start=1):
        event_type = child.tag.lower()
        if event_type not in ({'call'} if tag == 'calls' else {'sms', 'mms'}):
            issues.append({'row': row_index, 'issues': ['unsupported_record_type'], 'tag': child.tag})
            continue
        attrs = dict(child.attrib)
        start, timestamp = _timestamp(attrs.get('date'))
        row_issues = [] if start else [f"timestamp_{timestamp['status']}"]
        metadata = {
            'raw_attributes': attrs,
            'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION, 'row': row_index},
            'timestamp': timestamp,
        }
        kwargs = {
            'source_id': source_id, 'artifact_id': artifact_id, 'event_type': event_type,
            'ts_start': start,
            # The raw occurrence identifies the logical event; version is provenance.
            'external_id': f'{PARSER_NAME}:row:{row_index}',
            'metadata': metadata,
            # Parsing success is not a measured probability of source truth.
            'confidence': None,
        }
        if event_type == 'call':
            duration_raw = attrs.get('duration')
            duration = None
            try:
                if duration_raw is not None and duration_raw.strip():
                    duration = int(duration_raw)
                    if duration < 0:
                        duration = None
            except ValueError:
                pass
            if duration is None:
                row_issues.append('duration_missing' if not duration_raw else 'duration_invalid')
            end = None
            if start and duration is not None:
                try:
                    end = (datetime.fromisoformat(start) + timedelta(seconds=duration)).isoformat()
                except OverflowError:
                    row_issues.append('end_timestamp_out_of_range')
            metadata['duration'] = {'raw': duration_raw, 'seconds': duration, 'unit': 'seconds'}
            kwargs.update(
                ts_end=end,
                direction={'1': 'incoming', '2': 'outgoing', '3': 'missed', '5': 'rejected'}.get(attrs.get('type'), attrs.get('type')),
                contact_label=attrs.get('contact_name'), phone_or_address=attrs.get('number'),
            )
            count['calls'] += 1
        elif event_type == 'sms':
            kwargs.update(
                direction={'1': 'incoming', '2': 'outgoing'}.get(attrs.get('type'), attrs.get('type')),
                contact_label=attrs.get('contact_name'), phone_or_address=attrs.get('address'),
                body=attrs.get('body'),
            )
            count['sms'] += 1
        else:
            parts = [dict(part.attrib) for part in child.findall('./parts/part')]
            metadata['raw_parts'] = parts
            kwargs.update(
                phone_or_address=attrs.get('address'),
                body='\n'.join(part['text'] for part in parts if part.get('ct') == 'text/plain' and part.get('text')),
            )
            count['mms'] += 1
        metadata['issues'] = row_issues
        add_event(**kwargs)
        if row_issues:
            issues.append({'row': row_index, 'issues': row_issues})

    # Counts describe source occurrences, including ones already imported.
    return {'artifact_id': artifact_id, **count, 'parser_version': PARSER_VERSION, 'issues': issues}
