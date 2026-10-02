#!/usr/bin/env python3
"""Standalone, standard-library-only inspector for AGEDS citation packets.

No imports from AGEDS and no dereferencing of packet locators. All verification
is internal consistency of supplied metadata, never source authenticity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import unicodedata

SCHEMA = 'ageds.citation-evidence/v1'
CANONICALIZATION = 'python-json-sorted-keys-compact-utf8-no-nan/v1'
MAX_BYTES = 2_097_152
MAX_DEPTH = 32
MAX_NODES = 100_000
MAX_INT = 2**63 - 1


class InspectionError(ValueError):
    """The supplied packet cannot be verified under this contract."""


def require(condition, message):
    if not condition:
        raise InspectionError(message)


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise InspectionError('value is not canonical UTF-8 JSON') from error


class Decoder:
    def __init__(self):
        self.remaining = MAX_NODES

    def decode(self, raw, *, legacy_segments=False):
        require(isinstance(raw, (str, bytes)), 'JSON input must be bytes or text')
        try:
            encoded = raw.encode('utf-8') if isinstance(raw, str) else raw
            require(len(encoded) <= MAX_BYTES, 'packet exceeds byte limit')
            text = encoded.decode('utf-8', errors='strict')
            # Check depth before invoking the recursive standard JSON decoder.
            depth = 0
            quoted = escaped = False
            for char in text:
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == '\\':
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in '[{':
                    depth += 1
                    require(depth <= MAX_DEPTH, 'JSON exceeds depth limit')
                elif char in ']}':
                    depth -= 1
            def pairs(items):
                result = {}
                for key, value in items:
                    require(key not in result, 'duplicate JSON key')
                    result[key] = value
                return result
            def constant(token):
                if legacy_segments:
                    return {'NaN': float('nan'), 'Infinity': float('inf'), '-Infinity': -float('inf')}[token]
                raise InspectionError('nonfinite JSON number')
            value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
            stack = [value]
            while stack:
                item = stack.pop()
                self.remaining -= 1
                require(self.remaining >= 0, 'JSON exceeds node limit')
                if isinstance(item, dict):
                    stack.extend(item.keys())
                    stack.extend(item.values())
                elif isinstance(item, list):
                    stack.extend(item)
                elif isinstance(item, str):
                    item.encode('utf-8', errors='strict')
                elif type(item) is float and not legacy_segments:
                    require(math.isfinite(item), 'nonfinite JSON number')
            return value
        except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError) as error:
            raise InspectionError('invalid strict UTF-8 JSON') from error


def fields(value, names, label):
    require(type(value) is dict and set(value) == set(names.split()), label + ' fields differ from v1')


def integer(value, label, minimum=0):
    require(type(value) is int and minimum <= value <= MAX_INT, label + ' must be an integer')


def digest(value, label):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None,
            label + ' must be lowercase SHA-256')


def milliseconds(value):
    require(type(value) in (int, float) and 0 <= value <= MAX_INT / 1000,
            'invalid time in seconds')
    result = round(value * 1000)
    require(result <= MAX_INT, 'time exceeds millisecond limit')
    return result


def interval(value, text_key):
    require(type(value) is dict and isinstance(value.get(text_key), str)
            and value[text_key].strip(), 'selected text is missing or blank')
    milliseconds(value.get('start'))
    milliseconds(value.get('end'))
    require(value['end'] >= value['start'], 'reversed time interval')


def projection(segments, selector):
    require(type(segments) is list and len(segments) <= 10_000, 'invalid transcript segments')
    require(type(selector) is dict, 'selector must be an object')
    common = {'text_join': 'concatenate_exact', 'time_unit': 'seconds',
              'stored_time_unit': 'milliseconds', 'rounding': 'nearest_ms'}
    selected = []
    if selector.get('kind') == 'segments':
        indices = selector.get('indices')
        require(type(indices) is list and indices, 'empty segment selection')
        previous = None
        for index in indices:
            integer(index, 'segment index')
            require(index < len(segments), 'segment index outside transcript')
            require(previous is None or index == previous + 1, 'segment indices must be contiguous')
            segment = segments[index]
            interval(segment, 'text')
            require(not selected or segment['start'] >= selected[-1]['end'], 'overlapping segments')
            selected.append(segment)
            previous = index
        expected = dict(common, kind='segments', indices=indices, precision='segment')
        quote = ''.join(item['text'] for item in selected)
    elif selector.get('kind') == 'words':
        refs = selector.get('word_refs')
        require(type(refs) is list and 0 < len(refs) <= 10_000, 'invalid word reference count')
        checked = {}
        previous = None
        for ref in refs:
            fields(ref, 'segment_index word_index', 'word reference')
            si, wi = ref['segment_index'], ref['word_index']
            integer(si, 'segment index')
            integer(wi, 'word index')
            require(si < len(segments), 'word segment outside transcript')
            if si not in checked:
                segment = segments[si]
                interval(segment, 'text')
                words = segment.get('words')
                require(type(words) is list and 0 < len(words) <= 100_000, 'missing stored words')
                end = segment['start']
                for word in words:
                    interval(word, 'word')
                    require(word['start'] >= end and word['end'] <= segment['end'], 'invalid word interval')
                    end = word['end']
                require(''.join(word['word'] for word in words) == segment['text'], 'word text differs from segment')
                checked[si] = words
            require(wi < len(checked[si]), 'word index outside segment')
            if previous is not None:
                ps, pw = previous
                require((si == ps and wi == pw + 1) or
                        (si == ps + 1 and wi == 0 and pw == len(checked[ps]) - 1), 'noncontiguous words')
                require(si == ps or segments[si]['start'] >= segments[ps]['end'], 'overlapping word segments')
            selected.append(checked[si][wi])
            previous = si, wi
        expected = dict(common, kind='words', word_refs=refs, precision='word_asr',
                        source_start=selected[0]['start'], source_end=selected[-1]['end'],
                        alignment_verification='not_performed')
        quote = ''.join(item['word'] for item in selected)
    else:
        raise InspectionError('unsupported selector kind')
    require(canonical(selector) == canonical(expected), 'selector is not the exact canonical projection')
    return {'quote_text': quote, 'quote_sha256': hashlib.sha256(quote.encode('utf-8')).hexdigest(),
            'start_ms': milliseconds(selected[0]['start']), 'end_ms': milliseconds(selected[-1]['end']),
            'selector': expected}


def inspect_packet(raw):
    decoder = Decoder()
    packet = decoder.decode(raw)
    fields(packet, 'schema payload integrity', 'envelope')
    require(packet['schema'] == SCHEMA, 'unsupported packet schema')
    integrity = packet['integrity']
    fields(integrity, 'algorithm canonicalization domain payload_sha256', 'integrity')
    require(integrity['algorithm'] == 'sha256' and integrity['canonicalization'] == CANONICALIZATION
            and integrity['domain'] == SCHEMA + '\n', 'unsupported integrity contract')
    digest(integrity['payload_sha256'], 'payload digest')
    payload = packet['payload']
    fields(payload, 'artifact source source_observations processing_run derived_text anchor projection unknowns scope', 'payload')
    require(len(canonical(packet)) <= MAX_BYTES, 'canonical packet exceeds byte limit')
    actual = hashlib.sha256((SCHEMA + '\n').encode() + canonical(payload)).hexdigest()
    require(actual == integrity['payload_sha256'], 'payload digest mismatch')
    scope = payload['scope']
    fields(scope, 'source_bytes_included media_bytes_included live_restore_supported tasks_imported locators stored_path_included', 'scope')
    for key in ('source_bytes_included', 'media_bytes_included', 'live_restore_supported', 'tasks_imported'):
        require(scope[key] is False, 'unsupported scope claim')
    require(scope['locators'] == 'literal_inert_metadata' and type(scope['stored_path_included']) is bool, 'invalid locator scope')
    artifact, source = payload['artifact'], payload['source']
    version, anchor, run = payload['derived_text'], payload['anchor'], payload['processing_run']
    for value, label in ((artifact, 'artifact'), (version, 'derived text'), (anchor, 'anchor')):
        require(type(value) is dict, label + ' must be an object')
        integer(value.get('id'), label + ' id', 1)
    aid, vid = artifact['id'], version['id']
    for value in (version, anchor):
        integer(value.get('artifact_id'), 'artifact reference', 1)
        require(value['artifact_id'] == aid, 'artifact reference mismatch')
    integer(anchor.get('derived_text_id'), 'derived reference', 1)
    require(anchor['derived_text_id'] == vid and version.get('kind') == 'transcript', 'pinned version mismatch')
    require(isinstance(version.get('text'), str), 'transcript text must be literal text')
    require(('stored_path' in artifact) == scope['stored_path_included'], 'stored path scope mismatch')
    if artifact.get('sha256') is not None:
        digest(artifact['sha256'], 'source content digest')
    if artifact.get('size_bytes') is not None:
        integer(artifact['size_bytes'], 'source size')
    if source is None:
        require(artifact.get('source_id') is None, 'missing source identity')
    else:
        require(type(source) is dict, 'source must be object or null')
        integer(source.get('id'), 'source id', 1)
        integer(artifact.get('source_id'), 'source reference', 1)
        require(source['id'] == artifact['source_id'], 'source reference mismatch')
    observations = payload['source_observations']
    require(type(observations) is list and len(observations) <= 1000, 'invalid acquisition observations')
    seen = set()
    for observation in observations:
        require(type(observation) is dict, 'acquisition observation must be object')
        integer(observation.get('id'), 'observation id', 1)
        integer(observation.get('artifact_id'), 'observation artifact id', 1)
        require(observation['id'] not in seen and observation['artifact_id'] == aid, 'observation identity mismatch')
        seen.add(observation['id'])
        if observation.get('source_id') is not None:
            integer(observation['source_id'], 'observation source id', 1)
        for key in ('sha256', 'size_bytes'):
            if observation.get(key) is not None:
                (digest if key == 'sha256' else integer)(observation[key], 'observation ' + key)
                require(artifact.get(key) is None or observation[key] == artifact[key], 'acquisition content mismatch')
    if run is None:
        require(version.get('run_id') is None, 'missing processing run')
    else:
        require(type(run) is dict and 'lease_token' not in run, 'invalid processing run')
        integer(run.get('id'), 'run id', 1)
        integer(version.get('run_id'), 'run reference', 1)
        integer(run.get('artifact_id'), 'run artifact reference', 1)
        require(run['id'] == version['run_id'] and run['artifact_id'] == aid, 'processing run mismatch')
    segments = decoder.decode(version.get('segments_json'), legacy_segments=True)
    selector = decoder.decode(anchor.get('selector_json'))
    rebuilt = projection(segments, selector)
    require(canonical(payload['projection']) == canonical(rebuilt), 'declared projection mismatch')
    for key in ('quote_text', 'quote_sha256', 'start_ms', 'end_ms'):
        require(key in anchor and canonical(anchor[key]) == canonical(rebuilt[key]), 'anchor projection mismatch: ' + key)
    unknowns = payload['unknowns']
    require(type(unknowns) is list and all(isinstance(item, str) for item in unknowns), 'unknowns must be strings')
    missing = []
    for key in ('sha256', 'size_bytes'):
        if artifact.get(key) is None:
            missing.append('artifact.' + key)
    if source is None:
        missing.append('source')
    if not observations:
        missing.append('acquisition_history')
    if any(item.get('acquisition_kind') == 'legacy_snapshot' for item in observations):
        missing.append('complete_acquisition_history')
    if run is None:
        missing.append('processing_run')
    else:
        for key in ('tool', 'tool_version', 'provider', 'model', 'model_version'):
            if run.get(key) in (None, '', 'unknown'):
                missing.append('processing_run.' + key)
    return {'schema': 'ageds.citation-inspection/v1', 'verification': 'matches_included_pinned_transcript',
            'payload_sha256': actual, 'artifact_id': aid, 'derived_text_id': vid, 'anchor_id': anchor['id'],
            'quote_text': rebuilt['quote_text'], 'quote_sha256': rebuilt['quote_sha256'],
            'selector': rebuilt['selector'], 'start_ms': rebuilt['start_ms'], 'end_ms': rebuilt['end_ms'],
            'origins': {'artifact': artifact, 'source': source, 'source_observations': observations, 'processing_run': run},
            'declared_unknowns': unknowns,
            'observed_missing_provenance': missing,
            'provenance_warnings': (['Pinned processing run does not declare done status.']
                                    if run is not None and run.get('status') != 'done' else []),
            'limitations': ['Internal metadata consistency only; unsigned and not authenticated.',
                            'Source/media bytes absent; content digest is a declaration, not rechecked bytes.',
                            'No truth, authorship, acoustic alignment or listening verification.',
                            'All source locators are inert; no source access, restore or task execution.',
                            'Producer unknowns are declarations; missing provenance is not reconstructed.',
                            'Unused raw transcript fields may be malformed/nonfinite; only selected projection is validated.',
                            'No adapter to another project has been exercised.']}


def report_json(report):
    """JSON data, with terminal controls/bidi and HTML delimiters escaped.

    Decoding restores exact literal Unicode; this is never an HTML renderer.
    """
    text = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)
    return ''.join('\\u%04x' % ord(char) if char in '<>&' or
                   (unicodedata.category(char) in ('Cf', 'Cc', 'Zl', 'Zp') and char not in '\n\t')
                   else char for char in text)


def inspect_path(path):
    # The only file opened is the explicitly supplied packet path, never fields.
    require(hasattr(os, 'O_NOFOLLOW'), 'safe no-follow file reading unavailable')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode), 'packet must be a regular file')
        require(info.st_size <= MAX_BYTES, 'packet exceeds byte limit')
        raw = stream.read(MAX_BYTES + 1)
    return inspect_packet(raw)


inspect_packet_bytes = inspect_packet
inspect_packet_file = inspect_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packet', help='explicit path of UTF-8 citation packet')
    args = parser.parse_args(argv)
    try:
        print(report_json(inspect_path(args.packet)))
    except (OSError, ValueError) as error:
        print('Packet inspection failed: ' + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
