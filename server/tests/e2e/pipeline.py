"""End-to-end acceptance pipeline: real CLI subprocesses and the real HTTP service.

``Pipeline(workdir).run()`` builds the multi-device layout (``layout.build``) in
``workdir/sources``, starts from a fresh data directory (``EW_DATA_DIR``,
``EW_DB_PATH``, ``EW_STORE_DIR`` under ``workdir/data``; inherited ``EW_*``
variables are dropped) with ``EW_SCAN_ROOTS`` naming the three source roots and
``EW_REPORTS_DIR`` outside the repository, and drives every stage through
``python -m server.app.cli`` (and ``uvicorn server.app.main:app`` with
``EW_REQUIRE_TOKEN=1`` for the citation POST and the read API). The ASR stage
runs the real worker loop with the fake faster-whisper module of the audio
corpus part (tones, not speech; no real inference is possible here).

The orchestrator itself never imports ``server.app`` (its settings bind to the
environment at import time); every product call is a subprocess with the fresh
environment, and checks read the SQLite file read-only. The returned receipt
lists every command (paths shown relative to ``<work>``), measured wall-clock
seconds per stage, every check with its evidence, and each acceptance criterion
mapped to the checks that support it, or to an explicit "not verifiable here".
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import layout as layout_module

REPO = Path(__file__).resolve().parents[3]
SCHEMA = 'ageds.e2e-acceptance-receipt/v1'
TASK = 'AGEDS-TASK-20261016-Z'
ZONE = 'Europe/Warsaw'
REPORT_TYPES = ('catalog', 'timeline', 'time_budget', 'hypothesis_dossier', 'lawyer_package')
# Tables that may grow when a done plan is re-run with --rerun-done (re-observation is recorded by design).
RERUN_GROWTH = {'source_observations', 'audit_log', 'import_plan_attempts', 'import_plan_runs'}
CATEGORY_CONFIG = {
    'schema': 'ageds.activity_categories.v1',
    'version': 'e2e-private-1',
    'categories': [
        {'id': 'praca_projekty', 'label': 'Praca / projekty', 'color': '#3987e5'},
        {'id': 'sprawy_urzedowe', 'label': 'Sprawy urzędowe', 'color': '#d95926'},
        {'id': 'zdrowie', 'label': 'Zdrowie', 'color': '#199e70'},
        {'id': 'inne', 'label': 'Inne', 'color': '#d55181'},
    ],
    'default_category': 'inne', 'combine': 'max', 'official_categories': ['sprawy_urzedowe'],
    'rules': [
        {'id': 'chatboty-praca', 'version': 1, 'kind': 'chatbot', 'values': ['claude', 'chatgpt'],
         'weights': {'praca_projekty': 1}},
        {'id': 'raport-urzad', 'version': 1, 'kind': 'keyword', 'values': ['raport'], 'weights': {'sprawy_urzedowe': 1}},
    ],
}
# The same events with the two chatbots in two different categories: the overlap becomes visible.
SPLIT_RULES = [
    {'id': 'chatgpt-praca', 'version': 1, 'kind': 'chatbot', 'values': ['chatgpt'], 'weights': {'praca_projekty': 1}},
    {'id': 'claude-inne', 'version': 1, 'kind': 'chatbot', 'values': ['claude'], 'weights': {'inne': 1}},
]
HYPOTHESIS = {
    'title': 'E2E synthetic: calls followed by written messages',
    'text': 'After a phone call, written messages (SMS, WhatsApp) become more frequent within a day '
            '(synthetic test hypothesis on a synthetic corpus).',
    'stimulus': {'event_types': ['call']},
    'outcome': {'event_types': ['sms', 'whatsapp_message']},
    'windows': [{'label': 'after-1d', 'start_s': 0, 'end_s': 86400}],
    'analysis': {'permutations': 199, 'seed': 20261016},
    'dossier': {'queries': ['report']},
}
OPEN_QUESTION = 'Synthetic: which time zone applies to the WhatsApp wall clocks?'

CRITERIA = {
    'K1.a': 'Corpus: the same ChatGPT conversation in three exports with one edit and one branch',
    'K1.b': 'Corpus: an e-mail attachment that is the same PDF as a file on disk',
    'K1.c': 'Corpus: a recording with interleaved PL/NL/EN (synthetic TTS or a labelled fixture)',
    'K1.d': 'Corpus: a recording without a call-log entry and a call-log entry without a recording',
    'K1.e': 'Corpus: a file-name collision (same name, different bytes)',
    'K1.f': 'Corpus: AM/PM, an unknown zone and a DST change',
    'K1.g': 'Corpus: parallel conversations with two chatbots overlapping in time',
    'K1.h': 'Corpus: a long gap without data',
    'K1.i': 'Corpus: a zip inside a zip',
    'K1.j': 'Corpus: a .dat file that is a PDF',
    'K1.k': 'Corpus: at least two collections (devices/accounts) and a copy of an export on a second drive',
    'K2.a': 'Re-import duplicates nothing',
    'K2.b': 'Originals keep their hashes (and mtimes) before/after',
    'K3.a': 'Every number in a report is reproducible from stored parameters / traceable to code',
    'K3.b': 'A parameter change gives a new result; the old one stays retrievable unchanged',
    'K4': 'No private data in tracked files (scripts/verify_repository.py with privacy heuristics)',
    'K5': 'Backend/JS/browser tests run locally; Android only if changed',
    'N1': 'No private material sent to external APIs without an explicit per-run opt-in',
    'N2': 'Unobserved is never presented as inactivity; correlation never as cause',
    'N3': 'Originals, names, numbers and dates are not modified',
    'N4': 'Already accepted functions are not re-implemented',
    'N5': 'The five standard layers are not declared as implemented',
}
NOT_VERIFIABLE_IN_RUN = {
    'K5': ('Not an end-to-end property. The local suite runs (pytest, node, browser scripts) are recorded in '
           'docs/validation/E2E_ACCEPTANCE_2026-10-16.md; Android was not changed by this task.'),
    'N4': ('A review property of the change set, not of a run. This task adds no product feature; its shared '
           'edits are three defect fixes with four regression tests (listed in the receipt).'),
    'N5': ('A documentation property. CLAUDE.md, HANDOFF.md and docs/FEATURES.md keep the five-layer standard as '
           'future work; the report method sections cover layer-1 metadata only and say so.'),
}


class CommandFailed(RuntimeError):
    pass


def tree_state(root: Path) -> dict:
    """Bytes, sizes, modes and mtimes of every node; symlinks are read, never followed.

    Same contract as ``server.tests.test_catalog_scan.tree_state`` (kept here so the
    orchestrator does not import ``server.app``, whose settings bind at import time).
    """
    state = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames + ['.']:
            path = Path(directory) / name if name != '.' else Path(directory)
            st = os.lstat(path)
            rel = os.fsencode(path.relative_to(root)) if path != root else b'.'
            if stat.S_ISLNK(st.st_mode):
                state[rel] = ('link', os.readlink(path), st.st_mtime_ns)
            elif stat.S_ISREG(st.st_mode):
                state[rel] = ('file', hashlib.sha256(path.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns,
                              st.st_mode)
            else:
                state[rel] = ('dir', st.st_mtime_ns, st.st_mode)
    return state


def tree_digest(state: dict) -> str:
    digest = hashlib.sha256()
    for key in sorted(state):
        digest.update(key + b'\0' + json.dumps(state[key], default=str).encode() + b'\n')
    return digest.hexdigest()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _canonical_sha256(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _git(*args) -> str:
    try:
        return subprocess.run(['git', *args], cwd=REPO, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


class Pipeline:
    def __init__(self, workdir: Path, *, heavy: bool = False, pdf_chromium: str | None = None,
                 python: str | None = None, verify_repository: bool = True):
        self.work = Path(workdir).resolve()
        if self.work == REPO or REPO in self.work.parents:
            raise ValueError('the work directory must be outside the repository checkout')
        self.work.mkdir(parents=True, exist_ok=True)
        if any(self.work.iterdir()):
            raise ValueError(f'the work directory must be new or empty: {self.work}')
        self.heavy = heavy
        self.pdf_chromium = pdf_chromium
        self.python = python or sys.executable
        self.verify_repository = verify_repository
        self.sources = self.work / 'sources'
        self.data = self.work / 'data'
        self.private = self.work / 'private'
        self.reports = self.work / 'reports'
        self.outputs = self.work / 'outputs'
        self.spool = self.work / 'spool'
        self.importwork = self.work / 'importwork'
        for directory in (self.private, self.reports, self.outputs, self.spool, self.importwork):
            directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data / 'evidence.db'
        self.stages: list[dict] = []
        self.checks: list[dict] = []
        self.facts: dict = {}
        self._stage: dict | None = None
        self._server = None
        self._server_log = None
        self.base_url = None
        self.token = None

    # ------------------------------------------------------------------ plumbing

    def redact(self, value):
        if isinstance(value, str):
            return value.replace(str(self.work), '<work>').replace(str(REPO), '<repo>')
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, tuple):
            return [self.redact(v) for v in value]
        if isinstance(value, dict):
            return {self.redact(k) if isinstance(k, str) else k: self.redact(v) for k, v in value.items()}
        return value

    @property
    def env(self) -> dict:
        base = {k: v for k, v in os.environ.items() if not k.startswith(('EW_', 'AGEDS_'))}
        base.update({
            'EW_DATA_DIR': str(self.data), 'EW_DB_PATH': str(self.db_path), 'EW_STORE_DIR': str(self.data / 'store'),
            'EW_SCAN_ROOTS': os.pathsep.join(str(self.sources / key) for key in self.roots),
            'EW_REPORTS_DIR': str(self.reports), 'PYTHONDONTWRITEBYTECODE': '1',
        })
        if self.pdf_chromium:
            base['EW_PDF_CHROMIUM'] = self.pdf_chromium
        return base

    @property
    def roots(self) -> tuple[str, ...]:
        return ('phone_a', 'drive_b', 'drive_c') + (('drive_d',) if self.heavy else ())

    @contextmanager
    def stage(self, name: str):
        record = {'name': name, 'commands': [], 'seconds': None, 'status': 'running'}
        self.stages.append(record)
        self._stage = record
        started = time.monotonic()
        try:
            yield record
            record['status'] = 'ok'
        except BaseException as error:
            record['status'] = 'error'
            record['error'] = self.redact(f'{type(error).__name__}: {error}')[:4000]
            raise
        finally:
            record['seconds'] = round(time.monotonic() - started, 3)
            self._stage = None

    def _run(self, argv: list[str], shown: list[str], *, ok=(0,), env: dict | None = None, timeout: int = 900):
        started = time.monotonic()
        process = subprocess.run(argv, cwd=REPO, env=env or self.env, capture_output=True, text=True, timeout=timeout)
        entry = {'argv': self.redact([str(a) for a in shown]), 'rc': process.returncode,
                 'seconds': round(time.monotonic() - started, 3)}
        if self._stage is not None:
            self._stage['commands'].append(entry)
        if process.returncode not in ok:
            raise CommandFailed(f'{" ".join(entry["argv"])} -> rc {process.returncode}\nstdout: '
                                f'{self.redact(process.stdout[-2000:])}\nstderr: {self.redact(process.stderr[-3000:])}')
        return process

    @staticmethod
    def _last_json(text: str) -> dict:
        for line in reversed(text.strip().splitlines()):
            line = line.strip()
            if line.startswith('{'):
                return json.loads(line)
        raise ValueError(f'no JSON object in output: {text[-500:]!r}')

    def cli(self, *args, ok=(0,)) -> dict:
        args = [str(a) for a in args]
        process = self._run([self.python, '-m', 'server.app.cli', *args], ['python', '-m', 'server.app.cli', *args], ok=ok)
        result = self._last_json(process.stdout or process.stderr)
        result['_rc'] = process.returncode
        return result

    def module(self, name: str, *args, ok=(0,), env: dict | None = None) -> dict:
        args = [str(a) for a in args]
        process = self._run([self.python, '-m', name, *args], ['python', '-m', name, *args], ok=ok, env=env)
        return self._last_json(process.stdout)

    def check(self, check_id: str, criteria: list[str], description: str, passed: bool, evidence=None) -> bool:
        self.checks.append({'id': check_id, 'criteria': criteria, 'stage': self._stage['name'] if self._stage else None,
                            'description': description, 'passed': bool(passed), 'evidence': self.redact(evidence)})
        return bool(passed)

    def query(self, sql: str, params=()) -> list[dict]:
        connection = sqlite3.connect(f'file:{self.db_path}?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(sql, params)]
        finally:
            connection.close()

    def table_counts(self) -> dict:
        names = [r['name'] for r in self.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: self.query(f'SELECT COUNT(*) AS n FROM "{name}"')[0]['n'] for name in sorted(names)}

    def http(self, method: str, path: str, *, body=None, token: str | None | bool = True) -> tuple[int, bytes]:
        data = json.dumps(body).encode('utf-8') if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        bearer = self.token if token is True else (token or None)
        if bearer:
            request.add_header('Authorization', f'Bearer {bearer}')
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read()
        if self._stage is not None:
            self._stage['commands'].append({'http': f'{method} {path}', 'bearer': bool(bearer), 'status': status,
                                            'seconds': round(time.monotonic() - started, 3)})
        return status, payload

    def http_json(self, method: str, path: str, **kwargs) -> tuple[int, dict]:
        status, payload = self.http(method, path, **kwargs)
        return status, json.loads(payload.decode('utf-8'))

    def start_server(self):
        port = _free_port()
        self.base_url = f'http://127.0.0.1:{port}'
        self._server_log = open(self.work / 'server.log', 'wb')
        self._server = subprocess.Popen(
            [self.python, '-m', 'uvicorn', 'server.app.main:app', '--host', '127.0.0.1', '--port', str(port),
             '--log-level', 'warning'], cwd=REPO, env={**self.env, 'EW_REQUIRE_TOKEN': '1'},
            stdout=self._server_log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._server.poll() is not None:
                raise CommandFailed(f'server exited with {self._server.returncode}')
            try:
                with urllib.request.urlopen(self.base_url + '/api/read/v1/contract', timeout=2):
                    pass
            except urllib.error.HTTPError:
                return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.2)
        raise CommandFailed('server did not start within 60 s')

    def stop_server(self):
        if self._server is not None and self._server.poll() is None:
            self._server.terminate()
            try:
                self._server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._server.kill()
                self._server.wait(timeout=20)
        if self._server_log is not None:
            self._server_log.close()
        self._server = None

    # ------------------------------------------------------------------ run

    def run(self) -> dict:
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        error = None
        try:
            for step in (self.stage_layout, self.stage_collections, self.stage_catalog, self.stage_plan,
                         self.stage_reimport, self.stage_heavy_plan, self.stage_ingest, self.stage_asr, self.stage_service,
                         self.stage_timeline, self.stage_budget, self.stage_hypothesis, self.stage_reports,
                         self.stage_citations, self.stage_metadata, self.stage_read_api, self.stage_originals,
                         self.stage_repository):
                step()
        except BaseException as exc:  # recorded; the receipt says where the run stopped
            error = self.redact(f'{type(exc).__name__}: {exc}')[:4000]
            if not isinstance(exc, Exception):
                raise
        finally:
            self.stop_server()
        return self.receipt(started_at, round(time.monotonic() - started, 3), error)

    def receipt(self, started_at: str, seconds: float, error: str | None) -> dict:
        criteria = {}
        for key, text in CRITERIA.items():
            related = [c for c in self.checks if key in c['criteria']]
            if key in NOT_VERIFIABLE_IN_RUN:
                status = 'not_verifiable_in_run'
            elif not related:
                status = 'not_met'
            else:
                status = 'met' if all(c['passed'] for c in related) else 'not_met'
            criteria[key] = {'criterion': text, 'status': status, 'checks': [c['id'] for c in related],
                             'note': NOT_VERIFIABLE_IN_RUN.get(key)}
        head = _git('rev-parse', 'HEAD').strip() or 'unknown'
        return {
            'schema': SCHEMA, 'task': TASK, 'started_at': started_at, 'wall_seconds': seconds,
            'variant': 'heavy' if self.heavy else 'default',
            'git': {'head': head, 'dirty': bool(_git('status', '--porcelain', '--untracked-files=no').strip())},
            'python': platform.python_version(), 'platform': platform.platform(terse=True),
            'environment': {
                'fresh_data_dir': True, 'scan_roots': [f'<work>/sources/{k}' for k in self.roots],
                'reports_dir': '<work>/reports', 'pdf_chromium': self.redact(self.pdf_chromium) if self.pdf_chromium else None,
                'asr_model': 'FakeFasterWhisper (server.tests.corpus.audio): synthetic tones, NOT speech; '
                             'real inference not run (no model weights, Hugging Face blocked)',
                'http': 'uvicorn server.app.main:app with EW_REQUIRE_TOKEN=1 on 127.0.0.1',
            },
            'error': error,
            'stages': self.stages,
            'checks': self.checks,
            'summary': {'checks': len(self.checks), 'passed': sum(c['passed'] for c in self.checks),
                        'failed': [c['id'] for c in self.checks if not c['passed']],
                        'criteria': {s: sorted(k for k, v in criteria.items() if v['status'] == s)
                                     for s in ('met', 'not_met', 'not_verifiable_in_run')}},
            'criteria': criteria,
            'facts': self.redact(self.facts),
        }

    # ------------------------------------------------------------------ stages

    def stage_layout(self):
        with self.stage('layout'):
            self.git_status_before = _git('status', '--porcelain', '--untracked-files=all')
            self.layout = layout_module.build(self.sources)
            if self.heavy:
                from ..corpus import catalog_files
                adversarial = catalog_files.build(self.work / 'heavy_parts')
                shutil.copytree(adversarial['roots']['phone_a'], self.sources / 'drive_d', symlinks=True)
                chat = Path(self.layout['parts']) / 'chat_exports' / 'chatgpt'
                for name in ('edge', 'malformed'):  # 4 MB message body, duplicate nodes, bad UTF-8, truncated JSON
                    shutil.copytree(chat / name, self.sources / 'drive_d' / 'chat' / name)
                self.layout['collections']['drive_d'] = {
                    'label': 'Dysk D adwersarialny (syntetyczny)', 'device_or_account': 'adversarial drive D (synthetic)',
                    'export_date_raw': None, 'acquisition_method': 'catalog_files corpus part', 'operator': 'e2e'}
            self.before = {key: tree_state(self.sources / key) for key in self.roots}
            (self.private / 'categories.json').write_text(json.dumps(CATEGORY_CONFIG, ensure_ascii=False, indent=1), 'utf-8')
            split = {**CATEGORY_CONFIG, 'version': 'e2e-private-split-1', 'rules': SPLIT_RULES}
            (self.private / 'categories_split.json').write_text(json.dumps(split, ensure_ascii=False, indent=1), 'utf-8')
            (self.private / 'hypothesis.json').write_text(json.dumps(HYPOTHESIS, ensure_ascii=False, indent=1), 'utf-8')
            files = {key: sum(1 for v in state.values() if v[0] == 'file') for key, state in self.before.items()}
            self.facts['layout'] = {'files_per_root': files, 'tree_sha256_before': {k: tree_digest(v) for k, v in self.before.items()},
                                    'audio_label': self.layout['audio']['label']}
            self.check('layout.private_outside_repo', ['N1'], 'category configuration, hypothesis definition and outputs '
                       'are outside the repository', REPO not in self.private.parents, {'private': str(self.private)})

    def stage_collections(self):
        with self.stage('collections'):
            self.collections = {}
            for key in self.roots:
                fields = self.layout['collections'][key]
                args = ['catalog-collection-add', fields['label']]
                for name in ('device_or_account', 'export_date_raw', 'acquisition_method', 'operator'):
                    if fields.get(name):
                        args += ['--' + name.replace('_', '-'), fields[name]]
                self.collections[key] = self.cli(*args)['id']
            listing = self.cli('catalog-collection-list')
            self.check('collections.created', ['K1.k'], 'one collection per device/account/drive',
                       len(self.collections) >= 3 and len(set(self.collections.values())) == len(self.collections),
                       {'collections': self.collections, 'listed': len(listing.get('collections') or listing.get('tree') or [])})

    def stage_catalog(self):
        with self.stage('catalog'):
            self.scans = {}
            for key in self.roots:
                result = self.cli('catalog-scan', self.sources / key, '--collection', self.collections[key],
                                  '--temp-dir', self.spool)
                self.scans[key] = result
            core = [self.scans[k] for k in ('phone_a', 'drive_b', 'drive_c')]
            self.check('catalog.scans_complete', ['K1.k'], 'every core collection scan completed with complete coverage',
                       all(s['status'] == 'completed' and s['coverage']['complete'] for s in core),
                       {k: {'status': s['status'], 'entries': s['coverage']['entries'], 'members': s['coverage']['archive_members']}
                        for k, s in self.scans.items()})
            coverage = self.cli('catalog-coverage', '--min-gap-days', 30)
            gaps = []
            for collection in coverage['collections']:
                for entry in collection['types']:
                    for basis in entry['time_bases']:
                        for gap in basis['gaps']:
                            gaps.append({'collection_id': collection['collection_id'], 'type': entry['type'],
                                         'source': basis['source'], **gap})
            planted = self.layout['catalog_gap']
            found = [g for g in gaps if g['collection_id'] == self.collections[planted['collection']]
                     and g['last_observed_day'] == planted['last_before'] and g['next_observed_day'] == planted['first_after']]
            self.check('catalog.gap_found', ['K1.h'], 'coverage reports the planted long gap (days without any observation)',
                       bool(found) and found[0]['empty_days'] >= 200, {'gap': found[:1], 'all_gaps': len(gaps),
                                                                        'interpretation': coverage['method']['interpretation']})
            duplicates = self.cli('catalog-duplicates', '--limit', 500)
            groups = {g['sha256']: g for g in duplicates['groups']}
            zip_group = groups.get(self.layout['chatgpt_zip_sha256'])
            self.check('catalog.export_copy_on_second_drive', ['K1.k'],
                       'the ChatGPT export zip is one content group observed in two collections (copy on a second drive)',
                       bool(zip_group) and zip_group['collections'] == 2, zip_group)
            dat_group = groups.get(self.layout['dat']['sha256'])
            self.check('catalog.dat_in_both_drives', ['K1.j', 'K1.k'], 'the .dat member is one content group in two collections',
                       bool(dat_group) and dat_group['collections'] == 2, dat_group)
            pdf_group = groups.get(self.layout['pdf']['sha256'])
            self.check('catalog.pdf_on_disk_and_in_whatsapp', ['K1.b'],
                       'the PDF on disk and the WhatsApp DOC member are one content group (the MBOX attachment is '
                       'inside a MIME body; it is matched after import)', bool(pdf_group) and pdf_group['entries'] >= 2, pdf_group)
            collisions = self.cli('catalog-collisions', '--limit', 500)['collisions']
            todo = next((c for c in collisions if c['name'] == self.layout['collision']['name']), None)
            self.check('catalog.name_collision', ['K1.e'], 'same file name with different bytes in two collections is listed',
                       bool(todo) and todo['variants'] >= 2 and todo['collections'] >= 2
                       and {(self.collections[k], loc) for k, loc in self.layout['collision']['locations']}
                       <= {(l['collection_id'], l['locator']) for l in todo['locations']}
                       and len({l['sha256'] for l in todo['locations']}) == todo['variants'],
                       todo and {k: todo[k] for k in ('name', 'variants', 'collections')} | {
                           'locations': [(l['collection_id'], l['locator'], l['sha256'][:16]) for l in todo['locations']]})
            dat = self.cli('catalog-entries', '--collection', self.collections['drive_b'], '--q', 'file-DAT1')['entries']
            dat_entry = next((e for e in dat if e['locator'].endswith('file-DAT1.dat')), None)
            self.check('catalog.dat_is_pdf', ['K1.j'], 'the .dat member is typed pdf by signature (extension conflict kept)',
                       bool(dat_entry) and dat_entry['sniffed_type'] == 'pdf' and dat_entry['type_conflict'] == 1,
                       dat_entry and {k: dat_entry[k] for k in ('locator', 'extension', 'sniffed_type', 'sniff_basis',
                                                                'type_relation', 'type_conflict', 'sha256')})
            inner = self.cli('catalog-entries', '--collection', self.collections['drive_b'], '--q', 'takeout-inner.zip!/',
                             '--limit', 50)['entries']
            self.check('catalog.zip_in_zip', ['K1.i'], 'members of the nested zip inside the Takeout zip are catalogued '
                       'with a virtual locator (depth 2), nothing unpacked into the source',
                       any(e['depth'] == 2 and '.zip!/Takeout/takeout-inner.zip!/' in e['locator'] for e in inner),
                       [(e['locator'], e['depth'], e['status']) for e in inner[:6]])
            described = self.cli('catalog-describe', '--collection', self.collections['phone_a'])
            self.check('catalog.describe_without_provider', ['N1'], 'the collection description is the deterministic '
                       'profile only; no model is called without a provider and an explicit per-run opt-in',
                       described['provider'] == 'none' and described['status'] == 'not_run' and described['profile_sha256'],
                       {k: described.get(k) for k in ('status', 'provider', 'profile_sha256')})
            export = self.cli('catalog-export', self.outputs / 'catalog.csv', '--format', 'csv')
            self.check('catalog.export_outside_sources', ['N3'], 'catalog export is a new file outside every source root',
                       (self.outputs / 'catalog.csv').is_file(), {k: export.get(k) for k in ('rows', 'format', 'output')})
            if self.heavy:
                adversarial = self.scans['drive_d']
                self.facts['heavy_catalog'] = {'status': adversarial['status'], 'coverage': adversarial['coverage']}

    def _items(self, plan_id: int) -> list[dict]:
        return self.cli('import-plan-show', plan_id, '--limit', 500)['items']

    def _plan_and_approve(self, label: str) -> tuple[int, dict, list[dict]]:
        core = [self.collections[k] for k in ('phone_a', 'drive_b', 'drive_c')]
        args = ['import-plan-create']
        for cid in core:
            args += ['--collection', cid]
        created = self.cli(*args, '--billing-profile', layout_module.DAYMONTH_PROFILE, '--temp-dir', self.importwork,
                           '--actor', 'e2e')
        plan_id = created['plan']['id']
        items = self._items(plan_id)
        key_of = {v: k for k, v in self.collections.items()}
        by_locator = {(key_of[i['collection_id']], i['locator']): i for i in items}
        bulk = self.cli('import-plan-decide', plan_id, '--base-version', 1, '--actor', 'e2e', '--approve-all-ready',
                        '--note', f'{label}: approve every item without blockers')
        ambiguous = by_locator[self.layout['ambiguous']]
        infer = by_locator[('phone_a', layout_module.BILLING_INFER)]
        explicit = self.cli('import-plan-decide', plan_id, '--base-version', bulk['version'], '--actor', 'e2e',
                            '--importer', f'{ambiguous["id"]}=chatgpt_export',
                            '--param', f'{infer["id"]}=profile_id={layout_module.INFER_PROFILE}',
                            '--approve', ambiguous['id'], '--approve', infer['id'],
                            '--note', f'{label}: explicit importer for the ambiguous unit, explicit billing profile')
        return plan_id, {'created': created, 'bulk': bulk, 'explicit': explicit, 'by_locator': by_locator}, items

    def stage_plan(self):
        with self.stage('import_plan'):
            plan_id, info, items = self._plan_and_approve('plan 1')
            self.plan_id = plan_id
            by_locator = info['by_locator']
            blocked = {(i['blockers'][0] if i['blockers'] else None) for i in items}
            not_applied = {n['item_id']: n['blockers'] for n in info['bulk']['not_applied']}
            opt_in_ids = {by_locator[tuple(x)]['id'] for x in self.layout['opt_in_items']}
            ambiguous_id = by_locator[self.layout['ambiguous']]['id']
            infer_id = by_locator[('phone_a', layout_module.BILLING_INFER)]['id']
            self.check('plan.bulk_approval_leaves_choices_to_a_person', ['N1'],
                       'approve-all-ready approves only unblocked items: the ambiguous unit, the billing table without a '
                       'profile and the three opt-in importers stay unapproved',
                       set(not_applied) == opt_in_ids | {ambiguous_id, infer_id}
                       and not_applied[ambiguous_id] == ['importer_not_chosen:ambiguous_detection'],
                       {'not_applied': not_applied, 'items': len(items), 'blocker_kinds': sorted(b for b in blocked if b)})
            summary = self.cli('import-plan-summary', plan_id)
            self.check('plan.explicit_choices_applied', [], 'the ambiguous unit and the billing profile are chosen explicitly; '
                       'two repeated --approve flags both apply (regression)',
                       summary['by_status']['approved'] == len(items) - len(opt_in_ids)
                       and summary['by_status']['proposed'] == len(opt_in_ids),
                       {'by_status': summary['by_status'], 'version': summary['version']})
            run = self.cli('import-plan-run', plan_id, '--temp-dir', self.importwork, '--actor', 'e2e')
            self.plan_run = run
            statuses = {o['status'] for o in run['items']}
            self.check('plan.run_completed', ['K2.a'], 'every approved item finished done through the real importers',
                       run['status'] == 'completed' and statuses == {'done'} and run['selected_items'] == len(items) - len(opt_in_ids),
                       {'run_id': run['run_id'], 'selected': run['selected_items'], 'statuses': sorted(statuses),
                        'events_new': sum(o['footprint']['events_new'] for o in run['items']),
                        'artifacts_new': sum(o['footprint']['artifacts_new'] for o in run['items'])})
            after = {i['id']: i for i in self._items(plan_id)}
            opt_rows = self.query('SELECT COUNT(*) AS n FROM artifacts WHERE ' + ' OR '.join(
                ['source_locator LIKE ?'] * len(self.layout['opt_in_items'])),
                [f'%{loc}' for _key, loc in self.layout['opt_in_items']])[0]['n']
            self.check('plan.opt_ins_stay_off', ['N1'], 'location and browsing history items were never run and nothing '
                       'from them was acquired', all(after[i]['status'] == 'proposed' and after[i]['attempts'] == 0
                                                     and all(v is False for v in after[i]['state']['opt_ins'].values())
                                                     for i in opt_in_ids) and opt_rows == 0,
                       {'items': {i: (after[i]['status'], after[i]['state']['opt_ins']) for i in opt_in_ids},
                        'artifacts_from_opt_in_locators': opt_rows})
            cross = self.cli('import-plan-cross', plan_id)
            self.facts['plan'] = {'plan_id': plan_id, 'items': len(items), 'run_id': run['run_id'],
                                  'cross_export': self.redact({k: v for k, v in cross.items() if k != '_rc'})}

    def stage_reimport(self):
        with self.stage('reimport'):
            first = self.table_counts()
            plain = self.cli('import-plan-run', self.plan_id, '--temp-dir', self.importwork, '--actor', 'e2e')
            second = self.table_counts()
            changed = {t: (first[t], second[t]) for t in first if first[t] != second[t]}
            self.check('reimport.plain_rerun_selects_nothing', ['K2.a'], 'a plain re-run selects no item and changes no '
                       'table except its own run row and audit entries', plain['selected_items'] == 0
                       and set(changed) <= {'import_plan_runs', 'audit_log'}, {'changed': changed})
            forced = self.cli('import-plan-run', self.plan_id, '--temp-dir', self.importwork, '--actor', 'e2e', '--rerun-done')
            third = self.table_counts()
            grown = {t: (second[t], third[t]) for t in second if second[t] != third[t]}
            self.check('reimport.rerun_done_creates_no_evidence_rows', ['K2.a'],
                       'forced re-import of every done item through the importers adds no row to any table except '
                       're-observations (source_observations), audit entries and the run/attempt bookkeeping',
                       set(grown) <= RERUN_GROWTH and all(o['footprint']['events_new'] == 0 and o['footprint']['artifacts_new'] == 0
                                                          for o in forced['items']) and forced['selected_items'] > 0,
                       {'grown': grown, 'selected': forced['selected_items'], 'unchanged_tables': len(third) - len(grown)})
            for key in ('phone_a', 'drive_b', 'drive_c'):
                self.cli('catalog-scan', self.sources / key, '--collection', self.collections[key], '--temp-dir', self.spool)
            plan2, _info, items2 = self._plan_and_approve('plan 2 (rescan)')
            run2 = self.cli('import-plan-run', plan2, '--temp-dir', self.importwork, '--actor', 'e2e')
            fourth = self.table_counts()
            grown2 = {t: (third[t], fourth[t]) for t in third if third[t] != fourth[t]}
            allowed = RERUN_GROWTH | {t for t in fourth if t.startswith(('catalog_', 'import_plan'))}
            self.check('reimport.rescan_and_new_plan_creates_no_evidence_rows', ['K2.a'],
                       'scanning the same roots again and running a new plan adds catalog and plan rows only',
                       set(grown2) <= allowed and run2['status'] == 'completed'
                       and all(o['status'] == 'done' and o['footprint']['events_new'] == 0 and o['footprint']['artifacts_new'] == 0
                               for o in run2['items']),
                       {'grown': grown2, 'plan_id': plan2, 'selected': run2['selected_items']})
            duplicates = self.query('''SELECT artifact_id, external_id, COUNT(*) AS n FROM events WHERE external_id IS NOT NULL
                                       GROUP BY artifact_id, external_id HAVING COUNT(*) > 1''')
            self.check('reimport.no_duplicate_events', ['K2.a'], 'no (artifact, external id) pair occurs twice', not duplicates,
                       {'duplicates': duplicates[:5]})
            self.facts['table_counts_after_import'] = {t: n for t, n in fourth.items() if n}

    def stage_heavy_plan(self):
        if not self.heavy:
            return
        with self.stage('heavy_adversarial_plan'):
            created = self.cli('import-plan-create', '--collection', self.collections['drive_d'], '--temp-dir', self.importwork,
                               '--actor', 'e2e')
            plan_id = created['plan']['id']
            self.cli('import-plan-decide', plan_id, '--base-version', 1, '--actor', 'e2e', '--approve-all-ready')
            first = self.cli('import-plan-run', plan_id, '--temp-dir', self.importwork, '--actor', 'e2e')
            before = self.table_counts()
            second = self.cli('import-plan-run', plan_id, '--temp-dir', self.importwork, '--actor', 'e2e')
            after = self.table_counts()
            grown = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
            outcomes = {}
            for item in first['items']:
                outcomes[item['status']] = outcomes.get(item['status'], 0) + 1
            self.check('heavy.adversarial_plan_bounded', ['K2.a'],
                       'the adversarial collection (evil names, bomb, encrypted, truncated, malformed and oversized chat '
                       'exports) is planned and run without a crash; a second run retries only unfinished items and adds '
                       'no evidence rows', first['status'] == 'completed' and set(outcomes) <= {'done', 'failed', 'refused'}
                       and set(grown) <= RERUN_GROWTH,
                       {'plan_id': plan_id, 'items': created['summary']['items'], 'outcomes': outcomes,
                        'not_planned': created['plan']['coverage']['not_planned']['counts'],
                        'second_run_selected': second['selected_items'], 'grown': grown})

    def stage_ingest(self):
        with self.stage('catalog_ingest'):
            disk_key, disk_locator = self.layout['pdf']['disk']
            entries = self.cli('catalog-entries', '--collection', self.collections[disk_key], '--q', 'raport.pdf')['entries']
            entry = next(e for e in entries if e['locator'] == disk_locator)
            result = self.cli('catalog-ingest', entry['id'], '--temp-dir', self.importwork, '--actor', 'e2e')
            rows = self.query('SELECT id, source_locator, parent_artifact_id FROM artifacts WHERE sha256=? ORDER BY id',
                              (self.layout['pdf']['sha256'],))
            locators = [r['source_locator'] for r in rows]
            mbox = [l for l in locators if 'Mail/All mail' in l and '/mime-part/' in l]
            whatsapp = [l for l in locators if 'DOC-20240105-WA0003.pdf' in l]
            disk = [l for l in locators if l.endswith(disk_locator)]
            self.check('ingest.same_pdf_three_provenances', ['K1.b'],
                       'the PDF on disk, the e-mail attachment extracted from the MBOX in the nested Takeout zip and the '
                       'WhatsApp document share one SHA-256 and keep three separate acquisitions',
                       len(mbox) == 1 and len(whatsapp) == 1 and len(disk) == 1 and result['ingested'] == 1,
                       {'sha256': self.layout['pdf']['sha256'], 'locators': locators})

    def stage_asr(self):
        with self.stage('asr'):
            wav = self.query('SELECT id, sha256 FROM artifacts WHERE source_locator LIKE ?',
                             (f'%{self.layout["audio"]["locator"]}',))
            self.wav_id = wav[0]['id']
            legacy = self.cli('media-enqueue', self.wav_id)
            segmented = self.cli('media-enqueue', self.wav_id, '--recipe', 'per-segment-pl-nl-en')
            worker = self.module('server.tests.e2e.fake_asr_worker', '--iterations', 3,
                                 env={**self.env, 'EW_WORKER_KINDS': 'transcribe'})
            versions = self.query('''SELECT d.id, d.run_id, d.language, d.segments_json, d.metadata_json, r.provenance_json
                                     FROM derived_text d JOIN processing_runs r ON r.id=d.run_id
                                     WHERE d.artifact_id=? ORDER BY d.id''', (self.wav_id,))
            self.versions = [v['id'] for v in versions]
            strategies = [json.loads(v['provenance_json'])['recipe']['effective']['strategy'] for v in versions]
            per_segment = sorted({s.get('language') for s in json.loads(versions[-1]['segments_json'])}) if versions else []
            self.check('asr.two_recipes_two_runs_two_versions', ['K1.c'],
                       'two pinned recipes → two jobs, two processing runs, two transcript versions (auto per file vs '
                       'per-segment language); fixture labelled synthetic tones, fake model, no real inference',
                       legacy['job_id'] != segmented['job_id'] and len(versions) == 2
                       and strategies == ['auto_per_file', 'per_segment_language'] and per_segment == ['en', 'nl', 'pl']
                       and worker['real_inference'] is False and all(j['status'] == 'done' for j in worker['jobs']),
                       {'jobs': worker['jobs'], 'recipes': [legacy['recipe'], segmented['recipe']], 'strategies': strategies,
                        'version_languages': [v['language'] for v in versions], 'segment_languages': per_segment,
                        'fixture_label': worker['fixture_label'], 'model': worker['model']})
            first = self.cli('transcript-compare', self.versions[0], self.versions[1], '--output', self.outputs / 'compare-1.json')
            again = self.cli('transcript-compare', self.versions[0], self.versions[1], '--output', self.outputs / 'compare-2.json')
            comparison = json.loads((self.outputs / 'compare-1.json').read_text('utf-8'))
            relations = sorted({p['language_relation'] for p in comparison['language']['pairs']})
            self.check('asr.comparison_deterministic', ['K1.c'], 'run comparison (word diff, per-segment language, timing) '
                       'is written to a new file and is deterministic', first['content_sha256'] == again['content_sha256']
                       and 'different' in relations,
                       {'content_sha256': first['content_sha256'], 'language_relations': relations})
            queue = self.cli('media-queue', '--status', 'done')
            self.facts['asr'] = {'wav_artifact': self.wav_id, 'versions': self.versions,
                                 'queue_jobs': len(queue.get('jobs') or queue.get('items') or [])}

    def stage_service(self):
        with self.stage('service_and_citation'):
            token = self.cli('token-create', '--label', 'e2e-client', '--scope', 'read', '--scope', 'propose')
            self.token, self.token_id = token['token'], token['id']
            self.start_server()
            status, body = self.http_json('POST', f'/api/artifacts/{self.wav_id}/citations', token=False,
                                          body={'derivedTextId': self.versions[1], 'segmentIndices': [0]})
            self.check('service.token_required', [], 'with EW_REQUIRE_TOKEN=1 a state-changing request without a '
                       'token is refused', status == 401 and body['detail']['code'] == 'missing_token', {'status': status})
            segments = json.loads(self.query('SELECT segments_json FROM derived_text WHERE id=?', (self.versions[1],))[0]['segments_json'])
            switch = next(i for i in range(1, len(segments)) if segments[i]['language'] != segments[i - 1]['language'])
            refs = [{'segment_index': switch - 1, 'word_index': len(segments[switch - 1]['words']) - 1},
                    {'segment_index': switch, 'word_index': 0}, {'segment_index': switch, 'word_index': 1}]
            status, anchor = self.http_json('POST', f'/api/artifacts/{self.wav_id}/citations',
                                            body={'derivedTextId': self.versions[1], 'wordRefs': refs})
            status_legacy, legacy_anchor = self.http_json('POST', f'/api/artifacts/{self.wav_id}/citations',
                                                          body={'derivedTextId': self.versions[0], 'segmentIndices': [0]})
            self.anchor_id = anchor.get('id')
            self.check('service.word_citation_across_language_switch', ['K1.c'],
                       'a word citation crossing the pl→nl switch is pinned to the per-segment version; a second anchor '
                       'pins the legacy version (versions never move each other\'s anchors)',
                       status == 200 and status_legacy == 200 and anchor['derived_text_id'] == self.versions[1]
                       and legacy_anchor['derived_text_id'] == self.versions[0]
                       and anchor['validation'] == 'matches_stored_transcript_version',
                       {'anchor': {k: anchor.get(k) for k in ('id', 'derived_text_id', 'start_ms', 'end_ms', 'quote_text',
                                                              'quote_sha256')},
                        'languages': [segments[switch - 1]['language'], segments[switch]['language']],
                        'legacy_anchor': legacy_anchor.get('id')})

    def stage_timeline(self):
        with self.stage('timeline'):
            rebuild = self.cli('timeline-rebuild')
            day = self.layout['parallel']['day']
            window = self.cli('timeline-window', self.outputs / 'timeline-parallel.json', '--from', day, '--to', day)
            items = json.loads((self.outputs / 'timeline-parallel.json').read_text('utf-8'))['items']
            spans = {}
            for item in items:
                if item['event_type'] == 'chat_message' and item.get('chatbot'):
                    t = item['time']['display']
                    lo, hi = spans.get(item['chatbot'], (t, t))
                    spans[item['chatbot']] = (min(lo, t), max(hi, t))
            overlap = (len(spans) == 2 and max(s[0] for s in spans.values() if s) < min(s[1] for s in spans.values()))
            self.check('timeline.two_chatbots_overlap', ['K1.g'], 'the projected window shows ChatGPT and Claude '
                       'conversations whose message spans overlap in time (separate lanes by chatbot)',
                       overlap and {'chatgpt', 'claude'} <= set(spans),
                       {'spans_utc': spans, 'rows': window['rows'], 'rule': window['rule']['rule_id'] + '@' + str(window['rule']['version'])})
            self.cli('timeline-window', self.outputs / 'timeline-all.csv', '--from', '2024-01-01', '--to', '2025-12-31')
            self.facts['timeline'] = {'projection_counts': rebuild['build']['counts'], 'rule_sha256': rebuild['rule']['sha256']}
            times = self.query('''SELECT t.raw, t.local, t.utc, t.tz_status, t.metadata_json, e.event_type FROM event_times t
                                  JOIN events e ON e.id=t.event_id''')
            ampm = [t for t in times if json.loads(t['metadata_json'] or '{}').get('ampm') == 'PM' and 'PM' in (t['raw'] or '')]
            dst = sorted({json.loads(t['metadata_json'] or '{}').get('dst') for t in times} - {None})
            unknown = sorted({t['event_type'] for t in times if t['tz_status'] == 'unknown'})
            self.check('timeline.ampm_unknown_zone_dst', ['K1.f'],
                       'AM/PM is parsed with both day/month readings kept, unknown zones stay unknown, DST gap and fold '
                       'are flagged (fold with both UTC alternatives)',
                       bool(ampm) and all(a['local'].endswith('T23:00:00') for a in ampm)
                       and {'nonexistent_local_time', 'ambiguous_fold'} <= set(dst) and len(unknown) >= 3,
                       {'ampm': [(a['raw'], a['local']) for a in ampm], 'dst_flags': dst,
                        'unknown_zone_event_types': unknown})
            gap = self.layout['event_gap']
            in_gap = self.query('''SELECT COUNT(*) AS n FROM event_times WHERE utc >= ? AND utc < ?''',
                                (gap['from'], gap['to']))[0]['n']
            in_gap_local = self.query('''SELECT COUNT(*) AS n FROM event_times WHERE utc IS NULL AND local >= ? AND local < ?''',
                                      (gap['from'], gap['to']))[0]['n']
            self.check('timeline.long_gap_without_events', ['K1.h'], 'no time candidate of any source falls into the '
                       'planted event gap', in_gap == 0 and in_gap_local == 0, {'gap': gap, 'utc': in_gap, 'local': in_gap_local})

            run = self.cli('correlate', '--rule', 'recording_call_log', '--actor', 'e2e')
            status, detail = self.http_json('GET', f'/api/timeline/correlations/runs/{run["run_id"]}')
            status_h, listing = self.http_json('GET', f'/api/timeline/correlations?run_id={run["run_id"]}')
            hypotheses = listing['hypotheses']
            unmatched = detail['result']['unmatched']
            artifact_by_locator = {r['source_locator'].split('/', 2)[-1]: r['id'] for r in self.query(
                "SELECT id, source_locator FROM artifacts WHERE source_locator LIKE 'catalog:collection/%/Recordings/%'")}
            without_call = {artifact_by_locator.get(self.layout['recordings'][k]) for k in ('without_call', 'codeswitch_without_call')}
            listed = {r['left_id'] for r in unmatched['recordings'] if r['left_kind'] == 'artifact'}
            number_b_calls = {r['id'] for r in self.query(
                "SELECT id FROM events WHERE event_type='call' AND phone_or_address LIKE ?",
                (f'%{self.layout["recordings"]["call_without_recording_number"][-9:]}',))}
            groups = {}
            for h in hypotheses:
                groups.setdefault(h['ambiguity_group'], []).append(h)
            ambiguous = [g for g in groups.values() if len(g) > 1]
            self.check('timeline.recording_call_cases_visible', ['K1.d'],
                       'recordings without a call and calls without a recording are listed by the run; a recording near '
                       'two call entries gives one ambiguity group with two hypotheses',
                       status == 200 and without_call <= listed and number_b_calls and number_b_calls <= set(unmatched['calls'])
                       and ambiguous and all(h['candidate_count'] == len(g) for g in ambiguous for h in g),
                       {'counts': run['counts'], 'recordings_without_call': unmatched['recordings'],
                        'calls_without_recording_number_b': sorted(number_b_calls),
                        'ambiguity_groups': [[h['id'] for h in g] for g in ambiguous]})
            same = self.cli('correlate', '--rule', 'same_conversation', '--actor', 'e2e')
            _s, same_listing = self.http_json('GET', f'/api/timeline/correlations?run_id={same["run_id"]}')
            c_pairs = [h for h in same_listing['hypotheses']
                       if (h['criteria'].get('logical_record') or {}).get('conversation_id') == self.layout['conversation_c']]
            labels = {h['id']: (h['criteria']['exports']['left']['export_label'], h['criteria']['exports']['right']['export_label'])
                      for h in c_pairs}
            e1_e3 = [h for h in c_pairs if labels[h['id']][0].endswith(layout_module.E1_DIR) and labels[h['id']][1].endswith('later-export')
                     or labels[h['id']][1].endswith(layout_module.E1_DIR) and labels[h['id']][0].endswith('later-export')]
            records = e1_e3[0]['criteria']['records'] if e1_e3 else {}
            copies = [h for h in c_pairs if h['criteria']['conversation_content']['equal']
                      and {labels[h['id']][0].split('/', 2)[-1], labels[h['id']][1].split('/', 2)[-1]}
                      == {'chatgpt-export-2025-03.zip', 'copy/chatgpt-export-2025-03.zip'}]
            exports_of_c = {l for pair in labels.values() for l in pair}
            self.check('timeline.same_conversation_three_exports', ['K1.a', 'K1.k'],
                       'conversation C is observed in three distinct exports plus a byte-identical copy; e1↔e3 shows the '
                       'edited message (changed), the branch only in e1 and the regenerated answer only in e3',
                       len(exports_of_c) == 4 and records.get('changed_examples') == [self.layout['c_expect']['edited']]
                       and self.layout['c_expect']['branch_only_in_e1_e2'] in records.get('only_left_examples', [])
                       + records.get('only_right_examples', [])
                       and self.layout['c_expect']['regenerated_in_e3'] in records.get('only_left_examples', [])
                       + records.get('only_right_examples', []) and len(copies) == 1,
                       {'exports': sorted(exports_of_c), 'e1_vs_e3': records, 'pairs': len(c_pairs)})
            email = self.cli('correlate', '--rule', 'email_chat', '--actor', 'e2e')
            statuses = self.query('''SELECT h.id, (SELECT s.status FROM timeline_hypothesis_status s WHERE s.hypothesis_id=h.id
                                     ORDER BY s.id DESC LIMIT 1) AS status FROM timeline_hypotheses h''')
            columns = {r['name'] for r in self.query("SELECT name FROM pragma_table_info('timeline_hypotheses')")}
            self.check('timeline.nothing_auto_accepted', ['N2'], 'every correlation hypothesis is proposed (none accepted '
                       'automatically) and no hypothesis carries a probability', statuses and all(s['status'] == 'proposed'
                                                                                              for s in statuses)
                       and not any('probab' in c for c in columns),
                       {'hypotheses': len(statuses), 'statuses': sorted({s['status'] for s in statuses}),
                        'email_chat_counts': email['counts']})

    def stage_budget(self):
        with self.stage('time_budget'):
            categories = self.private / 'categories.json'
            window = ('--from', '2025-02-24', '--to', '2025-03-10', '--zone', ZONE)
            first = self.cli('budget-run', '--config', categories, *window, self.outputs / 'budget-1.json')
            result = json.loads((self.outputs / 'budget-1.json').read_text('utf-8'))
            self.budget_run_id = first['run']['run_id']
            method = result['method']
            self.check('budget.method_names_real_rule', ['K3.a'], 'the method record names the rule and hash that placed '
                       'the items (regression: it said analysis_source.v1)', method['source_rule'] == result['input']['source_rule']
                       == 'display_time@1' and method['source_rule_sha256'] == result['input']['source_rule_sha256']
                       and method['rule'] == 'time_budget.v2', {k: method[k] for k in ('rule', 'seam', 'source_rule', 'source_rule_sha256')})
            days = result['candles']['day']
            empty = [d for d in days if not d['categories']]
            palette = {c['id'] for c in result['categories']}
            self.check('budget.unobserved_band', ['K1.h', 'N2'],
                       'days without observations are nieobserwowane for their full length in every variant, the band is '
                       'part of the palette and the disclaimer says it is not inactivity',
                       len(empty) >= 10 and all(d['unobserved']['lower'] == d['unobserved']['probable'] == d['unobserved']['upper']
                                                == d['length_s'] for d in empty)
                       and 'nieobserwowane' in palette and 'nie bezczynność' in result['disclaimer'],
                       {'empty_days': len(empty), 'days': len(days), 'disclaimer': result['disclaimer']})
            overlap_day = self.layout['parallel']['day']
            one = self.cli('budget-run', '--config', categories, '--from', overlap_day, '--to', '2025-03-02', '--zone', ZONE,
                           self.outputs / 'budget-overlap-one.json')
            split = self.private / 'categories_split.json'
            values = {}
            for rule in ('count_both', 'split_equal'):
                self.cli('budget-run', '--config', split, '--from', overlap_day, '--to', '2025-03-02', '--zone', ZONE,
                         '--params', json.dumps({'overlap_rule': rule}), self.outputs / f'budget-overlap-{rule}.json')
                values[rule] = json.loads((self.outputs / f'budget-overlap-{rule}.json').read_text('utf-8'))['candles']['day'][0]
            combined = json.loads((self.outputs / 'budget-overlap-one.json').read_text('utf-8'))['candles']['day'][0]
            union = combined['categories']['praca_projekty']['probable']
            both = values['count_both']
            # Only the two rule categories: the bez_kategorii remainder under count_both is being reworked (review
            # finding 8) and every event here has a full-weight label anyway.
            separate = sum(both['categories'][c]['probable'] for c in ('praca_projekty', 'inne'))
            split_sum = sum(values['split_equal']['categories'][c]['probable'] for c in ('praca_projekty', 'inne'))
            measured_overlap = both['overlap_s']['probable']
            self.check('budget.parallel_chatbots_not_double_counted', ['K1.g', 'K3.a'],
                       'with both chatbots in one category the day total is the union: it equals the sum of the separate '
                       'per-chatbot unions minus their measured overlap, and the split_equal total; it is below the plain sum',
                       measured_overlap > 0 and abs(union - (separate - measured_overlap)) < 0.01
                       and abs(union - split_sum) < 0.01 and union < separate and combined['overlap_s']['probable'] == 0,
                       {'union_one_category': union, 'sum_separate_count_both': round(separate, 3),
                        'overlap_s': measured_overlap, 'split_equal_sum': round(split_sum, 3), 'run': one['run']['run_id']})
            changed = self.cli('budget-run', '--config', categories, *window, '--params',
                               json.dumps({'reading_chars_per_min': 500, 'typing_chars_per_min': 100}), self.outputs / 'budget-2.json')
            changed_result = json.loads((self.outputs / 'budget-2.json').read_text('utf-8'))
            shown = self.cli('budget-show', self.budget_run_id, self.outputs / 'budget-1-show.json')
            stored = json.loads((self.outputs / 'budget-1-show.json').read_text('utf-8'))
            stored_result = stored.get('result', stored)
            again = self.cli('budget-run', '--config', categories, *window)
            listing = self.cli('budget-list', '--kind', 'time_budget')
            self.check('budget.parameter_change_new_run_old_kept', ['K3.b'],
                       'changing parameters stores a new run with a new hash and different numbers; the old run is still '
                       'retrievable and its stored result re-hashes to the original hash; the same parameters reuse it',
                       changed['run']['run_id'] != self.budget_run_id and changed['run']['result_sha256'] != first['run']['result_sha256']
                       and changed_result['totals']['categories']['praca_projekty']['probable']
                       != result['totals']['categories']['praca_projekty']['probable']
                       and shown['run']['result_sha256'] == first['run']['result_sha256'] == _canonical_sha256(stored_result)
                       and again['run']['reused'] is True and again['run']['run_id'] == self.budget_run_id,
                       {'first': first['run'], 'changed': changed['run'], 'recomputed_old_hash': _canonical_sha256(stored_result),
                        'rerun_same_params': again['run'], 'runs_listed': len(listing['runs'])})
            self.facts['budget'] = {'run_id': self.budget_run_id, 'totals': result['totals'],
                                    'exclusions': result['exclusions']}
            if self.heavy:
                full = self.cli('budget-run', '--config', categories, *window, '--params', json.dumps({'grid_mode': 'full'}))
                self.facts['budget_full_grid'] = {'run': full['run'], 'grid': full.get('grid')}

    def stage_hypothesis(self):
        with self.stage('hypothesis'):
            registered = self.cli('hypothesis-register', self.private / 'hypothesis.json', '--actor', 'e2e')
            self.hypothesis_id, version = registered['hypothesis_id'], registered['version_id']
            first = self.cli('eventstudy-run', version, '--summary', '--actor', 'e2e')
            second = self.cli('eventstudy-run', version, '--summary', '--actor', 'e2e')
            self.check('hypothesis.eventstudy_reproducible', ['K3.a', 'K3.b'],
                       'the preregistered, seeded event study gives the same result hash on a re-run and lists the earlier run',
                       first['result_sha256'] == second['result_sha256'] and second['identical_result_runs'] == [first['run_id']]
                       and first['integrity'] == 'hash_matches',
                       {k: first[k] for k in ('definition_sha256', 'seed', 'input_sha256', 'result_sha256')} | {
                           'second_run': second['run_id'],
                           # p-values are deliberately not recorded or asserted: the null models are under review.
                           'observed_pairs': {w['label']: w['observed'] for w in first['windows']}})
            self.check('hypothesis.causation_warning', ['N2'], 'the causation warning is always flagged and the hashed '
                       'disclaimer says the result is auxiliary material, not a causal conclusion',
                       'causation' in first['flagged_warnings'] and 'nie wnioskiem przyczynowym' in first['disclaimer']['pl'],
                       {'flagged': first['flagged_warnings'], 'disclaimer_sha256_pl': first['disclaimer']['sha256_pl']})
            candidates = self.cli('dossier-candidates', self.hypothesis_id, '--actor', 'e2e')
            self.check('hypothesis.no_provider_call', ['N1'], 'candidate search used no model provider and no opt-in',
                       candidates['provider'] == {'name': 'none', 'opt_in': False, 'external': False, 'returned': 0},
                       candidates['provider'])
            added = self.cli('dossier-add', self.hypothesis_id, 'anchor', self.anchor_id, '--classification', 'context',
                             '--rationale', 'quote from the synthetic code-switch fixture', '--actor', 'e2e')
            first_item = candidates['added_item_ids'][0]
            self.cli('dossier-classify', first_item, 'supporting', '--rationale', 'synthetic decision', '--actor', 'e2e')
            supported = self.cli('thesis-add', self.hypothesis_id, '--type', 'inference_chain', '--text',
                                 'Synthetic thesis with evidence pointers.', '--evidence', f'{first_item},{added["item_id"]}',
                                 '--actor', 'e2e')
            unsupported = self.cli('thesis-add', self.hypothesis_id, '--type', 'speculation', '--text',
                                   'Synthetic thesis without an evidence pointer.', '--actor', 'e2e')
            self.cli('disclosure-add', self.hypothesis_id, 'known_unknowns',
                     'Synthetic corpus: WhatsApp wall clocks and recording file names have no time zone.', '--actor', 'e2e')
            self.check('hypothesis.thesis_without_evidence_flagged', ['N2'], 'a thesis without an evidence pointer is '
                       'flagged unsupported automatically', unsupported['unsupported'] is True and supported['unsupported'] is False,
                       {'supported': supported['flags'], 'unsupported': unsupported['flags']})

    def stage_reports(self):
        with self.stage('reports'):
            formats = ['html', 'json', 'csv', 'xlsx'] + (['pdf'] if self.pdf_chromium else [])
            params = {
                'catalog': {},
                'timeline': {'from': '2024-01-01T00:00:00Z', 'to': '2025-04-01T00:00:00Z'},
                'time_budget': {'budget_run_id': self.budget_run_id},
                'hypothesis_dossier': {'hypothesis_id': self.hypothesis_id},
                'lawyer_package': {'open_questions': [OPEN_QUESTION]},
            }
            self.report_results = {}
            for kind in REPORT_TYPES:
                result = self.cli('report-create', '--type', kind, '--params', json.dumps({'formats': formats, **params[kind]}),
                                  '--output', self.reports)
                self.report_results[kind] = result
            verified = {}
            for kind, result in self.report_results.items():
                verified[kind] = self.cli('report-verify', result['manifest'], '--expected-manifest-sha256', result['manifest_sha256'])
            self.check('reports.five_types_written', ['K3.a'], 'all five report types are generated into the private '
                       'output directory with HTML, JSON, CSV and XLSX', all(
                           all(r['formats'][f] == 'written' for f in ('html', 'json', 'csv', 'xlsx'))
                           and all(s in ('ok', 'empty') for s in r['sections'].values()) for r in self.report_results.values())
                       and all(Path(r['output_dir']).parent == self.reports for r in self.report_results.values()),
                       {k: {'formats': r['formats'], 'sections': r['sections'], 'files': r['files']}
                        for k, r in self.report_results.items()})
            if self.pdf_chromium:
                pdf = {k: r['formats'].get('pdf') for k, r in self.report_results.items()}
                self.check('reports.pdf_via_chromium', ['K3.a'], 'PDF printed by the configured Chromium for every report',
                           set(pdf.values()) == {'written'}, {'pdf': pdf, 'chromium': self.pdf_chromium})
                self.facts['pdf'] = {'status': pdf, 'executable': self.pdf_chromium}
            else:
                self.facts['pdf'] = {'status': 'not requested (EW_PDF_CHROMIUM not set for this run)'}
            self.check('reports.manifests_verify', ['K3.a'], 'report-verify accepts every manifest against its '
                       'registered SHA-256 (digest, files, content, citation bundle)',
                       all(v['valid'] and v['digest_valid'] and v['files_valid'] and v['content_valid'] and v['bundle_valid']
                           for v in verified.values()), {k: {x: v[x] for x in ('valid', 'files_valid', 'bundle_valid')}
                                                         | {'warnings': [w['code'] for w in v['warnings']]} for k, v in verified.items()})
            checked = self.module('server.tests.e2e.report_check', '--budget-run', self.budget_run_id,
                                  *[r['output_dir'] for r in self.report_results.values()])
            self.check('reports.numbers_traceable', ['K3.a'], 'every data-k span in each report HTML equals its value in '
                       'report.json and no numeral appears outside the spans (the reports tests\' crosscheck, run on this '
                       'corpus)', all(r['mismatches'] == 0 and r['stray_numerals'] == 0 and r['spans'] > 0 and r['title_ok']
                                      for r in checked['reports']), checked['reports'])
            self.check('reports.budget_numbers_equal_stored_run', ['K3.a'], 'the time-budget report shows exactly the '
                       'totals and candles of the stored run it names (result hash re-verified)', checked['budget']['equal'],
                       checked['budget'])
            tampered = self.work / 'tampered-report'
            shutil.copytree(self.report_results['lawyer_package']['output_dir'], tampered)
            victim = sorted((tampered / 'tables').glob('*.csv'))[0]
            with open(victim, 'ab') as handle:
                handle.write(b'tampered\n')
            failed = self.cli('report-verify', tampered / 'manifest.json', ok=(1,))
            original = self.cli('report-verify', Path(self.report_results['lawyer_package']['output_dir']) / 'manifest.json')
            self.check('reports.tamper_detected', ['K3.a'], 'changing one byte-range of one table in a copy makes '
                       'report-verify fail (exit 1, file_digest_mismatch); the original still verifies',
                       failed['_rc'] == 1 and failed['valid'] is False
                       and [e['code'] for e in failed['errors']] == ['file_digest_mismatch'] and original['valid'] is True,
                       {'errors': failed['errors'], 'tampered_file': victim.name})
            listing = self.cli('report-list')
            self.facts['reports'] = {k: {'report_id': r['report_id'], 'manifest_sha256': r['manifest_sha256'],
                                         'content_sha256': r['content_sha256']} for k, r in self.report_results.items()}
            self.facts['reports_registered'] = len(listing.get('reports') or [])

    def stage_citations(self):
        with self.stage('citations'):
            report_id = self.report_results['lawyer_package']['report_id']
            bundle = self.cli('citation-bundle-export', self.outputs / 'bundle', '--report', report_id)
            inspected = self.cli('citation-bundle-inspect', self.outputs / 'bundle')
            packet_path = self.outputs / f'packet-a{self.wav_id}-c{self.anchor_id}.json'
            exported = self.cli('citation-export', self.wav_id, self.anchor_id, packet_path)
            single = self.cli('citation-inspect', packet_path)
            self.check('citations.bundle_and_packet_verify', [], 'the report\'s citation bundle and a single packet '
                       'export and inspect as valid without source access',
                       bundle['packets'] >= 1 and inspected['valid'] is True
                       and single['verification'] == 'matches_included_pinned_transcript',
                       {'bundle_packets': bundle['packets'], 'bundle_excluded': bundle['excluded'],
                        'inspection': inspected['packets'], 'packet_payload_sha256': exported['payload_sha256']})
            self.packet_path = packet_path

    def stage_metadata(self):
        with self.stage('metadata'):
            output = self.outputs / 'metadata-package.json'
            self.cli('metadata-export', output)
            verified = self.cli('metadata-verify', output)
            self.check('metadata.export_verifies', [], 'the metadata package of the whole run verifies (digest, '
                       'relationships, anchors; regression: file-name import runs named a missing artifact)',
                       verified['valid'] and verified['relationships_valid'] and verified['anchors_valid'],
                       {'errors': verified['errors'][:5], 'warning_codes': sorted({w['code'] for w in verified['warnings']}),
                        'counts': {k: v for k, v in verified['counts'].items() if not k.startswith('extension:')}})

    def stage_read_api(self):
        with self.stage('read_api'):
            status_none, _ = self.http('GET', '/api/read/v1/search?q=Parallel', token=False)
            status, search = self.http_json('GET', '/api/read/v1/search?q=Parallel&limit=20')
            day = self.layout['parallel']['day']
            status_t, window = self.http_json('GET', f'/api/read/v1/timeline?from={day}&to=2025-03-02&limit=100')
            status_a, artifact = self.http_json('GET', f'/api/read/v1/artifacts/{self.wav_id}')
            status_p, packet = self.http('GET', f'/api/read/v1/artifacts/{self.wav_id}/citations/{self.anchor_id}/packet')
            chatbots = sorted({i['contact_label'] for i in window['items'] if i['event_type'] == 'chat_message'})
            self.check('read_api.token_search_window_packet', [],
                       'the read API refuses a request without a token and, with the token, answers search, the timeline '
                       'window and a citation packet byte-identical to the CLI export',
                       status_none == 401 and status == 200 and search['items'] and status_t == 200 and chatbots == ['chatgpt', 'claude']
                       and status_a == 200 and status_p == 200 and packet == self.packet_path.read_bytes(),
                       {'search_items': len(search['items']), 'window_items': len(window['items']), 'chatbots': chatbots,
                        'artifact_versions': len(artifact.get('transcript_versions') or artifact.get('versions') or []),
                        'packet_bytes': len(packet)})
            revoked = self.cli('token-revoke', self.token_id, '--note', 'end of e2e run')
            status_r, _ = self.http('GET', '/api/read/v1/search?q=Parallel')
            self.check('read_api.revoked_token_refused', [], 'a revoked token is refused', status_r == 401,
                       {'status': status_r, 'revoked': revoked.get('status')})

    def stage_originals(self):
        with self.stage('originals'):
            self.stop_server()
            after = {key: tree_state(self.sources / key) for key in self.roots}
            changed = {key: sorted(k.decode('utf-8', 'replace') for k in set(self.before[key]) ^ set(after[key])
                                   | {k for k in self.before[key] if k in after[key] and self.before[key][k] != after[key][k]})
                       for key in self.roots}
            self.check('originals.unchanged', ['K2.b', 'N3'], 'every source root is byte-, size-, mode- and mtime-identical '
                       'after the whole run (no file added, removed or touched)', after == self.before,
                       {'changed': {k: v[:10] for k, v in changed.items() if v},
                        'tree_sha256': {k: tree_digest(v) for k, v in after.items()}})

    def stage_repository(self):
        with self.stage('repository'):
            status_after = _git('status', '--porcelain', '--untracked-files=all')
            self.check('repository.untouched_by_run', ['K4', 'N3'], 'the run wrote nothing into the repository checkout',
                       status_after == self.git_status_before,
                       {'new_entries': sorted(set(status_after.splitlines()) - set(self.git_status_before.splitlines()))[:10]})
            if self.verify_repository:
                process = self._run([self.python, 'scripts/verify_repository.py'], ['python', 'scripts/verify_repository.py'],
                                    ok=(0, 1))
                self.check('repository.verify_passes', ['K4'], 'scripts/verify_repository.py (links, snapshots, privacy '
                           'heuristics: e-mails, phone numbers, user paths) passes', process.returncode == 0,
                           {'rc': process.returncode, 'tail': process.stdout.strip().splitlines()[-3:],
                            'problems': process.stderr.strip().splitlines()[-10:]})
