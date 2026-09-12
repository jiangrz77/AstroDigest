#!/usr/bin/env python3
"""Update checker and installer for AstroDigest.

Checks GitHub Releases (or a static JSON manifest) for a newer version,
downloads and verifies the source zip, then applies it with a restart.

All standard-library only - no new dependencies.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from src import paths as _paths
_PROJECT_DIR = _paths.data_dir()
VERSION_FILE = _PROJECT_DIR / "version.txt"
UPDATES_DIR = _PROJECT_DIR / "output" / "updates"
BACKUPS_DIR = _PROJECT_DIR / "backups"
PENDING_FILE = _PROJECT_DIR / "pending_update.json"

GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_UA = "AstroDigest-Updater/1.0"
NETWORK_TIMEOUT = 6
DOWNLOAD_TIMEOUT = 60
DISPLAY_APP_BUNDLE_NAME = "Astro Digest.app"
LEGACY_APP_BUNDLE_NAMES = ("AstroDigest.app", "AstroPaperDigest.app")

# Top-level entries that belong to the user and must NEVER be replaced.
KEEP_TOP = {
    ".env",
    "config.yaml",
    "preferences.json",
    "feedback.json",
    "data",
    "output",
    ".venv",
    "venv",
    ".git",
    "backups",
    ".DS_Store",
    "pending_update.json",
}


class UpdateCheckError(Exception):
    """Raised when the update check fails (network, 404, rate limit...)."""


class UpdateApplyError(Exception):
    """Raised when applying an update fails."""


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def get_current_version() -> str:
    """Return the installed version.

    Frozen builds: prefer CFBundleShortVersionString from the bundle's
    Info.plist (version.txt in the data dir is only written after an update).
    Source builds: read version.txt from the repo root.
    """
    if getattr(sys, "frozen", False):
        try:
            import plistlib

            exe = Path(sys.executable).resolve()
            plist_path = exe.parent.parent.parent / "Contents" / "Info.plist"
            if plist_path.exists():
                with open(plist_path, "rb") as f:
                    info = plistlib.load(f)
                v = str(info.get("CFBundleShortVersionString") or "").strip()
                if v and v != "0.0.0":
                    return v
        except Exception:
            pass
    try:
        v = VERSION_FILE.read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass
    return "0.0.0"


def parse_version(v: str):
    """Return a comparable tuple (major, minor, patch) from 'v1.2.3' etc."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v or "")
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def _http_json(url: str, timeout: int = NETWORK_TIMEOUT):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": GITHUB_UA,  # GitHub API requires a User-Agent
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _update_package_name(version: str) -> str:
    """Name of this channel's update package (source zip vs .app bundle zip)."""
    suffix = ".app" if getattr(sys, "frozen", False) else ".source"
    return f"AstroDigest-v{version}{suffix}.zip"


def _release_asset_sha256(asset: dict, zip_assets: list, body: str) -> str | None:
    """Find the checksum belonging to the selected package, not another channel."""
    digest = str(asset.get("digest") or "").strip()
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", digest, re.IGNORECASE)
    if match:
        return match.group(1).lower()

    filename = str(asset.get("name") or "")
    filename_pattern = re.compile(r"(?<![\w.-])" + re.escape(filename) + r"(?![\w.-])")
    hash_pattern = re.compile(r"(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])", re.IGNORECASE)
    archive_pattern = re.compile(r"\S+\.(?:zip|dmg)\b", re.IGNORECASE)
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if not filename_pattern.search(line):
            continue
        match = hash_pattern.search(line)
        if match:
            return match.group(1).lower()
        # The release workflow lists a filename followed by its SHA256 line.
        for next_line in lines[index + 1:]:
            if not next_line.strip():
                continue
            if not archive_pattern.search(next_line):
                match = hash_pattern.search(next_line)
                if match:
                    return match.group(1).lower()
            break

    # Historical app releases put the app checksum first, without a filename.
    # A source client must never consume that first hash when multiple ZIPs exist.
    legacy_app = bool(getattr(sys, "frozen", False)) and filename.startswith("AstroPaperDigest-")
    if (legacy_app or len(zip_assets) == 1) and not any(
        str(other.get("name") or "") in body
        for other in zip_assets
        if other is not asset
    ):
        match = re.search(r"(?i)\bsha-?256[^0-9a-f]{0,8}([0-9a-f]{64})(?![0-9a-f])", body)
        if match:
            return match.group(1).lower()
    return None


