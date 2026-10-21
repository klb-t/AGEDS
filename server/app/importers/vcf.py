"""vCard (VCF 2.1/3.0/4.0) import into ``contact_observations``.

A contact card is not an event and not an identity: it records what one
address book said, with its raw lines. Phone/e-mail values get a conservative
``normalized_candidate`` for later matching; any match is a separate,
revocable hypothesis made elsewhere.
"""
from __future__ import annotations

import binascii
import json
import re
from pathlib import Path

from ..db import session
from ..evidence import IntegrityError
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, bounded_text, content_id,
                          parse_content_line, physical_lines, record_import_run, split_unescaped, stored_path,
                          unescape_text)

PARSER_NAME = 'vcard'
PARSER_VERSION = '1'
_QP_HINT = re.compile(r'(?:ENCODING=QUOTED-PRINTABLE|;QUOTED-PRINTABLE)(?:[;:,])', re.IGNORECASE)


def logical_lines(lines: list[str]) -> list[tuple[str, int, int]]:
    """Unfold RFC 2425/6350 continuations and vCard 2.1 quoted-printable soft breaks."""
    logical: list[tuple[str, int, int]] = []
    for number, line in enumerate(lines, start=1):
        if logical:
            text, start, _ = logical[-1]
            head = text.split(':', 1)[0] + ':'
            if line[:1] in (' ', '\t'):
                logical[-1] = (text + line[1:], start, number)
                continue
            if text.endswith('=') and _QP_HINT.search(head):
                logical[-1] = (text[:-1] + line, start, number)
                continue
        if line.strip():
            logical.append((line, number, number))
    return logical


def decode_value(line, problems: list[str]) -> str:
    encoding = ((line.param('ENCODING') or [''])[0] or '').upper()
    bare = [p.upper() for p in line.bare_params()]
    charset = (line.param('CHARSET') or ['utf-8'])[0]
    if encoding == 'QUOTED-PRINTABLE' or 'QUOTED-PRINTABLE' in bare:
        data = binascii.a2b_qp(line.value.encode('latin-1', errors='replace'))
        try:
            return data.decode(charset)
        except LookupError:
            problems.append(f'unknown_charset:{charset}')
        except UnicodeDecodeError:
            problems.append(f'invalid_bytes_for_charset:{charset}')
        return data.decode('utf-8', errors='replace')
    if encoding in ('B', 'BASE64') or 'BASE64' in bare:
        return f'<binary value: {len(line.value)} base64 characters not decoded>'
    return line.value


def _types(line) -> list[str]:
    types = []
    for value in line.param('TYPE'):
        types.extend(part.strip().lower() for part in value.split(',') if part.strip())
    types.extend(p.lower() for p in line.bare_params() if p.upper() not in ('QUOTED-PRINTABLE', 'BASE64'))
    return sorted(set(types))


def normalize_phone(value: str) -> str | None:
    """Digits with an optional leading '+'; no country code is ever inferred."""
    text = value.strip()
    if text.lower().startswith('tel:'):
        text = text[4:]
    text = text.split(';', 1)[0]
    cleaned = re.sub(r'[\s().\-/]', '', text)
    return cleaned if re.fullmatch(r'\+?\d{3,20}', cleaned) else None


def normalize_email(value: str) -> str | None:
    text = value.strip()
    if text.lower().startswith('mailto:'):
        text = text[7:]
    if text.count('@') != 1:
        return None
    local, domain = text.split('@')
    return f'{local}@{domain.lower()}' if local and '.' in domain else None


