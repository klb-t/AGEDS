"""WhatsApp export folder or zip: chat text plus media as child artifacts.

The chat ``.txt`` is imported by the unchanged ``whatsapp_txt`` parser (its
parser version and events are untouched). This module then scans each
message's body lines for attachment/omission markers from
``data/whatsapp_media_patterns.v1.json`` (examples) and records an
``event_media_references`` row per marker, anchored to the message event with
its physical line:

* ``present`` — the named file is in the export; it is acquired as a child
  artifact (parent: the zip, or the chat ``.txt`` for a folder export);
* ``missing_from_export`` — a file is named but absent;
* ``omitted_by_export`` — the export says media were left out.

Media files that no message names are still acquired and reported.
"""
from __future__ import annotations

import json
import re
import tempfile
import zipfile
from contextlib import nullcontext
from pathlib import Path

from ..db import session
from ..evidence import ensure_source, temporary_copy_acquisitions
from . import whatsapp
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, acquire_bytes, add_media_reference, container_scheme,
                          load_data, record_import_run, stored_path, walk_files)

PARSER_NAME = 'whatsapp_media'
# '2' (2026-10-16, review finding 5): members of a zip export are located under
# the zip's SHA-256 (comm_common.container_scheme); a zip imported by '1' stays '1'.
# Folder exports keep their filesystem paths in both versions.
PARSER_VERSION = '2'
_BIDI = re.compile(r'[‎‏‪-‮⁦-⁩]')
_CHAT_NAME = re.compile(r'(^_chat\.txt$|whatsapp|czat|chat)', re.IGNORECASE)


def _patterns() -> dict:
    data = load_data('whatsapp_media_patterns.v1.json')
    return {'set': f"{data['schema']}/v{data['version']}",
            'attached': [(p['id'], re.compile(p['regex'])) for p in data['attached']],
            'omitted': [(p['id'], re.compile(p['regex'])) for p in data['omitted']]}


def _key(name: str) -> str:
    return _BIDI.sub('', name).strip()


def _choose_chat(names: list[str], chat_member: str | None) -> str:
    if chat_member is not None:
        if chat_member not in names:
            raise ValueError(f'chat member not found in export: {chat_member}')
        return chat_member
    texts = [n for n in names if n.lower().endswith('.txt')]
    if len(texts) == 1:
        return texts[0]
    likely = [n for n in texts if _CHAT_NAME.search(Path(n).name)]
    if len(likely) == 1:
        return likely[0]
    raise ValueError('cannot choose the chat text file; pass chat_member explicitly. Candidates: ' + ', '.join(texts[:20]))


def _folder_version(chat_locator: str) -> str:
    """Folder exports: keep '1' when an earlier import recorded version-1 references for this chat."""
    with session() as db:
        earlier = {r[0] for r in db.execute(
            '''SELECT DISTINCT r.parser_version FROM event_media_references r JOIN events e ON e.id=r.event_id
               JOIN artifacts a ON a.id=e.artifact_id WHERE a.source_locator=? AND r.parser=?''',
            (chat_locator, PARSER_NAME))}
    return '1' if '1' in earlier else PARSER_VERSION


def _references(chat_artifact_id: int, patterns: dict):
    """Yield (event_id, physical_line, raw_line, status, file_name, pattern_id) per marker."""
    with session() as db:
        rows = db.execute("SELECT id, body, metadata_json FROM events WHERE artifact_id=? AND event_type='whatsapp_message' "
                          "AND external_id LIKE 'whatsapp_txt:line:%' ORDER BY id", (chat_artifact_id,)).fetchall()
    for row in rows:
        metadata = json.loads(row['metadata_json'])
        first_line = metadata.get('parser', {}).get('line_start')
        for offset, text in enumerate((row['body'] or '').split('\n')):
            line_number = first_line + offset if isinstance(first_line, int) else None
            candidate = text.strip()
            for pattern_id, regex in patterns['attached']:
                match = regex.match(candidate)
                if match:
                    yield row['id'], line_number, text, 'attached', match.group('file'), pattern_id
                    break
            else:
                for pattern_id, regex in patterns['omitted']:
                    match = regex.match(candidate)
                    if match:
                        yield row['id'], line_number, text, 'omitted', None, pattern_id
                        break