def normalize_release(data: dict) -> dict:
    """Map a GitHub release object to our normalized shape."""
    version = (data.get("tag_name") or "").lstrip("v")
    assets = data.get("assets") or []
    zip_assets = [
        a for a in assets
        if (a.get("name") or "").lower().endswith(".zip")
    ]
    package_name = _update_package_name(version)
    package_names = (package_name, package_name.replace("AstroDigest-", "AstroPaperDigest-", 1))
    chosen = next((asset for name in package_names for asset in zip_assets
                   if asset.get("name") == name), None)
    download_url = (chosen.get("browser_download_url") or "") if chosen else ""
    body = data.get("body") or ""
    sha256 = _release_asset_sha256(chosen, zip_assets, body) if chosen else None
    return {
        "version": version,
        "tag": data.get("tag_name") or f"v{version}",
        "name": data.get("name") or data.get("tag_name") or f"v{version}",
        "notes": body.strip(),
        "published_at": data.get("published_at"),
        "download_url": download_url,
        "sha256": sha256,
        "prerelease": bool(data.get("prerelease")),
        "has_update_package": chosen is not None,
    }


def check_github_release(repo: str, timeout: int = NETWORK_TIMEOUT) -> dict:
    """Fetch the latest GitHub release. Raises UpdateCheckError on failure."""
    url = GITHUB_API.format(repo=repo)
    data = _http_json(url, timeout)
    return normalize_release(data)


