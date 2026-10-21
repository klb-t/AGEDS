"""Text decoding with explicit encoding hypotheses, and a bounded delimited-text reader.

Single-byte encodings (CP1250, ISO-8859-2, CP1252) decode almost any byte
sequence, so "it decoded" is not evidence. Without a BOM or valid non-ASCII
UTF-8 the choice is a *hypothesis*: every candidate is decoded, scored by letter
plausibility (letters inside words count for it, symbols inside words and C1
control characters against it) and reported with whether its text differs from
the chosen one. Equal decodings are reported as equivalent, not as evidence.
A profile or request may name the encoding explicitly; the hypothesis is still
recorded so a disagreement is visible.
"""
from __future__ import annotations

import csv
import io
import re

ENCODINGS = ('utf-8', 'utf-8-sig', 'utf-16', 'cp1250', 'iso-8859-2', 'cp1252')
SINGLE_BYTE = ('cp1250', 'iso-8859-2', 'cp1252')
ALIASES = {
    'utf8': 'utf-8', 'utf-8': 'utf-8', 'utf_8': 'utf-8', 'utf-8-bom': 'utf-8-sig', 'utf-8-sig': 'utf-8-sig',
    'utf16': 'utf-16', 'utf-16': 'utf-16', 'cp1250': 'cp1250', 'windows-1250': 'cp1250', 'win1250': 'cp1250',
    'iso-8859-2': 'iso-8859-2', 'iso8859-2': 'iso-8859-2', 'latin2': 'iso-8859-2', 'latin-2': 'iso-8859-2',
    'cp1252': 'cp1252', 'windows-1252': 'cp1252', 'win1252': 'cp1252',
}
DELIMITERS = (';', ',', '\t', '|')


def canonical_encoding(name: str) -> str:
    key = str(name).strip().lower()
    if key not in ALIASES:
        raise ValueError(f'unsupported encoding {name!r}; supported: {", ".join(ENCODINGS)}')
    return ALIASES[key]


def _score(text: str) -> dict:
    letters = symbols = controls = 0
    for index, ch in enumerate(text):
        code = ord(ch)
        if code < 128:
            continue
        if 0x80 <= code <= 0x9F:
            controls += 1
            continue
        before = text[index - 1] if index else ' '
        after = text[index + 1] if index + 1 < len(text) else ' '
        if ch.isalpha():
            letters += 1
        elif before.isalpha() or after.isalpha():
            symbols += 1
    return {'letters': letters, 'symbols_in_words': symbols, 'c1_controls': controls,
            'score': letters - 3 * symbols - 5 * controls}


def encoding_hypotheses(raw: bytes, preference: tuple[str, ...] = SINGLE_BYTE) -> dict:
    """Ranked decoding hypotheses for ``raw``; see the module docstring."""
    if raw.startswith(b'\xef\xbb\xbf'):
        return {'chosen': 'utf-8-sig', 'basis': 'utf8_bom', 'ambiguous': False, 'candidates': []}
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return {'chosen': 'utf-16', 'basis': 'utf16_bom', 'ambiguous': False, 'candidates': []}
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        utf8_error = exc.start
    else:
        ascii_only = all(ord(ch) < 128 for ch in text)
        return {'chosen': 'utf-8', 'basis': 'ascii_only_all_candidates_equal' if ascii_only else 'valid_utf8_non_ascii',
                'ambiguous': False, 'candidates': []}
    order = [canonical_encoding(e) for e in preference if canonical_encoding(e) in SINGLE_BYTE]
    order += [e for e in SINGLE_BYTE if e not in order]
    decoded, candidates = {}, []
    for rank, name in enumerate(order):
        try:
            text = raw.decode(name)
        except UnicodeDecodeError as exc:
            candidates.append({'encoding': name, 'decodable': False, 'first_undecodable_byte': exc.start})
            continue
        decoded[name] = text
        candidates.append({'encoding': name, 'decodable': True, 'preference_rank': rank, **_score(text)})
    usable = [c for c in candidates if c['decodable']]
    if not usable:
        raise ValueError('bytes are neither UTF-8 nor decodable as CP1250/ISO-8859-2/CP1252')
    best = max(usable, key=lambda c: (c['score'], -c['preference_rank']))
    chosen = best['encoding']
    tied = [c['encoding'] for c in usable if c['score'] == best['score'] and decoded[c['encoding']] != decoded[chosen]]
    for c in candidates:
        if c['decodable']:
            c['same_text_as_chosen'] = decoded[c['encoding']] == decoded[chosen]
            if not c['same_text_as_chosen']:
                c['differing_characters'] = sum(1 for a, b in zip(decoded[c['encoding']], decoded[chosen]) if a != b)
    return {'chosen': chosen, 'basis': 'single_byte_plausibility_hypothesis' + ('_tie_broken_by_preference' if tied else ''),
            'ambiguous': bool(tied), 'tied_with': tied, 'utf8_invalid_at_byte': utf8_error, 'candidates': candidates}


