"""Email notifications for daily AstroPaperDigest recommendations."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import format_datetime, make_msgid
from pathlib import Path
from urllib.parse import quote

from . import paths as _paths
from .email_math import render_math_html


APP_SCHEME = "astropaperdigest"
EMAIL_STATE_PATH = _paths.data_dir() / "output" / "email" / "notification_state.json"
RUN_INFO_PATH = _paths.app_support_dir() / "apd-run.json"


def _env_or_config(env_name: str, email_config: dict, key: str, default=""):
    """Read GUI-written environment values before config.yaml fallbacks."""
    value = os.environ.get(env_name)
    if value is not None and value.strip():
        return value.strip()
    return email_config.get(key, default)


def _parse_port(value, default: int = 587) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _parse_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("1", "true", "yes", "on", "ssl"):
            return True
        if value in ("0", "false", "no", "off", "starttls"):
            return False
    return default


def _normalise_date(date_str: str | None) -> str:
    if date_str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date_str)):
        return str(date_str)
    return datetime.now().date().isoformat()


def app_deep_link(date_str: str) -> str:
    """Return a macOS custom URL that launches/focuses the app for a date."""
    return f"{APP_SCHEME}://digest/{quote(_normalise_date(date_str), safe='-')}"


def running_app_url(date_str: str) -> str:
    """Return the current loopback URL when the desktop app is running.

    This is an optimisation only. The custom URL scheme remains the reliable
    fallback because a notification can be sent before the GUI writes its
    dynamic port information.
    """
    try:
        with open(RUN_INFO_PATH, "r", encoding="utf-8") as f:
            info = json.load(f)
        port = int(info.get("port", 0))
        pid = int(info.get("pid", 0))
        if port <= 0 or pid <= 0:
            return ""
        os.kill(pid, 0)
        return f"http://127.0.0.1:{port}/digest/{_normalise_date(date_str)}"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ""


def app_links(date_str: str) -> dict:
    """Return both the running-app URL and the launch-capable deep link."""
    return {
        "running": running_app_url(date_str),
        "launch": app_deep_link(date_str),
    }


def _state_path(path=None) -> Path:
    return Path(path) if path else EMAIL_STATE_PATH


def load_email_state(path=None) -> dict:
    state_path = _state_path(path)
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_email_state(state: dict, path=None) -> None:
    state_path = _state_path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, state_path)


def _message_headers(state: dict, message_id: str, date_str: str) -> dict:
    """Build headers that help clients display daily messages as one series."""
    headers = {
        "Message-ID": message_id,
        "Date": format_datetime(datetime.now(timezone.utc)),
        "Thread-Topic": "AstroPaper Daily",
        "X-AstroPaperDigest-Thread": "daily-digest",
        "X-AstroPaperDigest-Date": date_str,
    }
    previous = str(state.get("last_message_id") or "").strip()
    raw_references = state.get("references", [])
    if not isinstance(raw_references, list):
        raw_references = []
    references = [str(v).strip() for v in raw_references if str(v).strip()]
    if previous:
        headers["In-Reply-To"] = previous
        references.append(previous)
    if references:
        headers["References"] = " ".join(list(dict.fromkeys(references))[-20:])
    return headers


def send_email(
    subject: str,
    body: str,
    email_config: dict,
    *,
    html_body: str | None = None,
    headers: dict | None = None,
) -> bool:
    """Send a plain-text or HTML/alternative email using configured SMTP."""
    if not email_config.get("enabled", False):
        print("  Email notification disabled.")
        return False

    sender = _env_or_config("EMAIL_SENDER", email_config, "sender")
    recipient = _env_or_config("EMAIL_RECIPIENT", email_config, "recipient")
    smtp_server = _env_or_config("SMTP_SERVER", email_config, "smtp_server", "smtp.gmail.com")
    login_user = os.environ.get("SMTP_USERNAME") or email_config.get("username") or sender
    password_env = email_config.get("password_env", "EMAIL_APP_PASSWORD")
    password = os.environ.get(password_env)

    if not sender or not recipient or not password:
        print(f"  Email config incomplete. Need sender, recipient, and {password_env} env var.")
        return False

    use_ssl = _parse_bool(
        os.environ.get("SMTP_USE_SSL"),
        _parse_bool(email_config.get("use_ssl"), True),
    )
    default_port = 465 if use_ssl else 587
    smtp_port = _parse_port(
        os.environ.get("SMTP_PORT"),
        _parse_port(email_config.get("smtp_port", default_port), default_port),
    )

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    for key, value in (headers or {}).items():
        if value:
            message[key] = str(value)
    message.attach(MIMEText(body, "plain", "utf-8"))
    if html_body:
        message.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        tls_context = ssl.create_default_context()
        if use_ssl:
            with smtplib.SMTP_SSL(
                smtp_server,
                smtp_port,
                timeout=30,
                context=tls_context,
            ) as server:
                server.login(login_user, password)
                server.sendmail(sender, recipient, message.as_string())
        else:
            with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
                server.starttls(context=tls_context)
                server.login(login_user, password)
                server.sendmail(sender, recipient, message.as_string())
        print(f"  Email sent to {recipient}")
        return True
    except Exception as exc:
        print(f"  Failed to send email: {exc}")
        return False


def _paper_id(paper: dict) -> str:
    return str(paper.get("id") or paper.get("paper_id") or "").strip()


def _paper_link(paper: dict) -> str:
    return str(paper.get("pdf_url") or paper.get("link") or f"https://arxiv.org/abs/{_paper_id(paper)}")


def _authors(paper: dict) -> str:
    authors = paper.get("authors", "")
    if isinstance(authors, str):
        return authors
    return ", ".join(str(author) for author in authors)


def _categories(paper: dict) -> str:
    categories = paper.get("categories", "")
    if isinstance(categories, str):
        return categories
    return ", ".join(str(category) for category in categories)


def _notification_papers(papers: list[dict]) -> list[dict]:
    return [
        paper for paper in papers
        if not paper.get("scoring_failed") and int(paper.get("score", 0) or 0) == 5
    ]


def _notification_fingerprint(date_str: str, papers: list[dict]) -> str:
    selected = []
    for paper in _notification_papers(papers):
        selected.append({
            "id": _paper_id(paper),
            "title": paper.get("title", ""),
            "reason": paper.get("reason", ""),
            "abstract": paper.get("abstract", ""),
        })
    payload = json.dumps({"date": date_str, "papers": selected}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text_body(date_str: str, papers: list[dict], links: dict) -> str:
    stars = _notification_papers(papers)
    lines = [
        "AstroPaperDigest Daily Digest",
        f"Date: {date_str}",
        f"Papers today: {len(papers)}",
        f"5-star recommendations: {len(stars)}",
        "",
    ]
    if stars:
        lines.extend(["5-Star Recommendations", "========================"])
        for index, paper in enumerate(stars, 1):
            lines.extend([
                f"{index}. {paper.get('title', '').strip()}",
                "Rating: ★★★★★",
                f"Why recommended: {paper.get('reason', '').strip() or '—'}",
                f"Authors: {_authors(paper) or '—'}",
                f"Categories: {_categories(paper) or '—'}",
                f"Abstract: {(paper.get('abstract') or '').strip() or '—'}",
                f"arXiv: {_paper_link(paper)}",
                "",
            ])
    else:
        lines.extend(["There are no 5-star recommendations today.", ""])
    # Always use the registered macOS URL scheme for the App link.  A
    # loopback HTTP URL would open in a browser, while the custom scheme can
    # launch the App or hand the date to its already-running instance.
    lines.append("Open AstroPaperDigest to view the complete daily digest:")
    lines.append(links["launch"])
    return "\n".join(lines)


def _html_body(date_str: str, papers: list[dict], links: dict) -> str:
    stars = _notification_papers(papers)
    # Keep the App CTA on the custom URL scheme so Mail launches AstroPaperDigest
    # instead of opening the local web server in a browser.
    launch_url = links.get("launch")
    cards = []
    for paper in stars:
        title = render_math_html(str(paper.get("title", "")))
        abstract = render_math_html(str(paper.get("abstract") or "—"))
        reason = render_math_html(str(paper.get("reason") or "—"))
        authors = render_math_html(_authors(paper) or "—")
        categories = render_math_html(_categories(paper) or "—")
        abstract = abstract.replace("\n", "<br>")
        link = html.escape(_paper_link(paper), quote=True)
        cards.append(
            "<article style='margin:20px 0;padding:20px 22px;border:1px solid #dbe4f0;"
            "border-radius:12px;background:#f8fbff;box-sizing:border-box'>"
            f"<h3 style='margin:0 0 10px;color:#1f2937;line-height:1.4'>{title}</h3>"
            "<div style='color:#d97706;font-size:18px;letter-spacing:1px'>★★★★★</div>"
            f"<p style='margin:18px 0 12px'><b>Why recommended:</b> {reason}</p>"
            f"<p style='margin:12px 0'><b>Authors:</b> {authors}<br><b>Categories:</b> {categories}</p>"
            f"<p style='margin:16px 0 0'><b>Abstract:</b><br>{abstract}</p>"
            f"<p><a href='{link}'>View paper on arXiv</a></p>"
            "</article>"
        )
    if not cards:
        cards.append("<p>There are no 5-star recommendations today.</p>")
    button = (
        f"<p style='margin-top:24px'><a href='{html.escape(launch_url, quote=True)}' "
        "style='display:inline-block;padding:10px 16px;background:#2563eb;color:#fff;"
        "border-radius:7px;text-decoration:none'>Open AstroPaperDigest</a></p>"
    )
    rich = (
        "<!doctype html><html><body style='font-family:-apple-system,BlinkMacSystemFont,"
        "Segoe UI,Arial,sans-serif;color:#334155;font-size:15px;line-height:1.7;"
        "margin:0;background:#ffffff'>"
        "<h2 style='color:#1f2937'>AstroPaperDigest Daily Digest</h2>"
        f"<p><b>Date:</b> {html.escape(date_str)}<br>"
        f"<b>Papers today:</b> {len(papers)}<br>"
        f"<b>5-star recommendations:</b> {len(stars)}</p>"
        + "".join(cards) + button + "</body></html>"
    )
    return rich


def send_digest_notification(
    papers: list[dict],
    email_config: dict,
    date_str: str | None = None,
    *,
    force: bool = False,
    state_path=None,
) -> bool:
    """Send one daily notification, including full abstracts for 5-star papers."""
    date_str = _normalise_date(date_str)
    state = load_email_state(state_path)
    fingerprint = _notification_fingerprint(date_str, papers)
    sent_dates = state.get("sent_dates")
    if not isinstance(sent_dates, dict):
        sent_dates = {}
        state["sent_dates"] = sent_dates
    if sent_dates.get(date_str) and not force:
        print(f"  Email for {date_str} already sent; skipping duplicate notification.")
        return True

    sender = str(email_config.get("sender", "astro-paper-digest"))
    domain = sender.split("@")[-1] or "localhost"
    message_id = make_msgid(domain=domain)
    headers = _message_headers(state, message_id, date_str)
    links = app_links(date_str)
    plain = _text_body(date_str, papers, links)
    rich = _html_body(date_str, papers, links)
    subject = f"【AstroPaper Daily】{date_str}"
    ok = send_email(
        subject,
        plain,
        email_config,
        html_body=rich,
        headers=headers,
    )
    if not ok:
        return False

    sent_dates[date_str] = {
        "fingerprint": fingerprint,
        "message_id": message_id,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "five_star_count": len(_notification_papers(papers)),
    }
    state["last_message_id"] = message_id
    raw_references = state.get("references", [])
    if not isinstance(raw_references, list):
        raw_references = []
    references = [str(v).strip() for v in raw_references if str(v).strip()]
    references.append(message_id)
    state["references"] = list(dict.fromkeys(references))[-20:]
    _save_email_state(state, state_path)
    return True


def _papers_from_digest_file(digest_path: str | os.PathLike) -> list[dict]:
    """Load parsed digest papers and replace snippets with full sidecar abstracts."""
    from .digest_parser import parse_digest

    path = Path(digest_path)
    parsed = parse_digest(str(path))
    full_path = path.with_name(f"{path.stem}.full.json")
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            full = json.load(f)
    except (OSError, ValueError):
        full = {}
    papers = []
    for tier in parsed.get("tiers", []):
        for paper in tier.get("papers", []):
            item = dict(paper)
            item["id"] = item.get("paper_id", "")
            item["abstract"] = full.get(item["id"], item.get("abstract", ""))
            papers.append(item)
    return papers


def send_digest_file(
    digest_path: str | os.PathLike,
    email_config: dict,
    *,
    force: bool = False,
    state_path=None,
) -> bool:
    path = Path(digest_path)
    date_match = re.search(r"digest_(\d{4}-\d{2}-\d{2})\.md$", path.name)
    date_str = date_match.group(1) if date_match else None
    return send_digest_notification(
        _papers_from_digest_file(path), email_config, date_str,
        force=force, state_path=state_path,
    )


def send_test_email(email_config: dict) -> bool:
    """Send a short configuration test without touching daily-send state."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        "AstroPaperDigest email configuration test succeeded.\n\n"
        f"Test time: {now}\n"
        "After the next successful daily digest update, emails will be sent according to your settings."
    )
    rich = (
        "<p><b>AstroPaperDigest email configuration test succeeded.</b></p>"
        f"<p>Test time: {html.escape(now)}</p>"
    )
    return send_email("AstroPaperDigest Email Configuration Test", body, email_config, html_body=rich)


def send_digest_email(digest_content: str, email_config: dict, date_str: str = None) -> bool:
    """Backward-compatible legacy sender for an arbitrary digest body."""
    date_str = _normalise_date(date_str)
    return send_email(f"AstroPaperDigest - {date_str}", digest_content, email_config)
