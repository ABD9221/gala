"""Conflate Overture places against OpenStreetMap via Nominatim.

Overture gives us the corpus; OSM gives us three fields Overture omits entirely:

* ``name:ar`` / ``name:en`` -- a *reliable* bilingual pair. Overture's
  ``names.common['ar']`` is empty across the whole Riyadh sample we measured,
  and its ``names.primary`` mixes scripts arbitrarily, so Arabic-language UIs
  cannot rely on it.
* ``opening_hours`` -- in OSM's documented grammar, parsed by ``gala.hours``.
* ``wikidata`` -- the join key that unlocks photos and a notability signal.

Matching is the hard part. Nominatim search is fuzzy, so a naive "first result
wins" would attach the wrong hours to the wrong shop. We require *both* spatial
proximity and name similarity before accepting a link, and record the score so a
bad threshold is auditable after the fact rather than silently baked in.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests
from rapidfuzz import fuzz

from ..config import DEFAULT_HTTP_TIMEOUT, NOMINATIM_MIN_INTERVAL, NOMINATIM_URL
from ..http import make_session
from ..normalize import normalize

# Accept a link only when the candidate is both close and named alike.
MAX_MATCH_METRES = 200.0
MIN_NAME_SIMILARITY = 70.0


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class RateLimiter:
    """Thread-safe minimum-interval gate.

    The public Nominatim instance allows one request per second and blocks
    clients that ignore it. Being throttled out of the only free source of
    Arabic names would be a self-inflicted outage, so the limit is enforced
    here rather than left to the caller's discipline.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.monotonic()


@dataclass
class OsmMatch:
    osm_type: str
    osm_id: int
    name_ar: str | None
    name_en: str | None
    opening_hours: str | None
    wikidata_id: str | None
    importance: float | None
    distance_m: float
    name_similarity: float


class NominatimClient:
    """Minimal Nominatim client returning the extra tags we actually need."""

    def __init__(self, base_url: str = NOMINATIM_URL, *, min_interval: float = NOMINATIM_MIN_INTERVAL) -> None:
        self._base = base_url.rstrip("/")
        self._limiter = RateLimiter(min_interval)
        self._client = make_session()
        self._client.headers["Accept-Language"] = "ar,en"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NominatimClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        self._limiter.wait()
        resp = self._client.get(f"{self._base}{path}", params=params, timeout=DEFAULT_HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str, lon: float, lat: float, *, span: float = 0.02) -> list[dict[str, Any]]:
        """Search inside a small viewbox around the Overture coordinate."""
        return self._get(
            "/search",
            {
                "q": query,
                "format": "jsonv2",
                "extratags": 1,
                "namedetails": 1,
                "limit": 5,
                "viewbox": f"{lon - span},{lat + span},{lon + span},{lat - span}",
                "bounded": 1,
            },
        )

    def match(self, name: str, lon: float, lat: float) -> OsmMatch | None:
        """Return the best OSM candidate for a place, or None if none qualifies."""
        if not name:
            return None
        try:
            candidates = self.search(name, lon, lat)
        except requests.RequestException:
            return None

        best: OsmMatch | None = None
        target = normalize(name)
        for c in candidates:
            try:
                clon, clat = float(c["lon"]), float(c["lat"])
            except (KeyError, TypeError, ValueError):
                continue
            dist = haversine_m(lon, lat, clon, clat)
            if dist > MAX_MATCH_METRES:
                continue

            names = c.get("namedetails") or {}
            # Score against every name variant OSM knows, not just the display
            # name: an Overture record in English should still match an OSM
            # object whose primary name is Arabic.
            variants = {normalize(v) for v in (c.get("name"), names.get("name"), names.get("name:ar"), names.get("name:en")) if v}
            sim = max((fuzz.token_set_ratio(target, v) for v in variants), default=0.0)
            if sim < MIN_NAME_SIMILARITY:
                continue

            extra = c.get("extratags") or {}
            cand = OsmMatch(
                osm_type=c.get("osm_type", ""),
                osm_id=int(c.get("osm_id", 0)),
                name_ar=names.get("name:ar"),
                name_en=names.get("name:en"),
                opening_hours=extra.get("opening_hours"),
                wikidata_id=extra.get("wikidata"),
                importance=c.get("importance"),
                distance_m=dist,
                name_similarity=sim,
            )
            # Prefer the strongest name match; break ties by proximity.
            if best is None or (cand.name_similarity, -cand.distance_m) > (best.name_similarity, -best.distance_m):
                best = cand
        return best
