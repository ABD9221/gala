"""Lead scoring for web-development prospecting.

The corpus was built for search, where ``prominence`` is the right ranking:
it rewards well-attested, well-known places. For prospecting it is exactly
backwards. Sorting the "has a phone, has no website" set by prominence puts
Kingdom Centre, Dunkin' Donuts, Hardee's, Audi and Sephora on top -- all of
which have corporate web teams. Their website is missing from *the data*, not
from the world.

A good lead is almost the opposite of a prominent place:

* **Reachable** -- a phone number, or there is nothing to act on.
* **Genuinely without a website** -- and an Instagram page is not a website.
* **Independent, not a chain** -- a branch of a 40-outlet brand cannot buy a
  website from you; head office already did.
* **In a trade that buys websites** -- a restaurant, salon or clinic does; an
  ATM, a mosque or a bus stop does not.
* **Alive** -- a business with real customers has budget. Overture's confidence
  is a weak proxy; review counts from :mod:`gala.enrich.serpapi` are a strong
  one when available.

Everything here reads the corpus that is already built. No extra fetching is
required to produce a first list.
"""
from __future__ import annotations

import collections
import math
import re
from dataclasses import dataclass, field

import duckdb

from .config import BBox
from .normalize import normalize
from .quality import BRAND_CATEGORIES

# ---------------------------------------------------------------------------
# What counts as "not really a website"
# ---------------------------------------------------------------------------

# A business whose only web presence is one of these has no website in the sense
# that matters here -- and is a *better* lead than one with no link at all: it
# has already tried to be findable online and settled for someone else's
# platform. `business.site` is Google's own free one-page stub, which is close
# to a signed confession that they wanted a site and did not get one.
# Every pattern anchors the domain on a start-of-string, dot or slash. Writing
# it as `(^|\.)a\.com|b\.com` instead binds the anchor to the first
# alternative only, so `https://linktr.ee/x` (slash before the domain, no dot)
# reads as a real website -- the exact case this list exists to catch.
_DOMAIN = r"(?:^|[./])"
NON_WEBSITE_PATTERNS = [
    (re.compile(_DOMAIN + r"instagram\.com", re.I), "instagram"),
    (re.compile(_DOMAIN + r"facebook\.com|(?:^|[./])fb\.(?:com|me)", re.I), "facebook"),
    (re.compile(_DOMAIN + r"(?:twitter|x)\.com", re.I), "twitter/x"),
    (re.compile(_DOMAIN + r"tiktok\.com", re.I), "tiktok"),
    (re.compile(r"snapchat", re.I), "snapchat"),
    (re.compile(_DOMAIN + r"linkedin\.com", re.I), "linkedin"),
    (re.compile(_DOMAIN + r"(?:linktr\.ee|linkin\.bio|beacons\.ai|bio\.link|taplink\.)", re.I), "link aggregator"),
    (re.compile(_DOMAIN + r"(?:wa\.me|whatsapp\.com|api\.whatsapp)", re.I), "whatsapp"),
    (re.compile(_DOMAIN + r"business\.site", re.I), "google business stub"),
    (re.compile(_DOMAIN + r"(?:blogspot\.|wordpress\.com|wixsite\.com|weebly\.com|blogger\.com|sites\.google\.com)", re.I), "free site builder"),
    (re.compile(_DOMAIN + r"(?:easymenu|hungerstation|jahez|talabat|zomato|foodpanda|deliveroo|ubereats|thechefz)", re.I), "delivery aggregator"),
    (re.compile(_DOMAIN + r"(?:google\.|maps\.app|goo\.gl)", re.I), "google link"),
    (re.compile(_DOMAIN + r"(?:youtube\.com|youtu\.be)", re.I), "youtube"),
]

