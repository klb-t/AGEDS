"""OCR and diarization adapters: unavailable without dependencies, fake engines, fencing."""
from __future__ import annotations

import json
import shutil
import struct
import time
import unittest
import zlib
from unittest.mock import patch

from server.app import db, jobs, worker
from server.app.jobs import LeaseLost, claim_job, recover_expired_jobs
from server.app.media import ocr as ocr_module
from server.app.media.common import AdapterUnavailable, UnsupportedInput
from server.app.media.diarization import add_speaker_assignment, speaker_view, validate_turns
from server.app.media.queue import enqueue_media_job
from server.app.transcription import enqueue_transcription
from server.tests.test_media_support import MediaCase


def png_bytes(width: int = 36, height: int = 64) -> bytes:
    """A valid blank grayscale PNG generated in code (no pixels of any real screen)."""
    raw = b''.join(b'\x00' + b'\xff' * width for _ in range(height))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def pdf_bytes(pages: list[str]) -> bytes:
    """Minimal multi-page PDF with a Helvetica text layer, generated in code."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>']
    kids = ' '.join(f'{3 + 2 * i} 0 R' for i in range(len(pages)))
    objects.append(f'<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>'.encode())
    font = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents {4 + 2 * i} 0 R '
                       f'/Resources << /Font << /F1 {font} 0 R >> >> >>'.encode())
        stream = f'BT /F1 18 Tf 20 100 Td ({text}) Tj ET'.encode()
        objects.append(b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    out, offsets = b'%PDF-1.4\n', []
    for i, obj in enumerate(objects):
        offsets.append(len(out))
        out += f'{i + 1} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
    out += b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    out += f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return out


def tsv(width: int, height: int, words: list[tuple]) -> str:
    """Tesseract TSV from (block, par, line, word, left, top, width, height, conf, text) tuples."""
    rows = ['level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext',
            f'1\t1\t0\t0\t0\t0\t0\t0\t{width}\t{height}\t-1\t']
    rows += ['5\t1\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%s\t%s' % w for w in words]
    return '\n'.join(rows) + '\n'


CHAT_WORDS = [
    (1, 1, 1, 1, 160, 5, 40, 14, 91.0, '12:30'),                 # status bar: time only, centered
    (2, 1, 1, 1, 20, 100, 70, 20, 88.5, 'Kontakt'), (2, 1, 1, 2, 94, 100, 18, 20, 90.0, 'A:'),
    (2, 1, 1, 3, 116, 100, 50, 20, 87.0, 'Dzień'), (2, 1, 1, 4, 170, 100, 40, 20, 86.0, 'dobry'),
    (2, 1, 2, 1, 20, 124, 30, 20, 80.0, 'jak'), (2, 1, 2, 2, 54, 124, 30, 20, 81.0, 'się'),
    (2, 1, 2, 3, 88, 124, 60, 20, 79.0, 'masz?'), (2, 1, 2, 4, 160, 124, 40, 20, 95.0, '10:42'),
    (3, 1, 1, 1, 200, 200, 60, 20, 92.0, 'Goed,'), (3, 1, 1, 2, 264, 200, 40, 20, 93.0, 'dank'),
    (3, 1, 1, 3, 308, 200, 24, 20, 94.0, 'je'),
    (3, 1, 2, 1, 270, 224, 44, 20, 96.0, '10:45'), (3, 1, 2, 2, 318, 224, 22, 20, 96.0, 'PM'),
    (4, 1, 1, 1, 20, 300, 30, 20, 70.0, 'See'), (4, 1, 1, 2, 54, 300, 30, 20, 71.0, 'you'),
    (4, 1, 1, 3, 88, 300, 20, 20, 72.0, 'at'), (4, 1, 1, 4, 112, 300, 36, 20, 73.0, '9.30'),
    (4, 1, 1, 5, 152, 300, 70, 20, 74.0, 'tomorrow'), (4, 1, 1, 6, 226, 300, 40, 20, 75.0, '11:02'),
]


class FakeOCR:
    """Stands in for TesseractCLI; returns parsed TSV prepared per call."""
    def __init__(self, pages: list[str], status='available'):
        self.pages, self.status, self.calls = list(pages), status, []

    def availability(self, languages):
        return {'status': self.status, 'engine': 'fake-ocr', 'reason': 'fake engine switched off'}

    def describe(self):
        return {'tool': 'fake-ocr', 'tool_version': 'test-1', 'provider': 'local-test'}

    def recognize(self, image, **options):
        self.calls.append({'signature': image[:8], **options})
        return ocr_module.parse_tsv(self.pages[min(len(self.calls), len(self.pages)) - 1])


class FakeDiarizer:
    def __init__(self, turns, status='available', on_call=None):
        self.turns, self.status, self.on_call = turns, status, on_call

    def availability(self):
        return {'status': self.status, 'reason': 'fake switched off'}

    def describe(self):
        return {'tool': 'fake-diarizer', 'tool_version': 'test-1', 'provider': 'local-test', 'model': 'fake'}

    def diarize(self, audio):
        assert audio.read(4) == b'RIFF'
        if self.on_call:
            self.on_call()
        return self.turns


class OcrTests(MediaCase):
    def test_tsv_parsing_keeps_boxes_and_engine_confidence(self):
        parsed = ocr_module.parse_tsv(tsv(360, 640, CHAT_WORDS))
        self.assertEqual({'width': 360, 'height': 640}, parsed['image'])
        self.assertEqual(len(CHAT_WORDS), len(parsed['words']))
        lines = ocr_module.lines_from_words(parsed['words'])
        self.assertEqual('Kontakt A: Dzień dobry', lines[1]['text'])
        self.assertEqual({'left': 20, 'top': 100, 'width': 190, 'height': 20}, lines[1]['box'])
        self.assertEqual(['pdf', 'png', 'jpeg', 'unknown'],
                         [ocr_module.detect_input(b) for b in (b'%PDF-1.7', png_bytes(), b'\xff\xd8\xff\xe0', b'plain')])

    def test_chat_screenshot_yields_candidates_never_events(self):
        aid = self.add_artifact(png_bytes(), 'screenshot.png', 'image/png')
        queued = enqueue_media_job('ocr', aid, recipe_id='ocr-chat-screenshot-pol-nld-eng')
        engine = FakeOCR([tsv(360, 640, CHAT_WORDS)])
        did = worker.process_job(claim_job(), engine)
        self.assertEqual([{'signature': b'\x89PNG\r\n\x1a\n', 'languages': ['pol', 'nld', 'eng'], 'psm': 4,
                           'oem': None, 'timeout': 300}], engine.calls)
        version = self.rows('derived_text')[0]
        self.assertEqual((did, 'ocr'), (version['id'], version['kind']))
        self.assertIn('Kontakt A: Dzień dobry\njak się masz? 10:42', version['text'])
        segments = json.loads(version['segments_json'])
        self.assertEqual(1, segments[0]['page'])
        self.assertEqual(95.0, segments[2]['words'][3]['engine_confidence'])
        candidates = self.rows('event_candidates')
        self.assertEqual(4, len(candidates))
        self.assertEqual({'candidate'}, {c['status'] for c in candidates})
        status_bar, first, second, third = candidates
        self.assertEqual(('12:30', None, 'center'), (status_bar['time_text'], status_bar['body_text'], status_bar['layout_side']))
        self.assertEqual(('Kontakt A', '10:42', 'Dzień dobry\njak się masz?', 'left'),
                         (first['sender_text'], first['time_text'], first['body_text'], first['layout_side']))
        self.assertEqual({'left': 160, 'top': 124, 'width': 40, 'height': 20}, json.loads(first['locators_json'])['time_box'])
        self.assertEqual((None, '10:45 PM', 'Goed, dank je', 'right'),
                         (second['sender_text'], second['time_text'], second['body_text'], second['layout_side']))
        self.assertEqual('11:02', third['time_text'])
        self.assertEqual(['9.30', '11:02'], json.loads(third['metadata_json'])['time_candidates'])
        self.assertTrue(all(c['run_id'] == version['run_id'] and c['derived_text_id'] == did for c in candidates))
        self.assertEqual([], self.rows('events'))
        with db.session() as conn, self.assertRaises(Exception):
            conn.execute("UPDATE event_candidates SET status='accepted'")
        run = self.rows('processing_runs')[0]
        self.assertEqual(queued['recipe']['sha256'], json.loads(run['provenance_json'])['recipe']['sha256'])
        self.assertEqual({'unit': 'page', 'amount': 1, 'basis': 'pages_recognized'}, json.loads(run['metadata_json'])['work'])

    @unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdfinfo'), 'poppler-utils not installed')
    def test_pdf_pages_are_rasterized_by_poppler_and_ocr_per_page(self):
        aid = self.add_artifact(pdf_bytes(['Synthetic page one', 'Synthetic page two', 'Third']), 'scan.pdf', 'application/pdf')
        enqueue_media_job('ocr', aid, overrides={'pdf': {'max_pages': 2, 'dpi': 72}})
        engine = FakeOCR([tsv(300, 200, [(1, 1, 1, 1, 20, 90, 100, 20, 60.0, 'page-one')]),
                          tsv(300, 200, [(1, 1, 1, 1, 20, 90, 100, 20, 61.0, 'page-two')])])
        worker.process_job(claim_job(), engine)
        self.assertEqual([b'\x89PNG\r\n\x1a\n'] * 2, [call['signature'] for call in engine.calls])
        version = self.rows('derived_text')[0]
        self.assertEqual('page-one\fpage-two', version['text'])
        metadata = json.loads(version['metadata_json'])
        self.assertEqual({'status': 'partial', 'pages_total': 3, 'pages_processed': 2, 'reason': 'pdf.max_pages reached'},
                         metadata['coverage'])
        self.assertEqual(['pdf_raster', 'pdf_raster'], [p['source'] for p in metadata['pages']])
        self.assertEqual({'not_extracted'}, {p['text_layer'] for p in metadata['pages']})
        self.assertEqual([1, 2], [s['page'] for s in json.loads(version['segments_json'])])
        self.assertEqual([], self.rows('event_candidates'))

    def test_missing_tesseract_is_reported_unavailable_and_job_fails_explicitly(self):
        aid = self.add_artifact(png_bytes(), 'page.png', 'image/png')
        enqueue_media_job('ocr', aid)
        with patch.object(ocr_module.shutil, 'which', return_value=None), patch.dict('os.environ', {'EW_TESSERACT': ''}):
            self.assertEqual('unavailable', ocr_module.TesseractCLI().availability(['pol'])['status'])
            with self.assertRaisesRegex(AdapterUnavailable, 'tesseract executable not found'):
                worker.process_job(claim_job())
        self.assertEqual(('failed', 'failed'), (self.rows('jobs')[0]['status'], self.rows('processing_runs')[0]['status']))
        self.assertIn('OCR engine unavailable', self.rows('jobs')[0]['error'])
        self.assertEqual([], self.rows('derived_text'))

    def test_pdf_without_rasterizer_and_unknown_input_are_unsupported(self):
        class NoRaster:
            def availability(self):
                return {'status': 'unavailable', 'reason': 'pdftoppm not found'}
        aid = self.add_artifact(pdf_bytes(['x']), 'scan.pdf', 'application/pdf')
        enqueue_media_job('ocr', aid)
        with self.assertRaisesRegex(UnsupportedInput, 'unsupported here: pdftoppm not found'):
            ocr_module.run_ocr(claim_job(), FakeOCR(['']), rasterizer=NoRaster())
        other = self.add_artifact(b'not an image', 'notes.txt', 'text/plain')
        enqueue_media_job('ocr', other)
        with self.assertRaisesRegex(UnsupportedInput, 'unsupported OCR input type: unknown'):
            worker.process_job(claim_job(), FakeOCR(['']))
        self.assertEqual([], self.rows('derived_text'))

    def test_candidate_insert_failure_rolls_back_the_whole_publication(self):
        aid = self.add_artifact(png_bytes(), 'screenshot.png', 'image/png')
        enqueue_media_job('ocr', aid, recipe_id='ocr-chat-screenshot-pol-nld-eng')
        original = ocr_module.chat_candidates
        def invalid_side(pages):
            return [dict(c, layout_side='sideways') for c in original(pages)]
        with patch.object(ocr_module, 'chat_candidates', invalid_side), self.assertRaises(Exception):
            worker.process_job(claim_job(), FakeOCR([tsv(360, 640, CHAT_WORDS)]))
        self.assertEqual([], self.rows('derived_text'))
        self.assertEqual([], self.rows('event_candidates'))
        self.assertEqual('failed', self.rows('jobs')[0]['status'])

    def test_late_ocr_worker_cannot_publish_after_lease_reclaim(self):
        aid = self.add_artifact(png_bytes(), 'screenshot.png', 'image/png')
        enqueue_media_job('ocr', aid, recipe_id='ocr-chat-screenshot-pol-nld-eng')
        replacement = []

        class Reclaimed(FakeOCR):
            def recognize(self, image, **options):
                with db.session() as conn:
                    conn.execute("UPDATE jobs SET lease_expires_at=? WHERE status='running'", (time.time() - 1,))
                replacement.append(claim_job(worker_id='replacement'))
                return super().recognize(image, **options)
        old = claim_job()
        with self.assertRaises(LeaseLost):
            worker.process_job(old, Reclaimed([tsv(360, 640, CHAT_WORDS)]))
        self.assertEqual([], self.rows('derived_text'))
        self.assertEqual([], self.rows('event_candidates'))
        self.assertEqual(['abandoned', 'running'], [r['status'] for r in self.rows('processing_runs')])
        worker.process_job(replacement[0], FakeOCR([tsv(360, 640, CHAT_WORDS)]))
        self.assertEqual(1, len(self.rows('derived_text')))
        self.assertEqual({replacement[0]['run_id']}, {c['run_id'] for c in self.rows('event_candidates')})


class DiarizationTests(MediaCase):
    TURNS = [{'start': 0.5, 'end': 2.0, 'speaker': 'SPEAKER_00'}, {'start': 2.0, 'end': 3.5, 'speaker': 'SPEAKER_01'}]

    def setUp(self):
        super().setUp()
        self.aid = self.add_audio('audio_single_en')

    def test_missing_pyannote_is_unavailable_and_fails_the_job_explicitly(self):
        enqueue_media_job('diarize', self.aid)
        with patch('importlib.util.find_spec', return_value=None):
            with self.assertRaisesRegex(AdapterUnavailable, 'pyannote.audio is not importable'):
                worker.process_job(claim_job())
        self.assertEqual('failed', self.rows('jobs')[0]['status'])
        self.assertEqual([], self.rows('speaker_turns'))

    def test_turns_are_hypotheses_linked_to_the_run_and_invalid_turns_are_reported(self):
        enqueue_media_job('diarize', self.aid)
        turns = self.TURNS + [{'start': float('nan'), 'end': 1, 'speaker': 'X'}, {'start': 3, 'end': 2, 'speaker': 'Y'},
                              {'start': 1, 'end': 2, 'speaker': ' '}]
        run_id = worker.process_job(claim_job(), FakeDiarizer(turns))
        stored = self.rows('speaker_turns')
        self.assertEqual([(500, 2000, 'SPEAKER_00'), (2000, 3500, 'SPEAKER_01')],
                         [(t['start_ms'], t['end_ms'], t['speaker_label']) for t in stored])
        self.assertEqual({run_id}, {t['run_id'] for t in stored})
        metadata = json.loads(self.rows('processing_runs')[0]['metadata_json'])
        self.assertEqual(3, metadata['rejected_count'])
        self.assertEqual(('audio_second', 4.0), (metadata['work']['unit'], metadata['work']['amount']))
        with db.session() as conn, self.assertRaises(Exception):
            conn.execute("UPDATE speaker_turns SET speaker_label='renamed'")
        accepted, rejected, coverage = validate_turns(self.TURNS * 3, 2)
        self.assertEqual((2, 'partial'), (len(accepted), coverage['status']))

    def test_manual_assignment_takes_precedence_and_both_layers_stay_visible(self):
        self.fake_whisper()
        enqueue_transcription(self.aid)
        transcript_id = worker.process_job(claim_job())
        enqueue_media_job('diarize', self.aid)
        run_id = worker.process_job(claim_job(), FakeDiarizer(self.TURNS))
        text_before = self.rows('derived_text')[0]['text']
        view = speaker_view(self.aid, transcript_id)
        self.assertEqual(run_id, view['diarization_run_id'])
        self.assertEqual('diarization_hypothesis', view['segments'][0]['display_source'])
        hypothesis = view['segments'][0]['hypothesis_label']
        add_speaker_assignment(self.aid, diarization_run_id=run_id, speaker_label=hypothesis, speaker='Speaker A (manual)')
        view = speaker_view(self.aid, transcript_id)
        self.assertEqual(('Speaker A (manual)', 'manual_label_mapping', hypothesis),
                         (view['segments'][0]['display_speaker'], view['segments'][0]['display_source'],
                          view['segments'][0]['hypothesis_label']))
        add_speaker_assignment(self.aid, derived_text_id=transcript_id, segment_index=0, speaker='Speaker B (manual)')
        add_speaker_assignment(self.aid, derived_text_id=transcript_id, segment_index=0, speaker='Speaker C (corrected)')
        segment = speaker_view(self.aid, transcript_id)['segments'][0]
        self.assertEqual(('Speaker C (corrected)', 'manual_segment', 2), (segment['display_speaker'],
                         segment['display_source'], segment['manual_segment_history']))
        self.assertEqual('Speaker A (manual)', segment['manual_label_mapping']['speaker'])
        self.assertTrue(segment['hypotheses'])
        self.assertEqual(text_before, self.rows('derived_text')[0]['text'])
        for bad in ({'speaker': ' ', 'derived_text_id': transcript_id, 'segment_index': 0},
                    {'speaker': 'x', 'derived_text_id': transcript_id, 'segment_index': 99},
                    {'speaker': 'x', 'diarization_run_id': run_id, 'speaker_label': 'SPEAKER_99'},
                    {'speaker': 'x', 'derived_text_id': transcript_id, 'segment_index': 0,
                     'diarization_run_id': run_id, 'speaker_label': hypothesis},
                    {'speaker': 'x'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                add_speaker_assignment(self.aid, **bad)
        with db.session() as conn, self.assertRaises(Exception):
            conn.execute('DELETE FROM speaker_assignments')

    def test_late_diarization_worker_cannot_publish_and_recovery_requeues(self):
        enqueue_media_job('diarize', self.aid)
        old = claim_job(now=1000, lease_seconds=10)
        with self.assertRaises(LeaseLost):
            jobs.publish_result(old, lambda conn, current: None, now=1010)
        self.assertEqual(1, recover_expired_jobs(now=1010))
        self.assertEqual('abandoned', self.rows('processing_runs')[0]['status'])
        replacement = claim_job()
        self.assertEqual(old['id'], replacement['id'])
        self.assertEqual(2, replacement['attempts'])
        worker.process_job(replacement, FakeDiarizer(self.TURNS))
        self.assertEqual(['abandoned', 'done'], [r['status'] for r in self.rows('processing_runs')])
        self.assertEqual({replacement['run_id']}, {t['run_id'] for t in self.rows('speaker_turns')})
        self.assertFalse(jobs.finish_job(old['id'], old['lease_token'], 'failed', 'stale'))


class NewKindFencingTests(MediaCase):
    def test_bare_finish_cannot_complete_publishing_kinds_and_claim_can_filter_kinds(self):
        aid = self.add_artifact(png_bytes(), 'page.png', 'image/png')
        enqueue_media_job('ocr', aid)
        self.assertIsNone(claim_job(kinds=('transcribe',)))
        job = claim_job(kinds=('ocr',))
        self.assertEqual('ocr', job['kind'])
        self.assertEqual('AGEDS OCR worker', json.loads(self.rows('processing_runs')[0]['provenance_json'])['pipeline'])
        with self.assertRaisesRegex(ValueError, 'ocr completion requires a fenced publication'):
            jobs.finish_job(job['id'], job['lease_token'], 'done')
        with self.assertRaises(ValueError):
            claim_job(kinds=())

    def test_worker_kind_filter_comes_from_environment(self):
        with patch.dict('os.environ', {'EW_WORKER_KINDS': ' ocr, transcribe ,'}):
            self.assertEqual(('ocr', 'transcribe'), worker.worker_kinds())
        with patch.dict('os.environ', {'EW_WORKER_KINDS': ''}):
            self.assertIsNone(worker.worker_kinds())
