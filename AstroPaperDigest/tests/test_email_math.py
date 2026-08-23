"""Regression tests for native MathML in HTML email."""

from __future__ import annotations

import unittest

from src.email_math import render_math_html


class EmailMathTests(unittest.TestCase):
    def test_inline_formula_uses_native_mathml_at_text_size(self):
        rendered = render_math_html(
            r"Metallicity $-4.05\leq\mbox{[Fe/H]}\leq-2.33$.",
        )

        self.assertIn('<math ', rendered)
        self.assertIn('xmlns="http://www.w3.org/1998/Math/MathML"', rendered)
        self.assertIn('display="inline"', rendered)
        self.assertIn('font-size:1em', rendered)
        self.assertIn('aria-label=', rendered)
        self.assertNotIn(r"\mbox", rendered)
        self.assertNotIn("<img ", rendered)
        self.assertNotIn("cid:", rendered)

    def test_common_arxiv_commands_are_normalised(self):
        rendered = render_math_html(
            r"$L_{\rm X}$, $\alpha\text{-rich freeze-out}$, and $\dfrac{1}{2}$",
        )

        self.assertNotIn(r"\rm", rendered)
        self.assertNotIn(r"\dfrac", rendered)
        self.assertEqual(rendered.count("<math "), 3)

    def test_display_formula_is_centered_without_oversizing(self):
        rendered = render_math_html(r"$$E = mc^2$$")

        self.assertIn('display="block"', rendered)
        self.assertIn("text-align:center", rendered)
        self.assertIn("font-size:1em", rendered)
        self.assertNotIn("<img ", rendered)

    def test_unparseable_formula_uses_readable_html_fallback(self):
        rendered = render_math_html(r"Fallback $\unknowncommand{x}$")

        self.assertIn('role="math"', rendered)
        self.assertIn(r"\unknowncommand", rendered)
        self.assertNotIn("<img ", rendered)
        self.assertNotIn("cid:", rendered)

    def test_non_math_text_is_escaped(self):
        rendered = render_math_html(r"<script>alert(1)</script> and $T_e$")

        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("<math ", rendered)


if __name__ == "__main__":
    unittest.main()
