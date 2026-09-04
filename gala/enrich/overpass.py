"""Bulk OSM enrichment via Overpass.

Nominatim was the obvious first choice and turned out to be the wrong tool. It
is a *geocoder*: it answers "where is this address", and it does that well --
a direct lookup of ``مركز المملكة`` matched at 5 m with a 100% name score. But
asked for ordinary retail POIs by name inside a tight viewbox it returns nothing
at all (``Fendi`` -> 0 results, ``ibis Riyadh Olaya Street`` -> 0 results), and
its one-request-per-second policy makes a per-place loop take hours for a single
district.

Overpass queries the OSM database directly. One request returns every named
object in a bounding box with all of its tags, which turns enrichment from
N network round-trips into one, and lets the actual matching happen locally
where we can tune it. Nominatim stays available in ``enrich.osm`` for the
single-place lookups it is genuinely good at.

Mirrors are tried in order: the main ``overpass-api.de`` endpoint is frequently
unreachable from behind egress proxies, so a failover list is not optional.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

from ..config import BBox, DEFAULT_HTTP_TIMEOUT
from ..http import make_session

MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Tags worth carrying over. Everything else in OSM is either geometry detail or
# not relevant to a places API.
WANTED_TAGS = (
    "name", "name:ar", "name:en", "opening_hours", "wikidata", "wikipedia",
    "phone", "contact:phone", "website", "contact:website", "brand",
    "brand:wikidata", "cuisine", "amenity", "shop", "tourism", "office",
    "leisure", "healthcare", "craft", "highway",
)

# A `name` tag alone does not make something a place: in the Olaya sample 843 of
# 987 named OSM objects were streets, buildings and districts. Left in the
# candidate pool they produce confident nonsense -- a road named
# "طريق التخصصي الفرعي" scoring against a shop called "Al-Thabit Doors -
# Takhassosi Branch". Conflation only ever considers objects carrying one of
# these feature tags.
POI_TAGS = ("amenity", "shop", "tourism", "office", "leisure", "healthcare", "craft")


@dataclass
class OsmPoi:
    osm_type: str
    osm_id: int
    lon: float
    lat: float
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_poi(self) -> bool:
        """True when this object is a place, not a road or a plain building."""
        if self.tags.get("highway"):
            return False
        return any(self.tags.get(t) for t in POI_TAGS)

    @property
    def name(self) -> str | None:
        return self.tags.get("name")

    @property
    def name_ar(self) -> str | None:
        return self.tags.get("name:ar")

    @property
    def name_en(self) -> str | None:
        return self.tags.get("name:en")

    @property
    def opening_hours(self) -> str | None:
        return self.tags.get("opening_hours")

    @property
    def wikidata(self) -> str | None:
        return self.tags.get("wikidata") or self.tags.get("brand:wikidata")

    @property
    def phone(self) -> str | None:
        return self.tags.get("phone") or self.tags.get("contact:phone")

    @property
    def website(self) -> str | None:
        return self.tags.get("website") or self.tags.get("contact:website")


def build_query(bbox: BBox, *, timeout: int = 180) -> str:
    """Every named node/way/relation in the box, with centroids for areas."""
    b = f"{bbox.min_lat},{bbox.min_lon},{bbox.max_lat},{bbox.max_lon}"
    return f'[out:json][timeout:{timeout}];nwr({b})["name"];out center tags;'


def fetch_pois(
    bbox: BBox,
    *,
    mirrors: Iterable[str] = MIRRORS,
    session: requests.Session | None = None,
    timeout: float = 180.0,
) -> list[OsmPoi]:
    """Fetch all named OSM POIs in ``bbox``, failing over between mirrors."""
    owns = session is None
    session = session or make_session(retries=1)
    query = build_query(bbox)
    errors: list[str] = []
    try:
        for url in mirrors:
            try:
                resp = session.post(url, data={"data": query}, timeout=timeout)
                resp.raise_for_status()
                payload = resp.json()
            except (requests.RequestException, ValueError) as exc:
                errors.append(f"{url}: {type(exc).__name__}")
                continue
            return [p for e in payload.get("elements", []) if (p := _to_poi(e))]
    finally:
        if owns:
            session.close()
    raise RuntimeError("all Overpass mirrors failed: " + "; ".join(errors))


def _to_poi(element: dict[str, Any]) -> OsmPoi | None:
    # Nodes carry lat/lon directly; ways and relations get a `center` because
    # the query asked for `out center`.
    lat = element.get("lat", (element.get("center") or {}).get("lat"))
    lon = element.get("lon", (element.get("center") or {}).get("lon"))
    if lat is None or lon is None:
        return None
    tags = element.get("tags") or {}
    return OsmPoi(
        osm_type=element.get("type", ""),
        osm_id=int(element.get("id", 0)),
        lon=float(lon),
        lat=float(lat),
        tags={k: v for k, v in tags.items() if k in WANTED_TAGS},
    )
