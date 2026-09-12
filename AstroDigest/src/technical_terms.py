"""Domain and technical term extraction for Astro Digest.

The application historically used ``keyword`` as a broad synonym for any
interesting-looking word.  This module gives that vocabulary a narrower,
deterministic contract:

* accepted items belong to one of eight domain/technical taxonomies;
* hard exclusions are applied before any score threshold;
* four 0--2 dimensions add up to ``technical_term_score`` (0--8);
* the default acceptance gate is score >= 6 and confidence >= 0.70;
* canonicalization, acronym folding, and meaningful multi-word preference are
  shared by profile extraction and feedback learning; and
* model-produced records are parsed as a strict JSON object rather than
  guessed from free-form text.

The lexical hints below are intentionally small and conservative.  They are a
bootstrap for local metadata and feedback learning; an LLM can provide richer
candidate records through :func:`parse_technical_terms_response`, but the same
validation and acceptance gate is always applied afterwards.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping


# Public taxonomy contract.  Keep this tuple stable: persisted records and
# prompts use these exact identifiers.
TECHNICAL_TERM_TAXONOMY = (
    "scientific_concept",
    "physical_quantity",
    "process_or_phenomenon",
    "method_or_technique",
    "algorithm_or_model",
    "instrument_or_software",
    "dataset_or_resource",
    "domain_object_or_class",
)

# Short alias for callers that naturally refer to the list as TAXONOMY.
TAXONOMY = TECHNICAL_TERM_TAXONOMY
TERM_TAXONOMY = TECHNICAL_TERM_TAXONOMY

DEFAULT_MIN_TECHNICAL_TERM_SCORE = 6
DEFAULT_MIN_CONFIDENCE = 0.70
MIN_TECHNICAL_TERM_SCORE = DEFAULT_MIN_TECHNICAL_TERM_SCORE
MIN_TECHNICAL_TERM_CONFIDENCE = DEFAULT_MIN_CONFIDENCE
MIN_TERM_SCORE = DEFAULT_MIN_TECHNICAL_TERM_SCORE
MIN_TERM_CONFIDENCE = DEFAULT_MIN_CONFIDENCE
TERM_SCHEMA_VERSION = "technical-terms.v1"

# JSON-Schema-like description kept as a plain JSON-ready object.  Validation
# is implemented locally below so the app does not add another runtime
# dependency just for a small response contract.
TECHNICAL_TERM_RECORD_SCHEMA = {
    "type": "object",
    "required": [
        "surface_form", "canonical_term", "category", "scores", "confidence",
        "reason", "already_known", "learning_candidate",
    ],
    "additionalProperties": False,
    "properties": {
        "surface_form": {"type": "string", "minLength": 1},
        "canonical_term": {"type": "string", "minLength": 1},
        "category": {"enum": list(TECHNICAL_TERM_TAXONOMY)},
        "scores": {
            "type": "object",
            "required": [
                "domain_specificity", "conceptual_independence",
                "terminological_stability", "technical_informativeness",
                "technical_term_score",
            ],
            "additionalProperties": False,
            "properties": {
                "domain_specificity": {"type": "integer", "minimum": 0, "maximum": 2},
                "conceptual_independence": {"type": "integer", "minimum": 0, "maximum": 2},
                "terminological_stability": {"type": "integer", "minimum": 0, "maximum": 2},
                "technical_informativeness": {"type": "integer", "minimum": 0, "maximum": 2},
                "technical_term_score": {"type": "integer", "minimum": 0, "maximum": 8},
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "already_known": {"type": "boolean"},
        "learning_candidate": {"type": "boolean"},
    },
}
TECHNICAL_TERM_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["domain", "subdomain", "terms"],
    "additionalProperties": False,
    "properties": {
        "domain": {"type": "string"},
        "subdomain": {"type": ["string", "null"]},
        "terms": {
            "type": "array",
            "items": TECHNICAL_TERM_RECORD_SCHEMA,
        },
    },
}


class TechnicalTermValidationError(ValueError):
    """Raised when a structured technical-term response violates the schema."""


_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2212"
_DASH_TRANSLATION = str.maketrans({ch: "-" for ch in _DASHES})
_TOKEN_RE = re.compile(r"[A-Za-z\u00c0-\u024f\u0391-\u03c9][A-Za-z0-9\u00c0-\u024f\u0391-\u03c9]*(?:[-/][A-Za-z0-9\u00c0-\u024f\u0391-\u03c9]+)*")
_WORD_RE = re.compile(r"[a-z0-9]+(?:[-/][a-z0-9]+)*")


# These are intentionally hard exclusions, not merely low-scoring terms.
# A compound containing one of these words may still be valid (``spectral
# analysis``), so exact terms and clearly structural/descriptive phrases are
# handled below instead of rejecting every compound by substring.
HARD_EXCLUSION_TERMS = frozenset(
    {
        # Ordinary language and generic academic language.
        "important", "interesting", "large", "small", "new", "novel",
        "recent", "different", "similar", "possible", "likely", "main",
        "increase", "decrease", "high", "low", "significant",
        "study", "studies", "work", "paper", "papers", "result", "results",
        "finding", "findings", "analysis", "analyses", "method", "methods",
        "approach", "approaches", "technique", "techniques", "model", "models",
        "data", "dataset", "sample", "samples", "observation", "observations",
        "measurement", "measurements", "property", "properties", "effect", "effects",
        "calculation", "calculations", "comparison", "comparisons", "uncertainty",
        "feature", "features", "parameter", "parameters", "value", "values",
        "information", "evidence", "framework", "pipeline", "process", "case",
        "system", "systems", "object", "objects", "source", "sources",
        "resulting", "using", "used", "based", "present", "shows", "showing",
        "discussion", "introduction", "conclusion", "conclusions", "section",
        "sections", "figure", "figures", "table", "tables", "appendix",
        "future work", "related work", "this study", "the present study",
        # Over-wide domain words.
        "star", "stars", "galaxy", "galaxies", "universe", "astronomy",
        "astrophysics", "physics", "cosmology", "astronomical", "stellar",
        "extragalactic", "survey", "surveys", "catalog", "catalogue",
        "classification", "classifications", "spectrum", "spectra", "telescope",
        "stellar spectrum", "abundance", "abundances", "planet", "planets",
        "element", "elements", "procedure", "procedures", "determine", "determined",
        "generated", "generation", "photometric", "photometrically",
    }
)
HARD_EXCLUSIONS = HARD_EXCLUSION_TERMS

_STRUCTURAL_WORDS = frozenset(
    {
        "abstract", "background", "motivation", "overview", "introduction",
        "methods", "methodology", "results", "discussion", "conclusion",
        "summary", "references", "appendix", "section", "figure", "table",
    }
)

_GENERIC_CONSTITUENTS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "in", "on", "for", "with",
        "from", "to", "by", "via", "using", "based", "study", "analysis",
        "result", "results", "method", "methods", "model", "models", "data",
        "sample", "samples", "paper", "papers", "work", "approach", "effect",
        "property", "properties", "observation", "observations", "survey",
        "surveys", "system", "systems", "object", "objects", "source", "sources",
    }
)

_DESCRIPTIVE_STARTERS = frozenset(
    {
        "new", "novel", "recent", "large", "small", "high", "low", "early",
        "late", "young", "old", "nearby", "distant", "possible", "likely",
        "observed", "predicted", "measured", "different", "similar", "extremely",
        "highly", "strongly", "weakly", "our", "this", "the", "a", "an",
    }
)


# A compact astronomy vocabulary provides stable taxonomy and a useful
# confidence prior for metadata/feedback candidates.  It is not a whitelist:
# terms can also pass using the compositional heuristics in ``score_term``.
_KNOWN_TERM_TAXONOMY = {
    # Scientific concepts.
    "metallicity": "scientific_concept",
    "dark matter": "scientific_concept",
    "dark energy": "scientific_concept",
    "stellar atmosphere": "scientific_concept",
    "stellar atmospheres": "scientific_concept",
    "stellar population": "scientific_concept",
    "stellar populations": "scientific_concept",
    "chemical evolution": "scientific_concept",
    "chemical enrichment": "scientific_concept",
    "nucleosynthesis": "process_or_phenomenon",
    "reionization": "scientific_concept",
    "galaxy formation": "scientific_concept",
    "stellar evolution": "scientific_concept",
    "circumgalactic": "scientific_concept",
    "interstellar medium": "scientific_concept",
    "large-scale structure": "scientific_concept",
    "cosmic web": "scientific_concept",
    "non-lte": "scientific_concept",
    "local thermodynamic equilibrium": "scientific_concept",
    "stellar atmosphere model": "scientific_concept",
    "non-lte radiative transfer": "process_or_phenomenon",
    "equivalent-width measurement": "physical_quantity",
    "equivalent width measurement": "physical_quantity",
    "monte carlo uncertainty propagation": "method_or_technique",
    # Physical quantities.
    "effective temperature": "physical_quantity",
    "surface gravity": "physical_quantity",
    "equivalent width": "physical_quantity",
    "radial velocity": "physical_quantity",
    "velocity dispersion": "physical_quantity",
    "carbon abundance": "physical_quantity",
    "stellar abundance": "physical_quantity",
    "stellar abundances": "physical_quantity",
    "abundance": "physical_quantity",
    "abundances": "physical_quantity",
    "metal abundance": "physical_quantity",
    "initial mass function": "physical_quantity",
    "mass function": "physical_quantity",
    "redshift": "physical_quantity",
    "luminosity function": "physical_quantity",
    "mass-to-light ratio": "physical_quantity",
    "line parameters": "physical_quantity",
    "spectral energy distribution": "physical_quantity",
    "sed": "physical_quantity",
    "imf": "physical_quantity",
    "effective temperatures": "physical_quantity",
    "carbon abundances": "physical_quantity",
    "surface gravities": "physical_quantity",
    "equivalent widths": "physical_quantity",
    "velocity dispersions": "physical_quantity",
    "synthetic spectrum": "scientific_concept",
    "synthetic spectra": "scientific_concept",
    "microturbulence": "scientific_concept",
    "departure coefficient": "scientific_concept",
    "nlte departure coefficient": "scientific_concept",
    "curve of growth": "scientific_concept",
    "continuum-normalized spectrum": "scientific_concept",
    "high-resolution spectroscopy": "method_or_technique",
    "molecular equilibrium": "scientific_concept",
    "opacity distribution function": "scientific_concept",
    "contribution function": "scientific_concept",
    "synthetic spectral grid": "scientific_concept",
    "ch g-band": "scientific_concept",
    "high-resolution spectrum": "scientific_concept",
    # Processes and phenomena.
    "radiative transfer": "process_or_phenomenon",
    "stellar convection": "process_or_phenomenon",
    "line broadening": "process_or_phenomenon",
    "gravitational collapse": "process_or_phenomenon",
    "star formation": "process_or_phenomenon",
    "core-collapse": "process_or_phenomenon",
    "pair-instability": "process_or_phenomenon",
    "proton ingestion": "process_or_phenomenon",
    "gravitational wave": "process_or_phenomenon",
    "gravitational waves": "process_or_phenomenon",
    "neutron star merger": "process_or_phenomenon",
    "neutron star mergers": "process_or_phenomenon",
    "galactic outflow": "process_or_phenomenon",
    "galactic outflows": "process_or_phenomenon",
    "accretion": "process_or_phenomenon",
    "feedback": "process_or_phenomenon",
    "r-process": "process_or_phenomenon",
    "s-process": "process_or_phenomenon",
    # Methods and techniques.
    "spectral synthesis": "method_or_technique",
    "spectroscopy": "method_or_technique",
    "spectroscopic survey": "method_or_technique",
    "spectral line": "method_or_technique",
    "spectral lines": "method_or_technique",
    "continuum normalization": "method_or_technique",
    "continuum normalisation": "method_or_technique",
    "abundance analysis": "method_or_technique",
    "spectral analysis": "method_or_technique",
    "bayesian inference": "method_or_technique",
    "cross-correlation": "method_or_technique",
    "cross-correlation function": "method_or_technique",
    "abundance matching": "method_or_technique",
    "forward modeling": "method_or_technique",
    "forward modelling": "method_or_technique",
    "photometry": "method_or_technique",
    "asteroseismology": "method_or_technique",
    # Algorithms and models.
    "mcmc": "algorithm_or_model",
    "markov chain monte carlo": "algorithm_or_model",
    "gaussian process": "algorithm_or_model",
    "convolutional neural network": "algorithm_or_model",
    "principal component analysis": "algorithm_or_model",
    "random forest": "algorithm_or_model",
    "neural network": "algorithm_or_model",
    "machine learning": "algorithm_or_model",
    "cnn": "algorithm_or_model",
    "pca": "algorithm_or_model",
    "the payne": "algorithm_or_model",
    "ferre": "algorithm_or_model",
    "atlas9": "algorithm_or_model",
    # Instruments and software.
    "gaia": "instrument_or_software",
    "jwst": "instrument_or_software",
    "lamost": "instrument_or_software",
    "apogee": "instrument_or_software",
    "espresso": "instrument_or_software",
    "keck/hires": "instrument_or_software",
    "moog": "instrument_or_software",
    "turbospectrum": "instrument_or_software",
    "marcs": "instrument_or_software",
    # Datasets and resources.
    "gaia dr3": "dataset_or_resource",
    "gaia dr2": "dataset_or_resource",
    "apogee dr17": "dataset_or_resource",
    "sdss-v": "dataset_or_resource",
    "vald": "dataset_or_resource",
    "kurucz line list": "dataset_or_resource",
    "atomic data": "dataset_or_resource",
    "molecular data": "dataset_or_resource",
    "line list": "dataset_or_resource",
    # Domain objects/classes.
    "first stars": "domain_object_or_class",
    "high-redshift galaxies": "domain_object_or_class",
    "population iii": "domain_object_or_class",
    "metal-poor star": "domain_object_or_class",
    "metal-poor stars": "domain_object_or_class",
    "extremely metal-poor star": "domain_object_or_class",
    "extremely metal-poor stars": "domain_object_or_class",
    "carbon-enhanced metal-poor star": "domain_object_or_class",
    "carbon-enhanced metal-poor stars": "domain_object_or_class",
    "cemp-no": "domain_object_or_class",
    "cemp-no star": "domain_object_or_class",
    "cemp-no stars": "domain_object_or_class",
    "supernova": "domain_object_or_class",
    "supernovae": "domain_object_or_class",
    "type ia": "domain_object_or_class",
    "type ii": "domain_object_or_class",
    "hypernova": "domain_object_or_class",
    "kilonova": "domain_object_or_class",
    "massive star": "domain_object_or_class",
    "massive stars": "domain_object_or_class",
    "red supergiant": "domain_object_or_class",
    "red supergiants": "domain_object_or_class",
    "red giant": "domain_object_or_class",
    "red giants": "domain_object_or_class",
    "white dwarf": "domain_object_or_class",
    "white dwarfs": "domain_object_or_class",
    "neutron star": "domain_object_or_class",
    "neutron stars": "domain_object_or_class",
    "black hole": "domain_object_or_class",
    "black holes": "domain_object_or_class",
    "m dwarf": "domain_object_or_class",
    "m dwarfs": "domain_object_or_class",
    "agb": "domain_object_or_class",
    "dwarf galaxy": "domain_object_or_class",
    "dwarf galaxies": "domain_object_or_class",
    "globular cluster": "domain_object_or_class",
    "globular clusters": "domain_object_or_class",
    "galactic halo": "domain_object_or_class",
    "stellar halo": "domain_object_or_class",
    "milky way": "domain_object_or_class",
    "quasar": "domain_object_or_class",
    "quasar outflow": "domain_object_or_class",
    "quasar outflows": "domain_object_or_class",
    "hot jupiter": "domain_object_or_class",
    "protoplanetary disk": "domain_object_or_class",
    "circumstellar disk": "domain_object_or_class",
    "planetary nebula": "domain_object_or_class",
    "supernova remnant": "domain_object_or_class",
    "type ia supernova": "domain_object_or_class",
    "damped lyman-alpha system": "domain_object_or_class",
}

_KNOWN_ACRONYMS = frozenset(
    {
        "mcmc", "cnn", "pca", "sed", "jwst", "gaia", "apogee", "lamost", "sdss",
        "sdss-v", "vald", "cemp-no", "agb", "imf", "sn ia", "sn ii",
        "teff", "logg", "ew", "nlte", "lte",
    }
)

_ACRONYM_EXPANSIONS = {
    "markov chain monte carlo": "mcmc",
    "convolutional neural network": "cnn",
    "principal component analysis": "pca",
    "spectral energy distribution": "sed",
    "james webb space telescope": "jwst",
    "initial mass function": "imf",
}

# Semantic canonicalization is kept separate from ``canonicalize_term``'s
# conservative comparison key.  This lets callers still match the surface
# form ``MCMC`` while serializing the canonical concept as
# ``Markov chain Monte Carlo``.
_CANONICAL_ALIASES = {
    "mcmc": "markov chain monte carlo",
    "pca": "principal component analysis",
    "cnn": "convolutional neural network",
    "sed": "spectral energy distribution",
    "teff": "effective temperature",
    "logg": "surface gravity",
    "log g": "surface gravity",
    "ew": "equivalent width",
    "nlte": "non-lte",
    "lte": "local thermodynamic equilibrium",
    "imf": "initial mass function",
    "sn ia": "type ia",
    "sn ii": "type ii",
    "effective temperatures": "effective temperature",
    "carbon abundances": "carbon abundance",
    "surface gravities": "surface gravity",
    "equivalent widths": "equivalent width",
    "velocity dispersions": "velocity dispersion",
    "synthetic spectra": "synthetic spectrum",
    "stellar atmospheres": "stellar atmosphere",
    "stellar populations": "stellar population",
    "globular clusters": "globular cluster",
    "dwarf galaxies": "dwarf galaxy",
    "massive stars": "massive star",
    "red giants": "red giant",
    "red supergiants": "red supergiant",
    "white dwarfs": "white dwarf",
    "neutron stars": "neutron star",
    "black holes": "black hole",
    "supernovae": "supernova",
    "cemp-no stars": "cemp-no star",
    "neutron star mergers": "neutron star merger",
}

_CANONICAL_DISPLAY = {
    "markov chain monte carlo": "Markov chain Monte Carlo",
    "the payne": "The Payne",
    "effective temperature": "effective temperature",
    "surface gravity": "surface gravity",
    "equivalent width": "equivalent width",
    "non-lte": "non-LTE",
    "non-lte radiative transfer": "non-LTE radiative transfer",
    "local thermodynamic equilibrium": "local thermodynamic equilibrium",
    "initial mass function": "initial mass function",
    "type ia": "Type Ia",
    "type ii": "Type II",
    "population iii": "Population III",
    "agb": "AGB",
    "ch g-band": "CH G-band",
    "cemp-no": "CEMP-no",
    "cemp-no star": "CEMP-no star",
    "gaia": "Gaia",
    "gaia dr3": "Gaia DR3",
    "gaia dr2": "Gaia DR2",
    "apogee": "APOGEE",
    "apogee dr17": "APOGEE DR17",
    "jwst": "JWST",
    "lamost": "LAMOST",
    "espresso": "ESPRESSO",
    "keck/hires": "Keck/HIRES",
    "moog": "MOOG",
    "ferre": "FERRE",
    "atlas9": "ATLAS9",
    "turbospectrum": "Turbospectrum",
    "marcs": "MARCS",
    "vald": "VALD",
    "sdss-v": "SDSS-V",
}

_GENERIC_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "in", "on", "for", "with",
        "from", "to", "by", "via", "using", "based", "this", "our", "we",
        "study", "studies", "paper", "papers", "analysis", "method", "methods",
        "model", "models", "data", "results", "result", "sample", "samples",
        "show", "shows", "found", "find", "new", "recent", "using",
    }
)

_DOMAIN_HINTS = frozenset(
    {
        "abundance", "abundances", "accretion", "atmosphere", "atmospheres", "black",
        "collapse", "cluster", "clusters", "cosmic", "dwarf", "energy", "enrichment",
        "evolution", "galactic", "galaxy", "galaxies", "giant", "giants", "gravity",
        "halo", "hypernova", "instability", "kilonova", "line", "lines", "metal",
        "metallicity", "merger", "mergers", "neutron", "nucleosynthesis", "outflow",
        "outflows", "pair", "photometry", "planet", "pulsar", "quasar", "radiative",
        "redshift", "reionization", "spectral", "spectroscopy", "star", "stars",
        "stellar", "supernova", "supernovae", "transfer", "white", "dwarf", "process",
        "population", "reionization", "gravitational", "wave", "waves", "continuum",
    }
)

_QUANTITY_HINTS = frozenset(
    {
        "abundance", "abundances", "temperature", "gravity", "width", "velocity",
        "dispersion", "mass", "luminosity", "density", "redshift", "ratio", "fraction",
        "parameter", "parameters", "equivalent", "function",
    }
)

_PROCESS_HINTS = frozenset(
    {
        "formation", "evolution", "enrichment", "nucleosynthesis", "collapse", "transfer",
        "broadening", "reionization", "merger", "mergers", "outflow", "outflows", "accretion",
        "feedback", "ingestion", "instability", "emission", "absorption",
    }
)

_METHOD_HINTS = frozenset(
    {
        "spectroscopy", "photometry", "synthesis", "normalization", "normalisation", "inference",
        "correlation", "matching", "retrieval", "fitting", "calibration", "modeling", "modelling",
        "seismology",
    }
)

_OBJECT_HINTS = frozenset(
    {
        "star", "stars", "galaxy", "galaxies", "cluster", "clusters", "supernova", "supernovae",
        "quasar", "planet", "jupiter", "dwarf", "dwarfs", "giant", "giants", "hole", "halo",
        "nebula", "disk", "system", "population", "object", "objects", "cloud", "pulsar",
    }
)


def canonicalize_term(value) -> str:
    """Return a stable comparison key for a term.

    Canonicalization is intentionally conservative.  It folds Unicode and
    whitespace, normalizes dash variants, removes harmless TeX braces, and
    lower-cases.  It does not perform broad stemming: ``supernova`` and
    ``supernovae`` remain separate surface concepts unless they are explicitly
    deduplicated by the caller.
    """

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.translate(_DASH_TRANSLATION)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\\(?:text|mathrm|mathbf|mathit)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*([/-])\s*", r"\1", text)
    # Common scientific compounds are written both with and without a
    # separator in bibliographic metadata (``non LTE``/``non-LTE``).
    text = re.sub(
        r"\b(non|metal|carbon|core|pair|r|s)\s+(lte|poor|enhanced|collapse|instability|process)\b",
        r"\1-\2",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(".,;:!?()[]<>\"'`$")
    return text.casefold()


def _surface_term(value) -> str:
    """Normalize a display form while preserving useful capitalization."""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).translate(_DASH_TRANSLATION)
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"\s*([/-])\s*", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(".,;:!?()[]<>\"'`$")


def canonical_term_key(value) -> str:
    """Return the canonical concept key, including supported acronym aliases."""

    key = canonicalize_term(value)
    return _CANONICAL_ALIASES.get(key, key)


def canonical_term_display(value) -> str:
    """Return a stable human-readable canonical form for a term."""

    key = canonical_term_key(value)
    return _CANONICAL_DISPLAY.get(key, _surface_term(value) or key)


def term_match_keys(value) -> tuple[str, ...]:
    """Return canonical and safe surface aliases for whole-term matching.

    Only explicit acronym aliases are added.  Morphological variants such as
    ``supernovae`` are deliberately not treated as a match for ``supernova``
    because the legacy matcher promises whole-token behavior.
    """

    raw = canonicalize_term(value)
    canonical = canonical_term_key(value)
    variants = {raw, canonical}
    for alias, target in _CANONICAL_ALIASES.items():
        if target == canonical and _is_acronym(alias):
            variants.add(alias)
    return tuple(variant for variant in variants if variant)


def _words(value: str) -> list[str]:
    return _WORD_RE.findall(canonicalize_term(value))


def _is_acronym(value: str) -> bool:
    # A code-like spelling alone does not establish a technical concept.
    return canonicalize_term(value) in _KNOWN_ACRONYMS


def _acronym_for_phrase(value: str) -> str:
    # Matching arbitrary initials would conflate unrelated concepts.
    return _ACRONYM_EXPANSIONS.get(canonicalize_term(value), "")


def _has_known_equivalent(key: str, known: set[str]) -> bool:
    key = canonical_term_key(key)
    if key in known:
        return True
    if _is_acronym(key):
        return any(_acronym_for_phrase(item) == key for item in known)
    acronym = _acronym_for_phrase(key)
    return bool(acronym and acronym in known)


def is_hard_excluded(term: str) -> bool:
    """Return whether a candidate is categorically ineligible.

    Hard exclusion is evaluated before score/confidence, including for model
    records that claim a high score.
    """

    key = canonicalize_term(term)
    if not key:
        return True
    if key in HARD_EXCLUSION_TERMS or key in _STRUCTURAL_WORDS:
        return True
    # Complete registered expressions take precedence over constituent-word
    # rules: The Payne, curve of growth, stellar atmosphere model, etc.
    if canonical_term_key(key) in _KNOWN_TERM_TAXONOMY:
        return False
    words = _words(key)
    if not words:
        return True
    if len(words) == 1 and words[0] in _GENERIC_CONSTITUENTS:
        return True
    if key.startswith(("the ", "a ", "an ", "this ", "our ")):
        return True
    # A trailing generic noun usually means the model extracted a descriptive
    # fragment such as "chemical evolution model" rather than the concept
    # itself.  Exact registered terms like "atomic data" are allowed.
    if key not in _KNOWN_TERM_TAXONOMY and len(words) >= 2:
        if words[-1] in _GENERIC_CONSTITUENTS or words[0] in _GENERIC_CONSTITUENTS:
            return True
    # An astronomical noun does not turn "new/observed stars" into a stable
    # class. Specific registered classes have already been handled above.
    if words[0] in _DESCRIPTIVE_STARTERS | {"very", "best", "strong", "weak"}:
        return True
    if any(word in {"our", "this", "used", "using", "observed", "obtained", "derived"}
           for word in words):
        return True
    # A phrase made entirely from generic/structural words cannot become a
    # term just by receiving model-generated scores.
    if len(words) > 1 and all(word in _GENERIC_CONSTITUENTS or word in _STRUCTURAL_WORDS for word in words):
        return True
    return False


def infer_taxonomy(term: str, context: str = "") -> str:
    """Infer a taxonomy for a raw candidate using exact and compositional hints."""

    key = canonical_term_key(term)
    exact = _KNOWN_TERM_TAXONOMY.get(key)
    if exact:
        return exact
    words = set(_words(key))
    if _is_acronym(key) or re.search(r"\b(?:dr\d+|sn[- ]?(?:ia|ii)|cemp[- ]?\w+)\b", key):
        if key in {"gaia", "jwst", "apogee", "lamost", "espresso", "moog", "ferre", "atlas9"}:
            return "instrument_or_software"
        return "algorithm_or_model" if key in {"mcmc", "cnn", "pca"} else "dataset_or_resource"
    if words & {"dr3", "dr2", "dr17", "survey", "catalog", "catalogue", "database", "list", "release", "data"}:
        return "dataset_or_resource"
    if words & {"telescope", "instrument", "spectrograph", "software", "code", "pipeline"}:
        return "instrument_or_software"
    if words & {"neural", "network", "forest", "process", "monte", "carlo", "gaussian", "bayesian"}:
        return "algorithm_or_model"
    if words & _QUANTITY_HINTS:
        return "physical_quantity"
    if words & _METHOD_HINTS:
        return "method_or_technique"
    if words & _PROCESS_HINTS:
        return "process_or_phenomenon"
    if words & _OBJECT_HINTS:
        return "domain_object_or_class"
    if words & _DOMAIN_HINTS or len(words) >= 2:
        return "scientific_concept"
    return "scientific_concept"


def score_term(term: str, taxonomy: str | None = None, context: str = "") -> dict:
    """Return the four-dimensional deterministic score for a candidate.

    The result contains the four 0--2 dimensions, their exact sum, and a
    confidence prior.  Structured LLM records use the same shape and are
    checked against the same sum by the strict parser.
    """

    raw_key = canonicalize_term(term)
    key = canonical_term_key(term)
    words = _words(key)
    hard = is_hard_excluded(key)
    taxonomy = taxonomy if taxonomy in TECHNICAL_TERM_TAXONOMY else infer_taxonomy(key, context)
    known = key in _KNOWN_TERM_TAXONOMY or key in _KNOWN_ACRONYMS
    has_domain_hint = bool(set(words) & _DOMAIN_HINTS) or known
    multiword = len(words) >= 2
    acronym = _is_acronym(raw_key)
    technical_shape = multiword or acronym or any(
        suffix in key for suffix in ("-process", "-collapse", "-instability", "-ology", "-synthesis", "-ization")
    )

    if hard:
        dimensions = {
            "domain_specificity": 0,
            "conceptual_independence": 0,
            "terminological_stability": 0,
            "technical_informativeness": 0,
        }
        return {**dimensions, "technical_term_score": 0, "confidence": 0.0}

    domain_specificity = 2 if has_domain_hint or taxonomy != "scientific_concept" else (1 if technical_shape else 0)
    conceptual_independence = 2 if known or acronym or (multiword and not is_hard_excluded(key)) else (1 if len(key) >= 6 else 0)
    terminological_stability = 2 if known or acronym else (1 if multiword or len(key) >= 8 else 0)
    technical_informativeness = 2 if known or acronym or (multiword and has_domain_hint) else (1 if len(key) >= 6 else 0)

    if not known:
        # Lexical hints cannot establish stability or independence. Unknown
        # metadata needs a qualified structured record rather than fabricated
        # confidence from word length or an astronomical constituent.
        conceptual_independence = min(conceptual_independence, 1)
        terminological_stability = 0
        technical_informativeness = min(technical_informativeness, 1)

    dimensions = {
        "domain_specificity": max(0, min(2, int(domain_specificity))),
        "conceptual_independence": max(0, min(2, int(conceptual_independence))),
        "terminological_stability": max(0, min(2, int(terminological_stability))),
        "technical_informativeness": max(0, min(2, int(technical_informativeness))),
    }
    total = sum(dimensions.values())
    confidence = 0.35 + 0.06 * total
    if known:
        confidence += 0.08
    if acronym:
        confidence += 0.04
    if context and re.search(re.escape(key), canonicalize_term(context)):
        confidence += 0.03
    confidence = round(max(0.0, min(1.0, confidence)), 3)
    return {**dimensions, "technical_term_score": total, "confidence": confidence}


def _known_keys(known_terms: Iterable | None) -> set[str]:
    if known_terms is None:
        return set()
    if isinstance(known_terms, (str, Mapping)):
        known_terms = [known_terms]
    keys = set()
    for item in known_terms:
        if isinstance(item, Mapping):
            item = item.get("canonical_term") or item.get("term") or item.get("surface_form")
        key = canonical_term_key(item)
        if key:
            keys.add(key)
    return keys


def _coerce_candidate(candidate, context: str = "", known: set[str] | None = None) -> dict | None:
    """Score local strings; strictly validate externally qualified records."""

    known = known or set()
    if isinstance(candidate, Mapping):
        return validate_technical_term_record(candidate, known_terms=known, min_score=0, min_confidence=0)
    else:
        display = _surface_term(candidate)
        key = canonical_term_key(display)
        if not key:
            return None
        taxonomy = infer_taxonomy(key, context)
        score_data = score_term(display, taxonomy=taxonomy, context=context)
        dimensions = {name: score_data[name] for name in (
            "domain_specificity", "conceptual_independence", "terminological_stability", "technical_informativeness"
        )}
        score = score_data["technical_term_score"]
        confidence = score_data["confidence"]
        hard = is_hard_excluded(key)

    is_known = _has_known_equivalent(key, known)
    return {
        "term": display,
        "surface_form": display,
        "canonical_term": key,
        "canonical_display": canonical_term_display(key),
        "taxonomy": taxonomy,
        **dimensions,
        "technical_term_score": int(score),
        "confidence": round(float(confidence), 3),
        "hard_exclusion": hard,
        "learning_candidate": not is_known,
    }


def _record_rank(record: dict) -> tuple:
    words = _words(record["canonical_term"])
    return (
        len(words),
        int(record.get("technical_term_score", 0)),
        float(record.get("confidence", 0.0)),
        len(record.get("canonical_term", "")),
    )


def _prefer_record(current: dict | None, candidate: dict) -> dict:
    """Choose a stable representative when canonical keys collide."""

    if current is None:
        return candidate
    current_surface = current.get("surface_form") or current.get("term") or ""
    candidate_surface = candidate.get("surface_form") or candidate.get("term") or ""
    current_acronym = _is_acronym(current_surface)
    candidate_acronym = _is_acronym(candidate_surface)
    if candidate_acronym != current_acronym:
        return candidate if candidate_acronym else current
    return candidate if _record_rank(candidate) > _record_rank(current) else current


def _same_concept(a: dict, b: dict) -> bool:
    ka = canonical_term_key(a.get("canonical_term") or a.get("surface_form") or a.get("term"))
    kb = canonical_term_key(b.get("canonical_term") or b.get("surface_form") or b.get("term"))
    if ka == kb:
        return True
    a_surface = canonicalize_term(a.get("surface_form") or a.get("term") or ka)
    b_surface = canonicalize_term(b.get("surface_form") or b.get("term") or kb)
    return (
        _is_acronym(a_surface) and _acronym_for_phrase(b_surface) == a_surface
    ) or (
        _is_acronym(b_surface) and _acronym_for_phrase(a_surface) == b_surface
    )


def are_equivalent_terms(first: str, second: str) -> bool:
    """Return whether two surface forms share a canonical/acronym concept."""

    first_key = canonical_term_key(first)
    second_key = canonical_term_key(second)
    if not first_key or not second_key:
        return False
    if first_key == second_key:
        return True
    return (
        _is_acronym(first_key) and _acronym_for_phrase(second_key) == first_key
    ) or (
        _is_acronym(second_key) and _acronym_for_phrase(first_key) == second_key
    )


def _prefer_specific_terms(records: list[dict], known: set[str]) -> list[dict]:
    """Deduplicate and suppress generic constituents.

    A longer meaningful phrase wins over its shorter constituent.  A term
    explicitly present in the known vocabulary is retained even when it is a
    constituent: known terms are user intent, not automatically discovered
    noise.
    """

    by_key: dict[str, dict] = {}
    for record in records:
        key = record["canonical_term"]
        by_key[key] = _prefer_record(by_key.get(key), record)

    deduped: list[dict] = []
    for record in sorted(by_key.values(), key=lambda item: (_record_rank(item), item["canonical_term"]), reverse=True):
        duplicate_index = next((i for i, other in enumerate(deduped) if _same_concept(record, other)), None)
        if duplicate_index is None:
            deduped.append(record)
            continue
        other = deduped[duplicate_index]
        # For abbreviation/expansion pairs, the meaningful multi-word form is
        # preferred; for ordinary duplicates use score/confidence/rank.
        record_words = _words(record["canonical_term"])
        other_words = _words(other["canonical_term"])
        if len(record_words) > len(other_words) or _record_rank(record) > _record_rank(other):
            deduped[duplicate_index] = record

    kept: list[dict] = []
    for record in deduped:
        key = record["canonical_term"]
        if _has_known_equivalent(key, known):
            kept.append(record)
            continue
        short_words = _words(key)
        suppress = False
        for other in deduped:
            if other is record:
                continue
            long_words = _words(other["canonical_term"])
            if len(long_words) <= len(short_words):
                continue
            # Require a contiguous constituent, not just overlapping words.
            for start in range(len(long_words) - len(short_words) + 1):
                if long_words[start : start + len(short_words)] == short_words:
                    if not is_hard_excluded(other["canonical_term"]):
                        suppress = True
                        break
            if suppress:
                break
        if not suppress:
            kept.append(record)
    return sorted(kept, key=lambda item: (-item["technical_term_score"], -item["confidence"], item["canonical_term"]))


def filter_technical_term_records(
    candidates: Iterable,
    known_terms: Iterable | None = None,
    *,
    context: str = "",
    min_score: int = DEFAULT_MIN_TECHNICAL_TERM_SCORE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict:
    """Validate, score, deduplicate, and split technical-term candidates.

    The return value is a JSON-ready envelope with ``technical_terms`` and
    ``learning_candidates``.  Both lists contain the same normalized record
    schema; learning candidates are the subset whose canonical term is not in
    ``known_terms``.
    """

    if known_terms is not None and not isinstance(known_terms, (str, Mapping)):
        known_terms = list(known_terms)
    known = _known_keys(known_terms)
    try:
        if isinstance(candidates, (str, Mapping)):
            candidate_list = [candidates]
        else:
            candidate_list = list(candidates or [])
    except TypeError:
        candidate_list = []
    accepted = []
    for candidate in candidate_list:
        record = _coerce_candidate(candidate, context=context, known=known)
        if record is None or record["hard_exclusion"]:
            continue
        if record["technical_term_score"] < int(min_score):
            continue
        if record["confidence"] < float(min_confidence):
            continue
        accepted.append(record)

    accepted = _prefer_specific_terms(accepted, known)
    # Recompute the known flag after acronym/expanded-form selection.
    for record in accepted:
        record["learning_candidate"] = not _has_known_equivalent(record["canonical_term"], known)
    learning = [record for record in accepted if record["learning_candidate"]]
    return {
        "technical_terms": accepted,
        "learning_candidates": learning,
    }


def _candidate_strings_from_text(text: str, max_words: int | None = None) -> list[str]:
    """Match complete registered phrases on contiguous source spans.

    The default length follows the vocabulary, not a two/four-word cap.
    Punctuation and paragraph boundaries cannot manufacture new phrases.
    Longest matches protect constituents (including LTE inside non LTE).
    """
    text = unicodedata.normalize("NFKC", str(text or "")).translate(_DASH_TRANSLATION)
    tokens = list(_TOKEN_RE.finditer(text))
    vocabulary = set(_KNOWN_TERM_TAXONOMY) | set(_CANONICAL_ALIASES) | set(_ACRONYM_EXPANSIONS) | set(_KNOWN_ACRONYMS)
    limit = max(len(_TOKEN_RE.findall(term)) for term in vocabulary) + 1
    if max_words is not None:
        limit = max(1, int(max_words))
    candidates, seen, occupied = [], set(), []
    for size in range(min(limit, len(tokens)), 0, -1):
        for start in range(len(tokens) - size + 1):
            span = tokens[start:start + size]
            if any(not re.fullmatch(r"\s+", text[a.end():b.start()])
                   or "\n\n" in text[a.end():b.start()]
                   for a, b in zip(span, span[1:])):
                continue
            begin, end = span[0].start(), span[-1].end()
            phrase = text[begin:end]
            key = canonicalize_term(phrase)
            if key not in vocabulary and canonical_term_key(key) not in _KNOWN_TERM_TAXONOMY:
                continue
            if is_hard_excluded(phrase) or any(a <= begin and end <= b for a, b in occupied):
                continue
            occupied.append((begin, end))
            if key not in seen:
                seen.add(key)
                candidates.append(phrase)
    return candidates


def classify_technical_terms(
    candidates,
    known_terms: Iterable | None = None,
    *,
    context: str = "",
    min_score: int = DEFAULT_MIN_TECHNICAL_TERM_SCORE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict:
    """Return the internal normalized record view used by app components."""

    return filter_technical_term_records(
        candidates,
        known_terms=known_terms,
        context=context,
        min_score=min_score,
        min_confidence=min_confidence,
    )


def _internal_record_to_wire(record: Mapping) -> dict:
    """Convert an internal record to the formal Part III JSON shape."""

    if isinstance(record.get("scores"), Mapping) and record.get("category"):
        scores = dict(record["scores"])
        return {
            "surface_form": _surface_term(record.get("surface_form") or record.get("term")),
            "canonical_term": _surface_term(record.get("canonical_term")),
            "category": record.get("category"),
            "scores": {
                name: int(scores[name])
                for name in (
                    "domain_specificity",
                    "conceptual_independence",
                    "terminological_stability",
                    "technical_informativeness",
                    "technical_term_score",
                )
            },
            "confidence": float(record.get("confidence", 0.0)),
            "reason": str(record.get("reason") or "Accepted as a stable domain or technical term."),
            "already_known": bool(record.get("already_known", False)),
            "learning_candidate": bool(record.get("learning_candidate", False)),
        }
    return {
        "surface_form": _surface_term(record.get("surface_form") or record.get("term")),
        "canonical_term": record.get("canonical_display") or canonical_term_display(record.get("canonical_term") or record.get("term")),
        "category": record.get("taxonomy"),
        "scores": {
            "domain_specificity": int(record.get("domain_specificity", 0)),
            "conceptual_independence": int(record.get("conceptual_independence", 0)),
            "terminological_stability": int(record.get("terminological_stability", 0)),
            "technical_informativeness": int(record.get("technical_informativeness", 0)),
            "technical_term_score": int(record.get("technical_term_score", 0)),
        },
        "confidence": float(record.get("confidence", 0.0)),
        "reason": str(record.get("reason") or "Accepted as a stable domain or technical term."),
        "already_known": bool(record.get("already_known", not record.get("learning_candidate", True))),
        "learning_candidate": bool(record.get("learning_candidate", False)),
    }


def _wire_envelope(records: dict, domain: str = "astronomy", subdomain: str | None = None) -> dict:
    """Build the formal extraction envelope from internal normalized records."""

    wire_terms = [_internal_record_to_wire(record) for record in records["technical_terms"]]
    return {
        "domain": str(domain),
        "subdomain": subdomain if subdomain is None or isinstance(subdomain, str) else str(subdomain),
        "terms": wire_terms,
    }


def technical_term_records_to_json(
    records: Iterable[Mapping] | None,
    domain: str = "astronomy",
    subdomain: str | None = None,
) -> dict:
    """Serialize internal records using the formal extraction contract."""

    return _wire_envelope(
        {"technical_terms": list(records or [])},
        domain=domain,
        subdomain=subdomain,
    )


serialize_technical_terms = technical_term_records_to_json


def extract_technical_term_result(
    text_or_candidates,
    known_terms: Iterable | None = None,
    *,
    max_words: int | None = None,
    min_score: int = DEFAULT_MIN_TECHNICAL_TERM_SCORE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    domain: str = "astronomy",
    subdomain: str | None = None,
) -> dict:
    """Extract technical terms into the formal JSON envelope."""

    if isinstance(text_or_candidates, str):
        text = text_or_candidates
        candidates = _candidate_strings_from_text(text, max_words=max_words)
    elif isinstance(text_or_candidates, Mapping):
        if any(key in text_or_candidates for key in ("title", "abstract", "abstract_snippet")):
            text = "\n\n".join(
                str(text_or_candidates.get(key) or "")
                for key in ("title", "abstract", "abstract_snippet")
            )
            candidates = _candidate_strings_from_text(text, max_words=max_words)
        else:
            text = ""
            candidates = [text_or_candidates]
    else:
        text = ""
        candidates = text_or_candidates or []
    internal = filter_technical_term_records(
        candidates,
        known_terms=known_terms,
        context=text,
        min_score=min_score,
        min_confidence=min_confidence,
    )
    return _wire_envelope(internal, domain=domain, subdomain=subdomain)


def extract_technical_terms(text_or_candidates, known_terms: Iterable | None = None, **kwargs) -> dict:
    """Extract terms using the formal ``domain/subdomain/terms`` JSON shape."""

    return extract_technical_term_result(text_or_candidates, known_terms=known_terms, **kwargs)


def select_technical_terms(candidates, known_terms: Iterable | None = None, **kwargs) -> list[dict]:
    """Return only the accepted technical-term records."""

    return extract_technical_term_result(candidates, known_terms=known_terms, **kwargs)["terms"]


def extract_candidate_strings(text: str, max_words: int | None = None) -> list[str]:
    """Expose the deterministic candidate generator for feedback learning."""

    return _candidate_strings_from_text(text, max_words=max_words)


_REQUIRED_RECORD_FIELDS = frozenset(
    {
        "term",
        "taxonomy",
        "domain_specificity",
        "conceptual_independence",
        "terminological_stability",
        "technical_informativeness",
        "technical_term_score",
        "confidence",
    }
)
_OPTIONAL_RECORD_FIELDS = frozenset(
    {"canonical_term", "surface_form", "hard_exclusion", "learning_candidate", "evidence"}
)
_WIRE_REQUIRED_RECORD_FIELDS = frozenset(
    {
        "surface_form",
        "canonical_term",
        "category",
        "scores",
        "confidence",
        "reason",
        "already_known",
        "learning_candidate",
    }
)
_WIRE_SCORE_FIELDS = frozenset(
    {
        "domain_specificity",
        "conceptual_independence",
        "terminological_stability",
        "technical_informativeness",
        "technical_term_score",
    }
)


def _strict_number(value, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TechnicalTermValidationError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < low or value > high:
        raise TechnicalTermValidationError(f"{name} must be between {low} and {high}")
    return value


def _strict_dimension(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2:
        raise TechnicalTermValidationError(f"{name} must be an integer from 0 to 2")
    return value


def validate_technical_term_record(
    item: Mapping,
    known_terms: Iterable | None = None,
    *,
    min_score: int = DEFAULT_MIN_TECHNICAL_TERM_SCORE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict | None:
    """Strictly validate one model-produced record.

    The preferred wire shape uses ``surface_form``/``category`` and a nested
    ``scores`` object.  The older flat ``term``/``taxonomy`` shape is also
    accepted by this low-level helper so local integrations can migrate
    without a flag day.  A well-formed but low-scoring/hard-excluded term
    returns ``None``; schema violations raise an exception.
    """

    if not isinstance(item, Mapping):
        raise TechnicalTermValidationError("each technical term must be an object")

    is_wire = any(name in item for name in ("surface_form", "category", "scores", "already_known"))
    if is_wire:
        keys = set(item)
        missing = _WIRE_REQUIRED_RECORD_FIELDS - keys
        if missing:
            raise TechnicalTermValidationError("missing fields: " + ", ".join(sorted(missing)))
        unknown = keys - _WIRE_REQUIRED_RECORD_FIELDS
        if unknown:
            raise TechnicalTermValidationError("unknown fields: " + ", ".join(sorted(unknown)))
        surface = item["surface_form"]
        canonical = item["canonical_term"]
        taxonomy = item["category"]
        scores = item["scores"]
        if not isinstance(surface, str) or not _surface_term(surface):
            raise TechnicalTermValidationError("surface_form must be a non-empty string")
        if "\n" in surface or "\r" in surface:
            raise TechnicalTermValidationError("surface_form must be a single line")
        if not isinstance(canonical, str) or not _surface_term(canonical):
            raise TechnicalTermValidationError("canonical_term must be a non-empty string")
        if not isinstance(scores, Mapping):
            raise TechnicalTermValidationError("scores must be an object")
        score_unknown = set(scores) - _WIRE_SCORE_FIELDS
        score_missing = _WIRE_SCORE_FIELDS - set(scores)
        if score_missing:
            raise TechnicalTermValidationError("missing score fields: " + ", ".join(sorted(score_missing)))
        if score_unknown:
            raise TechnicalTermValidationError("unknown score fields: " + ", ".join(sorted(score_unknown)))
        dimensions = {
            name: _strict_dimension(scores[name], name)
            for name in (
                "domain_specificity",
                "conceptual_independence",
                "terminological_stability",
                "technical_informativeness",
            )
        }
        score = scores["technical_term_score"]
        if isinstance(score, bool) or not isinstance(score, int) or score < 0 or score > 8:
            raise TechnicalTermValidationError("technical_term_score must be an integer from 0 to 8")
        reason = item["reason"]
        already_known_from_model = item["already_known"]
        learning_from_model = item["learning_candidate"]
        if not isinstance(reason, str):
            raise TechnicalTermValidationError("reason must be a string")
        if not isinstance(already_known_from_model, bool):
            raise TechnicalTermValidationError("already_known must be boolean")
        if not isinstance(learning_from_model, bool):
            raise TechnicalTermValidationError("learning_candidate must be boolean")
        hard_from_model = False
    else:
        keys = set(item)
        missing = _REQUIRED_RECORD_FIELDS - keys
        if missing:
            raise TechnicalTermValidationError("missing fields: " + ", ".join(sorted(missing)))
        unknown = keys - _REQUIRED_RECORD_FIELDS - _OPTIONAL_RECORD_FIELDS
        if unknown:
            raise TechnicalTermValidationError("unknown fields: " + ", ".join(sorted(unknown)))
        surface = item["term"]
        canonical = item.get("canonical_term") or surface
        taxonomy = item["taxonomy"]
        if not isinstance(surface, str) or not _surface_term(surface):
            raise TechnicalTermValidationError("term must be a non-empty string")
        if "\n" in surface or "\r" in surface:
            raise TechnicalTermValidationError("term must be a single line")
        if not isinstance(canonical, str) or not _surface_term(canonical):
            raise TechnicalTermValidationError("canonical_term must be a non-empty string")
        dimensions = {
            name: _strict_dimension(item[name], name)
            for name in (
                "domain_specificity",
                "conceptual_independence",
                "terminological_stability",
                "technical_informativeness",
            )
        }
        score = item["technical_term_score"]
        reason = str(item.get("reason", ""))
        already_known_from_model = bool(item.get("already_known", False))
        learning_from_model = bool(item.get("learning_candidate", True))
        hard_from_model = item.get("hard_exclusion", False)
        if not isinstance(hard_from_model, bool):
            raise TechnicalTermValidationError("hard_exclusion must be boolean")

    if taxonomy not in TECHNICAL_TERM_TAXONOMY:
        raise TechnicalTermValidationError(f"category/taxonomy must be one of {TECHNICAL_TERM_TAXONOMY}")
    expected = sum(dimensions.values())
    if score != expected:
        raise TechnicalTermValidationError(
            f"technical_term_score must equal the four-dimensional sum ({expected})"
        )
    confidence = _strict_number(item["confidence"], "confidence", 0.0, 1.0)
    key = canonical_term_key(canonical)
    if not key:
        raise TechnicalTermValidationError("canonical_term must not be empty")
    # A canonical term may intentionally normalize a surface form (MCMC,
    # Teff, plural forms, spelling variants), so equality is checked at the
    # semantic-key level only where an alias is known.  Arbitrary but
    # non-empty canonical forms are allowed for model-discovered terminology.
    known = _known_keys(known_terms)
    already_known = (
        _has_known_equivalent(key, known)
        if known_terms is not None else already_known_from_model
    )
    hard = (
        hard_from_model
        or is_hard_excluded(surface)
        or is_hard_excluded(canonical)
    )
    record = {
        "term": _surface_term(surface),
        "surface_form": _surface_term(surface),
        "canonical_term": key,
        "canonical_display": canonical_term_display(canonical),
        "taxonomy": taxonomy,
        **dimensions,
        "technical_term_score": score,
        "confidence": confidence,
        "hard_exclusion": hard,
        "already_known": already_known,
        "learning_candidate": not already_known,
        "reason": reason,
    }
    if hard or score < int(min_score) or confidence < float(min_confidence):
        return None
    return record


def _strip_json_wrapper(content: str) -> str:
    if not isinstance(content, str):
        raise TechnicalTermValidationError('technical-term response must be JSON text')
    text = (content or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1]).strip()
    return text


def normalize_technical_terms(terms: list, known_terms=None, *,
                              min_score=DEFAULT_MIN_TECHNICAL_TERM_SCORE,
                              min_confidence=DEFAULT_MIN_CONFIDENCE) -> list[dict]:
    """Validate persisted/public records without replacing their scores.

    None preserves a recorded known-term decision; an explicit vocabulary
    recalculates it. Invalid records fail the entire list, never get rescored.
    """
    if not isinstance(terms, list):
        raise TechnicalTermValidationError("terms must be a JSON array")
    known = _known_keys(known_terms)
    records = []
    for item in terms:
        if not isinstance(item, Mapping) or set(item) != _WIRE_REQUIRED_RECORD_FIELDS:
            raise TechnicalTermValidationError("terms must use the complete structured record schema")
        record = validate_technical_term_record(
            item, known_terms=known if known_terms is not None else None,
            min_score=min_score, min_confidence=min_confidence)
        if record is not None:
            records.append(record)
    # Preserve recorded known flags when equivalent surface forms merge.
    recorded_known = {r['canonical_term'] for r in records if r['already_known']}
    records = _prefer_specific_terms(records, known | recorded_known)
    for record in records:
        record['already_known'] = record['canonical_term'] in recorded_known
        record['learning_candidate'] = not record['already_known']
    return [_internal_record_to_wire(record) for record in records]


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TechnicalTermValidationError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def parse_technical_terms_response(
    content: str,
    known_terms: Iterable | None = None,
    *,
    min_score: int = DEFAULT_MIN_TECHNICAL_TERM_SCORE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict:
    """Strictly parse the formal ``domain/subdomain/terms`` response.

    The candidate split is recomputed from ``known_terms`` when the caller has
    a vocabulary; model-provided ``already_known``/``learning_candidate``
    flags are never allowed to override it.  Free-form prose, top-level
    arrays, malformed records, inconsistent scores, and unknown fields are
    rejected.  Hard-excluded records are valid candidates but are removed
    before the returned ``terms`` list is built.
    """

    text = _strip_json_wrapper(content)
    try:
        payload = json.loads(text, object_pairs_hook=_unique_json_object)
    except (TypeError, json.JSONDecodeError) as exc:
        raise TechnicalTermValidationError("technical-term response must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise TechnicalTermValidationError("technical-term response must be a JSON object")
    allowed_top_level = {"domain", "subdomain", "terms"}
    unknown = set(payload) - allowed_top_level
    if unknown:
        raise TechnicalTermValidationError("unknown top-level fields: " + ", ".join(sorted(unknown)))
    if "domain" not in payload or not isinstance(payload["domain"], str):
        raise TechnicalTermValidationError("domain must be a string")
    if "subdomain" not in payload or not (
        payload["subdomain"] is None or isinstance(payload["subdomain"], str)
    ):
        raise TechnicalTermValidationError("subdomain must be a string or null")
    if "terms" not in payload or not isinstance(payload["terms"], list):
        raise TechnicalTermValidationError("terms must be a JSON array")

    return {
        "domain": payload["domain"],
        "subdomain": payload["subdomain"],
        "terms": normalize_technical_terms(payload['terms'], known_terms,
                                          min_score=min_score, min_confidence=min_confidence),
    }


# Clear aliases make the parser discoverable to older callers/tests that use
# ``parse_*_json`` naming.
parse_technical_terms_json = parse_technical_terms_response
validate_technical_terms_response = parse_technical_terms_response


def build_term_extraction_prompt(paper_or_text, known_terms: Iterable | None = None) -> str:
    """Build the strict JSON prompt used by an OpenAI-compatible model."""

    if isinstance(paper_or_text, Mapping):
        title = str(paper_or_text.get("title") or "")
        abstract = str(paper_or_text.get("abstract") or paper_or_text.get("abstract_snippet") or "")
        source = f"Title: {title}\nAbstract: {abstract[:4000]}"
    else:
        source = str(paper_or_text or "")[:5000]
    known = []
    if isinstance(known_terms, (str, Mapping)):
        known_terms = [known_terms]
    for item in known_terms or []:
        if isinstance(item, Mapping):
            item = item.get("canonical_term") or item.get("term")
        display = _surface_term(item)
        if display and display not in known:
            known.append(display)
    known_block = ", ".join(known[:80]) if known else "(none)"
    taxonomy_block = "\n".join(f"- {name}" for name in TECHNICAL_TERM_TAXONOMY)
    return f"""You are a domain terminology extraction system for astronomy.

