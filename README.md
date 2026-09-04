# gala — an open-data places API

A working Google Places replacement built from open data: **Overture Maps** for
the corpus, **OpenStreetMap** for opening hours and Arabic names,
**Wikidata/Wikimedia Commons** for photos. No API key, no per-call billing, no
restriction on storing what you fetch.

Every number below was measured on a real build of the Olaya district in
Riyadh (bbox `46.665,24.700,46.690,24.725`), not estimated.

```bash
pip install -r requirements.txt
python scripts/build.py olaya          # ingest -> enrich -> repair -> index
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
  quality.py           category repair, brand lexicon, de-duplication
  search.py            BM25 + name match + distance + prominence
  store.py             DuckDB schema and inverted index
  api.py               FastAPI service
  http.py              shared HTTP session
  ingest/overture.py   Parquet footer pruning, bbox ingest
  enrich/overpass.py   bulk OSM fetch with mirror failover
  enrich/osm.py        Nominatim client (single-place lookups)
  enrich/wikidata.py   photos, attribution, notability
  enrich/pipeline.py   conflation and orchestration
scripts/build.py       end-to-end build
tests/                 74 tests, no network required
```

## Data sources and attribution

- **Overture Maps** — CDLA-Permissive 2.0 / ODbL depending on the source record
- **OpenStreetMap** — © OpenStreetMap contributors, ODbL 1.0
- **Wikidata** — CC0; **Wikimedia Commons** images carry per-file licences,
  surfaced as `photo.attribution` (fetched, not omitted — Commons content is
  free to reuse but almost always requires credit)
