"""Coverage for the domain/technical-term extraction contract."""

import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import preference_learning as pl
from src.profile import build_profile_from_config
from src.technical_terms import (
    TECHNICAL_TERM_TAXONOMY,
    TechnicalTermValidationError,
    build_term_extraction_prompt,
    canonicalize_term,
    extract_technical_terms,
    filter_technical_term_records,
    is_hard_excluded,
    parse_technical_terms_response,
    score_term,
)


def _record(term, taxonomy, score=8, confidence=0.9, **extra):
    dimensions = {
        "domain_specificity": 2,
        "conceptual_independence": 2,
        "terminological_stability": 2,
        "technical_informativeness": 2,
    }
    if score != 8:
        # Keep test fixtures honest while making it easy to create a lower
        # passing record.
        dimensions = {
            "domain_specificity": 2,
            "conceptual_independence": 2,
            "terminological_stability": 1,
            "technical_informativeness": score - 5,
        }
    return {
        "surface_form": term,
        "canonical_term": term,
        "category": taxonomy,
        "scores": {**dimensions, "technical_term_score": score},
        "confidence": confidence,
        "reason": "A stable technical term.",
        "already_known": False,
        "learning_candidate": True,
        **extra,
    }


def test_taxonomy_is_exactly_the_eight_part_contract():
    assert set(TECHNICAL_TERM_TAXONOMY) == {
        "scientific_concept",
        "physical_quantity",
        "process_or_phenomenon",
        "method_or_technique",
        "algorithm_or_model",
        "instrument_or_software",
        "dataset_or_resource",
        "domain_object_or_class",
    }


def test_positive_astrophysics_terms_receive_structured_scores():
    result = extract_technical_terms([
        "chemical evolution",
        "effective temperature",
        "spectral synthesis",
        "MCMC",
        "Gaia DR3",
        "CEMP-no stars",
    ])
    by_key = {canonicalize_term(item["canonical_term"]): item for item in result["terms"]}
    for term in ("chemical evolution", "effective temperature", "spectral synthesis", "markov chain monte carlo", "gaia dr3", "cemp-no star"):
        assert term in by_key
        item = by_key[term]
        assert item["scores"]["technical_term_score"] == sum(
            item["scores"][name]
            for name in (
                "domain_specificity",
                "conceptual_independence",
                "terminological_stability",
                "technical_informativeness",
            )
        )
        assert item["scores"]["technical_term_score"] >= 6
        assert item["confidence"] >= 0.70


def test_hard_exclusions_win_over_score_and_cover_boundaries():
    for term in ("study", "analysis", "results", "method", "data", "model", "star", "galaxy", "high redshift"):
        assert is_hard_excluded(term)
    accepted = extract_technical_terms([
        "chemical evolution model",  # generic trailing constituent
        "chemical evolution",
        "high-redshift galaxies",
        "new method",
    ])
    keys = {canonicalize_term(item["canonical_term"]) for item in accepted["terms"]}
    assert "chemical evolution" in keys
    assert "chemical evolution model" not in keys
    assert "high-redshift galaxies" in keys
    assert "new method" not in keys

    # A model cannot smuggle a hard exclusion through an otherwise perfect
    # structured score.
    payload = {
        "domain": "astronomy",
        "subdomain": "stellar astrophysics",
        "terms": [_record("analysis", "scientific_concept")],
    }
    assert parse_technical_terms_response(json.dumps(payload))["terms"] == []


def test_canonicalization_and_acronym_deduplication_prefer_expanded_term():
    assert canonicalize_term("  Markov\u2013chain  Monte Carlo ") == "markov-chain monte carlo"
    result = filter_technical_term_records([
        "Markov chain Monte Carlo",
        "MCMC",
        "markov chain monte carlo",
    ])
    assert [item["canonical_term"] for item in result["technical_terms"]] == [
        "markov chain monte carlo"
    ]


def test_most_specific_meaningful_multi_word_term_suppresses_constituent():
    result = filter_technical_term_records(["neutron star", "neutron star merger", "star"])
    keys = {item["canonical_term"] for item in result["technical_terms"]}
    assert "neutron star merger" in keys
    assert "neutron star" not in keys
    assert "star" not in keys


