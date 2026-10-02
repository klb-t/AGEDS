"""Independent exact-record acceptance through the production source scanner."""
from pathlib import Path
import tempfile
import unittest

from server.app.scanner import ScanLimits, scan_sources


class CsvRawRecordFidelityTests(unittest.TestCase):
    def scan(self, text, *, encoding='utf-8', limits=None, suffix='.csv'):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            path = source / ('synthetic' + suffix)
            original = text.encode(encoding)
            path.write_bytes(original)
            before = path.stat()
            result = scan_sources(source, limits=limits)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(list(source.iterdir()), [path])
            return result, result['tables'][0]

    def assert_rows(self, table, records, starts):
        self.assertEqual([row['raw_record'] for row in table['rows']], records)
        self.assertEqual([row['start_line'] for row in table['rows']], starts)
        self.assertTrue(all(row['raw_record_complete'] for row in table['rows']))

    def test_nonphysical_separators_remain_cell_content_across_physical_endings(self):
        for separator in ('\u2028', '\u2029', '\x85', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e'):
            for ending in ('\r', '\n', '\r\n'):
                for quoted in (False, True):
                    with self.subTest(separator=repr(separator), ending=repr(ending), quoted=quoted):
                        cell = 'left' + separator + 'right'
                        record = ('"' + cell + '"' if quoted else cell) + ',x' + ending
                        records = ['A,B' + ending, record, 'tail,y']
                        result, table = self.scan(''.join(records))
                        self.assert_rows(table, records, [1, 2, 3])
                        self.assertEqual(table['rows'][1]['cells'][0]['value'], cell)
                        self.assertTrue(table['complete'])
                        self.assertTrue(result['coverage']['complete'])

    def test_utf16_mixed_multiline_quote_and_preamble_keep_exact_boundaries(self):
        for encoding in ('utf-8-sig', 'utf-16'):
            with self.subTest(encoding=encoding):
                preamble = 'sep=;\r'
                records = ['A;B\r\n', '"α\u2028β\rγ\nδ\r\nε\x85ζ";x\n', 'tail;y']
                result, table = self.scan(preamble + ''.join(records), encoding=encoding)
                self.assertEqual(table['preamble'], preamble)
                self.assert_rows(table, records, [2, 3, 7])
                self.assertEqual(table['rows'][1]['cells'][0]['value'], 'α\u2028β\rγ\nδ\r\nε\x85ζ')
                self.assertTrue(result['coverage']['complete'])

    def test_tsv_embedded_controls_do_not_change_physical_line_numbers(self):
        records = ['A\tB\r', 'a\x0bb\x0cc\u2029d\tx\r', 'tail\ty']
        result, table = self.scan(''.join(records), suffix='.tsv')
        self.assertEqual(table['delimiter'], '\t')
        self.assert_rows(table, records, [1, 2, 3])
        self.assertTrue(result['coverage']['complete'])

    def test_unclosed_trailing_quote_keeps_fragment_and_start_after_multiline_row(self):
        for ending in ('\n', '\r', '\r\n'):
            with self.subTest(ending=repr(ending)):
                prefix = ['A,B' + ending, '"good' + ending + 'continued",x' + ending]
                fragment = 'bad,"raw\u2028middle' + ending + 'still\x0bopen'
                result, table = self.scan(''.join(prefix) + fragment)
                self.assert_rows(table, prefix, [1, 2])
                issue = next(item for item in result['issues'] if item['code'] == 'csv_record_parse_failed')
                self.assertEqual(issue['start_line'], 4)
                self.assertEqual(issue['raw_fragment'], fragment)
                self.assertTrue(issue['raw_fragment_complete'])
                self.assertFalse(table['complete'])
                self.assertFalse(result['coverage']['complete'])
                self.assertEqual(result['files'][0]['parse_status'], 'failed')

    def test_row_limit_preserves_admitted_raw_records_and_reports_partial(self):
        records = ['A,B\r', '"one\u2028two\nthree",x\r\n', 'omitted,y']
        result, table = self.scan(''.join(records), limits=ScanLimits(max_rows_per_file=2))
        self.assert_rows(table, records[:2], [1, 2])
        self.assertFalse(table['complete'])
        self.assertFalse(result['coverage']['complete'])
        self.assertIn('row_limit', [item['code'] for item in result['issues']])

    def test_raw_record_truncation_never_claims_complete(self):
        result, table = self.scan('A,B\n"abcdef\u2028ghij",x', limits=ScanLimits(max_cell_characters=8, max_total_cell_characters=23))
        row = table['rows'][1]
        self.assertEqual(row['raw_record'], '"abcdef\u2028')
        self.assertFalse(row['raw_record_complete'])
        self.assertEqual(row['start_line'], 2)
        self.assertFalse(table['complete'])
        self.assertFalse(result['coverage']['complete'])

    def test_malformed_fragment_truncation_retains_exact_prefix(self):
        fragment = 'x,"a\u2028b\r\ncdefghijkl'
        result, table = self.scan('A,B\r\n' + fragment, limits=ScanLimits(max_cell_characters=8))
        issue = next(item for item in result['issues'] if item['code'] == 'csv_record_parse_failed')
        self.assertEqual(issue['raw_fragment'], fragment[:8])
        self.assertFalse(issue['raw_fragment_complete'])
        self.assertEqual(issue['start_line'], 2)
        self.assertFalse(table['complete'])


if __name__ == '__main__':
    unittest.main()
