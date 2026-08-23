"""Regression tests for publishing a digest before sending its email."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import gui


class PipelineEmailTransactionTests(unittest.TestCase):
    def setUp(self):
        gui._pipeline_cancel_requested = False
        gui._pipeline_status = "idle"

    def test_email_is_sent_only_after_staged_outputs_are_committed(self):
        events = []
        config = {
            "email": {
                "enabled": True,
                "sender": "sender@example.com",
                "recipient": "sender@example.com",
            }
        }
        env_values = {"EMAIL_APP_PASSWORD": "saved-password"}
        with tempfile.TemporaryDirectory() as tmp:
            digest = Path(tmp) / "digest_2026-08-23.md"
            digest.write_text("# AstroPaperDigest - 2026-08-23\n", encoding="utf-8")
            stdout = f"=== Done! ===\nDigest: {digest}\n"

            def stream(command):
                self.assertIn("--no-email", command)
                return 0, stdout, tmp

            with mock.patch.object(gui, "_stream_pipeline", side_effect=stream), \
                 mock.patch.object(gui, "_commit_staged_outputs", side_effect=lambda _p: events.append("commit")), \
                 mock.patch.object(gui, "clear_date"), \
                 mock.patch.object(gui, "get_digest_path_for_date", return_value=str(digest)), \
                 mock.patch.object(gui, "parse_digest", return_value={"date": "2026-08-23"}), \
                 mock.patch.object(gui, "_load_config_and_env", return_value=(config, env_values)), \
                 mock.patch.object(gui, "send_digest_file", side_effect=lambda *_a, **_k: events.append("send") or True):
                gui.run_pipeline(target_date="2026-08-23")

        self.assertEqual(events, ["commit", "send"])

    def test_commit_reports_final_custom_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            source = staging / "digests" / "digest_2026-08-23.md"
            source.parent.mkdir(parents=True)
            source.write_text("digest", encoding="utf-8")
            destination = root / "custom-digests"
            config = {
                "output": {
                    "digest_dir": str(destination),
                    "bibtex_dir": str(root / "custom-bibtex"),
                }
            }

            with mock.patch.object(gui, "_load_config_and_env", return_value=(config, {})):
                published = gui._commit_staged_outputs(str(staging))

            final_path = destination / source.name
            self.assertEqual(published["digests"], [str(final_path)])
            self.assertEqual(final_path.read_text(encoding="utf-8"), "digest")
            self.assertFalse(source.exists())

    def test_cancelled_pipeline_neither_commits_nor_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            def stream(_command):
                gui._pipeline_cancel_requested = True
                return 0, "=== Done! ===\n", tmp

            with mock.patch.object(gui, "_stream_pipeline", side_effect=stream), \
                 mock.patch.object(gui, "_commit_staged_outputs") as commit, \
                 mock.patch.object(gui, "send_digest_file") as send:
                gui.run_pipeline(target_date="2026-08-23")

        commit.assert_not_called()
        send.assert_not_called()
        self.assertEqual(gui._pipeline_status, "cancelled")


if __name__ == "__main__":
    unittest.main()
