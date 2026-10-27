"""Report manifest: every output file with SHA-256 and size, the inputs (row
ids + hashes) and the method record, in canonical JSON.

``verify_report`` recomputes everything from the files alone (no database, no
configuration import): manifest digest, each file's size and SHA-256, unlisted
files, the content digest of ``report.json`` and the citation bundle. A
manifest digest detects change against a known value (the register row keeps
it); it is not a signature and does not authenticate who produced the report.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .bundle_consumer import inspect_bundle, read_regular, strict_json
from .common import CANONICALIZATION, canonical_json, sha256_json

MANIFEST_SCHEMA = 'ageds.report-manifest/v1'
MANIFEST_NAME = 'manifest.json'
DOMAIN = MANIFEST_SCHEMA + '\n'
REPORT_SCHEMA = 'ageds.report/v1'
REPORT_DOMAIN = REPORT_SCHEMA + '\n'
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_REPORT_JSON_BYTES = 256 * 1024 * 1024
FIELDS = {'schema', 'report_uid', 'report_type', 'template', 'created_at', 'signed', 'signature', 'content_sha256',
          'files', 'formats', 'inputs', 'method', 'bundle', 'limitations', 'integrity'}
FILE_FIELDS = {'path', 'sha256', 'size_bytes', 'media_type', 'role'}
MEDIA_TYPES = {'.html': 'text/html', '.json': 'application/json', '.csv': 'text/csv', '.pdf': 'application/pdf',
               '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
LIMITATIONS = [
    'Digests detect change against a known value; the manifest is unsigned and does not authenticate its producer.',
    'Hash agreement proves byte identity of listed files, not authorship, truth or time of creation of sources.',
    'Source and media bytes are not included; locators and paths are inert metadata.',
    'Numbers were computed by code from the listed input rows at generation time; later database changes are not reflected.',
]
_PATH = re.compile(r'(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+')


def _role(path: str) -> str:
    if path == 'report.html':
        return 'primary_html'
    if path == 'report.json':
        return 'computed_document'
    if path == 'report.pdf':
        return 'pdf_from_html'
    if path == 'report.xlsx':
        return 'tables_xlsx'
    if path.startswith('tables/'):
        return 'table_csv'
    if path == 'citations/citation-bundle.json':
        return 'citation_bundle_manifest'
    if path.startswith('citations/packets/'):
        return 'citation_packet'
    return 'other'


def _hash_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def list_files(root: Path) -> list[dict]:
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            relative = Path(os.path.relpath(os.path.join(directory, name), root)).as_posix()
            if relative == MANIFEST_NAME:
                continue
            digest, size = _hash_file(root / relative)
            files.append({'path': relative, 'sha256': digest, 'size_bytes': size,
                          'media_type': MEDIA_TYPES.get(Path(relative).suffix, 'application/octet-stream'),
                          'role': _role(relative)})
    return sorted(files, key=lambda item: item['path'])


def build_manifest(document: dict, root: Path, formats: dict, bundle: dict | None) -> dict:
    method = document['method']
    manifest = {
        'schema': MANIFEST_SCHEMA, 'report_uid': document['report_uid'], 'report_type': document['report_type'],
        'template': method['template'], 'created_at': method['generated_at'], 'signed': False, 'signature': None,
        'content_sha256': document['integrity']['content_sha256'], 'files': list_files(root), 'formats': formats,
        'inputs': method['inputs'], 'method': method, 'bundle': bundle, 'limitations': list(LIMITATIONS),
    }
    manifest['integrity'] = {'algorithm': 'sha256', 'canonicalization': CANONICALIZATION, 'domain': DOMAIN,
                             'payload_sha256': sha256_json(manifest, DOMAIN)}
    return manifest


def content_digest(document: dict) -> str:
    return sha256_json({key: value for key, value in document.items() if key != 'integrity'}, REPORT_DOMAIN)


def verify_report(manifest_path, *, expected_manifest_sha256: str | None = None, check_bundle: bool = True) -> dict:
    """Recompute and compare; returns ``{valid, ..., errors, warnings, counts}``."""
    manifest_path = Path(manifest_path)
    root = manifest_path.parent
    errors: list[dict] = []
    warnings = [{'code': 'unsigned_manifest', 'path': '$',
                 'message': 'Digests detect change only; no signature authenticates this report.'}]
    groups = {'digest': True, 'files': True, 'content': True, 'bundle': True}
    counts = {'files': 0, 'bytes': 0}
    state = {'uid': None, 'sha': None}

    def error(code, path, message, group):
        errors.append({'code': code, 'path': path, 'message': message})
        groups[group] = False

    def result():
        return {'schema': 'ageds.report-verification/v1', 'valid': not errors, 'digest_valid': groups['digest'],
                'files_valid': groups['files'], 'content_valid': groups['content'], 'bundle_valid': groups['bundle'],
                'report_uid': state['uid'], 'manifest_sha256': state['sha'], 'errors': errors,
                'warnings': warnings, 'counts': counts}

    try:
        raw = read_regular(manifest_path, MAX_MANIFEST_BYTES)
        state['sha'] = hashlib.sha256(raw).hexdigest()
        manifest = strict_json(raw)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        error('unreadable_manifest', '$', str(exc), 'digest')
        return result()
    if expected_manifest_sha256 is not None and state['sha'] != expected_manifest_sha256:
        error('manifest_sha256_mismatch', '$', 'Manifest bytes differ from the expected (registered) SHA-256.', 'digest')
    if not isinstance(manifest, dict) or set(manifest) != FIELDS:
        error('invalid_manifest', '$', 'Manifest fields differ from v1.', 'digest')
        return result()
    state['uid'] = manifest['report_uid'] if isinstance(manifest['report_uid'], str) else None
    if manifest['schema'] != MANIFEST_SCHEMA:
        error('unsupported_schema', '$.schema', f'Expected {MANIFEST_SCHEMA}.', 'digest')
    if manifest['signed'] is not False or manifest['signature'] is not None:
        error('invalid_signature_claim', '$.signed', 'This manifest format carries no signature.', 'digest')
    integrity = manifest['integrity']
    if (not isinstance(integrity, dict) or integrity.get('algorithm') != 'sha256'
            or integrity.get('canonicalization') != CANONICALIZATION or integrity.get('domain') != DOMAIN):
        error('unsupported_integrity', '$.integrity', 'Unknown digest contract.', 'digest')
    else:
        payload = {key: value for key, value in manifest.items() if key != 'integrity'}
        try:
            if sha256_json(payload, DOMAIN) != integrity.get('payload_sha256'):
                error('manifest_digest_mismatch', '$.integrity.payload_sha256', 'Manifest payload digest does not match.', 'digest')
        except (TypeError, ValueError) as exc:
            error('noncanonical_json', '$', str(exc), 'digest')
    files = manifest['files']
    if not isinstance(files, list):
        error('invalid_files', '$.files', 'Files must be a list.', 'files')
        return result()
    listed = {MANIFEST_NAME}
    real_root = os.path.realpath(root)
    for position, entry in enumerate(files):
        where = f'$.files[{position}]'
        if not isinstance(entry, dict) or set(entry) != FILE_FIELDS:
            error('invalid_file_entry', where, 'File entry fields differ from v1.', 'files')
            continue
        name = entry['path']
        if (not isinstance(name, str) or not _PATH.fullmatch(name) or '..' in name.split('/')
                or name == MANIFEST_NAME or name in listed):
            error('invalid_file_path', where, 'Path must be a unique relative path inside the report directory.', 'files')
            continue
        listed.add(name)
        target = root / name
        if os.path.islink(target) or not os.path.realpath(target).startswith(real_root + os.sep):
            error('unsafe_file', name, 'Listed file is a symlink or resolves outside the report directory.', 'files')
            continue
        try:
            digest, size = _hash_file(target)
        except OSError as exc:
            error('missing_file', name, str(exc), 'files')
            continue
        counts['files'] += 1
        counts['bytes'] += size
        if size != entry['size_bytes'] or digest != entry['sha256']:
            error('file_digest_mismatch', name, 'File size or SHA-256 differs from the manifest.', 'files')
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in names + [d for d in dirs if os.path.islink(os.path.join(directory, d))]:
            relative = Path(os.path.relpath(os.path.join(directory, name), root)).as_posix()
            if relative not in listed:
                error('unlisted_file', relative, 'File is not listed in the manifest.', 'files')
    if 'report.json' not in listed:
        error('missing_computed_document', 'report.json', 'report.json must be listed.', 'content')
    else:
        try:
            document = strict_json(read_regular(root / 'report.json', MAX_REPORT_JSON_BYTES))
            declared = document.get('integrity', {}).get('content_sha256') if isinstance(document, dict) else None
            if not isinstance(document, dict) or document.get('schema') != REPORT_SCHEMA:
                error('invalid_computed_document', 'report.json', f'Expected {REPORT_SCHEMA}.', 'content')
            elif content_digest(document) != declared or declared != manifest['content_sha256']:
                error('content_digest_mismatch', 'report.json', 'Computed content digest differs.', 'content')
            elif (document.get('report_uid'), document.get('report_type')) != (manifest['report_uid'], manifest['report_type']):
                error('report_identity_mismatch', 'report.json', 'report.json belongs to another report.', 'content')
            elif canonical_json(document.get('method')) != canonical_json(manifest['method']):
                error('method_mismatch', 'report.json', 'Method record differs between report.json and manifest.', 'content')
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            error('unreadable_computed_document', 'report.json', str(exc), 'content')
    bundle = manifest['bundle']
    if bundle is not None:
        if not isinstance(bundle, dict) or bundle.get('path') != 'citations':
            error('invalid_bundle_reference', '$.bundle', 'Bundle reference must name the citations directory.', 'bundle')
        elif check_bundle:
            verdict = inspect_bundle(root / 'citations')
            for item in verdict['errors']:
                error('bundle_' + item['code'], 'citations/' + str(item['path']), item['message'], 'bundle')
            if verdict.get('report_uid') not in (None, manifest['report_uid']):
                error('bundle_report_mismatch', 'citations', 'Bundle belongs to another report.', 'bundle')
    return result()
