"""Bounded streaming reader for large JSON exports (Records.json can exceed 1 GB).

The reader never holds the whole document. It walks the top level only:

* a top-level **array** yields ``('$', index, value)`` per element;
* a top-level **object** yields ``(key, index, value)`` per element of every member
  whose value is an array, and ``(key, None, value)`` for every other member.

Each element is decoded by the C JSON decoder (``JSONDecoder.raw_decode``) from a
buffer holding at most ``chunk_bytes + max_element_bytes`` characters, so memory is
bounded by the largest element, not by the file. An element larger than
``max_element_bytes`` is skipped with a structural scan (strings and nesting
tracked byte-exactly) and reported; a malformed element is skipped the same way
when its end can be found. Truncation (EOF inside the structure) is reported as
``truncated``. Floats keep their source spelling (``PreciseFloat.text``) so the
caller can know the stated precision; ``NaN``/``Infinity`` literals decode to
non-finite floats that callers must refuse.
"""
from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass, field

DEFAULT_CHUNK = 1024 * 1024
DEFAULT_MAX_ELEMENT = 64 * 1024 * 1024
_WS = ' \t\r\n'
_NUMBER_TAIL = re.compile(r'[0-9.eE+-]*')


class PreciseFloat(float):
    """A float that remembers the text it was parsed from."""

    def __new__(cls, text: str):
        value = super().__new__(cls, text)
        value.text = text
        return value


def _constant(name: str) -> float:
    return PreciseFloat({'NaN': 'nan', 'Infinity': 'inf', '-Infinity': '-inf'}[name])


_DECODER = json.JSONDecoder(parse_float=PreciseFloat, parse_constant=_constant)


@dataclass
class StreamReport:
    """What the reader saw; ``complete`` is False when anything was skipped or cut."""
    top: str | None = None
    bytes_read: int = 0
    elements: int = 0
    skipped_large: int = 0
    skipped_malformed: int = 0
    truncated: bool = False
    trailing_data: bool = False
    not_json: bool = False
    issues: list = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not (self.skipped_large or self.skipped_malformed or self.truncated or self.not_json)

    def as_dict(self) -> dict:
        return {'top': self.top, 'bytes_read': self.bytes_read, 'elements': self.elements,
                'skipped_large': self.skipped_large, 'skipped_malformed': self.skipped_malformed,
                'truncated': self.truncated, 'trailing_data': self.trailing_data, 'not_json': self.not_json,
                'complete': self.complete}


class _Source:
    def __init__(self, handle, chunk: int, report: StreamReport):
        self.handle, self.chunk, self.report = handle, chunk, report
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.buf = ''
        self.pos = 0
        self.eof = False
        self.first = True

    def more(self) -> bool:
        if self.eof:
            return False
        data = self.handle.read(self.chunk)
        if not data:
            self.eof = True
            tail = self.decoder.decode(b'', final=True)
            if tail:
                self._append(tail)
                return True
            return False
        self.report.bytes_read += len(data)
        text = self.decoder.decode(data)
        if self.first:
            self.first = False
            text = text.lstrip('﻿')
        self._append(text)
        return True

    def _append(self, text: str) -> None:
        if self.pos > self.chunk:
            self.buf = self.buf[self.pos:]
            self.pos = 0
        self.buf += text

    def peek(self) -> str | None:
        """Next non-whitespace character without consuming it (None at EOF)."""
        while True:
            while self.pos < len(self.buf) and self.buf[self.pos] in _WS:
                self.pos += 1
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self.more():
                return None

    def take(self) -> str | None:
        ch = self.peek()
        if ch is not None:
            self.pos += 1
        return ch

    def value(self, max_chars: int):
        """Decode one value at the cursor; returns (ok, value, reason)."""
        self.peek()
        while True:
            try:
                value, end = _DECODER.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError:
                if len(self.buf) - self.pos > max_chars:
                    return False, None, 'element_too_large'
                if self.more():
                    continue
                return False, None, 'malformed_or_truncated'
            except RecursionError:
                return False, None, 'nesting_too_deep'
            if end - self.pos > max_chars:
                self.pos = end
                return False, None, 'element_too_large'
            # A number cut by the chunk boundary ("1" of "1.50", "1." of "1.5e3") may continue in the
            # next chunk: read more until a delimiter follows or the decoded end stops moving.
            if isinstance(value, (int, float)) and not isinstance(value, bool) and not self.eof and \
                    _NUMBER_TAIL.match(self.buf, end).end() == len(self.buf):
                if self.more():
                    continue
            self.pos = end
            return True, value, None

    def skip_value(self) -> bool:
        """Structural skip of one value (strings, escapes, nesting); False when EOF came first."""
        depth, in_string, escape, started = 0, False, False, False
        while True:
            if self.pos >= len(self.buf) and not self.more():
                return False
            ch = self.buf[self.pos]
            self.pos += 1
            if in_string:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == '"':
                    in_string = False
                    if depth == 0:
                        return True
                continue
            if ch in _WS and not started:
                continue
            started = True
            if ch == '"':
                in_string = True
            elif ch in '{[':
                depth += 1
            elif ch in '}]':
                depth -= 1
                if depth <= 0:
                    return True
            elif depth == 0 and ch in ',':
                self.pos -= 1
                return True
            elif depth == 0:
                # a scalar: consume until a delimiter
                while True:
                    if self.pos >= len(self.buf) and not self.more():
                        return True
                    if self.buf[self.pos] in ',]}' + _WS:
                        return True
                    self.pos += 1


