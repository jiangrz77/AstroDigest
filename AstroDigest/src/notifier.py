"""Email notifications for daily Astro Digest recommendations."""

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
from .email_addresses import parse_recipients, parse_sender
from .reason_highlight import reason_highlighter, reason_keywords


APP_SCHEME = "astrodigest"
# Match the score badges and recommendation callouts in gui.py.
_STAR_COLORS = {5: "#f4b400", 4: "#27ae60", 3: "#2563eb", 2: "#8b5cf6", 1: "#95a5a6"}
_REASON_PALETTES = {
    5: ("#e8a33d", "#fdf6e7", "#b45309"),  # border, background, emphasized text
    4: ("#27ae60", "#eefaf2", "#1e7e34"),
}
EMAIL_STATE_PATH = _paths.data_dir() / "output" / "email" / "notification_state.json"
# The legacy filename keeps running-app discovery compatible across upgrades.
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
        try:
            datetime.strptime(str(date_str), "%Y-%m-%d")
            return str(date_str)
        except ValueError:
            pass
    return datetime.now().date().isoformat()


def _digest_subject(date_str: str, paper_count: int) -> str:
    """Keep the digest date, its weekday, and the complete paper count together."""
    weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[
        datetime.strptime(date_str, "%Y-%m-%d").weekday()
    ]
    unit = "paper" if paper_count == 1 else "papers"
    return f"Astro Digest | {date_str} {weekday} | {paper_count} {unit}"


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
        "Thread-Topic": "Astro Digest Daily",
        "X-AstroDigest-Thread": "daily-digest",
        "X-AstroDigest-Date": date_str,
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
    password_env = email_config.get("password_env", "EMAIL_APP_PASSWORD")
    password = os.environ.get(password_env)

    if not sender or not recipient or not password:
        print(f"  Email config incomplete. Need sender, recipient, and {password_env} env var.")
        return False

    try:
        sender = parse_sender(sender)
        recipients = parse_recipients(recipient)
        if not recipients:
            raise ValueError("At least one recipient is required.")
    except ValueError as exc:
        print(f"  Invalid email configuration: {exc}")
        return False

    login_user = os.environ.get("SMTP_USERNAME") or email_config.get("username") or sender
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
    message["To"] = ", ".join(recipients)
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
                refused = server.sendmail(sender, recipients, message.as_string())
        else:
            with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
                server.starttls(context=tls_context)
                server.login(login_user, password)
                refused = server.sendmail(sender, recipients, message.as_string())
        if refused:
            print(f"  Email delivery incomplete; rejected recipients: {', '.join(refused)}")
            return False
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


