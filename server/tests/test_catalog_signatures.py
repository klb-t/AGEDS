"""Signature table, sniffing, name/byte relation and virtual locator escaping."""
from __future__ import annotations

import json
import random
import struct

import pytest

from server.app.catalog import archives, signatures
from server.app.catalog.locator import escape_name, member_locator, parse_locator, unescape_name
from server.tests.corpus.catalog_files import _cfb

SAMPLES = {
    'pdf': b'%PDF-1.4\n', 'png': b'\x89PNG\r\n\x1a\n' + b'\0' * 8, 'jpg': b'\xff\xd8\xff\xe0' + b'\0' * 8,
    'gif': b'GIF87a', 'wav': b'RIFF\0\0\0\0WAVEfmt ', 'webp': b'RIFF\0\0\0\0WEBPVP8 ', 'avi': b'RIFF\0\0\0\0AVI LIST',
    'mp4': struct.pack('>I', 20) + b'ftypmp42\0\0\0\0mp42', 'm4a': struct.pack('>I', 16) + b'ftypM4A \0\0\0\0',
    'mov': struct.pack('>I', 16) + b'ftypqt  \0\0\0\0', '3gp': struct.pack('>I', 16) + b'ftyp3gp5\0\0\0\0',
    'heic': struct.pack('>I', 20) + b'ftypheic\0\0\0\0mif1', 'avif': struct.pack('>I', 16) + b'ftypavif\0\0\0\0',
    'ogg': b'OggS' + b'\0' * 24 + b'\x01vorbis', 'opus': b'OggS' + b'\0' * 24 + b'OpusHead', 'flac': b'fLaC',
    'amr': b'#!AMR-WB\n', 'mkv': b'\x1a\x45\xdf\xa3\x93\x42\x82\x88matroska', 'webm': b'\x1a\x45\xdf\xa3\x42\x82\x84webm',
    'mp3': b'\xff\xfb\x90\x64\0\0', 'aac': b'\xff\xf1\x50\x80\x00\x1f\xfc',
    'zip': b'PK\x03\x04', 'gz': b'\x1f\x8b\x08\0', 'bz2': b'BZh91AY&SY', 'xz': b'\xfd7zXZ\0', '7z': b'7z\xbc\xaf\x27\x1c',
    'rar': b'Rar!\x1a\x07\x00', 'tar': b'\0' * 257 + b'ustar\x0000', 'ole_cfb': bytes.fromhex('d0cf11e0a1b11ae1'),
    'sqlite': b'SQLite format 3\0', 'tiff': b'II*\0',
    'json': b'\xef\xbb\xbf \r\n\t{"a": [1, 2]}', 'jsonl': b'{"a":1}\n{"a":2}\n{"a":3}\n', 'html': b'  <!doctype HTML><p>x',
    'xml': b'<?xml version="1.0"?><calls/>', 'svg': b'<svg xmlns="http://www.w3.org/2000/svg"/>',
    'mbox': b'From MAILER-DAEMON Thu Jan  1 00:00:00 2024\nReturn-Path: <x@example.org>\n\nhi',
    'eml': b'From: a@example.org\r\nSubject: s\r\nX-Custom: 1\r\n\r\nbody', 'vcf': b'begin:vcard\nend:vcard',
    'ics': b'BEGIN:VCALENDAR\r\n', 'csv': b'a;b;c\n1;2;3\n', 'tsv': b'a\tb\n1\t2\n', 'txt': 'Zażółć\n'.encode(),
}


def test_table_is_versioned_data_with_unique_extensions():
    table = signatures.table()
    assert table['schema'] == 'ageds.catalog-signatures/v1' and signatures.version() == table['version']
    raw = json.loads(signatures.TABLE_PATH.read_text())
    extensions = [e for spec in raw['types'].values() for e in spec['extensions']]
    assert len(extensions) == len(set(extensions))
    assert {r['type'] for r in raw['rules']} <= set(raw['types'])


@pytest.mark.parametrize('expected', sorted(SAMPLES))
def test_sniff_recognizes_each_type(expected):
    result = signatures.sniff(SAMPLES[expected], complete=True)
    assert result.type == expected, (expected, result)
    text_like = {'json', 'jsonl', 'html', 'xml', 'svg', 'mbox', 'eml', 'vcf', 'ics', 'csv', 'tsv', 'txt'}
    assert result.basis == ('heuristic' if expected in text_like else 'signature')


def test_sniff_edge_cases():
    assert signatures.sniff(b'').type is None
    assert signatures.sniff(bytes(range(256))).details['binary'] is True
    # A PDF after leading junk is still found, with its offset recorded.
    late = signatures.sniff(b'\r\n\r\njunk%PDF-1.5\n')
    assert late.type == 'pdf' and late.details['header_offset'] == 8
    # JPEG starts with FF D8 and must not be read as MPEG audio frame sync.
    assert signatures.sniff(b'\xff\xd8\xff\xdb').type == 'jpg'
    # Incomplete JSON prefix is labelled, not parsed.
    cut = signatures.sniff(b'{"conversations": [{"id": 1', complete=False)
    assert cut.type == 'json' and cut.details['json_valid'] == 'not_checked'
    broken = signatures.sniff(b'{"a": ', complete=True)
    assert broken.type == 'json' and broken.details['json_valid'] is False
    # ID3 tag followed by ADTS is AAC, not MP3.
    id3_aac = b'ID3\x04\x00\x00\x00\x00\x00\x00' + SAMPLES['aac']
    assert signatures.sniff(id3_aac).type == 'aac'
    # UTF-16 text with BOM is text, not binary.
    assert signatures.sniff('\ufeffBEGIN:VCARD\r\n'.encode('utf-16-le')).type == 'vcf'


