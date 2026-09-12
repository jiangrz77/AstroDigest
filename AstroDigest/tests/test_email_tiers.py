"""Offline coverage for detailed recommendations and compact email entries."""

import copy
import json
import os
import sys
import tempfile
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.notifier import _notification_fingerprint, send_digest_notification


DATE = "2026-09-12"
SECTION_NAMES = ("5-Star Recommendations", "4-Star Recommendations", "Other Papers")


def _paper(number, score, **overrides):
    paper = {
        "id": f"2609.{number:05d}",
        "title": f"Distinct paper {number}",
        "authors": [f"First author {number}", f"Second author {number}"],
        "categories": ["astro-ph.GA", "astro-ph.CO"],
        "score": score,
        "reason": f"Unique reason for paper {number}.",
        "abstract": f"Complete abstract for paper {number}.",
    }
    paper.update(overrides)
    return paper


class _HTMLText(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.parts = []
        self.feed(body)

    def handle_data(self, data):
        self.parts.append(data)

    @property
    def text(self):
        return " ".join(self.parts)


def _send(papers):
    captured = {}

    def capture(subject, body, config, **kwargs):
        captured.update(subject=subject, plain=body, **kwargs)
        return True

    with tempfile.TemporaryDirectory() as directory, mock.patch(
        "src.notifier.send_email", side_effect=capture
    ), mock.patch("src.notifier.reason_keywords", return_value=[]):
        state_path = os.path.join(directory, "email-state.json")
        assert send_digest_notification(
            papers, {"sender": "sender@example.com"}, DATE, state_path=state_path
        )
        with open(state_path, encoding="utf-8") as saved:
            captured["state"] = json.load(saved)["sent_dates"][DATE]
    captured["html_text"] = _HTMLText(captured["html_body"]).text
    return captured


def test_email_groups_all_papers_by_rating_with_stable_order():
    papers = [_paper(1, 3), _paper(2, 4), _paper(3, 5), _paper(4, 2),
              _paper(5, 4), _paper(6, 5), _paper(7, 1), _paper(8, 3)]
    message = _send(papers)
    assert message["subject"] == "Astro Digest | 2026-09-12 Sat | 8 papers"
    expected_order = [papers[i] for i in (2, 5, 1, 4, 0, 7, 3, 6)]

    for body in (message["plain"], message["html_text"]):
        section_positions = [body.index(section) for section in SECTION_NAMES]
        assert section_positions == sorted(section_positions)
        positions = [body.index(paper["title"]) for paper in expected_order]
        assert positions == sorted(positions)
        for paper in papers:
            assert body.count(paper["title"]) == 1
            assert all(author in body for author in paper["authors"])
            if paper["score"] >= 4:
                assert paper["reason"] in body
                assert paper["abstract"] in body
            else:
                assert paper["reason"] not in body
                assert paper["abstract"] not in body

    other_plain = message["plain"].split("Other Papers", 1)[1]
    other_html = message["html_body"].split("Other Papers", 1)[1]
    assert "Abstract:" not in other_plain
    assert "Abstract:" not in other_html
    assert "border-left:3px solid" not in other_html
    for paper in papers:
        link = f"https://arxiv.org/abs/{paper['id']}"
        assert link in message["plain"]
        assert link in message["html_body"]
    assert message["html_text"].count("View paper on arXiv") == len(papers)
    assert message["state"]["five_star_count"] == 2
    assert message["state"]["four_star_count"] == 2
    assert message["state"]["other_paper_count"] == 4


def test_four_star_only_email_keeps_overview_and_hides_empty_sections():
    paper = _paper(1, 4)
    message = _send([paper])
    for body in (message["plain"], message["html_text"]):
        assert "4-Star Recommendations" in body
        assert "5-Star Recommendations" not in body
        assert "Other Papers" not in body
        assert paper["reason"] in body
        assert paper["abstract"] in body
        assert "There are no" not in body
    assert message["state"]["five_star_count"] == 0
    assert message["state"]["four_star_count"] == 1
    assert message["state"]["other_paper_count"] == 0


def test_lower_rated_only_email_keeps_metadata_and_links():
    papers = [_paper(1, 1), _paper(2, 3), _paper(3, 2)]
    message = _send(papers)
    for body in (message["plain"], message["html_text"]):
        assert "Other Papers" in body
        assert "5-Star Recommendations" not in body
        assert "4-Star Recommendations" not in body
        assert "There are no" not in body
        assert "Abstract:" not in body
        for paper in papers:
            assert paper["title"] in body
            assert all(author in body for author in paper["authors"])
            assert all(category in body for category in paper["categories"])
            assert paper["reason"] not in body
            assert paper["abstract"] not in body
    assert "★★★" in message["plain"]
    assert message["state"]["five_star_count"] == 0
    assert message["state"]["four_star_count"] == 0
    assert message["state"]["other_paper_count"] == 3


def test_failed_scoring_is_compact_and_never_claims_stars():
    paper = _paper(1, 5, scoring_failed=True)
    message = _send([paper])
    assert message["subject"] == "Astro Digest | 2026-09-12 Sat | 1 paper"
    for body in (message["plain"], message["html_text"]):
        assert "Other Papers" in body
        assert "5-Star Recommendations" not in body
        assert "4-Star Recommendations" not in body
        assert "Not scored" in body
        assert "★" not in body
        assert paper["title"] in body
        assert all(author in body for author in paper["authors"])
        assert paper["reason"] not in body
        assert paper["abstract"] not in body
    assert "5 out of 5 stars" not in message["html_body"]
    assert message["state"]["five_star_count"] == 0
    assert message["state"]["four_star_count"] == 0
    assert message["state"]["other_paper_count"] == 1


def test_empty_email_uses_digest_empty_message_and_no_paper_sections():
    message = _send([])
    assert message["subject"] == "Astro Digest | 2026-09-12 Sat | 0 papers"
    for body in (message["plain"], message["html_text"]):
        assert "There are no papers in this daily digest." in body
        assert all(section not in body for section in SECTION_NAMES)
        assert "Open Astro Digest" in body
        assert "View paper on arXiv" not in body
    for key in ("five_star_count", "four_star_count", "other_paper_count"):
        assert message["state"][key] == 0


def test_fingerprint_tracks_displayed_fields_for_every_rating():
    papers = [_paper(1, 5), _paper(2, 4), _paper(3, 3)]
    original = _notification_fingerprint(DATE, papers)
    changes = {
        "title": "Changed displayed title",
        "authors": ["Changed author"],
        "categories": ["astro-ph.SR"],
        "link": "https://arxiv.org/abs/2609.99999",
        "score": 2,
        "scoring_failed": True,
    }
    for index in range(len(papers)):
        for field, value in changes.items():
            changed = copy.deepcopy(papers)
            changed[index][field] = value
            assert _notification_fingerprint(DATE, changed) != original, (index, field)
    assert _notification_fingerprint("2026-09-13", papers) != original


def test_fingerprint_ignores_hidden_overviews_but_tracks_recommended_overviews():
    papers = [_paper(1, 5), _paper(2, 4), _paper(3, 3),
              _paper(4, 5, scoring_failed=True)]
    original = _notification_fingerprint(DATE, papers)
    for index in range(len(papers)):
        for field in ("reason", "abstract"):
            changed = copy.deepcopy(papers)
            changed[index][field] = "A revised overview."
            updated = _notification_fingerprint(DATE, changed)
            if index < 2:
                assert updated != original, (index, field)
            else:
                assert updated == original, (index, field)
