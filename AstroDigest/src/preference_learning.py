"""Learn user preferences from taste feedback (overrated / underrated).

This module turns the feedback.json history into a structured "learned profile"
that the ranking stage can apply deterministically:

  * keyword/topic weights      (multiplicative, clamped, time-decayed)
  * arXiv category weights     (same math)
  * global score calibration   (overall upward/downward bias)

The profile is consumed by ranker.py to (a) inject explicit numeric weights
into the LLM prompt and (b) apply a bounded deterministic score adjustment.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime, timezone

from src import paths as _paths
from src.technical_terms import (
    canonicalize_term,
    canonical_term_key,
    extract_technical_terms,
    filter_technical_term_records,
    is_hard_excluded,
    TERM_SCHEMA_VERSION,
    term_match_keys,
    technical_term_records_to_json,
    normalize_technical_terms,
    TechnicalTermValidationError,
)
_PROJECT_DIR = _paths.data_dir()

FEEDBACK_FILE = os.path.join(str(_PROJECT_DIR), "feedback.json")
LEARNED_PROFILE_FILE = os.path.join(_PROJECT_DIR, "learned_profile.json")

# --- Tuning constants -------------------------------------------------------
WEIGHT_MIN = 0.4
WEIGHT_MAX = 2.5
BOOST = 1.4            # factor applied for a +★ (formerly "underrated") hit
PENALTY = 0.7          # factor applied for a -★ (formerly "overrated") hit
HALF_LIFE_DAYS = 60.0  # feedback influence decays back to 1.0 over this half-life
CALIBRATION_PER = 0.1    # global star offset per (+★ - -★) event
CALIBRATION_MAX = 0.5
ADJUSTMENT_MAX = 1.0     # deterministic score adjustment clamp (stars)
KEYWORD_ADJUST_COEF = 0.4   # stars per (weight - 1.0) per matched keyword
CATEGORY_ADJUST_COEF = 0.75 # stars per (weight - 1.0) per matched category
MIN_FEEDBACK_FOR_TERM = 2   # a discovered term must appear in N distinct papers
MAX_LEARNED_TERMS = 100
TUNING_VERSION = 4       # full phrases and persisted, validated term records

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those",
    "are", "was", "were", "have", "has", "had", "not", "but", "its", "their",
    "into", "over", "than", "then", "them", "they", "will", "would", "can",
    "could", "should", "may", "might", "about", "after", "before", "between",
    "through", "during", "using", "based", "which", "while", "where", "when",
    "paper", "papers", "study", "studies", "result", "results", "method",
    "methods", "model", "models", "data", "analysis", "show", "shows", "found",
    "find", "present", "report", "new", "two", "one", "also", "well", "use",
    "used", "via", "per", "within", "without", "under", "above", "below",
    "however", "thus", "therefore", "herein", "et", "al",
}


# --- Persistence ------------------------------------------------------------

def load_feedback(path: str | None = None) -> list:
    """Read the feedback history (list of dicts)."""
    path = path or FEEDBACK_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_feedback(feedback: list, path: str | None = None) -> None:
    # Resolve the module global at call time so tests and packaged callers can
    # safely redirect persistence without being trapped by a default argument
    # evaluated during import.
    path = path or FEEDBACK_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(feedback, f, indent=2, ensure_ascii=False)


def load_learned_profile(path: str | None = None):
    """Return the persisted learned profile, or None if missing/corrupt.

    The path resolves at call time so tests can repoint the module global.
    """
    path = path or LEARNED_PROFILE_FILE
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def save_learned_profile(profile: dict, path: str | None = None) -> None:
    path = path or LEARNED_PROFILE_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)


def reset_learned_profile(scope: str = "all") -> dict:
    """Forget learned preferences, wholly or in one dimension.

    - "all": clear feedback history and remove the learned profile file
      (the previous full-reset behaviour).
    - "keywords" / "categories": drop that dimension only — learned weights
      and manual overrides/ignored entries for the dimension are removed
      while the other dimension and the raw feedback history stay intact.
      (A later manual rebuild from feedback may re-derive terms for the
      forgotten dimension; Reset All is what clears the history itself.)

    Returns the profile that should be shown next (a fresh derive for
    scoped resets).
    """
    scope = scope if scope in ("keywords", "categories") else "all"
    if scope == "all":
        save_feedback([])
        if os.path.exists(LEARNED_PROFILE_FILE):
            os.remove(LEARNED_PROFILE_FILE)
        return {}

    existing = load_learned_profile() or {}
    weights_key = {"keywords": "keyword_weights",
                   "categories": "category_weights"}[scope]
    existing[weights_key] = {}
    if scope == "keywords":
        # The technical-term inventory is the metadata companion of keyword
        # learning; a scoped keyword reset must not leave stale candidates in
        # the API response until the next rebuild.
        existing["technical_terms"] = []
        existing["learning_candidates"] = []
        existing["technical_term_records"] = []
    manual = dict(existing.get("manual") or {})
    manual[weights_key] = {}
    existing["manual"] = manual
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_learned_profile(existing)
    return existing


# --- Text helpers -----------------------------------------------------------

def matches_term(text: str, term: str) -> bool:
    """Case-insensitive whole-token/subphrase match."""
    raw_term = canonicalize_term(term)
    if not raw_term:
        return False
    normalized_text = canonicalize_term(text)
    variants = term_match_keys(raw_term)
    return any(
        re.search(r"(?<![a-z0-9])" + re.escape(variant) + r"(?![a-z0-9])", normalized_text)
        for variant in variants
        if variant
    )


def extract_keyword_matches(text: str, keywords) -> list:
    """Return accepted technical terms present in text (normalized).

    The public name is retained for callers that already use it.  Generic or
    hard-excluded vocabulary is ignored even when supplied by an old profile.
    """
    matches = []
    seen = set()
    for keyword in keywords or []:
        term = canonical_term_key(keyword)
        if not term or is_hard_excluded(term) or term in seen:
            continue
        if matches_term(text, term):
            seen.add(term)
            matches.append(term)
    return matches


def extract_ngrams(text: str, stopwords=None) -> list:
    """Legacy API name; extract complete technical phrases of any known length.

    A caller's stop list excludes whole expressions, not words inside a name
    such as 'analysis' in principal component analysis or 'The' in The Payne.
    """
    stop = {canonical_term_key(t) for t in (stopwords or [])}
    return [canonical_term_key(r['canonical_term'])
            for r in extract_technical_terms(text)['terms']
            if canonical_term_key(r['canonical_term']) not in stop]


def feedback_technical_terms(feedback: dict, known_terms=None) -> list[dict]:
    """Use the original qualified records; extract only for legacy feedback.

    An explicit empty list is authoritative. Invalid metadata contributes no
    keyword signal; it is never replaced by newly guessed lexical scores.
    """
    field = next((k for k in ('technical_terms', 'terms') if k in feedback), None)
    if field:
        try:
            records = normalize_technical_terms(feedback[field])
            if known_terms is not None:
                known = list(known_terms) + [r['canonical_term'] for r in records if r['already_known']]
                records = normalize_technical_terms(records, known_terms=known)
            return records
        except TechnicalTermValidationError:
            logging.getLogger(__name__).warning('Invalid stored technical terms for paper %s', feedback.get('paper_id', ''))
            return []
    return extract_technical_terms({
        'title': feedback.get('title') or '',
        'abstract': feedback.get('abstract') or feedback.get('abstract_snippet') or '',
    }, known_terms=known_terms)['terms']


def _discover_terms(feedback: list, stopwords=None) -> list:
    """Canonical keys of terms repeated in distinct feedback papers."""
    return [canonical_term_key(r['canonical_term'])
            for r in _discover_term_records(feedback, stopwords)['technical_terms']]


def _discover_term_records(feedback: list, stopwords=None, known_terms=None) -> dict:
    """Keep qualifications and scores attached while counting distinct papers."""
    papers, candidates = {}, []
    stop = {canonical_term_key(t) for t in (stopwords or [])}
    for fb in feedback:
        pid = str(fb.get('paper_id') or '')
        if not pid or fb.get('action') not in ('underrated', 'overrated'):
            continue
        for record in feedback_technical_terms(fb, known_terms):
            key = canonical_term_key(record['canonical_term'])
            if key not in stop:
                papers.setdefault(key, set()).add(pid)
                candidates.append(record)
    records = normalize_technical_terms([
        r for r in candidates
        if len(papers[canonical_term_key(r['canonical_term'])]) >= MIN_FEEDBACK_FOR_TERM
    ])
    return {'technical_terms': records,
            'learning_candidates': [r for r in records if r['learning_candidate']]}


# --- Timestamp / decay helpers ----------------------------------------------

def _to_dt(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(s[:10], "%Y-%m-%d")
            except ValueError:
                return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _days_since(dt: datetime, now: datetime) -> float:
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _decayed_factor(factor: float, age_days: float) -> float:
    return 1.0 + (factor - 1.0) * math.exp(-age_days / HALF_LIFE_DAYS)


def _clamp_weight(w: float) -> float:
    return max(WEIGHT_MIN, min(WEIGHT_MAX, float(w)))


# --- Derivation -------------------------------------------------------------

def derive_learned_profile(feedback: list, config_keywords=None, manual=None,
                           now=None) -> dict:
    """Derive a learned profile from the feedback history.

    config_keywords seeds the topic vocabulary (stable, interpretable).
    manual carries user overrides: keyword_weights maps term to float or
    None, category_weights maps category to float or None. A None value
    suppresses the term.
    """
    now = now or datetime.now(timezone.utc).astimezone()
    feedback = [fb for fb in (feedback or []) if isinstance(fb, dict)]
    raw_config_keywords = [
        str(k).strip() for k in (config_keywords or []) if str(k).strip()
    ]
    config_result = filter_technical_term_records(
        raw_config_keywords,
        known_terms=raw_config_keywords,
    )
    discovered_result = _discover_term_records(
        feedback,
        known_terms=raw_config_keywords,
    )

    # Vocabulary: validated configured terms + validated terms discovered from
    # distinct feedback papers.  The configured vocabulary is user intent and
    # therefore never becomes a learning candidate, but it still participates
    # in weight updates.
    config_records = technical_term_records_to_json(config_result['technical_terms'])['terms']
    term_records = normalize_technical_terms(config_records + discovered_result['technical_terms'])
    term_records = term_records[:MAX_LEARNED_TERMS + len(config_records)]
    vocab = {canonical_term_key(r['canonical_term']) for r in term_records}

    entries = sorted(feedback, key=lambda fb: _to_dt(fb.get("timestamp") or fb.get("date")))
    kw_weights = {}
    cat_weights = {}
    kw_sources = {}
    cat_sources = {}
    kw_last_action = {}
    cat_last_action = {}

    for fb in entries:
        action = fb.get("action")
        if action not in ("underrated", "overrated"):
            continue
        factor = BOOST if action == "underrated" else PENALTY
        age = _days_since(_to_dt(fb.get("timestamp") or fb.get("date")), now)
        eff = _decayed_factor(factor, age)
        source = f"{action}:{fb.get('paper_id', '')}"

        matched = {canonical_term_key(r['canonical_term'])
                   for r in feedback_technical_terms(fb, raw_config_keywords)}
        for term in sorted(matched & vocab):
            if kw_last_action.get(term) is not None and kw_last_action[term] != action:
                kw_weights[term] = 1.0  # conflict: latest feedback wins
            kw_weights[term] = _clamp_weight(kw_weights.get(term, 1.0) * eff)
            kw_last_action[term] = action
            kw_sources[term] = source

        cats = fb.get("categories") or []
        if isinstance(cats, str):
            cats = [c.strip() for c in cats.split(",") if c.strip()]
        for c in cats:
            c = str(c).strip()
            if not c:
                continue
            if cat_last_action.get(c) is not None and cat_last_action[c] != action:
                cat_weights[c] = 1.0
            cat_weights[c] = _clamp_weight(cat_weights.get(c, 1.0) * eff)
            cat_last_action[c] = action
            cat_sources[c] = source

    cal = CALIBRATION_PER * (sum(1 for fb in feedback if fb.get("action") == "underrated")
                             - sum(1 for fb in feedback if fb.get("action") == "overrated"))
    cal = max(-CALIBRATION_MAX, min(CALIBRATION_MAX, cal))

    # Apply manual overrides (user priority). None suppresses an auto term.
    manual = dict(manual or {})
    manual['keyword_weights'] = {
        canonical_term_key(t): w for t, w in (manual.get('keyword_weights') or {}).items()
        if canonical_term_key(t)
    }
    for term, w in (manual.get("keyword_weights", {}) or {}).items():
        term = canonical_term_key(term)
        if not term:
            continue
        if w is None:
            kw_weights.pop(term, None)
            kw_sources.pop(term, None)
            continue
        # Manual ignore remains a supported UI operation for any historical
        # entry, but a manual positive weight must not reintroduce a hard
        # exclusion into scoring.
        if is_hard_excluded(term):
            continue
        try:
            kw_weights[term] = _clamp_weight(float(w))
        except (TypeError, ValueError):
            continue
        kw_sources[term] = "manual"
    for cat, w in (manual.get("category_weights", {}) or {}).items():
        cat = str(cat).strip()
        if not cat:
            continue
        if w is None:
            cat_weights.pop(cat, None)
            cat_sources.pop(cat, None)
            continue
        try:
            cat_weights[cat] = _clamp_weight(float(w))
        except (TypeError, ValueError):
            continue
        cat_sources[cat] = "manual"

    # Preserve records even when a configured term has no feedback yet.  This
    # makes the known-vocabulary/learning-candidate distinction explicit to
    # API consumers without changing the legacy keyword_weights shape.
    learning_candidates = [record for record in term_records if record["learning_candidate"]]
    wire_terms = term_records
    wire_learning = learning_candidates

    return {
        "keyword_weights": {
            t: {"weight": round(w, 3), "source": kw_sources.get(t, ""),
                "origin": "manual" if kw_sources.get(t) == "manual" else "auto"}
            for t, w in kw_weights.items()
        },
        "category_weights": {
            c: {"weight": round(w, 3), "source": cat_sources.get(c, ""),
                "origin": "manual" if cat_sources.get(c) == "manual" else "auto"}
            for c, w in cat_weights.items()
        },
        "global_calibration": round(cal, 3),
        "manual": manual,
        "technical_terms": wire_terms,
        "learning_candidates": wire_learning,
        # Explicit alias for integrations that do not use the legacy
        # ``keyword_weights`` terminology.
        "technical_term_records": wire_terms,
        "term_schema_version": TERM_SCHEMA_VERSION,
        "tuning_version": TUNING_VERSION,
        "updated_at": now.isoformat(timespec="seconds"),
    }


def rebuild_learned_profile(config_keywords=None, manual=None) -> dict:
    """Rebuild + persist the learned profile from feedback.json."""
    if manual is None:
        existing = load_learned_profile() or {}
        manual = existing.get("manual", {}) or {}
    profile = derive_learned_profile(load_feedback(), config_keywords=config_keywords,
                                     manual=manual)
    save_learned_profile(profile)
    return profile


def ensure_learned_profile(config_keywords=None) -> dict:
    """Return the persisted profile, deriving a fresh one if missing."""
    profile = load_learned_profile()
    if (
        profile is None
        or profile.get("tuning_version") != TUNING_VERSION
        or profile.get("term_schema_version") != TERM_SCHEMA_VERSION
    ):
        profile = rebuild_learned_profile(config_keywords=config_keywords)
    return profile


# --- Prompt formatting ------------------------------------------------------

def format_learned_weights_block(profile: dict) -> str:
    """Render the learned profile as explicit instructions for the LLM prompt."""
    if not profile:
        return ""
    lines = ["=== Learned Preference Weights (apply these explicitly when scoring) ==="]
    kw = profile.get("keyword_weights", {}) or {}

    def weight(meta):
        if isinstance(meta, dict):
            return meta.get("weight", 1.0)
        try:
            return float(meta)
        except (TypeError, ValueError):
            return 1.0

    boosts = sorted(((t, weight(m)) for t, m in kw.items()
                     if weight(m) > 1.0), key=lambda x: -x[1])
    penal = sorted(((t, weight(m)) for t, m in kw.items()
                    if weight(m) < 1.0), key=lambda x: x[1])
    if boosts:
        lines.append("Boost topics (these increase relevance):")
        for t, w in boosts[:20]:
            lines.append(f'  - "{t}" x{round(w, 2)}')
    if penal:
        lines.append("Penalize topics (these decrease relevance):")
        for t, w in penal[:20]:
            lines.append(f'  - "{t}" x{round(w, 2)}')
    cw = profile.get("category_weights", {}) or {}
    if cw:
        shown = [f"  - {c} x{round(weight(m), 2)}"
                 for c, m in sorted(cw.items()) if weight(m) != 1.0]
        if shown:
            lines.append("Category weights:")
            lines.extend(shown)
    cal = profile.get("global_calibration", 0.0) or 0.0
    if cal:
        sign = "+" if cal > 0 else ""
        lines.append(f"Global calibration: {sign}{round(cal, 2)} "
                     "(shift every score by this amount)")
    return "\n".join(lines)


# --- Deterministic scoring --------------------------------------------------

def compute_adjustment(paper: dict, profile: dict) -> float:
    """Deterministic score adjustment for one paper, clamped to ±ADJUSTMENT_MAX."""
    if not profile:
        return 0.0
    adj = 0.0
    text = " ".join([str(paper.get("title", "")), str(paper.get("abstract", ""))])
    qualified = None
    if 'technical_terms' in paper:
        qualified = {canonical_term_key(r['canonical_term'])
                     for r in feedback_technical_terms(paper)}
    seen = set()
    for term, meta in (profile.get("keyword_weights", {}) or {}).items():
        key = canonical_term_key(term)
        if key in seen or is_hard_excluded(term):
            continue
        seen.add(key)
        w = meta.get("weight", 1.0) if isinstance(meta, dict) else 1.0
        matched = key in qualified if qualified is not None else matches_term(text, term)
        if matched:
            adj += (w - 1.0) * KEYWORD_ADJUST_COEF

    cats = paper.get("categories") or []
    if isinstance(cats, str):
        cats = [c.strip() for c in cats.split(",") if c.strip()]
    cw = profile.get("category_weights", {}) or {}
    for c in cats:
        meta = cw.get(str(c).strip())
        if isinstance(meta, dict):
            adj += (meta.get("weight", 1.0) - 1.0) * CATEGORY_ADJUST_COEF

    adj += profile.get("global_calibration", 0.0) or 0.0
    return max(-ADJUSTMENT_MAX, min(ADJUSTMENT_MAX, adj))


def apply_adjustment(raw_score, adjustment: float) -> int:
    """Combine an LLM raw rating with an adjustment and clamp to 1–5 stars."""
    return max(1, min(5, int(round(float(raw_score) + adjustment))))
