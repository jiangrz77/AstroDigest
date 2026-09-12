"""Tests for paper first-figure fetching and the digest card integration."""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from threading import Barrier, Event
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
    Rotation of the <math><annotation encoding="application/x-tex">v_\\phi</annotation><mi>v</mi></math> halo
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

HTML_WITH_THREE_FIGURES = """
<figure><img src="x1.png" class="ltx_graphics"/><figcaption>F1</figcaption></figure>
<figure><img src="x2.png" class="ltx_graphics"/><figcaption>F2</figcaption></figure>
<figure><img src="x3.png" class="ltx_graphics"/><figcaption>F3</figcaption></figure>
"""


class _FakeResponse:
    def __init__(self, status_code=200, text="", content=b"", headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self.content = content
        self.headers = headers or {}
        self.url = url


class _BarrierImageSession:
    """Serves an HTML page whose image requests block on a shared barrier.

    The barrier only releases when the image downloads really overlap:
    serial downloads would trip its timeout, so this pins the concurrent
    download behaviour gallery fetching depends on.
    """

    page_url = "https://arxiv.org/html/p1"

    def __init__(self, image_count):
        self.barrier = Barrier(image_count, timeout=10)
        self.image_requests = 0

    def get(self, url, timeout=None):
        if url == self.page_url:
            return _FakeResponse(text=HTML_WITH_THREE_FIGURES, url=self.page_url)
        self.image_requests += 1
        self.barrier.wait()
        name = url.rsplit("/", 1)[-1]
        return _FakeResponse(
            content=b"IMG-" + name.encode(), headers={"Content-Type": "image/png"}
        )


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


class DownloadFigureHtmlTests(unittest.TestCase):
    def test_concurrent_downloads_keep_document_order(self):
        session = _BarrierImageSession(image_count=3)
        result, captions = figures.fetch_figure_html("p1", session, max_figures=3)
        # All three downloads ran (and could only pass the barrier together);
        # results stay in document order with their captions.
        self.assertEqual([data for data, _ext in result],
                         [b"IMG-x1.png", b"IMG-x2.png", b"IMG-x3.png"])
        self.assertEqual(captions, ["F1", "F2", "F3"])
        self.assertEqual(session.image_requests, 3)

    def test_skip_downloads_cached_figures_only(self):
        session = _BarrierImageSession(image_count=2)
        result, captions = figures.fetch_figure_html("p1", session, max_figures=3, skip=1)
        # The cached first figure is not requested again; the rest are.
        self.assertEqual([data for data, _ext in result],
                         [b"IMG-x2.png", b"IMG-x3.png"])
        self.assertEqual(captions, ["F1", "F2", "F3"])
        self.assertEqual(session.image_requests, 2)


class SanitizeTests(unittest.TestCase):
    def test_new_style_id_unchanged(self):
        self.assertEqual(figures.sanitize_paper_id("2609.01208v1"), "2609.01208v1")

    def test_old_style_slash_replaced(self):
        self.assertEqual(figures.sanitize_paper_id("astro-ph/0502001"), "astro-ph_0502001")

    def test_delete_paper_figures_removes_only_that_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("p1.png", "p1-2.jpg", "p1-3.png", "other.png"):
                with open(os.path.join(tmp, name), "wb") as f:
                    f.write(b"X")
            deleted = figures.delete_paper_figures(tmp, "p1")
            self.assertEqual(set(deleted), {"p1.png", "p1-2.jpg", "p1-3.png"})
            self.assertEqual(os.listdir(tmp), ["other.png"])


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
            self.assertEqual(sidecar["pv"], figures.PARSER_VERSION)

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

    def test_progress_callback_reports_each_fetched_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            figures_dir = os.path.join(tmp, "figures")
            os.makedirs(figures_dir)
            with open(os.path.join(figures_dir, "done1.png"), "wb") as f:
                f.write(b"OLD")
            digests = os.path.join(tmp, "digests")
            figures.write_sidecar(digests, "2026-09-02", {
                "papers": {"done1": {"source": "html", "files": ["done1.png"],
                                      "depth": 1, "captions": [""], "capv": 1}},
                "failed": {},
            })
            calls = []
            with patch.object(figures, "REQUEST_INTERVAL", 0), \
                 patch.object(
                     figures, "fetch_paper_figure",
                     return_value={"figures": [(b"N", ".png")],
                                   "captions": ["c"], "source": "html"}):
                figures.fetch_figures_for_digest(
                    [self._paper("done1", 5), self._paper("fresh1", 5)],
                    figures_dir,
                    digest_dir=digests,
                    digest_date="2026-09-02",
                    max_figures=1,
                    on_progress=lambda done, total: calls.append((done, total)),
                )
            # The cached paper is skipped silently; the fresh one reports its
            # position among the digest's figure targets.
            self.assertEqual(calls, [(2, 2)])

def _digest_markdown(score_by_pid: dict) -> str:
    """A minimal digest file parse_digest understands (one tier, N papers)."""
    lines = [
        f"# AstroPaperDigest - 2026-09-02",
        "",
        f"**Total papers reviewed:** {len(score_by_pid)}",
        f"**Highly relevant papers:** "
        f"{sum(1 for score in score_by_pid.values() if score >= 4)}",
        "",
        "## Strongly Recommended",
        "",
    ]
    for pid, score in score_by_pid.items():
        stars = "★" * score + "☆" * (5 - score)
        lines += [
            f"### Paper {pid}",
            f"**Score:** {score}/5 | **Reason:** reason {pid}",
            f"**Authors:** A. Author",
            f"**Link:** [{pid}](https://arxiv.org/abs/{pid})",
            f"> Abstract of {pid}.",
            "",
            f"> Score: {stars}",
            "",
        ]
    return "\n".join(lines)


class FigureRatingRetentionTests(unittest.TestCase):
    """±star rating changes drive the gallery: fetch on upgrade, retain-then-
    delete on downgrade (files survive a 7-day grace period)."""

    DATE = "2026-09-02"

    def setUp(self):
        setup = patch.object(gui, "_needs_setup", return_value=False)
        setup.start()
        self.addCleanup(setup.stop)
        # A previous test's backfill thread must never leak into this one.
        gui._figure_backfill_state.update({"running": False, "date": ""})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.figures_dir = os.path.join(self.tmp.name, "figures")
        self.digests_dir = os.path.join(self.tmp.name, "digests")
        os.makedirs(self.figures_dir)
        os.makedirs(self.digests_dir)
        # Score overrides the day's feedback would produce, keyed by pid.
        self.adjusted_scores = {}
        self.digest_file = os.path.join(self.digests_dir, "digest_2026-09-02.md")

        def fake_apply(digest, date_str, *args, **kwargs):
            for tier in digest.get("tiers", []):
                for paper in tier.get("papers", []):
                    pid = paper.get("paper_id")
                    if pid in self.adjusted_scores:
                        paper["score"] = self.adjusted_scores[pid]
            return digest

        patches = [
            patch.object(gui, "_figures_dir", return_value=self.figures_dir),
            patch.object(gui, "_digests_dir", return_value=self.digests_dir),
            patch.object(
                gui, "get_digest_path_for_date",
                side_effect=lambda date_str, *a, **k: (
                    self.digest_file if date_str == self.DATE else ""
                ),
            ),
            patch.object(gui, "apply_to_digest", side_effect=fake_apply),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _write_digest(self, score_by_pid):
        with open(self.digest_file, "w", encoding="utf-8") as f:
            f.write(_digest_markdown(score_by_pid))
        self.adjusted_scores.clear()

    def _cache_figures(self, pid, depth=1):
        name = f"{pid}.png"
        with open(os.path.join(self.figures_dir, name), "wb") as f:
            f.write(b"PNG")
        figures.write_sidecar(self.digests_dir, self.DATE, {
            "papers": {pid: {"source": "html", "files": [name], "depth": depth,
                              "captions": [""], "capv": figures.CAPTION_VERSION}},
            "failed": {},
            "pv": figures.PARSER_VERSION,
        })
        return name

    def test_sweep_marks_recent_downgrade_and_keeps_files(self):
        self._write_digest({"p1": 5, "p2": 4})
        self._cache_figures("p2")
        self.adjusted_scores["p2"] = 3  # downgraded out of the 4-5 star range
        gui._sweep_figure_retention()
        # Within the grace period the images stay; the paper is stamped.
        self.assertTrue(os.path.isfile(os.path.join(self.figures_dir, "p2.png")))
        store = gui._load_figure_retention()
        self.assertIn("p2", store)
        self.assertEqual(store["p2"]["date"], self.DATE)
        self.assertIn("p2", figures.load_sidecar(self.digests_dir, self.DATE)["papers"])

    def test_sweep_deletes_downgraded_figures_after_grace_period(self):
        self._write_digest({"p2": 4})
        self._cache_figures("p2")
        self.adjusted_scores["p2"] = 3
        stale = (
            datetime.now() - timedelta(days=gui._FIGURE_RETENTION_DAYS + 1)
        ).isoformat(timespec="seconds")
        gui._save_figure_retention({"p2": {"date": self.DATE, "below_since": stale}})
        gui._sweep_figure_retention()
        self.assertFalse(os.path.exists(os.path.join(self.figures_dir, "p2.png")))
        self.assertEqual(figures.load_sidecar(self.digests_dir, self.DATE)["papers"], {})
        self.assertEqual(gui._load_figure_retention(), {})

    def test_sweep_unmarks_and_keeps_reupgraded_paper(self):
        self._write_digest({"p2": 5})
        self._cache_figures("p2")
        stale = (
            datetime.now() - timedelta(days=gui._FIGURE_RETENTION_DAYS + 1)
        ).isoformat(timespec="seconds")
        gui._save_figure_retention({"p2": {"date": self.DATE, "below_since": stale}})
        gui._sweep_figure_retention()  # effective score is back at 5 stars
        self.assertTrue(os.path.isfile(os.path.join(self.figures_dir, "p2.png")))
        self.assertEqual(gui._load_figure_retention(), {})

    def test_retention_waits_for_worker_using_shared_cache(self):
        self._write_digest({'p2': 3})
        self._cache_figures('p2')
        stale = (datetime.now() - timedelta(days=30)).isoformat()
        gui._save_figure_retention({'p2': {'date': self.DATE, 'below_since': stale}})
        with patch.dict(gui._figure_backfill_state, running=True, date='2026-09-03'):
            gui._sweep_figure_retention()
        self.assertTrue(os.path.exists(os.path.join(self.figures_dir, 'p2.png')))
        self.assertIn('p2', gui._load_figure_retention())

    def test_high_rating_in_any_digest_cancels_shared_retention(self):
        self._write_digest({'p2': 3})
        self._cache_figures('p2')
        other = '2026-09-03'
        figures.write_sidecar(self.digests_dir, other,
            figures.load_sidecar(self.digests_dir, self.DATE))
        names = [f'digest_{other}.figures.json', f'digest_{self.DATE}.figures.json']
        for order in [names, names[::-1]]:
            stale = (datetime.now() - timedelta(days=30)).isoformat()
            gui._save_figure_retention({'p2': {'date': self.DATE, 'below_since': stale}})
            with patch.object(gui.os, 'listdir', return_value=order), \
                 patch.object(gui, 'get_digest_path_for_date', side_effect=lambda date: date), \
                 patch.object(gui, 'parse_digest', side_effect=lambda date: {
                     'tiers': [{'papers': [{'paper_id': 'p2', 'score': 5 if date == other else 3}]}]
                 }):
                gui._sweep_figure_retention()
            self.assertTrue(os.path.exists(os.path.join(self.figures_dir, 'p2.png')))
            self.assertEqual(gui._load_figure_retention(), {})

    def test_high_rated_digest_without_sidecar_protects_shared_image(self):
        self._write_digest({'p2': 3})
        self._cache_figures('p2')
        other_path = os.path.join(self.digests_dir, 'digest_2026-09-03.md')
        with open(other_path, 'w') as f:
            f.write(_digest_markdown({'p2': 5}))
        stale = (datetime.now() - timedelta(days=30)).isoformat()
        gui._save_figure_retention({'p2': {'date': self.DATE, 'below_since': stale}})
        with patch.object(gui, 'get_digest_path_for_date', side_effect=lambda day:
                          self.digest_file if day == self.DATE else other_path):
            gui._sweep_figure_retention()
        self.assertTrue(os.path.exists(os.path.join(self.figures_dir, 'p2.png')))
        self.assertEqual(gui._load_figure_retention(), {})

    def _feedback_client(self):
        """A /feedback test client with persistence pointed at temp files."""
        feedback_file = os.path.join(self.tmp.name, "feedback.json")
        adjustments = {}

        def fake_record(date_str, paper_id, amount, path=None):
            current = adjustments.setdefault(date_str, {}).get(paper_id, 0)
            current += int(amount)
            adjustments[date_str][paper_id] = current
            return current

        def fake_get(date_str, paper_id, path=None):
            return adjustments.get(date_str, {}).get(paper_id, 0)

        extra = [
            patch.object(gui, "FEEDBACK_FILE", feedback_file),
            patch.object(gui, "record_adjustment", side_effect=fake_record),
            patch.object(gui, "get_adjustment", side_effect=fake_get),
            patch.object(gui, "rebuild_learned_profile"),
            patch.object(gui, "_load_config_and_env", return_value=({"keywords": []}, {})),
        ]
        for item in extra:
            item.start()
            self.addCleanup(item.stop)
        return gui.app.test_client(), adjustments

    def test_post_feedback_downgrade_hides_but_keeps_figures(self):
        self._write_digest({"p1": 5})
        self._cache_figures("p1")
        client, _adjustments = self._feedback_client()
        for _click in range(2):  # 5 -> 4 -> 3 stars
            response = client.post("/feedback", json={
                "paper_id": "p1", "title": "Paper p1",
                "action": "overrated", "original_score": 5, "date": self.DATE,
            })
            self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["score"], 3)
        self.assertEqual(data["figures"], "hidden")
        # The gallery view is gone server-side, but the images are retained.
        self.assertTrue(os.path.isfile(os.path.join(self.figures_dir, "p1.png")))
        self.assertIn("p1", gui._load_figure_retention())
        self.assertIn("p1", figures.load_sidecar(self.digests_dir, self.DATE)["papers"])

    def test_post_feedback_upgrade_starts_figure_fetch(self):
        self._write_digest({"p1": 3, "p2": 5})
        # What apply_to_digest would report once the upgrade is persisted.
        self.adjusted_scores["p1"] = 4
        fetched = Event()

        def fake_fetch(papers, figures_dir, digest_dir=None, digest_date=None,
                       **kwargs):
            fetched.set()
            # Record the papers as fully fetched so the backfill loop's
            # re-check ends the thread instead of spinning (a lingering
            # thread would leak into later tests with their patches).
            sidecar = (
                figures.load_sidecar(digest_dir, digest_date)
                if digest_dir and digest_date
                else {"papers": {}, "failed": {}, "pv": figures.PARSER_VERSION}
            )
            for paper in papers:
                pid = paper.get("id")
                sidecar["papers"][pid] = {
                    "source": "html", "files": [f"{pid}.png"],
                    "depth": figures.MAX_FIGURES,
                    "captions": [""], "capv": figures.CAPTION_VERSION,
                }
            if digest_dir and digest_date:
                figures.write_sidecar(digest_dir, digest_date, sidecar)
            return sidecar

        with patch.object(gui, "fetch_figures_for_digest", side_effect=fake_fetch):
            client, _adjustments = self._feedback_client()
            response = client.post("/feedback", json={
                "paper_id": "p1", "title": "Paper p1",
                "action": "underrated", "original_score": 3, "date": self.DATE,
            })
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data["score"], 4)
            self.assertEqual(data["figures"], "pending")
            self.assertTrue(fetched.wait(timeout=5), "backfill thread never fetched")
            deadline = time.monotonic() + 5
            while gui._figure_backfill_state["running"] and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(gui._figure_backfill_state["running"])

    def test_post_feedback_upgrade_with_cached_figures_reports_cached(self):
        self._write_digest({"p1": 3})
        # A full-depth entry: e.g. downgraded earlier, images still retained.
        self._cache_figures("p1", depth=figures.MAX_FIGURES)
        gui._save_figure_retention({
            "p1": {"date": self.DATE,
                   "below_since": datetime.now().isoformat(timespec="seconds")},
        })
        with patch.object(gui, "fetch_figures_for_digest") as fetch:
            client, _adjustments = self._feedback_client()
            response = client.post("/feedback", json={
                "paper_id": "p1", "title": "Paper p1",
                "action": "underrated", "original_score": 3, "date": self.DATE,
            })
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data["figures"], "cached")
            self.assertTrue(os.path.isfile(os.path.join(self.figures_dir, "p1.png")))
            # Re-upgrade cancels the pending deletion.
            self.assertEqual(gui._load_figure_retention(), {})
            fetch.assert_not_called()  # depth and captions already current

    def test_digest_page_ships_rating_figure_script(self):
        """The on-demand figure poller must exist on every digest page: a
        rating upgrade calls it even when nothing was pending at load."""
        self._write_digest({"p1": 5, "p2": 3})
        with patch.object(gui, "_start_figure_backfill"), \
             patch.object(gui, "fetch_figures_for_digest"):
            client, _adjustments = self._feedback_client()
            response = client.get("/digest/" + self.DATE)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("function startFigurePolling()", html)
        self.assertIn("function syncCardFigureState(", html)
        self.assertIn("startFigurePolling();", html)  # p1 pending at render
        # The downgraded-card guard keeps re-added anchors suppressed.
        self.assertIn("Number(card.dataset.score || 0) < 4", html)


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
        self.assertNotIn('class="reason-label"', gui.DIGEST_TEMPLATE)
        self.assertNotIn(">Match</span>", gui.DIGEST_TEMPLATE)
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