# Trades that buy websites, weighted by how much a site is worth to them.
# 1.0 = a website is close to essential; 0.0 = it would never be bought.
CATEGORY_FIT: dict[str, float] = {
    # Hospitality and retail -- menus, bookings, galleries.
    "restaurant": 1.0, "cafe": 0.95, "coffee_shop": 0.95, "bakery": 0.9,
    "fast_food_restaurant": 0.7, "donuts": 0.7, "pizza_restaurant": 0.85,
    "seafood_restaurant": 1.0, "mediterranean_restaurant": 1.0,
    "indian_restaurant": 1.0, "breakfast_and_brunch_restaurant": 1.0,
    "hotel": 0.9, "hookah_bar": 0.8,
    # Appointment-driven services -- the strongest fit of all.
    "beauty_salon": 1.0, "hair_salon": 1.0, "spa": 1.0, "nail_salon": 1.0,
    "barber": 1.0, "medical_clinic": 1.0, "dentist": 1.0, "physiotherapist": 1.0,
    "veterinarian": 1.0, "gym": 0.95, "fitness_center": 0.95,
    "photography_store_and_services": 1.0, "party_and_event_planning": 1.0,
    "wedding_planning": 1.0, "travel_services": 0.95, "driving_school": 0.9,
    # Professional services -- credibility purchases.
    "lawyer": 1.0, "accounting": 1.0, "real_estate_agent": 1.0,
    "insurance_agency": 0.9, "marketing_agency": 0.8, "media_agency": 0.8,
    "professional_services": 0.9, "consulting": 0.9, "education": 0.9,
    "school": 0.8, "training_centre": 0.9,
    # Considered-purchase retail -- catalogue value.
    "furniture_store": 1.0, "jewelry_store": 1.0, "clothing_store": 0.9,
    "perfume_store": 0.9, "flowers_and_gifts_shop": 1.0, "car_dealer": 0.85,
    "automotive_repair": 0.9, "auto_repair_shop": 0.9, "electronics_store": 0.85,
    "art_gallery": 1.0, "book_store": 0.8, "optician": 0.85,
    "eyewear_and_optician": 0.85, "cosmetic_and_beauty_supplies": 0.85,
    "shopping": 0.7, "shopping_center": 0.5,
    # Walk-in trade: a site adds little.
    "grocery_store": 0.35, "convenience_store": 0.2, "pharmacy": 0.3,
    "gas_station": 0.15, "car_wash": 0.4, "laundry": 0.5,
    # Never.
    "atm": 0.0, "bank_credit_union": 0.0, "mosque": 0.0,
    "government_office": 0.0, "embassy": 0.0, "hospital": 0.1,
    "university": 0.1, "post_office": 0.0, "police": 0.0, "bus_stop": 0.0,
    "parking": 0.0, "public_restroom": 0.0, "school_district": 0.0,
}
DEFAULT_CATEGORY_FIT = 0.55  # unknown trade: plausible, not promoted

# Google returns categories in Arabic, and none of them match the English slugs
# above -- measured at 0 of 377 rows in the Mraykh sweep, meaning every Google
# lead was being scored on the default. Google's vocabulary also has a long
# tail ("متجر مفروشات المتاجر الصغيرة والكبيرة"), so an exact-match table would
# keep missing. Keywords are checked against the *normalized* category, most
# specific first: "صيدلية" has to be tested before the generic "متجر", or a
# pharmacy scores as retail.
ARABIC_CATEGORY_FIT: list[tuple[str, float]] = [
    # Never worth a website.
    ("صراف", 0.0), ("بنك", 0.0), ("مسجد", 0.0), ("جامع", 0.0),
    ("حكوم", 0.0), ("سفاره", 0.0), ("شرطه", 0.0), ("بريد", 0.0),
    ("محطه وقود", 0.15), ("محطة بنزين", 0.15),
    # Walk-in trade.
    ("صيدل", 0.3), ("بقاله", 0.35), ("سوبرماركت", 0.35), ("تموينات", 0.3),
    ("مغسل", 0.5), ("غسيل سيارات", 0.4),
    # Appointment-driven: the strongest fit there is.
    ("صالون", 1.0), ("حلاق", 1.0), ("تجميل", 1.0), ("سبا", 1.0),
    ("عياده", 1.0), ("اسنان", 1.0), ("مستوصف", 1.0), ("طبي", 0.95),
    ("مركز صحي", 0.95), ("بيطري", 1.0), ("علاج طبيعي", 1.0),
    # Hospitals have in-house IT and procurement; a freelancer does not sell
    # them a website. Placed before the "طبي" rule so it wins.
    ("مستشفي", 0.1),
    ("رياض", 0.95), ("لياقه", 0.95), ("نادي", 0.9),
    ("حفلات", 1.0), ("زفاف", 1.0), ("مناسبات", 1.0), ("كوش", 1.0),
    ("حدث", 1.0), ("احداث", 1.0), ("تنظيم", 0.95), ("مدرب", 0.9),
    ("تصوير", 1.0), ("مصور", 1.0), ("سياح", 0.95), ("سفر", 0.95),
    # Hospitality.
    ("مطعم", 1.0), ("مقهي", 0.95), ("كافيه", 0.95), ("كوفي", 0.95),
    ("مخبز", 0.9), ("حلويات", 0.9), ("عصير", 0.7), ("بوفيه", 0.8),
    ("فندق", 0.9), ("شقق مفروشه", 0.9), ("استراحه", 0.85),
    # Considered-purchase retail.
    ("مفروشات", 1.0), ("اثاث", 1.0), ("مجوهرات", 1.0), ("ذهب", 1.0),
    ("زهور", 1.0), ("ورد", 1.0), ("هدايا", 1.0), ("عطور", 0.9),
    ("ملابس", 0.9), ("ازياء", 0.9), ("عبايات", 0.95), ("خياط", 0.9),
    ("سيارات", 0.9), ("ورشه", 0.9), ("قطع غيار", 0.85),
    ("الكترون", 0.85), ("جوال", 0.85), ("كمبيوتر", 0.85),
    ("نظارات", 0.85), ("بصريات", 0.85), ("معرض", 0.9),
    # Professional services.
    ("محام", 1.0), ("محاسب", 1.0), ("عقار", 1.0), ("تامين", 0.9),
    ("استشار", 0.9), ("تسويق", 0.8), ("دعايه", 0.8), ("مقاول", 0.85),
    ("تدريب", 0.9), ("معهد", 0.9), ("مدرسه", 0.8), ("حضانه", 0.9),
    # Generic retail catch-alls, tested last so specifics win.
    ("متجر", 0.85), ("محل", 0.85), ("مركز تسوق", 0.5), ("سوق", 0.5),
]

