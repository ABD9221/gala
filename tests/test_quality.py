"""Corpus repair: category correction and duplicate marking."""
from gala import quality
from gala.ingest.overture import clean_phone


def test_osm_category_maps_only_unambiguous_tags():
    assert quality.osm_category({"amenity": "fast_food"}) == "fast_food_restaurant"
    assert quality.osm_category({"shop": "mall"}) == "shopping_center"
    assert quality.osm_category({"highway": "primary"}) is None
    assert quality.osm_category({}) is None


def test_brand_lexicon_fixes_uniformly_mislabelled_chains(con):
    """Overture files Krispy Kreme as `pharmacy` in every record it has."""
    quality.harmonize_categories(con)
    assert quality.apply_brand_lexicon(con) >= 1
    row = con.execute(
        "SELECT category_final, category_source FROM places WHERE name_primary = 'كرسبي كريم'"
    ).fetchone()
    assert row == ("donuts", "brand_lexicon")


def test_real_pharmacy_survives_the_correction(con):
    quality.harmonize_categories(con)
    quality.apply_brand_lexicon(con)
    category = con.execute(
        "SELECT category_final FROM places WHERE name_primary = 'صيدلية النهدي'"
    ).fetchone()[0]
    assert category == "pharmacy"


def test_osm_override_beats_overture(con):
    quality.harmonize_categories(con)
    corrected = quality.apply_osm_categories(con, {"p5": {"amenity": "fast_food"}})
    assert corrected == 1
    assert con.execute("SELECT category_final, category_source FROM places WHERE id='p5'").fetchone() \
        == ("fast_food_restaurant", "osm")


def test_corrections_never_overwrite_the_source(con):
    """The original category stays readable so a bad rule can be rolled back."""
    quality.harmonize_categories(con)
    quality.apply_brand_lexicon(con)
    original = con.execute("SELECT category FROM places WHERE name_primary = 'كرسبي كريم'").fetchone()[0]
    assert original == "pharmacy"


def test_duplicates_are_marked_not_deleted(con):
    before = con.execute("SELECT count(*) FROM places").fetchone()[0]
    con.execute(
        """INSERT INTO places (id, name_primary, category, lon, lat, confidence, prominence)
           VALUES ('dup', 'Caribou Coffee', 'coffee_shop', 46.67411, 24.71341, 0.5, 0.1)"""
    )
    assert quality.mark_duplicates(con) == 1
    assert con.execute("SELECT count(*) FROM places").fetchone()[0] == before + 1
    assert con.execute("SELECT duplicate_of FROM places WHERE id='dup'").fetchone()[0] == "p0"


def test_distant_namesakes_are_not_duplicates(con):
    con.execute(
        """INSERT INTO places (id, name_primary, category, lon, lat, confidence, prominence)
           VALUES ('far', 'Caribou Coffee', 'coffee_shop', 46.7400, 24.7800, 0.5, 0.1)"""
    )
    quality.mark_duplicates(con)
    assert con.execute("SELECT duplicate_of FROM places WHERE id='far'").fetchone()[0] is None


def test_phone_normalization():
    assert clean_phone("011 205 3727") == "+966112053727"
    assert clean_phone("0114604848") == "+966114604848"
    assert clean_phone("966112155550") == "+966112155550"
    assert clean_phone("+966597537117") == "+966597537117"
    assert clean_phone("00966112155550") == "+966112155550"
    assert clean_phone("920020088") == "920020088"   # service short code, left as dialled
    assert clean_phone(None) is None
    assert clean_phone("   ") is None
