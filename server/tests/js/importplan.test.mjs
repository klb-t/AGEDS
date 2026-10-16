import test from 'node:test';
import assert from 'node:assert/strict';
import {bulkPayload, decisionFromFields} from '../../app/static/importplan.mjs';

test('decision payload keeps importer, opt-ins and typed parameters', () => {
  const decision = decisionFromFields({
    itemId: '7', status: 'approved',
    entries: [['importer', 'location_history'], ['optin:enable_location', 'true'], ['param:coarse_decimals', ' 1 '],
              ['param:profile_id', '']],
    optInBoxes: [['optin:enable_location', true], ['optin:enable_browser_history', false]],
  });
  assert.deepEqual(decision, {
    itemId: 7, status: 'approved', importer: 'location_history',
    optIns: {enable_location: true, enable_browser_history: false},
    parameters: {coarse_decimals: 1, profile_id: null},
  });
});

test('an empty importer choice is not sent and source-like text stays a string', () => {
  const decision = decisionFromFields({itemId: 3, entries: [['importer', ''], ['param:chat_member', '<img src=x onerror=alert(1)>.txt']]});
  assert.deepEqual(decision, {itemId: 3, parameters: {chat_member: '<img src=x onerror=alert(1)>.txt'}});
});

test('bulk payloads', () => {
  assert.deepEqual(bulkPayload('approve-ready', []), {action: 'approve'});
  assert.deepEqual(bulkPayload('skip-selected', ['4', '5']), {action: 'skip', itemIds: [4, 5]});
  assert.deepEqual(bulkPayload('approve-selected', ['9', 'x', '-1']), {action: 'approve', itemIds: [9]});
  assert.throws(() => bulkPayload('approve-selected', []), /Nie zaznaczono/);
});
