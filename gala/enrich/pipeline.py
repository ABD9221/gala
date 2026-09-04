"""Conflate the Overture corpus against OSM, then attach Wikidata media.

Conflation -- deciding that Overture record X and OSM object Y are the same
real-world place -- is the load-bearing step. Get it wrong and you attach one
shop's opening hours to its neighbour, which is worse than having no hours at
all. Two safeguards:

*Both* signals must agree. A candidate has to be within
:data:`MAX_MATCH_METRES` **and** score above :data:`MIN_NAME_SIMILARITY` on
name. Distance alone matches the wrong unit in a mall; name alone matches the
other branch of the same chain across town.

Matching is **globally greedy, one-to-one**. Candidate pairs are scored, sorted
by quality, and consumed so that neither side is used twice. A per-row "best
match" loop would happily bind six Overture rows to one OSM node -- the classic
way chain outlets end up sharing a phone number.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

import duckdb
from rapidfuzz import fuzz

from .. import quality, rank
from ..config import BBox
from ..hours import parse as parse_hours
from ..normalize import is_arabic, match_variants, search_text
from .osm import haversine_m
from .overpass import OsmPoi, fetch_pois
from .wikidata import fetch_attribution, fetch_entities

MAX_MATCH_METRES = 150.0
MIN_NAME_SIMILARITY = 72.0
# Romanised Arabic is a lossy skeleton (no short vowels, several Arabic letters
# collapsing onto one Latin one), so a cross-script comparison carries less
# evidence per point of similarity than a same-script one and has to clear a
# higher bar. Without this, "Roberto Coin Kingdom Centre" binds to the mall it
# sits inside rather than to itself.
MIN_CROSS_SCRIPT_SIMILARITY = 84.0
# Grid cell ~0.002 deg ~= 200 m at this latitude, so a 3x3 neighbourhood always
# covers the match radius without scanning the whole POI list per place.
CELL = 0.002


@dataclass
class EnrichReport:
    overture_places: int = 0
    osm_pois: int = 0
    osm_error: str | None = None
    matched: int = 0
    got_name_ar: int = 0
    got_name_en: int = 0
    got_hours: int = 0
    parseable_hours: int = 0
    got_wikidata: int = 0
    got_photo: int = 0
    category_from_osm: int = 0
    category_from_brand: int = 0
    duplicates_marked: int = 0
    filled_phone: int = 0
    filled_website: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def _cell(lon: float, lat: float) -> tuple[int, int]:
    return (int(lon / CELL), int(lat / CELL))


def _name_variants(*names: str | None) -> set[str]:
    """Comparable forms of a place's names, including romanised Arabic."""
    return match_variants(*names)


def _best_similarity(targets: set[str], variants: set[str]) -> float:
    """Best score across name forms, held to a higher bar across scripts."""
    best = 0.0
    for t in targets:
        for v in variants:
            score = fuzz.token_set_ratio(t, v)
            floor = (
                MIN_NAME_SIMILARITY
                if is_arabic(t) == is_arabic(v)
                else MIN_CROSS_SCRIPT_SIMILARITY
            )
            if score >= floor:
                best = max(best, score)
    return best


def candidate_pairs(
    places: list[tuple[str, str | None, str | None, str | None, float, float]],
    pois: list[OsmPoi],
) -> list[tuple[float, float, str, OsmPoi]]:
    """Score every plausible (place, OSM POI) pair.

    Returns ``(similarity, -distance, place_id, poi)`` tuples, so a plain
    descending sort puts the best-named and then closest pairs first.
    """
    grid: dict[tuple[int, int], list[OsmPoi]] = defaultdict(list)
    for poi in pois:
        if not poi.is_poi:
            continue
        grid[_cell(poi.lon, poi.lat)].append(poi)

    pairs: list[tuple[float, float, str, OsmPoi]] = []
    for place_id, name, name_ar, name_en, lon, lat in places:
        targets = _name_variants(name, name_ar, name_en)
        if not targets:
            continue
        cx, cy = _cell(lon, lat)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for poi in grid.get((cx + dx, cy + dy), ()):
                    dist = haversine_m(lon, lat, poi.lon, poi.lat)
                    if dist > MAX_MATCH_METRES:
                        continue
                    variants = _name_variants(poi.name, poi.name_ar, poi.name_en)
                    if not variants:
                        continue
                    sim = _best_similarity(targets, variants)
                    if sim:
                        pairs.append((sim, -dist, place_id, poi))
    pairs.sort(key=lambda p: (p[0], p[1]), reverse=True)
    return pairs


def resolve_matches(pairs: list[tuple[float, float, str, OsmPoi]]) -> dict[str, OsmPoi]:
    """Greedy one-to-one assignment over pre-sorted candidate pairs."""
    taken_places: set[str] = set()
    taken_pois: set[tuple[str, int]] = set()
    matches: dict[str, OsmPoi] = {}
    for _sim, _negdist, place_id, poi in pairs:
        key = (poi.osm_type, poi.osm_id)
        if place_id in taken_places or key in taken_pois:
            continue
        taken_places.add(place_id)
        taken_pois.add(key)
        matches[place_id] = poi
    return matches