def test_relation_keeps_both_claims():
    pdf = signatures.sniff(b'%PDF-1.4')
    assert signatures.relation('dat', 'opaque', pdf) == ('opaque_extension', True)
    assert signatures.relation('xyz', None, pdf) == ('unregistered_extension', True)
    assert signatures.relation(None, None, pdf) == ('no_extension', False)
    assert signatures.relation('pdf', 'pdf', pdf) == ('match', False)
    assert signatures.relation('jpg', 'jpg', signatures.sniff(SAMPLES['png'])) == ('mismatch', True)
    assert signatures.relation('m4a', 'm4a', signatures.sniff(SAMPLES['mp4'])) == ('variant', True)
    assert signatures.relation('csv', 'csv', signatures.sniff(b'plain words\n', complete=True)) == ('text_variant', False)
    assert signatures.relation('log', 'txt', signatures.sniff(b'', complete=True)) == ('unrecognized_content', False)
    assert signatures.relation('xyz', None, signatures.sniff(b'words', complete=True)) == ('unregistered_extension', False)


def test_extension_parsing():
    assert signatures.extension_of('a.TAR.GZ') == ('tar.gz', 'tar.gz')
    assert signatures.extension_of('.bashrc') == (None, None)
    assert signatures.extension_of('archive.') == (None, None)
    assert signatures.extension_of('conversations-001.dat') == ('dat', 'opaque')
    assert signatures.extension_of('x.Unknown') == ('unknown', None)


def test_container_refinements():
    names = ['[Content_Types].xml', 'word/document.xml']
    assert signatures.refine_zip(names, lambda n: None)[0] == 'docx'
    assert signatures.refine_zip(['mimetype', 'content.xml'],
                                 lambda n: b'application/vnd.oasis.opendocument.spreadsheet')[0] == 'ods'
    assert signatures.refine_zip(['a.txt'], lambda n: None)[0] == 'zip'
    for stream, expected in (('Workbook', 'xls'), ('WordDocument', 'doc'), ('__substg1.0_0037001F', 'msg'), ('Other', 'ole_cfb')):
        data = _cfb(stream)
        kind, details = signatures.refine_cfb(lambda o, n: data[o:o + n], len(data))
        assert kind == expected and stream in details['cfb_streams_seen']
    assert signatures.refine_cfb(lambda o, n: b'', 0)[0] == 'ole_cfb'


def test_name_flags_and_display():
    assert set(archives.name_flags(b'../x')) == {'parent_traversal'}
    assert set(archives.name_flags(b'C:\\x')) >= {'absolute', 'backslash'}
    assert set(archives.name_flags(b'a\x00b')) >= {'control_chars'}
    assert archives.name_flags(b'') == ['empty_name']
    assert archives.display_name(b'caf\x82', utf8_flag=False)[:2] == ('caf\u00e9', 'cp437 (zip default)')
    assert archives.display_name('ł'.encode(), utf8_flag=False)[1] == 'utf-8 (unflagged)'
    assert archives.display_name(b'\xff', utf8_flag=None)[1] == 'undecodable_bytes'


@pytest.mark.parametrize('raw', [b'plain.txt', b'a!/b', b'!', b'%', b'%21', b'caf\x82.txt', 'zażółć'.encode(), b'..', b'/abs',
                                 b'x\\y', b'tab\tname', '\u202etxt.exe'.encode(), b'\xed\xa0\x80', b'', b'!2', b'\xf0\x9f\x98\x80'])
def test_locator_escape_roundtrip(raw):
    segment = escape_name(raw)
    assert '!' not in segment
    assert unescape_name(segment) == raw


def test_locator_escape_random_bytes_roundtrip():
    rng = random.Random(20261016)
    for _ in range(500):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 24)))
        assert unescape_name(escape_name(raw)) == raw


def test_locator_structure_and_errors():
    locator = member_locator(member_locator('dir/o%21.zip', b'in!/n.zip', 2), b'c.txt')
    assert locator == 'dir/o%21.zip!/in%21/n.zip!2!/c.txt'
    parsed = parse_locator(locator)
    assert parsed['disk'] == [b'dir', b'o!.zip']
    assert parsed['members'] == [{'name': b'in!/n.zip', 'occurrence': 2}, {'name': b'c.txt', 'occurrence': 1}]
    assert escape_name('\u202e'.encode()) == '%E2%80%AE'  # bidi override is displayed escaped
    for bad in ('', 'a.zip!x', 'a.zip!/b!1', 'a%2', 'a.zip!2'):
        with pytest.raises(ValueError):
            parse_locator(bad)
    with pytest.raises(ValueError):
        member_locator('a.zip', b'x', 0)
