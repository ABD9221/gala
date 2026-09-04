"""District catalog -- the unit of work for prospecting.

Calling every business in a city at once is not a plan; calling every business
in one district is. This module turns a city into a list of workable districts.

Districts come from OpenStreetMap, and the shape of that data differs sharply by
city. Riyadh's districts are mostly mapped as polygons with real boundaries;
**Jeddah's are almost entirely single points** -- 171 of 176 carry no extent at
all. A catalog that only accepted polygons would cover 3% of Jeddah.

So a point district gets a bounding box derived from how far away its nearest
neighbour is: half that distance, clamped. Districts packed tightly downtown get
small boxes, isolated ones on the edge of town get large ones, and the result
approximates the real coverage far better than one fixed radius for everything.
It is an approximation, and it is labelled as one in ``bbox_source``.
"""
from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import BBox
from .enrich.osm import haversine_m
from .enrich.overpass import MIRRORS
from .http import make_session
from .normalize import normalize

DATA_DIR = Path(__file__).parent / "data"

# Half the gap to the nearest district, clamped: a dense downtown block should
# not claim its neighbours, and an isolated district should not shrink to a dot.
MIN_RADIUS_M = 700.0
MAX_RADIUS_M = 2500.0
DEFAULT_RADIUS_M = 1200.0

CITY_BBOX: dict[str, tuple[float, float, float, float]] = {
    # city -> (min_lat, min_lon, max_lat, max_lon) as Overpass wants them
    "jeddah": (21.20, 38.95, 22.05, 39.45),
    "riyadh": (24.40, 46.35, 25.05, 47.10),
    "dammam": (26.20, 49.85, 26.65, 50.30),
    "makkah": (21.25, 39.70, 21.55, 40.00),
    "madinah": (24.35, 39.45, 24.65, 39.75),
}


@dataclass
class District:
    name: str
    name_en: str | None
    city: str
    lon: float
    lat: float
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    place_type: str
    osm_type: str
    osm_id: int
    bbox_source: str  # "osm" (real boundary) or "derived" (from neighbour spacing)

    @property
    def bbox(self) -> BBox:
        return BBox(self.min_lon, self.min_lat, self.max_lon, self.max_lat)

    @property
    def area_km2(self) -> float:
        return (self.max_lon - self.min_lon) * 101 * (self.max_lat - self.min_lat) * 111

    @property
    def label(self) -> str:
        return f"{self.name} ({self.name_en})" if self.name_en else self.name


def _query(city: str) -> str:
    min_lat, min_lon, max_lat, max_lon = CITY_BBOX[city]
    box = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    kinds = '["place"~"suburb|neighbourhood|quarter"]["name"]'
    return (
        f"[out:json][timeout:180];("
        f"relation({box}){kinds};way({box}){kinds};node({box}){kinds};"
        f");out bb tags;"
    )


def fetch(city: str, *, mirrors: list[str] | None = None) -> list[District]:
    """Fetch a city's districts from OSM and give every one a bounding box."""
    city = city.lower()
    if city not in CITY_BBOX:
        raise ValueError(f"unknown city {city!r}; known: {', '.join(sorted(CITY_BBOX))}")

    session = make_session(retries=1)
    payload = None
    errors: list[str] = []
    try:
        for url in (mirrors or MIRRORS):
            try:
                resp = session.post(url, data={"data": _query(city)}, timeout=200)
                resp.raise_for_status()
                payload = resp.json()
                break
            except Exception as exc:  # noqa: BLE001 - any mirror failure means try the next
                errors.append(f"{url}: {type(exc).__name__}")
    finally:
        session.close()
    if payload is None:
        raise RuntimeError("all Overpass mirrors failed: " + "; ".join(errors))

    raw: list[dict] = []
    for element in payload.get("elements", []):
        tags = element.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        bounds = element.get("bounds")
        if bounds:
            lon = (bounds["minlon"] + bounds["maxlon"]) / 2
            lat = (bounds["minlat"] + bounds["maxlat"]) / 2
        else:
            lon, lat = element.get("lon"), element.get("lat")
            if lon is None or lat is None:
                continue
        raw.append({
            "name": name, "name_en": tags.get("name:en"),
            "place_type": tags.get("place", "neighbourhood"),
            "osm_type": element.get("type", ""), "osm_id": int(element.get("id", 0)),
            "lon": float(lon), "lat": float(lat), "bounds": bounds,
        })

    return _assign_boxes(raw, city)