def enrich_bbox(con: duckdb.DuckDBPyConnection, bbox: BBox) -> EnrichReport:
    """Enrich every stored place inside ``bbox`` from OSM + Wikidata."""
    rows = con.execute(
        """
        SELECT id, name_primary, name_ar, name_en, lon, lat
        FROM places
        WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?
        """,
        [bbox.min_lon, bbox.max_lon, bbox.min_lat, bbox.max_lat],
    ).fetchall()

    # OSM enrichment adds Arabic names, opening hours and some category
    # corrections -- all valuable, none essential. Overpass is a free service
    # that rate-limits and goes down, so a failure here degrades the result
    # instead of destroying the run: the corpus, the category repair and the
    # lead list are all still produced without it.
    try:
        pois = fetch_pois(bbox)
        osm_error = None
    except (RuntimeError, OSError) as exc:
        pois, osm_error = [], str(exc)[:200]

    report = EnrichReport(overture_places=len(rows), osm_pois=len(pois), osm_error=osm_error)

    matches = resolve_matches(candidate_pairs(rows, pois))
    report.matched = len(matches)

    now = dt.datetime.now(dt.timezone.utc)
    qid_by_place: dict[str, str] = {}
    osm_tags: dict[str, dict[str, str]] = {}

    for place_id, poi in matches.items():
        osm_tags[place_id] = poi.tags
        report.got_name_ar += bool(poi.name_ar)
        report.got_name_en += bool(poi.name_en)
        if poi.opening_hours:
            report.got_hours += 1
            report.parseable_hours += parse_hours(poi.opening_hours) is not None
        if poi.wikidata:
            report.got_wikidata += 1
            qid_by_place[place_id] = poi.wikidata

        # OSM fills contact gaps but never overwrites Overture: Overture's
        # values are already normalised and cross-source verified, whereas OSM
        # is single-editor. coalesce() keeps the existing value when present.
        cur = con.execute(
            "SELECT phone IS NULL, website IS NULL FROM places WHERE id = ?", [place_id]
        ).fetchone()
        report.filled_phone += bool(cur[0] and poi.phone)
        report.filled_website += bool(cur[1] and poi.website)

        con.execute(
            """
            UPDATE places SET
                name_ar        = coalesce(name_ar, ?),
                name_en        = coalesce(name_en, ?),
                osm_type       = ?,
                osm_id         = ?,
                opening_hours  = coalesce(?, opening_hours),
                wikidata_id    = coalesce(?, wikidata_id),
                phone          = coalesce(phone, ?),
                website        = coalesce(website, ?),
                enriched_at    = ?
            WHERE id = ?
            """,
            [
                poi.name_ar, poi.name_en, poi.osm_type, poi.osm_id,
                poi.opening_hours, poi.wikidata, poi.phone, poi.website,
                now, place_id,
            ],
        )

    _attach_wikidata(con, qid_by_place, report)

    # Repair before deriving: the search index and prominence are both built
    # from the corrected category, so ordering matters here.
    quality.harmonize_categories(con)
    report.category_from_osm = quality.apply_osm_categories(con, osm_tags)
    report.category_from_brand = quality.apply_brand_lexicon(con)
    report.duplicates_marked = quality.mark_duplicates(con)

    refresh_derived(con)
    return report


def _attach_wikidata(con: duckdb.DuckDBPyConnection, qid_by_place: dict[str, str], report: EnrichReport) -> None:
    """Resolve collected Wikidata ids to photos and sitelink counts, in batch."""
    if not qid_by_place:
        return
    entities = fetch_entities(qid_by_place.values())
    attribution = fetch_attribution([e.image_file for e in entities.values() if e.image_file])
    for place_id, qid in qid_by_place.items():
        info = entities.get(qid)
        if info is None:
            continue
        report.got_photo += bool(info.image_url)
        con.execute(
            "UPDATE places SET wikidata_sitelinks = ?, photo_url = ?, photo_attribution = ? WHERE id = ?",
            [
                info.sitelinks,
                info.image_url,
                attribution.get(info.image_file) if info.image_file else None,
                place_id,
            ],
        )


def refresh_derived(con: duckdb.DuckDBPyConnection) -> None:
    """Rebuild the search blob and prominence after names/signals change.

    Enrichment moves two derived things: the newly acquired Arabic and English
    names belong in the index, and sitelink counts feed the ranking model.
    Skipping this leaves the enrichment invisible to every user-facing query.
    """
    con.create_function(
        "gala_blob6",
        lambda a, b, c, d, e, f: search_text(a, b, c, d, e, f),
        ["VARCHAR"] * 6,
        "VARCHAR",
        null_handling="special",
    )
    con.execute(
        """
        UPDATE places
        SET search_blob = gala_blob6(name_primary, name_ar, name_en, category, brand_name, address_freeform)
        """
    )
    con.remove_function("gala_blob6")
    rank.recompute(con)
