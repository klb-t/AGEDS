"""Read-only, self-contained metadata snapshots and structural verification.

This package carries literal references and recorded bytes hashes, never source
bytes. Its digest detects changes against a known digest; it is not a signature,
an authorship proof, a truth check, or a promise of live-state restoration.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from .verification_budget import VerificationBudget, VerificationLimitError

SCHEMA_ID = 'ageds.metadata-package/v1'
CANONICALIZATION = 'sorted-keys-compact-utf8-no-nan/v1'
TABLES = (
    'cases', 'sources', 'artifacts', 'source_observations', 'events',
    'processing_runs', 'derived_text', 'annotations', 'evidence_anchors',
    'tags', 'artifact_tags', 'event_tags', 'links', 'jobs', 'audit_log',
    'schema_migrations',
)
ORDER_COLUMNS = {
    'artifact_tags': 'artifact_id,tag_id', 'event_tags': 'event_id,tag_id',
    'schema_migrations': 'version',
}
OBJECT_TABLES = {
    'case': 'cases', 'source': 'sources', 'artifact': 'artifacts',
    'source_observation': 'source_observations', 'event': 'events',
    'processing_run': 'processing_runs', 'derived_text': 'derived_text',
    'annotation': 'annotations', 'evidence_anchor': 'evidence_anchors',
    'tag': 'tags', 'job': 'jobs', 'link': 'links',
}

# Extension tables (feature areas, schema versions >= 10) travel in a separate,
# versioned ``extension_tables`` section. The core ``tables``/``integrity``
# format above is unchanged. Only tables named here are exported: a new table
# is never included silently. A table present in the database but neither
# declared, excluded nor internal is listed by name in ``undeclared_tables``.
EXTENSION_SCHEMA_ID = 'ageds.metadata-extension-tables/v1'
EXTENSION_TABLES = (
    'collections', 'catalog_scans', 'catalog_content_groups', 'catalog_entries', 'catalog_entry_times',
    'catalog_issues', 'catalog_entry_artifacts', 'catalog_marks', 'catalog_profiles',
    'catalog_description_runs', 'catalog_annotations', 'catalog_annotation_decisions',
    'event_times', 'record_exports', 'record_export_files', 'record_export_parses',
    'logical_records', 'logical_occurrences', 'chat_attachment_refs',
    'contact_observations', 'contact_observation_values', 'recording_name_observations',
    'event_media_references',
    'speaker_turns', 'speaker_assignments', 'event_candidates', 'derived_text_status', 'gmail_api_fetches',
    'annotation_proposals', 'hypothesis_imports', 'relation_hypotheses',
    'reports',
    'timeline_correlation_runs', 'timeline_hypotheses', 'timeline_hypothesis_status',
    'workbench_saved_searches', 'workbench_saved_search_runs', 'workbench_folders', 'workbench_folder_items',
    'workbench_bookmarks',
    # E1 (time budget): append-only interpretive runs with parameters/config hash/result hash, the
    # labelling runs and per-event labels they reference (the private category configuration is only
    # hashed, so labels cannot be rebuilt from a package), human label corrections, and model runs
    # with their labelled outputs (A04: every model attempt is pinned).
    'analysis_runs', 'analysis_label_runs', 'analysis_event_labels', 'analysis_label_annotations',
    'analysis_model_runs', 'analysis_model_outputs',
    # E2 (hypotheses): preregistered versions (hash chain), event-study/candidate runs, dossier
    # pointers and human classifications, theses and disclosures; all append-only human/interpretive.
    'hypotheses', 'hypothesis_versions', 'hypothesis_runs', 'hypothesis_dossier_items',
    'hypothesis_dossier_decisions', 'hypothesis_theses', 'hypothesis_thesis_evidence', 'hypothesis_disclosures',
    # I (import planner): immutable plans and items, human decisions per plan version, execution runs
    # and attempts (provenance of what was imported with which importer/config; leases are historical
    # metadata as for core jobs/processing_runs), and audited abandonments of catalog scans.
    'import_plans', 'import_plan_items', 'import_plan_item_members', 'import_plan_versions',
    'import_plan_decisions', 'import_plan_runs', 'import_plan_attempts', 'catalog_scan_abandonments',
    # L1 (location): import identities and runs (importer, parser version, stored precision, stream, counts,
    # coverage), private place sets used by presence runs (they define what a presence interval means),
    # append-only presence runs/intervals and correlation runs, hypotheses and their review history.
    'location_imports', 'location_import_runs', 'location_place_sets', 'location_presence_runs',
    'location_presence_intervals', 'location_correlation_runs', 'location_correlation_hypotheses',
    'location_correlation_status',
    # L2 (traces): parser observations of personal-data exports and file metadata, all append-only.
    # Photo sidecars keep third-party ``people`` names raw (exported like every observation); geotag,
    # provider-location and browser-visit rows exist only as far as the per-run opt-ins
    # (enable_location / enable_browser_history) allowed; sidecar-to-media matches are rule-id
    # hypotheses over a hashed candidate scope.
    'trace_photo_sidecars', 'trace_photo_matches', 'trace_geotags', 'trace_document_metadata',
    'trace_browser_visits', 'trace_provider_locations',
    # L3 (ledger): append-only parser observations of bank statements (stated balances and the
    # code-computed balance check), typed ledger entries (exact decimal amounts, minor units,
    # counterparty text) and health/fitness samples (special category data, imported only with a
    # per-run opt-in; exported like other observations — the package is private metadata).
    'ledger_statements', 'ledger_entries', 'health_samples',
    # Advocate model (2026-10-25..27): principals, case assignments and fragments, material versions,
    # case relations, scopes, grants and settings (access history), packages and their snapshots,
    # conversations, turns, answers, derived states, provider consents and calls (hashes, token counts,
    # costs; prompt text only where log_prompt_content was enabled). This is the owner's whole-database
    # export; recipients get export_scope_package instead.
    'principals', 'assignments', 'fragments', 'artifact_versions', 'case_relations',
    'scopes', 'grants', 'capability_registry', 'authz_settings',
    'packages', 'package_items', 'package_snapshots',
    'conversations', 'advocate_turns', 'advocate_answers', 'derived_states',
    'provider_consents', 'provider_calls',
)
EXCLUDED_EXTENSION_TABLES = {
    'api_tokens': 'credential table: token keys, salts and secret digests are never exported',
    'auth_methods': 'credential table: advocate API tokens and browser sessions (salts, secret digests)',
    'timeline_projections': 'derived cache: rebuilt from events/event_times by the versioned display rule',
    'timeline_items': 'derived cache: rebuilt from events/event_times by the versioned display rule',
    'timeline_projection_builds': 'derived cache build log (rule hash and watermarks of a rebuildable projection)',
    'workbench_search_docs': 'derived search index content, rebuilt by workbench-index-check --rebuild',
    'timeline_projection_evictions': 'derived cache bookkeeping: evictions of rebuildable timeline projections',
    'api_token_rejections': ('transient security telemetry (rejected token attempts per hour, key and reason); '
                             'references the excluded credential table api_tokens'),
    # L1 (location): bulk raw samples. Millions of rows per export; re-derived exactly by re-importing the
    # preserved original with the parser version and parameters recorded in location_imports.
    'location_fixes': ('bulk raw position samples (millions per export); rebuilt by re-importing the preserved '
                       'original with the parser version and parameters recorded in location_imports'),
    'location_fix_observations': 'per-export locators of the excluded bulk table location_fixes (rebuilt by re-import)',
    'location_signals': ('bulk raw non-positional signals (Wi-Fi scan summaries, activity records); rebuilt by '
                         're-importing the preserved original'),
    'location_signal_observations': 'per-export locators of the excluded bulk table location_signals (rebuilt by re-import)',
}
EXCLUDED_EXTENSION_COLUMNS = {
    'annotation_proposals': {'token_id': 'reference into the excluded credential table api_tokens'},
}
# Credential-like column names refused in any exported extension row.
CREDENTIAL_COLUMNS = frozenset({'secret_sha256', 'salt', 'token_key', 'secret', 'password', 'token'})
# SQLite internals and the derived, rebuildable full-text index are not metadata.
INTERNAL_TABLE_PREFIXES = ('sqlite_', 'search_fts', 'workbench_fts')


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _integrity(payload: dict) -> dict:
    return {
        'algorithm': 'sha256', 'canonicalization': CANONICALIZATION,
        'payload_sha256': _sha(payload),
        'table_sha256': {name: _sha(payload['tables'][name]) for name in TABLES},
    }


def _order_clause(db, name: str) -> str:
    """Primary-key order; static identifiers from PRAGMA, never user input."""
    columns = sorted((row[5], row[1]) for row in db.execute(f'PRAGMA table_info("{name}")') if row[5])
    return ','.join('"' + column.replace('"', '""') + '"' for _, column in columns) or 'rowid'


def extension_section(db, existing: set[str]) -> dict:
    """Declared extension tables in the caller's read transaction.

    Excluded tables and columns are named with their reason; nothing outside
    ``EXTENSION_TABLES`` is exported. Missing declared tables are explicit.
    """
    declared = set(TABLES) | set(EXTENSION_TABLES) | set(EXCLUDED_EXTENSION_TABLES)
    tables = {}
    for name in EXTENSION_TABLES:
        if name not in existing:
            tables[name] = []
            continue
        excluded = EXCLUDED_EXTENSION_COLUMNS.get(name, {})
        rows = []
        for row in db.execute(f'SELECT * FROM "{name}" ORDER BY {_order_clause(db, name)}'):
            item = {key: row[key] for key in row.keys() if key not in excluded}
            leaked = CREDENTIAL_COLUMNS.intersection(item)
            if leaked:
                raise RuntimeError(f'extension table {name} has credential-like columns {sorted(leaked)}; '
                                   'declare an exclusion in packages.EXCLUDED_EXTENSION_COLUMNS')
            rows.append(item)
        tables[name] = rows
    return {
        'schema': EXTENSION_SCHEMA_ID,
        'declared': list(EXTENSION_TABLES),
        'excluded_tables': dict(sorted(EXCLUDED_EXTENSION_TABLES.items())),
        'excluded_columns': {table: dict(sorted(columns.items()))
                             for table, columns in sorted(EXCLUDED_EXTENSION_COLUMNS.items())},
        'missing_tables': [name for name in EXTENSION_TABLES if name not in existing],
        'undeclared_tables': sorted(name for name in existing if name not in declared
                                    and not name.startswith(INTERNAL_TABLE_PREFIXES)),
        'tables': tables,
        'table_sha256': {name: _sha(rows) for name, rows in tables.items()},
    }


def export_metadata_package(*, include_extension_tables: bool = True) -> dict:
    """Snapshot all core metadata in one read transaction, without migration.

    Older databases can be inspected without rewriting them: missing additive
    tables are explicit, and historical rows are not fabricated. A consumer must
    not treat empty missing tables as evidence that no such history existed.
    Declared extension tables are added as a separate versioned section; the
    payload digest covers it, the core table digests are unchanged.
    """
    # Verification can be used in an isolated CLI without importing config,
    # whose initialization creates live storage directories.
    from .db import connect
    db = connect(read_only=True)
    try:
        db.execute('BEGIN')
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = {}
        for name in TABLES:
            tables[name] = ([dict(row) for row in db.execute(
                f'SELECT * FROM {name} ORDER BY {ORDER_COLUMNS.get(name, "id")}')]
                if name in existing else [])
        payload = {
            'schema': SCHEMA_ID, 'metadata_only': True,
            'source_bytes_included': False, 'replay_supported': False,
            'signed': False,
            'exported_at': datetime.now(timezone.utc).isoformat(),
            'missing_tables': [name for name in TABLES if name not in existing],
            'tables': tables,
        }
        if include_extension_tables:
            payload['extension_tables'] = extension_section(db, existing)
        # Reject nonfinite numeric database values instead of emitting JSON NaN.
        payload['integrity'] = _integrity(payload)
        db.rollback()
        return payload
    finally:
        db.close()


def validate_metadata_package(package: Any, *, budget: VerificationBudget | None = None) -> dict:
    """Verify hashes and the metadata graph without reading referenced paths.

    Return structured errors rather than mutating a database. Active jobs and
    leases are historical metadata here and never resumed by this function.
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    counts: dict[str, int] = {}
    digest_valid = True
    relationships_valid = True
    anchors_valid = True

    def error(code: str, path: str, message: str, group: str = 'relationships'):
        nonlocal digest_valid, relationships_valid, anchors_valid
        errors.append({'code': code, 'path': path, 'message': message})
        if group == 'digest':
            digest_valid = False
        elif group == 'anchors':
            anchors_valid = False
        else:
            relationships_valid = False

    def warning(code: str, path: str, message: str):
        warnings.append({'code': code, 'path': path, 'message': message})

    def result() -> dict:
        return {'valid': not errors, 'digest_valid': digest_valid,
                'relationships_valid': relationships_valid, 'anchors_valid': anchors_valid,
                'errors': errors, 'warnings': warnings, 'counts': counts}

    budget = budget if budget is not None else VerificationBudget()
    try:
        budget.check_structure(package)
    except VerificationLimitError as exc:
        error('verification_limit_exceeded', '$', str(exc), 'anchors')
        return result()

    if not isinstance(package, dict):
        error('invalid_package', '$', 'Package must be a JSON object.', 'digest')
        return result()
    if package.get('schema') != SCHEMA_ID:
        error('unsupported_schema', '$.schema', f'Expected {SCHEMA_ID}.', 'digest')
    for field, expected in (('metadata_only', True), ('source_bytes_included', False),
                            ('replay_supported', False), ('signed', False)):
        if package.get(field) is not expected:
            error('invalid_capability_flag', f'$.{field}', f'{field} must be {expected}.', 'digest')
    tables = package.get('tables')
    if not isinstance(tables, dict):
        error('invalid_tables', '$.tables', 'Tables must be a JSON object.')
        return result()
    integrity = package.get('integrity')
    if not isinstance(integrity, dict):
        error('missing_integrity', '$.integrity', 'Integrity metadata is required.', 'digest')
        integrity = {}
    if integrity.get('algorithm') != 'sha256' or integrity.get('canonicalization') != CANONICALIZATION:
        error('unsupported_integrity', '$.integrity', 'Unknown hash or canonicalization contract.', 'digest')
    payload = {key: value for key, value in package.items() if key != 'integrity'}
    try:
        if integrity.get('payload_sha256') != _sha(payload):
            error('payload_digest_mismatch', '$.integrity.payload_sha256', 'Metadata payload digest does not match.', 'digest')
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        error('noncanonical_json', '$', str(exc), 'digest')
    table_digests = integrity.get('table_sha256')
    if not isinstance(table_digests, dict):
        error('invalid_table_digests', '$.integrity.table_sha256', 'Each core table must have a digest.', 'digest')
        table_digests = {}

    rows: dict[str, list[dict]] = {}
    indexes: dict[str, dict[int, dict]] = {}
    for name in TABLES:
        value = tables.get(name)
        if not isinstance(value, list):
            error('missing_or_invalid_table', f'$.tables.{name}', 'Core table must be an array.')
            value = []
        counts[name] = len(value)
        try:
            if table_digests.get(name) != _sha(value):
                error('table_digest_mismatch', f'$.tables.{name}', 'Table digest does not match.', 'digest')
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
            error('noncanonical_table_json', f'$.tables.{name}', str(exc), 'digest')
        rows[name] = []
        indexes[name] = {}
        seen_joins: set[tuple] = set()
        for position, row in enumerate(value):
            path = f'$.tables.{name}[{position}]'
            if not isinstance(row, dict):
                error('invalid_row', path, 'Table row must be an object.')
                continue
            rows[name].append(row)
            if name in ('artifact_tags', 'event_tags'):
                keys = ('artifact_id', 'tag_id') if name == 'artifact_tags' else ('event_id', 'tag_id')
                identity = tuple(row.get(key) for key in keys)
                if any(type(part) is not int or part <= 0 for part in identity):
                    error('invalid_join_identity', path, 'Join IDs must be positive integers.')
                elif identity in seen_joins:
                    error('duplicate_join_identity', path, 'Repeated join identity.')
                else:
                    seen_joins.add(identity)
                continue
            key = 'version' if name == 'schema_migrations' else 'id'
            identity = row.get(key)
            if type(identity) is not int or identity <= 0:
                error('invalid_identity', f'{path}.{key}', 'ID must be a positive integer.')
            elif identity in indexes[name]:
                error('duplicate_identity', f'{path}.{key}', 'Repeated table identity.')
            else:
                indexes[name][identity] = row

    def reference(row: dict, field: str, target: str, path: str, required=False) -> dict | None:
        identity = row.get(field)
        if identity is None:
            if required:
                error('missing_reference', f'{path}.{field}', f'Required reference to {target}.')
            return None
        if type(identity) is not int or identity not in indexes[target]:
            error('broken_reference', f'{path}.{field}', f'Referenced {target} ID does not exist.')
            return None
        return indexes[target][identity]

    def lookup(table: str, identity: Any) -> dict | None:
        return indexes[table].get(identity) if type(identity) is int else None

    relations = {
        'sources': (('case_id', 'cases', True),),
        'artifacts': (('source_id', 'sources', False), ('parent_artifact_id', 'artifacts', False)),
        'source_observations': (('artifact_id', 'artifacts', True), ('source_id', 'sources', False)),
        'events': (('artifact_id', 'artifacts', False), ('source_id', 'sources', False)),
        'processing_runs': (('job_id', 'jobs', False), ('artifact_id', 'artifacts', False)),
        'derived_text': (('artifact_id', 'artifacts', True), ('run_id', 'processing_runs', False)),
        'annotations': (('artifact_id', 'artifacts', False), ('event_id', 'events', False), ('derived_text_id', 'derived_text', False)),
        'evidence_anchors': (('artifact_id', 'artifacts', True), ('derived_text_id', 'derived_text', True)),
        'artifact_tags': (('artifact_id', 'artifacts', True), ('tag_id', 'tags', True)),
        'event_tags': (('event_id', 'events', True), ('tag_id', 'tags', True)),
        'jobs': (('artifact_id', 'artifacts', False),),
    }
    for table, specifications in relations.items():
        for position, row in enumerate(rows[table]):
            path = f'$.tables.{table}[{position}]'
            for field, target, required in specifications:
                reference(row, field, target, path, required)

    artifact_cases: dict[int, set] = {}
    for identity, artifact in indexes['artifacts'].items():
        source = lookup('sources', artifact.get('source_id'))
        case = source.get('case_id') if source else None
        artifact_cases[identity] = {case} if type(case) is int else set()
    for observation in rows['source_observations']:
        source = lookup('sources', observation.get('source_id'))
        case = source.get('case_id') if source else None
        artifact_id = observation.get('artifact_id')
        if type(artifact_id) is int and artifact_id in artifact_cases and type(case) is int:
            artifact_cases[artifact_id].add(case)
        artifact = lookup('artifacts', artifact_id)
        if artifact:
            for field in ('sha256', 'size_bytes'):
                if artifact.get(field) is not None and observation.get(field) is not None and artifact[field] != observation[field]:
                    error('observation_content_mismatch', f'$.tables.source_observations[id={observation.get("id")}].{field}', 'Observation content identity differs from its artifact.')
    for identity, cases in artifact_cases.items():
        if len(cases) > 1:
            error('cross_case_artifact', f'$.tables.artifacts[id={identity}]', 'Artifact provenance crosses case boundaries.')
        if not cases:
            warning('unknown_artifact_case', f'$.tables.artifacts[id={identity}]', 'Artifact case provenance is unknown.')
        parent_id = indexes['artifacts'][identity].get('parent_artifact_id')
        parent_cases = artifact_cases.get(parent_id, set()) if type(parent_id) is int else set()
        if cases and parent_cases and cases != parent_cases:
            error('cross_case_parent_artifact', f'$.tables.artifacts[id={identity}].parent_artifact_id', 'Artifact and its parent cross case boundaries.')
    for row in rows['events']:
        artifact_id = row.get('artifact_id')
        cases = artifact_cases.get(artifact_id, set()) if type(artifact_id) is int else set()
        source = lookup('sources', row.get('source_id'))
        source_case = source.get('case_id') if source else None
        if cases and type(source_case) is int and cases != {source_case}:
            error('cross_case_event', f'$.tables.events[id={row.get("id")}]', 'Event source and artifact belong to different cases.')

    for run in rows['processing_runs']:
        job = lookup('jobs', run.get('job_id'))
        if job and job.get('artifact_id') is not None and run.get('artifact_id') != job.get('artifact_id'):
            error('run_job_artifact_mismatch', f'$.tables.processing_runs[id={run.get("id")}]', 'Run and job artifacts disagree.')
    for text in rows['derived_text']:
        run = lookup('processing_runs', text.get('run_id'))
        if run and run.get('artifact_id') != text.get('artifact_id'):
            error('text_run_artifact_mismatch', f'$.tables.derived_text[id={text.get("id")}]', 'Text version and processing run artifacts disagree.')
    for annotation in rows['annotations']:
        text = lookup('derived_text', annotation.get('derived_text_id'))
        event = lookup('events', annotation.get('event_id'))
        implied = {item.get('artifact_id') for item in (text, event) if item and type(item.get('artifact_id')) is int}
        if type(annotation.get('artifact_id')) is int:
            implied.add(annotation['artifact_id'])
        if len(implied) > 1:
            error('annotation_artifact_mismatch', f'$.tables.annotations[id={annotation.get("id")}]', 'Annotation target artifacts disagree.')
        source = lookup('sources', event.get('source_id')) if event else None
        event_case = source.get('case_id') if source else None
        if type(event_case) is int and any(artifact_cases.get(identity, set()) and artifact_cases[identity] != {event_case} for identity in implied):
            error('cross_case_annotation_event', f'$.tables.annotations[id={annotation.get("id")}]', 'Annotation event source crosses its artifact case boundary.')

    def polymorphic(kind, identity, path, strict):
        if kind is None and identity is None:
            return
        target = OBJECT_TABLES.get(kind) if isinstance(kind, str) else None
        if target is None:
            if strict:
                error('unknown_link_target_kind', path, 'Unknown target kind cannot be verified.')
            else:
                warning('unknown_audit_target_kind', path, 'Domain-specific audit target cannot be resolved by v1.')
        elif identity is None and not strict:
            # An audit row naming a kind but no id records an action without a target
            # object (e.g. a file-name import run); nothing is referenced, so nothing can
            # be broken. Links (strict) still require both. Historical rows of this shape
            # exist and audit rows are never rewritten.
            warning('audit_target_id_missing', path, f'Audit row names {target} without an ID; no object is referenced.')
        elif type(identity) is not int or identity not in indexes[target]:
            error('broken_object_reference', path, f'Referenced {target} ID does not exist.')

    for link in rows['links']:
        path = f'$.tables.links[id={link.get("id")}]'
        for side in ('left', 'right'):
            polymorphic(link.get(f'{side}_kind'), link.get(f'{side}_id'), f'{path}.{side}', True)
    for audit in rows['audit_log']:
        polymorphic(audit.get('object_kind'), audit.get('object_id'), f'$.tables.audit_log[id={audit.get("id")}]', False)

    # Selector validation uses only text data pinned by derived_text_id. A later
    # transcript for the same artifact has no effect on the selected quote.
    transcript_cache = {}  # One validation call only, including failed decodes.
    for anchor in rows['evidence_anchors']:
        path = f'$.tables.evidence_anchors[id={anchor.get("id")}]'
        text = lookup('derived_text', anchor.get('derived_text_id'))
        if not text or text.get('kind') != 'transcript' or text.get('artifact_id') != anchor.get('artifact_id'):
            error('anchor_version_mismatch', path, 'Anchor must pin a text version of its artifact.', 'anchors')
            continue
        quote = anchor.get('quote_text')
        try:
            quote_digest = hashlib.sha256(quote.encode('utf-8')).hexdigest() if isinstance(quote, str) else None
        except UnicodeError:
            quote_digest = None
        if quote_digest is None or quote_digest != anchor.get('quote_sha256'):
            error('anchor_quote_digest_mismatch', path, 'Quote UTF-8 SHA-256 does not match.', 'anchors')
        start, end = anchor.get('start_ms'), anchor.get('end_ms')
        if type(start) is not int or type(end) is not int or start < 0 or end < start:
            error('invalid_anchor_interval', path, 'Anchor interval must be nonnegative integer milliseconds.', 'anchors')
        try:
            selector = _strict_stored_json(anchor.get('selector_json', ''), budget=budget)
            version_id = text['id']
            if version_id not in transcript_cache:
                try:
                    transcript_cache[version_id] = (True, _strict_stored_json(
                        text.get('segments_json', '[]'), finite=False, budget=budget))
                except VerificationLimitError:
                    raise
                except (ValueError, TypeError, KeyError, IndexError, OverflowError, UnicodeError, RecursionError) as exc:
                    transcript_cache[version_id] = (False, str(exc))
            decoded, value = transcript_cache[version_id]
            if not decoded:
                raise ValueError(value)
            segments = value
            _charge_projection_work(budget, selector, segments)
            projection = _select_quote(selector, segments)
            if (quote, anchor.get('quote_sha256'), start, end) != (projection['quote_text'],projection['quote_sha256'],projection['start_ms'],projection['end_ms']):
                error('anchor_selector_mismatch', path, 'Quote or interval differs from the pinned selector.', 'anchors')
        except VerificationLimitError as exc:
            error('verification_limit_exceeded', path, str(exc), 'anchors')
            return result()
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, UnicodeError, RecursionError) as exc:
            error('invalid_anchor_selector', path, str(exc), 'anchors')

    missing = package.get('missing_tables')
    if not isinstance(missing, list) or any(name not in TABLES for name in missing):
        error('invalid_missing_tables', '$.missing_tables', 'Missing tables must name core tables.')
    elif missing:
        warning('legacy_incomplete_schema', '$.missing_tables', 'Missing historical tables are unknown history, not reconstructed empty history.')
        for name in missing:
            if rows[name]:
                error('contradictory_missing_table', f'$.tables.{name}', 'A missing table cannot also contain rows.')
    if 'extension_tables' in package:
        _validate_extension_section(package['extension_tables'], error, counts)
    warning('unsigned_metadata_only', '$', 'Digest is not authentication; source bytes and replay are not included or verified.')
    return result()


