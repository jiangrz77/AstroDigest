"""Regression coverage for terms moving from ranking to persisted feedback."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import gui, preference_learning as pl, ranker
from src.digest_parser import parse_digest
from src.output import generate_markdown_digest, write_digest
from src.technical_terms import (
    canonical_term_key, extract_technical_terms, parse_technical_terms_response,
    TechnicalTermValidationError,
)


def term_keys(records):
    return {canonical_term_key(r['canonical_term']) for r in records}


def record(surface='heteroscedastic Gaussian likelihood'):
    return dict(surface_form=surface, canonical_term=surface,
                category='algorithm_or_model', scores=dict(
                    domain_specificity=2, conceptual_independence=2,
                    terminological_stability=1, technical_informativeness=1,
                    technical_term_score=6), confidence=.71,
                reason='A qualified statistical model.',
                already_known=False, learning_candidate=True)


@pytest.mark.parametrize('term', [
    'principal component analysis', 'convolutional neural network',
    'Markov chain Monte Carlo', 'The Payne', 'curve of growth',
    'non-LTE radiative transfer', 'stellar atmosphere model',
])
def test_complete_terms_learned_from_two_legacy_feedback_papers(term):
    entries = [dict(paper_id=pid, title=term, action='underrated') for pid in ('a', 'b')]
    result = pl.derive_learned_profile(entries)
    assert canonical_term_key(term) in result['keyword_weights']
    assert canonical_term_key(term) in term_keys(result['technical_terms'])
    assert not pl.derive_learned_profile([entries[0], entries[0]])['keyword_weights']


@pytest.mark.parametrize('text', [
    'new stars', 'observed stars', 'very strong feature', 'best fitting result',
    'stars in our sample', 'method used in this work', 'spectrum', 'ordinary123',
])
def test_descriptions_are_not_technical_terms_even_with_model_scores(text):
    assert not extract_technical_terms([text])['terms']
    if text != 'ordinary123':
        envelope = dict(domain='astronomy', subdomain=None, terms=[record(text)])
        assert not parse_technical_terms_response(json.dumps(envelope))['terms']


@pytest.mark.parametrize('text', ['carbon. abundance', 'carbon, abundance',
                                'principal component. analysis'])
def test_extraction_does_not_join_sentence_or_clause_boundaries(text):
    assert not extract_technical_terms(text)['terms']


def test_longest_complete_phrase_and_acronym_share_one_concept():
    terms = extract_technical_terms('Principal component analysis (PCA) with The Payne.')['terms']
    assert term_keys(terms) == {'principal component analysis', 'the payne'}
    assert len(terms) == 2


def test_stored_scores_are_not_replaced_by_lexical_rescoring():
    qualified = record()
    feedback = [dict(paper_id=pid, title='Only a short excerpt',
                     technical_terms=[qualified], action='underrated') for pid in ('a', 'b')]
    result = pl.derive_learned_profile(feedback)
    assert canonical_term_key(qualified['canonical_term']) in result['keyword_weights']
    assert result['technical_terms'][0]['scores'] == qualified['scores']
    assert result['technical_terms'][0]['confidence'] == .71


def test_empty_and_invalid_stored_terms_cannot_fall_back_to_text():
    for terms in ([], [dict(record(), scores={'technical_term_score': 8})]):
        feedback = [dict(paper_id=pid, title='Principal component analysis',
                         technical_terms=terms, action='underrated') for pid in ('a', 'b')]
        assert not pl.derive_learned_profile(feedback)['keyword_weights']


def test_pca_full_name_and_manual_ignore_share_one_weight():
    feedback = [dict(paper_id=pid, title=title, action='underrated')
                for pid, title in [('a', 'PCA'), ('b', 'Principal component analysis')]]
    learned = pl.derive_learned_profile(feedback)
    assert set(learned['keyword_weights']) == {'principal component analysis'}
    ignored = pl.derive_learned_profile(feedback, manual={'keyword_weights': {'PCA': None}})
    assert not ignored['keyword_weights']


def papers():
    return [dict(id=pid, title='Stellar parameters', authors=['Fixture Author'],
                 categories=['astro-ph.SR'], score=2, reason='Fixture', abstract=(
                     'A general report. ' * 60 +
                     'Effective temperature from principal component analysis (PCA), '
                     'The Payne and spectral synthesis.'))
            for pid in ('2609.99001', '2609.99002')]


@pytest.fixture
def feedback_client(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, '_needs_setup', lambda: False)
    monkeypatch.setattr(gui, 'FEEDBACK_FILE', str(tmp_path / 'feedback.json'))
    monkeypatch.setattr(pl, 'FEEDBACK_FILE', str(tmp_path / 'feedback.json'))
    monkeypatch.setattr(pl, 'LEARNED_PROFILE_FILE', str(tmp_path / 'learned_profile.json'))
    cfg = {'keywords': ['effective temperature']}
    monkeypatch.setattr(gui, '_load_config_and_env', lambda: (cfg, {}))
    monkeypatch.setattr(gui, 'get_digest_path_for_date',
                        lambda day: str(tmp_path / f'digest_{day}.md'))
    monkeypatch.setattr(gui, 'get_adjustment', lambda *_: 0)
    monkeypatch.setattr(gui, 'record_adjustment', lambda day, pid, amount: amount)
    monkeypatch.setattr(gui, '_handle_figure_rating_change', lambda *_: 'none')
    def full(day, digest_path=None):
        file = tmp_path / f'digest_{day}.full.json'
        return json.loads(file.read_text()) if file.exists() else {}
    monkeypatch.setattr(gui, '_load_full_abstracts', full)
    return gui.app.test_client()


def test_rank_save_reload_feedback_and_next_ranking(tmp_path, monkeypatch, feedback_client):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps([
        dict(index=i, score=2, reason='Fixture') for i in range(2)])))])
    monkeypatch.setenv('TERM_TEST_API_KEY', 'offline-fixture')
    monkeypatch.setattr(ranker, 'get_client', lambda *_: Mock())
    monkeypatch.setattr(ranker, '_create_completion', lambda *_: response)
    monkeypatch.setattr(ranker, 'ensure_learned_profile', lambda **_: {})
    monkeypatch.setattr(ranker, '_load_feedback_text', lambda: '')
    from src.profile import build_profile_from_config
    profile = build_profile_from_config({'keywords': ['effective temperature']})
    ranked = ranker.rank_papers(papers(), profile, {'api_key_env': 'TERM_TEST_API_KEY'})
    original = ranked[0]['technical_terms']
    path = write_digest(ranked, str(tmp_path), digest_date='2026-09-10')
    restored = parse_digest(path)['tiers'][0]['papers']
    assert restored[0]['technical_terms'] == original
    for paper in restored:
        assert 'principal component analysis' not in paper['abstract'].lower()
        response = feedback_client.post('/feedback', json=dict(
            paper_id=paper['paper_id'], date='2026-09-10', action='underrated',
            title='Untrusted client title', technical_terms=[record('new stars')]))
        assert response.status_code == 200
    history = pl.load_feedback()
    assert history[0]['title'] == 'Stellar parameters'
    assert history[0]['technical_terms'] == original
    result = feedback_client.get('/learned-profile').get_json()
    assert 'principal component analysis' in result['keyword_weights']
    assert 'the payne' in result['keyword_weights']
    assert 'effective temperature' in term_keys(result['technical_terms'])
    assert 'effective temperature' not in term_keys(result['learning_candidates'])
    assert pl.compute_adjustment(dict(title='', abstract='', technical_terms=original), result) > .2
    assert pl.load_learned_profile()['keyword_weights'] == result['keyword_weights']


def test_old_digest_feedback_uses_full_abstract(tmp_path, feedback_client):
    write_digest(papers(), str(tmp_path), digest_date='2026-09-10')
    for paper in papers():
        response = feedback_client.post('/feedback', json=dict(
            paper_id=paper['id'], date='2026-09-10', action='underrated'))
        assert response.status_code == 200
    result = pl.load_learned_profile()
    assert 'principal component analysis' in result['keyword_weights']


@pytest.mark.parametrize('score', [0, 1, 2, 5])
def test_terms_survive_every_digest_tier(tmp_path, score):
    paper = dict(papers()[0], score=score, scoring_failed=score == 0,
                 technical_terms=extract_technical_terms('The Payne and PCA')['terms'])
    path = write_digest([paper], str(tmp_path), digest_date='2026-09-10')
    restored = parse_digest(path)['tiers'][0]['papers'][0]
    assert restored['technical_terms'] == paper['technical_terms']


def test_term_metadata_cannot_escape_markdown_comment_or_replace_existing_digest(tmp_path):
    paper = dict(papers()[0], technical_terms=[dict(record(), reason='Note --> <script>bad</script>')])
    path = write_digest([paper], str(tmp_path), digest_date='2026-09-10')
    before = open(path).read()
    assert '<script>' not in before
    assert parse_digest(path)['tiers'][0]['papers'][0]['technical_terms'][0]['reason'] == paper['technical_terms'][0]['reason']
    paper['technical_terms'][0]['scores']['technical_term_score'] = 99
    with pytest.raises(TechnicalTermValidationError):
        write_digest([paper], str(tmp_path), digest_date='2026-09-10')
    assert open(path).read() == before


def test_metadata_does_not_change_score_parsing_and_corruption_is_rejected(tmp_path):
    paper = dict(papers()[0], technical_terms=[dict(record(), reason='**Score:** No score')])
    path = write_digest([paper], str(tmp_path), digest_date='2026-09-10')
    restored = parse_digest(path)['tiers'][0]['papers'][0]
    assert restored['score'] == 2
    assert restored['scoring_failed'] is False
    from pathlib import Path
    file = Path(path)
    file.write_text(file.read_text().replace('"technical_term_score": 6', '"technical_term_score": 99'))
    assert parse_digest(path)['tiers'][0]['papers'][0]['technical_terms'] == []


def test_stored_known_term_remains_known_without_config_anchor():
    known = dict(record(), already_known=True, learning_candidate=False)
    feedback = [dict(paper_id=pid, title='Excerpt', technical_terms=[known],
                     action='underrated') for pid in ('a', 'b')]
    result = pl.derive_learned_profile(feedback, config_keywords=[])
    assert result['technical_terms'][0]['already_known'] is True
    assert result['learning_candidates'] == []


def test_profile_migration_relearns_full_phrases_and_keeps_manual(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, 'FEEDBACK_FILE', str(tmp_path / 'feedback.json'))
    monkeypatch.setattr(pl, 'LEARNED_PROFILE_FILE', str(tmp_path / 'learned_profile.json'))
    pl.save_feedback([dict(paper_id=pid, title='Principal component analysis',
                           action='underrated') for pid in ('a', 'b')])
    pl.save_learned_profile(dict(tuning_version=3, term_schema_version='technical-terms.v1',
                                keyword_weights={'new stars': {'weight': 2}},
                                manual={'category_weights': {'astro-ph.SR': 1.3}}))
    result = pl.ensure_learned_profile(config_keywords=[])
    assert set(result['keyword_weights']) == {'principal component analysis'}
    assert result['category_weights']['astro-ph.SR']['weight'] == 1.3


def test_metadata_survives_staged_publication(tmp_path, monkeypatch):
    staged = tmp_path / 'staged'
    destination = tmp_path / 'published'
    paper = dict(papers()[0], technical_terms=extract_technical_terms('The Payne and PCA')['terms'])
    write_digest([paper], str(staged / 'digests'), digest_date='2026-09-10')
    monkeypatch.setattr(gui, '_load_config_and_env', lambda: ({'output': {
        'digest_dir': str(destination), 'bibtex_dir': str(tmp_path / 'bib')
    }}, {}))
    paths = gui._commit_staged_outputs(str(staged))
    restored = parse_digest(paths['digests'][0])['tiers'][0]['papers'][0]
    assert restored['technical_terms'] == paper['technical_terms']


def test_legacy_custom_digest_directory_uses_its_own_full_abstract(tmp_path, monkeypatch):
    path = write_digest(papers(), str(tmp_path / 'custom'), digest_date='2026-09-10')
    monkeypatch.setattr(gui, '_PROJECT_DIR', tmp_path / 'unrelated')
    full = gui._load_full_abstracts('2026-09-10', digest_path=path)
    assert 'principal component analysis' in full['2609.99001']


def test_cancel_feedback_rebuilds_learned_terms(tmp_path, feedback_client):
    write_digest(papers(), str(tmp_path), digest_date='2026-09-10')
    for paper in papers():
        feedback_client.post('/feedback', json=dict(paper_id=paper['id'], date='2026-09-10', action='underrated'))
    assert 'principal component analysis' in pl.load_learned_profile()['keyword_weights']
    response = feedback_client.post('/feedback', json=dict(paper_id=papers()[0]['id'], date='2026-09-10', action='cancel'))
    assert response.status_code == 200
    assert 'principal component analysis' not in pl.load_learned_profile()['keyword_weights']


def test_confidence_is_not_rounded_up_across_acceptance_boundary():
    assert extract_technical_terms([dict(record('spectral synthesis'), confidence=.6999)])['terms'] == []
