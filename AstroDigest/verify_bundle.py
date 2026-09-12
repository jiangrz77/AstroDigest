#!/usr/bin/env python3
"""Fail a release if its actual frozen GUI/CLI cannot start.

Usage: python3 verify_bundle.py "/path/to/Astro Digest.app" [--window]
Runs without fetching papers, sending email, or reading existing user data.
--window also exercises the native webview; it needs a macOS desktop session.
"""

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def check_command(command, env, cwd, expected=None):
    result = subprocess.run(
        [str(arg) for arg in command], env=env, cwd=cwd,
        capture_output=True, text=True, timeout=30,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0 or (expected and expected not in output):
        raise RuntimeError(f"Command failed: {command}\n{output}")



def check_bundle_metadata(bundle):
    """Keep the visible brand, native executable, and old email links consistent."""
    with (Path(bundle) / "Contents" / "Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    for key, expected in (
        ("CFBundleDisplayName", "Astro Digest"),
        ("CFBundleName", "Astro Digest"),
        ("CFBundleExecutable", "AstroDigest"),
        # The existing identifier preserves macOS application identity.
        ("CFBundleIdentifier", "com.arxivdailydigest.app"),
    ):
        if info.get(key) != expected:
            raise RuntimeError(f"Invalid bundle metadata {key}: expected {expected!r}")
    schemes = {
        scheme
        for entry in info.get("CFBundleURLTypes", [])
        for scheme in entry.get("CFBundleURLSchemes", [])
    }
    if not {"astrodigest", "astropaperdigest"}.issubset(schemes):
        raise RuntimeError("Bundle must register current and historical digest URL schemes")


def verify_bundle(bundle, window=False):
    bundle = Path(bundle).resolve()
    frameworks = bundle / "Contents" / "Frameworks"
    gui = bundle / "Contents" / "MacOS" / "AstroDigest"
    cli = frameworks / "astrodigest-cli"
    # Both separately frozen executables need their own data-file collection.
    for root in (frameworks, frameworks / "_internal"):
        symbols = root / "latex2mathml" / "unimathsymbols.txt"
        if not symbols.is_file() or not symbols.stat().st_size:
            raise RuntimeError(f"Missing packaged math symbols: {symbols}")

    check_bundle_metadata(bundle)

    with tempfile.TemporaryDirectory(prefix="astrodigest-smoke-") as temp:
        data = Path(temp)
        env = dict(os.environ)
        # Do not inherit another frozen process's extraction state, Python
        # paths, or credentials into the standalone application under test.
        for key in list(env):
            if key.startswith(("_PYI", "PYTHON", "DYLD_", "SMTP_", "EMAIL_")) or key.endswith("_API_KEY"):
                env.pop(key)
        env["ASTRODIGEST_DATA_DIR"] = str(data)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        (data / "preferences.json").write_text(
            json.dumps({"auto_check_updates": False}), encoding="utf-8",
        )
        # Empty setup marker, not credentials: exercise the actual settings
        # page rather than silently accepting its first-run redirect.
        (data / ".env").touch()
        check_command(["/usr/bin/codesign", "--verify", "--deep", "--strict", bundle], env, temp)
        # The windowed PyInstaller target uses argv emulation, so invoking it
        # with --help does not reliably deliver that flag to Python and can
        # initialize macOS window services in a headless runner. The actual
        # --no-run/--no-window smoke test below validates the GUI executable.
        check_command([cli, "--help"], env, temp, expected="usage:")
        print("PASS: packaged GUI and CLI help/startup", flush=True)

        command = [str(gui), "--no-run"]
        if not window:
            command.append("--no-window")
        # File-backed output avoids blocking the child on a full pipe.
        with tempfile.TemporaryFile(mode="w+t") as log:
            process = subprocess.Popen(command, env=env, cwd=temp, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                info_path = data / "apd-run.json"
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"Packaged GUI exited early ({process.returncode})")
                    try:
                        info = json.loads(info_path.read_text(encoding="utf-8"))
                        if info["pid"] != process.pid:
                            raise RuntimeError("Smoke test connected to a different app instance")
                        base = f"http://127.0.0.1:{int(info['port'])}"
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        for route, expected in (
                            ("/", b"Astro Digest"),
                            ("/setup", b"Astro Digest"),
                            ("/settings", b"Recipients"),
                            ("/static/mathjax/config.js", b"MathJax"),
                        ):
                            with opener.open(base + route, timeout=3) as response:
                                if response.status != 200 or response.geturl() != base + route or expected not in response.read():
                                    raise RuntimeError(f"Invalid packaged response for {route}")
                        break
                    except (OSError, ValueError, KeyError, urllib.error.URLError):
                        time.sleep(0.2)
                else:
                    raise RuntimeError("Timed out waiting for the packaged GUI server")
                # Let the native backend initialize too, when requested.
                try:
                    process.wait(timeout=5 if window else 1)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    raise RuntimeError(f"Packaged GUI stopped after startup ({process.returncode})")
                print("PASS: packaged pages and offline assets (no pipeline/email)", flush=True)
            except Exception as exc:
                log.flush()
                log.seek(0)
                raise RuntimeError(f"{exc}\n{log.read()}") from exc
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--window", action="store_true")
    args = parser.parse_args()
    verify_bundle(args.bundle, window=args.window)
