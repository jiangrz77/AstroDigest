"""Regression tests for HTTP 429 backoff/retry behaviour.

arXiv tightened API rate-limit enforcement in early 2026, so transient 429s
reached even compliantly paced clients; fetching must retry politely with
escalating waits (honouring Retry-After) instead of failing after one retry.
"""

from contextlib import nullcontext
from datetime import datetime, timezone
import os
import sys
import unittest
from unittest.mock import patch

import arxiv
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fetch_arxiv import (
    _fetch_listed_papers,
    _parse_retry_after,
    _RateLimitBudget,
    _RATE_LIMIT_BACKOFF_SECONDS,
    _RATE_LIMIT_MAX_RETRIES,
    _RATE_LIMIT_TOTAL_BUDGET,
)


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


def _http_429():
    return arxiv.HTTPError("https://export.arxiv.org/api/query", 0, 429)


class _ScriptedClient:
    """Fake client whose results() follows a per-call script.

    ``outcomes`` holds one entry per call: an exception instance to raise, or
    a list of results to return. The last entry repeats once the script is
    exhausted.
    """

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = 0

    def _next(self):
        index = min(self.calls, len(self.outcomes) - 1)
        return self.outcomes[index]

    def results(self, _search):
        outcome = self._next()
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return iter(outcome)


def _run_fetch(client):
    """Run _fetch_listed_papers with the network layer patched out.

    Returns (papers, sleep_mock); the sleep mock also records waits for runs
    that end up raising, via the exception-path test helper below.
    """
    with patch("src.fetch_arxiv._new_client", return_value=client), \
         patch("src.fetch_arxiv._api_request_session", return_value=nullcontext()), \
         patch("src.fetch_arxiv.emit"), \
         patch("src.fetch_arxiv.time.sleep") as sleep, \
         patch("src.fetch_arxiv.time.monotonic", return_value=0):
        papers = _fetch_listed_papers(
            ["2608.00001"],
            ["astro-ph.GA"],
            include_cross=True,
            include_replacements=True,
        )
    return papers, sleep


def _run_fetch_until_error(client):
    """Same patches as _run_fetch, but expect an exception; return the mock."""
    with patch("src.fetch_arxiv._new_client", return_value=client), \
         patch("src.fetch_arxiv._api_request_session", return_value=nullcontext()), \
         patch("src.fetch_arxiv.emit"), \
         patch("src.fetch_arxiv.time.sleep") as sleep, \
         patch("src.fetch_arxiv.time.monotonic", return_value=0):
        try:
            _fetch_listed_papers(
                ["2608.00001"],
                ["astro-ph.GA"],
                include_cross=True,
                include_replacements=True,
            )
        except Exception:
            pass
    return sleep


_GA = [_Result("2608.00001", ["astro-ph.GA"])]


class RateLimitRetryTests(unittest.TestCase):
    def test_transient_429_retries_with_backoff_then_succeeds(self):
        client = _ScriptedClient([_http_429(), _http_429(), _GA])
        papers, sleep = _run_fetch(client)

        self.assertEqual([paper["id"] for paper in papers], ["2608.00001v1"])
        self.assertEqual(client.calls, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [_RATE_LIMIT_BACKOFF_SECONDS[0], _RATE_LIMIT_BACKOFF_SECONDS[1]],
        )

    def test_persistent_429_gives_up_after_bounded_backoffs(self):
        client = _ScriptedClient([_http_429()])
        with patch("src.fetch_arxiv._new_client", return_value=client), \
             patch("src.fetch_arxiv._api_request_session", return_value=nullcontext()), \
             patch("src.fetch_arxiv.emit"), \
             patch("src.fetch_arxiv.time.sleep"), \
             patch("src.fetch_arxiv.time.monotonic", return_value=0):
            with self.assertRaises(arxiv.HTTPError):
                _fetch_listed_papers(
                    ["2608.00001"], ["astro-ph.GA"],
                    include_cross=True, include_replacements=True,
                )
        self.assertEqual(client.calls, _RATE_LIMIT_MAX_RETRIES + 1)

    def test_backoff_waits_are_bounded(self):
        client = _ScriptedClient([_http_429()])
        sleep = _run_fetch_until_error(client)
        waits = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(waits), _RATE_LIMIT_MAX_RETRIES)
        self.assertLessEqual(sum(waits), _RATE_LIMIT_TOTAL_BUDGET)

    def test_retry_after_header_is_honoured_when_longer(self):
        client = _ScriptedClient([_http_429(), _GA])
        client._last_429_retry_after = 90.0
        _, sleep = _run_fetch(client)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [90.0])

    def test_budget_shared_between_batch_and_single_id_retries(self):
        # Batch call 429s twice (30s + 60s spent). The returned result is for
        # a different ID, so it enters the missing-ID phase, whose single
        # query 429s once more and must escalate to 120s, not restart at 30s.
        other = [_Result("2608.99999", ["astro-ph.GA"])]
        client = _ScriptedClient([
            _http_429(), _http_429(), other, _http_429(), other,
        ])
        papers, sleep = _run_fetch(client)
        waits = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(
            waits,
            [_RATE_LIMIT_BACKOFF_SECONDS[0], _RATE_LIMIT_BACKOFF_SECONDS[1],
             _RATE_LIMIT_BACKOFF_SECONDS[2]],
        )
        # The requested ID never came back, so no papers are returned.
        self.assertEqual(papers, [])

    def test_parse_retry_accepts_numeric_seconds_only(self):
        class _Resp:
            def __init__(self, value):
                self.headers = {} if value is None else {"Retry-After": value}

        self.assertIsNone(_parse_retry_after(_Resp(None)))
        self.assertEqual(_parse_retry_after(_Resp("120")), 120.0)
        self.assertEqual(_parse_retry_after(_Resp("0")), 1.0)
        self.assertIsNone(_parse_retry_after(_Resp("Wed, 21 Oct 2026 07:28:00 GMT")))

    def test_budget_exhaustion_returns_none(self):
        budget = _RateLimitBudget()
        waits = []
        while True:
            wait = budget.next_wait(client=None)
            if wait is None:
                break
            waits.append(wait)
        self.assertEqual(len(waits), _RATE_LIMIT_MAX_RETRIES)
        self.assertLessEqual(sum(waits), _RATE_LIMIT_TOTAL_BUDGET)
        self.assertLessEqual(max(waits), max(_RATE_LIMIT_BACKOFF_SECONDS))


class RateLimitConnectionRetryTests(unittest.TestCase):
    def test_connection_error_still_retries_once(self):
        client = _ScriptedClient([
            requests.exceptions.ConnectionError("boom"), _GA,
        ])
        papers, sleep = _run_fetch(client)
        self.assertEqual([paper["id"] for paper in papers], ["2608.00001v1"])
        self.assertEqual(client.calls, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5])


if __name__ == "__main__":
    unittest.main()
