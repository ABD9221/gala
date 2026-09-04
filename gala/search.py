"""Query layer: text search, nearby, autocomplete, details.

Relevance blends three independent signals, because any one of them alone
produces a familiar failure mode:

* **BM25 over the normalized blob** -- text match. Alone it ranks a defunct
  one-line stub above a landmark whose name matches slightly less exactly.
* **Distance decay** -- proximity to the search origin. Alone it returns
  whatever is nearest regardless of what was asked for.
* **Prominence** (:mod:`gala.rank`) -- the open-signal stand-in for a star
  rating. Alone it returns the same famous places for every query.

The weights are a deliberate prior rather than a fitted model; with no click
logs yet there is nothing to fit against. They are exposed as
:data:`DEFAULT_WEIGHTS` so callers can tune per surface -- autocomplete wants
more text weight, a "near me" list wants more distance weight.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import duckdb
from rapidfuzz import fuzz
from rapidfuzz import process as fuzz_process

from .hours import is_open, weekly_schedule
from .normalize import normalize, tokenize

# BM25 parameters. Standard defaults: k1 controls term-frequency saturation,
# b controls length normalization.
K1 = 1.2
B = 0.75

# Distance at which the geographic score halves. 1.5 km suits city search --
# far enough that the next neighbourhood still competes, close enough that
# "coffee near me" does not surface the other side of Riyadh.
DISTANCE_HALF_LIFE_M = 1500.0

# `text` is BM25 over every indexed field; `name` is how completely the query
# accounts for the place's own name. They are separate because BM25 cannot tell
# "Kingdom Centre" from "Zara Kingdom Centre" -- both contain every query term,
# and the shop's shorter document actually scores *better* under length
# normalization. Whole-string similarity is what separates the mall from its
# tenants, and it is why searching a business name returns the business.
DEFAULT_WEIGHTS = {"text": 0.42, "name": 0.16, "distance": 0.22, "prominence": 0.20}


@dataclass
class SearchResult:
    id: str
    name: str | None
    name_ar: str | None
    name_en: str | None
    category: str | None
    lon: float
    lat: float
    address: str | None
    phone: str | None
    website: str | None
    photo_url: str | None
    photo_attribution: str | None
    opening_hours: str | None
    open_now: bool | None
    prominence: float | None
    distance_m: float | None
    text_score: float
    score: float
    rating: float | None = None
    rating_count: int | None = None
    rating_source: str | None = None
    debug: dict[str, float] = field(default_factory=dict)


def _rows_to_dicts(cur: duckdb.DuckDBPyConnection) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _bm25_sql(terms: list[str]) -> tuple[str, list]:
    """BM25 scoring CTE over the postings table."""
    placeholders = ",".join(["(?)"] * len(terms))
    sql = f"""
    WITH q(term) AS (VALUES {placeholders}),
         corpus AS (SELECT count(*) AS n, avg(len) AS avgdl FROM doc_len),
         scored AS (
            SELECT p.place_id,
                   sum(
                     ln(1 + (corpus.n - t.df + 0.5) / (t.df + 0.5))
                     * (p.tf * ({K1} + 1))
                     / (p.tf + {K1} * (1 - {B} + {B} * d.len / corpus.avgdl))
                   ) AS text_score,
                   count(DISTINCT p.term) AS matched_terms
            FROM postings p
            JOIN q         ON q.term = p.term
            JOIN terms t   ON t.term = p.term
            JOIN doc_len d ON d.place_id = p.place_id
            CROSS JOIN corpus
            GROUP BY p.place_id
         )
    """
    return sql, list(terms)


def search(
    con: duckdb.DuckDBPyConnection,
    query: str,
    *,
    lon: float | None = None,
    lat: float | None = None,
    radius_m: float | None = None,
    category: str | None = None,
    open_now: bool = False,
    now: datetime | None = None,
    limit: int = 20,
    weights: dict[str, float] | None = None,
    require_all_terms: bool = False,
) -> list[SearchResult]:
    """Blended text + geography + prominence search."""
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    terms = tokenize(query)
    if not terms:
        return nearby(con, lon=lon, lat=lat, radius_m=radius_m, category=category,
                      open_now=open_now, now=now, limit=limit) if lon is not None else []

    query_norm = normalize(query)
    terms = _with_fuzzy_fallback(con, terms)
    cte, params = _bm25_sql(terms)

    # Distance is computed in SQL so the radius filter prunes before the sort
    # rather than after the limit.
    has_origin = lon is not None and lat is not None
    dist_expr = (
        "2 * 6371000 * asin(sqrt(pow(sin(radians(pl.lat - ?) / 2), 2)"
        " + cos(radians(?)) * cos(radians(pl.lat))"
        " * pow(sin(radians(pl.lon - ?) / 2), 2)))"
        if has_origin else "NULL"
    )

    sql = f"""
    {cte}
    SELECT pl.*, s.text_score, s.matched_terms, {dist_expr} AS distance_m
    FROM scored s JOIN places pl ON pl.id = s.place_id
    WHERE coalesce(pl.duplicate_of, '') = ''
    """
    if has_origin:
        params += [lat, lat, lon]
    if require_all_terms:
        sql += " AND s.matched_terms >= ?"
        params.append(len(terms))
    if category:
        sql += " AND coalesce(pl.category_final, pl.category) = ?"
        params.append(category)
    if radius_m and has_origin:
        sql += f" AND {dist_expr} <= ?"
        params += [lat, lat, lon, radius_m]
    # Over-fetch: open_now is evaluated in Python (the parser lives there), so
    # the SQL limit has to leave room for rows that filter out afterwards.
    sql += " ORDER BY s.text_score DESC LIMIT ?"
    params.append(limit * 8 if open_now else limit * 4)

    rows = _rows_to_dicts(con.execute(sql, params))
    max_text = max((r["text_score"] for r in rows), default=0.0) or 1.0
    when = now or datetime.now()

    results: list[SearchResult] = []
    for r in rows:
        state = is_open(r.get("opening_hours"), when)
        if open_now and state is not True:
            continue
        results.append(_to_result(r, max_text, weights, when, state, query_norm))

    results.sort(key=lambda x: x.score, reverse=True)
    return results[:limit]


def _name_match(query_norm: str, r: dict) -> float:
    """How completely the query accounts for the place's whole name (0..1).

    ``ratio`` rather than ``token_set_ratio`` on purpose: extra tokens in the
    name must cost something, otherwise every tenant of a mall matches the
    mall's name perfectly.
    """
    if not query_norm:
        return 0.0
    candidates = [
        normalize(n)
        for n in (r.get("name_primary"), r.get("name_ar"), r.get("name_en"), r.get("brand_name"))
        if n
    ]
    return max((fuzz.ratio(query_norm, c) / 100.0 for c in candidates), default=0.0)


def _to_result(
    r: dict,
    max_text: float,
    weights: dict[str, float],
    when: datetime,
    state: bool | None,
    query_norm: str = "",
) -> SearchResult:
    text_norm = (r["text_score"] / max_text) if max_text else 0.0
    name_score = _name_match(query_norm, r)
    dist = r.get("distance_m")
    geo = math.exp(-dist / DISTANCE_HALF_LIFE_M) if dist is not None else 0.0
    prom = r.get("prominence") or 0.0

    # With no origin the geographic term is undefined; drop it and renormalize
    # so a query without coordinates is not silently scored out of 0.75.
    parts = {
        "text": weights.get("text", 0.0) * text_norm,
        "name": weights.get("name", 0.0) * name_score,
        "prominence": weights.get("prominence", 0.0) * prom,
    }
    if dist is None:
        # No origin means the geographic term is undefined. Renormalise over the
        # remaining weights instead of silently capping every score below 1.
        total = sum(weights.get(k, 0.0) for k in parts) or 1.0
        score = sum(parts.values()) / total
    else:
        score = sum(parts.values()) + weights.get("distance", 0.0) * geo

    return SearchResult(
        id=r["id"],
        name=r.get("name_primary"),
        name_ar=r.get("name_ar"),
        name_en=r.get("name_en"),
        category=r.get("category_final") or r.get("category"),
        lon=r["lon"], lat=r["lat"],
        address=r.get("address_freeform"),
        phone=r.get("phone"),
        website=r.get("website"),
        photo_url=r.get("photo_url"),
        photo_attribution=r.get("photo_attribution"),
        opening_hours=r.get("opening_hours"),
        open_now=state,
        prominence=r.get("prominence"),
        distance_m=dist,
        text_score=r["text_score"],
        score=score,
        debug={"text_norm": text_norm, "name": name_score, "geo": geo, "prominence": prom},
    )


def _with_fuzzy_fallback(con: duckdb.DuckDBPyConnection, terms: list[str], *, cutoff: int = 82) -> list[str]:
    """Replace terms absent from the vocabulary with their closest known form.

    Typos are the normal case in POI search -- on phone keyboards, and across a
    language whose transliterations nobody spells consistently. A term that
    exists in no document contributes nothing to BM25, so an otherwise good
    query returns an empty page. Substituting the nearest indexed term degrades
    to "did you mean" behaviour instead of a dead end.
    """
    known = {t for (t,) in con.execute("SELECT term FROM terms").fetchall()}
    out: list[str] = []
    vocabulary: list[str] | None = None
    for term in terms:
        if term in known:
            out.append(term)
            continue
        if vocabulary is None:
            vocabulary = sorted(known)
        match = fuzz_process.extractOne(term, vocabulary, score_cutoff=cutoff)
        out.append(match[0] if match else term)
    return out


def nearby(
    con: duckdb.DuckDBPyConnection,
    *,
    lon: float,
    lat: float,
    radius_m: float | None = 1000,
    category: str | None = None,
    open_now: bool = False,
    now: datetime | None = None,
    limit: int = 20,
) -> list[SearchResult]:
    """Places around a point, ranked by proximity and prominence."""
    dist_expr = (
        "2 * 6371000 * asin(sqrt(pow(sin(radians(lat - ?) / 2), 2)"
        " + cos(radians(?)) * cos(radians(lat))"
        " * pow(sin(radians(lon - ?) / 2), 2)))"
    )
    sql = f"SELECT *, {dist_expr} AS distance_m FROM places WHERE coalesce(duplicate_of, '') = ''"
    params: list = [lat, lat, lon]
    if category:
        sql += " AND coalesce(category_final, category) = ?"
        params.append(category)
    if radius_m:
        sql += f" AND {dist_expr} <= ?"
        params += [lat, lat, lon, radius_m]
    sql += " ORDER BY distance_m LIMIT ?"
    params.append(limit * 8 if open_now else limit * 3)

    rows = _rows_to_dicts(con.execute(sql, params))
    when = now or datetime.now()
    results: list[SearchResult] = []
    for r in rows:
        state = is_open(r.get("opening_hours"), when)
        if open_now and state is not True:
            continue
        r["text_score"] = 0.0
        results.append(_to_result(r, 1.0, {"text": 0.0, "distance": 0.6, "prominence": 0.4}, when, state))
    results.sort(key=lambda x: x.score, reverse=True)
    return results[:limit]


def autocomplete(
    con: duckdb.DuckDBPyConnection,
    prefix: str,
    *,
    lon: float | None = None,
    lat: float | None = None,
    limit: int = 8,
) -> list[SearchResult]:
    """Prefix suggestions -- the reason this stack does not use DuckDB's FTS.

    The last token is matched as a prefix and earlier tokens as whole terms, so
    "قهوه الع" narrows as the user types rather than waiting for a complete word.
    """
    tokens = tokenize(prefix, expand=False)
    if not tokens:
        return []
    *complete, partial = tokens

    sql = "SELECT DISTINCT place_id FROM postings WHERE term LIKE ? || '%'"
    params: list = [partial]
    for term in complete:
        sql += " AND place_id IN (SELECT place_id FROM postings WHERE term = ?)"
        params.append(term)

    ids = [r[0] for r in con.execute(sql, params).fetchall()]
    if not ids:
        return []

    placeholders = ",".join("?" * len(ids))
    dist_expr = (
        "2 * 6371000 * asin(sqrt(pow(sin(radians(lat - ?) / 2), 2)"
        " + cos(radians(?)) * cos(radians(lat))"
        " * pow(sin(radians(lon - ?) / 2), 2)))"
        if lon is not None and lat is not None else "NULL"
    )
    geo_params = [lat, lat, lon] if lon is not None and lat is not None else []
    rows = _rows_to_dicts(
        con.execute(
            f"SELECT *, {dist_expr} AS distance_m FROM places WHERE id IN ({placeholders})"
            " AND coalesce(duplicate_of, '') = ''"
            " ORDER BY prominence DESC NULLS LAST LIMIT ?",
            geo_params + ids + [limit * 3],
        )
    )
    when = datetime.now()
    out = []
    for r in rows:
        r["text_score"] = 1.0
        out.append(_to_result(
            r, 1.0, {"text": 0.15, "name": 0.35, "distance": 0.2, "prominence": 0.3},
            when, is_open(r.get("opening_hours"), when), normalize(prefix),
        ))
    out.sort(key=lambda x: x.score, reverse=True)
    return out[:limit]


def details(con: duckdb.DuckDBPyConnection, place_id: str, *, now: datetime | None = None) -> dict | None:
    """Full record for one place, including the parsed weekly schedule."""
    rows = _rows_to_dicts(con.execute("SELECT * FROM places WHERE id = ?", [place_id]))
    if not rows:
        return None
    r = rows[0]
    when = now or datetime.now()
    r["open_now"] = is_open(r.get("opening_hours"), when)
    r["schedule"] = weekly_schedule(r.get("opening_hours"))
    return r