def import_vcf(path: Path, *, source_locator: str | None = None, original_name: str | None = None,
               collection_id: int | None = None, label: str | None = None,
               limits: CommLimits = DEFAULT_LIMITS) -> dict:
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(
        path, source_kind='contacts_export', source_label=label or 'VCF', locator=locator, importer=PARSER_NAME,
        parser_version=PARSER_VERSION, original_name=original_name, collection_id=collection_id,
        mime_type='text/vcard')
    issues = Issues(limits.max_issues)
    snapshot = stored_path(artifact_id)
    if snapshot.stat().st_size > limits.max_json_bytes:
        issues.add('document_size_limit')
        coverage = {'complete': False}
        record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options={},
                          counts={'contacts': 0}, coverage=coverage, issues=issues)
        return {'artifact_id': artifact_id, 'contacts': 0, 'coverage': coverage, 'issues': issues.as_dict()}
    lines, encoding = physical_lines(snapshot.read_bytes())
    if encoding != 'utf-8':
        issues.add('vcf_invalid_utf8_latin1_fallback')
    cards: list[dict] = []
    current = None
    for text, start, end in logical_lines(lines):
        try:
            line = parse_content_line(text, start, end)
        except ValueError as exc:
            issues.add('vcf_malformed_content_line', line=start, error=str(exc), raw=text[:120])
            if current is not None:
                current['unparsed'].append(text[:limits.max_raw_chars])
            continue
        if line.name == 'BEGIN' and line.value.strip().upper() == 'VCARD':
            if current is not None:
                issues.add('vcf_nested_or_unterminated_card', line=start)
                cards.append(current)
            current = {'lines': [], 'unparsed': [], 'line_start': start, 'line_end': None}
        elif line.name == 'END' and line.value.strip().upper() == 'VCARD':
            if current is None:
                issues.add('vcf_end_without_begin', line=start)
                continue
            current['line_end'] = end
            cards.append(current)
            current = None
        elif current is None:
            issues.add('vcf_property_outside_card', line=start)
        else:
            current['lines'].append(line)
    if current is not None:
        issues.add('vcf_unterminated_card', line=current['line_start'])
        cards.append(current)
    counts = {'contacts': 0, 'phones': 0, 'emails': 0, 'without_uid': 0}
    complete = True
    for card_index, card in enumerate(cards):
        if card_index >= limits.max_records:
            issues.add('record_limit', limit=limits.max_records)
            complete = False
            break
        problems: list[str] = []
        by_name: dict[str, list] = {}
        for line in card['lines']:
            by_name.setdefault(line.name, []).append(line)
        version = by_name.get('VERSION', [None])[0]
        uid_line = by_name.get('UID', [None])[0]
        raw_lines = []
        for line in card['lines'][:limits.max_raw_items]:
            kept, ok = bounded_text(line.raw, limits.max_raw_chars)
            raw_lines.append(kept if ok else {'truncated': kept})
        if uid_line is not None:
            external_id, id_kind = unescape_text(uid_line.value.strip()), 'uid'
        else:
            external_id = 'content-sha256:' + content_id([l.raw for l in card['lines']])
            id_kind = 'content_derived'
            counts['without_uid'] += 1
        fn_line = by_name.get('FN', [None])[0]
        formatted_name = unescape_text(decode_value(fn_line, problems)) if fn_line is not None else None
        n_line = by_name.get('N', [None])[0]
        name_parts = [unescape_text(p) for p in split_unescaped(decode_value(n_line, problems), ';')] if n_line else []
        org_line = by_name.get('ORG', [None])[0]
        organization = '; '.join(unescape_text(p) for p in split_unescaped(decode_value(org_line, problems), ';')) if org_line else None
        values = []
        for kind, name, normalize in (('tel', 'TEL', normalize_phone), ('email', 'EMAIL', normalize_email)):
            for position, line in enumerate(by_name.get(name, [])):
                raw_value = unescape_text(decode_value(line, problems))
                values.append({'value_kind': kind, 'position': position, 'raw_value': raw_value,
                               'normalized_candidate': normalize(raw_value), 'types': _types(line),
                               'metadata': {'line': line.line_start, 'group': line.group,
                                            'normalization': 'syntactic_only_no_country_inference'}})
        if version is None:
            problems.append('version_missing')
        if fn_line is None:
            problems.append('fn_missing')
        locator = {'artifact_id': artifact_id, 'card_index': card_index, 'line_start': card['line_start'],
                   'line_end': card['line_end']}
        metadata = {'unparsed_lines': card['unparsed'][:limits.max_raw_items], 'issues': sorted(set(problems)),
                    'raw_lines_total': len(card['lines']), 'source_encoding': encoding,
                    'matching': 'not_performed; any match to events is a later hypothesis'}
        row = (artifact_id, source_id, PARSER_NAME, PARSER_VERSION, card_index, external_id, id_kind,
               version.value.strip() if version is not None else None, formatted_name,
               json.dumps(name_parts, ensure_ascii=False), organization,
               json.dumps(raw_lines, ensure_ascii=False), json.dumps(locator, sort_keys=True),
               json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        _store_card(row, values)
        for problem in sorted(set(problems)):
            issues.add(problem, card_index=card_index)
        counts['contacts'] += 1
        counts['phones'] += sum(1 for v in values if v['value_kind'] == 'tel')
        counts['emails'] += sum(1 for v in values if v['value_kind'] == 'email')
    coverage = {'complete': complete}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options={},
                      counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser_version': PARSER_VERSION, **counts,
            'coverage': coverage, 'issues': issues.as_dict()}


_COLUMNS = ('artifact_id', 'source_id', 'parser', 'parser_version', 'card_index', 'external_id', 'external_id_kind',
            'vcard_version', 'formatted_name', 'name_parts_json', 'organization', 'raw_lines_json', 'locator_json',
            'metadata_json')


def _store_card(row: tuple, values: list[dict]) -> int:
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM contact_observations WHERE artifact_id=? AND parser=? AND parser_version=? '
                              'AND card_index=?', (row[0], row[2], row[3], row[4])).fetchone()
        if existing is not None:
            if tuple(existing[c] for c in _COLUMNS) != row:
                raise IntegrityError('contact observation conflicts with an earlier parse; use a new parser version')
            return int(existing['id'])
        cur = db.execute(f'INSERT INTO contact_observations({",".join(_COLUMNS)}) VALUES ({",".join("?" * len(_COLUMNS))})', row)
        observation_id = int(cur.lastrowid)
        for value in values:
            db.execute('''INSERT INTO contact_observation_values(contact_observation_id,value_kind,position,raw_value,
                          normalized_candidate,types_json,metadata_json) VALUES(?,?,?,?,?,?,?)''',
                       (observation_id, value['value_kind'], value['position'], value['raw_value'],
                        value['normalized_candidate'], json.dumps(value['types']),
                        json.dumps(value['metadata'], sort_keys=True)))
        title = ' | '.join(x for x in ('contact', row[8], row[10]) if x)
        db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES ('contact_observation',?,?,?)",
                   (observation_id, title, ' '.join(v['raw_value'] for v in values)))
        return observation_id
