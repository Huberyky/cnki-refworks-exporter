"""Real Chromium tests against intercepted local fixtures, with no CNKI traffic.

Run with the project's .venv Python after Playwright Chromium is installed.
"""
import html
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cnki_crawler as core
import cnki_refworks_v2 as v2

@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is optional for offline tests')
class BrowserExportTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        fixture = (Path(__file__).parent / 'fixtures' / 'cnki_export_fixture.html').read_text(encoding='utf-8')

        def route(request):
            url = request.request.url
            if urlsplit(url).path == '/export':
                text = parse_qs(urlsplit(url).query).get('payload', [''])[0]
                body = '<!doctype html><meta charset="utf-8"><title>虚构 Refworks 导出</title><button>复制到剪贴板</button><pre>' + html.escape(text) + '</pre><button>打印</button>'
            else:
                body = fixture
            request.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
        self.context.route('https://cnki-fixture.invalid/**', route)
        self.page = self.context.new_page()
        self.cfg = v2.load_config(core.BASE / 'config_v2.json')
        self.cfg['timeout_ms'] = 2000
        self.cfg['delay_seconds'] = 0
        self.cfg['export_ui']['manual_fallback'] = False
        self.task = core.make_tasks(self.cfg)[0]

    def tearDown(self):
        self.context.close()

    def run_mode(self, mode, batch_size=50):
        self.cfg['export_ui']['batch_size'] = batch_size
        self.page.goto('https://cnki-fixture.invalid/search?mode=' + mode)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            out = Path(temporary)
            store = core.Store(out / 'state.sqlite3')
            try:
                store.add_tasks([self.task])
                with patch('builtins.input', return_value='end'):
                    v2.run_task(self.context, self.page, self.task, self.cfg, store, out, max_pages=5)
                status = store.db.execute('SELECT status FROM tasks').fetchone()[0]
                self.assertEqual(status, 'collected_end_confirmed')
                fields = core.parse_refworks((out / 'all_refworks.txt').read_text(encoding='utf-8'))
                self.assertEqual(len(fields), 4)
                self.assertTrue(all((f['AD'] == ['虚构机构;'] for f in fields)))
                self.assertTrue(all((f['CN'] == ['00-0000/C'] for f in fields)))
                batch_count = store.db.execute("SELECT COUNT(*) FROM export_batches WHERE status='done'").fetchone()[0]
                self.assertEqual(batch_count, 2 if batch_size == 50 else 3)
                self.assertEqual(self.page.locator('tbody input:checked').count(), 0)
                self.assertEqual(len(self.context.pages), 1)
                if mode == 'popup' and batch_size == 50:
                    self.page.goto('https://cnki-fixture.invalid/search?mode=popup')
                    with patch('builtins.input', return_value='end'):
                        v2.run_task(self.context, self.page, self.task, self.cfg, store, out, max_pages=5)
                    stats = json.loads(store.db.execute('SELECT stats FROM tasks').fetchone()[0])
                    self.assertEqual(stats['batches_this_run'], 0)
                    self.assertEqual(store.db.execute('SELECT COUNT(*) FROM records').fetchone()[0], 4)
            finally:
                store.db.close()

    def test_popup_two_pages_and_dedup(self):
        self.run_mode('popup')

    def test_iframe_export_two_pages(self):
        self.run_mode('iframe')

    def test_page_splitting_respects_batch_limit(self):
        self.run_mode('popup', batch_size=2)

    def test_same_tab_export_restores_search_page(self):
        self.cfg['export_ui']['close_export'] = ''
        self.page.goto('https://cnki-fixture.invalid/search?mode=same-tab')
        entries = v2.snapshot(self.page, self.cfg)
        v2.select_batch(self.page, self.cfg, entries, entries)
        (native, fields, source), restore = v2.capture_batch(self.context, self.page, self.cfg, [e['title'] for e in entries])
        restored = v2.restore_results(self.context, self.page, self.cfg, *restore)
        self.assertEqual(len(fields), 3)
        self.assertEqual([e['title'] for e in restored], [e['title'] for e in entries])

    def test_partial_export_does_not_advance_or_commit(self):
        self.page.goto('https://cnki-fixture.invalid/search?mode=truncated')
        self.cfg['timeout_ms'] = 500
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            out = Path(temporary)
            store = core.Store(out / 'state.sqlite3')
            try:
                store.add_tasks([self.task])
                v2.run_task(self.context, self.page, self.task, self.cfg, store, out)
                self.assertEqual(store.db.execute('SELECT status FROM tasks').fetchone()[0], 'needs_retry')
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
                self.assertEqual(store.db.execute('SELECT status FROM export_batches').fetchone()[0], 'failed')
                self.assertEqual(v2.snapshot(self.page, self.cfg)[0]['title'], '测试民族文化甲')
                self.assertTrue(list((out / 'failed_batches').glob('*.txt')))
            finally:
                store.db.close()
if __name__ == '__main__':
    unittest.main()
