"""Local OCR adapter (Tesseract CLI) with per-page PDF rasterization (poppler).

* Images go to ``tesseract stdin stdout -l pol+nld+eng --psm N tsv`` through a
  pipe; PDFs are rasterized page by page with ``pdftoppm -f N -l N -r DPI -png -``
  (stdin/stdout). No temporary copy of the input is written anywhere.
* Output is one ``derived_text`` version of kind ``ocr`` per run, with line
  segments carrying page numbers and pixel boxes. Engine word confidence is
  stored as reported; it is not correctness.
* With layout ``chat_screenshot`` a heuristic proposes ``event_candidates``
  (sender/time/text with box locators). Candidates are never events.

If Tesseract is missing the adapter reports ``unavailable`` with a reason; if a
PDF needs rasterization and poppler is missing it is ``unsupported`` here.
PDF text layers are not extracted by this adapter (that would be parser output,
not OCR) and are recorded as ``not_extracted``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess

from ..jobs import publish_derived_text, update_run_metadata
from .common import (AdapterUnavailable, UnsupportedInput, input_provenance, load_artifact, open_verified,
                     read_all, verification_record)
from .recipe import recipe_from_job, run_recipe_record

CONFIDENCE_MEANING = 'engine-reported word confidence (0-100, -1 = none); not correctness'
CHAT_PARSER = 'chat-screenshot-layout/1'
SIGNATURES = (
    (b'%PDF-', 'pdf'), (b'\x89PNG\r\n\x1a\n', 'png'), (b'\xff\xd8\xff', 'jpeg'), (b'II*\x00', 'tiff'),
    (b'MM\x00*', 'tiff'), (b'BM', 'bmp'), (b'GIF87a', 'gif'), (b'GIF89a', 'gif'),
)
IMAGE_KINDS = ('png', 'jpeg', 'tiff', 'bmp', 'gif', 'webp')
TIME_PATTERN = re.compile(r'(?<![\d:.])(?:[01]?\d|2[0-3])[:.][0-5]\d(?:\s?[AaPp]\.?\s?[Mm]\.?)?(?![\d:])')
SENDER_PATTERN = re.compile(r'^\s*([^:\n]{1,48}?):\s+\S')


def detect_input(data: bytes) -> str:
    for signature, kind in SIGNATURES:
        if data.startswith(signature):
            return kind
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return 'unknown'


def _run(command: list[str], *, data: bytes | None = None, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, input=data, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f'{os.path.basename(command[0])} exceeded timeout of {timeout} s') from error


class TesseractCLI:
    """Tesseract via its command line; ``EW_TESSERACT`` may name the executable."""

    def __init__(self, binary: str | None = None):
        self.binary = binary or os.getenv('EW_TESSERACT', '') or shutil.which('tesseract')
        self._version = None

    def availability(self, languages: list[str]) -> dict:
        if not self.binary or not os.path.isfile(self.binary):
            return {'status': 'unavailable', 'engine': 'tesseract-cli',
                    'reason': 'tesseract executable not found (install tesseract-ocr or set EW_TESSERACT)'}
        version = self.version()
        listed = _run([self.binary, '--list-langs'], timeout=30)
        available = {line.strip() for line in listed.stdout.decode('utf-8', 'replace').splitlines()[1:] if line.strip()}
        missing = [code for code in languages if code not in available]
        if missing:
            return {'status': 'unavailable', 'engine': 'tesseract-cli', 'version': version,
                    'reason': f'missing traineddata for {", ".join(missing)}', 'languages_available': sorted(available)}
        return {'status': 'available', 'engine': 'tesseract-cli', 'version': version, 'languages_available': sorted(available)}

    def version(self) -> str:
        if self._version is None:
            result = _run([self.binary, '--version'], timeout=30)
            text = (result.stdout or result.stderr).decode('utf-8', 'replace').strip().splitlines()
            self._version = text[0] if text else 'unknown'
        return self._version

    def describe(self) -> dict:
        return {'tool': 'tesseract', 'tool_version': self.version() if self.binary and os.path.isfile(self.binary) else 'unknown',
                'provider': 'local', 'executable': self.binary or 'not_found'}

    def recognize(self, image: bytes, *, languages: list[str], psm: int, oem: int | None, timeout: float) -> dict:
        command = [self.binary, 'stdin', 'stdout', '-l', '+'.join(languages), '--psm', str(psm)]
        if oem is not None:
            command += ['--oem', str(oem)]
        result = _run(command + ['tsv'], data=image, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError('tesseract failed: ' + result.stderr.decode('utf-8', 'replace')[:2000])
        return parse_tsv(result.stdout.decode('utf-8', 'replace'))


def parse_tsv(text: str) -> dict:
    """Parse Tesseract TSV into page size and words with pixel boxes; raw text kept."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith('level'):
        raise RuntimeError('tesseract TSV header missing')
    header = lines[0].split('\t')
    image = {'width': None, 'height': None}
    words = []
    for line in lines[1:]:
        fields = line.split('\t')
        if len(fields) < len(header):
            fields += [''] * (len(header) - len(fields))
        row = dict(zip(header, fields[:len(header) - 1] + ['\t'.join(fields[len(header) - 1:])]))
        try:
            level = int(row['level'])
            box = {key: int(row[key]) for key in ('left', 'top', 'width', 'height')}
        except (KeyError, ValueError):
            continue
        if level == 1:
            image = {'width': box['width'], 'height': box['height']}
        elif level == 5 and row.get('text', '').strip():
            try:
                confidence = float(row.get('conf', '-1'))
            except ValueError:
                confidence = -1.0
            words.append({'text': row['text'], 'box': box, 'engine_confidence': confidence,
                          'block': int(row.get('block_num') or 0), 'paragraph': int(row.get('par_num') or 0),
                          'line': int(row.get('line_num') or 0), 'word': int(row.get('word_num') or 0)})
    return {'image': image, 'words': words}