def _validate_extension_section(section: Any, error, counts: dict) -> None:
    """Structure and per-table digests of the optional extension section.

    Packages without the section (older exports) validate exactly as before.
    Rows are not cross-checked against core tables here: the section declares
    and digests what was exported; its semantics belong to each owning area.
    """
    base = '$.extension_tables'
    if not isinstance(section, dict):
        error('invalid_extension_section', base, 'Extension section must be an object.')
        return
    expected = {'schema', 'declared', 'excluded_tables', 'excluded_columns', 'missing_tables',
                'undeclared_tables', 'tables', 'table_sha256'}
    if set(section) != expected:
        error('invalid_extension_section', base, 'Extension section fields differ from v1.')
        return
    if section['schema'] != EXTENSION_SCHEMA_ID:
        error('unsupported_extension_schema', f'{base}.schema', f'Expected {EXTENSION_SCHEMA_ID}.', 'digest')
        return
    declared, tables, digests = section['declared'], section['tables'], section['table_sha256']
    excluded_tables, excluded_columns = section['excluded_tables'], section['excluded_columns']
    if (not isinstance(declared, list) or any(not isinstance(name, str) for name in declared)
            or len(set(declared)) != len(declared)):
        error('invalid_extension_section', f'{base}.declared', 'Declared tables must be unique names.')
        return
    if not isinstance(tables, dict) or set(tables) != set(declared):
        error('undeclared_extension_table', f'{base}.tables', 'Exported tables must equal the declared list.')
        return
    if not isinstance(digests, dict) or set(digests) != set(declared):
        error('invalid_extension_section', f'{base}.table_sha256', 'Each declared table must have a digest.', 'digest')
        return
    if not isinstance(excluded_tables, dict) or not isinstance(excluded_columns, dict):
        error('invalid_extension_section', base, 'Exclusions must be objects with reasons.')
        return
    for name in excluded_tables:
        if name in tables:
            error('excluded_extension_table_present', f'{base}.tables.{name}', 'An excluded table cannot be exported.')
    for field in ('missing_tables', 'undeclared_tables'):
        value = section[field]
        if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
            error('invalid_extension_section', f'{base}.{field}', 'Must be a list of table names.')
    if isinstance(section['missing_tables'], list):
        for name in section['missing_tables']:
            if name not in tables:
                error('invalid_extension_section', f'{base}.missing_tables', 'Missing tables must be declared tables.')
            elif tables[name]:
                error('contradictory_missing_table', f'{base}.tables.{name}', 'A missing table cannot also contain rows.')
    for name in declared:
        rows = tables[name]
        path = f'{base}.tables.{name}'
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            error('invalid_extension_table', path, 'Extension table must be an array of objects.')
            continue
        counts[f'extension:{name}'] = len(rows)
        try:
            if digests.get(name) != _sha(rows):
                error('extension_table_digest_mismatch', path, 'Extension table digest does not match.', 'digest')
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
            error('noncanonical_table_json', path, str(exc), 'digest')
        forbidden = set(excluded_columns.get(name, {})) if isinstance(excluded_columns.get(name), dict) else set()
        for position, row in enumerate(rows):
            present = (forbidden | CREDENTIAL_COLUMNS).intersection(row)
            if present:
                error('excluded_extension_column_present', f'{path}[{position}]',
                      'Excluded or credential-like column present: ' + ', '.join(sorted(present)))
                break