def import_whatsapp_export(path: Path, *, chat_label: str | None = None, chat_member: str | None = None,
                           source_locator: str | None = None, collection_id: int | None = None,
                           limits: CommLimits = DEFAULT_LIMITS) -> dict:
    patterns = _patterns()
    issues = Issues(limits.max_issues)
    base_locator = source_locator if source_locator is not None else str(path)
    counts = {'messages': 0, 'references': 0, 'present': 0, 'missing_from_export': 0, 'omitted_by_export': 0,
              'unreferenced_media_files': 0, 'media_not_extracted': 0}
    complete = True
    with tempfile.TemporaryDirectory(prefix='ageds-whatsapp-') as scratch:
        if path.is_dir():
            mode = 'folder'
            files = walk_files(path, limits, issues)
            if issues.counts.get('walk_entry_limit'):
                complete = False
            members = {p.relative_to(path).as_posix(): p for p in files}
            chat_name = _choose_chat(sorted(members), chat_member)
            chat_locator = f'{base_locator}/{chat_name}'
            chat_path = members[chat_name]
            container_id = None
            container_source = None
            version, member_base = _folder_version(chat_locator), base_locator
        elif zipfile.is_zipfile(path):
            mode = 'zip'
            container_source, container_id = acquire(
                path, source_kind='whatsapp_export', source_label=chat_label or Path(path.name).stem,
                locator=base_locator, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                collection_id=collection_id, mime_type='application/zip')
            archive = zipfile.ZipFile(stored_path(container_id))
            infos = [i for i in archive.infolist() if not i.is_dir()]
            if len(infos) > limits.max_archive_members:
                issues.add('archive_member_limit', members=len(infos))
                infos = infos[:limits.max_archive_members]
                complete = False
            if sum(i.file_size for i in infos) > limits.max_archive_expanded_bytes:
                raise ValueError('archive expanded size exceeds the configured limit')
            members = {i.filename: i for i in infos}
            for name in members:
                if name.startswith('/') or '..' in Path(name).parts:
                    issues.add('suspicious_member_name_not_used_as_path', member=name[:200])
            chat_name = _choose_chat(sorted(members), chat_member)
            version, member_base = container_scheme(container_id, base_locator, PARSER_NAME, PARSER_VERSION)
            chat_locator = f'{member_base}!/{chat_name}'
            chat_path = Path(scratch) / 'chat.txt'
            chat_path.write_bytes(archive.read(members[chat_name]))
        else:
            raise ValueError('expected a WhatsApp export folder or a .zip file')
        label = chat_label or Path(chat_name).stem
        # A chat text extracted from the zip is a scratch copy: no source time for it.
        chat_scope = temporary_copy_acquisitions() if mode == 'zip' else nullcontext()
        with chat_scope:
            if mode == 'zip' or collection_id is not None:
                # Pre-acquire under the source whatsapp_txt will use, so the chat text
                # records its container parent / collection; whatsapp_txt then reuses it.
                chat_source = ensure_source('whatsapp_export', label, chat_locator)
                acquire(chat_path, source_kind='whatsapp_export', source_label=label, locator=chat_locator,
                        importer=PARSER_NAME, parser_version=version, original_name=Path(chat_name).name,
                        collection_id=collection_id, source_id=chat_source, parent_artifact_id=container_id,
                        mime_type='text/plain')
            chat = whatsapp.import_whatsapp_txt(chat_path, chat_label, source_locator=chat_locator,
                                                original_name=Path(chat_name).name)
        chat_artifact_id = chat['artifact_id']
        counts['messages'] = chat['messages']
        if mode == 'folder':
            with session() as db:
                container_source = db.execute('SELECT source_id FROM artifacts WHERE id=?', (chat_artifact_id,)).fetchone()['source_id']
        parent_id = container_id if mode == 'zip' else chat_artifact_id
        parent_relation = 'contained_in_export_zip' if mode == 'zip' else 'file_in_same_export_folder_as_chat_text'
        by_key: dict[str, list[str]] = {}
        for name in members:
            if name != chat_name:
                by_key.setdefault(_key(Path(name).name), []).append(name)
        for key, names in by_key.items():
            if len(names) > 1:
                issues.add('duplicate_media_file_name', name=key, members=names[:10])
        acquired: dict[str, int] = {}

        def media_artifact(name: str) -> int | None:
            if name in acquired:
                return acquired[name]
            locator = f'{member_base}!/{name}' if mode == 'zip' else f'{base_locator}/{name}'
            metadata = {'parent_relation': parent_relation, 'chat_artifact_id': chat_artifact_id}
            if mode == 'zip':
                info = members[name]
                if info.file_size > limits.max_attachment_bytes:
                    issues.add('media_size_limit', member=name[:200], size=info.file_size)
                    counts['media_not_extracted'] += 1
                    return None
                if info.flag_bits & 0x1:
                    issues.add('media_member_encrypted', member=name[:200])
                    counts['media_not_extracted'] += 1
                    return None
                artifact_id = acquire_bytes(archive.read(info), name=Path(name).name, source_id=container_source,
                                            locator=locator, importer=PARSER_NAME, parser_version=version,
                                            parent_artifact_id=parent_id, collection_id=collection_id,
                                            extra_metadata=metadata)
            else:
                _, artifact_id = acquire(members[name], source_kind='whatsapp_export', source_label=label,
                                         locator=locator, importer=PARSER_NAME, parser_version=version,
                                         original_name=Path(name).name, collection_id=collection_id,
                                         source_id=container_source, parent_artifact_id=parent_id,
                                         extra_metadata=metadata)
            acquired[name] = artifact_id
            return artifact_id

        referenced: set[str] = set()
        for ordinal, (event_id, line, raw_line, kind, file_name, pattern_id) in enumerate(_references(chat_artifact_id, patterns)):
            counts['references'] += 1
            locator = {'chat_artifact_id': chat_artifact_id, 'line': line, 'raw_line': raw_line,
                       'pattern_id': pattern_id, 'pattern_set': patterns['set']}
            if kind == 'omitted':
                status, artifact_id, name = 'omitted_by_export', None, None
            else:
                name = file_name
                matches = by_key.get(_key(Path(file_name).name), [])
                if matches:
                    referenced.update(matches)
                    artifact_id = media_artifact(matches[0])
                    status = 'present' if artifact_id is not None else 'not_extracted'
                    if len(matches) > 1:
                        locator['other_members_with_same_name'] = matches[1:10]
                else:
                    status, artifact_id = 'missing_from_export', None
            counts[status] = counts.get(status, 0) + 1
            add_media_reference(event_id=event_id, artifact_id=artifact_id, reference_kind='whatsapp_media',
                                reference_key=f'line:{line}:{ordinal}' if line is None else f'line:{line}',
                                reference_name=name, status=status, parser=PARSER_NAME,
                                parser_version=version, locator=locator,
                                metadata={'export_mode': mode, 'parent_relation': parent_relation})
        unreferenced = sorted(set(n for names in by_key.values() for n in names) - referenced)
        for name in unreferenced:
            if media_artifact(name) is not None:
                counts['unreferenced_media_files'] += 1
        if mode == 'zip':
            archive.close()
    if counts['missing_from_export']:
        issues.add('media_named_but_missing_from_export', count=counts['missing_from_export'])
    options = {'pattern_set': patterns['set'], 'mode': mode, 'chat_member': chat_name}
    coverage = {'complete': complete and not counts['media_not_extracted']}
    record_import_run(artifact_id=container_id or chat_artifact_id, importer=PARSER_NAME,
                      parser_version=version, options=options, counts=counts, coverage=coverage, issues=issues)
    return {'parser_version': version, 'mode': mode, 'container_artifact_id': container_id,
            'chat_artifact_id': chat_artifact_id, 'chat_parser_version': chat['parser_version'], **counts,
            'unreferenced_media': unreferenced[:limits.max_raw_items], 'options': options, 'coverage': coverage,
            'issues': issues.as_dict(), 'chat_issues_count': len(chat['issues'])}
