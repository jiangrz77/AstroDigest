#!/usr/bin/env python3
"""Send existing digest files by email using the configured SMTP settings.

Use this on a machine that can reach your SMTP server (e.g. inside the campus
network) when the automated email step was skipped or failed.

Usage:
    python send_digests.py [--resend] [YYYY-MM-DD ...]

Without arguments, the latest digest in output/digests is sent.  With dates,
each matching digest_YYYY-MM-DD.md is sent. Email settings come from
config.yaml (email section) and the .env file (EMAIL_APP_PASSWORD). Daily
sends are deduplicated by date unless --resend is provided.
"""

import os
import sys
from pathlib import Path

from src import paths as _paths
_PROJECT_DIR = _paths.data_dir()
os.chdir(_PROJECT_DIR)
sys.path.insert(0, str(_PROJECT_DIR))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src.notifier import send_digest_file  # noqa: E402


def main() -> int:
    load_dotenv(_PROJECT_DIR / ".env", interpolate=False)
    with open(_PROJECT_DIR / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    email_config = config.get("email", {})

    force = "--resend" in sys.argv[1:]
    dates = [arg for arg in sys.argv[1:] if arg != "--resend"]
    if not dates:
        digest_dir = _PROJECT_DIR / "output" / "digests"
        files = sorted(digest_dir.glob("digest_*.md"))
        if not files:
            print("No digest files found under output/digests/.")
            return 1
        dates = [f.stem.replace("digest_", "") for f in files[-1:]]

    ok = True
    for d in dates:
        path = _PROJECT_DIR / "output" / "digests" / f"digest_{d}.md"
        if not path.exists():
            print(f"  {d}: digest file not found ({path.name})")
            ok = False
            continue
        sent = send_digest_file(path, email_config, force=force)
        print(f"  {d}: sent={sent}")
        ok = ok and sent
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