def _assign_boxes(raw: list[dict], city: str) -> list[District]:
    """Keep real boundaries; derive the rest from nearest-neighbour spacing."""
    centres = [(r["lon"], r["lat"]) for r in raw]
    districts: list[District] = []

    for i, r in enumerate(raw):
        if r["bounds"]:
            b = r["bounds"]
            box = (b["minlon"], b["minlat"], b["maxlon"], b["maxlat"])
            source = "osm"
        else:
            nearest = min(
                (haversine_m(r["lon"], r["lat"], lon, lat)
                 for j, (lon, lat) in enumerate(centres) if j != i),
                default=DEFAULT_RADIUS_M * 2,
            )
            radius = min(MAX_RADIUS_M, max(MIN_RADIUS_M, nearest / 2))
            # Degrees per metre: latitude is constant, longitude shrinks with
            # the cosine of latitude, so a square in metres is not a square in
            # degrees. At Jeddah's latitude that is a 7% difference -- enough to
            # under-cover the east and west edges of every district if ignored.
            dlat = radius / 111_000.0
            dlon = radius / (111_000.0 * math.cos(math.radians(r["lat"])))
            box = (r["lon"] - dlon, r["lat"] - dlat, r["lon"] + dlon, r["lat"] + dlat)
            source = "derived"

        districts.append(District(
            name=r["name"], name_en=r["name_en"], city=city,
            lon=r["lon"], lat=r["lat"],
            min_lon=box[0], min_lat=box[1], max_lon=box[2], max_lat=box[3],
            place_type=r["place_type"], osm_type=r["osm_type"], osm_id=r["osm_id"],
            bbox_source=source,
        ))

    districts.sort(key=lambda d: d.name)
    return districts


def catalog_path(city: str) -> Path:
    return DATA_DIR / f"districts_{city.lower()}.json"


def save(districts: list[District], city: str) -> Path:
    path = catalog_path(city)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(d) for d in districts], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return path


def load(city: str = "jeddah") -> list[District]:
    """Load a committed catalog. No network needed once it is shipped."""
    path = catalog_path(city)
    if not path.exists():
        raise FileNotFoundError(
            f"no catalog for {city!r}. Build one with: python scripts/districts.py {city} --refresh"
        )
    return [District(**d) for d in json.loads(path.read_text(encoding="utf-8"))]


def find(query: str, city: str = "jeddah") -> District | None:
    """Look a district up by Arabic or English name.

    Matching goes through ``gala.normalize``, so "النزهة", "النزهه" and "nuzha"
    all reach the same district -- the whole point of that module.
    """
    districts = load(city)
    key = normalize(query)
    if not key:
        return None

    for d in districts:
        if normalize(d.name) == key or (d.name_en and normalize(d.name_en) == key):
            return d
    # Arabic districts are commonly written with or without the article, so a
    # containment pass catches "نزهة" for "حي النزهة".
    for d in districts:
        haystack = f"{normalize(d.name)} {normalize(d.name_en or '')}"
        if key in haystack or normalize(d.name) in key:
            return d
    return None


def search(query: str, city: str = "jeddah", *, limit: int = 10) -> list[District]:
    """All districts whose name contains the query."""
    key = normalize(query)
    if not key:
        return []
    hits = [
        d for d in load(city)
        if key in f"{normalize(d.name)} {normalize(d.name_en or '')}"
    ]
    return hits[:limit]


# ---------------------------------------------------------------------------
# Assigning places to districts
# ---------------------------------------------------------------------------

# Beyond this, a place is not really "in" the district -- it is just the closest
# label available, which is worse than no label.
MAX_ASSIGN_M = 3000.0


def city_bbox(districts_: list[District]) -> BBox:
    """The bounding box covering every district in the list."""
    return BBox(
        min(d.min_lon for d in districts_), min(d.min_lat for d in districts_),
        max(d.max_lon for d in districts_), max(d.max_lat for d in districts_),
    )


def assign(lon: float, lat: float, districts_: list[District]) -> District | None:
    """Which district a point belongs to.

    Prefers a district whose box actually contains the point, breaking ties by
    distance to the centre -- derived boxes overlap, so containment alone is
    ambiguous. Falls back to the nearest centre within :data:`MAX_ASSIGN_M`.
    """
    containing = [
        d for d in districts_
        if d.min_lon <= lon <= d.max_lon and d.min_lat <= lat <= d.max_lat
    ]
    pool = containing or districts_
    best, best_dist = None, float("inf")
    for d in pool:
        dist = haversine_m(lon, lat, d.lon, d.lat)
        if dist < best_dist:
            best, best_dist = d, dist
    if best is None:
        return None
    if not containing and best_dist > MAX_ASSIGN_M:
        return None
    return best


def stamp(con, city: str = "jeddah") -> dict[str, int]:
    """Write a ``district`` column onto every place in the store.

    Ingesting a whole city once and labelling afterwards is far cheaper than
    running the Overture scan per district: one pass over the Parquet instead
    of 176.
    """
    districts_ = load(city)

    rows = con.execute("SELECT id, lon, lat FROM places").fetchall()
    assigned, orphaned = 0, 0
    updates: list[tuple[str, str]] = []
    for place_id, lon, lat in rows:
        d = assign(lon, lat, districts_)
        if d is None:
            orphaned += 1
            continue
        updates.append((d.name, place_id))
        assigned += 1

    con.executemany("UPDATE places SET district = ? WHERE id = ?", updates)
    return {"assigned": assigned, "outside_any_district": orphaned,
            "districts_used": len({u[0] for u in updates})}
