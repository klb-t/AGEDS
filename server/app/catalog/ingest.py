"""Ingest selected catalog entries into the evidence store (an acquisition).

The bytes are re-read from the source (disk file or the archive-member chain),
copied to a temporary file outside the source root, verified against the
catalog SHA-256 and passed to ``evidence.ingest_file`` with the collection and
a virtual source locator. ``ingest_file`` writes the append-only
``source_observations`` row; this module links it back to the catalog entry.
If the bytes changed since the scan, nothing is ingested.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

from .. import db as dbmod
from .. import evidence
from . import archives
from .locator import parse_locator
from .scan import CatalogLimits
from .store import NotFound

LOCATOR_PREFIX = 'catalog:collection'
IntegrityError = evidence.IntegrityError


def source_locator(collection_id: int, locator: str) -> str:
    return f'{LOCATOR_PREFIX}/{collection_id}/{locator}'


def _open_disk(root: Path, root_kind: str, parts: list[bytes]):
    """Open a regular file component-wise without following symlinks."""
    if root_kind == 'file':
        if len(parts) != 1 or os.fsencode(root.name) != parts[0]:
            raise IntegrityError('locator does not match the single-file scan root')
        directory_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
        name = parts[0]
    else:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            finally:
                os.close(directory_fd)
            directory_fd = child
        name = parts[-1]
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    return os.fdopen(fd, 'rb')



def _copy(stream, target, limit: int) -> tuple[str, int]:
    digest, total = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise IntegrityError('member grew beyond the catalogued size limit')
        digest.update(chunk)
        target.write(chunk)
    return digest.hexdigest(), total


def _materialize(chain: list[dict], scan: dict, workdir: Path, limits: CatalogLimits) -> Path:
    """Write the bytes of the last chain element to a file in ``workdir``."""
    parsed = parse_locator(chain[-1]['locator'])
    with ExitStack() as stack:
        current = stack.enter_context(_open_disk(Path(scan['root_path']), scan['root_kind'], parsed['disk']))
        for level, entry in enumerate(chain[1:], start=1):
            container_entry = chain[level - 1]
            size = os.fstat(current.fileno()).st_size if hasattr(current, 'fileno') else None
            if size is None:
                current.seek(0, os.SEEK_END)
                size = current.tell()
            container = archives.open_container(current, size, container_entry['sniffed_type'], container_entry['name'], limits)
            stack.callback(container.close)
            member = next((m for m in container.members() if m.index == entry['member_index']), None)
            if member is None or member.raw_name != parsed['members'][level - 1]['name']:
                raise IntegrityError('archive member no longer matches the catalog entry')
            stream = stack.enter_context(_closing_stream(container.open(member)))
            spool = stack.enter_context(tempfile.TemporaryFile(dir=workdir))
            _copy(stream, spool, limits.max_member_hash_bytes)
            spool.seek(0)
            current = spool
        target = workdir / 'payload'
        current.seek(0)
        with open(target, 'wb') as out:
            digest, _ = _copy(current, out, max(limits.max_hash_bytes, limits.max_member_hash_bytes))
    if digest != chain[-1]['sha256']:
        raise IntegrityError('content changed since the catalog scan; nothing was ingested')
    return target


class _closing_stream:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self.stream

    def __exit__(self, *exc):
        try:
            self.stream.close()
        except archives.DAMAGE_ERRORS:
            pass


def ingest_entries(entry_ids: list[int], *, actor: str = 'user', temp_dir: str | Path | None = None) -> dict:
    results = []
    for entry_id in entry_ids:
        with closing(dbmod.connect()) as db:
            entry = db.execute('SELECT * FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
            if entry is None:
                raise NotFound(f'catalog entry {entry_id} does not exist')
            entry = dict(entry)
            scan = dict(db.execute('SELECT * FROM catalog_scans WHERE id=?', (entry['scan_id'],)).fetchone())
            chain = [entry]
            while chain[0]['parent_entry_id'] is not None:
                chain.insert(0, dict(db.execute('SELECT * FROM catalog_entries WHERE id=?', (chain[0]['parent_entry_id'],)).fetchone()))
        if entry['status'] != 'ok' or not entry['sha256']:
            results.append({'entry_id': entry_id, 'status': 'not_ingestible',
                            'reason': f"entry status {entry['status']}, hash {entry['hash_status']}"})
            continue
        limits = CatalogLimits(**json.loads(scan['limits_json']))
        root = Path(scan['root_path'])
        work_parent = Path(temp_dir or tempfile.gettempdir()).expanduser().resolve(strict=False)
        source_root = root if scan['root_kind'] == 'directory' else root.parent
        if work_parent == source_root or source_root in work_parent.parents:
            raise ValueError('temporary directory must be outside the scanned source root')
        work_parent.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix='ageds-catalog-ingest-', dir=work_parent))
        try:
            payload = _materialize(chain, scan, workdir, limits)
            locator = source_locator(entry['collection_id'], entry['locator'])
            source_id = evidence.ensure_source('catalog_collection', f'catalog collection {entry["collection_id"]}',
                                               f'{LOCATOR_PREFIX}/{entry["collection_id"]}', case_id=scan['case_id'])
            metadata = {'catalog_entry_id': entry_id, 'catalog_scan_id': entry['scan_id'], 'virtual_locator': entry['locator'],
                        'archive_depth': entry['depth'], 'container_chain': [c['locator'] for c in chain[:-1]],
                        'acquisition': 'copied from the catalogued location via a temporary file outside the source root',
                        'catalog_sha256_verified_before_ingest': True}
            artifact_id = evidence.ingest_file(payload, source_id=source_id, source_locator=locator, original_name=entry['name'],
                                               mime_type=entry['sniffed_mime'], metadata=metadata, collection_id=entry['collection_id'])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        with closing(dbmod.connect()) as db:
            observation = db.execute('SELECT MAX(id) FROM source_observations WHERE artifact_id=? AND source_locator=?',
                                     (artifact_id, locator)).fetchone()[0]
            link = db.execute('INSERT INTO catalog_entry_artifacts(entry_id,artifact_id,observation_id,source_locator) VALUES(?,?,?,?)',
                              (entry_id, artifact_id, observation, locator)).lastrowid
            db.execute('INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,?,?,?)',
                       (actor, 'catalog_ingest', 'catalog_entry', entry_id,
                        json.dumps({'artifact_id': artifact_id, 'observation_id': observation, 'link_id': link}, sort_keys=True)))
            db.commit()
        results.append({'entry_id': entry_id, 'status': 'ingested', 'artifact_id': artifact_id,
                        'observation_id': observation, 'source_locator': locator})
    return {'results': results, 'ingested': sum(r['status'] == 'ingested' for r in results)}
