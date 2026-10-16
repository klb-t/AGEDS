"""Run the REAL worker loop with a FAKE faster-whisper module (synthetic tones, not speech).

``python -m server.tests.e2e.fake_asr_worker --iterations N`` installs
``server.tests.corpus.audio.FakeFasterWhisper`` into ``sys.modules`` and then
calls ``server.app.worker.main`` (claim with lease → pinned recipe → adapter →
fenced publication) exactly as the service worker does. Only the model is
replaced: the fake maps tone frequencies to ``pl``/``nl``/``en`` and derives
deterministic words from the samples. Real inference is impossible in this
environment (no model weights, Hugging Face blocked), so this proves the
pipeline wiring (jobs, runs, versions, word timings, comparison), never
recognition quality.

The database is the one named by ``EW_DB_PATH``. Prints one JSON object.
"""
from __future__ import annotations

import argparse
import json
import sys
from unittest.mock import patch

from server.tests.corpus.audio import LABEL, FakeFasterWhisper


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m server.tests.e2e.fake_asr_worker')
    parser.add_argument('--iterations', type=int, default=4, help='worker loop iterations (claims)')
    args = parser.parse_args(argv)
    fake = FakeFasterWhisper()
    with patch.dict(sys.modules, fake.modules()):
        from server.app import db, worker
        worker.main(iterations=args.iterations, sleep=lambda _seconds: None)
        with db.session() as connection:
            jobs = [dict(row) for row in connection.execute(
                "SELECT id, kind, artifact_id, status, error FROM jobs WHERE kind='transcribe' ORDER BY id")]
    calls = {}
    for name, _params in fake.calls:
        calls[name] = calls.get(name, 0) + 1
    print(json.dumps({'command': 'fake_asr_worker', 'model': 'FakeFasterWhisper (tests only)', 'fixture_label': LABEL,
                      'real_inference': False, 'jobs': jobs, 'fake_calls': calls}, ensure_ascii=False))
    return 0 if all(job['status'] == 'done' for job in jobs) else 1


if __name__ == '__main__':
    raise SystemExit(main())
