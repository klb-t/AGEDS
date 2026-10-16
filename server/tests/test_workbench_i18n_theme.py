"""i18n (PL default, NL, EN; precedence; visible fallback), theme tokens/contrast and page rendering."""
from __future__ import annotations

import json
import logging
import re
from unittest.mock import patch

from server.app.workbench import i18n, theme
from server.tests.test_workbench_support import WorkbenchCase

PAGES = ('/', '/workbench', '/workbench?q=umowa', '/workbench/folders', '/workbench/bookmarks', '/integrations')


class NegotiationTests(WorkbenchCase):
    def test_precedence_query_cookie_header_default(self):
        self.assertEqual(i18n.negotiate('en', 'nl', 'pl'), ('en', 'query'))
        self.assertEqual(i18n.negotiate('xx', 'nl', 'en'), ('nl', 'cookie'))
        self.assertEqual(i18n.negotiate(None, 'zz', 'de-DE,nl-BE;q=0.8,en;q=0.9'), ('en', 'accept-language'))
        self.assertEqual(i18n.negotiate(None, None, 'de, fr'), ('pl', 'default'))
        self.assertEqual(i18n.negotiate(None, None, None), ('pl', 'default'))

    def test_accept_language_parsing_is_bounded_and_strict(self):
        self.assertEqual(i18n.parse_accept_language('en;q=0, nl;q=0.5'), [('nl', 0.5)])
        self.assertEqual(i18n.parse_accept_language('nl;q=abc, en'), [('en', 1.0)])
        self.assertEqual(i18n.parse_accept_language('<script>, pl'), [('pl', 1.0)])
        self.assertEqual(i18n.parse_accept_language('x' * 2000), [])
        self.assertEqual(len(i18n.parse_accept_language(','.join(['de'] * 100))), 32)
        self.assertEqual(i18n.parse_accept_language('en-US;q=0.8, nl;q=0.8'), [('en', 0.8), ('nl', 0.8)])

    def test_http_language_selection(self):
        def lang(response):
            return re.search(r'<html lang="([a-z]+)"', response.text).group(1)
        self.assertEqual(lang(self.client.get('/')), 'pl')
        self.assertEqual(lang(self.client.get('/', headers={'Accept-Language': 'nl-NL,nl;q=0.9'})), 'nl')
        self.client.cookies.set('ageds_lang', 'en')
        self.assertEqual(lang(self.client.get('/', headers={'Accept-Language': 'nl'})), 'en')
        self.assertEqual(lang(self.client.get('/?lang=nl')), 'nl')
        self.client.cookies.set('ageds_lang', 'klingon')
        self.assertEqual(lang(self.client.get('/?lang=zz')), 'pl')
        self.client.cookies.clear()
        info = self.client.get('/api/workbench/i18n', params={'lang': 'en'}).json()
        self.assertEqual((info['lang'], info['source']), ('en', 'query'))


class CatalogTests(WorkbenchCase):
    def test_catalogs_complete_and_placeholders_consistent(self):
        report = i18n.catalog_report()
        self.assertTrue(report['complete'], json.dumps(report['languages'], ensure_ascii=False)[:2000])
        for lang in i18n.LANGUAGES:
            for key, value in i18n.catalog(lang).items():
                if key != '_meta':
                    self.assertTrue(value, (lang, key))

    def test_polish_plural_rules(self):
        cases = {1: 'one', 2: 'few', 4: 'few', 5: 'many', 12: 'many', 14: 'many', 22: 'few', 25: 'many', 101: 'many', 0: 'many'}
        for n, expected in cases.items():
            self.assertEqual(i18n.plural_category('pl', n), expected, n)
        self.assertEqual(i18n.translate('pl', 'index.search.count', n=1), '1 wynik')
        self.assertEqual(i18n.translate('pl', 'index.search.count', n=3), '3 wyniki')
        self.assertEqual(i18n.translate('pl', 'index.search.count', n=12), '12 wyników')
        self.assertEqual(i18n.translate('en', 'index.search.count', n=1), '1 result')
        self.assertEqual(i18n.translate('nl', 'index.search.count', n=2), '2 resultaten')

    def test_missing_key_falls_back_visibly_and_is_logged(self):
        patched = dict(i18n.catalog('en'))
        del patched['wb.title']
        i18n._reported.clear()
        original = i18n.catalog
        with patch.object(i18n, 'catalog', side_effect=lambda lang: patched if lang == 'en' else original(lang)):
            with self.assertLogs('server.app.workbench.i18n', level='WARNING') as logs:
                self.assertEqual(i18n.translate('en', 'wb.title'), 'Warsztat')
                self.assertEqual(i18n.translate('en', 'no.such.key'), '⟦no.such.key⟧')
        self.assertTrue(any('wb.title' in line and 'WARNING' in line for line in logs.output))
        self.assertTrue(any('no.such.key' in line and 'ERROR' in line for line in logs.output))
        self.assertEqual(i18n.translate('pl', 'wb.missing.never'), '⟦wb.missing.never⟧')
        self.assertEqual(i18n.translate('xx', 'wb.title'), 'Warsztat')

    def test_malformed_catalog_is_rejected(self):
        for bad in ({'k': ''}, {'k': 3}, {'k': {'one': 'x'}}, {'k': {'other': 'x', 'plural': 'y'}}, []):
            with self.assertRaises(i18n.CatalogError):
                i18n._validate_catalog('pl', bad)


