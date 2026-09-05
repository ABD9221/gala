#!/usr/bin/env python3
"""Find businesses to pitch website development to.

    python scripts/leads.py olaya                          # rank and print
    python scripts/leads.py olaya --csv leads.csv          # export
    python scripts/leads.py olaya --category restaurant cafe
    python scripts/leads.py olaya --enrich                 # add Google ratings first
                                                           # (needs SERPAPI_KEY)

Output is ordered by how worth calling each business is, not by how well known
it is -- see gala/leads.py for why those are opposites here.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gala.config import DB_PATH, PRESETS, BBox
from gala.leads import find_leads
from gala.store import connect

CSV_COLUMNS = [
    "score", "district", "name", "name_ar", "category", "phone", "web_presence",
    "rating", "reviews", "address", "locality", "maps_url", "why",
]


def maps_url(lat: float, lon: float) -> str:
    """A link to stand on the spot in Google Maps -- for eyeballing before calling."""
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # No default preset. Defaulting to a Riyadh district silently filtered a
    # Jeddah store down to nothing and reported "no leads matched", which reads
    # as an empty corpus rather than a wrong filter. With no area given, the
    # whole store is reported.
    ap.add_argument("preset", nargs="?", choices=sorted(PRESETS),
                    help="a built-in area; omit to report the whole store")
    ap.add_argument("--district", help="one district by name (Arabic or English)")
    ap.add_argument("--city", default="jeddah", help="city the district belongs to")
    ap.add_argument("--bbox", help="min_lon,min_lat,max_lon,max_lat (overrides preset)")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--category", nargs="*", help="restrict to these categories")
    ap.add_argument("--locality", help="restrict to a district name")
    ap.add_argument("--min-score", type=float, default=0.35)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--csv", help="write results to this CSV file")
    ap.add_argument("--enrich", action="store_true",
                    help="fetch Google ratings via SerpApi first (needs SERPAPI_KEY)")
    ap.add_argument("--max-calls", type=int, default=12, help="cap SerpApi calls")
    args = ap.parse_args()

    district = None
    if args.district:
        from gala.districts import find
        district = find(args.district, args.city)
        if district is None:
            print(f"no district matching {args.district!r} in {args.city}.", file=sys.stderr)
            print(f"Try: python scripts/districts.py {args.city} --search {args.district}", file=sys.stderr)
            return 2
        bbox = district.bbox
        print(f"district: {district.label}  ({district.area_km2:.1f} km²)\n")
    elif args.bbox:
        bbox = BBox(*map(float, args.bbox.split(",")))
    elif args.preset:
        bbox = PRESETS[args.preset]
    else:
        bbox = None  # whole store
    con = connect(args.db)

    if args.enrich:
        from gala.enrich.serpapi import SerpApiError, enrich_leads
        if not (os.getenv("SERPAPI_KEY") or os.getenv("SERPAPI_API_KEY")):
            print("warning: no SERPAPI_KEY set -- trying the free allowance", file=sys.stderr)
        try:
            stats = enrich_leads(con, bbox, max_calls=args.max_calls)
            print(f"enrichment: {stats}\n")
        except SerpApiError as exc:
            # Ratings only improve the ranking; losing them must not lose the run.
            print(f"enrichment skipped: {exc}\n", file=sys.stderr)

    leads = find_leads(
        con, locality=args.locality, categories=args.category,
        bbox=bbox, min_score=args.min_score, limit=args.limit,
    )

    if not leads:
        print("no leads matched. Try --min-score 0.2 or a wider area.")
        return 1

    print(f"{len(leads)} prospects, best first\n")
    print(f"{'#':>3}  {'score':>5}  {'name':32}  {'category':22}  {'phone':16}  presence")
    print("-" * 110)
    for i, lead in enumerate(leads[:40], 1):
        rating = f" {lead.rating}★×{lead.review_count}" if lead.rating else ""
        print(f"{i:3}  {lead.score:5.3f}  {str(lead.name)[:32]:32}  "
              f"{str(lead.category)[:22]:22}  {str(lead.phone):16}  {lead.web_presence}{rating}")

    if any(l.district for l in leads):
        print("\nby district (work them one at a time):")
        for name, n in Counter(l.district for l in leads if l.district).most_common(12):
            print(f"   {n:4}  {name}")

    print("\nby trade:")
    for category, n in Counter(l.category for l in leads).most_common(10):
        print(f"   {n:4}  {category}")

    print("\nby web presence:")
    for presence, n in Counter(l.web_presence for l in leads).most_common():
        print(f"   {n:4}  {presence}")

    # Grouping by street makes a day of door-knocking or calling routable.
    streets: dict[str, int] = defaultdict(int)
    for lead in leads:
        if lead.address:
            streets[lead.address.split(",")[-1].strip()[:40]] += 1
    print("\nbusiest streets (plan a route):")
    for street, n in sorted(streets.items(), key=lambda kv: -kv[1])[:8]:
        print(f"   {n:4}  {street}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for lead in leads:
                writer.writerow({
                    "score": lead.score, "district": lead.district or "",
                    "name": lead.name, "name_ar": lead.name_ar or "",
                    "category": lead.category or "", "phone": lead.phone or "",
                    "web_presence": lead.web_presence,
                    "rating": lead.rating or "", "reviews": lead.review_count or "",
                    "address": lead.address or "", "locality": lead.locality or "",
                    "maps_url": maps_url(lead.lat, lead.lon),
                    "why": "; ".join(lead.reasons),
                })
        # utf-8-sig: Excel misreads plain UTF-8 Arabic without the BOM.
        print(f"\nwrote {len(leads)} rows -> {args.csv}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
