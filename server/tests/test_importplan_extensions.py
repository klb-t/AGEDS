import json

import pytest

from server.app.importplan import detect


def _table(rules, version='1'):
    return {'schema': 'ageds.importplan-detectors', 'version': version, 'files': rules}


def test_extension_tables_append_rules_and_change_the_recorded_hash(tmp_path, monkeypatch):
    ext = tmp_path / 'detectors.d'
    ext.mkdir()
    monkeypatch.setattr(detect, 'EXTENSION_DIR', ext)
    base = detect.load_detectors()
    (ext / 'area.v1.json').write_text(json.dumps(_table([{'id': 'x_area_rule', 'types': ['json'],
                                                           'candidates': [{'importer': 'x', 'reason': 'test'}]}])))
    merged = detect.load_detectors()
    assert [r['id'] for r in merged['files']][-1] == 'x_area_rule'
    assert merged['_sha256'] != base['_sha256'] and merged['_extensions'][0]['name'] == 'area.v1.json'
    explicit = detect.load_detectors(detect.DEFAULT_DETECTORS)
    assert explicit['_sha256'] == base['_sha256']  # an explicit path loads that table alone


def test_duplicate_rule_ids_across_extensions_are_refused(tmp_path, monkeypatch):
    ext = tmp_path / 'detectors.d'
    ext.mkdir()
    monkeypatch.setattr(detect, 'EXTENSION_DIR', ext)
    first_id = detect.load_detectors()['files'][0]['id']
    (ext / 'dup.v1.json').write_text(json.dumps(_table([{'id': first_id, 'candidates': [{'importer': 'x', 'reason': 'r'}]}])))
    with pytest.raises(ValueError, match='duplicate detector rule id'):
        detect.load_detectors()