def iter_document(handle, *, max_element_bytes: int = DEFAULT_MAX_ELEMENT, chunk_bytes: int = DEFAULT_CHUNK,
                  report: StreamReport | None = None, array_keys: tuple[str, ...] | None = None):
    """Yield ``(key, index, value)``; see the module docstring. ``report`` collects what happened.

    ``array_keys`` limits which top-level arrays are streamed element by element;
    other members are decoded whole (bounded) or skipped when too large.
    """
    report = report if report is not None else StreamReport()
    src = _Source(handle, chunk_bytes, report)
    first = src.take()
    if first == '[':
        report.top = 'array'
        yield from _array(src, '$', max_element_bytes, report)
    elif first == '{':
        report.top = 'object'
        yield from _object(src, max_element_bytes, report, array_keys)
    else:
        report.not_json = True
        report.issues.append({'code': 'not_a_json_document', 'first_char': first})
        return
    if not report.truncated and src.peek() is not None:
        report.trailing_data = True
        report.issues.append({'code': 'trailing_data_after_document'})


def _array(src: _Source, key: str, max_chars: int, report: StreamReport):
    index = 0
    if src.peek() == ']':
        src.take()
        return
    while True:
        ch = src.peek()
        if ch is None:
            report.truncated = True
            report.issues.append({'code': 'truncated', 'section': key, 'index': index})
            return
        ok, value, reason = src.value(max_chars)
        if ok:
            report.elements += 1
            yield key, index, value
        else:
            if reason == 'element_too_large':
                report.skipped_large += 1
            else:
                report.skipped_malformed += 1
            report.issues.append({'code': f'element_{reason}', 'section': key, 'index': index})
            if not src.skip_value() and src.peek() is None:
                report.truncated = True
                report.issues.append({'code': 'truncated', 'section': key, 'index': index})
                return
        index += 1
        sep = src.take()
        if sep == ',':
            continue
        if sep == ']':
            return
        if sep is None:
            report.truncated = True
            report.issues.append({'code': 'truncated', 'section': key, 'index': index})
            return
        report.skipped_malformed += 1
        report.issues.append({'code': 'unexpected_character', 'section': key, 'index': index, 'char': sep})
        # resynchronise: skip to the next separator at this level
        src.pos -= 1
        if not src.skip_value():
            report.truncated = True
            return
        nxt = src.take()
        if nxt == ']':
            return
        if nxt != ',':
            report.truncated = True
            return


def _object(src: _Source, max_chars: int, report: StreamReport, array_keys):
    if src.peek() == '}':
        src.take()
        return
    while True:
        if src.peek() != '"':
            if src.peek() is None:
                report.truncated = True
                report.issues.append({'code': 'truncated', 'section': '$'})
            else:
                report.not_json = True
                report.issues.append({'code': 'object_key_expected'})
            return
        ok, key, _ = src.value(64 * 1024)
        if not ok or not isinstance(key, str):
            report.truncated = src.peek() is None
            report.not_json = not report.truncated
            report.issues.append({'code': 'object_key_unreadable'})
            return
        if src.take() != ':':
            report.not_json = True
            report.issues.append({'code': 'colon_expected', 'key': key[:64]})
            return
        ch = src.peek()
        if ch is None:
            report.truncated = True
            report.issues.append({'code': 'truncated', 'section': key[:64]})
            return
        if ch == '[' and (array_keys is None or key in array_keys):
            src.take()
            yield from _array(src, key, max_chars, report)
            if report.truncated:
                return
        else:
            ok, value, reason = src.value(max_chars)
            if ok:
                report.elements += 1
                yield key, None, value
            else:
                if reason == 'element_too_large':
                    report.skipped_large += 1
                else:
                    report.skipped_malformed += 1
                report.issues.append({'code': f'member_{reason}', 'section': key[:64]})
                if not src.skip_value():
                    report.truncated = True
                    return
        sep = src.take()
        if sep == ',':
            continue
        if sep == '}':
            return
        if sep is None:
            report.truncated = True
            report.issues.append({'code': 'truncated', 'section': key[:64]})
            return
        report.not_json = True
        report.issues.append({'code': 'unexpected_character', 'section': key[:64], 'char': sep})
        return
