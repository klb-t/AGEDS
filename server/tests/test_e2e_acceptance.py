"""End-to-end acceptance of the whole increment (AGEDS-TASK-20261016-Z).

One module-scoped run of ``server.tests.e2e.pipeline.Pipeline`` drives the real CLI
(subprocesses with a fresh data directory, ``EW_SCAN_ROOTS`` and ``EW_REPORTS_DIR``
outside the repository) and the real HTTP service over the composed multi-device
layout; the tests below assert its checks grouped by acceptance criterion, so a
failure names the criterion. Default run: bounded (about a minute here).

Opt-in variants (environment):

* ``AGEDS_E2E_HEAVY=1`` adds the adversarial catalog corpus as a fourth collection and
  a full-grid budget run;
* ``AGEDS_E2E_PDF_CHROMIUM=/path/to/chrome`` (or ``EW_PDF_CHROMIUM``) also prints every
  report to PDF and requires it to succeed;
* ``AGEDS_E2E_SKIP=1`` skips the module (e.g. on a machine without a free local port).
"""
from __future__ import annotations

import json
import os

import pytest

from server.tests.e2e.pipeline import CRITERIA, NOT_VERIFIABLE_IN_RUN, REPO, Pipeline

pytestmark = pytest.mark.skipif(os.environ.get('AGEDS_E2E_SKIP') == '1', reason='AGEDS_E2E_SKIP=1')


@pytest.fixture(scope='module')
def run(tmp_path_factory):
    pdf = os.environ.get('AGEDS_E2E_PDF_CHROMIUM') or os.environ.get('EW_PDF_CHROMIUM') or None
    pipeline = Pipeline(tmp_path_factory.mktemp('ageds-e2e'), heavy=os.environ.get('AGEDS_E2E_HEAVY') == '1',
                        pdf_chromium=pdf)
    return pipeline, pipeline.run()


@pytest.fixture(scope='module')
def receipt(run):
    return run[1]


def _checks(receipt, criterion):
    return [c for c in receipt['checks'] if criterion in c['criteria']]


def _assert_criterion(receipt, criterion):
    checks = _checks(receipt, criterion)
    assert checks, f'no check supports {criterion}'
    failed = [(c['id'], json.dumps(c['evidence'], ensure_ascii=False)[:1500]) for c in checks if not c['passed']]
    assert not failed, failed
    assert receipt['criteria'][criterion]['status'] == 'met'


def test_pipeline_ran_every_stage_without_error(receipt):
    assert receipt['error'] is None, receipt['error']
    assert [s['status'] for s in receipt['stages']] == ['ok'] * len(receipt['stages'])
    heavy = ['heavy_adversarial_plan'] if receipt['variant'] == 'heavy' else []
    assert [s['name'] for s in receipt['stages']] == [
        'layout', 'collections', 'catalog', 'import_plan', 'reimport', *heavy, 'catalog_ingest', 'asr',
        'service_and_citation', 'timeline', 'time_budget', 'hypothesis', 'reports', 'citations', 'metadata', 'read_api',
        'originals', 'repository']
    assert all(isinstance(s['seconds'], float) for s in receipt['stages'])


def test_every_check_passed(receipt):
    assert receipt['summary']['failed'] == [], [
        (c['id'], json.dumps(c['evidence'], ensure_ascii=False)[:800]) for c in receipt['checks'] if not c['passed']]


@pytest.mark.parametrize('criterion', sorted(k for k in CRITERIA if k.startswith('K1')))
def test_corpus_planted_cases(receipt, criterion):
    _assert_criterion(receipt, criterion)


@pytest.mark.parametrize('criterion', ['K2.a', 'K2.b', 'N3'])
def test_reimport_duplicates_nothing_and_originals_are_untouched(receipt, criterion):
    _assert_criterion(receipt, criterion)


@pytest.mark.parametrize('criterion', ['K3.a', 'K3.b'])
def test_numbers_are_reproducible_and_parameter_changes_keep_old_results(receipt, criterion):
    _assert_criterion(receipt, criterion)


@pytest.mark.parametrize('criterion', ['K4', 'N1', 'N2'])
def test_privacy_opt_ins_and_interpretive_labels(receipt, criterion):
    _assert_criterion(receipt, criterion)


def test_criteria_not_verifiable_in_a_run_are_explicit(receipt):
    for key in NOT_VERIFIABLE_IN_RUN:
        assert receipt['criteria'][key]['status'] == 'not_verifiable_in_run' and receipt['criteria'][key]['note']
    assert set(receipt['criteria']) == set(CRITERIA)


def test_receipt_holds_no_absolute_work_or_repository_paths(run):
    pipeline, receipt = run
    text = json.dumps(receipt, ensure_ascii=False)
    assert str(pipeline.work) not in text and str(REPO) not in text
    assert '<work>/reports' in text  # paths are shown relative to the work directory
