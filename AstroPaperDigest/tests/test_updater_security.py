"""Security regression tests for update package verification."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.updater import check_update, verify_sha256


class UpdaterSecurityTests(unittest.TestCase):
    def test_update_hash_is_mandatory_and_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "update.zip"
            package.write_bytes(b"trusted update payload")
            digest = hashlib.sha256(package.read_bytes()).hexdigest()

            self.assertTrue(verify_sha256(package, digest))
            self.assertTrue(verify_sha256(package, digest.upper()))
            self.assertFalse(verify_sha256(package, ""))
            self.assertFalse(verify_sha256(package, None))
            self.assertFalse(verify_sha256(package, "not-a-sha256"))
            self.assertFalse(verify_sha256(package, "0" * 64))

    def test_release_without_hash_is_not_offered_for_installation(self):
        release = {
            "version": "2.0.0",
            "tag": "v2.0.0",
            "notes": "Release without a checksum",
            "download_url": "https://example.com/update.zip",
            "sha256": None,
            "published_at": "2026-08-23T00:00:00Z",
            "prerelease": False,
            "has_update_package": True,
        }
        with mock.patch("src.updater.check_github_release", return_value=release):
            result = check_update("owner/repo", current="1.0.0")

        self.assertFalse(result["available"])
        self.assertEqual(result["download_url"], "")
        self.assertIn("SHA-256", result["error"])


if __name__ == "__main__":
    unittest.main()
