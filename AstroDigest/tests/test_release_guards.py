"""Reject mismatched release inputs before invoking the expensive build."""
import os
import hashlib
import json
import zipfile
from pathlib import Path
import shutil
import subprocess
import pytest

@pytest.mark.parametrize('case,expected', [
    ('dirty', 'commit tracked changes'),
    ('head', 'HEAD must match'),
    ('trigger', 'triggering tag differs'),
])
def test_release_rejects_mismatched_inputs(tmp_path, case, expected):
    app = tmp_path / 'AstroDigest'
    app.mkdir()
    shutil.copy2(Path(__file__).parents[1] / 'release.sh', app / 'release.sh')
    (app / 'version.txt').write_text('1.0.0')
    (app / 'build_dmg.sh').write_text('#!/bin/sh\necho UNEXPECTED_BUILD\nexit 87\n')
    (app / 'build_dmg.sh').chmod(0o755)
    def git(*args):
        subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True)
    git('init')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'fixture')
    git('tag', 'v1.0.0')
    env = dict(os.environ, GITHUB_REF_NAME='v1.0.0')
    if case == 'dirty':
        (app / 'version.txt').write_text('1.0.0\n')
    elif case == 'head':
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-m', 'later')
    else:
        env['GITHUB_REF_NAME'] = 'v0.9.0'
    result = subprocess.run(['bash', str(app / 'release.sh')], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert expected in result.stdout + result.stderr
    assert 'UNEXPECTED_BUILD' not in result.stdout


def test_release_produces_matching_legacy_aliases_and_scoped_source_zip(tmp_path):
    """Exercise release packaging with a cheap builder, including both old channels."""
    app = tmp_path / "AstroDigest"
    app.mkdir()
    shutil.copy2(Path(__file__).parents[1] / "release.sh", app / "release.sh")
    (app / "version.txt").write_text("1.0.0")
    (app / "build_dmg.sh").write_text(
        '#!/bin/sh\nset -eu\n'
        'test "$ASTRODIGEST_RELEASE_BUILD" = 1\n'
        'mkdir -p dist\n'
        'printf app-package > dist/AstroDigest-v1.0.0.app.zip\n'
        'printf disk-image > dist/AstroDigest-1.0.0.dmg\n'
    )
    (app / "build_dmg.sh").chmod(0o755)
    (tmp_path / "outside-project.txt").write_text("not part of the source update")
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fixture")
    git("tag", "v1.0.0")
    result = subprocess.run(
        ["bash", str(app / "release.sh")],
        env=dict(os.environ, GITHUB_REF_NAME="v1.0.0"),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    dist = app / "dist"
    for channel in ("app", "source"):
        assert (dist / f"AstroDigest-v1.0.0.{channel}.zip").read_bytes() == (
            dist / f"AstroPaperDigest-v1.0.0.{channel}.zip"
        ).read_bytes()
    with zipfile.ZipFile(dist / "AstroDigest-v1.0.0.source.zip") as archive:
        assert "AstroDigest/version.txt" in archive.namelist()
        assert all(name.startswith("AstroDigest/") for name in archive.namelist())
        assert not any("outside-project.txt" in name for name in archive.namelist())
    manifest = json.loads((dist / "version.json").read_text())
    app_sha = hashlib.sha256((dist / "AstroDigest-v1.0.0.app.zip").read_bytes()).hexdigest()
    assert manifest["sha256"] == app_sha
    assert manifest["url"] == (
        "https://github.com/jiangrz77/AstroDigest/releases/download/v1.0.0/"
        "AstroDigest-v1.0.0.app.zip"
    )
    checksums = (dist / "SHA256SUMS").read_text().splitlines()
    assert len(checksums) == 5
    for line in checksums:
        digest, filename = line.split()
        assert digest == hashlib.sha256((app / filename).read_bytes()).hexdigest()
