#!/usr/bin/env python3
"""End-to-end corpus build: ingest -> enrich -> repair -> index.

    python scripts/build.py olaya
    python scripts/build.py --bbox 46.50,24.55,46.90,24.90
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Runnable straight from a checkout, without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gala.config import DB_PATH, PRESETS, BBox
from gala.enrich.pipeline import enrich_bbox
from gala.ingest.overture import ingest_bbox, list_parts
from gala.store import build_index, connect, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("preset", nargs="?", default="olaya", choices=sorted(PRESETS))
    ap.add_argument("--district", help="build one district by name (Arabic or English)")
    ap.add_argument("--all-districts", action="store_true",
                    help="ingest the whole city in one pass, then label every place by district")
    ap.add_argument("--city", default="jeddah", help="city the district belongs to")
    ap.add_argument("--bbox", help="min_lon,min_lat,max_lon,max_lat (overrides preset)")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--skip-enrich", action="store_true", help="Overture only, no network enrichment")
    args = ap.parse_args()

    district = None
    if args.all_districts:
        from gala.districts import city_bbox, load as load_districts
        catalog = load_districts(args.city)
        bbox = city_bbox(catalog)
        print(f"city: {args.city} -- {len(catalog)} districts, {bbox.max_lon - bbox.min_lon:.2f}"
              f"x{bbox.max_lat - bbox.min_lat:.2f} deg")
    elif args.district:
        from gala.districts import find
        district = find(args.district, args.city)
        if district is None:
            print(f"no district matching {args.district!r} in {args.city}.", file=sys.stderr)
            print(f"Try: python scripts/districts.py {args.city} --search {args.district}", file=sys.stderr)
            return 2
        bbox = district.bbox
        print(f"district: {district.label}  ({district.area_km2:.1f} km², bbox {district.bbox_source})")
    elif args.bbox:
        bbox = BBox(*map(float, args.bbox.split(",")))
    else:
        bbox = PRESETS[args.preset]
    con = connect(args.db)

    t0 = time.time()
    print(f"[1/5] ingesting Overture for {bbox} ...", flush=True)
    count = ingest_bbox(con, bbox, parts=list_parts())
    print(f"      {count} places in {time.time() - t0:.1f}s")

    if args.all_districts:
        from gala.districts import stamp
        print("      labelling places by district ...", flush=True)
        print(f"      {stamp(con, args.city)}")
    elif district is not None:
        # Stamp the district so the store accumulates across runs and each
        # district stays separately queryable -- the unit of work is one
        # district, not one database.
        con.execute(
            """UPDATE places SET district = ?
               WHERE district IS NULL
                 AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?""",
            [district.name, bbox.min_lon, bbox.max_lon, bbox.min_lat, bbox.max_lat],
        )

    if args.all_districts and not args.skip_enrich:
        print("[2/5] skipping OSM enrichment for a whole-city build "
              "(one Overpass query per district would be rate-limited; "
              "run per-district builds to add it)")
    elif not args.skip_enrich:
        t1 = time.time()
        print("[2/5] enriching from OSM + Wikidata ...", flush=True)
        report = enrich_bbox(con, bbox)
        print(f"      {time.time() - t1:.1f}s")
        for key, value in report.as_dict().items():
            if value not in (None, ""):
                print(f"        {key:20} {value}")
        if report.osm_error:
            print("        (OSM enrichment unavailable -- corpus and leads are "
                  "unaffected; re-run later to add Arabic names and hours)")

    # Category repair is network-free and belongs on every path. Burying it
    # inside the OSM enrichment meant a whole-city build shipped Krispy Kreme
    # and KFC still filed as `pharmacy`.
    print("[3/5] repairing categories ...", flush=True)
    from gala import quality
    report_q = quality.harmonize_categories(con)
    from_brand = quality.apply_brand_lexicon(con)
    dupes = quality.mark_duplicates(con)
    changed = con.execute(
        "SELECT count(*) FROM places WHERE category_final IS DISTINCT FROM category"
    ).fetchone()[0]
    print(f"      {changed} categories changed "
          f"(consensus {report_q.conflicted_clusters} clusters, brand lexicon {from_brand}), "
          f"{dupes} duplicates marked")

    print("[4/5] building search index ...", flush=True)
    print(f"      {build_index(con)}")

    print("[5/5] coverage:")
    for key, value in stats(con).items():
        print(f"        {key:16} {value if not isinstance(value, float) else round(value, 3)}")
    con.close()
    print(f"\ndone in {time.time() - t0:.1f}s -> {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