def test_known_terms_stay_accepted_but_are_not_learning_candidates():
    result = filter_technical_term_records(
        ["spectral synthesis", "chemical evolution"],
        known_terms=["spectral synthesis"],
    )
    records = {item["canonical_term"]: item for item in result["technical_terms"]}
    assert records["spectral synthesis"]["learning_candidate"] is False
    assert records["chemical evolution"]["learning_candidate"] is True
    assert [item["canonical_term"] for item in result["learning_candidates"]] == [
        "chemical evolution"
    ]


def test_strict_parser_rejects_malformed_json_or_inconsistent_dimensions():
    with pytest.raises(TechnicalTermValidationError):
        parse_technical_terms_response("not json")
    with pytest.raises(TechnicalTermValidationError):
        parse_technical_terms_response(json.dumps({
            "domain": "astronomy",
            "subdomain": None,
            "terms": [dict(
                _record("spectral synthesis", "method_or_technique"),
                scores={
                    "domain_specificity": 2,
                    "conceptual_independence": 2,
                    "terminological_stability": 2,
                    "technical_informativeness": 2,
                    "technical_term_score": 7,
                },
            )],
        }))
    with pytest.raises(TechnicalTermValidationError):
        parse_technical_terms_response(json.dumps({
            "domain": "astronomy", "subdomain": None, "terms": [], "unexpected": True
        }))


def test_formal_parser_normalizes_acronym_and_recomputes_known_flag():
    payload = {
        "domain": "astronomy",
        "subdomain": "stellar spectroscopy",
        "terms": [{
            "surface_form": "MCMC",
            "canonical_term": "Markov chain Monte Carlo",
            "category": "algorithm_or_model",
            "scores": {
                "domain_specificity": 2,
                "conceptual_independence": 2,
                "terminological_stability": 2,
                "technical_informativeness": 2,
                "technical_term_score": 8,
            },
            "confidence": 0.95,
            "reason": "A stable inference algorithm.",
            "already_known": False,
            "learning_candidate": True,
        }],
    }
    result = parse_technical_terms_response(json.dumps(payload), known_terms=["MCMC"])
    assert result["terms"][0]["surface_form"] == "MCMC"
    assert result["terms"][0]["canonical_term"] == "Markov chain Monte Carlo"
    assert result["terms"][0]["already_known"] is True
    assert result["terms"][0]["learning_candidate"] is False


def test_prompt_requires_json_and_documents_the_taxonomy_and_gate():
    prompt = build_term_extraction_prompt(
        {"title": "A paper", "abstract": "We study spectral synthesis."},
        known_terms=["spectral synthesis"],
    )
    assert '"terms"' in prompt
    assert "domain_specificity" in prompt
    assert "spectral synthesis" in prompt
    assert "Hard-exclude" in prompt
    assert "Return ONLY" in prompt


def test_profile_keeps_legacy_keywords_counter_but_filters_non_terms():
    profile = build_profile_from_config({
        "arxiv_categories": ["astro-ph.SR"],
        "keywords": ["analysis", "galaxy", "chemical evolution", "MCMC", "Population III"],
    })
    assert "analysis" not in profile["keywords"]
    assert "galaxy" not in profile["keywords"]
    assert "chemical evolution" in profile["keywords"]
    assert "MCMC" in profile["keywords"]
    assert all(item["learning_candidate"] is False for item in profile["technical_terms"])


def test_feedback_learning_discovers_only_repeated_technical_terms():
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    feedback = [
        {
            "paper_id": "a",
            "title": "Chemical evolution from spectral synthesis",
            "abstract_snippet": "The study reports results and data.",
            "action": "underrated",
            "timestamp": timestamp,
        },
        {
            "paper_id": "b",
            "title": "Chemical evolution from spectral synthesis",
            "abstract_snippet": "The study reports results and data.",
            "action": "underrated",
            "timestamp": timestamp,
        },
    ]
    profile = pl.derive_learned_profile(
        feedback,
        config_keywords=["chemical evolution"],
        now=datetime.now(timezone.utc),
    )
    records = {item["canonical_term"]: item for item in profile["technical_terms"]}
    assert records["chemical evolution"]["learning_candidate"] is False
    assert records["spectral synthesis"]["learning_candidate"] is True
    assert "study" not in profile["keyword_weights"]
    assert "results" not in profile["keyword_weights"]
    assert "data" not in profile["keyword_weights"]
    assert profile["keyword_weights"]["chemical evolution"]["weight"] > 1.0
