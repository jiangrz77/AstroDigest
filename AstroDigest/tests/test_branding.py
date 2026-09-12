"""Regression coverage for renamed output and inputs from previous releases."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main as cli
from src import gui
from src.digest_parser import parse_digest
from src.notifier import send_digest_notification
from src.output import generate_markdown_digest


class DigestBrandingTests(unittest.TestCase):
    def test_current_and_historical_titles_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest = Path(tmp) / "digest.md"
            for title in ("Astro Digest", "AstroDigest", "AstroPaperDigest", "Arxiv Daily Digest"):
                with self.subTest(title=title):
                    digest.write_text(f"# {title} - 2026-08-23\n", encoding="utf-8")
                    self.assertEqual(parse_digest(str(digest))["date"], "2026-08-23")

    def test_new_digests_and_empty_digests_use_display_name(self):
        content = generate_markdown_digest([], digest_date="2026-08-23")
        self.assertTrue(content.startswith("# Astro Digest - 2026-08-23\n"))
        with tempfile.TemporaryDirectory() as tmp:
            cli._write_empty_digest(tmp, "no_announcement", "2026-08-23")
            digest = Path(tmp) / "digest_2026-08-23.md"
            self.assertTrue(digest.read_text().startswith("# Astro Digest - 2026-08-23\n"))
            self.assertEqual(parse_digest(str(digest))["status"], "no_announcement")

    def test_staging_accepts_legacy_environment_and_prefers_new_name(self):
        cases = (
            ({"APD_OUTPUT_STAGING_DIR": "/legacy"}, "/legacy/digests"),
            ({"ASTRODIGEST_OUTPUT_STAGING_DIR": "/current"}, "/current/digests"),
            ({"ASTRODIGEST_OUTPUT_STAGING_DIR": " /current ", "APD_OUTPUT_STAGING_DIR": "/legacy"}, "/current/digests"),
            ({"ASTRODIGEST_OUTPUT_STAGING_DIR": " ", "APD_OUTPUT_STAGING_DIR": "/legacy"}, "/legacy/digests"),
            ({}, "/configured"),
        )
        for environment, expected in cases:
            with self.subTest(environment=environment), \
                 mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch("sys.argv", ["astrodigest-cli", "--target-date", "2026-08-23", "--no-email"]), \
                 mock.patch.object(cli, "load_config", return_value={"output": {"digest_dir": "/configured"}}), \
                 mock.patch.object(cli, "build_profile_from_config", return_value={"keywords": [], "categories": []}), \
                 mock.patch.object(cli, "fetch_daily_batch", return_value={"status": "no_announcement", "message": "Weekend"}), \
                 mock.patch.object(cli, "_write_empty_digest") as write:
                cli.main()
                write.assert_called_once_with(expected, "no_announcement", "2026-08-23", note="")

    def test_new_email_continues_historical_thread(self):
        historical_id = "<historical-digest@example.com>"
        previous_id = "<previous-digest@example.com>"
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "notification_state.json"
            state.write_text(json.dumps({
                "last_message_id": previous_id,
                "references": [historical_id, previous_id],
                "sent_dates": {"2026-08-23": {"message_id": previous_id}},
            }), encoding="utf-8")
            with mock.patch("src.notifier.send_email", return_value=True) as send:
                self.assertTrue(send_digest_notification(
                    [], {"enabled": True, "sender": "sender@example.com"},
                    "2026-08-24", state_path=state,
                ))
            headers = send.call_args.kwargs["headers"]
            self.assertEqual(headers["In-Reply-To"], previous_id)
            self.assertEqual(headers["References"], f"{historical_id} {previous_id}")
            self.assertEqual(headers["Thread-Topic"], "Astro Digest Daily")
            self.assertEqual(headers["X-AstroDigest-Date"], "2026-08-24")
            self.assertEqual(headers["X-AstroDigest-Thread"], "daily-digest")
            self.assertIn("2026-08-23", json.loads(state.read_text())["sent_dates"])

    def test_saved_official_update_repository_tracks_rename(self):
        cases = (
            ("jiangrz77/AstroPaperDigest", "jiangrz77/AstroDigest"),
            ("jiangrz77/AstroDigest", "jiangrz77/AstroDigest"),
            ("another-user/custom-build", "another-user/custom-build"),
        )
        for configured, expected in cases:
            with self.subTest(configured=configured), \
                 mock.patch("builtins.open", mock.mock_open(read_data=f"update:\n  github_repo: {configured}\n  extra_option: true\n")):
                settings = gui._update_config()
            self.assertEqual(settings["github_repo"], expected)
            self.assertTrue(settings["extra_option"])


if __name__ == "__main__":
    unittest.main()
