"""Figure failures must remain recoverable and distinguish absence from outages."""
import os
import sys
import tempfile
import time
from threading import Event
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import figures, gui


class FetchStateTests(unittest.TestCase):
    def test_network_failure_is_not_no_figure(self):
        with patch.object(figures, 'fetch_figure_html', side_effect=figures.requests.Timeout), \
             patch.object(figures, 'fetch_figure_pdf', return_value=([], [])), \
             patch.object(figures, 'fitz', object()):
            result = figures.fetch_paper_figure('paper', object())
        self.assertEqual(result['source'], 'request_failed')

    def test_successful_empty_sources_report_no_usable_figures(self):
        with patch.object(figures, 'fetch_figure_html', return_value=([], [])), \
             patch.object(figures, 'fetch_figure_pdf', return_value=([], [])), \
             patch.object(figures, 'fitz', object()):
            result = figures.fetch_paper_figure('paper', object())
        self.assertEqual(result['source'], 'no_figure')

    def test_pdf_can_recover_html_download_failure(self):
        with patch.object(figures, 'fetch_figure_html', side_effect=figures.requests.Timeout), \
             patch.object(figures, 'fetch_figure_pdf', return_value=([(b'png', '.png')], [])), \
             patch.object(figures, 'fitz', object()):
            result = figures.fetch_paper_figure('paper', object())
        self.assertEqual(result['source'], 'pdf')

    def test_missing_pdf_support_is_not_evidence_of_no_figures(self):
        with patch.object(figures, 'fetch_figure_html', return_value=([], [])), \
             patch.object(figures, 'fitz', None):
            result = figures.fetch_paper_figure('paper', object())
        self.assertEqual(result['source'], 'no_html_figure')

    def test_transient_failure_retried_once_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(figures, 'REQUEST_INTERVAL', 0), \
             patch.object(figures, 'fetch_paper_figure', side_effect=[
                 {'figures': [], 'captions': [], 'source': 'request_failed'},
                 {'figures': [(b'png', '.png')], 'captions': ['Caption'], 'source': 'html'},
             ]) as fetch:
            result = figures.fetch_figures_for_digest([{'id': 'p', 'score': 4}], tmp)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn('p', result['papers'])
        self.assertEqual(result['failed'], {})

    def test_permanent_network_failure_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(figures, 'REQUEST_INTERVAL', 0), \
             patch.object(figures, 'fetch_paper_figure', return_value={
                 'figures': [], 'captions': [], 'source': 'request_failed'
             }) as fetch:
            result = figures.fetch_figures_for_digest([{'id': 'p', 'score': 4}], tmp)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result['failed'], {'p': 'request_failed'})

    def test_parser_migration_persists_when_images_are_already_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'p.png').write_bytes(b'image')
            figures.write_sidecar(tmp, '2026-09-09', {
                'pv': 2, 'failed': {}, 'papers': {'p': {
                    'files': ['p.png'], 'depth': figures.MAX_FIGURES,
                    'capv': figures.CAPTION_VERSION, 'captions': ['Caption']
                }}
            })
            with patch.object(figures, 'fetch_paper_figure') as fetch:
                figures.fetch_figures_for_digest([{'id': 'p', 'score': 4}], tmp,
                    digest_dir=tmp, digest_date='2026-09-09')
            fetch.assert_not_called()
            self.assertEqual(figures.load_sidecar(tmp, '2026-09-09')['pv'], figures.PARSER_VERSION)

    def test_partial_download_keeps_caption_indices_and_remains_incomplete(self):
        class Response:
            status_code = 200
            text = '<figure><img src="1.png"><figcaption>One</figcaption></figure><figure><img src="2.png"><figcaption>Two</figcaption></figure><figure><img src="3.png"><figcaption>Three</figcaption></figure>'
            url = 'https://arxiv.org/html/p'
        def download(session, url, src):
            return None if src == '2.png' else (src.encode(), '.png')
        with patch.object(figures, '_get', return_value=Response()), \
             patch.object(figures, '_download_image', side_effect=download):
            images, captions = figures.fetch_figure_html('p', object(), max_figures=3)
        self.assertEqual(images, [(b'1.png', '.png')])
        self.assertEqual(captions, ['One', 'Two', 'Three'])
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(figures, 'fetch_paper_figure', return_value={
                 'figures': images, 'captions': captions, 'source': 'html'
             }):
            result = figures.fetch_figures_for_digest([{'id': 'p', 'score': 4}], tmp, max_figures=3)
        self.assertEqual(result['papers']['p']['captions'], ['One'])
        self.assertLess(result['papers']['p']['depth'], 3)

    def test_disk_failure_with_cached_pdf_prefix_remains_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'p.png').write_bytes(b'first')
            figures.write_sidecar(tmp, '2026-09-09', {'papers': {'p': {
                'files': ['p.png'], 'captions': [''], 'depth': 1,
                'capv': figures.CAPTION_VERSION, 'source': 'pdf',
            }}, 'failed': {}, 'pv': figures.PARSER_VERSION})
            with patch.object(figures, 'fetch_paper_figure', return_value={
                'figures': [(b'second', '.png')], 'captions': [''], 'source': 'pdf'
            }), patch.object(figures, '_write_figure', side_effect=OSError('disk full')):
                result = figures.fetch_figures_for_digest([{'id': 'p', 'score': 4}], tmp,
                    digest_dir=tmp, digest_date='2026-09-09', max_figures=3)
            self.assertLess(result['papers']['p']['depth'], 3)
            self.assertEqual((Path(tmp) / 'p.png').read_bytes(), b'first')
            with patch.object(figures, 'fetch_paper_figure', return_value={
                'figures': [(b'second', '.png'), (b'third', '.png')],
                'captions': ['', ''], 'source': 'pdf'
            }) as fetch:
                result = figures.fetch_figures_for_digest([{'id': 'p', 'score': 4}], tmp,
                    digest_dir=tmp, digest_date='2026-09-09', max_figures=3)
            self.assertEqual(fetch.call_args.kwargs['skip'], 1)
            self.assertEqual(result['papers']['p']['files'], ['p.png', 'p-2.png', 'p-3.png'])
            self.assertEqual(result['papers']['p']['depth'], 3)



class BackfillWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for mock in [patch.object(gui, '_figures_dir', return_value=str(self.root / 'figures')),
                     patch.object(gui, '_digests_dir', return_value=str(self.root / 'digests')),
                     patch.object(gui, 'get_digest_path_for_date', side_effect=lambda day: day),
                     patch.object(gui, 'parse_digest', return_value={'tiers': [{'papers': [{'paper_id': 'p', 'score': 4}]}]}),
                     patch.object(gui, 'apply_to_digest')]:
            mock.start()
            self.addCleanup(mock.stop)
        gui._figure_backfill_queue.clear()

    def wait_for_idle(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with gui._figure_backfill_lock:
                idle = not gui._figure_backfill_state['running'] and not gui._figure_backfill_queue
            if idle:
                return
            time.sleep(.01)
        self.fail('Figure worker did not stop')

    def complete(self, papers, figures_dir, digest_dir=None, digest_date=None, **kwargs):
        Path(figures_dir).mkdir(parents=True, exist_ok=True)
        (Path(figures_dir) / 'p.png').write_bytes(b'new image')
        figures.write_sidecar(digest_dir, digest_date, {
            'pv': figures.PARSER_VERSION, 'failed': {}, 'papers': {'p': {
                'files': ['p.png'], 'captions': ['Caption'], 'source': 'html',
                'capv': figures.CAPTION_VERSION, 'depth': figures.MAX_FIGURES,
            }}
        })

    def test_navigation_queues_second_digest_without_losing_it(self):
        started, release = Event(), Event()
        days = []
        def fetch(*args, **kwargs):
            days.append(kwargs['digest_date'])
            if len(days) == 1:
                started.set()
                release.wait(5)
            self.complete(*args, **kwargs)
        with patch.object(gui, 'fetch_figures_for_digest', side_effect=fetch):
            try:
                self.assertTrue(gui._start_figure_backfill('2026-09-08'))
                self.assertTrue(started.wait(5))
                self.assertFalse(gui._start_figure_backfill('2026-09-09'))
                self.assertIn('2026-09-09', gui._figure_backfill_queue)
            finally:
                release.set()
                self.wait_for_idle()
        self.assertEqual(days, ['2026-09-08', '2026-09-09'])

    def test_explicit_retry_replaces_corrupt_cache(self):
        directory = self.root / 'figures'
        directory.mkdir()
        (directory / 'p.png').write_bytes(b'corrupt')
        removed = []
        def fetch(*args, **kwargs):
            removed.append(not (directory / 'p.png').exists())
            self.complete(*args, **kwargs)
        with patch.object(gui, 'fetch_figures_for_digest', side_effect=fetch):
            self.assertTrue(gui._start_figure_backfill('2026-09-09', retry_paper='p'))
            self.wait_for_idle()
        self.assertEqual(removed, [True])
        self.assertEqual((directory / 'p.png').read_bytes(), b'new image')


class FigureRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = gui.app.test_client()
        self.digest = {'tiers': [{'papers': [{'paper_id': 'p', 'score': 4}, {'paper_id': 'low', 'score': 3}]}]}
        for mock in [patch.object(gui, '_needs_setup', return_value=False),
                     patch.object(gui, 'get_digest_path_for_date', return_value='digest.md'),
                     patch.object(gui, 'parse_digest', return_value=self.digest),
                     patch.object(gui, 'apply_to_digest')]:
            mock.start()
            self.addCleanup(mock.stop)

    def test_retry_queues_only_requested_paper_without_running_pipeline(self):
        with patch.object(gui, '_start_figure_backfill', return_value=True) as start, \
             patch.object(gui, '_start_pipeline') as pipeline:
            response = self.client.post('/digest/2026-09-09/figures-retry', json={'paper_id': 'p'})
        self.assertEqual(response.status_code, 202)
        start.assert_called_once_with('2026-09-09', retry_paper='p')
        pipeline.assert_not_called()

    def test_retry_rejects_non_object_json(self):
        response = self.client.post('/digest/2026-09-09/figures-retry', json=['p'])
        self.assertEqual(response.status_code, 400)

    def test_busy_worker_returns_retryable_conflict(self):
        with patch.object(gui, '_start_figure_backfill', return_value=False):
            response = self.client.post('/digest/2026-09-09/figures-retry', json={'paper_id': 'p'})
        self.assertEqual(response.status_code, 409)

    def test_retry_rejects_unknown_and_low_rated_papers(self):
        with patch.object(gui, '_start_figure_backfill') as start:
            for pid in ['unknown', 'low', '../escape', None]:
                response = self.client.post('/digest/2026-09-09/figures-retry', json={'paper_id': pid})
                self.assertEqual(response.status_code, 404)
        start.assert_not_called()

    def test_status_preserves_failure_reasons(self):
        with patch.object(gui, 'load_figure_sidecar', return_value={
            'papers': {}, 'failed': {'p': 'request_failed', 'other': 'no_figure'}
        }):
            response = self.client.get('/digest/2026-09-09/figures-status')
        self.assertEqual(response.json['failed'], {'p': 'request_failed', 'other': 'no_figure'})


class EmptyDigestTests(unittest.TestCase):
    def test_latest_content_skips_empty_and_missing_digests(self):
        with patch.object(gui, 'get_available_dates', return_value=['2026-09-09', '2026-09-08', '2026-09-07']), \
             patch.object(gui, 'get_digest_path_for_date', side_effect=['empty', None, 'full']), \
             patch.object(gui, 'parse_digest', side_effect=[{'total_papers': 0}, {'total_papers': 2}]):
            self.assertEqual(gui._latest_content_date(), '2026-09-07')

    def test_empty_page_has_one_message_and_content_destination(self):
        with gui.app.test_request_context(), \
             patch.dict(gui.app.jinja_env.globals, latest_content_date=lambda: '2026-09-07', digest_status_map=lambda: {}):
            page = gui.render_template_string(gui.NO_DIGEST_TEMPLATE, selected_date='2026-09-08',
                today_str='2026-09-09', is_update_day=True, available_dates=['2026-09-08'], custom_message='Long obsolete explanation')
        self.assertIn('href="/digest/2026-09-07"', page)
        self.assertIn('No digest for this date', page)
        self.assertNotIn('No data available', page)
        self.assertNotIn('Available:', page)
        self.assertNotIn('Long obsolete explanation', page)
        self.assertNotIn('class="toolbar"', page)

    def test_future_date_has_no_generate_action(self):
        with gui.app.test_request_context(), \
             patch.dict(gui.app.jinja_env.globals, latest_content_date=lambda: None, digest_status_map=lambda: {}):
            page = gui.render_template_string(gui.NO_DIGEST_TEMPLATE, selected_date='2026-09-10',
                today_str='2026-09-09', is_update_day=True)
        self.assertNotIn('/run?date=', page)
        self.assertNotIn('View latest digest', page)


if __name__ == '__main__':
    unittest.main()
