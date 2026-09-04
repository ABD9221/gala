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
    ap.add_argument("--bbox", help="min_lon,min_lat,max_lon,max_lat (overrides preset)")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--skip-enrich", action="store_true", help="Overture only, no network enrichment")
    args = ap.parse_args()

    bbox = BBox(*map(float, args.bbox.split(","))) if args.bbox else PRESETS[args.preset]
    con = connect(args.db)

    t0 = time.time()
    print(f"[1/4] ingesting Overture for {bbox} ...", flush=True)
    count = ingest_bbox(con, bbox, parts=list_parts())
    print(f"      {count} places in {time.time() - t0:.1f}s")

    if not args.skip_enrich:
        t1 = time.time()
        print("[2/4] enriching from OSM + Wikidata ...", flush=True)
        report = enrich_bbox(con, bbox)
        print(f"      {time.time() - t1:.1f}s")
        for key, value in report.as_dict().items():
            print(f"        {key:20} {value}")

    print("[3/4] building search index ...", flush=True)
    print(f"      {build_index(con)}")

    print("[4/4] coverage:")
    for key, value in stats(con).items():
        print(f"        {key:16} {value if not isinstance(value, float) else round(value, 3)}")
    con.close()
    print(f"\ndone in {time.time() - t0:.1f}s -> {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
