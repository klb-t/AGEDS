"""N51 independent Python WAV semantics and actual scanner integration.

Synthetic bytes only. Actual read-budget/race instrumentation belongs to N54.
"""
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from server.app import scanner
from server.app.wav_header import probe_wav_header


def chunk(name,payload=b'',declared=None,pad=True):
    return name.encode('ascii')+struct.pack('<I',len(payload) if declared is None else declared)+payload+(b'\x00' if pad and len(payload)%2 else b'')


def fmt(code=1,channels=1,rate=8000,byte_rate=16000,align=2,bits=16,extra=b''):
    return chunk('fmt ',struct.pack('<HHIIHH',code,channels,rate,byte_rate,align,bits)+extra)


def riff(*chunks,declared=None):
    payload=b'WAVE'+b''.join(chunks)
    return b'RIFF'+struct.pack('<I',len(payload) if declared is None else declared)+payload


class WavScanAcceptanceTests(unittest.TestCase):
    def scan(self,raw,limits=None):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'clip.wav'
            path.write_bytes(raw)
            before=(path.read_bytes(),path.stat().st_mtime_ns)
            result=scanner.scan_sources(folder,limits=limits)
            self.assertEqual((path.read_bytes(),path.stat().st_mtime_ns),before)
        return result,result['files'][0]

    def assert_no_duration(self,manifest,file):
        self.assertNotIn('duration_seconds',file['audio_metadata'])
        self.assertIsNone(file['audio_metadata']['header_probe']['declared_duration_sec'])
        self.assertFalse(any(observation['kind']=='duration' for observation in manifest['observations']))
        self.assertFalse(manifest['coverage']['complete'])

    def test_old_44_byte_false_complete_is_now_explicit_contradiction(self):
        # Old scan_sources produced frames8000/duration1.0/hashcomplete/coverage
        # complete with no issues, although there is no data body at all.
        raw=riff(fmt(),chunk('data',declared=16000),declared=16036)
        self.assertEqual(len(raw),44)
        manifest,file=self.scan(raw)
        self.assertEqual(file['hash_status'],'complete')
        self.assertEqual(file['sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(file['parse_status'],'failed')
        report=file['audio_metadata']['header_probe']
        self.assertEqual(report['provider_size_bytes'],44)
        self.assertEqual(report['riff_declared_bytes'],16044)
        self.assertEqual(report['data_declared_bytes'],16000)
        self.assertEqual(report['byte_rate'],16000)
        self.assertFalse(report['body_validated'])
        self.assert_no_duration(manifest,file)

    def test_10000_junk_chunks_stop_as_partial_instead_of_metadata_success(self):
        # Old hash-skipped scanner traversed all10000 chunks:20009 reads/80044
        # returned bytes. Read-budget instrumentation is a separate N54 suite.
        raw=riff(chunk('JUNK')*10000,fmt(),chunk('data'))
        self.assertEqual(len(raw),80044)
        manifest,file=self.scan(raw,scanner.ScanLimits(max_hash_bytes=1))
        self.assertEqual(file['hash_status'],'unknown')
        self.assertEqual(file['parse_status'],'limited')
        report=file['audio_metadata']['header_probe']
        self.assertEqual(report['status'],'partial')
        self.assertLessEqual(report['bytes_inspected'],65536)
        self.assert_no_duration(manifest,file)

    def test_normal_pcm_preserves_legacy_values_but_declares_header_only_scope(self):
        raw=riff(fmt(),chunk('data',b'\x00'*8000))
        manifest,file=self.scan(raw)
        audio=file['audio_metadata']
        self.assertEqual(file['parse_status'],'metadata_only')
        self.assertEqual({key:audio[key] for key in ('frames','sample_rate','channels','sample_width_bytes','duration_seconds')},
                         {'frames':4000,'sample_rate':8000,'channels':1,'sample_width_bytes':2,'duration_seconds':.5})
        self.assertEqual(audio['source'],'same_descriptor_riff_header')
        self.assertEqual(audio['scope'],'header_prefix_only')
        self.assertFalse(audio['body_validated'])
        self.assertEqual(audio['size_basis'],'fstat_same_descriptor')
        observation=next(value for value in manifest['observations'] if value['kind']=='duration')
        self.assertEqual(observation['basis'],'wav_header')
        self.assertEqual(observation['seconds'],.5)
        self.assertEqual(observation['duration_basis'],'declared_data_bytes_divided_by_header_byte_rate')
        self.assertFalse(observation['body_validated'])
        self.assertFalse(manifest['coverage']['complete'])

    def test_large_pcm_body_stays_uninspected_header_declaration(self):
        raw=riff(fmt(),chunk('data',b'\x00'*320000))
        manifest,file=self.scan(raw,scanner.ScanLimits(max_hash_bytes=1))
        audio=file['audio_metadata']
        self.assertEqual(audio['duration_seconds'],20.0)
        self.assertFalse(audio['body_validated'])
        self.assertEqual(audio['header_probe']['data_declared_bytes'],320000)
        self.assertFalse(audio['header_probe']['end_of_input'])
        self.assertEqual(file['hash_status'],'unknown')
        self.assertFalse(manifest['coverage']['complete'])

    def test_visible_duplicate_chunks_never_yield_first_plausible_duration(self):
        for raw in (riff(fmt(),fmt(),chunk('data',b'\x00'*16)),
                    riff(fmt(),chunk('data',b'\x00'*16),chunk('data',b'\x00'*16)),
                    riff(fmt(),chunk('data',b'\x00'*16),fmt())):
            with self.subTest(length=len(raw)):
                manifest,file=self.scan(raw)
                self.assertEqual(file['parse_status'],'failed')
                self.assertTrue(any(value['code'].startswith('duplicate_') for value in file['audio_metadata']['header_probe']['issues']))
                self.assert_no_duration(manifest,file)

    def test_unsupported_codec_and_extension_retain_raw_fields_without_duration(self):
        for format_chunk in (fmt(code=6),fmt(code=0xFFFE),fmt(extra=struct.pack('<H',1))):
            manifest,file=self.scan(riff(format_chunk,chunk('data',b'\x00'*16)))
            report=file['audio_metadata']['header_probe']
            self.assertEqual(file['parse_status'],'unsupported')
            self.assertIsNotNone(report['format_code'])
            self.assertEqual(report['byte_rate'],16000)
            self.assert_no_duration(manifest,file)

    def test_invalid_geometry_is_not_silently_normalized(self):
        for format_chunk in (fmt(byte_rate=7),fmt(rate=0),fmt(channels=0),fmt(align=1)):
            manifest,file=self.scan(riff(format_chunk,chunk('data',b'\x00'*16)))
            self.assertEqual(file['parse_status'],'failed')
            self.assert_no_duration(manifest,file)
        manifest,file=self.scan(riff(fmt(byte_rate=7),chunk('data',b'\x00'*16)))
        self.assertEqual(file['audio_metadata']['header_probe']['byte_rate'],7)

    def test_odd_padding_and_partial_sample_frame_are_distinct(self):
        valid=riff(chunk('JUNK',b'x'),fmt(),chunk('data',b'\x00'*16))
        _,file=self.scan(valid)
        self.assertEqual(file['audio_metadata']['duration_seconds'],.001)
        invalid=(riff(chunk('JUNK',b'x',pad=False),fmt(),chunk('data',b'\x00'*16)),
                 riff(fmt(),chunk('data',b'\x00'*3)))
        for raw in invalid:
            manifest,file=self.scan(raw)
            self.assert_no_duration(manifest,file)

    def test_provider_extent_and_unknown_length_are_separate_observations(self):
        raw=riff(fmt(),chunk('data',declared=16000),declared=16036)
        unknown=probe_wav_header(raw)
        self.assertEqual(unknown['declared_duration_sec'],1.0)
        self.assertIsNone(unknown['provider_size_bytes'])
        self.assertFalse(unknown['body_validated'])
        for actual,eof in ((44,True),(16043,False),(16045,False)):
            with self.subTest(actual=actual,eof=eof):
                report=probe_wav_header(raw,actual,eof)
                self.assertIsNone(report['declared_duration_sec'])
                self.assertEqual(report['riff_declared_bytes'],16044)
                self.assertEqual(report['data_declared_bytes'],16000)
        self.assertIsNone(probe_wav_header(raw,end_of_input=True)['declared_duration_sec'])

    def test_each_basic_header_truncation_is_explicit_and_safe(self):
        raw=riff(fmt(),chunk('data',b'\x00'*16))
        for size in range(44):
            with self.subTest(size=size):
                report=probe_wav_header(raw[:size],size,True)
                self.assertIsNone(report['declared_duration_sec'])
                self.assertFalse(report['body_validated'])
                self.assertTrue(report['issues'])

    def test_unsigned_extent_overflow_cannot_wrap_to_visible_fake_headers(self):
        raw=riff(chunk('JUNK',declared=0xFFFFFFFF),fmt(),chunk('data',b'\x00'*16),declared=0xFFFFFFFF)
        report=probe_wav_header(raw,4294967303,False)
        self.assertEqual(report['riff_declared_bytes'],4294967303)
        self.assertIsNone(report['declared_duration_sec'])
        self.assertFalse(report['body_validated'])

    def test_exact_prefix_boundary_and_beyond_boundary_are_distinguished(self):
        boundary=riff(fmt(),chunk('JUNK',b'\x00'*65484),chunk('data',declared=160),declared=65688)
        self.assertEqual(len(boundary),65536)
        self.assertEqual(probe_wav_header(boundary,65696,False)['declared_duration_sec'],.01)
        crossing=riff(fmt(),chunk('JUNK',b'\x00'*65486),chunk('data',b'\x00'*160))
        report=probe_wav_header(crossing,len(crossing),True)
        self.assertEqual(report['bytes_inspected'],65536)
        self.assertFalse(report['end_of_input'])
        self.assertIsNone(report['declared_duration_sec'])
        self.assertEqual(report['status'],'partial')

    def test_probe_inputs_reject_boolean_sizes_and_invalid_container_types(self):
        for args in ((bytearray(),),('',),(b'',True),(b'',-1),(b'',2**63),(b'',None,1)):
            with self.subTest(args=args),self.assertRaises(ValueError):
                probe_wav_header(*args)

    def test_actual_scanner_trailing_extent_mismatch_suppresses_duration(self):
        base=riff(fmt(),chunk('data',b'\x00'*16))
        manifest,file=self.scan(base+b'TRAILING')
        report=file['audio_metadata']['header_probe']
        self.assertEqual(report['riff_declared_bytes'],len(base))
        self.assertEqual(report['provider_size_bytes'],len(base)+8)
        self.assert_no_duration(manifest,file)

    def test_actual_scanner_unsupported_containers_and_empty_file_are_incomplete(self):
        base=riff(fmt(),chunk('data',b'\x00'*16))
        for signature in (b'RIFX',b'RF64',b'FORM'):
            manifest,file=self.scan(signature+base[4:])
            self.assertEqual(file['parse_status'],'unsupported')
            self.assert_no_duration(manifest,file)
        manifest,file=self.scan(b'')
        self.assertEqual(file['parse_status'],'failed')
        self.assert_no_duration(manifest,file)

    def test_unseen_duplicate_beyond_data_body_is_not_claimed_absent(self):
        raw=riff(fmt(),chunk('data',b'\x00'*80000),fmt())
        manifest,file=self.scan(raw,scanner.ScanLimits(max_hash_bytes=1))
        report=file['audio_metadata']['header_probe']
        self.assertEqual(report['status'],'observed')
        self.assertEqual(report['declared_duration_sec'],5.0)
        self.assertTrue(any(issue['code']=='trailing_chunks_uninspected' for issue in report['issues']))
        self.assertFalse(report['body_validated'])
        self.assertFalse(manifest['coverage']['complete'])

    def test_ieee_float_header_projects_scalars_without_claiming_decode(self):
        raw=riff(fmt(code=3,byte_rate=32000,align=4,bits=32),chunk('data',b'\xff'*3200))
        manifest,file=self.scan(raw)
        self.assertEqual(file['audio_metadata']['duration_seconds'],.1)
        self.assertEqual(file['audio_metadata']['sample_width_bytes'],4)
        self.assertEqual(file['audio_metadata']['header_probe']['format_code'],3)
        self.assertFalse(file['audio_metadata']['body_validated'])
        self.assertFalse(manifest['coverage']['complete'])
