"""Regression coverage for the AstroDigest update transition."""

import zipfile
from pathlib import Path
from unittest import TestCase, mock

from src import updater


class UpdateBrandingTests(TestCase):
    def test_display_bundle_name_is_spaced_while_executable_name_stays_technical(self):
        self.assertEqual(updater.DISPLAY_APP_BUNDLE_NAME, "Astro Digest.app")
        self.assertIn("AstroDigest.app", updater.LEGACY_APP_BUNDLE_NAMES)

    def _release(self):
        app_hash = "a" * 64
        source_hash = "b" * 64
        body = "\n".join(
            [
                "- AstroDigest-v3.0.0.app.zip",
                f"  SHA256: {app_hash}",
                "- AstroDigest-v3.0.0.source.zip",
                f"  SHA256: {source_hash}",
                "- AstroDigest-3.0.0.dmg",
                f"  SHA256: {'c' * 64}",
            ]
        )
        return {
            "tag_name": "v3.0.0",
            "assets": [
                {"name": "AstroDigest-v3.0.0.app.zip", "browser_download_url": "app"},
                {"name": "AstroDigest-v3.0.0.source.zip", "browser_download_url": "source"},
                {"name": "AstroDigest-3.0.0.dmg", "browser_download_url": "dmg"},
                {"name": "AstroPaperDigest-v3.0.0.app.zip", "browser_download_url": "old-app"},
            ],
            "body": body,
        }

    def test_frozen_channel_prefers_new_app_and_uses_its_hash(self):
        with mock.patch.object(updater.sys, "frozen", True, create=True):
            release = updater.normalize_release(self._release())
        self.assertEqual(release["download_url"], "app")
        self.assertEqual(release["sha256"], "a" * 64)

    def test_source_channel_selects_source_hash(self):
        with mock.patch.object(updater.sys, "frozen", False, create=True):
            release = updater.normalize_release(self._release())
        self.assertEqual(release["download_url"], "source")
        self.assertEqual(release["sha256"], "b" * 64)

    def test_legacy_frozen_app_hash_is_supported_when_filename_is_old(self):
        digest = "d" * 64
        data = {
            "tag_name": "v2.9.0",
            "assets": [{
                "name": "AstroPaperDigest-v2.9.0.app.zip",
                "browser_download_url": "old-app",
            }],
            "body": f"SHA-256: {digest}",
        }
        with mock.patch.object(updater.sys, "frozen", True, create=True):
            release = updater.normalize_release(data)
        self.assertEqual(release["download_url"], "old-app")
        self.assertEqual(release["sha256"], digest)

    def test_flat_and_wrapped_source_archives_are_accepted(self):
        # Exercise the locator with real temporary directories through a ZIP.
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "main.py").write_text("", encoding="utf-8")
            (root / "build_app.sh").write_text("", encoding="utf-8")
            (root / "src").mkdir()
            archive = root / "flat.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for path in (root / "main.py", root / "build_app.sh"):
                    zf.write(path, path.name)
                zf.writestr("src/__init__.py", "")
            with tempfile.TemporaryDirectory() as extract:
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(extract)
                self.assertEqual(updater._source_package_root(Path(extract)), Path(extract))

            wrapped = root / "wrapped"
            (wrapped / "AstroDigest/src").mkdir(parents=True)
            (wrapped / "AstroDigest/main.py").write_text("", encoding="utf-8")
            (wrapped / "AstroDigest/build_app.sh").write_text("", encoding="utf-8")
            self.assertEqual(updater._source_package_root(wrapped), wrapped / "AstroDigest")