def _strict_stored_json(raw: str, *, finite: bool = True, budget: VerificationBudget | None = None) -> Any:
    """Selectors are executable selection data; ambiguous object keys are invalid.

    This deliberately does not parse arbitrary raw metadata/error strings.
    """
    local = budget if budget is not None else VerificationBudget()
    return local.decode_json(raw, finite=finite, start_depth=4)


def _charge_projection_work(budget: VerificationBudget, selector: Any, segments: Any):
    """Conservative visits before real projection, including repeated word scans.

    Units: one anchor, each selector entry, each touched segment/word, and each
    candidate text character. This bounds preflight plus repeated projection;
    it is not an exact instruction, allocation or wall-clock measurement.
    """
    budget.charge_projection(1)
    if not isinstance(selector, dict) or not isinstance(segments, list):
        return
    def text_charge(value, key):
        if isinstance(value, dict) and isinstance(value.get(key), str):
            budget.charge_projection(len(value[key]))
    if selector.get('kind') == 'segments':
        indices = selector.get('indices')
        if isinstance(indices, list):
            budget.charge_projection(len(indices))
            for index in indices:
                if type(index) is int and 0 <= index < len(segments):
                    budget.charge_projection(1)
                    text_charge(segments[index], 'text')
    elif selector.get('kind') == 'words':
        refs = selector.get('word_refs')
        if not isinstance(refs, list):
            return
        budget.charge_projection(len(refs))
        checked = set()
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            si, wi = ref.get('segment_index'), ref.get('word_index')
            if type(si) is not int or not 0 <= si < len(segments):
                continue
            segment = segments[si]
            if not isinstance(segment, dict):
                continue
            words = segment.get('words')
            if si not in checked:
                checked.add(si)
                budget.charge_projection(1)
                text_charge(segment, 'text')
                if isinstance(words, list):
                    budget.charge_projection(len(words))
                    for word in words:
                        text_charge(word, 'word')
            if isinstance(words, list) and type(wi) is int and 0 <= wi < len(words):
                text_charge(words[wi], 'word')


def _select_quote(selector: Any, segments: Any) -> dict:
    """Resolve the exact stored selector against its pinned text version."""
    from .citations import projection_from_selector
    return projection_from_selector(segments, selector)
