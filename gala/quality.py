"""Corpus quality repair.

Bulk open data is cheap, plentiful, and wrong in specific measurable ways. In
the Olaya sample (1,506 places) the two defects that actually reach users are:

* **Category noise.** Overture labels Krispy Kreme, KFC and a Bank Al Jazira
  ATM as ``pharmacy``. 15 of the 37 multi-record name clusters carry mutually
  contradictory categories. A "nearby pharmacies" query returns doughnuts.
* **Duplicates.** The same storefront appears more than once, sometimes with
  different categories, so a result page repeats itself.

Nothing here invents data. Every correction is either a consensus *within* the
corpus or a value taken from OSM, which is human-curated and, on the subset we
could conflate, demonstrably more accurate on category. Corrections are written
to separate columns rather than overwriting the source, so the original stays
auditable and a bad rule can be rolled back.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass

import duckdb

from .enrich.osm import haversine_m
from .normalize import normalize

# OSM feature tags -> our (Overture-derived) taxonomy. Only unambiguous
# mappings are listed: a guess here would trade one wrong category for another.
OSM_CATEGORY_MAP: dict[tuple[str, str], str] = {
    ("amenity", "cafe"): "cafe",
    ("amenity", "fast_food"): "fast_food_restaurant",
    ("amenity", "restaurant"): "restaurant",
    ("amenity", "pharmacy"): "pharmacy",
    ("amenity", "bank"): "bank_credit_union",
    ("amenity", "atm"): "atm",
    ("amenity", "hospital"): "hospital",
    ("amenity", "clinic"): "medical_clinic",
    ("amenity", "doctors"): "medical_clinic",
    ("amenity", "dentist"): "dentist",
    ("amenity", "place_of_worship"): "mosque",
    ("amenity", "fuel"): "gas_station",
    ("amenity", "car_wash"): "car_wash",
    ("amenity", "cinema"): "movie_theater",
    ("amenity", "school"): "school",
    ("amenity", "university"): "university",
    ("shop", "supermarket"): "grocery_store",
    ("shop", "convenience"): "convenience_store",
    ("shop", "bakery"): "bakery",
    ("shop", "clothes"): "clothing_store",
    ("shop", "jewelry"): "jewelry_store",
    ("shop", "mobile_phone"): "mobile_phone_store",
    ("shop", "optician"): "eyewear_and_optician",
    ("shop", "hairdresser"): "hair_salon",
    ("shop", "car_repair"): "auto_repair_shop",
    ("shop", "mall"): "shopping_center",
    ("shop", "perfumery"): "perfume_store",
    ("tourism", "hotel"): "hotel",
    ("tourism", "museum"): "museum",
    ("leisure", "fitness_centre"): "gym",
    ("office", "government"): "government_office",
}

# Curated brand -> category lexicon, keyed on the normalized name from
# `gala.normalize`. This is the third and last correction source, for the case
# neither of the others can reach: a chain that Overture labels wrongly in
# *every* record, with no conflated OSM object to contradict it. In the Olaya
# sample that is Krispy Kreme, Hardee's and Subway, all filed as `pharmacy`,
# which is how "pharmacies near me" ends up returning doughnuts.
#
# Curated data earns its keep only if it is honest about being curated: matches
# are tagged `brand_lexicon` in `category_source`, so a wrong entry here is
# findable and revertible. Every production places API maintains a list like
# this; the alternative is shipping the doughnuts.
BRAND_CATEGORIES: dict[str, str] = {
    # Fast food
    "krispy kreme": "donuts", "كرسبي كريم": "donuts",
    "dunkin": "donuts", "دانكن دونتس": "donuts",
    "kfc": "fast_food_restaurant", "كنتاكي": "fast_food_restaurant",
    "hardees": "fast_food_restaurant", "هارديز": "fast_food_restaurant",
    "mcdonalds": "fast_food_restaurant", "ماكدونالدز": "fast_food_restaurant",
    "burger king": "fast_food_restaurant", "برجر كنج": "fast_food_restaurant",
    "subway": "fast_food_restaurant", "صب واي": "fast_food_restaurant",
    "albaik": "fast_food_restaurant", "البيك": "fast_food_restaurant",
    "kudu": "fast_food_restaurant", "كودو": "fast_food_restaurant",
    "herfy": "fast_food_restaurant", "هرفي": "fast_food_restaurant",
    "papa johns": "pizza_restaurant", "pizza hut": "pizza_restaurant",
    "بيتزا هت": "pizza_restaurant", "دومينوز": "pizza_restaurant",
    "shawarmer": "fast_food_restaurant", "شاورمر": "fast_food_restaurant",
    # Coffee
    "starbucks": "coffee_shop", "ستاربكس": "coffee_shop",
    "costa coffee": "coffee_shop", "كوستا": "coffee_shop",
    "caribou coffee": "coffee_shop", "دوز": "coffee_shop",
    "tim hortons": "coffee_shop", "تيم هورتنز": "coffee_shop",
    "barns": "coffee_shop", "بارنز": "coffee_shop",
    "%arabica": "coffee_shop", "ارابيكا": "coffee_shop",
    # Pharmacy
    "nahdi": "pharmacy", "النهدي": "pharmacy", "صيدليه النهدي": "pharmacy",
    "aldawaa": "pharmacy", "الدواء": "pharmacy",
    "united pharmacy": "pharmacy", "المتحده": "pharmacy",
    # Grocery
    "panda": "grocery_store", "بنده": "grocery_store",
    "danube": "grocery_store", "الدانوب": "grocery_store",
    "tamimi markets": "grocery_store", "التميمي": "grocery_store",
    "carrefour": "grocery_store", "كارفور": "grocery_store",
    "lulu hypermarket": "grocery_store", "لولو": "grocery_store",
    "othaim": "grocery_store", "العثيم": "grocery_store",
    # Retail / electronics
    "jarir bookstore": "book_store", "مكتبه جرير": "book_store",
    "extra": "electronics_store", "اكسترا": "electronics_store",
    "ikea": "furniture_store", "ايكيا": "furniture_store",
    # Banks
    "al rajhi bank": "bank_credit_union", "مصرف الراجحي": "bank_credit_union",
    "الراجحي": "bank_credit_union", "الاهلي": "bank_credit_union",
    "بنك الجزيره": "bank_credit_union", "صراف بنك الجزيره": "atm",
    "riyad bank": "bank_credit_union", "بنك الرياض": "bank_credit_union",
    # Telecom
    "stc": "mobile_phone_store", "اس تي سي": "mobile_phone_store",
    "mobily": "mobile_phone_store", "موبايلي": "mobile_phone_store",
    "zain": "mobile_phone_store", "زين": "mobile_phone_store",
}

DUPLICATE_RADIUS_M = 120.0


def osm_category(tags: dict[str, str]) -> str | None:
    """Map an OSM POI's tags onto our category vocabulary, if unambiguous."""
    for key in ("amenity", "shop", "tourism", "leisure", "office"):
        value = tags.get(key)
        if value and (mapped := OSM_CATEGORY_MAP.get((key, value))):
            return mapped
    return None