# A name appearing this many times in one corpus is a chain, whatever the brand
# fields say. Deriving it from the data catches local chains no lexicon lists.
CHAIN_BRANCH_THRESHOLD = 3

# Groups whose outlets carry the parent's site. Overture tags a brand on only
# 172 of 1,506 Olaya records, so franchise names that appear inside a longer
# title -- "Rosh Rayhaan by Rotana" -- need catching by substring.
CHAIN_MARKERS = {
    "rotana", "marriott", "hilton", "accor", "novotel", "ibis", "radisson",
    "movenpick", "fairmont", "kempinski", "sheraton", "holiday inn", "ritz",
    "courtyard", "four seasons", "intercontinental", "crowne plaza", "hyatt",
    "centro", "braira", "boudl", "swissotel", "voco", "millennium",
}

# Review count at which "clearly a going concern" saturates.
REVIEW_SATURATION = 150.0


@dataclass
class Lead:
    id: str
    name: str
    name_ar: str | None
    category: str | None
    phone: str | None
    lon: float
    lat: float
    address: str | None
    locality: str | None
    district: str | None
    website: str | None
    web_presence: str          # "none" | the platform name | "real site"
    rating: float | None
    review_count: int | None
    score: float
    reasons: list[str] = field(default_factory=list)


def classify_website(url: str | None) -> str:
    """Return "none", "real site", or the name of the platform standing in for one."""
    if not url or not url.strip():
        return "none"
    for pattern, label in NON_WEBSITE_PATTERNS:
        if pattern.search(url):
            return label
    return "real site"


def category_fit(category: str | None) -> float:
    """How much a website is worth to this trade, 0..1.

    Handles both vocabularies: Overture's English slugs by exact match, and
    Google's free-form Arabic by keyword. The Arabic pass runs against the
    normalized string so that ``عيادة`` and ``عياده`` -- and every other
    spelling users and Google alternate between -- reach the same rule.
    """
    if not category:
        return DEFAULT_CATEGORY_FIT
    if category in CATEGORY_FIT:
        return CATEGORY_FIT[category]

    folded = normalize(category)
    if folded:
        for keyword, fit in ARABIC_CATEGORY_FIT:
            if normalize(keyword) in folded:
                return fit
    return DEFAULT_CATEGORY_FIT


def chain_names(con: duckdb.DuckDBPyConnection, *, threshold: int = CHAIN_BRANCH_THRESHOLD) -> set[str]:
    """Normalized names that occur often enough to be a chain.

    Derived from the corpus rather than a list, so it catches local chains --
    a Riyadh bakery with five branches -- that no curated lexicon would carry.
    """
    counts = collections.Counter(
        normalize(row[0])
        for row in con.execute(
            "SELECT name_primary FROM places WHERE name_primary IS NOT NULL"
        ).fetchall()
    )
    return {name for name, n in counts.items() if n >= threshold and name}


def is_chain(name: str | None, brand_name: str | None, brand_wikidata: str | None, chains: set[str]) -> bool:
    """True if this looks like a branch of something with a head office."""
    # Overture sets a brand only for recognised brands, so its mere presence --
    # even when it equals the place's own name, as with "Hardee's" -- says a
    # head office exists. Requiring it to differ from the name let every
    # single-word chain through.
    if brand_wikidata or brand_name:
        return True
    key = normalize(name)
    if not key:
        return False
    if key in chains:
        return True
    if any(marker in key for marker in CHAIN_MARKERS):
        return True
    # Known global/regional chains from the category lexicon.
    return any(key == brand or key.startswith(brand + " ") for brand in BRAND_CATEGORIES)