def _union(boxes: list[dict]) -> dict:
    left = min(b['left'] for b in boxes)
    top = min(b['top'] for b in boxes)
    right = max(b['left'] + b['width'] for b in boxes)
    bottom = max(b['top'] + b['height'] for b in boxes)
    return {'left': left, 'top': top, 'width': right - left, 'height': bottom - top}


def lines_from_words(words: list[dict]) -> list[dict]:
    """Group words by Tesseract block/paragraph/line in reading order; join with one space."""
    grouped: dict[tuple, list[dict]] = {}
    for word in words:
        grouped.setdefault((word['block'], word['paragraph'], word['line']), []).append(word)
    lines = []
    for key, members in grouped.items():
        lines.append({'block': key[0], 'paragraph': key[1], 'line': key[2],
                      'text': ' '.join(w['text'] for w in members), 'box': _union([w['box'] for w in members]),
                      'words': [{'text': w['text'], 'box': w['box'], 'engine_confidence': w['engine_confidence']}
                                for w in members]})
    return lines


class PopplerRasterizer:
    def __init__(self):
        self.pdftoppm = shutil.which('pdftoppm')
        self.pdfinfo = shutil.which('pdfinfo')

    def availability(self) -> dict:
        missing = [name for name, path in (('pdftoppm', self.pdftoppm), ('pdfinfo', self.pdfinfo)) if not path]
        if missing:
            return {'status': 'unavailable', 'engine': 'poppler-utils',
                    'reason': f'{", ".join(missing)} not found; scanned PDF pages cannot be rasterized here'}
        result = _run([self.pdftoppm, '-v'], timeout=30)
        text = (result.stderr or result.stdout).decode('utf-8', 'replace').strip().splitlines()
        return {'status': 'available', 'engine': 'poppler-utils', 'version': text[0] if text else 'unknown'}

    def page_count(self, pdf: bytes, timeout: float) -> int:
        result = _run([self.pdfinfo, '-'], data=pdf, timeout=timeout)
        match = re.search(rb'^Pages:\s+(\d+)\s*$', result.stdout, re.M)
        if result.returncode != 0 or not match:
            raise UnsupportedInput('pdfinfo could not read the PDF: ' + result.stderr.decode('utf-8', 'replace')[:500])
        return int(match.group(1))

    def rasterize(self, pdf: bytes, page: int, *, dpi: int, timeout: float, max_bytes: int) -> bytes:
        result = _run([self.pdftoppm, '-f', str(page), '-l', str(page), '-r', str(dpi), '-png', '-'],
                      data=pdf, timeout=timeout)
        if result.returncode != 0 or not result.stdout.startswith(b'\x89PNG'):
            raise RuntimeError(f'pdftoppm failed on page {page}: ' + result.stderr.decode('utf-8', 'replace')[:500])
        if len(result.stdout) > max_bytes:
            raise UnsupportedInput(f'page {page} raster exceeds max_page_image_bytes')
        return result.stdout