class PageRenderingTests(WorkbenchCase):
    def test_every_page_renders_in_every_language_without_missing_keys(self):
        for lang in i18n.LANGUAGES:
            for path in PAGES + (f'/artifact/{self.artifact}', f'/workbench/artifacts/{self.artifact}'):
                with self.subTest(lang=lang, path=path):
                    response = self.client.get(path, params={'lang': lang})
                    self.assertEqual(response.status_code, 200, response.text[:500])
                    self.assertNotIn('⟦', response.text)
                    self.assertIn(f'lang="{lang}"', response.text)

    def test_shared_nav_is_translated_and_source_text_is_not(self):
        en = self.client.get('/workbench', params={'lang': 'en', 'q': 'biuro'}).text
        self.assertIn('>Workbench</a>', en)
        self.assertIn('>Folders</a>', en)
        self.assertIn('biuro &lt;script&gt;x&lt;/script&gt; zamknięte', en, 'source text is shown as stored')
        nl = self.client.get('/', params={'lang': 'nl'}).text
        self.assertIn('>Werkplaats</a>', nl)
        pl = self.client.get('/').text
        self.assertIn('>Warsztat</a>', pl)
        self.assertIn('>Integracje</a>', pl, 'the area label is the Polish source for nav keys')

    def test_existing_polish_strings_are_kept(self):
        page = self.client.get(f'/artifact/{self.artifact}').text
        for text in ('Proweniencja', 'Cytat ze wskazanej wersji', 'Zapisane cytaty', 'Wczytaj starsze cytaty', 'Odtwórz zapisany cytat'):
            self.assertIn(text, page)
        en = self.client.get(f'/artifact/{self.artifact}', params={'lang': 'en'}).text
        self.assertIn('Provenance', en)
        self.assertIn('transkrypt #', en, 'citation list items mirror citations.mjs and stay Polish')
        self.assertIn('Co wiemy / czego nie wiemy', page)
        self.assertIn("What we know / what we don", en)

    def test_artifact_page_survives_a_failing_knowledge_summary(self):
        from server.app.workbench import knowledge
        with patch.object(knowledge, 'artifact_knowledge', side_effect=OSError('disk')):
            response = self.client.get(f'/artifact/{self.artifact}')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Podsumowanie jest chwilowo niedostępne', response.text)


class ThemeTests(WorkbenchCase):
    def test_contrast_meets_wcag_aa_for_text_tokens(self):
        report = theme.contrast_report()
        for name, data in report['themes'].items():
            failing = [p for p in data['pairs'] if not p['aa_normal_text']]
            self.assertEqual(failing, [], name)
        self.assertAlmostEqual(theme.contrast('#000000', '#ffffff'), 21.0, places=5)
        self.assertAlmostEqual(theme.contrast('#777777', '#777'), 1.0, places=5)

    def test_light_blocks_must_match_and_tokens_exist(self):
        css = theme.CSS_PATH.read_text(encoding='utf-8')
        tokens = theme.theme_tokens(css)
        self.assertEqual(set(tokens['dark']), set(tokens['light']))
        broken = css.replace('--text:#1f2328', '--text:#1f2329', 1)
        with self.assertRaises(ValueError):
            theme.theme_tokens(broken)
        self.assertNotRegex(re.sub(r':root[^{]*\{[^}]*\}', '', css.split('*{box-sizing')[1]), r'#[0-9a-fA-F]{6}\b(?!\w)',
                            'outside the token blocks, colours come from variables only')

    def test_server_renders_explicit_theme_from_cookie_only_on_supporting_pages(self):
        self.client.cookies.set('ageds_theme', 'light')
        page = self.client.get('/').text
        self.assertIn('data-theme-support="light dark" data-theme="light"', page)
        self.client.cookies.set('ageds_theme', 'neon')
        self.assertNotIn('data-theme="', self.client.get('/').text)
        self.client.cookies.clear()
        config = json.loads(re.search(r'<script type="application/json" id="ageds-shell-config">(.*?)</script>',
                                      self.client.get('/workbench').text).group(1))
        self.assertEqual((config['lang'], config['theme'], config['keysUrl']), ('pl', 'auto', '/static/workbench-keys.json'))

    def test_shell_config_cannot_break_out_of_its_script(self):
        page = self.client.get('/', params={'lang': '</script><script>alert(1)</script>'}).text
        self.assertNotIn('<script>alert(1)', page)
