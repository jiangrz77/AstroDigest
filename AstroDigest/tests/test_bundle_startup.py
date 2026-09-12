"""Regression checks for the v2.2.0 missing-data startup failure."""

import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest import mock

from src import paths
from verify_bundle import check_bundle_metadata, check_command, verify_bundle


PROJECT = Path(__file__).resolve().parents[1]


class BundleStartupTests(unittest.TestCase):
    def test_both_builders_collect_symbols_for_both_executables(self):
        for name in ("build_app.sh", "build_dmg.sh"):
            with self.subTest(builder=name):
                source = (PROJECT / name).read_text(encoding="utf-8")
                self.assertEqual(source.count("--collect-data latex2mathml"), 2)
                self.assertIn("verify_bundle.py", source)
                self.assertLess(source.index("codesign --force --deep"), source.index("verify_bundle.py"))

    def test_dmg_is_created_only_after_startup_verification(self):
        source = (PROJECT / "build_dmg.sh").read_text(encoding="utf-8")
        self.assertLess(source.index("verify_bundle.py"), source.index("hdiutil create"))

    def test_bundle_metadata_preserves_identity_and_both_email_schemes(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "Astro Digest.app"
            contents = bundle / "Contents"
            contents.mkdir(parents=True)
            info = {
                "CFBundleDisplayName": "Astro Digest",
                "CFBundleName": "Astro Digest",
                "CFBundleExecutable": "AstroDigest",
                "CFBundleIdentifier": "com.arxivdailydigest.app",
                "CFBundleURLTypes": [{"CFBundleURLSchemes": ["astrodigest", "astropaperdigest"]}],
            }
            plist = contents / "Info.plist"
            plist.write_bytes(plistlib.dumps(info))
            check_bundle_metadata(bundle)
            for key in ("CFBundleDisplayName", "CFBundleName", "CFBundleExecutable", "CFBundleIdentifier"):
                with self.subTest(key=key):
                    plist.write_bytes(plistlib.dumps({**info, key: "Wrong"}))
                    with self.assertRaisesRegex(RuntimeError, key):
                        check_bundle_metadata(bundle)
            for scheme in ("astrodigest", "astropaperdigest"):
                with self.subTest(scheme=scheme):
                    plist.write_bytes(plistlib.dumps({
                        **info, "CFBundleURLTypes": [{"CFBundleURLSchemes": [scheme]}],
                    }))
                    with self.assertRaisesRegex(RuntimeError, "URL schemes"):
                        check_bundle_metadata(bundle)

    def test_missing_symbols_fail_before_any_app_launch(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch("verify_bundle.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "Missing packaged math symbols"):
                verify_bundle(Path(temp) / "Broken.app")
            run.assert_not_called()

    def test_missing_cli_symbols_are_also_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "App.app" / "Contents" / "Frameworks" / "latex2mathml"
            root.mkdir(parents=True)
            (root / "unimathsymbols.txt").write_text("fixture", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "_internal/latex2mathml"):
                verify_bundle(Path(temp) / "App.app")

    def test_failed_executable_is_not_accepted(self):
        result = subprocess.CompletedProcess([], 1, "", "FileNotFoundError")
        with mock.patch("verify_bundle.subprocess.run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "FileNotFoundError"):
                check_command(["app", "--help"], {}, "/tmp")

    def test_silent_or_hung_executable_is_not_accepted(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("verify_bundle.subprocess.run", return_value=result):
            with self.assertRaises(RuntimeError):
                check_command(["app", "--help"], {}, "/tmp", expected="usage:")
        with mock.patch("verify_bundle.subprocess.run", side_effect=subprocess.TimeoutExpired("app", 30)):
            with self.assertRaises(subprocess.TimeoutExpired):
                check_command(["app", "--help"], {}, "/tmp")

    def test_frozen_data_and_instance_paths_share_test_override(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"APD_DATA_DIR": temp}), \
                mock.patch("sys.frozen", True, create=True):
            self.assertEqual(paths.data_dir(), Path(temp))
            self.assertEqual(paths.app_support_dir(), Path(temp))

    def test_default_data_path_is_unchanged(self):
        with mock.patch.dict(os.environ, {"ASTRODIGEST_DATA_DIR": "", "APD_DATA_DIR": ""}), \
                mock.patch.object(paths, "_LEGACY_APP_SUPPORT_DIR", Path("/nonexistent-astrodigest-legacy")):
            self.assertEqual(paths.app_support_dir(), paths._APP_SUPPORT_DIR)

    def test_relative_override_is_rejected(self):
        with mock.patch.dict(os.environ, {"APD_DATA_DIR": "relative"}):
            with self.assertRaisesRegex(ValueError, "absolute path"):
                paths.app_support_dir()


if __name__ == "__main__":
    unittest.main()