def _time_matches(line: dict) -> list[dict]:
    """Time-like strings in an OCR line with the boxes of the words they cover."""
    spans, position = [], 0
    for index, word in enumerate(line['words']):
        spans.append((position, position + len(word['text']), index))
        position += len(word['text']) + 1
    found = []
    for match in TIME_PATTERN.finditer(line['text']):
        covered = [i for start, end, i in spans if start < match.end() and end > match.start()]
        found.append({'text': match.group(0), 'span': [match.start(), match.end()],
                      'box': _union([line['words'][i]['box'] for i in covered]) if covered else line['box']})
    return found


def chat_candidates(pages: list[dict], *, gap_factor: float = 0.8) -> list[dict]:
    """Heuristic bubbles -> sender/time/text candidates with locators (never events)."""
    candidates = []
    for page in pages:
        lines = sorted(page['lines'], key=lambda line: (line['box']['top'], line['box']['left']))
        if not lines:
            continue
        width = page['image'].get('width')
        heights = [line['box']['height'] for line in lines if line['box']['height'] > 0]
        typical = statistics.median(heights) if heights else 0

        def side(box):
            if not width:
                return 'unknown'
            center = (box['left'] + box['width'] / 2) / width
            return 'left' if center < 0.45 else ('right' if center > 0.55 else 'center')
        groups: list[list[int]] = []
        for index, line in enumerate(lines):
            if groups:
                previous = lines[groups[-1][-1]]
                gap = line['box']['top'] - (previous['box']['top'] + previous['box']['height'])
                if gap <= gap_factor * typical and side(line['box']) == side(previous['box']):
                    groups[-1].append(index)
                    continue
            groups.append([index])
        for group in groups:
            members = [lines[i] for i in group]
            times = [match for line in members for match in _time_matches(line)]
            time = times[-1] if times else None
            sender = SENDER_PATTERN.match(members[0]['text'])
            body_lines = [line['text'] for line in members]
            if time is not None:
                owner = next(line for line in reversed(members) if time in _time_matches(line))
                position = members.index(owner)
                start, end = time['span']
                body_lines[position] = (owner['text'][:start] + owner['text'][end:]).strip()
            prefix = SENDER_PATTERN.match(body_lines[0]) if sender is not None else None
            if prefix is not None:
                body_lines[0] = body_lines[0][prefix.end(1) + 1:].strip()
            body = '\n'.join(line for line in body_lines if line)
            box = _union([line['box'] for line in members])
            candidates.append({
                'page': page['page'], 'sender_text': sender.group(1).strip() if sender else None,
                'time_text': time['text'] if time else None, 'body_text': body or None, 'layout_side': side(box),
                'locators': {'page': page['page'], 'unit': 'px', 'group_box': box,
                             'line_boxes': [line['box'] for line in members],
                             'time_box': time['box'] if time else None,
                             'sender_box': members[0]['words'][0]['box'] if sender else None},
                'metadata': {'raw_text': '\n'.join(line['text'] for line in members),
                             'time_candidates': [match['text'] for match in times],
                             'engine_confidence_min': min((w['engine_confidence'] for line in members for w in line['words']),
                                                          default=None),
                             'interpretation': 'layout heuristic; side is not identity; time text is a raw OCR '
                                               'string with unknown date and zone; not an event'},
            })
    return candidates


