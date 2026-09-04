# gala — find local businesses that need a website

A prospecting tool for web developers, built on an open-data places corpus:
**Overture Maps** for the businesses, **OpenStreetMap** for Arabic names and
opening hours, **Wikidata** for photos, optional **SerpApi** for Google ratings.
No API key needed for the core, no per-query billing.

It answers one question: *which businesses here are real, reachable, and have no
website?* Across Jeddah that is **2,346 prospects out of 21,899 places**, split
across 113 districts and ranked so the ones worth calling come first.

Prospecting happens **district by district** — calling a whole city is not a
plan, calling one district is.

```bash
pip install -r requirements.txt

# 1. Build the city once (~5 min, 21,899 places labelled into 149 districts)
python scripts/build.py --city jeddah --all-districts --db data/jeddah.duckdb

# 2. Ask where to start
python scripts/districts.py jeddah --rank --db data/jeddah.duckdb

# 3. Work one district
python scripts/leads.py --district "الروضة" --city jeddah \
    --db data/jeddah.duckdb --csv rawdah.csv
```

```
district                       prospects  places   rate
الروضة                               206    1678    12%
البلد                                135    1040    13%
الصفاء                               101     740    14%
الخالدية                              99     848    12%
السلامة                               96     709    14%
```

Inside a district:

```
  #  score  name                          category       phone            presence
  1  0.837  Dania's Salon                 beauty_salon   +966506671072    google link
  3  0.824  مطعم أفران حلب                 restaurant     +966581713191    instagram
  4  0.821  عيادات نجم الأفق لطب الأسنان    dentist        +966126693880    whatsapp
 10  0.788  Pioneer Coffee                coffee_shop    +966555514856    google business stub
```

## Districts

The unit of work comes from OpenStreetMap, and its shape differs sharply by
city. Riyadh's districts are mostly polygons with real boundaries; **Jeddah's
are almost entirely single points — 171 of 176 carry no extent at all.** A
catalog that only accepted polygons would cover 3% of Jeddah.

So a point district gets a box derived from how far its nearest neighbour is:
half that distance, clamped to 0.7–2.5 km. Districts packed tightly downtown get
small boxes, isolated ones on the edge get large ones — much closer to reality
than one fixed radius, and labelled `derived` rather than passed off as a real
boundary.

One detail that is easy to get wrong: a square in metres is not a square in
degrees. Longitude shrinks with the cosine of latitude, which at Jeddah is a 7%
difference — enough to under-cover every district east and west if ignored.

Building the whole city in one Overture pass and labelling afterwards costs one
scan instead of 176.

Underneath it is a full places search stack — Arabic-aware search, autocomplete
and a Google-Places-shaped API — because ranking prospects needs the same
corpus, conflation and category repair that search does.

Every number below was measured on a real build of Olaya
(bbox `46.665,24.700,46.690,24.725`), not estimated.

## Ranking prospects is the opposite of ranking search results

The corpus already scores places by `prominence` — how well attested and well
known they are. Sorting "has a phone, has no website" by that puts **Kingdom
Centre, Dunkin' Donuts, Hardee's, Audi and Sephora** on top. All of them have
corporate web teams; their website is missing from *the data*, not from the
world. Prominence is precisely the wrong signal here.

`gala/leads.py` scores the opposite qualities:

| Signal | Why |
|---|---|
| Has a phone | Otherwise there is nothing to act on — hard requirement |
| No **real** website | An Instagram page is not a website |
| Not a chain | A branch cannot buy a website; head office already did |
| Trade that buys websites | A salon does, an ATM does not |
| Alive | Review counts, or Overture confidence as a weak fallback |

**A social-only presence outranks no presence at all.** A business on Instagram
or Linktree has already shown it wants to be findable and settled for someone
else's platform. `business.site` — Google's free one-page stub — is close to a
signed confession.

Two bugs worth recording, both found by testing rather than reading:

