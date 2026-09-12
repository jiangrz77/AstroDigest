"""Tests for recommendation-reason keyword bolding."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import gui, reason_highlight


class HighlightReasonTextTests(unittest.TestCase):
    def test_bolds_matching_keywords_case_insensitive(self):
        out = gui._highlight_reason_text(
            "Directly studies Milky Way halo formation.",
            ["milky way", "stellar halo"],
        )
        self.assertEqual(out, "Directly studies <b>Milky Way</b> halo formation.")

    def test_longest_phrase_wins(self):
        out = gui._highlight_reason_text(
            "Galaxy cluster scaling relations.",
            ["galaxy", "galaxy cluster"],
        )
        self.assertEqual(out, "<b>Galaxy cluster</b> scaling relations.")

    def test_word_boundary_prevents_substring_hits(self):
        out = gui._highlight_reason_text("supernovae remnants", ["supernova"])
        self.assertEqual(out, "supernovae remnants")

    def test_caps_total_highlights(self):
        text = "dark matter, black hole, pulsar, quasar studies"
        out = gui._highlight_reason_text(
            text, ["dark matter", "black hole", "pulsar", "quasar"]
        )
        self.assertEqual(out.count("<b>"), gui.MAX_REASON_HIGHLIGHTS)

    def test_escapes_html(self):
        out = gui._highlight_reason_text("metal-poor <stars> & r-process", ["r-process"])
        self.assertEqual(
            out, "metal-poor &lt;stars&gt; &amp; <b>r-process</b>"
        )

    def test_no_keywords_returns_escaped_text(self):
        self.assertEqual(
            gui._highlight_reason_text("a < b", []), "a &lt; b"
        )


class ReasonKeywordsTests(unittest.TestCase):
    def test_merges_config_learned_and_glossary_longest_first(self):
        cfg = {"keywords": ["first stars", "Population III"]}
        with patch.object(
            reason_highlight, "load_learned_profile",
            return_value={"keyword_weights": {
                "asteroseismology": 1.6, "rejected-topic": 0.5, "pulsar timing": 1.4,
            }},
        ):
            keywords = gui._reason_keywords(cfg)
        lowered = [k.lower() for k in keywords]
        self.assertIn("first stars", lowered)
        self.assertIn("asteroseismology", lowered)   # learned boost included
        self.assertIn("pulsar timing", lowered)
        self.assertNotIn("rejected-topic", lowered)  # penalized terms excluded
        self.assertIn("milky way", lowered)          # glossary included
        # Longest first so longer phrases match before their substrings.
        self.assertEqual(keywords, sorted(keywords, key=len, reverse=True))

    def test_short_terms_dropped(self):
        keywords = gui._reason_keywords({"keywords": ["ab", "halo"]})
        self.assertIn("halo", keywords)
        self.assertNotIn("ab", keywords)


if __name__ == "__main__":
    unittest.main()
