"""Shared writer for messaging/social data downloads (``chat_message`` conventions).

Format modules (``importers.facebook``, ``telegram``, ``google_chat``,
``twitter_x``, ``linkedin``) turn one exported file into ``Thread``/``Message``
values; this module acquires the files, binds them to one logical export and
writes, per thread and in one transaction:

* ``chat_message`` events (``social_post`` for public posts): ``thread_id`` =
  ``<namespace>:<thread id>``, ``contact_label`` = platform lane, ``sender`` as
  exported (decoded layer when a decoding rule applies), ``direction`` only when
  the owner is known (parameter or the export's own account record; the basis
  is recorded), ``body`` = message text, ``confidence`` NULL;
* every stated time as a candidate (``created``; ``updated`` for edits);
* ``metadata.chat`` (platform, conversation id/title, message id, content kind,
  reply/forward references, reactions), ``metadata.signal``, ``metadata.text``
  (raw vs decoded layer), bounded ``metadata.raw``, locator and issues;
* logical identity across exports (``records.add_occurrence``: record kinds
  ``message`` and ``thread``) — a source id when the format has one, otherwise a
  content-derived id whose basis is named in ``metadata.chat.record_id_basis``;
* attachment references (``event_media_references``): ``present`` (acquired
  child), ``missing_from_export``, ``omitted_by_export`` or ``not_extracted``.

Absence of a message in another export is "not observed", never deletion.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import records
from ..db import session
from ..evidence import IntegrityError, ensure_source, ingest_file
from ..importers.comm_common import CommLimits, Issues, legacy_instant, record_import_run
from ..timeinfo import TimeCandidate, record_event_times
from .common import bounded, bounded_body, canonical_sha256, clean_text, dumps

NORMALIZATION = 'messaging-content-v1'


@dataclass
class MediaRef:
    kind: str
    uri: str | None
    name: str | None = None
    status_hint: str | None = None  # e.g. 'omitted_by_export' when the export says so
    metadata: dict = field(default_factory=dict)


@dataclass
class Message:
    key: str
    record_id: str
    record_id_basis: str
    body: str | None
    times: list[TimeCandidate]
    content_kind: str
    chat: dict
    raw: object
    text_layers: dict
    signal: dict
    sender: str | None = None
    sender_id: str | None = None
    recipients: str | None = None
    direction: str | None = None
    direction_basis: str | None = None
    subject: str | None = None
    event_type: str = 'chat_message'
    refs: list[MediaRef] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    locator: dict = field(default_factory=dict)


@dataclass
class Thread:
    thread_id: str
    title: str | None
    participants: list
    messages: list[Message]
    metadata: dict = field(default_factory=dict)


@dataclass
class Owner:
    """Who the export belongs to; only used to set ``direction``, never stored as an identity."""
    name: str | None = None
    ids: frozenset = frozenset()
    basis: str | None = None

    def direction(self, *, sender_name: str | None = None, sender_id: str | None = None) -> tuple[str | None, str | None]:
        if self.ids and sender_id is not None:
            return ('outgoing' if str(sender_id) in self.ids else 'incoming'), self.basis
        if self.name and sender_name is not None:
            return ('outgoing' if sender_name == self.name else 'incoming'), self.basis
        return None, None


@dataclass
class InputFile:
    path: Path
    locator: str
    name: str
    role: str = 'messages'
    shard_index: int | None = None


@dataclass
class FileContext:
    namespace: str
    platform: str
    parser: str
    parser_version: str
    mapping_version: str
    mapping_sha256: str
    artifact_id: int
    source_id: int
    file: InputFile
    limits: CommLimits
    issues: Issues
    owner: Owner
    file_meta: dict = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return f'{self.parser}@{self.parser_version}/map{self.mapping_version}:{self.mapping_sha256[:12]}'

    @property
    def interpretation(self) -> str:
        return f'{self.identity}/{NORMALIZATION}'

    def time_source(self, field_name: str) -> str:
        return f'{self.parser}@{self.parser_version}:{field_name}'


def content_hash(message: Message) -> str:
    return canonical_sha256({'n': NORMALIZATION, 'kind': message.content_kind, 'sender': message.sender,
                             'sender_id': message.sender_id, 'body': (message.body or '').replace('\r\n', '\n'),
                             'media': sorted(f'{r.kind}:{r.uri}' for r in message.refs)})


def derived_record_id(*parts) -> str:
    return 'derived:' + canonical_sha256(list(parts))[:32]


# --------------------------------------------------------------------- media references

MediaResolver = Callable[[MediaRef, 'FileContext'], tuple[str, int | None, dict]]


def no_files_resolver(reason: str = 'export_files_not_available_in_this_import') -> MediaResolver:
    def resolve(ref: MediaRef, ctx: FileContext):
        if ref.status_hint:
            return ref.status_hint, None, {}
        return 'not_extracted', None, {'reason': reason}
    return resolve


def directory_resolver(roots: list[Path], *, collection_id: int | None, label: str,
                       locator_of: Callable[[Path], str]) -> MediaResolver:
    """Resolve an exported relative ``uri`` against candidate export roots and acquire the file."""
    resolved_roots = [r.resolve() for r in roots]
    acquired: dict[str, int] = {}

    def resolve(ref: MediaRef, ctx: FileContext):
        if ref.status_hint:
            return ref.status_hint, None, {}
        uri = (ref.uri or '').replace('\\', '/')
        if not uri or uri.startswith(('http://', 'https://')):
            return 'not_extracted', None, {'reason': 'remote_or_empty_uri'}
        relative = Path(uri)
        if relative.is_absolute() or '..' in relative.parts:
            return 'unreadable', None, {'reason': 'uri_escapes_export_root'}
        for root in resolved_roots:
            candidate = root / relative
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if not candidate.resolve().is_relative_to(root):
                return 'unreadable', None, {'reason': 'uri_escapes_export_root'}
            size = candidate.stat().st_size
            if size > ctx.limits.max_attachment_bytes:
                return 'not_extracted', None, {'reason': 'attachment_size_limit', 'bytes': size}
            key = str(candidate)
            if key not in acquired:
                source_id = ensure_source(f'{ctx.namespace}_export', label, locator_of(candidate))
                acquired[key] = ingest_file(candidate, source_id=source_id, source_locator=locator_of(candidate),
                                            original_name=candidate.name,
                                            metadata={'importer': ctx.parser, 'parser_version': ctx.parser_version,
                                                      'role': 'message_attachment', 'referenced_as': uri},
                                            collection_id=collection_id, parent_artifact_id=ctx.artifact_id)
            return 'present', acquired[key], {'resolved_under_root': str(root.name)}
        return 'missing_from_export', None, {'roots_checked': len(resolved_roots)}
    return resolve


def catalog_resolver(entry_id: int) -> MediaResolver:
    """Import plan: look the uri up among catalogued entries of the same scan (no acquisition)."""
    with session() as db:
        entry = db.execute('SELECT scan_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
    scan_id = entry['scan_id'] if entry else None

    def resolve(ref: MediaRef, ctx: FileContext):
        if ref.status_hint:
            return ref.status_hint, None, {}
        uri = (ref.uri or '').replace('\\', '/').lstrip('/')
        if not uri or scan_id is None or uri.startswith(('http://', 'https://')):
            return 'not_extracted', None, {'reason': 'remote_or_empty_uri' if uri else 'no_uri'}
        with session() as db:
            rows = db.execute('''SELECT id, locator, sha256 FROM catalog_entries WHERE scan_id=?
                                 AND (locator=? OR substr(locator, -?)=?) ORDER BY id LIMIT 5''',
                              (scan_id, uri, len(uri) + 1, '/' + uri)).fetchall()
        if not rows:
            return 'missing_from_export', None, {'basis': 'not_in_catalog_scan', 'scan_id': scan_id}
        return 'not_extracted', None, {'reason': 'catalogued_import_as_its_own_plan_item',
                                       'catalog_entries': [{'entry_id': r['id'], 'sha256': r['sha256']} for r in rows]}
    return resolve


# --------------------------------------------------------------------- writing

def _write_thread(thread: Thread, ctx: FileContext, *, case_id: int, export_id: int, resolver: MediaResolver,
                  stats: Counter) -> None:
    resolutions = []
    for message in thread.messages:
        resolutions.append([resolver(ref, ctx) for ref in message.refs])
    thread_key = f'{ctx.namespace}:{thread.thread_id}'
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        for message, resolved in zip(thread.messages, resolutions):
            body, body_info = bounded_body(message.body, ctx.limits)
            raw, truncated = bounded(message.raw, ctx.limits)
            metadata = {
                'parser': {'name': ctx.parser, 'version': ctx.parser_version, 'mapping_version': ctx.mapping_version,
                           'mapping_sha256': ctx.mapping_sha256, 'interpretation': ctx.interpretation},
                'locator': {'artifact_id': ctx.artifact_id, 'file': ctx.file.name, 'shard_index': ctx.file.shard_index,
                            **message.locator},
                'chat': {'platform': ctx.platform, 'conversation_id': thread.thread_id,
                         'conversation_title': clean_text(thread.title), 'content_kind': message.content_kind,
                         'record_id': message.record_id, 'record_id_basis': message.record_id_basis,
                         'sender_id': message.sender_id, 'direction_basis': message.direction_basis,
                         'hidden': False, **message.chat},
                'signal': message.signal, 'text': message.text_layers, 'body': body_info,
                'raw': raw, 'raw_truncated_values': truncated,
                'semantics': 'message record as exported by the service; not proof of reading, authorship or presence',
                'issues': message.issues,
            }
            from ..evidence import add_event
            primary = next((t for t in message.times if t.kind == 'created' and t.utc), None)
            event_id = add_event(db=db, source_id=ctx.source_id, artifact_id=ctx.artifact_id,
                                 event_type=message.event_type, ts_start=legacy_instant(primary),
                                 direction=message.direction, sender=clean_text(message.sender),
                                 recipients=clean_text(message.recipients), contact_label=ctx.platform,
                                 subject=clean_text(message.subject), body=body,
                                 external_id=f'{ctx.identity}:{message.key}', thread_id=thread_key, confidence=None,
                                 metadata=metadata)
            record_event_times(db, event_id, message.times)
            stats[message.event_type] += 1
            stats['time_candidates'] += len(message.times)
            records.add_occurrence(db, case_id=case_id, namespace=ctx.namespace, export_id=export_id,
                                   artifact_id=ctx.artifact_id, event_id=event_id, record_kind='message',
                                   container_id=thread.thread_id, record_id=message.record_id, locator=message.key,
                                   interpretation=ctx.interpretation, normalization_version=NORMALIZATION,
                                   content_sha256=content_hash(message), structure={'thread': thread.thread_id})
            for ref, (status, artifact_id, extra) in zip(message.refs, resolved):
                _media_reference(db, event_id=event_id, artifact_id=artifact_id, ref=ref, status=status, ctx=ctx,
                                 locator={'artifact_id': ctx.artifact_id, **message.locator}, extra=extra)
                stats[f'media_{status}'] += 1
        records.add_occurrence(db, case_id=case_id, namespace=ctx.namespace, export_id=export_id,
                               artifact_id=ctx.artifact_id, event_id=None, record_kind='thread',
                               container_id=thread.thread_id, record_id=thread.thread_id,
                               locator=f'thread:{thread.thread_id}', interpretation=ctx.interpretation,
                               normalization_version=NORMALIZATION,
                               content_sha256=canonical_sha256({'title': thread.title, 'participants': thread.participants}),
                               structure={'messages_in_file': len(thread.messages)})
    stats['threads'] += 1


def _media_reference(db, *, event_id: int, artifact_id: int | None, ref: MediaRef, status: str, ctx: FileContext,
                     locator: dict, extra: dict) -> None:
    locator_json = dumps(locator)
    metadata_json = dumps({**ref.metadata, **extra})
    key = clean_text(ref.uri or ref.name or ref.kind)
    existing = db.execute('''SELECT id FROM event_media_references WHERE event_id=? AND reference_key=? AND parser=?
                             AND parser_version=? AND artifact_id IS ? AND status=? AND locator_json=? AND metadata_json=?
                             AND reference_kind=?''',
                          (event_id, key, ctx.parser, ctx.parser_version, artifact_id, status, locator_json,
                           metadata_json, ref.kind)).fetchone()
    if existing:
        return
    db.execute('''INSERT INTO event_media_references(event_id,artifact_id,reference_kind,reference_key,reference_name,
                  status,parser,parser_version,locator_json,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',
               (event_id, artifact_id, ref.kind, key, clean_text(ref.name), status, ctx.parser, ctx.parser_version,
                locator_json, metadata_json))


Parser = Callable[[object, FileContext], list[Thread]]
Loader = Callable[[Path, CommLimits, Issues], object]


def import_files(files: list[InputFile], *, namespace: str, platform: str, parser: str, parser_version: str,
                 mapping_version: str, mapping_sha256: str, parse: Parser, load: Loader, owner: Owner,
                 resolver: MediaResolver, export_label: str | None, collection_id: int | None, label: str,
                 limits: CommLimits, options: dict, mime_type: str = 'application/json') -> dict:
    """Acquire every file, bind them to one export, parse and write thread by thread."""
    if not files:
        raise ValueError(f'no {platform} message files among the inputs')
    if export_label:
        records.check_export_binding(namespace=namespace, label=export_label, collection_id=collection_id)
    run_issues = Issues(limits.max_issues)
    acquired = []
    case_id = None
    for item in files[:limits.max_records]:
        source_id = ensure_source(f'{namespace}_export', label, item.locator)
        artifact_id = ingest_file(item.path, source_id=source_id, source_locator=item.locator, original_name=item.name,
                                  mime_type=mime_type,
                                  metadata={'importer': parser, 'parser_version': parser_version, 'file_role': item.role,
                                            'shard_index': item.shard_index, 'options': options},
                                  collection_id=collection_id)
        with session() as db:
            row = db.execute('SELECT a.sha256, a.stored_path, s.case_id FROM artifacts a JOIN sources s ON s.id=? '
                             'WHERE a.id=?', (source_id, artifact_id)).fetchone()
        if case_id is None:
            case_id = int(row['case_id'])
        elif case_id != int(row['case_id']):
            raise IntegrityError('export files resolve to different cases')
        acquired.append((item, source_id, artifact_id, row['sha256'], Path(row['stored_path'])))
    if len(files) > limits.max_records:
        run_issues.add('file_limit', limit=limits.max_records, files=len(files))
    label_origin = 'user'
    if not export_label:
        digest = hashlib.sha256('\n'.join(sorted(a[3] for a in acquired)).encode()).hexdigest()
        export_label, label_origin = f'derived:{digest[:16]}', 'derived_from_file_hashes'
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        export_id = records.ensure_export(db, case_id=case_id, namespace=namespace, label=export_label,
                                          label_origin=label_origin, collection_id=collection_id)
        export_files = {a[2]: records.ensure_export_file(db, export_id=export_id, artifact_id=a[2], file_role=a[0].role,
                                                         shard_index=a[0].shard_index, file_name=a[0].name)
                        for a in acquired}
    totals = Counter()
    results = []
    complete = len(files) <= limits.max_records
    for item, source_id, artifact_id, sha256, stored in acquired:
        issues = Issues(limits.max_issues)
        ctx = FileContext(namespace, platform, parser, parser_version, mapping_version, mapping_sha256, artifact_id,
                          source_id, item, limits, issues, owner)
        stats = Counter()
        coverage = 'complete'
        try:
            data = load(stored, limits, issues)
            threads = parse(data, ctx)
        except (ValueError, OverflowError) as exc:
            issues.add('file_not_parsed', detail=str(exc)[:300])
            threads, coverage = [], 'failed'
        for thread in threads:
            try:
                _write_thread(thread, ctx, case_id=case_id, export_id=export_id, resolver=resolver, stats=stats)
            except (IntegrityError, ValueError) as exc:
                issues.add('thread_not_recorded', thread=thread.thread_id[:200], detail=str(exc)[:300])
                coverage = 'partial'
        if coverage == 'complete' and set(issues.counts) & PARTIAL_CODES:
            coverage = 'partial'
        issue_dict = issues.as_dict()
        stats_dict = dict(sorted(stats.items()))
        with session() as db:
            db.execute('BEGIN IMMEDIATE')
            records.record_parse(db, export_file_id=export_files[artifact_id], interpretation=ctx.interpretation,
                                 parser=parser, parser_version=parser_version, mapping_version=mapping_version,
                                 mapping_sha256=mapping_sha256, normalization_version=NORMALIZATION,
                                 limits={'max_records': limits.max_records, 'max_body_chars': limits.max_body_chars,
                                         'max_json_bytes': limits.max_json_bytes},
                                 coverage=coverage, stats=stats_dict, issues=issue_dict)
        totals.update(stats)
        complete = complete and coverage == 'complete'
        results.append({'file_name': item.name, 'locator': item.locator, 'artifact_id': artifact_id, 'sha256': sha256,
                        'coverage': coverage, 'stats': stats_dict, 'issues': issue_dict})
        record_import_run(artifact_id=artifact_id, importer=parser, parser_version=parser_version, options=options,
                          counts=stats_dict, coverage={'complete': coverage == 'complete', 'status': coverage},
                          issues=issues)
    return {'namespace': namespace, 'platform': platform, 'parser_version': parser_version,
            'export': {'id': export_id, 'label': export_label, 'label_origin': label_origin, 'collection_id': collection_id},
            'interpretation': f'{parser}@{parser_version}/map{mapping_version}:{mapping_sha256[:12]}/{NORMALIZATION}',
            'files': results, 'totals': dict(sorted(totals.items())), 'options': options,
            'coverage': {'complete': complete}, 'run': run_issues.as_dict(),
            'time_zone_policy': 'source offsets/epochs only; the local machine zone is never consulted'}


# Issue codes meaning "some source content was not interpreted".
PARTIAL_CODES = frozenset({'record_limit', 'message_limit', 'message_not_an_object', 'thread_not_an_object',
                           'message_shape_not_recognized', 'thread_not_recorded'})


def single_input(path: Path, *, source_locator: str | None, original_name: str | None, role: str = 'messages',
                 shard_index: int | None = None) -> InputFile:
    return InputFile(path, source_locator if source_locator is not None else str(path),
                     original_name or path.name, role, shard_index)


def walk_inputs(root: Path, matcher: Callable[[Path], tuple[str, int | None] | None], *, limits: CommLimits,
                issues: Issues, source_locator: str | None) -> list[InputFile]:
    """Bounded, symlink-free walk selecting files by ``matcher`` (→ role, shard index)."""
    from ..importers.comm_common import walk_files
    out = []
    for path in walk_files(root, limits, issues):
        hit = matcher(path)
        if hit is None:
            continue
        relative = path.relative_to(root).as_posix()
        locator = f'{source_locator}/{relative}' if source_locator else str(path)
        out.append(InputFile(path, locator, path.name, hit[0], hit[1]))
    return out

