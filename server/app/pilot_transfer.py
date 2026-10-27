"""Carry model output from a pilot database into this installation without a new model call.

The originals are acquired here through the normal catalog path first; this step then
copies each pilot text version (ASR/OCR) onto the artifact with the same SHA-256, as a
new version with its own processing run. The run records the pilot run it copies, the
pilot database's SHA-256 and that no model was called. Event candidates follow their
text version. Running it again changes nothing (one transfer key per pilot version).
Nothing is copied for an artifact whose bytes are not already preserved here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .db import session
from .evidence import add_derived_text


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def transfer(pilot_db: Path, *, apply: bool = False, actor: str = 'pilot-transfer') -> dict:
    pilot_db = Path(pilot_db)
    pilot_sha = _sha256_file(pilot_db)
    source = sqlite3.connect(f'file:{pilot_db}?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    report = {'pilot_db_sha256': pilot_sha, 'applied': apply, 'copied': [], 'skipped': []}
    try:
        versions = source.execute('''SELECT d.*, a.sha256 AS artifact_sha256, a.original_name
                                     FROM derived_text d JOIN artifacts a ON a.id = d.artifact_id ORDER BY d.id''').fetchall()
        for version in versions:
            key = f'{pilot_sha}:{version["id"]}'
            with session() as db:
                target = db.execute('SELECT id FROM artifacts WHERE sha256=? ORDER BY id LIMIT 1',
                                    (version['artifact_sha256'],)).fetchone()
                if target is None:
                    report['skipped'].append({'pilot_derived_text_id': version['id'], 'name': version['original_name'],
                                              'reason': 'no artifact with these bytes here; acquire the original first'})
                    continue
                done = db.execute("SELECT id FROM derived_text WHERE json_extract(metadata_json, '$.pilot_transfer.key')=?",
                                  (key,)).fetchone()
                if done is not None:
                    report['skipped'].append({'pilot_derived_text_id': version['id'], 'reason': 'already transferred',
                                              'derived_text_id': done['id']})
                    continue
            run = source.execute('SELECT * FROM processing_runs WHERE id=?', (version['run_id'],)).fetchone() \
                if version['run_id'] else None
            entry = {'pilot_derived_text_id': version['id'], 'kind': version['kind'], 'model': version['model'],
                     'artifact_id': target['id'], 'name': version['original_name']}
            if not apply:
                report['copied'].append({**entry, 'dry_run': True})
                continue
            now = datetime.now(timezone.utc).isoformat()
            provenance = {'transferred_from': {
                'pilot_db_sha256': pilot_sha, 'pilot_run': dict(run) if run else None,
                'pilot_derived_text_id': version['id'], 'pilot_created_at': version['created_at'],
                'note': 'model output copied from the pilot; no new model call; original bytes matched by SHA-256'}}
            if run is not None:
                provenance['transferred_from']['pilot_run'].pop('lease_token', None)
            with session() as db:
                cur = db.execute('''INSERT INTO processing_runs(job_id,artifact_id,lease_token,status,started_at,finished_at,
                    tool,tool_version,provider,model,model_version,parameters_json,provenance_json,metadata_json)
                    VALUES(NULL,?,?,'done',?,?,?,?,?,?,?,?,?,?)''',
                    (target['id'], f'pilot-transfer:{uuid.uuid4().hex}', now, now, 'ageds-pilot-transfer', '1',
                     run['provider'] if run else 'unknown', run['model'] if run else (version['model'] or 'unknown'),
                     run['model_version'] if run else 'unknown', run['parameters_json'] if run else '{}',
                     json.dumps(provenance, ensure_ascii=False), json.dumps({'actor': actor}, ensure_ascii=False)))
                run_id = int(cur.lastrowid)
                metadata = json.loads(version['metadata_json'] or '{}')
                metadata['pilot_transfer'] = {'key': key, 'pilot_derived_text_id': version['id'], 'run_id': run_id}
                derived_id = add_derived_text(target['id'], version['kind'], version['text'], model=version['model'],
                                              language=version['language'],
                                              segments=json.loads(version['segments_json'] or '[]'),
                                              confidence=version['confidence'], run_id=run_id, metadata=metadata, db=db)
                candidates = 0
                if source.execute("SELECT 1 FROM sqlite_master WHERE name='event_candidates'").fetchone():
                    for row in source.execute('SELECT * FROM event_candidates WHERE derived_text_id=? ORDER BY candidate_index',
                                              (version['id'],)):
                        db.execute('''INSERT INTO event_candidates(artifact_id,derived_text_id,run_id,candidate_index,parser,
                                      sender_text,time_text,body_text,layout_side,locators_json,metadata_json)
                                      VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                                   (target['id'], derived_id, run_id, row['candidate_index'], row['parser'], row['sender_text'],
                                    row['time_text'], row['body_text'], row['layout_side'], row['locators_json'],
                                    row['metadata_json']))
                        candidates += 1
                db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)',
                           (actor, 'pilot_transfer', 'derived_text', derived_id,
                            json.dumps({'key': key, 'run_id': run_id, 'event_candidates': candidates})))
            report['copied'].append({**entry, 'derived_text_id': derived_id, 'run_id': run_id, 'event_candidates': candidates})
    finally:
        source.close()
    return report


def _pilot_artifacts(pilot_db: Path) -> dict[str, str]:
    source = sqlite3.connect(f'file:{pilot_db}?mode=ro', uri=True)
    try:
        return {sha: name for sha, name in source.execute('SELECT sha256, original_name FROM artifacts')}
    finally:
        source.close()


def acquire_and_transfer(pilot_db: Path, source_dir: Path, *, apply: bool = False, actor: str = 'pilot-transfer',
                         label: str | None = None) -> dict:
    """Acquire the pilot originals here from SOURCE_DIR (read-only catalog scan + ingest), then copy the results.

    Refuses when SOURCE_DIR lacks any pilot original (bytes compared by SHA-256). Only entries whose bytes
    are pilot originals are ingested. Originals already preserved here are not acquired again.
    """
    from .catalog import ingest, store
    from .catalog.scan import run_catalog_scan
    pilot_db, source_dir = Path(pilot_db), Path(source_dir)
    wanted = _pilot_artifacts(pilot_db)
    found = {_sha256_file(p): p.name for p in sorted(source_dir.iterdir()) if p.is_file()}
    missing = sorted(name for sha, name in wanted.items() if sha not in found)
    if missing:
        raise ValueError(f'source directory lacks {len(missing)} pilot originals: {missing}')
    with session() as db:
        present = {row['sha256'] for row in db.execute('SELECT sha256 FROM artifacts')}
    to_acquire = sorted(sha for sha in wanted if sha not in present)
    report = {'pilot_originals': len(wanted), 'already_here': len(wanted) - len(to_acquire),
              'to_acquire': [found[sha] for sha in to_acquire], 'source_files_not_in_pilot': len(set(found) - set(wanted))}
    if not apply:
        report['transfer'] = {'dry_run': True, 'pilot_versions': len(transfer(pilot_db)['copied']) if not to_acquire else
                              'counted after acquisition (originals not here yet)'}
        return report
    if to_acquire:
        source = sqlite3.connect(f'file:{pilot_db}?mode=ro', uri=True)
        source.row_factory = sqlite3.Row
        pilot_collection = source.execute('SELECT * FROM collections ORDER BY id LIMIT 1').fetchone()
        source.close()
        fields = {key: pilot_collection[key] for key in ('acquisition_method', 'operator', 'device_or_account', 'export_date_raw')
                  if pilot_collection is not None and pilot_collection[key]}
        collection = store.create_collection(label or (pilot_collection['label'] if pilot_collection else 'Pilot originals'),
                                             actor=actor, notes=f'acquired again from the pilot source for this installation; '
                                             f'pilot database {_sha256_file(pilot_db)}', **fields)
        scan = run_catalog_scan(source_dir, collection['id'])
        entry_ids, after = [], None
        while True:
            page = store.list_entries({'scan_id': scan['scan_id']}, after_id=after, limit=500)
            entry_ids += [e['id'] for e in page['entries'] if e['sha256'] in to_acquire and e['status'] == 'ok']
            after = page['next_after_id']
            if after is None:
                break
        ingested = ingest.ingest_entries(entry_ids, actor=actor)
        report['acquisition'] = {'collection_id': collection['id'], 'scan_id': scan['scan_id'], 'ingested': ingested['ingested']}
    report['transfer'] = transfer(pilot_db, apply=True, actor=actor)
    return report
