"""Semantic preflight and projection work are bounded before interpretation."""
from copy import deepcopy
import json
import runpy
import unittest
from unittest.mock import patch

from server.app import packages
from server.app.verification_budget import VerificationBudget, VerificationLimitError


def fixture():
    # Reuse the established fully related synthetic package, not a live database.
    return runpy.run_path('server/tests/test_metadata_archive.py')['fixture']()


def rehash(package):
    package['integrity'] = packages._integrity({key: value for key, value in package.items() if key != 'integrity'})
    return package


class VerificationBudgetTests(unittest.TestCase):
    def test_preflight_values_and_container_nodes_match_structure(self):
        values = [None, True, 1, 'literal: [ ] \\"', {}, [], {'key': [1, {'other': 'text'}]}, {'é': 'ź'}]
        for value in values:
            with self.subTest(value=value):
                tree, text = VerificationBudget(), VerificationBudget()
                tree.check_structure(value)
                text.preflight_json(json.dumps(value))
                self.assertEqual(tree.nodes, text.nodes)

    def test_preflight_stops_before_json_loader_for_large_nested_structure(self):
        budget = VerificationBudget(max_nodes=10)
        with patch('server.app.verification_budget.json.loads', side_effect=AssertionError('decoder must not start')):
            with self.assertRaises(VerificationLimitError):
                budget.decode_json('[0,0,0,0,0,0,0,0,0,0]')
        self.assertTrue(budget.exhausted)
        with self.assertRaises(VerificationLimitError):
            budget.charge_projection(0)

    def test_depth_includes_expanded_field_location_and_leaves(self):
        value = '{"x":[0]}'
        with self.assertRaises(VerificationLimitError):
            VerificationBudget(max_depth=5).decode_json(value, start_depth=4)
        self.assertEqual(VerificationBudget(max_depth=6).decode_json(value, start_depth=4), {'x': [0]})

    def test_unanchored_and_auxiliary_raw_stay_opaque(self):
        package = fixture()
        raw = '[' * 100 + 'NaN' + ']' * 100
        package['tables']['derived_text'][1]['segments_json'] = raw
        package['tables']['artifacts'][0]['metadata_json'] = raw
        package['tables']['processing_runs'][0]['error'] = raw
        result = packages.validate_metadata_package(rehash(package), budget=VerificationBudget(max_depth=10))
        self.assertTrue(result['valid'], result)
        self.assertEqual(package['tables']['derived_text'][1]['segments_json'], raw)

    def test_same_hidden_depth_in_pinned_version_is_bounded(self):
        package = fixture()
        segments = json.loads(package['tables']['derived_text'][0]['segments_json'])
        nested = None
        for _ in range(80):
            nested = [nested]
        segments[0]['unused_raw'] = nested
        package['tables']['derived_text'][0]['segments_json'] = json.dumps(segments)
        with patch.object(packages, '_select_quote', side_effect=AssertionError('projection must not start')):
            result = packages.validate_metadata_package(rehash(package), budget=VerificationBudget(max_depth=10))
        self.assertEqual([item['code'] for item in result['errors']], ['verification_limit_exceeded'])

    def test_segment_selector_does_not_validate_or_charge_unused_bad_words(self):
        package = fixture()
        segments = json.loads(package['tables']['derived_text'][0]['segments_json'])
        segments[0]['words'] = [{'word': 'irrelevant', 'start': float('nan')}] * 100
        package['tables']['derived_text'][0]['segments_json'] = json.dumps(segments)
        budget = VerificationBudget(max_projection_visits=100)
        result = packages.validate_metadata_package(rehash(package), budget=budget)
        self.assertTrue(result['valid'], result)
        self.assertEqual(budget.projection_visits, 3 + len(segments[0]['text']))

    def test_projection_budget_checked_before_real_selector(self):
        package = fixture()
        with patch.object(packages, '_select_quote', side_effect=AssertionError('expensive projection not permitted')):
            result = packages.validate_metadata_package(package, budget=VerificationBudget(max_projection_visits=1))
        self.assertEqual([item['code'] for item in result['errors']], ['verification_limit_exceeded'])

    def test_cached_duplicate_key_errors_never_repeat_large_source_key(self):
        package = fixture()
        key = 'private-large-key-' * 10000
        raw = '[{"' + key + '":0,"' + key + '":1}]'
        package['tables']['derived_text'][0]['segments_json'] = raw
        anchor = package['tables']['evidence_anchors'][0]
        package['tables']['evidence_anchors'] = [dict(anchor, id=1000 + i) for i in range(8)]
        original = packages._strict_stored_json
        parsed = []
        def observe(value, **kwargs):
            if value == raw:
                parsed.append(True)
            return original(value, **kwargs)
        with patch.object(packages, '_strict_stored_json', side_effect=observe):
            result = packages.validate_metadata_package(rehash(package))
        self.assertFalse(result['valid'])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(result['errors']), 8)
        self.assertTrue(all(len(item['message']) < 100 for item in result['errors']))
        self.assertNotIn('private-large-key-', json.dumps(result))
        self.assertEqual(package['tables']['derived_text'][0]['segments_json'], raw)

    def test_invalid_budget_configuration_rejected(self):
        for value in (0, -1, True, 1.0, '5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                VerificationBudget(max_nodes=value)