Your task is NOT to extract general keywords, salient words, or frequent
words. Extract only domain-specific or technical terminology that denotes a
recognizable and relatively stable professional concept, physical quantity,
process, phenomenon, method, technique, algorithm, model, instrument,
software package, dataset, technical resource, or formally defined object or
class. Precision is more important than recall.

Use exactly one category from this taxonomy:
{taxonomy_block}

Hard-exclude ordinary language, generic academic vocabulary, broad field
nouns, article-structure words, and descriptive phrases created only by the
local sentence. Examples normally excluded are: result, study, analysis,
method, approach, model, data, sample, observation, measurement, parameter,
value, calculation, comparison, uncertainty, important, high, low, star,
galaxy, spectrum, telescope, and "new method". A generic word may be kept
only when it forms a stable, sufficiently specific technical expression, such
as stellar atmosphere model, carbon abundance, radiative transfer, or
high-resolution spectroscopy. Hard exclusion overrides every numerical score.

Prefer the most specific meaningful canonical term. Do not emit generic
constituents, do not artificially extend a term with surrounding prose, and
deduplicate acronym/expanded variants into one term object. For example,
MCMC should have surface_form "MCMC" and canonical_term "Markov chain Monte
Carlo"; Teff should have canonical_term "effective temperature".

Score each accepted candidate on these four dimensions from 0 to 2:
domain_specificity, conceptual_independence, terminological_stability, and
technical_informativeness. technical_term_score MUST equal their sum (0-8).
The default acceptance gate is technical_term_score >= 6 and confidence >=
0.70. Known terms remain in terms but must have already_known=true and
learning_candidate=false.

Known or already-learned terms:
{known_block}

Return ONLY valid JSON matching this exact shape, with no prose or Markdown:
{{
  "domain": "astronomy",
  "subdomain": "stellar astrophysics",
  "terms": [
    {{
      "surface_form": "MCMC",
      "canonical_term": "Markov chain Monte Carlo",
      "category": "algorithm_or_model",
      "scores": {{
        "domain_specificity": 2,
        "conceptual_independence": 2,
        "terminological_stability": 2,
        "technical_informativeness": 2,
        "technical_term_score": 8
      }},
      "confidence": 0.95,
      "reason": "A stable named inference algorithm used in scientific modeling.",
      "already_known": false,
      "learning_candidate": true
    }}
  ]
}}

Paper text:
{source}
"""


def technical_terms_to_keywords(records: Iterable[Mapping] | None) -> list[str]:
    """Return display terms from records for legacy keyword consumers."""

    out = []
    seen = set()
    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        term = _surface_term(record.get("term") or record.get("canonical_term"))
        key = canonicalize_term(term)
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return out