def check_update(repo: str, current: str | None = None) -> dict:
    """Compare the latest release with the installed version.

    Returns a dict with: available/current/latest/tag/notes/download_url/
    sha256/published_at/error. Raises UpdateCheckError on network/API errors.
    """
    current = current or get_current_version()
    try:
        release = check_github_release(repo)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateCheckError(
                "No version information found: the repository does not exist, "
                "has no releases, or is private (private repositories cannot be "
                "checked anonymously; make it public or use a self-hosted update source)."
            ) from e
        if e.code == 403:
            raise UpdateCheckError("GitHub API access is limited (HTTP 403). Please try again later.") from e
        raise UpdateCheckError(f"Update check failed (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise UpdateCheckError(f"Network error, could not reach the update server: {reason}") from e
    except Exception as e:
        raise UpdateCheckError(f"Update check failed: {e}") from e

    if release.get("prerelease"):
        return {
            "available": False,
            "current": current,
            "latest": release["version"],
            "tag": release["tag"],
            "notes": release["notes"],
            "download_url": release["download_url"],
            "sha256": release["sha256"],
            "published_at": release["published_at"],
            "error": None,
        }

    # Both channels can only update from their own update package
    # (.app.zip for the bundled app, .source.zip for the source channel).
    # dmg-only releases ship no update package; do not offer a download that
    # would fail to install.
    if not release.get("has_update_package"):
        return {
            "available": False,
            "current": current,
            "latest": release["version"],
            "tag": release["tag"],
            "notes": release["notes"],
            "download_url": "",
            "sha256": None,
            "published_at": release["published_at"],
            "error": "A newer version is available on GitHub Releases; in-app "
                     "updates are not enabled for this release channel.",
        }
    available = is_newer(release["version"], current)
    expected_sha = str(release.get("sha256") or "").strip().lower()
    if available and not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        return {
            "available": False,
            "current": current,
            "latest": release["version"],
            "tag": release["tag"],
            "notes": release["notes"],
            "download_url": "",
            "sha256": None,
            "published_at": release["published_at"],
            "error": "The update release does not provide a valid SHA-256 checksum; "
                     "automatic installation has been disabled for safety.",
        }
    return {
        "available": available,
        "current": current,
        "latest": release["version"],
        "tag": release["tag"],
        "notes": release["notes"],
        "download_url": release["download_url"],
        "sha256": expected_sha or None,
        "published_at": release["published_at"],
        "error": None,
    }


# ---------------------------------------------------------------------------
# Download & verify
# ---------------------------------------------------------------------------

def download_file(url: str, dest: Path, progress_callback=None) -> Path:
    """Download url to dest (atomically via a .part file)."""
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": GITHUB_UA})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_callback:
                    progress_callback(done, total)
    tmp.replace(dest)
    return dest


def sha256_of(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path, expected: str) -> bool:
    expected = str(expected or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    return hmac.compare_digest(sha256_of(path).lower(), expected)


# ---------------------------------------------------------------------------
# Applying (runs in a detached process)
# ---------------------------------------------------------------------------

def _backup_and_replace(new_root: Path, log) -> None:
    project = _PROJECT_DIR
    backup_dir = BACKUPS_DIR / f"backup_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    for item in new_root.iterdir():
        name = item.name
        if name in KEEP_TOP:
            continue
        target = project / name
        if target.exists():
            if target.is_dir():
                shutil.copytree(target, backup_dir / name)
            else:
                shutil.copy2(target, backup_dir / name)
    log(f"Backed up to {backup_dir}")

    for item in new_root.iterdir():
        name = item.name
        if name in KEEP_TOP:
            continue
        target = project / name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def apply_update(version: str, zip_path: Path, log) -> dict:
    """Install a new version.

    Frozen (self-contained .app): the update package is a zip of the whole
    Astro Digest.app bundle - replace the running bundle and
    relaunch.  Source mode keeps the historical behavior (replace source
    files, rebuild the .app).
    """
    if getattr(sys, "frozen", False):
        return _apply_frozen_bundle(version, zip_path, log)
    return _apply_source(version, zip_path, log)


def _source_package_root(extract_root: Path) -> Path:
    """Locate app sources in flat archives and current or historical wrappers."""
    roots = [extract_root]
    entries = [path for path in extract_root.iterdir() if path.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        roots.append(entries[0])
    candidates = list(roots)
    for root in roots:
        candidates.extend(root / name for name in ("AstroDigest", "AstroPaperDigest"))
    valid = {path for path in candidates
             if (path / "main.py").is_file()
             and (path / "src").is_dir()
             and (path / "build_app.sh").is_file()}
    if len(valid) != 1:
        raise UpdateApplyError("Invalid update package: a unique project root was not found.")
    return valid.pop()


def _apply_source(version: str, zip_path: Path, log) -> dict:
    """Source channel: extract zip, backup old code, replace, rebuild .app."""
    log(f"Installing v{version} ...")
    if not zip_path.exists():
        raise UpdateApplyError(f"Update package not found: {zip_path}")

    with tempfile.TemporaryDirectory(prefix="astrodigest-update-") as td:
        extract_root = Path(td)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_root)
        _backup_and_replace(_source_package_root(extract_root), log)

    log(f"Code replaced with v{version}")

    # Rebuild the .app bundle from the new source.
    log("Rebuilding Astro Digest.app ...")
    res = subprocess.run(
        ["bash", "build_app.sh"],
        cwd=str(_PROJECT_DIR),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if res.returncode != 0:
        log("Rebuilding .app failed (you can run ./build_app.sh manually later)")
        tail = (res.stdout or "")[-1500:] + (res.stderr or "")[-1500:]
        if tail.strip():
            log("Output snippet: " + tail.strip()[-1500:])
    else:
        log("Rebuilt .app")

    # Relaunch the app (macOS).
    relaunched = False
    if sys.platform == "darwin":
        app_path = next((path for name in (DISPLAY_APP_BUNDLE_NAME, *LEGACY_APP_BUNDLE_NAMES)
                         if (path := _PROJECT_DIR.parent / name).exists()), None)
        if app_path is not None:
            time.sleep(3)  # let the old process exit and release its resources
            subprocess.Popen(["open", str(app_path)])
            log(f"Restarted {app_path.name}")
            relaunched = True
    if not relaunched:
        log("Please restart the app manually after installation.")
    return {"ok": True}


def _apply_frozen_bundle(version: str, zip_path: Path, log) -> dict:
    """Frozen channel: replace the running .app bundle with the update zip."""
    log(f"Installing v{version} (whole-bundle replace) ...")
    if not zip_path.exists():
        raise UpdateApplyError(f"Update package not found: {zip_path}")

    # The updater process itself runs from inside the bundle we replace:
    #   <App>.app/Contents/Frameworks/astrodigest-cli
    exe = Path(sys.executable).resolve()
    app_bundle = exe.parent.parent.parent
    if app_bundle.suffix != ".app" or not (app_bundle / "Contents" / "Info.plist").exists():
        raise UpdateApplyError(f"Could not locate the app bundle (resolved exe: {exe})")

    # ditto (not zipfile) so symlinks inside the .app survive extraction.
    with tempfile.TemporaryDirectory(prefix="astrodigest-update-") as td:
        root = Path(td)
        res = subprocess.run(
            ["ditto", "-x", "-k", str(zip_path), str(root)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise UpdateApplyError(f"Failed to extract update package: {(res.stderr or '')[-500:]}")
        entries = [p for p in root.iterdir() if p.name != "__MACOSX"]
        if len(entries) != 1 or not entries[0].is_dir():
            raise UpdateApplyError("Invalid update package: expected one .app folder.")
        new_app = entries[0]
        if new_app.suffix != ".app" or not (new_app / "Contents" / "Info.plist").exists():
            raise UpdateApplyError("Invalid update package: not a .app bundle.")
        install_bundle = app_bundle
        renamed_bundle = app_bundle.with_name(DISPLAY_APP_BUNDLE_NAME)
        if app_bundle.name in LEGACY_APP_BUNDLE_NAMES and not renamed_bundle.exists():
            install_bundle = renamed_bundle
        if new_app.name != install_bundle.name:
            new_app = new_app.rename(root / install_bundle.name)

        backup = app_bundle.with_name(
            f"{app_bundle.name}.old-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        log(f"Updating {app_bundle} to {install_bundle}")
        shutil.move(str(app_bundle), str(backup))
        try:
            shutil.move(str(new_app), str(install_bundle))
        except Exception:
            # A cross-volume move can leave a partial copied bundle behind.
            if install_bundle.exists():
                shutil.rmtree(install_bundle)
            shutil.move(str(backup), str(app_bundle))  # roll back
            raise

    # Record the installed version in the data dir (the bundle carries none).
    try:
        (_PROJECT_DIR / "version.txt").write_text(f"{version}\n", encoding="utf-8")
    except OSError:
        pass

    # Prefer the renamed setting while honoring existing test/dev environments.
    no_relaunch = os.environ.get("ASTRODIGEST_UPDATE_NO_RELAUNCH",
                                os.environ.get("APD_UPDATE_NO_RELAUNCH"))
    if no_relaunch != "1":
        subprocess.Popen(["open", str(install_bundle)])
        log(f"Restarted {install_bundle.name}")

    # Best-effort, detached cleanup of the old bundle.
    subprocess.Popen(
        ["rm", "-rf", str(backup)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True}


def main_apply(marker_path: Path) -> int:
    log_path = UPDATES_DIR / "apply.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    time.sleep(3)  # give the Flask server time to shut down

    # Defensively stop the server if it is still alive.
    pid = marker.get("server_pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
            time.sleep(1)
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass

    try:
        result = apply_update(
            marker.get("version", ""),
            Path(marker.get("zip_path", "")),
            log,
        )
        log("Update complete.")
        result["log"] = "See output/updates/apply.log"
        try:
            marker_path.unlink()
        except OSError:
            pass
        return 0
    except Exception as e:
        log(f"Update failed: {e}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="AstroDigest updater")
    parser.add_argument("--check", metavar="REPO", help="Check GitHub repo for updates")
    parser.add_argument("--apply", metavar="MARKER", help="Apply a pending update (marker JSON path)")
    args = parser.parse_args()

    if args.apply:
        return main_apply(Path(args.apply))

    if args.check:
        try:
            result = check_update(args.check)
        except UpdateCheckError as e:
            print(f"ERROR: {e}")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
