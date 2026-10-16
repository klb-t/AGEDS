"""Dashboard list and events.csv use projected time, never import time or string order."""
from __future__ import annotations

import csv
import io

from server.tests.test_timeline_api import add, client, seed  # noqa: F401  (fixture reuse)


def test_events_csv_orders_by_projected_time_and_guards_formulas(client):  # noqa: F811
    ids = seed(client)
    formula = add(client, ts_start='2023-12-31T23:00:00+00:00', body='=HYPERLINK("http://example.org")')
    response = client.get('/export/events.csv')
    assert response.status_code == 200
    assert response.headers['x-ageds-csv-formula-guard'] == 'apostrophe_prefix_v1'
    rows = list(csv.DictReader(io.StringIO(response.text)))
    order = [int(r['id']) for r in rows]
    # Dated events by projected UTC time (the unknown-zone WhatsApp row sorts at its
    # local time as if UTC, per the display rule), the undated row last.
    assert order[0] == formula and order[-1] == ids['undated']
    assert order.index(ids['sms']) < order.index(ids['chat'])
    assert set(order) == {formula, *ids.values()}
    by_id = {int(r['id']): r for r in rows}
    assert by_id[formula]['body'] == '\'=HYPERLINK("http://example.org")'
    assert by_id[ids['sms']]['body'] == '<script>window.x=1</script>'


def test_dashboard_lists_newest_projected_first_and_undated_last(client):  # noqa: F811
    ids = seed(client)
    page = client.get('/').text
    positions = {name: page.find(text) for name, text in (('chat', 'pytanie'), ('sms', '&lt;script&gt;window.x=1'), ('undated', 'bez czasu'))}
    assert all(p > 0 for p in positions.values()), positions
    assert positions['chat'] < positions['sms'] < positions['undated']
