"""Raw decoded CSV records use the same physical-line grammar as csv.reader."""
from pathlib import Path
import tempfile
import unittest

from server.app.scanner import ScanLimits, scan_sources


class ScannerCsvPhysicalLineTests(unittest.TestCase):
    def scan(self, text, *, encoding='utf-8', limits=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.csv'
            raw = text.encode(encoding)
            path.write_bytes(raw)
            result = scan_sources(Path(directory), limits=limits)
            self.assertEqual(path.read_bytes(), raw)
            return result, result['tables'][0]

    def test_nonphysical_unicode_separators_remain_in_exact_record(self):
        for separator in ('\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
            with self.subTest(separator=repr(separator)):
                text = f'name,value\nalpha{separator}beta,x\nsecond,y\n'
                result, table = self.scan(text)
                self.assertEqual([row['raw_record'] for row in table['rows']],
                                 ['name,value\n', f'alpha{separator}beta,x\n', 'second,y\n'])
                self.assertEqual(table['rows'][1]['cells'][0]['value'], f'alpha{separator}beta')
                self.assertEqual([row['start_line'] for row in table['rows']], [1, 2, 3])
                self.assertTrue(all(row['raw_record_complete'] for row in table['rows']))
                self.assertTrue(table['complete'])
                self.assertTrue(result['coverage']['complete'])

    def test_cr_lf_crlf_and_mixed_endings_are_preserved(self):
        for endings in [('\r',)*3, ('\n',)*3, ('\r\n',)*3, ('\r', '\r\n', '\n')]:
            with self.subTest(endings=endings):
                records = [value + ending for value, ending in zip(('h,v', 'a,1', 'b,2'), endings)]
                result, table = self.scan(''.join(records))
                self.assertEqual([row['raw_record'] for row in table['rows']], records)
                self.assertTrue(result['coverage']['complete'])
                self.assertEqual(result['files'][0]['parse_status'], 'complete')

    def test_utf16_bare_cr_preamble_and_quoted_multiline_raw_text(self):
        text = 'sep=;\rh;v\r"alpha\r\nbeta\rgamma\u2028delta";x\rlast;y'
        result, table = self.scan(text, encoding='utf-16')
        self.assertEqual(table['preamble'], 'sep=;\r')
        self.assertEqual(table['rows'][1]['raw_record'], '"alpha\r\nbeta\rgamma\u2028delta";x\r')
        self.assertEqual(table['rows'][1]['cells'][0]['value'], 'alpha\r\nbeta\rgamma\u2028delta')
        self.assertEqual([row['start_line'] for row in table['rows']], [2, 3, 6])
        self.assertEqual(table['rows'][2]['raw_record'], 'last;y')
        self.assertTrue(result['coverage']['complete'])

    def test_malformed_fragment_starts_after_last_complete_physical_record(self):
        text = 'h,v\r"alpha\u2028beta",ok\r"unfinished\r\nrest'
        result, table = self.scan(text)
        issue = next(item for item in result['issues'] if item['code'] == 'csv_record_parse_failed')
        self.assertEqual(issue['raw_fragment'], '"unfinished\r\nrest')
        self.assertTrue(issue['raw_fragment_complete'])
        self.assertEqual(issue['start_line'], 3)
        self.assertEqual(table['rows'][1]['raw_record'], '"alpha\u2028beta",ok\r')
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['files'][0]['parse_status'], 'failed')

    def test_fragment_and_record_truncation_stay_explicit_and_bounded(self):
        result, table = self.scan('h,v\r"' + 'x'*50, limits=ScanLimits(max_cell_characters=8))
        issue = next(item for item in result['issues'] if item['code'] == 'csv_record_parse_failed')
        self.assertEqual(issue['raw_fragment'], '"' + 'x'*7)
        self.assertFalse(issue['raw_fragment_complete'])
        self.assertFalse(result['coverage']['complete'])
        result, table = self.scan('h,v\na\u2028b,x\n', limits=ScanLimits(max_total_cell_characters=10))
        self.assertLessEqual(result['coverage']['cell_characters_captured'], 10)
        self.assertFalse(table['rows'][1]['raw_record_complete'])
        self.assertFalse(table['complete'])


if __name__ == '__main__':
    unittest.main()
