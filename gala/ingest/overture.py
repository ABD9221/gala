"""Ingest Overture Maps places for a bounding box.

Overture ships the places theme as ~16 Parquet parts of roughly 4.6M rows each
(~73M POI worldwide). Scanning all of them for one city would move tens of GB,
so we prune twice:

1. **File pruning.** Each part's Parquet footer carries per-row-group min/max
   statistics. Reading only footers (a few hundred KB) tells us which parts
   overlap the target longitude range. The parts turn out to be sorted by
   longitude, so a city typically lives in two or three of them.
2. **Row-group pruning.** DuckDB pushes the ``bbox`` predicate down into the
   surviving files and skips non-matching row groups on its own.

The one non-obvious detail: in ``parquet_metadata`` a nested struct field is
addressed as ``'bbox, xmin'`` -- comma and space, not the ``bbox.xmin`` dotted
path used in SQL. Querying the dotted name silently matches zero rows, which
looks exactly like "there is no data for this region".
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import duckdb

from ..config import (
    BBox,
    DEFAULT_HTTP_TIMEOUT,
    OVERTURE_BUCKET_URL,
    OVERTURE_PLACES_PREFIX,
)
from ..http import make_session
from ..normalize import search_text

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def list_parts(prefix: str = OVERTURE_PLACES_PREFIX) -> list[str]:
    """List the Parquet parts of the release as absolute HTTPS URLs."""
    keys: list[str] = []
    token: str | None = None
    with make_session() as client:
        while True:
            params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                params["continuation-token"] = token
            resp = client.get(OVERTURE_BUCKET_URL, params=params, timeout=DEFAULT_HTTP_TIMEOUT)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            keys += [e.text for e in root.iter(f"{_S3_NS}Key") if e.text and e.text.endswith(".parquet")]
            nxt = root.find(f"{_S3_NS}NextContinuationToken")
            if nxt is None or not nxt.text:
                break
            token = nxt.text
    if not keys:
        raise RuntimeError(f"no Parquet parts found under {prefix!r} -- is the release id current?")
    return [OVERTURE_BUCKET_URL + k for k in sorted(keys)]


def parts_covering(con: duckdb.DuckDBPyConnection, bbox: BBox, parts: list[str]) -> list[str]:
    """Keep only the parts whose longitude span overlaps ``bbox``."""
    hits: list[str] = []
    for url in parts:
        overlapping = con.execute(
            """
            SELECT count(*) FROM parquet_metadata($url)
            WHERE path_in_schema = 'bbox, xmin'
              AND TRY_CAST(stats_min AS DOUBLE) <= $max_lon
              AND TRY_CAST(stats_max AS DOUBLE) >= $min_lon
            """,
            {"url": url, "max_lon": bbox.max_lon, "min_lon": bbox.min_lon},
        ).fetchone()[0]
        if overlapping:
            hits.append(url)
    return hits


_PHONE_CLEAN = re.compile(r"[^\d+]")


def clean_phone(raw: str | None, default_cc: str = "+966") -> str | None:
    """Normalize a phone number to E.164-ish form.

    Overture carries Saudi numbers in every shape: ``920020088`` (short code),
    ``011 205 3727`` (national with trunk 0), ``+9668001188884``. Search and
    dedupe both need one canonical form.
    """
    if not raw:
        return None
    s = _PHONE_CLEAN.sub("", raw.strip())
    if not s:
        return None
    if s.startswith("+"):
        return s
    if s.startswith("00"):
        return "+" + s[2:]
    if s.startswith("966"):
        return "+" + s
    if s.startswith("0"):  # national trunk prefix
        return default_cc + s[1:]
    if len(s) in (4, 5, 9, 10) and s.startswith(("9", "8")):
        return s  # short/service code -- leave as dialled
    return default_cc + s


def ingest_bbox(
    con: duckdb.DuckDBPyConnection,
    bbox: BBox,
    *,
    parts: list[str] | None = None,
    min_confidence: float = 0.0,
) -> int:
    """Load every Overture place inside ``bbox`` into the ``places`` table."""
    parts = parts if parts is not None else list_parts()
    covering = parts_covering(con, bbox, parts)
    if not covering:
        return 0

    # Fixed arity: DuckDB UDFs cannot be variadic, and explicit types avoid
    # the numpy-based signature inference path.
    con.create_function(
        "gala_blob",
        lambda a, b, c, d, e, f: search_text(a, b, c, d, e, f),
        ["VARCHAR"] * 6,
        "VARCHAR",
        null_handling="special",
    )

    file_list = "[" + ",".join(f"'{u}'" for u in covering) + "]"
    con.execute(
        f"""
        INSERT OR REPLACE INTO places BY NAME
        SELECT
            id,
            names.primary                                   AS name_primary,
            names.common['ar']                              AS name_ar,
            names.common['en']                              AS name_en,
            categories.primary                              AS category,
            basic_category,
            taxonomy.hierarchy                              AS taxonomy,
            (bbox.xmin + bbox.xmax) / 2                     AS lon,
            (bbox.ymin + bbox.ymax) / 2                     AS lat,
            addresses[1].freeform                           AS address_freeform,
            addresses[1].locality                           AS locality,
            addresses[1].region                             AS region,
            addresses[1].postcode                           AS postcode,
            addresses[1].country                            AS country,
            phones[1]                                       AS phone,
            websites[1]                                     AS website,
            emails[1]                                       AS email,
            socials,
            brand.names.primary                             AS brand_name,
            brand.wikidata                                  AS brand_wikidata,
            confidence,
            len(sources)                                    AS source_count,
            operating_status,
            gala_blob(
                names.primary, names.common['ar'], names.common['en'],
                categories.primary, brand.names.primary, addresses[1].freeform
            )                                               AS search_blob
        FROM read_parquet({file_list})
        WHERE bbox.xmin BETWEEN $min_lon AND $max_lon
          AND bbox.ymin BETWEEN $min_lat AND $max_lat
          AND coalesce(confidence, 0) >= $min_confidence
        """,
        {
            "min_lon": bbox.min_lon, "max_lon": bbox.max_lon,
            "min_lat": bbox.min_lat, "max_lat": bbox.max_lat,
            "min_confidence": min_confidence,
        },
    )

    # Overture uses '' rather than NULL for some absent address/contact values.
    # Left alone, "missing" then means two different things depending on which
    # column you look at, and any `IS NOT NULL` check silently counts blanks as
    # present -- which is exactly how the coverage stats and the prominence
    # score drift apart from each other. Canonicalise on NULL at the boundary.
    for col in ("name_primary", "name_ar", "name_en", "address_freeform", "locality",
                "region", "postcode", "country", "phone", "website", "email",
                "brand_name", "brand_wikidata", "operating_status"):
        con.execute(f"UPDATE places SET {col} = nullif(trim({col}), '') WHERE {col} IS NOT NULL")

    # Phone normalization runs as a second pass: doing it inside the INSERT
    # would mean a Python UDF call per row on the hot path of a bulk scan.
    con.create_function(
        "gala_phone", lambda v: clean_phone(v), ["VARCHAR"], "VARCHAR", null_handling="special"
    )
    con.execute("UPDATE places SET phone = gala_phone(phone) WHERE phone IS NOT NULL")

    return con.execute("SELECT count(*) FROM places").fetchone()[0]
