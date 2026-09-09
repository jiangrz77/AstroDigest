# User guide

[Home](../README.md) · [中文](usage_zh.md)

## Email notifications

Use **Sender Email** for the single mailbox that authenticates with SMTP. In **Recipients**, enter one or more addresses separated by commas, semicolons, or spaces; leave it blank to receive mail at the sender address.

Enable email in **Settings → Email Notification** and provide your SMTP settings. After a successful daily digest update, AstroPaperDigest sends one email for that date. The message includes every 5-star paper with its title, score, reason, authors, categories, arXiv link, and full abstract; lower-rated papers remain available in the App. A message is still sent when there are no 5-star papers.

Daily sends are deduplicated by date. Use **Resend Latest Digest** in Settings or run `send_digests.py --resend YYYY-MM-DD` for an explicit resend. Messages include both HTML and plain-text parts and threading headers to help mailbox clients display the updates as one series. The App link uses the `astropaperdigest://` macOS URL scheme so it can launch the App and open the requested date when the App is not already running.

## Installation

### Option A: dmg installer (no Python required)

1. Download **`AstroPaperDigest-<version>.dmg`** from GitHub Releases
2. Double-click it and drag **AstroPaperDigest.app** into **Applications**
3. Launch it — a native desktop window with the setup wizard opens

> First launch of a downloaded app may ask for confirmation once (right-click → **Open**, or **System Settings → Privacy & Security → Open Anyway**). Only needed once.

### Option B: source / Install.command (requires Python 3.9+)

> **Note:** Do not run from `~/Downloads/` — macOS blocks downloaded files. Move the project to a permanent location first (e.g., `~/Projects/`).

1. Double-click **`Install.command`** — sets up Python environment and builds the app
2. Double-click **`AstroPaperDigest.app`** — a native desktop window opens automatically

## Zotero research profile

In **Settings → Research Profile**, select **Use Zotero Library**. The app first tries the default database location:

```text
~/Zotero/zotero.sqlite
```

When you save the profile setting, AstroPaperDigest immediately reads a private copy in read-only mode and validates the library. If the default location cannot be read, the Settings page explains the error and asks for the real `zotero.sqlite` path. For custom data locations, check Zotero **Settings → Advanced → Files and Folders**.

After a custom path succeeds, it is saved for later runs. The same setting can be written directly in `config.yaml`:

```yaml
profile_source: zotero
zotero_db: ~/Zotero/zotero.sqlite
```

Missing, unreadable, invalid, or incompatible databases are reported explicitly; the app does not silently switch to a keyword or BibTeX profile.

## Auto Update

- **When checked**: once in the background at app startup (silent on network failure), plus a manual "Check for Updates" button on the **Settings (⛭) → General → Update** page.
- **Notification**: a blue banner appears at the top of the Digest page when a new version is found; the Settings page shows current/latest version and release notes in the Update panel.
- **Install flow** (semi-automatic): click "Download Update" on the Settings page → SHA-256 verification after download → click "Install & Restart" → old code is backed up, sources replaced, the .app is rebuilt, and the app relaunches.
- **Update source**: GitHub Releases (public repo). Check endpoint: `https://api.github.com/repos/jiangrz77/AstroPaperDigest/releases/latest`.
- **Version**: single source of truth in `version.txt` (read by `build_app.sh` when building the .app).
- **Preserved files**: updates never touch `.env`, `config.yaml`, `preferences.json`, `feedback.json`, `data/`, `output/`, `.venv`; old code is backed up to `backups/`.
