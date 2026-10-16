"""Virtual locators for files and archive members.

Syntax (documented in docs/CATALOG.md)::

    locator   := disk_path ( "!/" member_path [ "!" occurrence ] )*
    disk_path := component ( "/" component )*     # relative to the scan root
    member_path := the member name inside the preceding container

Examples: ``Takeout/outer.zip!/inner/dir/file.txt`` and
``a.zip!/b.zip!/c.txt``. Each name is escaped byte-wise from its *raw* bytes:

* ``%`` becomes ``%25`` and every literal ``!`` becomes ``%21``; so an
  unescaped ``!`` is always structural and a name containing ``!/`` can never
  be mistaken for an archive boundary;
* bytes that are not valid UTF-8, C0/C1 controls, DEL and invisible format
  characters (Unicode categories Cc/Cf/Zl/Zp, e.g. bidi overrides) become
  ``%XX`` per UTF-8 byte, so a locator always displays what it addresses;
* everything else (including ``/``, ``\\``, ``..`` and spaces) is kept as is.

``!N`` (N >= 2, decimal) after a member path marks the N-th member with the same
raw name inside the same container (duplicates are data, not errors).

A locator is an inert identifier: it is never used as a filesystem path. Member
names such as ``../x`` or ``/etc/x`` stay names.
"""
from __future__ import annotations

import re
import unicodedata

_HEX = '0123456789ABCDEF'


def escape_name(raw: bytes) -> str:
    """Escape raw name bytes into a locator segment (see module docstring)."""
    out: list[str] = []
    index = 0
    length = len(raw)
    while index < length:
        byte = raw[index]
        if byte < 0x80:
            char = chr(byte)
            if byte < 0x20 or byte == 0x7F or char in '%!':
                out.append('%' + _HEX[byte >> 4] + _HEX[byte & 15])
            else:
                out.append(char)
            index += 1
            continue
        width = 2 if 0xC2 <= byte <= 0xDF else 3 if 0xE0 <= byte <= 0xEF else 4 if 0xF0 <= byte <= 0xF4 else 0
        if width and index + width <= length:
            chunk = raw[index:index + width]
            try:
                char = chunk.decode('utf-8')
            except UnicodeDecodeError:
                char = None
            if char is not None:
                if unicodedata.category(char) in ('Cc', 'Cf', 'Zl', 'Zp'):
                    out.extend('%' + _HEX[b >> 4] + _HEX[b & 15] for b in chunk)
                else:
                    out.append(char)
                index += width
                continue
        out.append('%' + _HEX[byte >> 4] + _HEX[byte & 15])
        index += 1
    return ''.join(out)


def unescape_name(segment: str) -> bytes:
    """Inverse of :func:`escape_name`."""
    out = bytearray()
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == '%':
            pair = segment[index + 1:index + 3]
            if len(pair) != 2 or not re.fullmatch(r'[0-9A-Fa-f]{2}', pair):
                raise ValueError(f'invalid escape in locator segment at {index}')
            out.append(int(pair, 16))
            index += 3
            continue
        if char == '!':
            raise ValueError('unescaped "!" inside a locator segment')
        out.extend(char.encode('utf-8'))
        index += 1
    return bytes(out)


def disk_locator(relative_raw_parts: list[bytes]) -> str:
    return '/'.join(escape_name(part) for part in relative_raw_parts)


def member_locator(container_locator: str, member_raw: bytes, occurrence: int = 1) -> str:
    if occurrence < 1:
        raise ValueError('occurrence starts at 1')
    suffix = f'!{occurrence}' if occurrence > 1 else ''
    return f'{container_locator}!/{escape_name(member_raw)}{suffix}'


def parse_locator(locator: str) -> dict:
    """Split a locator into the disk path and the chain of archive members.

    Returns ``{'disk': [raw component bytes...], 'members': [{'name': raw, 'occurrence': n}, ...]}``.
    """
    tokens = locator.split('!')
    disk = tokens[0]
    if not disk:
        raise ValueError('locator has no disk path')
    members: list[dict] = []
    for token in tokens[1:]:
        if token.startswith('/'):
            members.append({'name': unescape_name(token[1:]), 'occurrence': 1})
        elif re.fullmatch(r'[1-9][0-9]*', token) and members and members[-1]['occurrence'] == 1 and int(token) >= 2:
            members[-1]['occurrence'] = int(token)
        else:
            raise ValueError(f'invalid locator token: {token!r}')
    return {'disk': [unescape_name(part) for part in disk.split('/')], 'members': members}


def basename(raw: bytes) -> bytes:
    """Last non-empty path element of a raw member name, using '/' only.

    Backslashes are data (they are not separators inside archives written on
    POSIX tools); they are flagged elsewhere instead of interpreted.
    """
    stripped = raw.rstrip(b'/')
    return stripped.rsplit(b'/', 1)[-1] if stripped else raw
