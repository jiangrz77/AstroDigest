"""Fetch paper figures for the desktop digest gallery.

The desktop digest shows, for 4-5 star papers, the paper's figures next to
the recommendation reason: one main thumbnail in the card and a lightbox
gallery (up to ``MAX_FIGURES`` images) on click.  Figures are fetched once
per paper and cached as ``output/figures/<sanitized-id>[-<n>].<ext>`` (the
first figure keeps the extensionless-suffix legacy name so caches from
single-figure versions stay valid); a per-digest sidecar
(``digest_<date>.figures.json``) records which papers have figures, how
many, and which were tried without success, so a paper without a usable
figure is never retried on every view.

Sources, in order:

1. arXiv's official HTML rendering (``https://arxiv.org/html/<id>``,
   LaTeXML output): figures in document order, as the authors laid them
   out.  Only newer LaTeX submissions have an HTML version.
2. The PDF: the qualifying images (>= 120x120 px) in reading order, largest
   first within each page (PyMuPDF).  Covers everything else.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

try:
    import fitz  # PyMuPDF, used only for the PDF fallback
except ImportError:  # pragma: no cover - optional dependency at runtime
    fitz = None

# Papers below this final score get no figures.
MIN_SCORE = 4

# Gallery depth: figures fetched per paper — ALL of the paper's figures,
# bounded as a sanity cap (arXiv papers rarely exceed ~20 figures; the bound
# also keeps a pathological PDF extraction from running away).  Recorded
# sidecar entries carry the depth they were fetched with; raising this value
# makes existing entries fetch only their missing figures.
MAX_FIGURES = 24

# Sidecar schema version for captions: entries below it get refreshed (the
# HTML page is refetched for caption text; cached images are not redownloaded).
CAPTION_VERSION = 1

# Parser version: bumped when figure parsing gains a new capability, so
# papers previously misrecorded as figure-less get one retry.  v2 added
# <object>-embedded SVG figures; v3 distinguishes unavailable sources from
# successfully inspected papers with no usable figures.
PARSER_VERSION = 3

PAGE_TIMEOUT = (10, 30)
IMAGE_TIMEOUT = (10, 60)
PDF_TIMEOUT = (10, 180)
# arXiv asks automated clients to stay around one request per second.
REQUEST_INTERVAL = 1.2
# Within one paper, figure images are downloaded several at a time: they are
# static assets behind arXiv's CDN, and serial downloads made gallery
# completion the slowest part of figure fetching once galleries grew to a
# paper's full figure set.  Pacing between papers is unaffected, and a 429 is
# still retried politely per request.
IMAGE_DOWNLOAD_WORKERS = 4
MAX_HTML_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_IMAGE_BYTES = 4 * 1024 * 1024
# Ignore PDF images smaller than a 120x120 block: logos, separators, inline
# glyphs.  They are never "the paper's figures".
MIN_PIXEL_AREA = 120 * 120

USER_AGENT = "AstroPaperDigest/2.3 (arXiv daily digest desktop app)"

FIGURE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

_CONTENT_TYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}

_FIGURE_BLOCK_RE = re.compile(r"<figure\b[^>]*>(.*?)</figure>", re.IGNORECASE | re.DOTALL)
_FIGCAPTION_RE = re.compile(r"<figcaption\b[^>]*>(.*?)</figcaption>", re.IGNORECASE | re.DOTALL)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# Newer LaTeXML renderings embed SVG figures as <object data="..."> instead
# of <img src="...">; both carry class ltx_graphics.
_OBJECT_TAG_RE = re.compile(r"<object\b[^>]*>", re.IGNORECASE)
_DATA_ATTR_RE = re.compile(r"\bdata=[\"']([^\"']+)[\"']", re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r"\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
_CLASS_ATTR_RE = re.compile(r"\bclass=[\"']([^\"']*)[\"']", re.IGNORECASE)
# MathML renderings embed the raw TeX in <annotation>; it is noise in a
# caption, as are scripts and styles.
_NOISE_BLOCK_RE = re.compile(r"<(script|style|annotation)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_paper_id(paper_id: str) -> str:
    """Make an arXiv id safe as a filename (old ids contain '/')."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", paper_id)


def _figure_names(base: str, index: int) -> list[str]:
    """Candidate filenames for a paper's n-th figure (1st keeps legacy name)."""
    suffix = "" if index <= 1 else f"-{index}"
    return [base + suffix + ext for ext in FIGURE_EXTS]