@dataclass
class QualityReport:
    clusters: int = 0
    conflicted_clusters: int = 0
    category_corrected: int = 0
    osm_corrected: int = 0
    duplicates_marked: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def ensure_columns(con: duckdb.DuckDBPyConnection) -> None:
    """Backfill the repair columns on a store created before they existed.

    They are part of ``store.SCHEMA`` now -- the index and every query read
    them, so a database missing them fails to build at all. This remains for
    upgrading a store written by an earlier version.
    """
    for ddl in (
        "ALTER TABLE places ADD COLUMN IF NOT EXISTS category_final VARCHAR",
        "ALTER TABLE places ADD COLUMN IF NOT EXISTS category_source VARCHAR",
        "ALTER TABLE places ADD COLUMN IF NOT EXISTS category_confidence DOUBLE",
        "ALTER TABLE places ADD COLUMN IF NOT EXISTS duplicate_of VARCHAR",
    ):
        con.execute(ddl)


def harmonize_categories(con: duckdb.DuckDBPyConnection) -> QualityReport:
    """Resolve category disagreement inside same-name clusters.

    Records sharing a normalized name are almost always branches of one brand,
    and a brand has one category. Where a cluster disagrees we take the
    confidence-weighted majority and record how strong that majority was, so
    the API can distinguish a settled category from a coin-flip.

    The honest limit: when *every* record of a brand is mislabelled the same way
    -- Overture has all three "صب واي" (Subway) rows as ``pharmacy`` /
    ``educational_services`` -- no amount of internal consensus recovers the
    truth. That case needs the external signal applied by
    :func:`apply_osm_categories`.
    """
    ensure_columns(con)
    rows = con.execute(
        "SELECT id, name_primary, category, confidence FROM places WHERE name_primary IS NOT NULL"
    ).fetchall()

    clusters: dict[str, list[tuple]] = collections.defaultdict(list)
    for row in rows:
        clusters[normalize(row[1])].append(row)

    report = QualityReport(clusters=sum(1 for v in clusters.values() if len(v) >= 2))
    for members in clusters.values():
        if len(members) < 2:
            continue
        distinct = {m[2] for m in members if m[2]}
        if len(distinct) < 2:
            continue
        report.conflicted_clusters += 1

        weights: dict[str, float] = collections.defaultdict(float)
        for _id, _name, category, confidence in members:
            if category:
                weights[category] += (confidence or 0.0) + 0.1  # floor so a
                # zero-confidence record still counts as one vote
        if not weights:
            continue
        winner, top = max(weights.items(), key=lambda kv: kv[1])
        strength = top / sum(weights.values())

        for place_id, _name, category, _confidence in members:
            if category != winner:
                report.category_corrected += 1
            con.execute(
                """UPDATE places
                   SET category_final = ?, category_source = 'cluster_consensus', category_confidence = ?
                   WHERE id = ?""",
                [winner, strength, place_id],
            )

    # Everything not touched above keeps its original category at full trust.
    con.execute(
        """UPDATE places
           SET category_final = category, category_source = 'overture', category_confidence = 1.0
           WHERE category_final IS NULL"""
    )
    return report


