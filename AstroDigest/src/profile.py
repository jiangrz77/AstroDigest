"""Build a research-interest profile from BibTeX, keywords, or Zotero."""

import re
from collections import Counter
from typing import Optional

import bibtexparser

from .technical_terms import (
    are_equivalent_terms,
    canonicalize_term,
    filter_technical_term_records,
    technical_term_records_to_json,
)
from .zotero import read_zotero_library


def parse_bib(bib_path: str) -> list[dict]:
    """Parse a BibTeX file and return a list of entry dicts."""
    with open(bib_path, "r", encoding="utf-8") as f:
        bib_db = bibtexparser.loads(f.read())
    return bib_db.entries


def extract_categories(entries: list[dict]) -> Counter:
    """Count arxiv primary categories across all entries."""
    cats = Counter()
    for e in entries:
        pc = e.get("primaryclass", "")
        if pc:
            cats[pc] += 1
        # Also parse the keywords field for category info
        kw = e.get("keywords") or ""
        for part in kw.split(","):
            part = part.strip()
            if "Astrophysics -" in part:
                cats[part] += 1
    return cats


def _raw_keyword_values(entries: list[dict]) -> list[str]:
    """Collect raw metadata tags before the technical-term gate."""
    values = []
    for e in entries:
        kw = e.get("keywords") or ""
        for part in kw.split(","):
            part = part.strip()
            # Skip arxiv category labels
            if part.startswith("Astrophysics -") or part.startswith("Tag:") or part.startswith("FOS:"):
                continue
            if part and len(part) > 2:
                values.append(part)
    return values


def _keywords_from_term_result(raw_values: list[str], result: dict) -> Counter:
    """Keep legacy Counter output while counting only accepted terms."""
    accepted = {
        record.get("canonical_term"): record
        for record in result.get("technical_terms", [])
        if record.get("canonical_term")
    }
    keywords = Counter()
    for raw in raw_values:
        record = accepted.get(canonicalize_term(raw))
        if record is None:
            record = next(
                (
                    candidate for candidate in accepted.values()
                    if are_equivalent_terms(raw, candidate.get("canonical_term", ""))
                ),
                None,
            )
        if record:
            # The display form is stable and preserves useful forms such as
            # ``Population III`` and ``Gaia DR3`` for the existing UI/prompt.
            keywords[record["term"]] += 1
    return keywords


def extract_technical_term_records(entries: list[dict], extra_terms: Optional[list[str]] = None) -> dict:
    """Classify BibTeX/Zotero tags under the technical-term contract.

    Metadata tags are explicit user/library vocabulary, so accepted records
    are marked ``learning_candidate=False``.  New terms discovered from taste
    feedback use the same classifier with a different known-term set.
    """
    if isinstance(extra_terms, str):
        extra_terms = [extra_terms]
    raw_values = _raw_keyword_values(entries) + [
        str(term).strip() for term in (extra_terms or []) if str(term).strip()
    ]
    return filter_technical_term_records(raw_values, known_terms=raw_values)


def extract_keywords(entries: list[dict]) -> Counter:
    """Extract and count accepted domain/technical terms.

    The function name remains for API compatibility; generic academic words
    and broad domain words no longer enter the returned Counter.
    """
    raw_values = _raw_keyword_values(entries)
    result = filter_technical_term_records(raw_values, known_terms=raw_values)
    return _keywords_from_term_result(raw_values, result)


def extract_recent_titles(entries: list[dict], year_threshold: int = 2025) -> list[str]:
    """Extract titles from recent papers (used as examples for the LLM)."""
    recent = []
    for e in entries:
        year_str = e.get("year", "0")
        try:
            year = int(year_str)
        except ValueError:
            year = 0
        if year >= year_threshold:
            title = (e.get("title") or "").replace("{", "").replace("}", "").replace("\n", " ")
            recent.append(title)
    return recent


def extract_topic_phrases(entries: list[dict], year_threshold: int = 2025) -> Counter:
    """Extract common topic phrases from recent paper titles and abstracts."""
    # Common astrophysics topic phrases to look for
    topic_patterns = [
        r"chemical enrichment", r"metal.poor", r"globular cluster",
        r"star formation", r"supernov?a", r"[Pp]opulation III",
        r"Milky Way", r"stellar abundan", r"initial mass function",
        r"Hubble tension", r"pair.instability", r"core.collapse",
        r"red giant", r"stellar evolution", r"circumgalactic",
        r"galaxy formation", r"high.redshift", r"gravitational lens",
        r"JWST", r"Type Ia", r"nucleosynthesis", r"metallicity",
        r"stellar population", r"dwarf galaxy", r"galactic halo",
        r"white dwarf", r"neutron star", r"black hole",
        r"dark matter", r"cosmic ray", r"magnetic field",
        r"AGB", r"planetary nebula", r"supernova remnant",
        # Spectroscopy & stellar atmospheres
        r"APOGEE", r"LAMOST", r"Gaia", r"spectroscop",
        r"spectral line", r"line parameter", r"atomic data",
        r"molecular data", r"stellar atmospher", r"M dwarf",
        r"abundance analysis", r"photometr", r"survey",
        # Stellar physics
        r"massive star", r"red supergiant", r"binary",
        r"r.process", r"s.process", r"carbon.enhanced",
        r"extremely metal", r"reionization",
    ]
    
    phrases = Counter()
    for e in entries:
        year_str = e.get("year", "0")
        try:
            year = int(year_str)
        except ValueError:
            year = 0
        
        weight = 2 if year >= year_threshold else 1
        text = (e.get("title", "") + " " + e.get("abstract", "")).lower()
        
        for pattern in topic_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                phrases[pattern] += weight
    
    return phrases


