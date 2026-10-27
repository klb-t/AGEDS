"""Bounded, entity-free streaming XML events for GPX and KML (pyexpat, chunked).

Yields ``('start', name, attrs, depth)``, ``('text', name, chunk, depth)`` and
``('end', name, depth)`` where ``name`` is the local element name prefixed by a
short namespace tag for the Google KML extension (``gx:Track``, ``gx:coord``).
Character data arrives in chunks so a huge ``<coordinates>`` element never has to
be held whole. A document type declaration (and therefore any entity
declaration) is refused: GPX and KML never need one, and refusing it removes
entity-expansion attacks. Malformed or truncated XML stops the stream and is
reported through ``XmlReport``.
"""
from __future__ import annotations

from dataclasses import dataclass
from xml.parsers import expat

CHUNK = 256 * 1024
GX_NAMESPACES = ('http://www.google.com/kml/ext/2.2',)


class DoctypeRefused(ValueError):
    pass


@dataclass
class XmlReport:
    bytes_read: int = 0
    error: str | None = None
    error_line: int | None = None
    truncated: bool = False
    doctype_refused: bool = False
    root: str | None = None
    root_namespace: str | None = None

    @property
    def complete(self) -> bool:
        return self.error is None and not self.truncated and not self.doctype_refused

    def as_dict(self) -> dict:
        return {'bytes_read': self.bytes_read, 'error': self.error, 'error_line': self.error_line,
                'truncated': self.truncated, 'doctype_refused': self.doctype_refused, 'root': self.root,
                'root_namespace': self.root_namespace, 'complete': self.complete}


def _local(name: str) -> tuple[str, str | None]:
    if ' ' in name:
        uri, local = name.split(' ', 1)
        return (f'gx:{local}' if uri in GX_NAMESPACES else local), uri
    return name.split(':')[-1], None


def iter_events(handle, report: XmlReport, *, chunk: int = CHUNK):
    parser = expat.ParserCreate(namespace_separator=' ')
    parser.buffer_text = True
    events: list = []
    depth = [0]
    stack: list[str] = []

    def doctype(*_args):
        report.doctype_refused = True
        raise DoctypeRefused('document type declarations are refused (GPX/KML need none)')

    def entity(*_args):
        report.doctype_refused = True
        raise DoctypeRefused('entity declarations are refused')

    def start(name, attrs):
        local, uri = _local(name)
        if not stack:
            report.root, report.root_namespace = local, uri
        stack.append(local)
        depth[0] += 1
        events.append(('start', local, {_local(k)[0]: v for k, v in attrs.items()}, depth[0]))

    def end(name):
        local = stack.pop() if stack else _local(name)[0]
        events.append(('end', local, depth[0]))
        depth[0] -= 1

    def text(data):
        if stack:
            events.append(('text', stack[-1], data, depth[0]))

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = entity
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    try:
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    except AttributeError:  # pragma: no cover - very old pyexpat
        pass
    while True:
        data = handle.read(chunk)
        final = not data
        report.bytes_read += len(data)
        try:
            parser.Parse(data, final)
        except DoctypeRefused as exc:
            report.error = str(exc)
            yield from events
            return
        except expat.ExpatError as exc:
            report.error = expat.ErrorString(exc.code)
            report.error_line = exc.lineno
            report.truncated = exc.code in (expat.errors.codes.get(expat.errors.XML_ERROR_NO_ELEMENTS),
                                            expat.errors.codes.get(expat.errors.XML_ERROR_UNCLOSED_TOKEN)) or \
                bool(final and stack)
            yield from events
            return
        yield from events
        events.clear()
        if final:
            return
