"""Prominence scoring -- the open-data stand-in for Google's star rating.

The starting observation is that a star rating does two unrelated jobs, and
people conflate them when they say an open stack "cannot match Google":

1. **Display.** "4.6 ★ (1,203 reviews)" shown on the result card.
2. **Ranking.** Deciding that, of the eleven coffee shops matching a query,
   *this* one goes first.

Only (1) genuinely requires proprietary review data. (2) is a relevance problem,
and relevance can be modelled from open signals that correlate well with the
thing a rating is being used as a proxy for: is this a real, established,
well-attested business rather than a stale import or a one-line stub?

The signals, all free:

``confidence``       Overture's own certainty the POI exists. Directly earned
                     from cross-source agreement in their conflation pipeline.
``source_count``     How many independent datasets describe this place. Two
                     providers agreeing is a strong existence signal.
``contact``          Phone / website / address completeness. Businesses that
                     maintain a listing tend to be operating businesses.
``osm_importance``   Nominatim's own prominence measure.
``sitelinks``        Wikipedia language editions covering the place, log-scaled
                     -- separates landmarks from ordinary shops.
``brand``            A resolved brand entity, i.e. a recognised chain.

What this does **not** do is invent a star rating. Displayed ratings stay a
paid feature, behind the :class:`RatingsProvider` seam at the bottom of this
module, and the API reports them as null rather than fabricating a number.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import duckdb

# Weights sum to 1.0. They are a considered prior, not a fitted model: with no
# click logs yet there is nothing to fit against. Once the service has search
# logs, these become the obvious first thing to learn from engagement data.
WEIGHTS: dict[str, float] = {
    "confidence": 0.34,
    "sources": 0.16,
    "contact": 0.16,
    "importance": 0.16,
    "sitelinks": 0.12,
    "brand": 0.06,
}

# Above this many Wikipedia editions, extra ones stop telling us anything new.
SITELINK_SATURATION = 40.0


@dataclass
class Signals:
    confidence: float | None = None
    source_count: int | None = None
    has_phone: bool = False
    has_website: bool = False
    has_address: bool = False
    osm_importance: float | None = None
    wikidata_sitelinks: int | None = None
    has_brand: bool = False


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def score(sig: Signals) -> float:
    """Combine signals into a 0..1 prominence score."""
    confidence = _clamp(sig.confidence or 0.0)

    # Saturating, not linear: the jump from one source to two is meaningful,
    # from four to five is not.
    sources = _clamp(math.log1p(max(sig.source_count or 0, 0)) / math.log(5))

    contact = (
        0.4 * sig.has_phone + 0.35 * sig.has_website + 0.25 * sig.has_address
    )

    importance = _clamp(sig.osm_importance or 0.0)

    sitelinks = _clamp(
        math.log1p(max(sig.wikidata_sitelinks or 0, 0)) / math.log1p(SITELINK_SATURATION)
    )

    brand = 1.0 if sig.has_brand else 0.0

    return _clamp(
        WEIGHTS["confidence"] * confidence
        + WEIGHTS["sources"] * sources
        + WEIGHTS["contact"] * contact
        + WEIGHTS["importance"] * importance
        + WEIGHTS["sitelinks"] * sitelinks
        + WEIGHTS["brand"] * brand
    )


def signals_from_row(row: dict[str, Any]) -> Signals:
    return Signals(
        confidence=row.get("confidence"),
        source_count=row.get("source_count"),
        has_phone=bool(row.get("phone")),
        has_website=bool(row.get("website")),
        has_address=bool(row.get("address_freeform")),
        osm_importance=row.get("osm_importance"),
        wikidata_sitelinks=row.get("wikidata_sitelinks"),
        has_brand=bool(row.get("brand_wikidata") or row.get("brand_name")),
    )


def recompute(con: duckdb.DuckDBPyConnection) -> int:
    """Recompute ``places.prominence`` for the whole corpus.

    Expressed in SQL so a full-corpus refresh stays a single set-based pass;
    ``score`` above remains the readable reference implementation and the two
    are pinned together by ``tests/test_rank.py``.
    """
    w = WEIGHTS
    con.execute(
        f"""
        UPDATE places SET prominence = least(1.0, greatest(0.0,
              {w['confidence']} * coalesce(confidence, 0)
            + {w['sources']}    * least(1.0, ln(1 + greatest(coalesce(source_count, 0), 0)) / ln(5))
            + {w['contact']}    * (0.4  * (nullif(trim(coalesce(phone, '')), '') IS NOT NULL)::INT
                                 + 0.35 * (nullif(trim(coalesce(website, '')), '') IS NOT NULL)::INT
                                 + 0.25 * (nullif(trim(coalesce(address_freeform, '')), '') IS NOT NULL)::INT)
            + {w['importance']} * coalesce(osm_importance, 0)
            + {w['sitelinks']}  * least(1.0, ln(1 + greatest(coalesce(wikidata_sitelinks, 0), 0))
                                              / ln(1 + {SITELINK_SATURATION}))
            + {w['brand']}      * (nullif(trim(coalesce(brand_wikidata, brand_name, '')), '') IS NOT NULL)::INT
        ))
        """
    )
    return con.execute("SELECT count(*) FROM places WHERE prominence IS NOT NULL").fetchone()[0]


# ---------------------------------------------------------------------------
# The paid seam
# ---------------------------------------------------------------------------


@dataclass
class Rating:
    """A displayable rating, always from a licensed upstream."""

    value: float
    count: int
    source: str


@runtime_checkable
class RatingsProvider(Protocol):
    """Interface for a commercial ratings source.

    Kept as a seam rather than an implementation on purpose. Star ratings and
    review text are licensed data; there is no free source, and synthesising a
    number from prominence would be presenting a relevance score as if it were
    user sentiment. The API returns ``rating: null`` until a provider is
    configured -- an honest gap beats a fabricated number.

    To wire one up, implement ``fetch`` against e.g. the Foursquare Places
    Premium endpoints or TomTom's POI details, and register it on the app. Cache
    aggressively: call the upstream when a user opens a place detail view, never
    once per search result.
    """

    def fetch(self, place_id: str, name: str, lon: float, lat: float) -> Rating | None: ...


class NullRatingsProvider:
    """Default provider: reports honestly that no ratings source is configured."""

    def fetch(self, place_id: str, name: str, lon: float, lat: float) -> Rating | None:
        return None
