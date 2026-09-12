"""Exercise the BibTeX API used by profile import against release dependencies."""
from src.profile import parse_bib, build_profile


def test_bibtex_profile_import(tmp_path):
    bib = tmp_path / 'library.bib'
    bib.write_text('@article{star, title={Metal-poor stars}, author={Doe, Jane},\n'
                   'year={2026}, primaryclass={astro-ph.SR}, keywords={stellar abundances}}', encoding='utf-8')
    entries = parse_bib(str(bib))
    assert entries[0]['ID'] == 'star'
    assert entries[0]['title'] == 'Metal-poor stars'
    assert entries[0]['primaryclass'] == 'astro-ph.SR'
    assert build_profile(str(bib))