def decode(raw: bytes, encoding: str = 'auto', preference: tuple[str, ...] = SINGLE_BYTE) -> tuple[str, dict]:
    """Decode with an explicit encoding or the best hypothesis; strict, never replacement characters."""
    hypotheses = encoding_hypotheses(raw, preference)
    if encoding in (None, '', 'auto'):
        used, basis = hypotheses['chosen'], 'hypothesis'
    else:
        used, basis = canonical_encoding(encoding), 'explicit'
    try:
        text = raw.decode(used)
    except UnicodeDecodeError as exc:
        raise ValueError(f'bytes are not valid {used} at offset {exc.start}') from exc
    bom = False
    if text.startswith('﻿'):
        text, bom = text[1:], True
    info = {'used': used, 'basis': basis, 'bom_removed': bom, 'hypothesis': hypotheses}
    if basis == 'explicit' and hypotheses['chosen'] not in (used, 'utf-8'):
        info['explicit_differs_from_hypothesis'] = hypotheses['chosen']
    return text, info


def physical_lines(text: str) -> list[str]:
    """Lines with their own terminators, split only at CR/LF/CRLF (as ``csv`` counts them)."""
    return list(io.StringIO(text, newline=''))


def parse_records(lines: list[str], *, start: int, delimiter: str, quotechar: str = '"', max_records: int,
                  max_raw_chars: int) -> dict:
    """Parse delimited records from ``lines[start:]``; malformed quoting stops with a recorded issue."""
    reader = csv.reader(iter(lines[start:]), delimiter=delimiter, quotechar=quotechar or None,
                        quoting=csv.QUOTE_MINIMAL if quotechar else csv.QUOTE_NONE, strict=True)
    records, previous, error, limited = [], 0, None, False
    try:
        for values in reader:
            raw = ''.join(lines[start + previous:start + reader.line_num])
            first_line = start + previous + 1
            last_line = start + reader.line_num
            previous = reader.line_num
            if len(records) >= max_records:
                limited = True
                break
            records.append({'index': len(records) + 1, 'start_line': first_line, 'end_line': last_line,
                            'values': values, 'raw': raw[:max_raw_chars], 'raw_complete': len(raw) <= max_raw_chars})
    except csv.Error as exc:
        fragment = ''.join(lines[start + previous:start + previous + 3])
        error = {'start_line': start + previous + 1, 'error': str(exc), 'raw_fragment': fragment[:max_raw_chars]}
    return {'records': records, 'error': error, 'limited': limited}


def blank(values: list[str]) -> bool:
    return all(not str(v).strip() for v in values)


def guess_delimiter(lines: list[str]) -> tuple[str, dict]:
    """Delimiter hypothesis from the most consistent per-line counts (no header knowledge)."""
    sample = [line for line in lines[:50] if line.strip()]
    best, details = ',', {}
    best_key = None
    for delimiter in DELIMITERS:
        counts = [line.count(delimiter) for line in sample]
        nonzero = [c for c in counts if c]
        if not nonzero:
            details[delimiter] = {'lines_with_delimiter': 0}
            continue
        mode = max(set(nonzero), key=nonzero.count)
        consistent = nonzero.count(mode)
        details[delimiter] = {'lines_with_delimiter': len(nonzero), 'modal_count': mode, 'lines_at_mode': consistent}
        key = (consistent, mode)
        if best_key is None or key > best_key:
            best, best_key = delimiter, key
    return best, details


def norm_text(value) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()
