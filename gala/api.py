"""HTTP API, shaped to mirror Google Places so a client port stays mechanical.

Endpoint mapping:

===========================  ==========================================
Google Places                 gala
===========================  ==========================================
``places:searchText``         ``GET /v1/places:searchText``
``places:searchNearby``       ``GET /v1/places:searchNearby``
``places:autocomplete``       ``GET /v1/places:autocomplete``
``places/{place_id}``         ``GET /v1/places/{place_id}``
===========================  ==========================================

Field names follow Google's where an equivalent exists (``displayName``,
``formattedAddress``, ``currentOpeningHours``) so the response can be dropped
into an existing client. Two deliberate divergences:

* ``rating`` / ``userRatingCount`` are present but ``null`` unless a licensed
  provider is configured. They are never synthesised -- see
  :class:`gala.rank.RatingsProvider`.
* ``prominence`` and ``categorySource`` are additions. Google gives you no way
  to know why a result ranked where it did or where its category came from;
  since ours is assembled from several sources of differing trust, saying so is
  more useful than hiding it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from .config import DB_PATH
from .hours import weekly_schedule
from .rank import NullRatingsProvider, RatingsProvider
from .search import SearchResult, autocomplete, details, nearby, search
from .store import connect, stats

app = FastAPI(
    title="gala places API",
    version="0.1.0",
    description="Open-data places search: Overture + OpenStreetMap + Wikidata.",
)

# One read-only connection for the process. DuckDB handles concurrent reads on a
# single connection, and opening per request would reload extensions each time.
_con = None
_ratings: RatingsProvider = NullRatingsProvider()


def get_con():
    global _con
    if _con is None:
        _con = connect(DB_PATH, read_only=True)
    return _con


def set_ratings_provider(provider: RatingsProvider) -> None:
    """Install a licensed ratings source. Without one, ratings stay null."""
    global _ratings
    _ratings = provider


def _serialize(r: SearchResult, *, include_debug: bool = False) -> dict[str, Any]:
    rating = _ratings.fetch(r.id, r.name or "", r.lon, r.lat)
    out: dict[str, Any] = {
        "id": r.id,
        "displayName": {
            "text": r.name,
            "ar": r.name_ar,
            "en": r.name_en,
        },
        "primaryType": r.category,
        "location": {"latitude": r.lat, "longitude": r.lon},
        "formattedAddress": r.address,
        "nationalPhoneNumber": r.phone,
        "websiteUri": r.website,
        "photo": (
            {"uri": r.photo_url, "attribution": r.photo_attribution} if r.photo_url else None
        ),
        "currentOpeningHours": (
            {"openNow": r.open_now, "raw": r.opening_hours} if r.opening_hours else None
        ),
        "distanceMeters": round(r.distance_m) if r.distance_m is not None else None,
        # Additions over Google's schema.
        "prominence": round(r.prominence, 4) if r.prominence is not None else None,
        "relevanceScore": round(r.score, 4),
        # Always present, always honest.
        "rating": rating.value if rating else None,
        "userRatingCount": rating.count if rating else None,
        "ratingSource": rating.source if rating else None,
    }
    if include_debug:
        out["debug"] = {k: round(v, 4) for k, v in r.debug.items()}
    return out


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "corpus": stats(get_con())}


@app.get("/v1/places:searchText")
def search_text(
    textQuery: str = Query(..., min_length=1, description="Free text, Arabic or Latin"),
    latitude: float | None = None,
    longitude: float | None = None,
    radiusMeters: float | None = Query(None, gt=0, le=50000),
    includedType: str | None = None,
    openNow: bool = False,
    maxResultCount: int = Query(20, ge=1, le=50),
    debug: bool = False,
) -> dict[str, Any]:
    results = search(
        get_con(), textQuery,
        lon=longitude, lat=latitude, radius_m=radiusMeters,
        category=includedType, open_now=openNow, limit=maxResultCount,
    )
    return {"places": [_serialize(r, include_debug=debug) for r in results]}


@app.get("/v1/places:searchNearby")
def search_nearby(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    radiusMeters: float = Query(1000, gt=0, le=50000),
    includedType: str | None = None,
    openNow: bool = False,
    maxResultCount: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    results = nearby(
        get_con(), lon=longitude, lat=latitude, radius_m=radiusMeters,
        category=includedType, open_now=openNow, limit=maxResultCount,
    )
    return {"places": [_serialize(r) for r in results]}


@app.get("/v1/places:autocomplete")
def places_autocomplete(
    input: str = Query(..., min_length=1),
    latitude: float | None = None,
    longitude: float | None = None,
    maxResultCount: int = Query(8, ge=1, le=20),
) -> dict[str, Any]:
    results = autocomplete(get_con(), input, lon=longitude, lat=latitude, limit=maxResultCount)
    return {
        "suggestions": [
            {
                "placePrediction": {
                    "placeId": r.id,
                    "text": r.name,
                    "textAr": r.name_ar,
                    "primaryType": r.category,
                    "distanceMeters": round(r.distance_m) if r.distance_m is not None else None,
                }
            }
            for r in results
        ]
    }


@app.get("/v1/places/{place_id:path}")
def place_details(place_id: str) -> dict[str, Any]:
    row = details(get_con(), place_id)
    if row is None:
        raise HTTPException(status_code=404, detail="place not found")

    rating = _ratings.fetch(place_id, row.get("name_primary") or "", row["lon"], row["lat"])
    return {
        "id": row["id"],
        "displayName": {"text": row.get("name_primary"), "ar": row.get("name_ar"), "en": row.get("name_en")},
        "primaryType": row.get("category_final") or row.get("category"),
        "categorySource": row.get("category_source"),
        "categoryConfidence": row.get("category_confidence"),
        "location": {"latitude": row["lat"], "longitude": row["lon"]},
        "formattedAddress": row.get("address_freeform"),
        "addressComponents": {
            "locality": row.get("locality"),
            "region": row.get("region"),
            "postalCode": row.get("postcode"),
            "country": row.get("country"),
        },
        "nationalPhoneNumber": row.get("phone"),
        "websiteUri": row.get("website"),
        "photo": (
            {"uri": row["photo_url"], "attribution": row.get("photo_attribution")}
            if row.get("photo_url") else None
        ),
        "currentOpeningHours": {
            "openNow": row.get("open_now"),
            "raw": row.get("opening_hours"),
            "weekdayDescriptions": row.get("schedule"),
        } if row.get("opening_hours") else None,
        "prominence": row.get("prominence"),
        "rating": rating.value if rating else None,
        "userRatingCount": rating.count if rating else None,
        "ratingSource": rating.source if rating else None,
        "sources": {
            "base": "Overture Maps",
            "osm": {"type": row.get("osm_type"), "id": row.get("osm_id")} if row.get("osm_id") else None,
            "wikidata": row.get("wikidata_id"),
        },
    }