- `(^|\.)linktr\.ee|linkin\.bio` binds the anchor to the *first* alternative
  only, so `https://linktr.ee/x` (slash before the domain, no dot) classified
  as a real website — the exact case the list exists to catch.
- `normalize("Hardee's")` returned `"hardee s"`, matching neither the brand
  lexicon's `"hardees"` nor a user typing it, so a chain read as an
  independent business. Apostrophes are now deleted, not spaced.

## Qualifying leads with SerpApi (optional)

The corpus knows a business exists and has no website. It cannot tell a thriving
restaurant from a dead one with the sign still up — and that decides whether a
lead is worth a call. `gala/enrich/serpapi.py` fills that in:

```bash
export SERPAPI_KEY=...
python scripts/leads.py olaya --enrich --csv leads.csv
```

One request returns ~20 places with ratings, so a district costs 12 calls, not
one per business — comfortably inside the 250/month free tier.

**These fields are rented, not owned.** Google's terms permit caching place IDs
indefinitely but not ratings, hours or prices. They are stored in separate
`serpapi_*` columns with a `serpapi_fetched_at` stamp, kept out of the search
index and out of every export, and `purge_stale()` clears them after 30 days.
The open corpus stays the part you own; this is a live overlay on top of it.

## The places API underneath

```bash
uvicorn gala.api:app --port 8000
curl -G localhost:8000/v1/places:searchText \
     --data-urlencode 'textQuery=قهوة' -d 'latitude=24.7114&longitude=46.6744'
```

## What actually differs from Google Places

Google Places is not one product; it is six data fields plus a ranking. Taking
them one at a time makes the real gap much smaller than "you can't match
Google" suggests.

| Field | Google | gala | Measured on Olaya |
|---|---|---|---|
| Place exists, located, categorised | ✅ | ✅ | 1,506 places in a 2.5 km box |
| Address | ✅ | ✅ | 83% (1,251) |
| Phone | ✅ | ✅ | 70% (1,051) |
| Website | ✅ | ✅ | 61% (923) |
| Ranking quality | ✅ | ✅ | modelled, see below |
| Arabic/English name pair | partial | ✅ | from OSM |
| Opening hours | ✅ | partial | OSM coverage is thin |
| Photos | ✅ | partial | notable places only |
| **Star rating + review text** | ✅ | ❌ | **no open source exists** |

One field is genuinely unavailable, and it is worth being precise about why the
others are not.

### Ratings: separating the two jobs a star rating does

A rating is used for two unrelated things, and conflating them is what makes
the gap look unbridgeable:

1. **Display** — "4.6 ★ (1,203 reviews)" on the card.
2. **Ranking** — deciding which of eleven matching coffee shops goes first.

Only (1) needs proprietary review data. (2) is a relevance problem, solvable
from open signals that correlate with what a rating is being used as a proxy
for — *is this a real, established, well-attested business?*
[`gala/rank.py`](gala/rank.py) combines Overture's own confidence, how many
independent datasets describe the place, contact completeness, OSM's importance
measure, Wikipedia sitelink count, and brand presence.

For display, `rating` is in the API response and is **always `null`** until a
licensed provider is configured behind the `RatingsProvider` seam. Synthesising
a number from prominence would present a relevance score as user sentiment. An
honest gap beats a fabricated number.

## Architecture

```
Overture Maps (S3 Parquet, ~73M POI)     free, bulk, permissive licence
    │  bbox pruning via Parquet footers → 21 s for a district
    ▼
DuckDB  ── httpfs · spatial · custom inverted index
    │
    ├── OpenStreetMap (Overpass)   name:ar · name:en · opening_hours · wikidata
    ├── Wikidata / Commons         photos + licence attribution · notability
    └── gala.quality               category repair · de-duplication
    ▼
BM25 + whole-name match + distance decay + prominence
    ▼
FastAPI, Google-Places-shaped responses
```

