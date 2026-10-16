"""Bulk tagging, rename and merge over the existing ``tags`` tables.

Tags are human annotations. Every bulk operation is all-or-nothing: unknown
targets fail the whole request and nothing is written. Each operation writes
one append-only ``audit_log`` row that lists exactly which ids changed and
which already were in the requested state, so the operation can be reviewed
and reversed by hand. Similar tag names are surfaced as suggestions only;
merging is always an explicit operation.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata

from ..db import session
from .common import (WorkbenchConflict, WorkbenchInputError, WorkbenchNotFound, audit, clean_name,
                     positive_id)

MAX_BULK_TARGETS = 10_000
MAX_MERGE_MEMBERS = 200_000
MAX_TAG_CHARS = 200
_CHUNK = 900
TARGETS = {
    'artifact': ('artifacts', 'artifact_tags', 'artifact_id'),
    'event': ('events', 'event_tags', 'event_id'),
}
_TARGET_TEXT = re.compile(r'^(artifact|event|a|e):([1-9][0-9]{0,18})$')


def _chunks(values: list[int]):
    for start in range(0, len(values), _CHUNK):
        yield values[start:start + _CHUNK]


def parse_targets(raw) -> dict[str, list[int]]:
    """Accept [{'kind','id'}] objects or 'artifact:12' / 'e:7' strings; dedupe; keep first-seen order."""
    if not isinstance(raw, (list, tuple)):
        raise WorkbenchInputError('targets must be a list')
    result: dict[str, list[int]] = {'artifact': [], 'event': []}
    seen: set[tuple[str, int]] = set()
    for item in raw:
        if isinstance(item, str):
            match = _TARGET_TEXT.match(item.strip())
            if not match:
                raise WorkbenchInputError(f'invalid target {item[:80]!r}; use artifact:ID or event:ID')
            kind = {'a': 'artifact', 'e': 'event'}.get(match.group(1), match.group(1))
            ident = int(match.group(2))
        elif isinstance(item, dict):
            kind, ident = item.get('kind'), item.get('id')
            if kind not in TARGETS:
                raise WorkbenchInputError('target kind must be artifact or event')
        else:
            raise WorkbenchInputError('each target must be an object or a kind:id string')
        positive_id(ident, f'{kind} id')
        if (kind, ident) not in seen:
            seen.add((kind, ident))
            result[kind].append(ident)
    if not seen:
        raise WorkbenchInputError('at least one target is required')
    if len(seen) > MAX_BULK_TARGETS:
        raise WorkbenchInputError(f'at most {MAX_BULK_TARGETS} targets per operation')
    return result


def similarity_key(name: str) -> str:
    """Case-, width- and diacritic-insensitive key used only to suggest merges."""
    folded = unicodedata.normalize('NFKD', unicodedata.normalize('NFKC', name))
    folded = ''.join(ch for ch in folded if not unicodedata.combining(ch))
    return folded.replace('ł', 'l').replace('Ł', 'L').casefold()


def _tag(db: sqlite3.Connection, tag_id: int) -> sqlite3.Row:
    row = db.execute('SELECT id, name FROM tags WHERE id=?', (tag_id,)).fetchone()
    if row is None:
        raise WorkbenchNotFound(f'tag {tag_id} not found')
    return row


def _missing(db: sqlite3.Connection, table: str, ids: list[int]) -> list[int]:
    found: set[int] = set()
    for chunk in _chunks(ids):
        found.update(r[0] for r in db.execute(f'SELECT id FROM {table} WHERE id IN ({",".join("?" * len(chunk))})', chunk))
    return [i for i in ids if i not in found]


def _members(db: sqlite3.Connection, kind: str, tag_id: int, ids: list[int] | None = None) -> set[int]:
    _, link, column = TARGETS[kind]
    if ids is None:
        return {r[0] for r in db.execute(f'SELECT {column} FROM {link} WHERE tag_id=?', (tag_id,))}
    present: set[int] = set()
    for chunk in _chunks(ids):
        present.update(r[0] for r in db.execute(
            f'SELECT {column} FROM {link} WHERE tag_id=? AND {column} IN ({",".join("?" * len(chunk))})', (tag_id, *chunk)))
    return present


def list_tags() -> dict:
    with session() as db:
        rows = db.execute('''SELECT t.id, t.name,
              (SELECT count(*) FROM artifact_tags a WHERE a.tag_id=t.id) AS artifact_count,
              (SELECT count(*) FROM event_tags e WHERE e.tag_id=t.id) AS event_count
            FROM tags t ORDER BY t.name, t.id''').fetchall()
    tags = [dict(row) | {'epistemic_layer': 'human_annotation'} for row in rows]
    groups: dict[str, list[int]] = {}
    for tag in tags:
        groups.setdefault(similarity_key(tag['name']), []).append(tag['id'])
    similar = [ids for ids in groups.values() if len(ids) > 1]
    for tag in tags:
        tag['similar_tag_ids'] = next((sorted(set(ids) - {tag['id']}) for ids in similar if tag['id'] in ids), [])
    return {'tags': tags, 'possible_duplicates': similar,
            'possible_duplicates_rule': 'NFKC + NFKD without combining marks + ł→l + casefold; suggestion only, never merged automatically'}


def tags_of(kind: str, object_id: int) -> list[dict]:
    _, link, column = TARGETS[kind]
    with session() as db:
        return [dict(r) for r in db.execute(
            f'SELECT t.id, t.name FROM {link} l JOIN tags t ON t.id=l.tag_id WHERE l.{column}=? ORDER BY t.name', (object_id,))]


def apply_tag(tag_name, targets, *, remove: bool = False, channel: str = 'http') -> dict:
    """Add (or remove) one tag on many artifacts/events at once, all-or-nothing, with one audit row."""
    name = clean_name(tag_name, 'tag', max_chars=MAX_TAG_CHARS)
    wanted = parse_targets(targets)
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        missing = {kind: _missing(db, TARGETS[kind][0], ids) for kind, ids in wanted.items() if ids}
        missing = {kind: ids for kind, ids in missing.items() if ids}
        if missing:
            shown = {kind: ids[:50] for kind, ids in missing.items()}
            raise WorkbenchNotFound(f'targets not found (nothing was changed): {shown}')
        row = db.execute('SELECT id, name FROM tags WHERE name=?', (name,)).fetchone()
        created = False
        if row is None:
            if remove:
                raise WorkbenchNotFound('tag not found; nothing to remove')
            tag_id = int(db.execute('INSERT INTO tags(name) VALUES (?)', (name,)).lastrowid)
            created = True
        else:
            tag_id = int(row['id'])
        summary = {}
        for kind, ids in wanted.items():
            _, link, column = TARGETS[kind]
            present = _members(db, kind, tag_id, ids) if ids else set()
            if remove:
                changed = [i for i in ids if i in present]
                for chunk in _chunks(changed):
                    db.execute(f'DELETE FROM {link} WHERE tag_id=? AND {column} IN ({",".join("?" * len(chunk))})', (tag_id, *chunk))
                unchanged = [i for i in ids if i not in present]
            else:
                changed = [i for i in ids if i not in present]
                db.executemany(f'INSERT INTO {link}({column}, tag_id) VALUES (?, ?)', [(i, tag_id) for i in changed])
                unchanged = [i for i in ids if i in present]
            summary[kind] = {'changed': changed, 'unchanged': unchanged}
        action = 'tag_bulk_remove' if remove else 'tag_bulk_add'
        details = {'tag': name, 'tag_id': tag_id, 'tag_created': created, 'channel': channel, 'targets': summary,
                   'epistemic_layer': 'human_annotation'}
        audit_id = audit(db, action, 'tag', tag_id, details)
        return {'action': action, 'audit_id': audit_id, **details,
                'changed_count': sum(len(v['changed']) for v in summary.values()),
                'unchanged_count': sum(len(v['unchanged']) for v in summary.values())}


def rename_tag(tag_id: int, new_name, *, channel: str = 'http') -> dict:
    positive_id(tag_id, 'tag id')
    name = clean_name(new_name, 'tag', max_chars=MAX_TAG_CHARS)
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        old = _tag(db, tag_id)
        if old['name'] == name:
            raise WorkbenchInputError('new name equals the current name')
        clash = db.execute('SELECT id FROM tags WHERE name=?', (name,)).fetchone()
        if clash is not None:
            raise WorkbenchConflict(f'tag name already used by tag {clash["id"]}; merge the tags explicitly instead')
        db.execute('UPDATE tags SET name=? WHERE id=?', (name, tag_id))
        audit_id = audit(db, 'tag_rename', 'tag', tag_id, {'old_name': old['name'], 'new_name': name, 'channel': channel})
        return {'action': 'tag_rename', 'audit_id': audit_id, 'tag_id': tag_id, 'old_name': old['name'], 'new_name': name}


def merge_tags(source_ids, target_id: int, *, channel: str = 'http') -> dict:
    """Move all memberships of the source tags onto the target tag and delete the source tags."""
    positive_id(target_id, 'target tag id')
    if not isinstance(source_ids, (list, tuple)) or not source_ids:
        raise WorkbenchInputError('source tag ids must be a non-empty list')
    sources = []
    for value in source_ids:
        positive_id(value, 'source tag id')
        if value not in sources:
            sources.append(value)
    if target_id in sources:
        raise WorkbenchInputError('target tag cannot also be a source')
    if len(sources) > 1000:
        raise WorkbenchInputError('at most 1000 source tags per merge')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        target = _tag(db, target_id)
        merged, total = [], 0
        target_before = {kind: _members(db, kind, target_id) for kind in TARGETS}
        added: dict[str, set[int]] = {kind: set() for kind in TARGETS}
        for source_id in sources:
            source = _tag(db, source_id)
            members = {kind: sorted(_members(db, kind, source_id)) for kind in TARGETS}
            total += sum(len(v) for v in members.values())
            if total > MAX_MERGE_MEMBERS:
                raise WorkbenchInputError(f'merge touches more than {MAX_MERGE_MEMBERS} memberships; split it')
            for kind, ids in members.items():
                _, link, column = TARGETS[kind]
                new = [i for i in ids if i not in target_before[kind] and i not in added[kind]]
                db.executemany(f'INSERT INTO {link}({column}, tag_id) VALUES (?, ?)', [(i, target_id) for i in new])
                added[kind].update(new)
                db.execute(f'DELETE FROM {link} WHERE tag_id=?', (source_id,))
            db.execute('DELETE FROM tags WHERE id=?', (source_id,))
            merged.append({'id': source_id, 'name': source['name'], 'artifacts': members['artifact'], 'events': members['event']})
        details = {'target': {'id': target_id, 'name': target['name']}, 'sources_deleted': merged,
                   'added_to_target': {kind: sorted(ids) for kind, ids in added.items()}, 'channel': channel}
        audit_id = audit(db, 'tag_merge', 'tag', target_id, details)
        return {'action': 'tag_merge', 'audit_id': audit_id, **details}
