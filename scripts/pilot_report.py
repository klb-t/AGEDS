#!/usr/bin/env python3
"""Pilot report filled from an AGEDS database, read-only (Markdown + JSON).

    .venv/bin/python scripts/pilot_report.py [--db PATH] [--out DIR]

Defaults to the pilot of scripts/pilot_local.sh (~/ageds-pilot). Every figure is read from
the database; nothing is inferred. The report keeps the layers apart: source bytes and
their acquisitions, parser observations, model output (ASR/OCR, with model and run),
and interpretations. Model text is quoted only as a short excerpt and labelled as such.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

EXCERPT = 160


def rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params)]


def has_table(db, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def collect(db) -> dict:
    report = {'generated_at': datetime.now(timezone.utc).isoformat()}
    try:
        report['code_commit'] = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True,
                                               cwd=Path(__file__).resolve().parents[1]).stdout.strip() or 'unknown'
    except OSError:
        report['code_commit'] = 'unknown'
    report['collections'] = rows(db, 'SELECT id, label, acquisition_method, operator, created_at FROM collections ORDER BY id') \
        if has_table(db, 'collections') else []
    report['scans'] = []
    if has_table(db, 'catalog_scans'):
        for scan in rows(db, 'SELECT * FROM catalog_scans ORDER BY id'):
            coverage = json.loads(scan.get('coverage_json') or '{}') if 'coverage_json' in scan else {}
            report['scans'].append({'id': scan['id'], 'collection_id': scan.get('collection_id'), 'status': scan.get('status'),
                                    'entries': coverage.get('entries'), 'complete': coverage.get('complete'),
                                    'incomplete_reasons': coverage.get('incomplete_reasons')})
    artifacts = []
    for artifact in rows(db, 'SELECT id, original_name, mime_type, size_bytes, sha256, created_at FROM artifacts ORDER BY id'):
        observations = db.execute('SELECT count(*) FROM source_observations WHERE artifact_id=?', (artifact['id'],)).fetchone()[0]
        texts = []
        for text in rows(db, 'SELECT id, kind, model, language, text, segments_json, metadata_json, created_at '
                             'FROM derived_text WHERE artifact_id=? ORDER BY id', (artifact['id'],)):
            segments = json.loads(text['segments_json'] or '[]')
            metadata = json.loads(text['metadata_json'] or '{}')
            duration = metadata.get('duration_seconds_ffprobe')
            ends = [seg['end'] for seg in segments if isinstance(seg.get('end'), (int, float))]
            coverage = round(max(ends) / duration, 3) if duration and ends else None
            texts.append({'time_coverage': coverage, 'untimed_segments': sum(1 for seg in segments if seg.get('start') is None)
                          if text['kind'] == 'transcript' else None,
                          'id': text['id'], 'layer': 'model output' if text['kind'] in ('transcript', 'ocr') else text['kind'],
                          'kind': text['kind'], 'model': text['model'], 'language': text['language'],
                          'segments': len(segments), 'characters': len(text['text'] or ''),
                          'speakers': sorted({s.get('speaker_hypothesis') for s in segments if s.get('speaker_hypothesis')}),
                          'segment_languages': metadata.get('segment_languages'),
                          'timing': metadata.get('timing') or ('word timing' if any(s.get('words') for s in segments) else None),
                          'transcoding': (metadata.get('transcoding') or {}).get('to'),
                          'excerpt': (text['text'] or '')[:EXCERPT]})
        artifacts.append({**artifact, 'acquisitions': observations, 'derived_texts': texts})
    report['artifacts'] = artifacts
    report['reimport'] = {'artifacts': len(artifacts),
                          'acquisitions': db.execute('SELECT count(*) FROM source_observations').fetchone()[0],
                          'meaning': 'identical bytes keep one artifact; every acquisition is one observation'}
    report['jobs'] = rows(db, 'SELECT kind, status, count(*) AS count FROM jobs GROUP BY kind, status ORDER BY kind, status')
    report['failures'] = rows(db, "SELECT id, kind, artifact_id, error FROM jobs WHERE status='failed' ORDER BY id")
    report['runs'] = rows(db, 'SELECT id, job_id, artifact_id, status, tool, provider, model FROM processing_runs ORDER BY id')
    report['event_candidates'] = rows(db, 'SELECT artifact_id, parser, count(*) AS count FROM event_candidates '
                                          'GROUP BY artifact_id, parser') if has_table(db, 'event_candidates') else []
    report['events'] = db.execute('SELECT count(*) FROM events').fetchone()[0]
    if has_table(db, 'provider_calls'):
        settled = rows(db, "SELECT purpose, model, status, input_tokens, output_tokens, cost_basis_json FROM provider_calls "
                           "WHERE status<>'reserved'")
        report['provider'] = {'calls': len(settled),
                              'by_purpose': {p: sum(1 for c in settled if c['purpose'] == p) for p in {c['purpose'] for c in settled}},
                              'input_tokens': sum(c['input_tokens'] or 0 for c in settled),
                              'output_tokens': sum(c['output_tokens'] or 0 for c in settled),
                              'cost_eur': round(db.execute('SELECT COALESCE(SUM(cost_eur),0) FROM provider_calls').fetchone()[0], 6),
                              'errors': [c for c in settled if c['status'] == 'error'],
                              'cost_basis': 'preset prices (unverified with Google); reservations settled to actual usage'}
    gaps = []
    for artifact in artifacts:
        if not artifact['derived_texts']:
            gaps.append(f"artifact {artifact['id']} ({artifact['original_name']}): no derived text yet")
        for text in artifact['derived_texts']:
            if text['kind'] == 'transcript' and text['timing'] != 'word timing':
                gaps.append(f"artifact {artifact['id']}: transcript {text['id']} has no word timing (segment citations only)")
            if text['kind'] == 'transcript' and text['time_coverage'] is not None and not 0.8 <= text['time_coverage'] <= 1.02:
                gaps.append(f"artifact {artifact['id']}: transcript {text['id']} segment times cover "
                            f"{text['time_coverage']:.0%} of the recording: model timestamps are unreliable here")
            if text['kind'] == 'transcript' and text['untimed_segments']:
                gaps.append(f"artifact {artifact['id']}: transcript {text['id']} has {text['untimed_segments']} segments without usable time")
            if text['kind'] == 'ocr':
                gaps.append(f"artifact {artifact['id']}: OCR {text['id']} has no pixel boxes (page/line locators)")
    gaps += ['ASR/OCR quality is not measured against a human reference here; speaker labels are hypotheses',
             'importing an export is not account access: no mail/chat connectors were used']
    report['gaps'] = gaps
    return report


def markdown(report: dict) -> str:
    out = [f"# Raport pilotu AGEDS\n\nWygenerowano {report['generated_at']} z bazy (tylko odczyt); kod `{report['code_commit'][:12]}`.\n"
           "Warstwy: **bajty źródła i ich pozyskania** · **wynik modelu** (ASR/OCR, z modelem i runem) · interpretacje "
           "(tu brak). Fragmenty tekstu modelu są cytatem wyniku modelu, nie treścią źródła.\n"]
    out.append('## Kolekcje i skany\n')
    for c in report['collections']:
        out.append(f"- kolekcja {c['id']}: {c['label']} (pozyskanie: {c['acquisition_method'] or 'nieznane'})")
    for s in report['scans']:
        out.append(f"- skan {s['id']}: {s['status']}, wpisów {s['entries']}, kompletny: {s['complete']}")
    out.append('\n## Oryginały\n\n| ID | plik | typ | bajty | SHA-256 | pozyskania | wersje tekstu |\n|---|---|---|---|---|---|---|')
    for a in report['artifacts']:
        out.append(f"| {a['id']} | {a['original_name']} | {a['mime_type']} | {a['size_bytes']} | `{(a['sha256'] or '')[:16]}…` | "
                   f"{a['acquisitions']} | {len(a['derived_texts'])} |")
    r = report['reimport']
    out.append(f"\nPonowny import: {r['artifacts']} artefaktów, {r['acquisitions']} pozyskań ({r['meaning']}).\n")
    out.append('## Przetwarzanie\n')
    out.append('- zadania: ' + (', '.join(f"{j['kind']}/{j['status']}: {j['count']}" for j in report['jobs']) or 'brak'))
    for f in report['failures']:
        out.append(f"- błąd zadania {f['id']} ({f['kind']}, artefakt {f['artifact_id']}): {f['error']}")
    for a in report['artifacts']:
        for t in a['derived_texts']:
            out.append(f"- artefakt {a['id']} → {t['kind']} #{t['id']} ({t['layer']}, model {t['model']}): {t['segments']} segm., "
                       f"{t['characters']} znaków, języki {t['segment_languages'] or t['language']}, mówcy (hipotezy) "
                       f"{t['speakers'] or '—'}, czas: {t['timing'] or '—'}"
                       + (f", pokrycie czasem {t['time_coverage']:.0%}" if t.get('time_coverage') is not None else '')
                       + (f", przekodowano do {t['transcoding']}" if t['transcoding'] else '')
                       + f"\n  > {t['excerpt']!s}")
    out.append(f"- kandydaci zdarzeń (nigdy zdarzenia): {sum(c['count'] for c in report['event_candidates'])}; zdarzenia: {report['events']}")
    if 'provider' in report:
        p = report['provider']
        out.append(f"\n## Koszty dostawcy\n\n{p['calls']} wywołań {p['by_purpose']}, tokeny {p['input_tokens']}/{p['output_tokens']}, "
                   f"razem {p['cost_eur']} € ({p['cost_basis']}).")
    out.append('\n## Luki\n')
    out += [f'- {g}' for g in report['gaps']]
    return '\n'.join(out) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, default=Path.home() / 'ageds-pilot' / 'data' / 'evidence.db')
    parser.add_argument('--out', type=Path, default=Path.home() / 'ageds-pilot' / 'reports')
    args = parser.parse_args()
    db = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    report = collect(db)
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    (args.out / f'pilot-{stamp}.json').write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n')
    (args.out / f'pilot-{stamp}.md').write_text(markdown(report))
    print(args.out / f'pilot-{stamp}.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