def run_ocr(job: dict, engine=None, *, rasterizer=None) -> int:
    record = recipe_from_job(job)
    effective = record['effective']
    built = engine is None
    engine = engine or TesseractCLI()
    artifact = load_artifact(job['artifact_id'])
    verification = verification_record()
    described = engine.describe()
    metadata = {'tool': described.get('tool', 'unknown'), 'tool_version': described.get('tool_version', 'unknown'),
                'provider': described.get('provider', 'local'), 'model': 'tesseract:' + '+'.join(effective['languages']),
                'model_version': described.get('tool_version', 'unknown'),
                'parameters': {'recipe_id': record['id'], 'recipe_sha256': record['sha256'], 'recipe': effective},
                'metadata': {'engine': described},
                'provenance': {'recipe': run_recipe_record(record, 'adapter_built_from_pinned_recipe' if built else
                                                           'injected_engine_with_pinned_recipe_parameters'),
                               **input_provenance(artifact, verification)}}
    update_run_metadata(job, metadata)
    availability = engine.availability(effective['languages'])
    metadata['metadata']['engine_availability'] = availability
    update_run_metadata(job, metadata)
    if availability.get('status') != 'available':
        raise AdapterUnavailable(f'OCR engine unavailable: {availability.get("reason", "unknown")}')
    limits = effective['limits']
    with open_verified(artifact) as reader:
        verification['initial_full_sha256'] = 'passed'
        data = read_all(reader, limits['max_input_bytes'])
        reader.verify_unchanged()
        verification['final_same_descriptor'] = 'passed'
    kind = detect_input(data)
    pages, coverage = [], {'status': 'complete'}
    options = {'languages': effective['languages'], 'psm': effective['psm'], 'oem': effective['oem'],
               'timeout': limits['timeout_s']}
    if kind == 'pdf':
        rasterizer = rasterizer or PopplerRasterizer()
        raster = rasterizer.availability()
        metadata['metadata']['rasterizer'] = raster
        if raster.get('status') != 'available':
            update_run_metadata(job, metadata)
            raise UnsupportedInput(f'scanned PDF requires rasterization; unsupported here: {raster.get("reason")}')
        count = rasterizer.page_count(data, limits['timeout_s'])
        selected = min(count, effective['pdf']['max_pages'])
        if selected < count:
            coverage = {'status': 'partial', 'pages_total': count, 'pages_processed': selected,
                        'reason': 'pdf.max_pages reached'}
        for number in range(1, selected + 1):
            image = rasterizer.rasterize(data, number, dpi=effective['pdf']['dpi'], timeout=limits['timeout_s'],
                                         max_bytes=limits['max_page_image_bytes'])
            pages.append({'page': number, 'source': 'pdf_raster', 'dpi': effective['pdf']['dpi'],
                          'text_layer': 'not_extracted', **engine.recognize(image, **options)})
    elif kind in IMAGE_KINDS:
        pages.append({'page': 1, 'source': f'image/{kind}', **engine.recognize(data, **options)})
    else:
        update_run_metadata(job, metadata)
        raise UnsupportedInput(f'unsupported OCR input type: {kind} (expected PDF or a raster image)')
    total_words = sum(len(page['words']) for page in pages)
    if total_words > limits['max_words']:
        raise UnsupportedInput(f'OCR produced {total_words} words; recipe limit max_words is {limits["max_words"]}')
    segments, page_texts, page_records = [], [], []
    for page in pages:
        page['lines'] = lines_from_words(page['words'])
        page_texts.append('\n'.join(line['text'] for line in page['lines']))
        page_records.append({'page': page['page'], 'source': page['source'], 'image': page['image'],
                             'lines': len(page['lines']), 'words': len(page['words']),
                             **({'dpi': page['dpi'], 'text_layer': page['text_layer']} if 'dpi' in page else {})})
        for index, line in enumerate(page['lines']):
            segments.append({'page': page['page'], 'line_index': index, 'block': line['block'],
                             'paragraph': line['paragraph'], 'text': line['text'], 'box': line['box'],
                             'box_unit': 'px', 'words': line['words']})
    candidates = chat_candidates(pages) if effective['layout'] == 'chat_screenshot' else []
    work = {'unit': 'page', 'amount': len(pages), 'basis': 'pages_recognized'}
    metadata['metadata']['work'] = work
    update_run_metadata(job, metadata)
    result_metadata = {
        'recipe_id': record['id'], 'recipe_sha256': record['sha256'], 'input_type': kind,
        'languages_requested': effective['languages'], 'layout': effective['layout'], 'pages': page_records,
        'coverage': coverage, 'text_join': 'words joined by one space; lines by newline; pages by form feed',
        'engine_confidence_meaning': CONFIDENCE_MEANING, 'ocr_correctness': 'unknown',
        'input_sha256': artifact['sha256'], 'input_verification': verification,
        'event_candidates': {'parser': CHAT_PARSER, 'count': len(candidates)} if candidates or effective['layout'] == 'chat_screenshot' else None,
    }

    def insert_candidates(db, derived_text_id):
        for index, candidate in enumerate(candidates):
            db.execute('''INSERT INTO event_candidates(artifact_id,derived_text_id,run_id,candidate_index,parser,
                          sender_text,time_text,body_text,layout_side,locators_json,metadata_json)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                       (job['artifact_id'], derived_text_id, job['run_id'], index, CHAT_PARSER, candidate['sender_text'],
                        candidate['time_text'], candidate['body_text'], candidate['layout_side'],
                        json.dumps(candidate['locators'], sort_keys=True),
                        json.dumps(candidate['metadata'], ensure_ascii=False, sort_keys=True)))
    return publish_derived_text(job, kind='ocr', text='\f'.join(page_texts), model=metadata['model'], language=None,
                                segments=segments, metadata=result_metadata, also=insert_candidates)