def score_lead(
    *,
    web_presence: str,
    category: str | None,
    has_phone: bool,
    chain: bool,
    confidence: float | None,
    review_count: int | None = None,
    rating: float | None = None,
) -> tuple[float, list[str]]:
    """Score a prospect in 0..1 and explain the score.

    Returns 0 for anyone who cannot be contacted, already has a real site, or
    is a chain branch -- these are not weak leads, they are not leads.
    """
    reasons: list[str] = []
    if not has_phone:
        return 0.0, ["no phone -- unreachable"]
    if web_presence == "real site":
        return 0.0, ["already has a website"]
    if chain:
        return 0.0, ["chain branch -- head office owns the website"]

    fit = category_fit(category)
    if fit <= 0.0:
        return 0.0, [f"{category or 'unknown'} would not buy a website"]

    # A social-only presence beats no presence at all: they have already shown
    # they want to be found online.
    if web_presence == "none":
        opportunity = 0.75
        reasons.append("no web presence at all")
    else:
        opportunity = 1.0
        reasons.append(f"{web_presence} only -- wants a presence, has no site")

    reasons.append(f"{category or 'unknown'} (fit {fit:.2f})")

    # Is the business real? Reviews are the strong signal; Overture's confidence
    # is the weak fallback when SerpApi enrichment has not run.
    if review_count is not None:
        liveness = min(1.0, math.log1p(review_count) / math.log1p(REVIEW_SATURATION))
        reasons.append(f"{review_count} reviews" + (f", {rating}★" if rating else ""))
    else:
        liveness = 0.55 * (confidence or 0.0)
        reasons.append(f"confidence {confidence:.2f}" if confidence else "unverified")

    # A well-reviewed business with a good rating has both money and something
    # to protect -- the easiest pitch there is.
    quality_bonus = 0.0
    if rating is not None and review_count and review_count >= 20:
        quality_bonus = 0.1 * max(0.0, (rating - 3.5) / 1.5)
        if quality_bonus > 0:
            reasons.append(f"well rated ({rating}★)")

    score = min(1.0, 0.45 * opportunity * fit + 0.35 * liveness + 0.20 * fit + quality_bonus)
    return score, reasons


def find_leads(
    con: duckdb.DuckDBPyConnection,
    *,
    locality: str | None = None,
    categories: list[str] | None = None,
    bbox: "BBox | None" = None,
    min_score: float = 0.35,
    limit: int = 200,
) -> list[Lead]:
    """Rank the corpus as prospects, best first."""
    sql = """
        SELECT id, name_primary, name_ar, coalesce(category_final, category) AS category,
               phone, lon, lat, address_freeform, locality,
               district, website,
               brand_name, brand_wikidata, confidence, serpapi_rating, serpapi_reviews
        FROM places
        WHERE coalesce(duplicate_of, '') = ''
          AND name_primary IS NOT NULL
    """
    params: list = []
    if locality:
        sql += " AND locality ILIKE ?"
        params.append(f"%{locality}%")
    if categories:
        sql += f" AND coalesce(category_final, category) IN ({','.join('?' * len(categories))})"
        params += categories
    if bbox is not None:
        # One store can hold many districts, so a district run has to filter
        # geographically or it reports the whole city every time.
        sql += " AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
        params += [bbox.min_lon, bbox.max_lon, bbox.min_lat, bbox.max_lat]

    chains = chain_names(con)
    leads: list[Lead] = []
    for row in con.execute(sql, params).fetchall():
        (pid, name, name_ar, category, phone, lon, lat, address, loc, district, website,
         brand_name, brand_qid, confidence, rating, reviews) = row
        presence = classify_website(website)
        score, reasons = score_lead(
            web_presence=presence,
            category=category,
            has_phone=bool(phone),
            chain=is_chain(name, brand_name, brand_qid, chains),
            confidence=confidence,
            review_count=reviews,
            rating=rating,
        )
        # A zero is a disqualification, not a weak lead: unreachable, already
        # has a site, a chain branch, or a trade that never buys one. It must
        # never surface, whatever `min_score` is set to -- `0.0 < 0.0` is false,
        # so relying on the threshold alone lets every one of them through.
        if score <= 0.0 or score < min_score:
            continue
        leads.append(Lead(
            id=pid, name=name, name_ar=name_ar, category=category, phone=phone,
            lon=lon, lat=lat, address=address, locality=loc, district=district, website=website,
            web_presence=presence, rating=rating, review_count=reviews,
            score=round(score, 4), reasons=reasons,
        ))

    leads.sort(key=lambda l: l.score, reverse=True)
    return leads[:limit]
