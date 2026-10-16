from server.app.catalog.export import _csv_guard


def test_csv_formula_guard_prefixes_only_text_that_spreadsheets_execute():
    for value in ('=HYPERLINK("http://example.org")', '+1', '-2+3', '@SUM(A1)', '\tx', '\rx'):
        assert _csv_guard(value) == "'" + value
    for value in ('plain.pdf', 'a=b', '', 5, -3, None):
        assert _csv_guard(value) == value
