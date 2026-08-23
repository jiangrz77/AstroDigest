#!/usr/bin/env python3
"""Regression tests for the Settings email panel."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gui import (  # noqa: E402
    DIGEST_TEMPLATE,
    _apply_email,
    _deep_link_date,
    app,
)


class EmailSettingsTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_settings_page_contains_email_handlers(self):
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('id="enable_email"', page)
        self.assertIn('id="email_address"', page)
        self.assertIn("Send/Receive Email", page)
        self.assertNotIn('id="email_sender"', page)
        self.assertNotIn('id="email_recipient"', page)
        self.assertIn("function toggleEmail()", page)
        self.assertIn("function emailAction(url, message)", page)
        self.assertIn("emailAction('/email/test'", page)
        self.assertIn("emailAction('/email/resend'", page)
        self.assertNotIn("Email notification is under development", page)

    def test_saved_password_is_preserved_when_field_is_blank(self):
        config = {"email": {}}
        env_values = {"EMAIL_APP_PASSWORD": "already-saved"}
        with app.test_request_context("/"):
            _apply_email(
                config,
                env_values,
                enable_email=True,
                email_address="sender@example.com",
                smtp_server="smtp.example.com",
                smtp_protocol="ssl",
                smtp_port_value="465",
                email_password="",
            )
        self.assertEqual(env_values["EMAIL_APP_PASSWORD"], "already-saved")
        self.assertEqual(env_values["EMAIL_SENDER"], "sender@example.com")
        self.assertEqual(env_values["EMAIL_RECIPIENT"], "sender@example.com")
        self.assertTrue(config["email"]["enabled"])
        self.assertEqual(config["email"]["sender"], "sender@example.com")
        self.assertEqual(config["email"]["recipient"], "sender@example.com")

    def test_deep_link_date_is_validated(self):
        self.assertEqual(
            _deep_link_date("astropaperdigest://digest/2026-08-23"),
            "2026-08-23",
        )
        self.assertEqual(_deep_link_date("astropaperdigest://digest/not-a-date"), "")

    def test_digest_toolbar_uses_icon_controls_and_conditional_email_button(self):
        self.assertIn('id="search-trigger"', DIGEST_TEMPLATE)
        self.assertIn('id="search-expanded"', DIGEST_TEMPLATE)
        self.assertIn('id="filter-trigger"', DIGEST_TEMPLATE)
        self.assertIn('id="btn-email-current"', DIGEST_TEMPLATE)
        self.assertIn("{% if email_configured %}", DIGEST_TEMPLATE)
        self.assertNotIn('>Filters</span>', DIGEST_TEMPLATE)

    def test_send_current_digest_forces_explicit_resend(self):
        config = {
            "email": {
                "enabled": True,
                "sender": "sender@example.com",
                "recipient": "sender@example.com",
                "smtp_server": "smtp.example.com",
            }
        }
        env_values = {"EMAIL_APP_PASSWORD": "saved-password"}
        with tempfile.TemporaryDirectory() as tmp:
            digest_path = os.path.join(tmp, "digest_2026-08-23.md")
            with open(digest_path, "w", encoding="utf-8") as handle:
                handle.write("# AstroPaperDigest - 2026-08-23\n")
            with mock.patch("src.gui._load_config_and_env", return_value=(config, env_values)), \
                 mock.patch("src.gui.get_digest_path_for_date", return_value=digest_path), \
                 mock.patch("src.gui.send_digest_file", return_value=True) as send:
                response = self.client.post(
                    "/email/send-current",
                    json={"date": "2026-08-23"},
                )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        send.assert_called_once_with(digest_path, config["email"], force=True)


if __name__ == "__main__":
    unittest.main()
