"""Logical records observed across several exports of the same account data.

A logical record is an identity claimed by the source format (namespace,
container id, record id), e.g. ``('chatgpt', <conversation id>, <node id>)``.
Each time an import observes it, a ``logical_occurrences`` row binds that
identity to the export, the preserved artifact, the parsed event, a JSON
locator, the interpretation (parser + mapping) and a versioned hash of a
canonical content projection. Nothing here decides which occurrence is "true";
the report (``records.report``) lists presence, absence ("not observed"),
content variants and structure variants side by side.

All writers take the caller's connection and transaction. Rows are append-only;
re-observing identical values returns the existing row, a differing value under
the same key raises ``IntegrityError`` instead of overwriting.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from ..evidence import IntegrityError


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def check_export_binding(*, namespace: str, label: str, collection_id: int | None) -> None:
    """Read-only pre-check so a conflicting label fails before files are ingested."""
    from ..db import session
    with session() as db:
        for row in db.execute('SELECT collection_id FROM record_exports WHERE namespace=? AND label=?', (namespace, label)):
            if collection_id is not None and row['collection_id'] != collection_id:
                raise IntegrityError(f'export label {label!r} is already bound to collection {row["collection_id"]}; '
                                     'use a different --export-label')


def ensure_export(db: sqlite3.Connection, *, case_id: int, namespace: str, label: str, label_origin: str,
                  collection_id: int | None, metadata: dict | None = None) -> int:
    row = db.execute('SELECT id, label_origin, collection_id FROM record_exports WHERE case_id=? AND namespace=? AND label=?',
                     (case_id, namespace, label)).fetchone()
    if row:
        if collection_id is not None and row['collection_id'] != collection_id:
            raise IntegrityError(f'export label {label!r} is already bound to collection {row["collection_id"]}; '
                                 'use a different --export-label')
        return int(row['id'])
    if collection_id is not None:
        collection = db.execute('SELECT case_id FROM collections WHERE id=?', (collection_id,)).fetchone()
        if collection is None or collection['case_id'] != case_id:
            raise IntegrityError('collection does not exist in the export case')
    cur = db.execute('INSERT INTO record_exports(case_id,namespace,label,label_origin,collection_id,metadata_json) VALUES (?,?,?,?,?,?)',
                     (case_id, namespace, label, label_origin, collection_id, _json(metadata or {})))
    db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('record_export_registered','record_export',?,?)",
               (cur.lastrowid, _json({'namespace': namespace, 'label': label, 'label_origin': label_origin,
                                      'collection_id': collection_id})))
    return int(cur.lastrowid)


def ensure_export_file(db: sqlite3.Connection, *, export_id: int, artifact_id: int, file_role: str,
                       shard_index: int | None, file_name: str) -> int:
    row = db.execute('SELECT id, file_role, shard_index FROM record_export_files WHERE export_id=? AND artifact_id=?',
                     (export_id, artifact_id)).fetchone()
    if row:
        if (row['file_role'], row['shard_index']) != (file_role, shard_index):
            raise IntegrityError('export file was registered with a different role/shard')
        return int(row['id'])
    cur = db.execute('INSERT INTO record_export_files(export_id,artifact_id,file_role,shard_index,file_name) VALUES (?,?,?,?,?)',
                     (export_id, artifact_id, file_role, shard_index, file_name))
    return int(cur.lastrowid)


def record_parse(db: sqlite3.Connection, *, export_file_id: int, interpretation: str, parser: str, parser_version: str,
                 mapping_version: str | None, mapping_sha256: str | None, normalization_version: str,
                 limits: dict, coverage: str, stats: dict, issues: dict) -> int:
    payload = {'limits': limits, 'coverage': coverage, 'stats': stats, 'issues': issues}
    digest = hashlib.sha256(_json(payload).encode('utf-8')).hexdigest()
    row = db.execute('SELECT id FROM record_export_parses WHERE export_file_id=? AND interpretation=? AND result_sha256=?',
                     (export_file_id, interpretation, digest)).fetchone()
    if row:
        return int(row['id'])
    cur = db.execute('''INSERT INTO record_export_parses(export_file_id,interpretation,parser,parser_version,mapping_version,
                        mapping_sha256,normalization_version,limits_json,coverage,stats_json,issues_json,result_sha256)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (export_file_id, interpretation, parser, parser_version, mapping_version, mapping_sha256,
                      normalization_version, _json(limits), coverage, _json(stats), _json(issues), digest))
    return int(cur.lastrowid)


def ensure_logical_record(db: sqlite3.Connection, *, case_id: int, namespace: str, record_kind: str,
                          container_id: str, record_id: str) -> int:
    key = (case_id, namespace, record_kind, container_id, record_id)
    row = db.execute('SELECT id FROM logical_records WHERE case_id=? AND namespace=? AND record_kind=? AND container_id=? AND record_id=?',
                     key).fetchone()
    if row:
        return int(row['id'])
    return int(db.execute('INSERT INTO logical_records(case_id,namespace,record_kind,container_id,record_id) VALUES (?,?,?,?,?)',
                          key).lastrowid)


def add_occurrence(db: sqlite3.Connection, *, case_id: int, namespace: str, export_id: int, artifact_id: int,
                   event_id: int | None, record_kind: str, container_id: str, record_id: str, locator: str,
                   interpretation: str, normalization_version: str, content_sha256: str | None,
                   structure: dict) -> int:
    record = ensure_logical_record(db, case_id=case_id, namespace=namespace, record_kind=record_kind,
                                   container_id=container_id, record_id=record_id)
    structure_json = _json(structure)
    structure_sha256 = hashlib.sha256(structure_json.encode('utf-8')).hexdigest()
    row = db.execute('''SELECT id, event_id, content_sha256, structure_sha256, normalization_version FROM logical_occurrences
                        WHERE logical_record_id=? AND export_id=? AND artifact_id=? AND locator=? AND interpretation=?''',
                     (record, export_id, artifact_id, locator, interpretation)).fetchone()
    if row:
        if (row['event_id'], row['content_sha256'], row['structure_sha256'], row['normalization_version']) != \
           (event_id, content_sha256, structure_sha256, normalization_version):
            raise IntegrityError('logical occurrence conflicts with an earlier observation under the same interpretation')
        return int(row['id'])
    return int(db.execute('''INSERT INTO logical_occurrences(logical_record_id,export_id,artifact_id,event_id,locator,
                             interpretation,normalization_version,content_sha256,structure_sha256,structure_json)
                             VALUES (?,?,?,?,?,?,?,?,?,?)''',
                          (record, export_id, artifact_id, event_id, locator, interpretation, normalization_version,
                           content_sha256, structure_sha256, structure_json)).lastrowid)


def add_attachment_ref(db: sqlite3.Connection, *, event_id: int, export_id: int, ref_kind: str, ref: str,
                       file_id: str | None, name: str | None, locator: str, resolution: str,
                       resolved_artifact_id: int | None, candidates: list, match_rule: str,
                       available_files_sha256: str, metadata: dict | None = None) -> int:
    row = db.execute('''SELECT id, resolution, resolved_artifact_id, candidates_json FROM chat_attachment_refs
                        WHERE event_id=? AND export_id=? AND locator=? AND ref=? AND available_files_sha256=?''',
                     (event_id, export_id, locator, ref, available_files_sha256)).fetchone()
    candidates_json = _json(candidates)
    if row:
        if (row['resolution'], row['resolved_artifact_id'], row['candidates_json']) != (resolution, resolved_artifact_id, candidates_json):
            raise IntegrityError('attachment reference resolution conflicts with an earlier identical context')
        return int(row['id'])
    return int(db.execute('''INSERT INTO chat_attachment_refs(event_id,export_id,ref_kind,ref,file_id,name,locator,resolution,
                             resolved_artifact_id,candidates_json,match_rule,available_files_sha256,metadata_json)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (event_id, export_id, ref_kind, ref, file_id, name, locator, resolution, resolved_artifact_id,
                           candidates_json, match_rule, available_files_sha256, _json(metadata or {}))).lastrowid)
