"""Working folders ("teczki"): named, optionally nested selections of references.

An item references evidence by kind + id and, where the kind points into a
text version, the pinned ``derived_text`` id. At add time the item records the
identity facts it was chosen for (hashes, version, range); on read the code
recomputes them and reports ``unchanged``, ``changed`` (with the differing
keys) or ``missing``. Membership, order and notes are human working data: they
never alter, copy or re-rank evidence. Exports contain references only.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3

from ..db import session
from .common import (WorkbenchConflict, WorkbenchInputError, WorkbenchNotFound, audit, clean_name,
                     clean_text, loads_object, positive_id, utc_now)

ITEM_KINDS = ('artifact', 'event', 'citation', 'text_version', 'annotation')
MAX_FOLDERS = 10_000
MAX_FOLDER_DEPTH = 32
MAX_ITEMS_PER_FOLDER = 10_000
MAX_EXPORT_ITEMS = 100_000
EXPORT_SCHEMA = 'ageds.workbench-folder-references/v1'
CSV_COLUMNS = ('folder_id', 'folder_path', 'position', 'item_kind', 'item_id', 'pinned_version_id', 'status',
               'changed_keys', 'locator', 'note', 'pinned_json')
_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def _sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode('utf-8')).hexdigest()


def _identity(db: sqlite3.Connection, kind: str, item_id: int) -> tuple[int | None, dict] | None:
    """Identity facts recomputed from evidence tables; None when the target is gone."""
    if kind == 'artifact':
        row = db.execute('SELECT sha256, size_bytes, original_name FROM artifacts WHERE id=?', (item_id,)).fetchone()
        return None if row is None else (None, {'sha256': row['sha256'], 'size_bytes': row['size_bytes'], 'original_name': row['original_name']})
    if kind == 'event':
        row = db.execute('SELECT artifact_id, source_id, event_type, ts_start, external_id, body, subject FROM events WHERE id=?',
                         (item_id,)).fetchone()
        return None if row is None else (None, {'artifact_id': row['artifact_id'], 'source_id': row['source_id'],
                                                'event_type': row['event_type'], 'ts_start': row['ts_start'],
                                                'external_id': row['external_id'], 'body_sha256': _sha(row['body']),
                                                'subject_sha256': _sha(row['subject'])})
    if kind == 'citation':
        row = db.execute('SELECT artifact_id, derived_text_id, start_ms, end_ms, quote_sha256 FROM evidence_anchors WHERE id=?',
                         (item_id,)).fetchone()
        return None if row is None else (row['derived_text_id'], {'artifact_id': row['artifact_id'], 'derived_text_id': row['derived_text_id'],
                                                                  'start_ms': row['start_ms'], 'end_ms': row['end_ms'],
                                                                  'quote_sha256': row['quote_sha256']})
    if kind == 'text_version':
        row = db.execute('SELECT artifact_id, kind, run_id, model, language, text FROM derived_text WHERE id=?', (item_id,)).fetchone()
        return None if row is None else (item_id, {'artifact_id': row['artifact_id'], 'kind': row['kind'], 'run_id': row['run_id'],
                                                   'model': row['model'], 'language': row['language'], 'text_sha256': _sha(row['text'])})
    if kind == 'annotation':
        row = db.execute('SELECT artifact_id, event_id, derived_text_id, kind, label, body, start_ms, end_ms, updated_at FROM annotations WHERE id=?',
                         (item_id,)).fetchone()
        return None if row is None else (row['derived_text_id'], {'artifact_id': row['artifact_id'], 'event_id': row['event_id'],
                                                                   'derived_text_id': row['derived_text_id'], 'kind': row['kind'],
                                                                   'label_sha256': _sha(row['label']), 'body_sha256': _sha(row['body']),
                                                                   'start_ms': row['start_ms'], 'end_ms': row['end_ms'],
                                                                   'updated_at': row['updated_at']})
    raise WorkbenchInputError(f'item kind must be one of {", ".join(ITEM_KINDS)}')


def locator(kind: str, item_id: int, pinned_version_id: int | None) -> str:
    """Inert textual locator; it does not dereference anything by itself."""
    text = f'ageds:{kind}/{item_id}'
    return text + (f'@derived_text/{pinned_version_id}' if pinned_version_id is not None and kind != 'text_version' else '')


def _artifact_link(kind: str, pinned: dict, item_id: int) -> str | None:
    artifact_id = item_id if kind == 'artifact' else pinned.get('artifact_id')
    return f'/artifact/{artifact_id}' if isinstance(artifact_id, int) else None


def _item_payload(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    pinned = loads_object(row['pinned_json'])
    current = _identity(db, row['item_kind'], row['item_id'])
    if current is None:
        status, changed = 'missing', []
    else:
        changed = sorted(k for k in set(pinned) | set(current[1]) if pinned.get(k) != current[1].get(k))
        status = 'changed' if changed else 'unchanged'
    return {'id': row['id'], 'folder_id': row['folder_id'], 'item_kind': row['item_kind'], 'item_id': row['item_id'],
            'pinned_version_id': row['pinned_version_id'], 'pinned': pinned, 'status': status, 'changed_keys': changed,
            'position': row['position'], 'note': row['note'], 'added_at': row['added_at'], 'updated_at': row['updated_at'],
            'locator': locator(row['item_kind'], row['item_id'], row['pinned_version_id']),
            'url': _artifact_link(row['item_kind'], pinned, row['item_id'])}


def _folder_row(db: sqlite3.Connection, folder_id: int) -> sqlite3.Row:
    row = db.execute('SELECT * FROM workbench_folders WHERE id=?', (folder_id,)).fetchone()
    if row is None:
        raise WorkbenchNotFound(f'folder {folder_id} not found')
    return row


def _ancestors(db: sqlite3.Connection, folder_id: int | None) -> list[sqlite3.Row]:
    chain, seen = [], set()
    while folder_id is not None:
        if folder_id in seen or len(chain) > MAX_FOLDER_DEPTH + 1:
            raise WorkbenchConflict('stored folder hierarchy contains a cycle or exceeds the depth limit')
        seen.add(folder_id)
        row = _folder_row(db, folder_id)
        chain.append(row)
        folder_id = row['parent_id']
    return list(reversed(chain))


def _path(db: sqlite3.Connection, folder_id: int) -> list[dict]:
    return [{'id': r['id'], 'name': r['name']} for r in _ancestors(db, folder_id)]


def _folder_payload(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return {'id': row['id'], 'parent_id': row['parent_id'], 'name': row['name'], 'note': row['note'],
            'position': row['position'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
            'path': _path(db, row['id']),
            'item_count': db.execute('SELECT count(*) FROM workbench_folder_items WHERE folder_id=?', (row['id'],)).fetchone()[0],
            'child_count': db.execute('SELECT count(*) FROM workbench_folders WHERE parent_id=?', (row['id'],)).fetchone()[0],
            'epistemic_layer': 'human_annotation'}


def _check_parent(db: sqlite3.Connection, parent_id: int | None, moving_id: int | None = None) -> None:
    if parent_id is None:
        return
    positive_id(parent_id, 'parent folder id')
    chain = _ancestors(db, parent_id)
    if moving_id is not None and any(r['id'] == moving_id for r in chain):
        raise WorkbenchConflict('a folder cannot be moved into itself or its descendant')
    depth = len(chain) + 1
    if moving_id is not None:
        depth += _subtree_height(db, moving_id) - 1
    if depth > MAX_FOLDER_DEPTH:
        raise WorkbenchInputError(f'folder nesting exceeds {MAX_FOLDER_DEPTH} levels')


def _subtree_height(db: sqlite3.Connection, folder_id: int, level: int = 1) -> int:
    if level > MAX_FOLDER_DEPTH + 1:
        raise WorkbenchConflict('stored folder hierarchy exceeds the depth limit')
    children = [r[0] for r in db.execute('SELECT id FROM workbench_folders WHERE parent_id=?', (folder_id,))]
    return 1 + max((_subtree_height(db, child, level + 1) for child in children), default=0)


def _sibling_clash(db: sqlite3.Connection, parent_id: int | None, name: str, exclude: int | None = None) -> bool:
    row = db.execute('SELECT id FROM workbench_folders WHERE COALESCE(parent_id,0)=COALESCE(?,0) AND name=? AND id IS NOT ?',
                     (parent_id, name, exclude)).fetchone()
    return row is not None


def create_folder(name, parent_id: int | None = None, note=None, position: int | None = None) -> dict:
    name = clean_name(name, 'folder name')
    note = clean_text(note, 'note')
    if position is not None and (type(position) is not int or not 0 <= position <= MAX_FOLDERS):
        raise WorkbenchInputError('position must be a non-negative integer')
    now = utc_now()
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT count(*) FROM workbench_folders').fetchone()[0] >= MAX_FOLDERS:
            raise WorkbenchConflict(f'folder limit reached ({MAX_FOLDERS})')
        _check_parent(db, parent_id)
        if _sibling_clash(db, parent_id, name):
            raise WorkbenchConflict('a folder with this name already exists at this level')
        if position is None:
            position = db.execute('SELECT COALESCE(max(position)+1,0) FROM workbench_folders WHERE COALESCE(parent_id,0)=COALESCE(?,0)',
                                  (parent_id,)).fetchone()[0]
        folder_id = int(db.execute('INSERT INTO workbench_folders(parent_id,name,note,position,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                                   (parent_id, name, note, position, now, now)).lastrowid)
        audit(db, 'workbench_folder_create', 'workbench_folder', folder_id, {'name': name, 'parent_id': parent_id, 'note': note})
        return _folder_payload(db, _folder_row(db, folder_id))


_UNSET = object()


def update_folder(folder_id: int, *, name=_UNSET, note=_UNSET, parent_id=_UNSET, position=_UNSET) -> dict:
    positive_id(folder_id, 'folder id')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _folder_row(db, folder_id)
        new_name = row['name'] if name is _UNSET else clean_name(name, 'folder name')
        new_note = row['note'] if note is _UNSET else clean_text(note, 'note')
        new_parent = row['parent_id'] if parent_id is _UNSET else parent_id
        new_position = row['position'] if position is _UNSET else position
        if type(new_position) is not int or not 0 <= new_position <= MAX_FOLDERS:
            raise WorkbenchInputError('position must be a non-negative integer')
        if new_parent != row['parent_id']:
            _check_parent(db, new_parent, moving_id=folder_id)
        if (new_name, new_parent) != (row['name'], row['parent_id']) and _sibling_clash(db, new_parent, new_name, folder_id):
            raise WorkbenchConflict('a folder with this name already exists at this level')
        db.execute('UPDATE workbench_folders SET name=?, note=?, parent_id=?, position=?, updated_at=? WHERE id=?',
                   (new_name, new_note, new_parent, new_position, utc_now(), folder_id))
        audit(db, 'workbench_folder_update', 'workbench_folder', folder_id,
              {'before': {'name': row['name'], 'note': row['note'], 'parent_id': row['parent_id'], 'position': row['position']},
               'after': {'name': new_name, 'note': new_note, 'parent_id': new_parent, 'position': new_position}})
        return _folder_payload(db, _folder_row(db, folder_id))


def _descendants(db: sqlite3.Connection, folder_id: int) -> list[int]:
    order, stack = [], [folder_id]
    while stack:
        current = stack.pop()
        order.append(current)
        if len(order) > MAX_FOLDERS:
            raise WorkbenchConflict('folder subtree exceeds the folder limit')
        stack.extend(r[0] for r in db.execute('SELECT id FROM workbench_folders WHERE parent_id=? ORDER BY id DESC', (current,)))
    return order


def delete_folder(folder_id: int, *, recursive: bool = False) -> dict:
    """Delete a folder (and with ``recursive`` its subfolders); evidence is untouched, references are audited."""
    positive_id(folder_id, 'folder id')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        _folder_row(db, folder_id)
        ids = _descendants(db, folder_id)
        items = db.execute(f'SELECT * FROM workbench_folder_items WHERE folder_id IN ({",".join("?" * len(ids))}) ORDER BY folder_id, position, id',
                           ids).fetchall()
        if not recursive and (len(ids) > 1 or items):
            raise WorkbenchConflict('folder is not empty; pass recursive=true to delete it with its subfolders and references')
        folders = [dict(db.execute('SELECT * FROM workbench_folders WHERE id=?', (i,)).fetchone()) for i in ids]
        removed = [{'folder_id': r['folder_id'], 'item_kind': r['item_kind'], 'item_id': r['item_id'],
                    'pinned_version_id': r['pinned_version_id'], 'pinned_json': r['pinned_json'], 'note': r['note'],
                    'position': r['position']} for r in items]
        marks = ','.join('?' * len(ids))
        db.execute(f'DELETE FROM workbench_folder_items WHERE folder_id IN ({marks})', ids)
        for ident in reversed(ids):
            db.execute('DELETE FROM workbench_folders WHERE id=?', (ident,))
        audit_id = audit(db, 'workbench_folder_delete', 'workbench_folder', folder_id,
                         {'folders': folders, 'references_removed': removed, 'evidence_changed': False})
        return {'deleted_folders': ids, 'references_removed': len(removed), 'audit_id': audit_id, 'evidence_changed': False}


def list_folders() -> list[dict]:
    """Depth-first ordered tree as a flat list with ``depth``."""
    with session() as db:
        rows = db.execute('''SELECT f.*, (SELECT count(*) FROM workbench_folder_items i WHERE i.folder_id=f.id) AS item_count
                             FROM workbench_folders f ORDER BY position, name, id''').fetchall()
    children: dict[int | None, list[sqlite3.Row]] = {}
    known = {r['id'] for r in rows}
    for row in rows:
        parent = row['parent_id'] if row['parent_id'] in known else None
        children.setdefault(parent, []).append(row)
    result: list[dict] = []
    visited: set[int] = set()

    def walk(parent, depth, path):
        for row in children.get(parent, []):
            if row['id'] in visited or depth > MAX_FOLDER_DEPTH:
                continue
            visited.add(row['id'])
            here = path + [row['name']]
            result.append({'id': row['id'], 'parent_id': row['parent_id'], 'name': row['name'], 'note': row['note'],
                           'position': row['position'], 'depth': depth, 'path': here, 'item_count': row['item_count'],
                           'created_at': row['created_at'], 'updated_at': row['updated_at']})
            walk(row['id'], depth + 1, here)
    walk(None, 0, [])
    return result


def get_folder(folder_id: int) -> dict:
    positive_id(folder_id, 'folder id')
    with session() as db:
        folder = _folder_payload(db, _folder_row(db, folder_id))
        folder['children'] = [{'id': r['id'], 'name': r['name'], 'position': r['position']} for r in db.execute(
            'SELECT id, name, position FROM workbench_folders WHERE parent_id=? ORDER BY position, name, id', (folder_id,))]
        folder['items'] = [_item_payload(db, r) for r in db.execute(
            'SELECT * FROM workbench_folder_items WHERE folder_id=? ORDER BY position, id', (folder_id,))]
        folder['status_counts'] = {s: sum(1 for i in folder['items'] if i['status'] == s) for s in ('unchanged', 'changed', 'missing')}
        return folder


def _item_row(db: sqlite3.Connection, folder_id: int, entry_id: int) -> sqlite3.Row:
    row = db.execute('SELECT * FROM workbench_folder_items WHERE id=? AND folder_id=?', (entry_id, folder_id)).fetchone()
    if row is None:
        raise WorkbenchNotFound(f'folder item {entry_id} not found in folder {folder_id}')
    return row


def add_item(folder_id: int, kind, item_id, note=None, position: int | None = None) -> dict:
    positive_id(folder_id, 'folder id')
    if kind not in ITEM_KINDS:
        raise WorkbenchInputError(f'item kind must be one of {", ".join(ITEM_KINDS)}')
    positive_id(item_id, f'{kind} id')
    note = clean_text(note, 'note')
    now = utc_now()
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        _folder_row(db, folder_id)
        identity = _identity(db, kind, item_id)
        if identity is None:
            raise WorkbenchNotFound(f'{kind} {item_id} not found')
        if db.execute('SELECT 1 FROM workbench_folder_items WHERE folder_id=? AND item_kind=? AND item_id=?',
                      (folder_id, kind, item_id)).fetchone():
            raise WorkbenchConflict(f'{kind} {item_id} is already in this folder')
        count = db.execute('SELECT count(*) FROM workbench_folder_items WHERE folder_id=?', (folder_id,)).fetchone()[0]
        if count >= MAX_ITEMS_PER_FOLDER:
            raise WorkbenchConflict(f'folder item limit reached ({MAX_ITEMS_PER_FOLDER})')
        if position is None:
            position = count
        elif type(position) is not int or not 0 <= position <= count:
            raise WorkbenchInputError(f'position must be an integer from 0 to {count}')
        _renumber(db, folder_id, insert_at=position)
        version, pinned = identity
        entry_id = int(db.execute('''INSERT INTO workbench_folder_items(folder_id,item_kind,item_id,pinned_version_id,pinned_json,position,note,added_at,updated_at)
                                     VALUES (?,?,?,?,?,?,?,?,?)''',
                                  (folder_id, kind, item_id, version, json.dumps(pinned, sort_keys=True), position, note, now, now)).lastrowid)
        audit(db, 'workbench_folder_item_add', 'workbench_folder', folder_id,
              {'entry_id': entry_id, 'item_kind': kind, 'item_id': item_id, 'pinned_version_id': version, 'pinned': pinned,
               'note': note, 'evidence_changed': False})
        return _item_payload(db, _item_row(db, folder_id, entry_id))


def _renumber(db: sqlite3.Connection, folder_id: int, *, insert_at: int | None = None, order: list[int] | None = None) -> None:
    ids = order if order is not None else [r[0] for r in db.execute(
        'SELECT id FROM workbench_folder_items WHERE folder_id=? ORDER BY position, id', (folder_id,))]
    for index, entry_id in enumerate(ids):
        position = index + 1 if insert_at is not None and index >= insert_at else index
        db.execute('UPDATE workbench_folder_items SET position=? WHERE id=?', (position, entry_id))


def update_item(folder_id: int, entry_id: int, *, note=_UNSET) -> dict:
    positive_id(folder_id, 'folder id')
    positive_id(entry_id, 'folder item id')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _item_row(db, folder_id, entry_id)
        if note is _UNSET:
            return _item_payload(db, row)
        new_note = clean_text(note, 'note')
        db.execute('UPDATE workbench_folder_items SET note=?, updated_at=? WHERE id=?', (new_note, utc_now(), entry_id))
        audit(db, 'workbench_folder_item_note', 'workbench_folder', folder_id,
              {'entry_id': entry_id, 'item_kind': row['item_kind'], 'item_id': row['item_id'], 'before': row['note'], 'after': new_note})
        return _item_payload(db, _item_row(db, folder_id, entry_id))


def remove_item(folder_id: int, entry_id: int) -> dict:
    positive_id(folder_id, 'folder id')
    positive_id(entry_id, 'folder item id')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _item_row(db, folder_id, entry_id)
        db.execute('DELETE FROM workbench_folder_items WHERE id=?', (entry_id,))
        _renumber(db, folder_id)
        audit_id = audit(db, 'workbench_folder_item_remove', 'workbench_folder', folder_id,
                         {'entry_id': entry_id, 'item_kind': row['item_kind'], 'item_id': row['item_id'],
                          'pinned_version_id': row['pinned_version_id'], 'pinned_json': row['pinned_json'], 'note': row['note'],
                          'position': row['position'], 'evidence_changed': False})
        return {'removed': entry_id, 'audit_id': audit_id, 'evidence_changed': False}


def reorder_items(folder_id: int, order) -> dict:
    positive_id(folder_id, 'folder id')
    if not isinstance(order, (list, tuple)) or any(type(i) is not int for i in order):
        raise WorkbenchInputError('order must be a list of folder item ids')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        _folder_row(db, folder_id)
        current = [r[0] for r in db.execute('SELECT id FROM workbench_folder_items WHERE folder_id=? ORDER BY position, id', (folder_id,))]
        if sorted(order) != sorted(current) or len(set(order)) != len(order):
            raise WorkbenchInputError('order must list every item of the folder exactly once')
        _renumber(db, folder_id, order=list(order))
        audit(db, 'workbench_folder_reorder', 'workbench_folder', folder_id, {'before': current, 'after': list(order)})
    return get_folder(folder_id)


def move_item(folder_id: int, entry_id: int, delta: int) -> dict:
    if delta not in (-1, 1):
        raise WorkbenchInputError('delta must be -1 or 1')
    with session() as db:
        current = [r[0] for r in db.execute('SELECT id FROM workbench_folder_items WHERE folder_id=? ORDER BY position, id', (folder_id,))]
    if entry_id not in current:
        raise WorkbenchNotFound(f'folder item {entry_id} not found in folder {folder_id}')
    index = current.index(entry_id)
    target = index + delta
    if 0 <= target < len(current):
        current[index], current[target] = current[target], current[index]
    return reorder_items(folder_id, current)


def folders_containing(kind: str, item_id: int) -> list[dict]:
    with session() as db:
        return [{'folder_id': r['folder_id'], 'entry_id': r['id'], 'name': r['name']} for r in db.execute(
            '''SELECT i.id, i.folder_id, f.name FROM workbench_folder_items i JOIN workbench_folders f ON f.id=i.folder_id
               WHERE i.item_kind=? AND i.item_id=? ORDER BY f.name, f.id''', (kind, item_id))]


def _export_rows(folder_id: int, recursive: bool) -> tuple[dict, list[dict]]:
    with session() as db:
        root = _folder_payload(db, _folder_row(db, folder_id))
        ids = _descendants(db, folder_id) if recursive else [folder_id]
        rows = []
        for ident in ids:
            path = ' / '.join(p['name'] for p in _path(db, ident))
            for row in db.execute('SELECT * FROM workbench_folder_items WHERE folder_id=? ORDER BY position, id', (ident,)):
                rows.append(_item_payload(db, row) | {'folder_path': path})
                if len(rows) > MAX_EXPORT_ITEMS:
                    raise WorkbenchConflict(f'export exceeds {MAX_EXPORT_ITEMS} references')
    return root, rows


def _csv_cell(value) -> str:
    text = '' if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def export_folder(folder_id: int, fmt: str = 'json', *, recursive: bool = False) -> tuple[bytes, str]:
    """Reference list as JSON (exact) or CSV (spreadsheet formula guard on cells); returns (bytes, media type)."""
    positive_id(folder_id, 'folder id')
    if fmt not in ('json', 'csv'):
        raise WorkbenchInputError('format must be json or csv')
    root, rows = _export_rows(folder_id, recursive)
    if fmt == 'json':
        document = {
            'schema': EXPORT_SCHEMA, 'exported_at': utc_now(), 'recursive': recursive,
            'folder': {k: root[k] for k in ('id', 'name', 'note', 'path', 'created_at', 'updated_at')},
            'scope': {'references_only': True, 'source_bytes_included': False, 'text_included': False,
                      'membership_is_evidence': False, 'locators': 'inert', 'status_computed_at_export': True},
            'counts': {'items': len(rows), **{s: sum(1 for r in rows if r['status'] == s) for s in ('unchanged', 'changed', 'missing')}},
            'items': [{k: r[k] for k in ('folder_id', 'folder_path', 'position', 'item_kind', 'item_id', 'pinned_version_id',
                                          'pinned', 'status', 'changed_keys', 'locator', 'note', 'added_at')} for r in rows],
        }
        return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False, allow_nan=False).encode('utf-8') + b'\n', 'application/json'
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow([_csv_cell(v) for v in (r['folder_id'], r['folder_path'], r['position'], r['item_kind'], r['item_id'],
                                                 r['pinned_version_id'], r['status'], ' '.join(r['changed_keys']), r['locator'],
                                                 r['note'], json.dumps(r['pinned'], ensure_ascii=False, sort_keys=True))])
    return buffer.getvalue().encode('utf-8'), 'text/csv; charset=utf-8'
