"""Single source of truth for where AstroPaperDigest stores user data.

Two modes:

- Frozen (PyInstaller .app shipped via dmg): the bundle itself is read-only
  once installed (e.g. /Applications), so all writable data lives in
  ~/Library/Application Support/AstroPaperDigest (the same directory the
  GUI already used for its single-instance lock / run-info).
- Source (dev runs, Install.command channel, tests): data stays in the
  repository root, exactly as it always did.

Every module should call data_dir() instead of deriving the project
directory from __file__.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# In frozen (PyInstaller) builds the interpreter's baked-in default CA bundle
# points at the build machine and does not exist here, so stdlib urllib fails
# with CERTIFICATE_VERIFY_FAILED.  Point it at the certifi bundle that ships
# inside the app (harmless in source mode too).
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass

_APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "AstroPaperDigest"


def app_support_dir() -> Path:
    """Allow packaged smoke tests to use an isolated, explicit data directory.

    Normal launches keep the existing location. Never change HOME to isolate
    tests: Cocoa and other macOS services still need the real user home.
    """
    override = os.environ.get("APD_DATA_DIR")
    if override:
        data = Path(override)
        if not data.is_absolute():
            raise ValueError("APD_DATA_DIR must be an absolute path")
        return data
    return _APP_SUPPORT_DIR


def data_dir() -> Path:
    """Return the writable data directory, creating it if needed."""
    if getattr(sys, "frozen", False):
        data = app_support_dir()
    else:
        data = Path(__file__).resolve().parent.parent
    data.mkdir(parents=True, exist_ok=True)
    return data
