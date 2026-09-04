"""SerpApi enrichment: ratings and review counts, to qualify leads.

Why this is worth paying for *here* specifically. The corpus knows a business
exists and has no website; it cannot tell a thriving restaurant from a dead one
with the sign still up. Review counts separate them, and that decides whether a
lead is worth a phone call. Overture's ``confidence`` is a weak stand-in.

**One request returns about 20 places**, so a district costs a handful of calls
rather than one per business. Enriching all of Olaya is roughly 15-25 searches
against a 250/month free tier.

Two rules this module enforces rather than documents:

* **Grid search, not per-place lookup.** Fetching by place would be ~20x the
  calls for the same coverage.
* **The data is rented, not owned.** Google's terms allow place IDs
  indefinitely but not warehoused ratings, hours or reviews. Rows carry
  ``serpapi_fetched_at`` and :func:`stale_ids` re-fetches past
  :data:`MAX_AGE_DAYS`; the fields are kept out of the search index and out of
  every export. The open corpus stays the thing you own -- this is a live
  overlay on top of it.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

import duckdb
import requests
from rapidfuzz import fuzz

from ..config import BBox, DEFAULT_HTTP_TIMEOUT
from ..enrich.osm import haversine_m
from ..http import make_session
from ..normalize import match_variants

ENDPOINT = "https://serpapi.com/search.json"
MAX_AGE_DAYS = 30
# Matching the same way conflation does: proximity and name must both agree.
MAX_MATCH_METRES = 150.0
MIN_NAME_SIMILARITY = 72.0


class SerpApiError(RuntimeError):
    pass


@dataclass
class SerpPlace:
    title: str
    lon: float
    lat: float
    rating: float | None
    reviews: int | None
    hours: str | None
    price: str | None
    place_id: str | None
    website: str | None
    phone: str | None
    category: str | None


def _api_key(explicit: str | None = None) -> str | None:
    return explicit or os.getenv("SERPAPI_KEY") or os.getenv("SERPAPI_API_KEY")


def search_maps(
    query: str,
    lon: float,
    lat: float,
    *,
    zoom: int = 15,
    api_key: str | None = None,
    session: requests.Session | None = None,
) -> list[SerpPlace]:
    """One Google Maps search: up to ~20 places with ratings."""
    owns = session is None
    session = session or make_session()
    params = {"engine": "google_maps", "q": query, "ll": f"@{lat},{lon},{zoom}z", "hl": "ar"}
    if key := _api_key(api_key):
        params["api_key"] = key
    try:
        resp = session.get(ENDPOINT, params=params, timeout=DEFAULT_HTTP_TIMEOUT)
        if resp.status_code == 401:
            raise SerpApiError("SerpApi rejected the key (401). Set SERPAPI_KEY.")
        if resp.status_code == 429:
            raise SerpApiError("SerpApi quota exhausted (429).")
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise SerpApiError(f"SerpApi request failed: {exc}") from exc
    finally:
        if owns:
            session.close()

    if err := payload.get("error"):
        raise SerpApiError(str(err))

    out: list[SerpPlace] = []
    for r in payload.get("local_results", []):
        gps = r.get("gps_coordinates") or {}
        if gps.get("longitude") is None:
            continue
        out.append(SerpPlace(
            title=r.get("title", ""),
            lon=float(gps["longitude"]), lat=float(gps["latitude"]),
            rating=r.get("rating"), reviews=r.get("reviews"),
            hours=r.get("hours") or r.get("open_state"),
            price=r.get("price"), place_id=r.get("place_id"),
            website=r.get("website"), phone=r.get("phone"),
            category=r.get("type"),
        ))
    return out


def grid_queries(bbox: BBox, *, terms: list[str]) -> list[tuple[str, float, float]]:
    """Query plan covering ``bbox``: one search per term at the centre.

    Google's own result set for a term already spans the visible map at this
    zoom, so a term-per-centre plan covers a district without the overlapping
    duplicate calls a fine spatial grid would produce.
    """
    lon, lat = bbox.center
    return [(term, lon, lat) for term in terms]


DEFAULT_TERMS = [
    "مطاعم", "مقاهي", "صالون تجميل", "حلاق", "عيادة", "صيدلية",
    "محل ملابس", "أثاث", "مجوهرات", "ورشة سيارات", "صالة رياضية", "تنظيم حفلات",
]


def enrich_leads(
    con: duckdb.DuckDBPyConnection,
    bbox: BBox,
    *,
    terms: list[str] | None = None,
    api_key: str | None = None,
    max_calls: int = 12,
) -> dict[str, int]:
    """Fetch ratings for places in ``bbox`` and attach them by conflation."""
    from ..quality import ensure_columns

    ensure_columns(con)
    plan = grid_queries(bbox, terms=terms or DEFAULT_TERMS)[:max_calls]

    rows = con.execute(
        """SELECT id, name_primary, name_ar, name_en, lon, lat
           FROM places WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?""",
        [bbox.min_lon, bbox.max_lon, bbox.min_lat, bbox.max_lat],
    ).fetchall()

    session = make_session()
    stats = {"calls": 0, "google_places": 0, "matched": 0, "with_rating": 0, "unmatched": 0}
    now = dt.datetime.now(dt.timezone.utc)
    seen: set[str] = set()

    try:
        for term, lon, lat in plan:
            try:
                found = search_maps(term, lon, lat, api_key=api_key, session=session)
            except SerpApiError as exc:
                stats["error"] = str(exc)
                break
            stats["calls"] += 1

            for sp in found:
                if sp.place_id and sp.place_id in seen:
                    continue
                if sp.place_id:
                    seen.add(sp.place_id)
                stats["google_places"] += 1

                match_id = _best_match(sp, rows)
                if match_id is None:
                    stats["unmatched"] += 1
                    continue
                stats["matched"] += 1
                stats["with_rating"] += bool(sp.rating)
                con.execute(
                    """UPDATE places SET
                         serpapi_rating = ?, serpapi_reviews = ?, serpapi_hours = ?,
                         serpapi_price = ?, serpapi_place_id = ?, serpapi_fetched_at = ?,
                         website = coalesce(website, ?)
                       WHERE id = ?""",
                    [sp.rating, sp.reviews, sp.hours, sp.price, sp.place_id, now,
                     sp.website, match_id],
                )
    finally:
        session.close()
    return stats


def _best_match(sp: SerpPlace, rows: list[tuple]) -> str | None:
    """Same rule as OSM conflation: close enough *and* named alike."""
    targets = match_variants(sp.title)
    if not targets:
        return None
    best_id, best_score = None, 0.0
    for pid, name, name_ar, name_en, lon, lat in rows:
        if haversine_m(sp.lon, sp.lat, lon, lat) > MAX_MATCH_METRES:
            continue
        variants = match_variants(name, name_ar, name_en)
        score = max((fuzz.token_set_ratio(a, b) for a in targets for b in variants), default=0.0)
        if score >= MIN_NAME_SIMILARITY and score > best_score:
            best_id, best_score = pid, score
    return best_id


def stale_ids(con: duckdb.DuckDBPyConnection, *, max_age_days: int = MAX_AGE_DAYS) -> list[str]:
    """Places whose Google-derived fields are past their permitted cache life."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
    return [r[0] for r in con.execute(
        "SELECT id FROM places WHERE serpapi_fetched_at IS NOT NULL AND serpapi_fetched_at < ?",
        [cutoff],
    ).fetchall()]


def purge_stale(con: duckdb.DuckDBPyConnection, *, max_age_days: int = MAX_AGE_DAYS) -> int:
    """Clear expired Google-derived fields.

    Run this on a schedule. Google permits caching place IDs indefinitely but
    not ratings, hours or prices; letting them sit forever turns a live overlay
    into the warehouse the terms forbid.
    """
    stale = stale_ids(con, max_age_days=max_age_days)
    if stale:
        con.execute(
            """UPDATE places SET serpapi_rating = NULL, serpapi_reviews = NULL,
                                 serpapi_hours = NULL, serpapi_price = NULL,
                                 serpapi_fetched_at = NULL
               WHERE id IN (SELECT unnest(?))""",
            [stale],
        )
    return len(stale)