def figure_path(figures_dir: str, paper_id: str, index: int = 1):
    """Return the cached path of a paper's n-th figure, or None."""
    base = sanitize_paper_id(paper_id)
    for name in _figure_names(base, index):
        candidate = os.path.join(figures_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def cached_figure_files(figures_dir: str, paper_id: str, max_figures: int = MAX_FIGURES,
                         extra_dir: str | None = None) -> list[str]:
    """Basenames of the paper's cached figures, consecutive from index 1."""
    files = []
    for index in range(1, max_figures + 1):
        path = figure_path(figures_dir, paper_id, index)
        if path is None and extra_dir:
            path = figure_path(extra_dir, paper_id, index)
        if path is None:
            break
        files.append(os.path.basename(path))
    return files


def delete_paper_figures(figures_dir: str, paper_id: str,
                         max_figures: int = MAX_FIGURES) -> list[str]:
    """Remove a paper's cached figure files; returns the deleted names.

    Used by figure retention: a paper downgraded out of the 4-5 star range
    keeps its gallery for a grace period, then the cache is cleaned.
    """
    base = sanitize_paper_id(paper_id)
    deleted = []
    for index in range(1, max_figures + 1):
        for name in _figure_names(base, index):
            path = os.path.join(figures_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                os.remove(path)
                deleted.append(name)
            except OSError:
                pass  # locked or already gone; the next sweep retries
    return deleted


def _caption_text(fragment: str) -> str:
    """Plain-text caption from a figcaption's inner HTML (MathML stripped)."""
    text = _NOISE_BLOCK_RE.sub(" ", fragment)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600]


def _graphic_src(tag: str):
    """Figure resource URL from an <img src=...> or <object data=...> tag."""
    if tag.lower().startswith("<object"):
        match = _DATA_ATTR_RE.search(tag)
    else:
        match = _SRC_ATTR_RE.search(tag)
    return match.group(1).strip() if match else None


def _is_ltx_graphic(tag: str) -> bool:
    classes = _CLASS_ATTR_RE.search(tag)
    return bool(classes and "ltx_graphics" in classes.group(1))


def parse_figure_items(html_text: str, limit: int | None = None) -> list[tuple[str, str]]:
    """(src, caption) pairs from an arXiv HTML page, in document order.

    LaTeXML renders figures as <figure> blocks whose graphics are <img>
    tags or <object data=...> elements (class ltx_graphics) with
    page-relative srcs such as "x1.png"; a block may contain several
    subpanel images, which all share the block's <figcaption>.  Falls back
    to standalone ltx_graphics elements (no caption) when no figure blocks
    carry images.  Site chrome icons live outside figures and are skipped.
    """
    items: list[tuple[str, str]] = []
    seen: set[str] = set()

    def collect(tag: str, caption: str) -> bool:
        src = _graphic_src(tag)
        if src and not src.startswith("data:") and src not in seen:
            seen.add(src)
            items.append((src, caption))
            return limit is not None and len(items) >= limit
        return False

    for block in _FIGURE_BLOCK_RE.findall(html_text):
        caption_match = _FIGCAPTION_RE.search(block)
        caption = _caption_text(caption_match.group(1)) if caption_match else ""
        for tag in _IMG_TAG_RE.findall(block):
            if collect(tag, caption):
                return items
        for tag in _OBJECT_TAG_RE.findall(block):
            if collect(tag, caption):
                return items
    if items:
        return items
    for tag in _IMG_TAG_RE.findall(html_text):
        if _is_ltx_graphic(tag):
            if collect(tag, ""):
                return items
    for tag in _OBJECT_TAG_RE.findall(html_text):
        if _is_ltx_graphic(tag):
            if collect(tag, ""):
                return items
    return items


def parse_figure_srcs(html_text: str, limit: int = 1) -> list[str]:
    """Back-compat src-only variant of parse_figure_items."""
    return [src for src, _caption in parse_figure_items(html_text, limit)]


def parse_first_figure_src(html: str):
    """Back-compat single-figure variant of parse_figure_srcs."""
    srcs = parse_figure_srcs(html, 1)
    return srcs[0] if srcs else None


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _get(session: requests.Session, url: str, timeout):
    """GET with one polite retry: arXiv burst-limits (429) heavy clients."""
    response = session.get(url, timeout=timeout)
    if response.status_code == 429:
        try:
            delay = min(float(response.headers.get("Retry-After", "5")), 15.0)
        except (TypeError, ValueError):
            delay = 5.0
        time.sleep(max(delay, 2.0))
        response = session.get(url, timeout=timeout)
    return response


def _candidate_image_urls(page_url: str, src: str) -> list[str]:
    """Possible absolute URLs for a figure src found on an arXiv HTML page.

    arXiv has used two layouts: older renderings reference images next to
    the page ("x1.png"), newer ones include the paper directory
    ("2609.01208v1/img2/plot.png").  There is no <base> tag to disambiguate,
    so try the plausible resolutions in order.
    """
    urls = []
    for candidate in (
        urljoin(page_url.rstrip("/") + "/", src),
        urljoin("https://arxiv.org/html/", src),
        urljoin(page_url, src),
    ):
        if candidate not in urls:
            urls.append(candidate)
    return urls


def _download_image(session: requests.Session, page_url: str, src: str):
    """Download one figure src, trying its candidate URLs. (bytes, ext) or None."""
    for image_url in _candidate_image_urls(page_url, src):
        try:
            image = _get(session, image_url, IMAGE_TIMEOUT)
        except requests.RequestException:
            continue
        if image.status_code != 200 or not image.content:
            continue
        content_type = (image.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and content_type not in _CONTENT_TYPE_EXT:
            continue  # an HTML error page, not an image
        ext = _ext_for(image_url, content_type)
        if ext is None or len(image.content) > MAX_HTML_IMAGE_BYTES:
            continue
        return image.content, ext
    return None


def _ext_for(url: str, content_type: str):
    if content_type in _CONTENT_TYPE_EXT:
        return _CONTENT_TYPE_EXT[content_type]
    path = urljoin("https://arxiv.org/", url).lower()
    for ext in FIGURE_EXTS:
        if path.rstrip("/").endswith(ext):
            return ext
    return None


def fetch_figure_html(paper_id: str, session: requests.Session,
                      max_figures: int = 1, skip: int = 0):
    """HTML-source figures.

    Returns (figures, captions): figures are the (bytes, ext) downloads for
    item indices >= skip (already-cached figures are not redownloaded);
    captions cover ALL items up to max_figures so a caption refresh never
    needs to re-download images.
    """
    page_url = f"https://arxiv.org/html/{paper_id}"
    response = _get(session, page_url, PAGE_TIMEOUT)
    if response.status_code == 404:
        return [], []
    if response.status_code != 200 or not response.text.strip().startswith("<"):
        raise requests.RequestException("HTML unavailable")
    items = parse_figure_items(response.text, max_figures)
    if not items:
        return [], []
    offsets = [offset for offset in range(len(items)) if offset >= skip]
    # pool.map keeps document order while downloading concurrently.
    with ThreadPoolExecutor(max_workers=IMAGE_DOWNLOAD_WORKERS) as pool:
        downloads = list(pool.map(
            lambda offset: _download_image(session, response.url, items[offset][0]),
            offsets,
        ))
    # Keep a consecutive prefix so retries cannot skip a failed image or
    # pair later images with the wrong captions.
    figures = []
    for downloaded in downloads:
        if downloaded is None:
            break
        figures.append(downloaded)
    if offsets and not figures:
        raise requests.RequestException("Figure download failed")
    captions = [caption for _src, caption in items]
    return figures, captions


def fetch_figure_pdf(paper_id: str, session: requests.Session,
                     max_figures: int = 1, skip: int = 0):
    """PDF-source figures (no captions exist for extracted images)."""
    if fitz is None:
        return [], []
    pdf_url = f"https://arxiv.org/pdf/{paper_id}"
    response = _get(session, pdf_url, PDF_TIMEOUT)
    if response.status_code != 200 or not response.content[:5] == b"%PDF-":
        raise requests.RequestException("PDF unavailable")
    figures = _pdf_figures(response.content, max_figures)
    return figures[max(0, skip):], []


def _pdf_figures(pdf_bytes: bytes, limit: int):
    """Qualifying images in reading order, largest first within each page."""
    results: list[tuple[bytes, str]] = []
    used_xrefs: set[int] = set()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            qualifying = []
            seen: set[int] = set()
            for image in page.get_images(full=True):
                xref, width, height = image[0], image[2], image[3]
                if xref in seen or xref in used_xrefs:
                    continue
                seen.add(xref)
                area = (width or 0) * (height or 0)
                if area < MIN_PIXEL_AREA:
                    continue
                qualifying.append((area, xref))
            for _area, xref in sorted(qualifying, key=lambda item: -item[0]):
                if len(results) >= limit:
                    return results
                try:
                    pixmap = fitz.Pixmap(doc, xref)
                    if pixmap.colorspace is None:
                        continue  # stencil mask, not a displayable figure
                    if pixmap.n - pixmap.alpha > 3:
                        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
                    data = pixmap.tobytes("png")
                    while len(data) > MAX_PDF_IMAGE_BYTES and pixmap.width > 400:
                        pixmap.shrink(1)
                        data = pixmap.tobytes("png")
                    used_xrefs.add(xref)
                    results.append((data, ".png"))
                except Exception:
                    continue  # unconvertible image; try the next one
        return results
    finally:
        doc.close()


def fetch_paper_figure(paper_id: str, session: requests.Session,
                       max_figures: int = 1, skip: int = 0) -> dict:
    """Try both sources.

    Returns {"figures": [(bytes, ext)], "captions": [str], "source": str};
    an empty figures list with a reason string ("no_figure", ...) means
    nothing was retrievable.
    """
    html_failed = False
    try:
        figures, captions = fetch_figure_html(paper_id, session, max_figures, skip)
        if figures or captions:
            return {"figures": figures, "captions": captions, "source": "html"}
    except (requests.RequestException, ValueError):
        html_failed = True
    if fitz is None:
        return {"figures": [], "captions": [], "source": "request_failed" if html_failed else "no_html_figure"}
    try:
        figures, _captions = fetch_figure_pdf(paper_id, session, max_figures, skip)
    except Exception:
        # An unavailable or malformed PDF is not evidence of a figure-less paper.
        return {"figures": [], "captions": [], "source": "request_failed"}
    if figures:
        return {"figures": figures, "captions": ["" for _ in figures], "source": "pdf"}
    return {"figures": [], "captions": [], "source": "request_failed" if html_failed else "no_figure"}


def sidecar_path(digest_dir: str, digest_date: str) -> str:
    return os.path.join(digest_dir, f"digest_{digest_date}.figures.json")


def load_sidecar(digest_dir: str, digest_date: str) -> dict:
    """Read the per-digest figure index, tolerant of missing/legacy files.

    Every paper entry is normalized to {"source", "files": [names], "depth"}
    so callers never see the pre-gallery single-file format.
    """
    path = sidecar_path(digest_dir, digest_date)
    raw: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, ValueError):
            raw = {}
    papers: dict[str, dict] = {}
    for pid, entry in (raw.get("papers") or {}).items():
        if not isinstance(entry, dict):
            continue
        files = entry.get("files")
        if not isinstance(files, list):
            first = entry.get("file")
            files = [first] if isinstance(first, str) and first else []
        files = [name for name in files if isinstance(name, str) and name]
        try:
            depth = int(entry.get("depth") or len(files) or 1)
        except (TypeError, ValueError):
            depth = len(files) or 1
        source = entry.get("source") if isinstance(entry.get("source"), str) else "html"
        captions = entry.get("captions")
        if not isinstance(captions, list):
            captions = []
        captions = [c if isinstance(c, str) else "" for c in captions]
        captions = (captions + [""] * len(files))[: len(files)]
        capv = entry.get("capv") if isinstance(entry.get("capv"), int) else 0
        papers[pid] = {
            "source": source,
            "files": files,
            "depth": depth,
            "captions": captions,
            "capv": capv,
        }
    failed = raw.get("failed") if isinstance(raw.get("failed"), dict) else {}
    pv = raw.get("pv") if isinstance(raw.get("pv"), int) else 1
    return {"papers": papers, "failed": failed, "pv": pv}


def write_sidecar(digest_dir: str, digest_date: str, data: dict) -> None:
    os.makedirs(digest_dir, exist_ok=True)
    path = sidecar_path(digest_dir, digest_date)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _write_figure(figures_dir: str, paper_id: str, data: bytes, ext: str, index: int = 1) -> str:
    os.makedirs(figures_dir, exist_ok=True)
    base = sanitize_paper_id(paper_id) + ("" if index <= 1 else f"-{index}")
    path = os.path.join(figures_dir, base + ext)
    temp_path = path + ".tmp"
    with open(temp_path, "wb") as f:
        f.write(data)
    os.replace(temp_path, path)
    return path


def _paper_key(paper: dict):
    return paper.get("id") or paper.get("paper_id")


def fetch_figures_for_digest(
    papers: list[dict],
    figures_dir: str,
    digest_dir: str | None = None,
    digest_date: str | None = None,
    min_score: int = MIN_SCORE,
    existing_dir: str | None = None,
    session: requests.Session | None = None,
    max_figures: int = MAX_FIGURES,
    on_progress=None,
) -> dict:
    """Fetch gallery figures for a digest's recommended (>= min_score) papers.

    Never raises: a figure problem must not fail digest generation.  Results
    are recorded incrementally in the sidecar so a cancelled run keeps its
    progress and successful papers are not fetched again.  Papers recorded
    at a lower gallery depth than ``max_figures`` are upgraded by fetching
    only their missing figures.  ``on_progress(position, total)`` is called
    (when given) once per paper that actually needs fetching, with the
    paper's 1-based position among the digest's figure targets.
    """
    session = session or new_session()
    sidecar = load_sidecar(digest_dir, digest_date) if digest_dir else {"papers": {}, "failed": {}, "pv": PARSER_VERSION}
    # A parser upgrade may recover papers misrecorded as figure-less: retry
    # those failures once per version (genuinely figure-less papers simply
    # fail again and stay recorded at the current version).
    if sidecar.get("pv", 1) < PARSER_VERSION:
        sidecar["failed"] = {
            pid: reason
            for pid, reason in sidecar.get("failed", {}).items()
            if reason not in ("no_figure", "no_html_figure")
        }
    sidecar["pv"] = PARSER_VERSION
    targets = [
        p for p in papers
        if not p.get("scoring_failed")
        and int(p.get("score") or 0) >= min_score
        and _paper_key(p)
    ]
    fetched = existing = failed_count = 0
    next_allowed = 0.0
    for position, paper in enumerate(targets, start=1):
        pid = _paper_key(paper)
        if pid in sidecar["failed"]:
            continue
        entry = sidecar["papers"].get(pid)
        depth = 0
        capv = 0
        if entry is not None:
            try:
                depth = int(entry.get("depth") or 1)
            except (TypeError, ValueError):
                depth = 1
            capv = entry.get("capv") or 0
        have = cached_figure_files(figures_dir, pid, max_figures, extra_dir=existing_dir)
        if depth >= max_figures and capv >= CAPTION_VERSION and have:
            continue  # cached files must still exist

        if on_progress is not None:
            on_progress(position, len(targets))
        # Stay near one request per second to arXiv across papers.
        delay = next_allowed - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        next_allowed = time.monotonic() + REQUEST_INTERVAL
        # skip=len(have): cached figures are not redownloaded; captions come
        # back for the whole range so a caption refresh is page-fetch only.
        result = fetch_paper_figure(pid, session, max_figures, skip=len(have))
        if result["source"] == "request_failed":
            time.sleep(REQUEST_INTERVAL)
            result = fetch_paper_figure(pid, session, max_figures, skip=len(have))
        files = list(have)
        for index, (data, ext) in enumerate(result["figures"], start=len(have) + 1):
            try:
                path = _write_figure(figures_dir, pid, data, ext, index=index)
                files.append(os.path.basename(path))
                fetched += 1
            except OSError:
                break
        incomplete = result["source"] == "request_failed"
        fetched_captions = [str(c or "") for c in (result.get("captions") or [])]
        if incomplete and entry:
            fetched_captions = entry.get("captions", [])
        captions = (fetched_captions + [""] * len(files))[: len(files)]
        if files:
            if len(files) == len(have) and have:
                existing += 1  # caption refresh, no new downloads
            sidecar["papers"][pid] = {
                "source": result["source"] if result["figures"] else (entry or {}).get("source", "html"),
                "files": files,
                "captions": captions,
                "depth": (min(len(files), max_figures - 1) if incomplete else
                          max_figures if len(files) >= len(fetched_captions) else len(files)),
                "capv": CAPTION_VERSION,
            }
        else:
            sidecar["papers"].pop(pid, None)
            sidecar["failed"][pid] = result["source"] if result["source"] not in ("html", "pdf") else "write_failed"
            failed_count += 1
        if digest_dir:
            write_sidecar(digest_dir, digest_date, sidecar)
    # Persist parser migration even when every image was already cached.
    if digest_dir:
        write_sidecar(digest_dir, digest_date, sidecar)
    if targets:
        print(
            f"  Figures: {fetched} fetched, {existing} cached, "
            f"{failed_count} without a usable figure"
        )
    return sidecar
