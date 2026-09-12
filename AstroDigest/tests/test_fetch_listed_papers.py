"""Regression tests for exact official-list metadata fetching."""

from contextlib import nullcontext
from datetime import datetime, timezone
import os
import sys
import unittest
import arxiv
from urllib.parse import urlparse, parse_qs
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fetch_arxiv import _fetch_listed_papers


class _Author:
    def __init__(self, name):
        self.name = name


class _Result:
    def __init__(self, paper_id, categories):
        self.entry_id = f"https://arxiv.org/abs/{paper_id}v1"
        self.categories = categories
        self.updated = datetime(2026, 8, 20, tzinfo=timezone.utc)
        self.published = self.updated
        self.title = f"Paper {paper_id}"
        self.authors = [_Author("Test Author")]
        self.summary = "Test abstract"
        self.pdf_url = f"https://arxiv.org/pdf/{paper_id}"
        self.primary_category = categories[0]


class _Client:
    def __init__(self, results):
        self._results = results
        self.calls = 0

    def results(self, _search):
        self.calls += 1
        return iter(self._results)


class FetchListedPapersTests(unittest.TestCase):
    def test_filtered_papers_are_not_retried_as_missing_metadata(self):
        client = _Client([
            _Result("2608.00001", ["astro-ph.GA"]),
            _Result("2608.00002", ["astro-ph.HE"]),
            _Result("2608.00003", ["astro-ph.CO"]),
        ])
        events = []

        with patch("src.fetch_arxiv._new_client", return_value=client), \
             patch("src.fetch_arxiv._api_request_session", return_value=nullcontext()), \
             patch("src.fetch_arxiv.emit", side_effect=lambda *args: events.append(args)):
            papers = _fetch_listed_papers(
                ["2608.00001", "2608.00002", "2608.00003"],
                ["astro-ph.GA"],
                include_cross=True,
                include_replacements=True,
            )

        self.assertEqual([paper["id"] for paper in papers], ["2608.00001v1"])
        self.assertEqual(client.calls, 1)
        self.assertEqual(events[-1][1:3], (3, 3))
        self.assertIn("1 match selected categories", events[-1][3])

    def test_http_page_size_matches_batch_and_single_id_retry(self):
        urls = []
        ids = [f'2609.{i:05d}' for i in range(51)]
        class Client(arxiv.Client):
            def results(self, search):
                urls.append(self._format_url(search, 0, self.page_size))
                # One ID omitted from the first batch is retried individually.
                selected = search.id_list[1:] if len(search.id_list) > 1 else search.id_list
                return iter(_Result(pid, ['astro-ph.GA']) for pid in selected)
        with patch('src.fetch_arxiv._new_client', return_value=Client()), \
             patch('src.fetch_arxiv._api_request_session', return_value=nullcontext()), \
             patch('src.fetch_arxiv.emit'):
            papers = _fetch_listed_papers(ids, ['astro-ph.GA'], True, True)
        self.assertEqual(len(papers), 51)
        self.assertEqual([parse_qs(urlparse(url).query)['max_results'][0] for url in urls],
                         ['50', '1', '1'])


if __name__ == "__main__":
    unittest.main()
