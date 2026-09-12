"""Real Chromium DOM coverage; install requirements-release.lock and Chromium."""
import base64
import re
from unittest.mock import patch
import pytest
playwright = pytest.importorskip('playwright.sync_api')
from src import gui

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')

@pytest.mark.parametrize('pid', ['2609.01234', 'astro-ph/0601001'])
def test_real_figure_dom_retry_refresh_and_rating(pid):
    paper = dict(paper_id=pid, score=5, title='Test paper', authors='A. Author',
                 abstract='Test abstract', categories='astro-ph.SR')
    with gui.app.test_request_context(), patch.dict(gui.app.jinja_env.globals, digest_status_map=lambda: {}):
        page_html = gui.render_template_string(gui.DIGEST_TEMPLATE,
            digest={'date': '2026-09-09', 'tiers': [{'name': 'Strongly Recommended', 'papers': [paper]}]},
            prefs={}, display_categories=[], display_categories_all=[], today_str='2026-09-09',
            full_abstracts={}, reason_html={}, figure_map={}, figure_pending=[pid],
            figure_failures={}, figure_backfill_active=False, score_counts={})
    # Actual rendered card and figure functions; unrelated page scripts omitted.
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', page_html, re.S)
    poller = next(s for s in scripts if 'let figurePollActive = false;' in s)
    template = gui.DIGEST_TEMPLATE
    sync = template[template.index('function syncCardFigureState('):template.index('\nfunction refreshOrder(')]
    layout = template[template.index('function layoutFigures()'):template.index("\ndocument.querySelectorAll('.card-abstract').forEach")]
    caret = template[template.index('function setupAbstractCaret('):template.index('\nfunction abstractClicked(')]
    markup = re.sub(r'<script\b[^>]*>.*?</script>', '', page_html, flags=re.S)
    status = {'running': False, 'figures': {}, 'failed': {pid: 'request_failed'}}
    retries = []
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def route(request):
                url = request.request.url
                if url.endswith('/figures-status'):
                    request.fulfill(json=status)
                elif url.endswith('/figures-retry'):
                    retries.append(request.request.post_data_json)
                    status['failed'] = {}
                    status['figures'] = {pid: {'files': ['one.png'], 'captions': ['First']}}
                    request.fulfill(status=202, json={'status': 'pending'})
                elif '/figure/' in url:
                    request.fulfill(content_type='image/png', body=PNG)
                elif url.endswith('/test'):
                    request.fulfill(content_type='text/html', body=markup)
                else:
                    request.fulfill(status=204)
            page.route('**/*', route)
            page.goto('http://apd.test/test')
            page.add_script_tag(content=caret + '\n' + layout + '\n' + sync + '\n' + poller)
            card = page.locator('.card')
            playwright.expect(card.get_by_role('button', name='Retry', exact=True)).to_be_visible()
            card.get_by_role('button', name='Retry', exact=True).click()
            playwright.expect(card.locator('a.card-figure')).to_have_attribute('data-figure-count', '1')
            assert retries == [{'paper_id': pid}]
            status['figures'][pid] = {'files': ['one.png', 'two.png'], 'captions': ['First', 'Second']}
            page.evaluate('startFigurePolling()')
            playwright.expect(card.locator('.figure-badge')).to_have_text('+1')
            playwright.expect(card.locator('a.card-figure')).to_have_attribute('data-captions', '["First","Second"]')
            page.evaluate("document.querySelector('.card-figure img').dispatchEvent(new Event('error'))")
            playwright.expect(card.get_by_role('button', name='Retry', exact=True)).to_be_visible()
            page.evaluate('(pid) => {const c=document.querySelector(".card"); c.dataset.score="3"; syncCardFigureState(c,pid,3,"hidden"); startFigurePolling();}', pid)
            page.wait_for_function('!figurePollActive')
            playwright.expect(card.locator('.card-figure')).to_have_count(0)
            page.evaluate('(pid) => {const c=document.querySelector(".card"); c.dataset.score="5"; syncCardFigureState(c,pid,5,"pending");}', pid)
            playwright.expect(card.locator('a.card-figure')).to_have_attribute('data-figure-count', '2')
            assert not errors
        finally:
            browser.close()