## The five problems worth writing up

Each of these was found by measuring, not by reading documentation.

### 1. Arabic destroys a naive index

The same shop is written `مقهى` / `مقهي`, `القهوة` / `القهوه`, with or without
diacritics, with tatweel padding, with Arabic-Indic digits. A byte-level index
treats each as a different token. [`gala/normalize.py`](gala/normalize.py)
folds alef and hamza forms, teh marbuta, alef maqsura, tatweel, diacritics and
both digit sets, then applies a cross-script synonym table so **`قهوة` and
`coffee` return the same ranked list** — verified in
`tests/test_search.py::test_arabic_and_english_queries_agree`.

### 2. Overture and OSM name the same place in different alphabets

In this sample Overture is 62% Latin and OSM is 91% Arabic — nearly inverted.
Token similarity across scripts is 0 by construction, so `Coffee Hill` never
meets `كوفي هيل` however close the coordinates. `normalize.romanize()` maps
Arabic to a Latin consonant skeleton, lifting that pair to 86% similarity.

### 3. Conflation is where a plausible mistake does real damage

Attaching one shop's opening hours to its neighbour is worse than having no
hours. Three safeguards, each added after seeing it fail:

- **Distance *and* name must both agree.** Distance alone matches the wrong
  unit in a mall; name alone matches another branch across town.
- **Only POI-tagged OSM objects are candidates.** 843 of 987 named objects in
  the box are roads, buildings and districts. Left in, a road named
  `طريق التخصصي الفرعي` confidently matched a shop called *Al-Thabit Doors -
  Takhassosi Branch*.
- **Cross-script pairs clear a higher bar** (84 vs 72). Romanisation is lossy,
  so it carries less evidence per point. Without this, *Roberto Coin Kingdom
  Centre* bound to the mall it sits inside.

Adding the POI filter cut matches from 83 to 53 — that is precision improving,
not recall breaking. Parseable opening hours went *up*, from 4 to 5.

### 4. Overture's categories are wrong often enough to break search

Overture files Krispy Kreme, KFC, Hardee's and a Bank Al Jazira ATM as
`pharmacy`. "Pharmacies near me" returned doughnuts. 15 of the 37 multi-record
name clusters carry mutually contradictory categories.
[`gala/quality.py`](gala/quality.py) reassigns categories from three sources,
in increasing authority. The two columns differ because a source that *agrees*
with Overture confirms rather than corrects, and only the second column is a
claim about fixing anything:

| Source | Records touched | Category actually changed | How |
|---|---|---|---|
| `cluster_consensus` | 17 | 8 | confidence-weighted majority across a brand's records |
| `osm` | 35 | 15 | the conflated OSM object's own feature tag |
| `brand_lexicon` | 100 | 62 | curated chain → category map |
| **total** | **152** | **85** (5.6% of corpus) | |

The lexicon exists because consensus provably cannot reach the case where
*every* record of a brand is mislabelled identically — all three `صب واي`
(Subway) rows are `pharmacy` / `educational_services`. Corrections are written
to `category_final` and tagged in `category_source`; the original `category` is
never overwritten, so any rule stays auditable and revertible.

### 5. BM25 alone cannot tell a mall from its tenants

Every shop inside Kingdom Centre carries "Kingdom Centre" in its address, and
the shop's shorter document scores *better* under length normalisation. Two
fixes: field-weighted indexing (name and brand ×3, category ×2, address ×1),
and a separate whole-string name-match signal using `ratio` rather than
`token_set_ratio`, so extra tokens in a name cost something. The mall now ranks
first for both `kingdom centre` and `مركز المملكة`.

## Opening hours

OSM stores hours as a small DSL; [`gala/hours.py`](gala/hours.py) parses the
practical subset — **13 of the 14 distinct real values** found in the sample.
Two details matter here specifically:

