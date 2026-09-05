"""DuckDB-backed store for the unified place corpus.

DuckDB carries the whole stack here: ``httpfs`` reads Overture Parquet straight
off S3 over HTTPS, ``spatial`` gives real distance math, and ``fts`` provides
BM25. That keeps the prototype to a single embedded file with no server to run,
while staying a straight port to Postgres+PostGIS when the corpus outgrows it.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb

from .config import DB_PATH
from .normalize import tokenize

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    id                 VARCHAR PRIMARY KEY,
    name_primary       VARCHAR,
    name_ar            VARCHAR,
    name_en            VARCHAR,
    category           VARCHAR,
    basic_category     VARCHAR,
    taxonomy           VARCHAR[],
    lon                DOUBLE,
    lat                DOUBLE,
    address_freeform   VARCHAR,
    locality           VARCHAR,
    region             VARCHAR,
    postcode           VARCHAR,
    country            VARCHAR,
    phone              VARCHAR,
    website            VARCHAR,
    email              VARCHAR,
    socials            VARCHAR[],
    brand_name         VARCHAR,
    brand_wikidata     VARCHAR,
    confidence         DOUBLE,
    source_count       INTEGER,
    operating_status   VARCHAR,
    -- enrichment (nullable until the enrich pass runs)
    osm_type           VARCHAR,
    osm_id             BIGINT,
    osm_importance     DOUBLE,
    opening_hours      VARCHAR,
    wikidata_id        VARCHAR,
    wikidata_sitelinks INTEGER,
    photo_url          VARCHAR,
    photo_attribution  VARCHAR,
    enriched_at        TIMESTAMP,
    -- derived
    prominence         DOUBLE,
    search_blob        VARCHAR,
    -- corpus repair (gala.quality). Corrections live beside the source values
    -- rather than replacing them, so a bad rule stays auditable and revertible.
    category_final     VARCHAR,
    category_source    VARCHAR,
    category_confidence DOUBLE,
    duplicate_of       VARCHAR,
    -- SerpApi enrichment (gala.enrich.serpapi). Google-derived and therefore
    -- NOT part of the owned corpus: refreshed on a TTL, never treated as
    -- permanent. See the module docstring for why the distinction matters.
    serpapi_rating     DOUBLE,
    serpapi_reviews    INTEGER,
    serpapi_hours      VARCHAR,
    serpapi_price      VARCHAR,
    serpapi_place_id   VARCHAR,
    serpapi_fetched_at TIMESTAMP,
    -- The unit of work for prospecting: set by gala.districts.stamp().
    district           VARCHAR
);

CREATE INDEX IF NOT EXISTS places_lonlat ON places (lon, lat);
CREATE INDEX IF NOT EXISTS places_category ON places (category);
"""


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` will not
# alter a table that already exists, so a store built by an earlier version is
# missing them and every query naming one fails to bind. Applied on every
# writable open, which is cheap and keeps upgrades from being a manual step.
MIGRATIONS = [
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS category_final VARCHAR",
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS category_source VARCHAR",
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS category_confidence DOUBLE",
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS duplicate_of VARCHAR",
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS serpapi_rating DOUBLE",
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS serpapi_reviews INTEGER",
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS serpapi_hours VARCHAR",
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS serpapi_price VARCHAR",
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS serpapi_place_id VARCHAR",
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS serpapi_fetched_at TIMESTAMP",
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS district VARCHAR",
    # Which upstream a row came from: 'overture' (open corpus) or 'serpapi'
    # (Google, via SerpApi). The two carry different rights -- Overture rows are
    # owned outright, SerpApi rows are a licensed live copy with a cache life --
    # so anything that exports or retains data has to be able to tell them apart.
    "ALTER TABLE places ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'overture'",
]


def migrate(con: duckdb.DuckDBPyConnection) -> None:
    """Bring an older store up to the current schema."""
    for ddl in MIGRATIONS:
        con.execute(ddl)


def connect(db_path: str | os.PathLike[str] | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the store, loading the extensions every code path depends on."""
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    for ext in ("httpfs", "spatial", "fts"):
        con.execute(f"INSTALL {ext}; LOAD {ext};")
    if not read_only:
        con.execute(SCHEMA)
        migrate(con)
    return con


INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_len (place_id VARCHAR PRIMARY KEY, len INTEGER);
CREATE TABLE IF NOT EXISTS postings (term VARCHAR, place_id VARCHAR, tf INTEGER);
CREATE TABLE IF NOT EXISTS terms (term VARCHAR PRIMARY KEY, df INTEGER);
CREATE INDEX IF NOT EXISTS postings_term ON postings (term);
CREATE INDEX IF NOT EXISTS terms_term ON terms (term);
"""


def build_index(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Build an inverted index over ``search_blob``.

    DuckDB ships an ``fts`` extension, but we do not use it here for two
    concrete reasons:

    * **Autocomplete needs prefix matching.** ``create_fts_index`` only matches
      whole stemmed tokens, so "مطع" would return nothing. Google Places
      autocomplete is a headline feature, so prefix search is not optional.
    * **Its tokenizer fights ours.** The extension's ``lower`` /
      ``strip_accents`` / ``ignore`` pipeline is built around ASCII and would
      re-process text that ``gala.normalize`` has already canonicalized.

    An explicit postings table costs a few dozen lines, keeps the BM25 maths
    visible and tunable, and ports to Postgres unchanged.
    """
    con.execute(INDEX_SCHEMA)
    con.execute("DELETE FROM postings; DELETE FROM terms; DELETE FROM doc_len;")

    rows = con.execute(
        """
        SELECT id, name_primary, name_ar, name_en, brand_name,
               coalesce(category_final, category), address_freeform
        FROM places
        WHERE coalesce(duplicate_of, '') = ''
        """
    ).fetchall()

    postings: dict[tuple[str, str], int] = {}
    lengths: list[tuple[str, int]] = []
    for place_id, name, name_ar, name_en, brand, category, address in rows:
        # Field weights, not a flat bag of words. Without them the address
        # dominates: every shop inside Kingdom Centre carries "Kingdom Centre"
        # in its address, so a search for the mall returned its tenants and
        # buried the mall itself. Weighting name and brand above address puts
        # the building back on top, and BM25 consumes the weights as term
        # frequencies with no change to the scoring maths.
        weighted: list[tuple[str, int]] = []
        for value, weight in (
            (name, 3), (name_ar, 3), (name_en, 3), (brand, 3),
            (category, 2), (address, 1),
        ):
            for token in tokenize(value):
                weighted.append((token, weight))
        if not weighted:
            continue
        lengths.append((place_id, sum(w for _, w in weighted)))
        for token, weight in weighted:
            postings[(token, place_id)] = postings.get((token, place_id), 0) + weight

    con.executemany("INSERT OR REPLACE INTO doc_len VALUES (?, ?)", lengths)
    con.executemany(
        "INSERT INTO postings VALUES (?, ?, ?)",
        [(t, pid, tf) for (t, pid), tf in postings.items()],
    )
    con.execute(
        "INSERT OR REPLACE INTO terms SELECT term, count(DISTINCT place_id) FROM postings GROUP BY term"
    )
    return {
        "documents": len(lengths),
        "terms": con.execute("SELECT count(*) FROM terms").fetchone()[0],
        "postings": len(postings),
    }


def stats(con: duckdb.DuckDBPyConnection) -> dict[str, int | float]:
    """Coverage counters -- the numbers that tell you if the corpus is usable."""
    row = con.execute(
        """
        SELECT count(*),
               count(phone), count(website), count(address_freeform),
               count(name_ar), count(name_en),
               count(opening_hours), count(photo_url), count(wikidata_id),
               coalesce(avg(confidence), 0)
        FROM places
        """
    ).fetchone()
    keys = ["total", "phone", "website", "address", "name_ar", "name_en",
            "opening_hours", "photo", "wikidata", "avg_confidence"]
    return dict(zip(keys, row))
