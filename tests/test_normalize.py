"""Normalization is the foundation of Arabic recall -- every rule gets a test."""
import pytest

from gala.normalize import (
    is_arabic, match_variants, normalize, romanize, search_text, strip_article, tokenize,
)


@pytest.mark.parametrize("variant", [
    "مركز المملكة",         # plain
    "مَرْكَزُ الْمَمْلَكَة",   # fully vocalised
    "مـــركـــز المملكة",    # tatweel padding
    "مركز المملكه",         # teh marbuta written as heh
])
def test_arabic_variants_fold_together(variant):
    """Every common way of writing one name must reach the same key."""
    assert normalize(variant) == normalize("مركز المملكة")


def test_alef_forms_fold():
    assert normalize("أحمد") == normalize("احمد") == normalize("إحمد") == normalize("آحمد")


def test_arabic_indic_digits_become_ascii():
    assert normalize("كافيه ١٢٣") == "كافيه 123"
    assert normalize("۴۵۶") == "456"  # extended (Persian) digits too


def test_latin_is_casefolded_and_depunctuated():
    assert normalize("Al-Nakheel  Mall!") == "al nakheel mall"


def test_presentation_forms_normalize():
    """NFKC must run first, or Arabic presentation forms miss every later rule."""
    assert normalize("ﺱﻼﻡ") == normalize("سلام")


def test_empty_and_none():
    assert normalize(None) == ""
    assert normalize("") == ""
    assert tokenize(None) == []


def test_definite_article_is_indexed_both_ways():
    """Users search "نخيل مول" for a place stored as "النخيل مول"."""
    tokens = tokenize("النخيل مول")
    assert "النخيل" in tokens and "نخيل" in tokens


def test_strip_article_leaves_short_stems_alone():
    assert strip_article("الرياض") == "رياض"
    assert strip_article("الله") == "الله"


def test_cross_script_synonyms_unify():
    """An Arabic query must reach an English-named coffee shop."""
    assert set(tokenize("coffee")) & set(tokenize("كافيه"))
    assert set(tokenize("pharmacy")) & set(tokenize("صيدلية"))


def test_tokenize_deduplicates():
    """Expansion must not inflate BM25 term frequency."""
    tokens = tokenize("قهوة قهوة")
    assert len(tokens) == len(set(tokens))


def test_search_text_merges_fields():
    blob = search_text("شنب كافيه", "hookah_bar", "Olaya St")
    assert "قهوه" in blob and "olaya" in blob


def test_romanize_bridges_scripts():
    from rapidfuzz import fuzz
    for arabic, latin in [
        ("كوفي هيل", "Coffee Hill"),
        ("فندق النخيل", "Al Nakheel Hotel"),
        ("صيدلية النهدي", "Al Nahdi Pharmacy"),
    ]:
        assert fuzz.token_set_ratio(romanize(arabic), latin.lower()) >= 80


def test_romanize_ignores_latin_input():
    assert romanize("Coffee Hill") == ""
    assert romanize(None) == ""


def test_match_variants_carries_both_scripts():
    variants = match_variants("كوفي هيل", None, "Coffee Hill")
    assert "coffee hill" in variants
    assert any(v.startswith("coffee") and v != "coffee hill" for v in variants)


def test_is_arabic():
    assert is_arabic("مركز")
    assert not is_arabic("Mall")
    assert not is_arabic("")