- **Overnight spans.** `18:00-02:00` is normal for Gulf cafes; a naive
  `start <= now <= end` reports them closed all evening.
- **The weekend is not Sa–Su.** Saudi weeks run Sunday–Thursday, so OSM values
  like `Sa-Th` wrap the end of the day array and need modulo resolution.

Anything outside the subset returns `None` — *unknown*, never *closed*. A wrong
"closed" sends someone across town to a locked door.

## API

| Google Places | gala |
|---|---|
| `places:searchText` | `GET /v1/places:searchText` |
| `places:searchNearby` | `GET /v1/places:searchNearby` |
| `places:autocomplete` | `GET /v1/places:autocomplete` |
| `places/{id}` | `GET /v1/places/{id}` |

Field names mirror Google's (`displayName`, `formattedAddress`,
`currentOpeningHours`) so a client port is mechanical. Additions:
`prominence`, `relevanceScore`, `categorySource`, `categoryConfidence` — with a
corpus assembled from sources of differing trust, saying where a value came
from is more useful than hiding it.

Autocomplete is why this does not use DuckDB's built-in FTS extension: that
only matches whole stemmed tokens, so `مطع` returns nothing. The inverted index
in [`gala/store.py`](gala/store.py) is ~40 lines, prefix-matches, keeps the
BM25 maths visible, and ports to Postgres unchanged.

## The licensing advantage

Google's terms forbid storing most Places fields beyond 30 days and forbid use
outside Google Maps. Overture is open-licensed, OSM is ODbL, Wikidata is CC0.
You own the corpus permanently, can index and analyse it in bulk, and can serve
it from your own infrastructure. On cost, control and licence this is not a
compromise against Google — it is strictly better. It loses on ratings,
reviews and photo breadth.

## Honest limits

- **Opening-hours coverage is thin.** 5 places in this sample. OSM simply does
  not have them for most Saudi retail; this is a data gap, not a code gap.
- **Photos cover notable places only.** Wikidata has images for landmarks, not
  for the average shop.
- **Conflation reached 53 of 156 candidate POIs.** The remainder are places
  Overture knows and OSM does not, or vice versa.
- **The brand lexicon is curated and Saudi-focused.** It needs extending per
  market.
- **No ratings or reviews.** See above — this one does not have an open fix.

## Layout

```
gala/
  config.py            bounding boxes, endpoints, release pinning
  normalize.py         Arabic folding, synonyms, romanisation
  hours.py             OSM opening_hours parser
  rank.py              prominence model + RatingsProvider seam
  leads.py             prospect scoring: real-website detection, chain filter
  districts.py         district catalog, derived extents, place-to-district
  quality.py           category repair, brand lexicon, de-duplication
  search.py            BM25 + name match + distance + prominence
  store.py             DuckDB schema and inverted index
  api.py               FastAPI service
  http.py              shared HTTP session
  ingest/overture.py   Parquet footer pruning, bbox ingest
  enrich/overpass.py   bulk OSM fetch with mirror failover
  enrich/osm.py        Nominatim client (single-place lookups)
  enrich/wikidata.py   photos, attribution, notability
  enrich/serpapi.py    Google ratings overlay (optional, TTL-bound)
  enrich/pipeline.py   conflation and orchestration
scripts/build.py       end-to-end corpus build
scripts/leads.py       rank prospects, group by street, export CSV
scripts/districts.py   list, search, rank and refresh districts
gala/data/             shipped district catalogs (no network needed to use)
tests/                 110 tests, no network required
```

## Data sources and attribution

- **Overture Maps** — CDLA-Permissive 2.0 / ODbL depending on the source record
- **OpenStreetMap** — © OpenStreetMap contributors, ODbL 1.0
- **Wikidata** — CC0; **Wikimedia Commons** images carry per-file licences,
  surfaced as `photo.attribution` (fetched, not omitted — Commons content is
  free to reuse but almost always requires credit)
