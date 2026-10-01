"""Local resource accounting for interpreted metadata, not an evidence ontology."""
from __future__ import annotations

import json


class VerificationLimitError(ValueError):
    """Verification exhausted an explicit, sticky resource budget."""


class VerificationBudget:
    def __init__(self, *, max_nodes=2_000_000, max_depth=64,
                 max_projection_visits=2_000_000, max_json_bytes=32 * 1024 * 1024):
        for name, value in locals().copy().items():
            if name != 'self' and (type(value) is not int or value < 1):
                raise ValueError('Verification limits must be positive integers')
        self.max_nodes, self.max_depth = max_nodes, max_depth
        self.max_projection_visits, self.max_json_bytes = max_projection_visits, max_json_bytes
        self.nodes = 0
        self.projection_visits = 0
        self.exhausted = False

    def _active(self):
        if self.exhausted:
            raise VerificationLimitError('verification budget already exhausted')

    def _fail(self, message):
        self.exhausted = True
        raise VerificationLimitError(message)

    def _node(self, depth):
        self._active()
        if depth > self.max_depth:
            self._fail('semantic JSON depth limit exceeded')
        self.nodes += 1
        if self.nodes > self.max_nodes:
            self._fail('semantic JSON node limit exceeded')

    def check_structure(self, value, *, start_depth=0):
        """Count values/containers, not object keys; root has depth zero.

        Iterator DFS retains depth-sized state, never a sibling-sized work list.
        """
        self._active()
        stack = [(iter((value,)), start_depth)]
        while stack:
            iterator, depth = stack[-1]
            try:
                node = next(iterator)
            except StopIteration:
                stack.pop()
                continue
            self._node(depth)
            if isinstance(node, dict):
                stack.append((iter(node.values()), depth + 1))
            elif isinstance(node, list):
                stack.append((iter(node), depth + 1))

    def preflight_json(self, raw, *, start_depth=0):
        """Bound UTF-8 bytes, structural depth and token nodes BEFORE json.loads.

        This lexical pass is not a replacement JSON parser. It counts valid JSON
        nodes exactly under ArchiveLimits' values/containers convention; malformed
        grammar is rejected by the strict decoder after this bounded preflight.
        """
        self._active()
        if not isinstance(raw, (str, bytes)):
            raise ValueError('stored JSON must be UTF-8 text or bytes')
        if len(raw) > self.max_json_bytes:
            self._fail('semantic JSON byte limit exceeded')
        encoded = raw.encode('utf-8') if isinstance(raw, str) else raw
        if len(encoded) > self.max_json_bytes:
            self._fail('semantic JSON byte limit exceeded')
        text = encoded.decode('utf-8')
        position, depth, size = 0, start_depth, len(text)
        while position < size:
            char = text[position]
            if char.isspace() or char in ',:':
                position += 1
            elif char in '[{':
                self._node(depth)
                depth += 1
                position += 1
            elif char in ']}':
                depth -= 1
                position += 1
            elif char == '"':
                position += 1
                while position < size:
                    if text[position] == '\\':
                        position += 2
                    elif text[position] == '"':
                        position += 1
                        break
                    else:
                        position += 1
                following = position
                while following < size and text[following].isspace():
                    following += 1
                if following == size or text[following] != ':':
                    self._node(depth)
            else:
                self._node(depth)
                position += 1
                while position < size and not text[position].isspace() and text[position] not in ',:{}[]"':
                    position += 1

    def decode_json(self, raw, *, finite=True, start_depth=0):
        self.preflight_json(raw, start_depth=start_depth)
        def pairs(items):
            value = {}
            for key, child in items:
                if key in value:
                    raise ValueError('Duplicate stored JSON key')
                value[key] = child
            return value
        text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        value = json.loads(text, object_pairs_hook=pairs)
        if finite:
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return value

    def charge_projection(self, visits):
        self._active()
        if type(visits) is not int or visits < 0:
            raise ValueError('projection charge must be a nonnegative integer')
        self.projection_visits += visits
        if self.projection_visits > self.max_projection_visits:
            self._fail('citation projection work limit exceeded')