def build_profile(bib_path: str) -> dict:
    """Build a complete interest profile from the BibTeX collection.
    
    Returns a dict with:
      - categories: Counter of arxiv categories
      - keywords: Counter of keywords
      - topic_phrases: Counter of topic phrases from recent papers
      - recent_titles: list of recent paper titles (as examples for LLM)
      - all_entries: all parsed entries
    """
    entries = parse_bib(bib_path)
    return _build_profile_from_entries(entries)


def _build_profile_from_entries(entries: list[dict], collections: Optional[list[str]] = None) -> dict:
    """Build the shared profile shape from normalized bibliography entries."""
    raw_keywords = _raw_keyword_values(entries)
    if collections:
        raw_keywords.extend(
            str(collection).strip() for collection in collections if str(collection).strip()
        )
    technical_result = filter_technical_term_records(
        raw_keywords,
        known_terms=raw_keywords,
    )
    wire_terms = technical_term_records_to_json(technical_result["technical_terms"])["terms"]
    wire_learning = technical_term_records_to_json(technical_result["learning_candidates"])["terms"]
    profile = {
        "categories": extract_categories(entries),
        "keywords": _keywords_from_term_result(raw_keywords, technical_result),
        "topic_phrases": extract_topic_phrases(entries),
        "recent_titles": extract_recent_titles(entries),
        "all_entries": entries,
        "technical_terms": wire_terms,
        "learning_candidates": wire_learning,
    }
    return profile


def build_profile_from_zotero(zotero_db: str = "") -> dict:
    """Read Zotero and convert its items into the existing profile shape."""
    library = read_zotero_library(zotero_db or None)
    profile = _build_profile_from_entries(
        library["entries"],
        collections=library.get("collections", []),
    )
    profile["source"] = "zotero"
    profile["zotero_path"] = library["database_path"]
    profile["zotero_summary"] = {
        "item_count": library["item_count"],
        "deleted_count": library["deleted_count"],
        "tag_count": library["tag_count"],
        "collection_count": library["collection_count"],
    }
    return profile


def build_profile_from_config(config: dict) -> dict:
    """Build an interest profile from config keywords and categories (no bib file needed).

    Used as fallback when no BibTeX collection is available.
    """
    categories = Counter()
    for cat in config.get("arxiv_categories", []):
        categories[cat] = 1

    raw_keywords = [
        str(kw).strip() for kw in config.get("keywords", []) if str(kw).strip()
    ]
    technical_result = filter_technical_term_records(
        raw_keywords,
        # Configured interests are known/user-supplied terms, not terms to
        # learn as novel vocabulary from feedback.
        known_terms=raw_keywords,
    )
    wire_terms = technical_term_records_to_json(technical_result["technical_terms"])["terms"]
    wire_learning = technical_term_records_to_json(technical_result["learning_candidates"])["terms"]
    keywords = _keywords_from_term_result(raw_keywords, technical_result)

    return {
        "categories": categories,
        "keywords": keywords,
        "topic_phrases": Counter(),
        "recent_titles": [],
        "all_entries": [],
        "technical_terms": wire_terms,
        "learning_candidates": wire_learning,
    }


def profile_to_prompt_text(profile: dict, max_recent: int = 10) -> str:
    """Format the interest profile into a text block suitable for an LLM prompt."""
    lines = []
    
    # Top categories
    lines.append("=== Research Interest Profile ===")
    lines.append("\nTop arxiv categories of interest:")
    for cat, count in profile["categories"].most_common(10):
        lines.append(f"  - {cat} ({count} papers)")
    
    # Keep the old heading so prompt/API consumers remain compatible, while
    # making the narrower semantics explicit.
    lines.append("\nTop keywords (domain/technical terms only):")
    for kw, count in profile["keywords"].most_common(20):
        lines.append(f"  - {kw} ({count})")

    technical_terms = profile.get("technical_terms") or []
    if technical_terms:
        lines.append("\nTechnical-term taxonomy:")
        for record in technical_terms[:20]:
            term = record.get("term") or record.get("surface_form", "")
            taxonomy = record.get("taxonomy") or record.get("category", "")
            if term and taxonomy:
                lines.append(f"  - {term} [{taxonomy}]")
    
    # Top topic phrases
    lines.append("\nKey research topics (from recent papers):")
    for phrase, score in profile["topic_phrases"].most_common(15):
        lines.append(f"  - {phrase}")
    
    # Recent paper examples
    lines.append(f"\nExamples of recent papers of interest (up to {max_recent}):")
    for title in profile["recent_titles"][:max_recent]:
        lines.append(f"  - {title}")

    zotero_summary = profile.get("zotero_summary")
    if zotero_summary:
        lines.append(
            "\nZotero library: "
            f"{zotero_summary['item_count']} items, "
            f"{zotero_summary['tag_count']} tags, "
            f"{zotero_summary['collection_count']} collections"
        )
    
    return "\n".join(lines)
