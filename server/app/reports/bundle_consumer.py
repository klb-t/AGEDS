#!/usr/bin/env python3
"""Standalone inspector for AGEDS citation bundles (standard library only).

Checks the bundle manifest (strict JSON, schema, digest), every listed packet
file (regular file, size, SHA-256) and each packet with the independent
single-packet consumer (``exchange_consumer.inspect_packet``); unlisted files
are errors. Locators are never dereferenced. Internal consistency only:
unsigned, no source bytes, no truth or authorship verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

try:
    from .. import exchange_consumer as _packets
except ImportError:  # executed as a script: load the sibling standalone consumer by path
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        'ageds_exchange_consumer', Path(__file__).resolve().parents[1] / 'exchange_consumer.py')
    _packets = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_packets)

SCHEMA = 'ageds.citation-bundle/v1'
MANIFEST = 'citation-bundle.json'
CANONICALIZATION = 'sorted-keys-compact-utf8-no-nan/v1'
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_PACKETS = 10_000
FIELDS = {'schema', 'report_uid', 'created_at', 'packet_schema', 'packets', 'excluded', 'scope', 'snapshot',
          'signed', 'integrity'}
PACKET_FIELDS = {'path', 'sha256', 'size_bytes', 'artifact_id', 'anchor_id', 'derived_text_id', 'payload_sha256',
                 'quote_sha256'}
_NAME = re.compile(r'packets/a([1-9][0-9]{0,18})-c([1-9][0-9]{0,18})\.json')
_HEX = re.compile(r'[0-9a-f]{64}')


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def constant(token):
        raise ValueError('nonfinite JSON number')
    return json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=pairs, parse_constant=constant)


def read_regular(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('not a regular file within the size limit')
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError('file exceeds size limit')
    return data


def inspect_bundle(path) -> dict:
    """Inspect a bundle directory (or its manifest file); returns a verdict dict."""
    path = Path(path)
    root = path if path.is_dir() else path.parent
    errors: list[dict] = []
    checked: list[dict] = []

    def error(code, where, message):
        errors.append({'code': code, 'path': where, 'message': message})

    def verdict(manifest=None):
        return {'schema': 'ageds.citation-bundle-inspection/v1', 'valid': not errors, 'errors': errors,
                'packets': checked, 'excluded': (manifest or {}).get('excluded', []) if isinstance(manifest, dict) else [],
                'report_uid': manifest.get('report_uid') if isinstance(manifest, dict) else None,
                'limitations': ['Internal consistency only; unsigned and not authenticated.',
                                'Source/media bytes absent; locators are inert and never opened.',
                                'No truth, authorship, acoustic alignment or listening verification.']}

    try:
        raw = read_regular(root / MANIFEST, MAX_MANIFEST_BYTES)
        manifest = strict_json(raw)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        error('unreadable_bundle_manifest', MANIFEST, str(exc))
        return verdict()
    if not isinstance(manifest, dict) or set(manifest) != FIELDS:
        error('invalid_bundle_manifest', MANIFEST, 'bundle manifest fields differ from v1')
        return verdict()
    if manifest['schema'] != SCHEMA or manifest['signed'] is not False:
        error('unsupported_bundle_schema', MANIFEST, f'expected {SCHEMA} and signed=false')
    integrity = manifest['integrity']
    payload = {key: value for key, value in manifest.items() if key != 'integrity'}
    if (not isinstance(integrity, dict) or integrity.get('algorithm') != 'sha256'
            or integrity.get('canonicalization') != CANONICALIZATION or integrity.get('domain') != SCHEMA + '\n'):
        error('unsupported_bundle_integrity', MANIFEST, 'unknown digest contract')
    elif hashlib.sha256((SCHEMA + '\n').encode() + canonical(payload)).hexdigest() != integrity.get('payload_sha256'):
        error('bundle_digest_mismatch', MANIFEST, 'bundle manifest digest does not match')
    if manifest['packet_schema'] != _packets.SCHEMA:
        error('unsupported_packet_schema', MANIFEST, f'expected {_packets.SCHEMA}')
    packets = manifest['packets']
    if not isinstance(packets, list) or len(packets) > MAX_PACKETS or not isinstance(manifest['excluded'], list):
        error('invalid_bundle_manifest', MANIFEST, 'packets/excluded must be bounded lists')
        return verdict(manifest)
    listed = {MANIFEST}
    for position, entry in enumerate(packets):
        where = f'packets[{position}]'
        if not isinstance(entry, dict) or set(entry) != PACKET_FIELDS:
            error('invalid_packet_entry', where, 'packet entry fields differ from v1')
            continue
        name = entry['path']
        match = _NAME.fullmatch(name) if isinstance(name, str) else None
        if match is None or (int(match.group(1)), int(match.group(2))) != (entry['artifact_id'], entry['anchor_id']):
            error('invalid_packet_path', where, 'packet path must be packets/a<artifact>-c<anchor>.json for its ids')
            continue
        if name in listed:
            error('duplicate_packet_path', where, 'packet listed twice')
            continue
        listed.add(name)
        try:
            data = read_regular(root / name, _packets.MAX_BYTES)
        except (OSError, ValueError) as exc:
            error('unreadable_packet', name, str(exc))
            continue
        if len(data) != entry['size_bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            error('packet_file_mismatch', name, 'packet size or SHA-256 differs from the bundle manifest')
            continue
        try:
            report = _packets.inspect_packet(data)
        except (ValueError, OSError) as exc:
            error('packet_inspection_failed', name, str(exc))
            continue
        expected = (entry['artifact_id'], entry['anchor_id'], entry['derived_text_id'], entry['payload_sha256'],
                    entry['quote_sha256'])
        actual = (report['artifact_id'], report['anchor_id'], report['derived_text_id'], report['payload_sha256'],
                  report['quote_sha256'])
        if expected != actual:
            error('packet_identity_mismatch', name, 'packet content differs from its bundle entry')
            continue
        checked.append({'path': name, 'artifact_id': report['artifact_id'], 'anchor_id': report['anchor_id'],
                        'derived_text_id': report['derived_text_id'], 'quote_sha256': report['quote_sha256'],
                        'verification': report['verification']})
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(directory, d))]:
            relative = Path(os.path.relpath(os.path.join(directory, name), root)).as_posix()
            if relative not in listed:
                error('unlisted_file', relative, 'file is not listed in the bundle manifest')
    return verdict(manifest)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', help='bundle directory or its citation-bundle.json')
    args = parser.parse_args(argv)
    result = inspect_bundle(args.bundle)
    print(_packets.report_json(result))
    return 0 if result['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
