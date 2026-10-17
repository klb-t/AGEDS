"""Bounded streaming XML: yields small subtrees, never the whole document.

Expat is driven directly so declarations can be refused through handlers
(whatever the document encoding). By default any DOCTYPE is refused; with
``allow_internal_dtd`` (Apple Health ``export.xml`` ships an internal subset of
ELEMENT/ATTLIST declarations) the DOCTYPE is accepted only without a SYSTEM or
PUBLIC identifier, and ENTITY/NOTATION declarations and external entity
references are still refused. ``specified_attributes`` is on, so default
attribute values from an ATTLIST never appear as if they were in the file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator
from xml.parsers import expat

CHUNK = 1024 * 1024


class XmlRefused(ValueError):
    """A declaration that this reader does not accept."""


@dataclass
class Node:
    tag: str
    attrib: dict
    ns: str | None = None
    text: str = ''
    children: list = field(default_factory=list)
    line: int | None = None
    truncated: bool = False

    def find(self, *path: str) -> 'Node | None':
        node = self
        for name in path:
            node = next((c for c in node.children if c.tag == name), None)
            if node is None:
                return None
        return node

    def findall(self, name: str) -> list['Node']:
        return [c for c in self.children if c.tag == name]

    def text_of(self, *path: str) -> str | None:
        node = self.find(*path)
        if node is None:
            return None
        text = node.text.strip()
        return text if text else None

    def as_dict(self, max_items: int = 200) -> dict:
        out: dict = {'tag': self.tag}
        if self.attrib:
            out['attrib'] = dict(self.attrib)
        if self.text.strip():
            out['text'] = self.text.strip()
        if self.children:
            out['children'] = [c.as_dict(max_items) for c in self.children[:max_items]]
            if len(self.children) > max_items:
                out['children_truncated'] = len(self.children) - max_items
        return out


def _split(name: str) -> tuple[str | None, str]:
    if ' ' in name:
        ns, local = name.rsplit(' ', 1)
        return ns, local
    return None, name


def stream(path: Path, want: Callable[[str, str | None], bool], *, allow_internal_dtd: bool = False,
           max_depth: int = 64, max_subtree_nodes: int = 20_000, max_text_chars: int = 65_536,
           markers: frozenset = frozenset()) -> Iterator[tuple]:
    """Yield ``('root', tag, ns)``, ``('start', tag, attrib)`` for marker tags, ``('end', tag)`` and
    ``('node', Node, parent_tag)`` for every wanted subtree (outermost only), in document order."""
    parser = expat.ParserCreate(namespace_separator=' ')
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.specified_attributes = True
    pending: list[tuple] = []
    stack: list[str] = []
    building: list[Node] = []
    state = {'root': None, 'nodes': 0}

    def refuse(*_args):
        raise XmlRefused('XML entity/notation declarations and external entities are not accepted')

    def doctype(_name, system_id, public_id, _has_internal):
        if not allow_internal_dtd or system_id or public_id:
            raise XmlRefused('XML document type declarations are not accepted here')

    def start(name, attributes):
        ns, tag = _split(name)
        if len(stack) >= max_depth:
            raise ValueError(f'XML nesting deeper than {max_depth}')
        parent = stack[-1] if stack else None
        if state['root'] is None:
            state['root'] = tag
            pending.append(('root', tag, ns))
        if tag in markers and not building:
            pending.append(('start', tag, dict(attributes)))
        if building:
            state['nodes'] += 1
            if state['nodes'] > max_subtree_nodes:
                building[0].truncated = True
                building.append(None)
            elif building[-1] is None:
                building.append(None)
            else:
                node = Node(tag, {(_split(k)[1]): v for k, v in attributes.items()}, ns, line=parser.CurrentLineNumber)
                building[-1].children.append(node)
                building.append(node)
        elif want(tag, parent):
            state['nodes'] = 1
            building.append(Node(tag, {(_split(k)[1]): v for k, v in attributes.items()}, ns,
                                 line=parser.CurrentLineNumber))
        stack.append(tag)

    def end(name):
        _, tag = _split(name)
        stack.pop()
        if building:
            node = building.pop()
            if not building:
                pending.append(('node', node, stack[-1] if stack else None))
            return
        if tag in markers:
            pending.append(('end', tag))

    def chars(data):
        if building and building[-1] is not None:
            node = building[-1]
            remaining = max_text_chars - len(node.text)
            if remaining > 0:
                node.text += data[:remaining]
            if len(data) > remaining:
                node.truncated = True

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.NotationDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    with path.open('rb') as handle:
        while True:
            chunk = handle.read(CHUNK)
            try:
                parser.Parse(chunk, not chunk)
            except XmlRefused:
                raise
            except expat.ExpatError as exc:
                raise ValueError(f'XML is not well-formed: {exc}') from exc
            for item in pending:
                yield item
            pending.clear()
            if not chunk:
                break