def _notification_score(paper: dict) -> int:
    if paper.get("scoring_failed"):
        return 0
    try:
        score = int(paper.get("score", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return score if 1 <= score <= 5 else 0


def _notification_papers(papers: list[dict], score: int = 5) -> list[dict]:
    return [
        paper for paper in papers
        if _notification_score(paper) == score
    ]


def _notification_groups(papers: list[dict]) -> list[tuple[str, list[dict]]]:
    return [
        ("5-Star Recommendations", _notification_papers(papers, 5)),
        ("4-Star Recommendations", _notification_papers(papers, 4)),
        ("Other Papers", sorted(
            (paper for paper in papers if _notification_score(paper) < 4),
            key=_notification_score, reverse=True,
        )),
    ]


def _rating_text(paper: dict) -> str:
    score = _notification_score(paper)
    return "★" * score + "☆" * (5 - score) if score else "Not scored"


def _notification_fingerprint(date_str: str, papers: list[dict]) -> str:
    selected = []
    for _label, group in _notification_groups(papers):
        for paper in group:
            item = {
                "id": _paper_id(paper),
                "title": paper.get("title", ""),
                "score": _notification_score(paper),
                "authors": _authors(paper),
                "categories": _categories(paper),
                "link": _paper_link(paper),
            }
            if _notification_score(paper) >= 4:
                item.update({
                    "reason": paper.get("reason", ""),
                    "abstract": paper.get("abstract", ""),
                })
            selected.append(item)
    payload = json.dumps({"date": date_str, "papers": selected}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text_body(date_str: str, papers: list[dict], links: dict) -> str:
    groups = _notification_groups(papers)
    lines = [
        "Astro Digest Daily Digest",
        f"Date: {date_str}",
        f"Papers today: {len(papers)}",
        f"5-star recommendations: {len(groups[0][1])}",
        f"4-star recommendations: {len(groups[1][1])}",
        f"Other papers: {len(groups[2][1])}",
        "",
    ]
    index = 0
    for label, group in groups:
        if not group:
            continue
        lines.extend([label, "=" * len(label)])
        for paper in group:
            index += 1
            lines.extend([
                f"{index}. {paper.get('title', '').strip()}",
                f"Rating: {_rating_text(paper)}",
                f"Categories: {_categories(paper) or '—'}",
                f"Authors: {_authors(paper) or '—'}",
            ])
            if _notification_score(paper) >= 4:
                lines.extend([
                    paper.get('reason', '').strip() or '—',
                    f"Abstract: {(paper.get('abstract') or '').strip() or '—'}",
                ])
            lines.extend([f"arXiv: {_paper_link(paper)}", ""])
    if not papers:
        lines.extend(["There are no papers in this daily digest.", ""])
    # Always use the registered macOS URL scheme for the App link.  A
    # loopback HTTP URL would open in a browser, while the custom scheme can
    # launch the App or hand the date to its already-running instance.
    lines.append("Open Astro Digest to view the complete daily digest:")
    lines.append(links["launch"])
    return "\n".join(lines)


def _email_frame(rows: str, preview: str) -> str:
    """Fluid, single-column email with inline styles as the client fallback."""
    # Tables and inline defaults keep the message readable when a mail client
    # strips the stylesheet. Clients that ignore max-width still get a fluid
    # layout, so a narrow reading pane is never forced to a desktop width.
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='x-apple-disable-message-reformatting'>"
        "<title>Astro Digest</title>"
        "<style>"
        "@media screen and (max-width:600px){"
        ".email-gutter{padding:8px 4px!important}"
        ".email-section{padding:12px!important}"
        ".email-summary{padding:10px 12px!important}"
        ".email-summary-title{font-size:17px!important}"
        ".email-heading{font-size:20px!important}"
        ".email-paper-title{font-size:18px!important}"
        ".email-copy{font-size:15px!important;line-height:1.55!important}"
        ".email-paper-link{padding:12px 0!important}"
        "}"
        "</style></head>"
        "<body style='margin:0;padding:0;background-color:#f1f5f9;"
        "-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%'>"
        "<div style='display:none;font-size:1px;line-height:1px;color:#f1f5f9;"
        "max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all'>"
        f"{html.escape(preview)}</div>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' border='0' "
        "style='width:100%;border-collapse:collapse;table-layout:fixed;background-color:#f1f5f9;"
        "mso-table-lspace:0pt;mso-table-rspace:0pt'>"
        "<tr><td class='email-gutter' align='center' style='padding:16px 8px'>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' border='0' "
        "align='center' style='width:100%;max-width:800px;table-layout:fixed;"
        "border-collapse:collapse;background-color:#ffffff;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;"
        "color:#334155;font-size:14px;line-height:1.55;"
        "word-wrap:break-word;overflow-wrap:anywhere;"
        "mso-table-lspace:0pt;mso-table-rspace:0pt'>"
        f"{rows}</table>"
        "</td></tr></table></body></html>"
    )


def _html_paper_row(paper: dict, index: int, keywords: list[str]) -> str:
    score = _notification_score(paper)
    detailed = score >= 4
    title = render_math_html(str(paper.get("title", "")))
    authors = render_math_html(_authors(paper) or "—")
    categories = render_math_html(_categories(paper) or "—")
    link = html.escape(_paper_link(paper), quote=True)
    rating = _rating_text(paper)
    rating_label = f"{score} out of 5 stars" if score else "Not scored"
    rating_color = _STAR_COLORS.get(score, "#64748b")
    if score:
        rating = "★" * score
        if score < 5:
            rating += f"<span style='color:#d7dde5'>{'☆' * (5 - score)}</span>"
    section_class = "email-section" if detailed else "email-summary"
    title_class = "email-paper-title" if detailed else "email-summary-title"
    padding = "14px 20px" if detailed else "10px 20px"
    title_size = 19 if detailed else 16
    detail = ""
    if detailed:
        # Only recommended papers render reasons and abstracts. Besides keeping
        # the overview short, this avoids adding unused MathML to large emails.
        reason = render_math_html(
            str(paper.get("reason") or "—"), render_text=reason_highlighter(keywords),
        )
        abstract = render_math_html(str(paper.get("abstract") or "—")).replace("\n", "<br>")
        accent, background, emphasis = _REASON_PALETTES[score]
        reason = reason.replace(
            "<b>", f"<b style='color:{emphasis};font-weight:600'>",
        )
        detail = (
            "<div class='email-copy' style='margin:8px 0;padding:8px 10px;"
            f"border-left:3px solid {accent};background-color:{background};color:#3f4a5a;"
            "font-size:14px;line-height:1.55;overflow-x:auto'>"
            f"{reason}</div>"
            "<div class='email-copy' style='margin:0;font-size:14px;line-height:1.55;overflow-x:auto'>"
            f"<b>Abstract:</b> {abstract}</div>"
        )
    # Each block contains its own overflow so long formulas cannot widen the
    # entire message on a narrow screen. All tiers share the same reading order.
    return (
        f"<tr><td class='{section_class}' style='padding:{padding};border-top:1px solid #dbe4f0'>"
        f"<h3 class='{title_class}' style='margin:0 0 4px;color:#0f172a;"
        f"font-size:{title_size}px;font-weight:650;line-height:1.35;overflow-x:auto'>"
        f"<span style='color:#64748b'>{index}.</span> {title}</h3>"
        "<p style='margin:0;font-size:12px;line-height:1.5;color:#475569;overflow-x:auto'>"
        f"<span aria-label='{rating_label}' style='color:{rating_color}'>{rating}</span>"
        f" &nbsp;·&nbsp; <b>Categories:</b> {categories}<br><b>Authors:</b> {authors}</p>"
        f"{detail}"
        "<p style='margin:4px 0 0;font-size:13px;line-height:20px'>"
        f"<a class='email-paper-link' href='{link}' style='display:inline-block;padding:4px 0;"
        "color:#1d4ed8;text-decoration:underline'>View paper on arXiv</a></p>"
        "</td></tr>"
    )


def _html_body(date_str: str, papers: list[dict], links: dict) -> str:
    groups = _notification_groups(papers)
    five_count, four_count, other_count = (len(group) for _label, group in groups)
    keywords = reason_keywords() if five_count or four_count else []
    # Keep the App CTA on the custom URL scheme so Mail launches Astro Digest.
    launch_url = links.get("launch")
    rows = [
        "<tr><td class='email-section' style='padding:16px 20px;"
        "border-top:3px solid #2563eb'>"
        "<h1 class='email-heading' style='margin:0 0 6px;color:#0f172a;"
        "font-size:22px;font-weight:700;line-height:1.3'>Astro Digest Daily Digest</h1>"
        "<p style='margin:0;color:#475569;font-size:13px;line-height:1.6'>"
        f"<span style='display:inline-block;padding-right:12px'>{html.escape(date_str)}</span>"
        f"<span style='display:inline-block'>Papers today: <b>{len(papers)}</b></span></p>"
        "<p style='margin:2px 0 0;color:#475569;font-size:13px;line-height:1.6'>"
        f"<span style='display:inline-block;padding-right:12px'>5-star recommendations: <b>{five_count}</b></span>"
        f"<span style='display:inline-block;padding-right:12px'>4-star recommendations: <b>{four_count}</b></span>"
        f"<span style='display:inline-block'>Other papers: <b>{other_count}</b></span>"
        "</p></td></tr>"
    ]
    index = 0
    for (label, group), heading_color in zip(groups, ("#d97706", "#27ae60", "#64748b")):
        if not group:
            continue
        rows.append(
            f"<tr><td class='email-section' style='padding:10px 20px;border-top:2px solid {heading_color};"
            "background-color:#f8fafc'>"
            f"<h2 style='margin:0;color:{heading_color};font-size:15px;line-height:1.4'>"
            f"{label} <span style='color:#64748b;font-weight:400'>· {len(group)}</span></h2>"
            "</td></tr>"
        )
        for paper in group:
            index += 1
            rows.append(_html_paper_row(paper, index, keywords))
    if not papers:
        rows.append(
            "<tr><td class='email-section' style='padding:14px 20px;border-top:1px solid #dbe4f0'>"
            "<p class='email-copy' style='margin:0;font-size:14px;line-height:1.55'>"
            "There are no papers in this daily digest.</p></td></tr>"
        )
    rows.append(
        "<tr><td class='email-section' style='padding:14px 20px;border-top:1px solid #dbe4f0'>"
        f"<a href='{html.escape(launch_url, quote=True)}' "
        "style='display:inline-block;padding:11px 16px;background-color:#2563eb;color:#ffffff;"
        "font-size:14px;font-weight:600;line-height:22px;border-radius:6px;"
        "text-decoration:none;mso-padding-alt:11px 16px'>Open Astro Digest</a>"
        "<p style='margin:6px 0 0;color:#64748b;font-size:12px;line-height:1.5'>"
        "Complete daily digest · macOS app</p></td></tr>"
    )
    return _email_frame(
        "".join(rows),
        f"{date_str} · {five_count} five-star · {four_count} four-star recommendations · {other_count} other papers",
    )


def send_digest_notification(
    papers: list[dict],
    email_config: dict,
    date_str: str | None = None,
    *,
    force: bool = False,
    state_path=None,
) -> bool:
    """Send 4–5 star recommendations in full and metadata for all other papers."""
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

    sender = str(email_config.get("sender", "astrodigest"))
    domain = sender.split("@")[-1] or "localhost"
    message_id = make_msgid(domain=domain)
    headers = _message_headers(state, message_id, date_str)
    links = app_links(date_str)
    plain = _text_body(date_str, papers, links)
    rich = _html_body(date_str, papers, links)
    subject = _digest_subject(date_str, len(papers))
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
        "five_star_count": len(_notification_papers(papers, 5)),
        "four_star_count": len(_notification_papers(papers, 4)),
        "other_paper_count": sum(_notification_score(paper) < 4 for paper in papers),
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
        "Astro Digest email configuration test succeeded.\n\n"
        f"Test time: {now}\n"
        "After the next successful daily digest update, emails will be sent according to your settings."
    )
    rich = _email_frame(
        "<tr><td class='email-section' style='padding:16px 20px;border-top:3px solid #2563eb'>"
        "<h1 class='email-heading' style='margin:0 0 8px;color:#0f172a;"
        "font-size:22px;line-height:1.3'>Astro Digest</h1>"
        "<p class='email-copy' style='margin:0 0 6px;font-size:14px;line-height:1.55'>"
        "<b>Astro Digest email configuration test succeeded.</b></p>"
        f"<p style='margin:0;color:#475569;font-size:13px;line-height:1.5'>Test time: {html.escape(now)}</p>"
        "<p class='email-copy' style='margin:8px 0 0;font-size:14px;line-height:1.55'>"
        "After the next successful daily digest update, emails will be sent according to your settings."
        "</p></td></tr>",
        "Astro Digest email configuration test succeeded.",
    )
    return send_email("Astro Digest Email Configuration Test", body, email_config, html_body=rich)


def send_digest_email(digest_content: str, email_config: dict, date_str: str = None) -> bool:
    """Backward-compatible legacy sender for an arbitrary digest body."""
    date_str = _normalise_date(date_str)
    return send_email(f"Astro Digest - {date_str}", digest_content, email_config)
