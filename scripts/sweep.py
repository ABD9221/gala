#!/usr/bin/env python3
"""Sweep districts for businesses using Google (via SerpApi) as the source.

    python scripts/sweep.py --district "مريخ" --budget 20
    python scripts/sweep.py --districts "مريخ,الرحاب,النسيم" --budget 60
    python scripts/sweep.py --quota                       # check plan, costs nothing

Quota is the scarce resource, so every run takes a hard call budget and reports
what it actually spent. The key is read from SERPAPI_KEY or a .env file and is
never written to the store or to any export.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gala.districts import find, load
from gala.ingest.serpapi import SWEEP_TERMS, SerpApiError, account, sweep
from gala.store import build_index, connect

DEFAULT_DB = "data/jeddah_google.duckdb"


def api_key() -> str | None:
    if key := (os.getenv("SERPAPI_KEY") or os.getenv("SERPAPI_API_KEY")):
        return key
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("SERPAPI_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--district", help="one district by name")
    ap.add_argument("--districts", help="comma-separated district names")
    ap.add_argument("--all", action="store_true", help="every district in the city")
    ap.add_argument("--city", default="jeddah")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--budget", type=int, default=20, help="hard cap on API calls for this run")
    ap.add_argument("--pages", type=int, default=2, help="result pages per search term")
    ap.add_argument("--terms", help="comma-separated search terms (default: the built-in sweep)")
    ap.add_argument("--quota", action="store_true", help="report plan and remaining searches, then exit")
    args = ap.parse_args()

    key = api_key()
    if not key:
        print("no API key. Set SERPAPI_KEY or put it in .env", file=sys.stderr)
        return 2

    if args.quota:
        info = account(key)
        print(f"plan:      {info.get('plan_name')}")
        print(f"per month: {info.get('searches_per_month')}")
        print(f"used:      {info.get('this_month_usage')}")
        print(f"left:      {info.get('total_searches_left')}")
        return 0

    catalog = load(args.city)
    if args.all:
        targets = catalog
    elif args.districts:
        targets = []
        for name in args.districts.split(","):
            d = find(name.strip(), args.city)
            if d is None:
                print(f"no district matching {name.strip()!r}", file=sys.stderr)
                return 2
            targets.append(d)
    elif args.district:
        d = find(args.district, args.city)
        if d is None:
            print(f"no district matching {args.district!r}", file=sys.stderr)
            return 2
        targets = [d]
    else:
        ap.error("pass --district, --districts or --all")

    terms = [t.strip() for t in args.terms.split(",")] if args.terms else SWEEP_TERMS

    before = account(key).get("total_searches_left")
    print(f"quota before: {before}   budget for this run: {args.budget}")
    print(f"districts: {len(targets)}   terms: {len(terms)}   pages/term: {args.pages}\n")

    # Split the budget evenly. An early district must not eat the whole plan
    # and leave the rest unswept.
    per_district = max(2, args.budget // len(targets))
    con = connect(args.db)
    spent = 0

    try:
        for d in targets:
            if spent >= args.budget:
                print("budget exhausted")
                break
            allowance = min(per_district, args.budget - spent)
            try:
                report = sweep(con, d, key, terms=terms,
                               max_calls=allowance, pages_per_term=args.pages)
            except SerpApiError as exc:
                print(f"{d.name}: {exc}", file=sys.stderr)
                break
            spent += report.calls
            new = report.inserted
            print(f"{d.name[:22]:24} calls={report.calls:3}  new={new:4}  "
                  f"updated={report.updated:3}  no-website={report.without_website:4}  "
                  f"rated={report.with_rating:4}")
            if report.error:
                print(f"    stopped: {report.error}")
                break

        print("\nbuilding search index ...")
        build_index(con)

        rows = con.execute(
            """SELECT count(*), count(*) FILTER (WHERE website IS NULL),
                      count(*) FILTER (WHERE phone IS NOT NULL),
                      count(*) FILTER (WHERE serpapi_rating IS NOT NULL)
               FROM places WHERE source = 'serpapi'"""
        ).fetchone()
        print(f"\nstore now holds {rows[0]} Google places "
              f"({rows[1]} without a website, {rows[2]} with a phone, {rows[3]} rated)")
    finally:
        con.close()

    after = account(key).get("total_searches_left")
    print(f"quota after: {after}   (this run used {spent} calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
