"""Multi-citation bundle: one ``ageds.citation-evidence/v1`` packet per pinned
citation (byte-identical to the single-packet export) plus a bundle manifest.

The bundle manifest lists every packet with SHA-256, size and the packet's own
payload digest. A citation that cannot form a valid packet is listed under
``excluded`` with the reason; nothing is silently dropped. Each packet is read
in its own read transaction (as the single export does). Not a signature.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .common import BUNDLE_MANIFEST, CANONICALIZATION, canonical_json, packet_name, sha256_bytes, sha256_json, utc_now
from .outputs import write_new

BUNDLE_SCHEMA = 'ageds.citation-bundle/v1'
DOMAIN = BUNDLE_SCHEMA + '\n'
MAX_PACKETS = 10_000
SCOPE = {'source_bytes_included': False, 'media_bytes_included': False, 'live_restore_supported': False,
         'tasks_imported': False, 'locators': 'literal_inert_metadata', 'stored_path_included': False}


def export_bundle(pairs: list[tuple[int, int]], directory: Path, *, report_uid: str | None = None) -> dict:
    """Create NEW ``directory`` with ``packets/`` and the bundle manifest."""
    from ..exchange import SCHEMA_ID, PacketLimitError, PacketNotFound, canonical_packet_bytes, export_citation_packet
    if len(pairs) > MAX_PACKETS:
        raise ValueError(f'citation bundle exceeds {MAX_PACKETS} packets')
    directory = Path(directory)
    os.mkdir(directory, 0o700)
    try:
        os.mkdir(directory / 'packets', 0o700)
        packets, excluded, seen = [], [], set()
        for artifact_id, anchor_id in pairs:
            if (artifact_id, anchor_id) in seen:
                continue
            seen.add((artifact_id, anchor_id))
            try:
                packet = export_citation_packet(artifact_id, anchor_id)
                data = canonical_packet_bytes(packet)
            except (PacketNotFound, PacketLimitError, ValueError) as exc:
                excluded.append({'artifact_id': artifact_id, 'anchor_id': anchor_id,
                                 'reason': f'{type(exc).__name__}: {exc}'})
                continue
            name = packet_name(artifact_id, anchor_id)
            write_new(directory / name, data)
            payload = packet['payload']
            packets.append({'path': name, 'sha256': sha256_bytes(data), 'size_bytes': len(data),
                            'artifact_id': artifact_id, 'anchor_id': anchor_id,
                            'derived_text_id': payload['anchor']['derived_text_id'],
                            'payload_sha256': packet['integrity']['payload_sha256'],
                            'quote_sha256': payload['anchor']['quote_sha256']})
        manifest = {'schema': BUNDLE_SCHEMA, 'report_uid': report_uid, 'created_at': utc_now(),
                    'packet_schema': SCHEMA_ID, 'packets': packets, 'excluded': excluded, 'scope': dict(SCOPE),
                    'snapshot': 'one read transaction per packet', 'signed': False}
        manifest['integrity'] = {'algorithm': 'sha256', 'canonicalization': CANONICALIZATION, 'domain': DOMAIN,
                                 'payload_sha256': sha256_json(manifest, DOMAIN)}
        write_new(directory / BUNDLE_MANIFEST, canonical_json(manifest))
        return manifest
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
