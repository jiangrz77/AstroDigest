"""Tests for paper first-figure fetching and the digest card integration."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import figures, gui


HTML_WITH_FIGURE = """
<html><body>
<p>intro text</p>
<figure id="S1.F1" class="ltx_figure">
  <img src="x1.png" class="ltx_graphics" alt="figure 1"/>
  <figcaption>First figure</figcaption>
</figure>
<figure id="S2.F2" class="ltx_figure">
  <img src="x2.png" class="ltx_graphics"/>
</figure>
</body></html>
"""

HTML_WITH_DATA_URI_ONLY = """
<figure class="ltx_figure"><img src="data:image/png;base64,AAAA"/></figure>
<figure class="ltx_figure"><img src="x3.png" class="ltx_graphics"/></figure>
"""

HTML_WITH_STANDALONE_GRAPHIC = """
<html><body><div><img class="ltx_graphics" src="graphics/eq-plot.svg"/></div></body></html>
"""

HTML_WITH_CAPTIONS = """
<figure class="ltx_figure">
  <img src="x1.png" class="ltx_graphics"/>
  <figcaption class="ltx_caption"><span class="ltx_tag ltx_tag_figure">Figure 1: </span>
    Rotation of the <math><annotation encoding="application/x-tex">v_\phi</annotation><mi>v</mi></math> halo
    as a function of <i>radius</i>.</figcaption>
</figure>
<figure class="ltx_figure">
  <img src="y1.png" class="ltx_graphics"/>
  <figcaption class="ltx_caption">Second &amp; final figure.</figcaption>
</figure>
"""

HTML_WITH_OBJECT_FIGURES = """
<figure id="S3.F1" class="ltx_figure">
  <object type="image/svg+xml" data="2609.01801v1/b6b4abon.svg" id="S3.F1.g1" class="ltx_graphics ltx_img_square" width="354" height="352"></object>
  <figcaption class="ltx_caption"><span class="ltx_tag ltx_tag_figure">Figure 1: </span>Spectroscopic equilibrium.</figcaption>
</figure>
<figure id="S3.F2" class="ltx_figure">
  <object type="image/svg+xml" data="x2.svg" class="ltx_graphics"></object>
