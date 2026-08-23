"""Regression checks for the compact calendar legend."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gui import _CALENDAR_SNIPPET  # noqa: E402


class CalendarUiTests(unittest.TestCase):
    def test_calendar_has_no_default_explanation_labels(self):
        self.assertNotIn("Click a date to open it", _CALENDAR_SNIPPET)
        self.assertNotIn("Has content", _CALENDAR_SNIPPET)
        self.assertNotIn("Empty digest", _CALENDAR_SNIPPET)

    def test_unscored_legend_is_conditional_and_precise(self):
        self.assertIn("STATUS[key] === 'orange'", _CALENDAR_SNIPPET)
        self.assertIn("Some papers unscored", _CALENDAR_SNIPPET)
        self.assertIn("dot dot-orange", _CALENDAR_SNIPPET)


if __name__ == "__main__":
    unittest.main()