def apply_osm_categories(con: duckdb.DuckDBPyConnection, osm_tags_by_place: dict[str, dict[str, str]]) -> int:
    """Override categories with OSM's where a conflated POI supplies one.

    OSM is hand-maintained by people who visit the place, and on the conflated
    subset it fixes exactly the failures internal consensus cannot -- the brands
    Overture gets uniformly wrong. Applied after
    :func:`harmonize_categories` so it wins.
    """
    ensure_columns(con)
    corrected = 0
    for place_id, tags in osm_tags_by_place.items():
        mapped = osm_category(tags)
        if not mapped:
            continue
        current = con.execute("SELECT category_final FROM places WHERE id = ?", [place_id]).fetchone()
        if current and current[0] != mapped:
            corrected += 1
        con.execute(
            """UPDATE places
               SET category_final = ?, category_source = 'osm', category_confidence = 1.0
               WHERE id = ?""",
            [mapped, place_id],
        )
    return corrected


def apply_brand_lexicon(con: duckdb.DuckDBPyConnection) -> int:
    """Correct categories for chains listed in :data:`BRAND_CATEGORIES`.

    Runs last, so a curated entry overrides both the internal consensus and
    OSM. That ordering is deliberate: the lexicon exists precisely for the
    records where the other two sources are absent or agree on the wrong answer.
    """
    ensure_columns(con)
    rows = con.execute(
        "SELECT id, name_primary, brand_name, category_final FROM places WHERE name_primary IS NOT NULL"
    ).fetchall()
    corrected = 0
    for place_id, name, brand, current in rows:
        mapped = None
        for candidate in (brand, name):
            key = normalize(candidate)
            if not key:
                continue
            mapped = BRAND_CATEGORIES.get(key)
            if mapped:
                break
            # Chain outlets are usually named "<brand> - <branch>", so fall
            # back to a prefix hit before giving up.
            for brand_key, category in BRAND_CATEGORIES.items():
                if key.startswith(brand_key + " ") or key == brand_key:
                    mapped = category
                    break
            if mapped:
                break
        if not mapped:
            continue
        if current != mapped:
            corrected += 1
        con.execute(
            """UPDATE places
               SET category_final = ?, category_source = 'brand_lexicon', category_confidence = 1.0
               WHERE id = ?""",
            [mapped, place_id],
        )
    return corrected


def mark_duplicates(con: duckdb.DuckDBPyConnection, *, radius_m: float = DUPLICATE_RADIUS_M) -> int:
    """Point near-identical records at a single surviving row.

    Same normalized name within ``radius_m`` is treated as one storefront. The
    record with the most information wins -- prominence already encodes contact
    completeness and source agreement -- and the others get a ``duplicate_of``
    pointer rather than being deleted, so the merge stays reversible.
    """
    ensure_columns(con)
    rows = con.execute(
        """SELECT id, name_primary, lon, lat, coalesce(prominence, 0)
           FROM places WHERE name_primary IS NOT NULL AND duplicate_of IS NULL"""
    ).fetchall()

    clusters: dict[str, list[tuple]] = collections.defaultdict(list)
    for row in rows:
        clusters[normalize(row[1])].append(row)

    marked = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda m: m[4], reverse=True)
        kept: list[tuple] = []
        for cand in members:
            parent = next(
                (k for k in kept if haversine_m(cand[2], cand[3], k[2], k[3]) <= radius_m),
                None,
            )
            if parent is None:
                kept.append(cand)
            else:
                con.execute("UPDATE places SET duplicate_of = ? WHERE id = ?", [parent[0], cand[0]])
                marked += 1
    return marked
