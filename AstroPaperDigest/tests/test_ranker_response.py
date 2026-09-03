"""Regression tests for malformed and truncated LLM score responses."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import ranker
from src.ranker import _create_completion, _parse_score_response


class RankerResponseTests(unittest.TestCase):
    def test_recovers_complete_objects_before_unterminated_string(self):
        content = (
            '[{"index": 0, "score": 5, "reason": "Strong match"}, '
            '{"index": 1, "score": 3, "reason": "Unterminated reason'
        )
        items, partial = _parse_score_response(content)
        self.assertTrue(partial)
        self.assertEqual(items, [{"index": 0, "score": 5, "reason": "Strong match"}])

    def test_parses_markdown_wrapped_json_and_clamps_scores(self):
        content = '```json\n[{"index": 2, "score": 15, "reason": "Relevant"}]\n```'
        items, partial = _parse_score_response(content)
        self.assertFalse(partial)
        self.assertEqual(items[0]["score"], 5)


class _FakeCompletions:
    def __init__(self, reject_thinking_param: bool):
        self.reject_thinking_param = reject_thinking_param
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_thinking_param and "extra_body" in kwargs:
            err = Exception("Unrecognized request argument supplied: thinking")
            err.status_code = 400
            raise err
        return "OK"


class ThinkingDisabledTests(unittest.TestCase):
    """_create_completion asks reasoning models not to think (their chain of
    thought otherwise counts against max_tokens and can starve the JSON)."""

    def _client(self, reject=False):
        completions = _FakeCompletions(reject)
        return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions

    def setUp(self):
        patcher = patch.object(ranker, "_thinking_param_unsupported", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_requests_thinking_disabled(self):
        client, completions = self._client()
        result = _create_completion(client, "m", [{"role": "user", "content": "x"}])
        self.assertEqual(result, "OK")
        self.assertEqual(len(completions.calls), 1)
        self.assertEqual(
            completions.calls[0]["extra_body"], {"thinking": {"type": "disabled"}}
        )
        self.assertGreaterEqual(completions.calls[0]["max_tokens"], 16384)

    def test_falls_back_when_parameter_rejected(self):
        client, completions = self._client(reject=True)
        result = _create_completion(client, "m", [{"role": "user", "content": "x"}])
        self.assertEqual(result, "OK")
        self.assertEqual(len(completions.calls), 2)
        self.assertIn("extra_body", completions.calls[0])
        self.assertNotIn("extra_body", completions.calls[1])
        # The rejection is remembered: later calls skip the parameter.
        _create_completion(client, "m", [{"role": "user", "content": "x"}])
        self.assertEqual(len(completions.calls), 3)

    def test_unrelated_errors_are_raised(self):
        client, completions = self._client(reject=True)

        def boom(**kwargs):
            err = Exception("Rate limit reached")
            err.status_code = 429
            raise err

        completions.create = boom
        with self.assertRaises(Exception):
            _create_completion(client, "m", [{"role": "user", "content": "x"}])


if __name__ == "__main__":
    unittest.main()
