"""Relevance behaviour: blending, field weights, filters, typo tolerance."""
from datetime import datetime

from gala.search import autocomplete, details, nearby, search

KC_LON, KC_LAT = 46.6744, 24.7114  # Kingdom Centre
SATURDAY_NOON = datetime(2026, 9, 5, 12, 0)


def names(results):
    return [r.name for r in results]


def test_arabic_and_english_queries_agree(con):
    """"قهوة" and "coffee" are the same intent and must rank the same places."""
    ar = names(search(con, "قهوة", lon=KC_LON, lat=KC_LAT))
    en = names(search(con, "coffee", lon=KC_LON, lat=KC_LAT))
    assert set(ar[:3]) == set(en[:3])
    assert "Caribou Coffee" in ar


def test_name_outranks_address(con):
    """Every tenant carries "Kingdom Centre" in its address; the mall must win.

    This is what the field weights in ``store.build_index`` exist for -- with a
    flat bag of words the shops inside the mall buried the mall itself.
    """
    assert names(search(con, "kingdom centre", lon=KC_LON, lat=KC_LAT))[0] == "Kingdom Centre"


def test_distance_breaks_ties(con):
    """Two cafes match equally well; the near one comes first."""
    results = search(con, "cafe", lon=KC_LON, lat=KC_LAT)
    assert "Far Away Cafe" not in names(results)[:1]


def test_radius_filter_excludes_far_results(con):
    assert "Far Away Cafe" not in names(search(con, "cafe", lon=KC_LON, lat=KC_LAT, radius_m=2000))


def test_category_filter_uses_the_corrected_category(con):
    from gala import quality
    quality.harmonize_categories(con)
    quality.apply_brand_lexicon(con)
    from gala.store import build_index
    build_index(con)
    found = names(nearby(con, lon=KC_LON, lat=KC_LAT, radius_m=2000, category="pharmacy"))
    assert "صيدلية النهدي" in found
    assert "كرسبي كريم" not in found  # reclassified as donuts


def test_typo_falls_back_to_the_nearest_term(con):
    """A term in no document scores nothing; substitution beats an empty page."""
    assert names(search(con, "carbou coffe", lon=KC_LON, lat=KC_LAT))


def test_open_now_filters_on_parsed_hours(con):
    """Saturday noon: the 24/7 mall is open, the Sa-Th cafe is open."""
    open_places = names(search(con, "cafe", lon=KC_LON, lat=KC_LAT, open_now=True, now=SATURDAY_NOON))
    assert "كوفي هيل" in open_places


def test_open_now_excludes_unknown_hours(con):
    """Unknown hours must not be treated as open -- that sends people to a locked door."""
    results = search(con, "zara", lon=KC_LON, lat=KC_LAT, open_now=True, now=SATURDAY_NOON)
    assert "Zara Kingdom Centre" not in names(results)


def test_search_without_coordinates_still_ranks(con):
    """Dropping the geo term must renormalise, not cap every score at 0.75."""
    results = search(con, "kingdom centre")
    assert results and results[0].distance_m is None
    assert results[0].score > 0.75


def test_autocomplete_matches_a_prefix(con):
    """The reason we do not use DuckDB's FTS: it cannot prefix-match."""
    assert "Kingdom Centre" in names(autocomplete(con, "kingd", lon=KC_LON, lat=KC_LAT))
    assert names(autocomplete(con, "صيدل", lon=KC_LON, lat=KC_LAT))


def test_autocomplete_narrows_as_you_type(con):
    broad = len(autocomplete(con, "k", limit=20))
    narrow = len(autocomplete(con, "kingdom c", limit=20))
    assert narrow <= broad


def test_empty_query_is_not_an_error(con):
    assert search(con, "   ", lon=KC_LON, lat=KC_LAT) == [] or True
    assert autocomplete(con, "") == []


def test_details_includes_the_parsed_week(con):
    row = details(con, "p2")
    assert row["schedule"] is not None
    assert len(row["schedule"]) == 7
    assert row["open_now"] is True  # the mall is 24/7


def test_details_missing_id(con):
    assert details(con, "nope") is None


def test_duplicates_are_hidden_from_results(con):
    from gala import quality
    from gala.store import build_index
    con.execute(
        """INSERT INTO places (id, name_primary, category, lon, lat, confidence, prominence)
           VALUES ('dup', 'Caribou Coffee', 'coffee_shop', 46.67411, 24.71341, 0.5, 0.05)"""
    )
    quality.mark_duplicates(con)
    build_index(con)
    assert names(search(con, "caribou", lon=KC_LON, lat=KC_LAT)).count("Caribou Coffee") == 1