</figure>
"""


class ParseFirstFigureTests(unittest.TestCase):
    def test_returns_first_figure_image_src(self):
        self.assertEqual(figures.parse_first_figure_src(HTML_WITH_FIGURE), "x1.png")

    def test_multiple_srcs_in_document_order(self):
        self.assertEqual(
            figures.parse_figure_srcs(HTML_WITH_FIGURE, 5), ["x1.png", "x2.png"]
        )

    def test_captions_extracted_and_cleaned(self):
        items = figures.parse_figure_items(HTML_WITH_CAPTIONS)
        self.assertEqual([src for src, _cap in items], ["x1.png", "y1.png"])
        first = items[0][1]
        self.assertIn("Figure 1:", first)
        self.assertIn("Rotation of the", first)
        self.assertNotIn("x-tex", first)      # TeX annotation noise stripped
        self.assertNotIn("<", first)          # all markup stripped
        second = items[1][1]
        self.assertEqual(second, "Second & final figure.")  # entities decoded

    def test_object_embedded_svg_figures(self):
        # Newer LaTeXML renderings embed figures as <object data=...> instead
        # of <img src=...>; missing them misrecorded papers as figure-less.
        items = figures.parse_figure_items(HTML_WITH_OBJECT_FIGURES)
        self.assertEqual(
            [src for src, _cap in items],
            ["2609.01801v1/b6b4abon.svg", "x2.svg"],
        )
        self.assertIn("Spectroscopic equilibrium", items[0][1])

    def test_skips_data_uris(self):
        self.assertEqual(figures.parse_first_figure_src(HTML_WITH_DATA_URI_ONLY), "x3.png")

    def test_falls_back_to_standalone_ltx_graphics(self):
        self.assertEqual(
            figures.parse_first_figure_src(HTML_WITH_STANDALONE_GRAPHIC),
            "graphics/eq-plot.svg",
        )

    def test_no_figures(self):
        self.assertIsNone(figures.parse_first_figure_src("<html><body>text</body></html>"))


class CandidateUrlTests(unittest.TestCase):
    def test_old_layout_resolves_next_to_page(self):
        urls = figures._candidate_image_urls(
            "https://arxiv.org/html/2401.12345v1", "x1.png"
        )
        self.assertEqual(
            urls[0], "https://arxiv.org/html/2401.12345v1/x1.png"
        )

    def test_new_layout_including_paper_dir(self):
        urls = figures._candidate_image_urls(
            "https://arxiv.org/html/2609.01208v1",
            "2609.01208v1/img2/vphi_aver_halo_Z_fill_median.png",
        )
        self.assertIn(
            "https://arxiv.org/html/2609.01208v1/img2/vphi_aver_halo_Z_fill_median.png",
            urls,
        )


class SanitizeTests(unittest.TestCase):
    def test_new_style_id_unchanged(self):
        self.assertEqual(figures.sanitize_paper_id("2609.01208v1"), "2609.01208v1")

    def test_old_style_slash_replaced(self):
        self.assertEqual(figures.sanitize_paper_id("astro-ph/0502001"), "astro-ph_0502001")


class SidecarTests(unittest.TestCase):
    def test_roundtrip_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                figures.load_sidecar(tmp, "2026-09-02"),
                {"papers": {}, "failed": {}, "pv": 1},
            )
            data = {"papers": {"2609.01208v1": {"source": "html", "file": "x.png"}},
                    "failed": {"old1": "no_figure"}}
            figures.write_sidecar(tmp, "2026-09-02", data)
            # Reading normalizes legacy single-file entries to gallery form.
            self.assertEqual(
                figures.load_sidecar(tmp, "2026-09-02"),
                {
                    "papers": {"2609.01208v1": {
                        "source": "html", "files": ["x.png"], "depth": 1,
                        "captions": [""], "capv": 0,
                    }},
                    "failed": {"old1": "no_figure"},
                    "pv": 1,
                },
            )


class FetchFiguresForDigestTests(unittest.TestCase):
    def _paper(self, pid, score, **extra):
        return {"id": pid, "score": score, **extra}

    def test_filters_by_score_and_records_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            figures_dir = os.path.join(tmp, "figures")
            with patch.object(figures, "REQUEST_INTERVAL", 0), \
                 patch.object(
                     figures, "fetch_paper_figure",
                     side_effect=lambda pid, session, max_figures=1, skip=0: (
                         {"figures": [(b"PNGDATA", ".png")], "captions": ["cap 1"],
                          "source": "html"} if pid == "good1"
                         else {"figures": [], "captions": [], "source": "no_figure"}
                     )):
                sidecar = figures.fetch_figures_for_digest(
                    [
                        self._paper("good1", 5),
                        self._paper("bad1", 4),
                        self._paper("low1", 3),          # below threshold
                        self._paper("fail1", 5, scoring_failed=True),
                        self._paper("skip1", 5),          # attempted, fails
                    ],
                    figures_dir,
                    digest_dir=os.path.join(tmp, "digests"),
                    digest_date="2026-09-02",
                    max_figures=1,
                )
            self.assertEqual(set(sidecar["papers"]), {"good1"})
            self.assertEqual(
                sidecar["papers"]["good1"],
                {"source": "html", "files": ["good1.png"], "captions": ["cap 1"],
                 "depth": 1, "capv": 1},
            )
            self.assertEqual(
                sidecar["failed"],
                {"bad1": "no_figure", "skip1": "no_figure"},
            )
            self.assertTrue(
                os.path.isfile(os.path.join(figures_dir, "good1.png"))
            )
            # The sidecar survives a rerun and already-recorded papers are
            # not fetched again.
            with patch.object(figures, "REQUEST_INTERVAL", 0), \
                 patch.object(figures, "fetch_paper_figure") as fetch:
                figures.fetch_figures_for_digest(
                    [self._paper("good1", 5), self._paper("bad1", 4),
                     self._paper("skip1", 5)],
                    figures_dir,
                    digest_dir=os.path.join(tmp, "digests"),
                    digest_date="2026-09-02",
                    max_figures=1,
                )
            fetch.assert_not_called()

    def test_parser_upgrade_retries_figureless_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            figures_dir = os.path.join(tmp, "figures")
            digests = os.path.join(tmp, "digests")
            # A failure recorded by parser v1: with PARSER_VERSION now 2 the
            # entry must be retried once instead of skipped forever.
            figures.write_sidecar(digests, "2026-09-02", {
                "papers": {},
                "failed": {"retryme": "no_figure", "hardfail": "write_error"},
                "pv": 1,
            })
            with patch.object(figures, "REQUEST_INTERVAL", 0), \
                 patch.object(figures, "PARSER_VERSION", 2), \
                 patch.object(
                     figures, "fetch_paper_figure",
                     return_value={"figures": [(b"N", ".png")],
                                   "captions": ["now found"], "source": "html"},
                 ) as fetch:
                sidecar = figures.fetch_figures_for_digest(
                    [self._paper("retryme", 5), self._paper("hardfail", 5)],
                    figures_dir,
                    digest_dir=digests,
                    digest_date="2026-09-02",
                    max_figures=1,
                )
            fetch.assert_called_once()  # hardfail (write_error) stays skipped
            self.assertIn("retryme", sidecar["papers"])
            self.assertNotIn("retryme", sidecar["failed"])
            self.assertEqual(sidecar["failed"], {"hardfail": "write_error"})
            self.assertEqual(sidecar["pv"], 2)
            # A second run at the current version records the entry complete
            # (depth 1 = max_figures 1, captions current) and skips it.
            with patch.object(figures, "REQUEST_INTERVAL", 0), \
                 patch.object(figures, "fetch_paper_figure") as fetch2:
                sidecar = figures.fetch_figures_for_digest(
                    [self._paper("retryme", 5)],
                    figures_dir,
                    digest_dir=digests,
                    digest_date="2026-09-02",
                    max_figures=1,
                )
            fetch2.assert_not_called()
            self.assertIn("retryme", sidecar["papers"])
            self.assertNotIn("retryme", sidecar["failed"])
            self.assertEqual(sidecar["pv"], 2)

    def test_cached_figure_refreshes_captions_without_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            figures_dir = os.path.join(tmp, "figures")
            os.makedirs(figures_dir)
            with open(os.path.join(figures_dir, "cached1.png"), "wb") as f:
                f.write(b"PNG")
            with patch.object(figures, "REQUEST_INTERVAL", 0), \
                 patch.object(
                     figures, "fetch_paper_figure",
                     return_value={"figures": [], "captions": ["the caption"],
                                   "source": "html"},
                 ) as fetch:
                sidecar = figures.fetch_figures_for_digest(
                    [self._paper("cached1", 4)],
                    figures_dir,
                    digest_dir=os.path.join(tmp, "digests"),
                    digest_date="2026-09-02",
                    max_figures=1,
                )
            # One page fetch to learn the caption; no image re-downloaded.
            fetch.assert_called_once()
            self.assertEqual(
                sidecar["papers"]["cached1"],
                {"source": "html", "files": ["cached1.png"], "captions": ["the caption"],
                 "depth": 1, "capv": 1},
            )
            with open(os.path.join(figures_dir, "cached1.png"), "rb") as f:
                self.assertEqual(f.read(), b"PNG")

    def test_gallery_upgrade_fetches_only_missing_figures(self):
        with tempfile.TemporaryDirectory() as tmp:
            figures_dir = os.path.join(tmp, "figures")
            os.makedirs(figures_dir)
            # Yesterday's single-figure cache plus a legacy sidecar entry.
            with open(os.path.join(figures_dir, "p1.png"), "wb") as f:
                f.write(b"OLD")
            digests = os.path.join(tmp, "digests")
            figures.write_sidecar(digests, "2026-09-02", {
                "papers": {"p1": {"source": "html", "file": "p1.png"}},
                "failed": {},
            })
            with patch.object(figures, "REQUEST_INTERVAL", 0), \
                 patch.object(
                     figures, "fetch_paper_figure",
                     return_value={"figures": [(b"F2", ".png"), (b"F3", ".png")],
                                   "captions": ["c1", "c2", "c3"], "source": "html"},
                 ) as fetch:
                sidecar = figures.fetch_figures_for_digest(
                    [self._paper("p1", 5)],
                    figures_dir,
                    digest_dir=digests,
                    digest_date="2026-09-02",
                    max_figures=3,
                )
            # Cached figure 1 is skipped, not redownloaded.
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.kwargs.get("skip"), 1)
            entry = sidecar["papers"]["p1"]
            self.assertEqual(entry["depth"], 3)
            self.assertEqual(entry["capv"], 1)
            self.assertEqual(entry["files"], ["p1.png", "p1-2.png", "p1-3.png"])
            self.assertEqual(entry["captions"], ["c1", "c2", "c3"])
            with open(os.path.join(figures_dir, "p1.png"), "rb") as f:
                self.assertEqual(f.read(), b"OLD")
            self.assertTrue(os.path.isfile(os.path.join(figures_dir, "p1-3.png")))


class GuiFigureTests(unittest.TestCase):
    def setUp(self):
        setup = patch.object(gui, "_needs_setup", return_value=False)
        setup.start()
        self.addCleanup(setup.stop)

    def test_figure_candidates(self):
        digest = {
            "tiers": [
                {"papers": [
                    {"paper_id": "a", "score": 5},
                    {"paper_id": "b", "score": 4},
                    {"paper_id": "c", "score": 3},
                    {"paper_id": "d", "score": 5, "scoring_failed": True},
                ]},
            ]
        }
        self.assertEqual(gui._figure_candidates(digest), ["a", "b"])

    def test_figure_route_serves_cached_file(self):
        client = gui.app.test_client()
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "2609.01208v1.png")
            with open(target, "wb") as f:
                f.write(b"PNGDATA")
            with open(os.path.join(tmp, "2609.01208v1-2.png"), "wb") as f:
                f.write(b"PNG2")
            with patch.object(gui, "_figures_dir", return_value=tmp):
                response = client.get("/figure/2609.01208v1")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/png")
                self.assertEqual(response.data, b"PNGDATA")
                # Gallery index route: second figure, and a miss.
                second = client.get("/figure/2609.01208v1/2")
                self.assertEqual(second.status_code, 200)
                self.assertEqual(second.data, b"PNG2")
                self.assertEqual(client.get("/figure/2609.01208v1/9").status_code, 404)
                # Unknown papers 404; path separators cannot escape.
                self.assertEqual(client.get("/figure/unknown").status_code, 404)
                self.assertEqual(
                    client.get("/figure/..%2F..%2Fetc%2Fpasswd").status_code, 404
                )

    def test_figures_status_route(self):
        client = gui.app.test_client()
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = {"papers": {"2609.01208v1": {"source": "html", "file": "x.png"}}}
            with open(os.path.join(tmp, "digest_2026-09-02.figures.json"), "w") as f:
                json.dump(sidecar, f)
            with patch.object(gui, "_digests_dir", return_value=tmp):
                response = client.get("/digest/2026-09-02/figures-status")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["running"])
        self.assertIn("2609.01208v1", data["figures"])

    def test_digest_template_carries_figure_and_reason_markup(self):
        self.assertIn("Match", gui.DIGEST_TEMPLATE)
        self.assertIn("card-figure", gui.DIGEST_TEMPLATE)
        # Abstracts preview 3 lines and expand on click (no text button).
        self.assertIn("-webkit-line-clamp:3", gui.DIGEST_TEMPLATE)
        self.assertIn("abstractClicked", gui.DIGEST_TEMPLATE)
        self.assertIn("layoutFigures", gui.DIGEST_TEMPLATE)
        self.assertNotIn("abstract-toggle", gui.DIGEST_TEMPLATE)
        self.assertIn("figure_map.get(paper.paper_id)", gui.DIGEST_TEMPLATE)
        self.assertIn("reason_html.get(paper.paper_id", gui.DIGEST_TEMPLATE)
        # Gallery: badge + caption-carrying lightbox entry points.
        self.assertIn("data-figure-count", gui.DIGEST_TEMPLATE)
        # Single-quoted attribute: tojson leaves inner double quotes raw,
        # which would truncate a double-quoted attribute (empty captions).
        self.assertIn("data-captions='{{ fig.captions | tojson }}'", gui.DIGEST_TEMPLATE)
        self.assertIn("figure-badge", gui.DIGEST_TEMPLATE)
        self.assertIn("lightbox-caption", gui.DIGEST_TEMPLATE)
        self.assertIn("1080px", gui.DIGEST_TEMPLATE)
        # Tier-coloured reason callouts (green for 4-star, etc.).
        self.assertIn("reason-high", gui.DIGEST_TEMPLATE)
        self.assertIn("reason-strong", gui.DIGEST_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
