"""Ingest businesses directly from Google, via SerpApi, as the primary source.

``gala.enrich.serpapi`` bolts Google's ratings onto an Overture corpus. This
module skips Overture: the places themselves come from Google. Measured on the
Taysir/Mraykh area, that is a large difference in the one number that matters
for prospecting:

===============================  ==============  ===========
                                 Overture        Google
===============================  ==============  ===========
places in Mraykh district        23              hundreds
share with no website            11-14%          ~45%
share with a phone               70%             ~95%
===============================  ==============  ===========

Overture's gap is worst exactly where prospecting is best -- small, new,
independent businesses on the edge of a city. Google has them.

**What this costs, and what it does not buy.** Every place here is metered and
licensed. Google's terms permit storing place IDs indefinitely but not
warehousing names, ratings, hours or phone numbers, so rows land with
``source='serpapi'`` and a fetch timestamp, and ``gala.enrich.serpapi.purge_stale``
expires them. An Overture row is yours; a row here is rented, and the schema
keeps the two distinguishable rather than blurring them into one corpus.

Search coverage is also not enumeration. A Maps query returns ~20 results a
page, ranked -- there is no "list every business in this polygon". Coverage
comes from sweeping many category terms across many points and accepting that
the tail costs progressively more per new place.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

import duckdb
import requests

from ..config import DEFAULT_HTTP_TIMEOUT
from ..districts import District
from ..http import make_session
from ..normalize import search_text
from .overture import clean_phone

ENDPOINT = "https://serpapi.com/search.json"
ACCOUNT_ENDPOINT = "https://serpapi.com/account.json"

RESULTS_PER_PAGE = 20

# Arabic terms, because the map is queried with hl=ar and Saudi businesses are
# named in Arabic. The list is ordered by how much a trade needs a website, so
# a truncated budget still spends itself on the best prospects.
SWEEP_TERMS = [
    "مطاعم", "كافيه", "صالون تجميل", "حلاق", "عيادة اسنان", "عيادة",
    "صالة رياضية", "تنظيم حفلات", "كوش وقاعات", "محل ورد", "محل ملابس",
    "مفروشات", "مجوهرات", "ورشة سيارات", "مغسلة ملابس", "حلويات",
    "مخبز", "عطور", "سوبرماركت", "صيدلية",
]


class SerpApiError(RuntimeError):
    pass


@dataclass
class SweepReport:
    calls: int = 0
    raw_results: int = 0
    unique_places: int = 0
    inserted: int = 0
    updated: int = 0
    with_website: int = 0
    without_website: int = 0
    with_phone: int = 0
    with_rating: int = 0
    terms_exhausted: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], 0)}


def account(api_key: str, *, session: requests.Session | None = None) -> dict:
    """Plan and remaining quota. Does not consume a search."""
    owns = session is None
    session = session or make_session()
    try:
        resp = session.get(ACCOUNT_ENDPOINT, params={"api_key": api_key}, timeout=DEFAULT_HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    finally:
        if owns:
            session.close()


def _search(
    session: requests.Session, api_key: str, term: str, lat: float, lon: float,
    *, zoom: int, start: int | None = None,
) -> dict:
    params = {
        "engine": "google_maps", "q": term, "ll": f"@{lat},{lon},{zoom}z",
        "hl": "ar", "api_key": api_key,
    }
    if start:
        params["start"] = start
    resp = session.get(ENDPOINT, params=params, timeout=60)
    if resp.status_code == 401:
        raise SerpApiError("SerpApi rejected the key (401)")
    if resp.status_code == 429:
        raise SerpApiError("SerpApi quota exhausted (429)")
    resp.raise_for_status()
    payload = resp.json()
    if err := payload.get("error"):
        # "hasn't returned any results" is a normal empty page, not a failure.
        if "not return" in str(err) or "no results" in str(err).lower():
            return {"local_results": []}
        raise SerpApiError(str(err))
    return payload


def sweep(
    con: duckdb.DuckDBPyConnection,
    district: District,
    api_key: str,
    *,
    terms: list[str] | None = None,
    max_calls: int = 20,
    pages_per_term: int = 2,
    zoom: int = 15,
) -> SweepReport:
    """Sweep one district for businesses and write them into the store.

    ``max_calls`` is a hard budget, checked before every request. Quota is the
    scarce resource here, and an unbounded sweep would spend a month's plan on
    one district.
    """
    terms = terms or SWEEP_TERMS
    report = SweepReport()
    session = make_session()
    seen: set[str] = set()
    now = dt.datetime.now(dt.timezone.utc)

    try:
        for term in terms:
            if report.calls >= max_calls:
                break
            for page in range(pages_per_term):
                if report.calls >= max_calls:
                    break
                try:
                    payload = _search(
                        session, api_key, term, district.lat, district.lon,
                        zoom=zoom, start=page * RESULTS_PER_PAGE or None,
                    )
                except SerpApiError as exc:
                    report.error = str(exc)
                    return report
                report.calls += 1

                results = payload.get("local_results") or []
                report.raw_results += len(results)
                new_on_page = 0
                for raw in results:
                    place_id = raw.get("place_id") or raw.get("data_id")
                    if not place_id or place_id in seen:
                        continue
                    seen.add(place_id)
                    new_on_page += 1
                    _upsert(con, raw, district, now, report)

                # A page that adds nothing means this term is mined out here;
                # paging further just burns quota on duplicates.
                if new_on_page == 0:
                    report.terms_exhausted.append(term)
                    break
                if len(results) < RESULTS_PER_PAGE:
                    break
    finally:
        session.close()

    report.unique_places = len(seen)
    return report


def _upsert(
    con: duckdb.DuckDBPyConnection, raw: dict, district: District,
    now: dt.datetime, report: SweepReport,
) -> None:
    gps = raw.get("gps_coordinates") or {}
    lon, lat = gps.get("longitude"), gps.get("latitude")
    if lon is None or lat is None:
        return

    place_id = raw.get("place_id") or raw.get("data_id")
    website = (raw.get("website") or "").strip() or None
    phone = clean_phone(raw.get("phone"))
    name = raw.get("title") or ""
    category = raw.get("type")

    report.with_website += bool(website)
    report.without_website += not website
    report.with_phone += bool(phone)
    report.with_rating += bool(raw.get("rating"))

    existing = con.execute(
        "SELECT id FROM places WHERE serpapi_place_id = ?", [place_id]
    ).fetchone()

    # Google's own category string is the category. It is hand-curated per
    # business, which is why none of gala.quality's repair machinery -- built
    # for Overture's mislabelling -- is applied to these rows.
    values = {
        "name_primary": name,
        "category": category,
        "category_final": category,
        "category_source": "google",
        "category_confidence": 1.0,
        "lon": float(lon), "lat": float(lat),
        "address_freeform": raw.get("address"),
        "phone": phone,
        "website": website,
        "district": district.name,
        "source": "serpapi",
        "confidence": 0.95,
        "source_count": 1,
        "serpapi_rating": raw.get("rating"),
        "serpapi_reviews": raw.get("reviews"),
        "serpapi_hours": raw.get("hours") or raw.get("open_state"),
        "serpapi_price": raw.get("price"),
        "serpapi_place_id": place_id,
        "serpapi_fetched_at": now,
        "search_blob": search_text(name, category, raw.get("address")),
    }

    if existing:
        assignments = ", ".join(f"{k} = ?" for k in values)
        con.execute(f"UPDATE places SET {assignments} WHERE id = ?",
                    [*values.values(), existing[0]])
        report.updated += 1
    else:
        columns = ", ".join(["id", *values])
        placeholders = ", ".join(["?"] * (len(values) + 1))
        con.execute(f"INSERT INTO places ({columns}) VALUES ({placeholders})",
                    [str(uuid.uuid4()), *values.values()])
        report.inserted += 1
