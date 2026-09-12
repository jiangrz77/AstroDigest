"""Shared recommendation-reason keywords and safe emphasis for the app and email."""

from __future__ import annotations

import html
import re
from collections.abc import Callable

import yaml

from . import paths as _paths
from .preference_learning import load_learned_profile


# Generic astrophysics glossary merged with the user's interest keywords;
# matching is longest-first, so "galaxy cluster" wins over "galaxy".
_ASTRO_GLOSSARY = [
    "milky way", "galaxy cluster", "galaxy formation", "galaxy evolution",
    "globular cluster", "gravitational wave", "active galactic nucleus",
    "supermassive black hole", "black hole", "neutron star", "white dwarf",
    "dark matter", "dark energy", "cosmic web", "large-scale structure",
    "interstellar medium", "molecular cloud", "star formation", "stellar halo",
    "stellar evolution", "supernovae", "supernova", "kilonova", "pulsar",
    "magnetar", "quasar", "redshift", "reionization", "exoplanet",
    "protoplanetary disk", "circumstellar disk", "asteroseismology",
    "spectroscopy", "photometry", "nucleosynthesis", "metallicity",
    "tidal disruption event", "gamma-ray burst", "fast radio burst",
    "cosmic microwave background", "galaxies", "galaxy",
]
MAX_REASON_HIGHLIGHTS = 3
_LEARNED_KEYWORD_BOOST = 1.2
_LEARNED_KEYWORD_LIMIT = 20


def reason_keywords(cfg: dict | None = None) -> list[str]:
    """Keywords to bold in reasons: user interests + learned boosts + glossary.

    Penalized learned terms (weight < 1) are deliberately excluded: bolding
    something the user downvoted would emphasize the wrong thing.
    """
    if cfg is None:
        try:
            with open(_paths.data_dir() / "config.yaml", encoding="utf-8") as source:
                cfg = yaml.safe_load(source)
        except (OSError, yaml.YAMLError):
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
    keywords: list[str] = []
    seen: set[str] = set()

    def add(term):
        term = (term or "").strip()
        if len(term) < 3:
            return
        key = term.lower()
        if key not in seen:
            seen.add(key)
            keywords.append(term)

    for term in cfg.get("keywords") or []:
        if isinstance(term, str):
            add(term)
    try:
        learned = load_learned_profile()
        weights = learned.get("keyword_weights") or {}
        boosted = sorted(
            (
                (term, weight)
                for term, weight in weights.items()
                if isinstance(weight, (int, float)) and weight >= _LEARNED_KEYWORD_BOOST
            ),
            key=lambda item: -item[1],
        )[:_LEARNED_KEYWORD_LIMIT]
        for term, _weight in boosted:
            add(term)
    except Exception:
        pass
    for term in _ASTRO_GLOSSARY:
        add(term)
    return sorted(keywords, key=len, reverse=True)


def reason_highlighter(keywords: list[str]) -> Callable[[str], str]:
    """Return an escaping text renderer with one emphasis budget across chunks.

    Email math passes only prose chunks to this renderer, leaving entire TeX
    expressions intact while keeping the highlight limit per recommendation.
    """
    ordered = sorted((k for k in keywords if k), key=len, reverse=True)
    if not ordered:
        return html.escape
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(k) for k in ordered) + r")(?!\w)",
        re.IGNORECASE,
    )
    hits = 0

    def render(text: str) -> str:
        nonlocal hits
        parts: list[str] = []
        last = 0
        for match in pattern.finditer(text):
            if hits >= MAX_REASON_HIGHLIGHTS:
                break
            parts.append(html.escape(text[last:match.start()]))
            parts.append("<b>" + html.escape(match.group(0)) + "</b>")
            last = match.end()
            hits += 1
        parts.append(html.escape(text[last:]))
        return "".join(parts)

    return render


def highlight_reason_text(text: str, keywords: list[str]) -> str:
    """HTML-escape a reason and bold up to MAX_REASON_HIGHLIGHTS keywords."""
    return reason_highlighter(keywords)(text or "")
