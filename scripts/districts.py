#!/usr/bin/env python3
"""List, search and refresh the district catalog.

    python scripts/districts.py jeddah                  # list all districts
    python scripts/districts.py jeddah --search نزهة    # find one
    python scripts/districts.py jeddah --status         # what is built / worked
    python scripts/districts.py jeddah --refresh        # re-fetch from OSM
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gala import districts
from gala.config import DB_PATH
from gala.store import connect


def district_status(db: str) -> dict[str, tuple[int, int]]:
    """places and leads already in the store, per district name."""
    try:
        con = connect(db, read_only=True)
    except Exception:
        return {}
    try:
        rows = con.execute(
            """SELECT district, count(*),
                      count(*) FILTER (WHERE phone IS NOT NULL AND website IS NULL)
               FROM places WHERE district IS NOT NULL GROUP BY district"""
        ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}
    except Exception:
        return {}  # column not present yet -- nothing built
    finally:
        con.close()


def rank_districts(city: str, db: str, min_score: float, limit: int) -> int:
    """Order districts by prospect count -- the answer to "where do I start?"."""
    from collections import Counter

    from gala.leads import find_leads

    con = connect(db, read_only=True)
    try:
        leads = find_leads(con, min_score=min_score, limit=1_000_000)
        by_district: Counter[str] = Counter()
        totals: Counter[str] = Counter()
        for lead in leads:
            if lead.district:
                by_district[lead.district] += 1
        for name, n in con.execute(
            "SELECT district, count(*) FROM places WHERE district IS NOT NULL GROUP BY district"
        ).fetchall():
            totals[name] = n
    finally:
        con.close()

    if not by_district:
        print("no leads found. Build the corpus first:\n"
              f"  python scripts/build.py --city {city} --all-districts --db {db}")
        return 1

    print(f"{len(by_district)} districts with prospects (score >= {min_score}), best first\n")
    print(f"{'district':30} {'prospects':>9} {'places':>7} {'rate':>6}")
    print("-" * 56)
    for name, n in by_district.most_common(limit):
        total = totals.get(name, 0)
        rate = f"{100 * n / total:.0f}%" if total else "-"
        print(f"{name[:30]:30} {n:9} {total:7} {rate:>6}")
    print(f"\ntotal prospects: {sum(by_district.values())}")
    print(f"\nstart with:  python scripts/leads.py --district \"{by_district.most_common(1)[0][0]}\" "
          f"--city {city} --db {db} --csv leads.csv")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("city", nargs="?", default="jeddah")
    ap.add_argument("--search", help="find districts matching a name (Arabic or English)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch the catalog from OSM")
    ap.add_argument("--status", action="store_true", help="show which districts are built")
    ap.add_argument("--rank", action="store_true",
                    help="rank districts by how many prospects they hold -- where to start")
    ap.add_argument("--min-score", type=float, default=0.5,
                    help="lead score floor when ranking")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    if args.refresh:
        print(f"fetching {args.city} districts from OpenStreetMap ...", flush=True)
        fetched = districts.fetch(args.city)
        path = districts.save(fetched, args.city)
        derived = sum(1 for d in fetched if d.bbox_source == "derived")
        print(f"saved {len(fetched)} districts -> {path}")
        print(f"  {len(fetched) - derived} with real OSM boundaries, {derived} with derived extents")
        return 0

    if args.rank:
        return rank_districts(args.city, args.db, args.min_score, args.limit)

    found = districts.search(args.search, args.city, limit=args.limit) if args.search \
        else districts.load(args.city)
    if not found:
        print(f"no districts matched {args.search!r} in {args.city}")
        return 1

    status = district_status(args.db) if args.status else {}
    header = f"{'district':30} {'english':26} {'km²':>6}"
    if args.status:
        header += f"  {'places':>7} {'leads':>6}"
    print(f"{len(found)} districts in {args.city}\n")
    print(header)
    print("-" * (len(header) + 2))
    for d in found[: args.limit]:
        line = f"{d.name[:30]:30} {str(d.name_en or '-')[:26]:26} {d.area_km2:6.1f}"
        if args.status:
            places, leads = status.get(d.name, (0, 0))
            line += f"  {places or '-':>7} {leads or '-':>6}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
