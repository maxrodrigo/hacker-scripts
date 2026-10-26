#!/usr/bin/env python3
"""
hn_top_articles.py
==================
Fetch the top 50 Hacker News stories per month, year by year since 2007,
verify whether each article URL is still reachable, and — for URLs that are not
confirmed alive — look up the closest Wayback Machine snapshot so the content
can still be read.

Data source
-----------
Algolia HN Search API (https://hn.algolia.com/api/v1/search).
This is the only publicly available API that indexes the full HN archive
with date-range filtering and point-based sorting. The official HN Firebase
API does not support historical queries.

Output
------
One JSON file per year written to <output-dir>/<year>.json.
Each file is a dict keyed by "YYYY-MM"; the value is a list of up to 50
story objects sorted by points (descending).

Story object fields:
    rank         int   — position within the month (1-based)
    objectID     str   — Algolia/HN item ID
    title        str   — story title
    url          str|null — external URL (null for Ask HN / Show HN without link)
    points       int   — upvote score at time of indexing
    author       str   — submitter username
    created_at   str   — ISO 8601 submission timestamp
    num_comments int   — comment count at time of indexing
    hn_link      str   — canonical HN thread URL
    liveness     obj   — present only when --no-check-urls is NOT set:
        alive    bool|null — True:  server answered below 400.
                             False: gone — HTTP 404/410 or the domain no
                                    longer resolves.
                             null:  unknown — blocked (401/403), rate-limited,
                                    server error, timeout, TLS error; or no URL.
        status   int|null  — HTTP status code, or null on connection error
        error    str|null  — error message on failure, null on success
    wayback_url  str|null  — closest Wayback Machine snapshot URL for articles
                             not confirmed alive; null if none exists or alive

Resume behaviour
----------------
If <year>.json already exists, months already present in the file are skipped.
Only finished months are fetched, so a stored month is complete.
Progress is written after every month, so the script can be safely interrupted
and re-run without losing work.

Errors
------
A failed Algolia or Wayback request stops the run with a traceback. Months
saved before the failure are kept; re-run to resume.

Rate limiting
-------------
A 0.5-second delay is inserted between Algolia requests and a 4-second delay
before each Wayback request (~15 req/min; Wayback publishes no official limit
and blocks bursts for minutes at a time). On HTTP 429 the script backs off
(60 s, then 120 s) and gives up on the third 429. Network errors are retried
the same number of times. Run one process at a time: parallel runs trip the
Wayback limit.

Requirements
------------
Python 3.10+. No third-party packages — stdlib only (urllib, json, argparse).

Usage
-----
    # All years (2007 → current), with URL liveness checks
    python hn_top_articles.py

    # Single year, skip URL checks
    python hn_top_articles.py --year 2012 --no-check-urls

    # Custom output directory
    python hn_top_articles.py --output-dir ~/data/hn

Options
-------
    --year YEAR         Process only this year instead of all years since 2007.
    --output-dir DIR    Directory to write JSON files.
                        Default: data/hackernews/ at the repo root.
    --no-check-urls     Skip URL liveness checks (significantly faster).
                        Wayback lookups are also skipped when this flag is set.
"""

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone

import urllib.request
import urllib.error
import urllib.parse

ALGOLIA_BASE = "https://hn.algolia.com/api/v1/search"
WAYBACK_API  = "https://archive.org/wayback/available"
TOP_N = 50
START_YEAR = 2007
OUTPUT_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "hackernews")
)
REQUEST_DELAY = 0.5   # seconds between Algolia requests (be a good citizen)
WAYBACK_DELAY = 4.0   # seconds before each Wayback request: stays under the ~15 req/min community-reported limit
URL_TIMEOUT = 10      # seconds for the liveness request


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only, no third-party deps)
# ---------------------------------------------------------------------------

def _get_json(url: str, retries: int = 3) -> dict:
    """GET a URL and return parsed JSON. Retries 429s and network errors, then raises."""
    req = urllib.request.Request(url, headers={"User-Agent": "hn-archiver/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == retries - 1:
                raise
            wait = 60 * 2 ** attempt
            print(f"  [rate-limit] sleeping {wait}s ...", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
        time.sleep(wait)
    raise ValueError(f"retries must be >= 1, got {retries}")


def check_url(url: str) -> dict:
    """
    Return a dict with:
        alive  (bool|None) — True below 400, False if gone, None if unknown
        status (int|None)
        error  (str|None)

    Only 404/410 and an unresolvable domain count as gone: blocks, rate limits,
    server errors and timeouts say nothing about whether the article still exists.
    """
    if not url:
        return {"alive": None, "status": None, "error": "no url"}
    try:
        # GET rather than HEAD: many servers reject or block HEAD. The body is never read.
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; hn-archiver/1.0; "
                    "+https://maxrodrigo.com)"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=URL_TIMEOUT) as resp:
            return {"alive": True, "status": resp.status, "error": None}
    except urllib.error.HTTPError as e:
        alive = True if e.code < 400 else False if e.code in (404, 410) else None
        return {"alive": alive, "status": e.code, "error": None}
    except urllib.error.URLError as e:
        # EAI_NONAME: the domain does not exist. Other DNS errors (e.g. EAI_AGAIN when offline) are unknown.
        gone = isinstance(e.reason, socket.gaierror) and e.reason.errno == socket.EAI_NONAME
        return {"alive": False if gone else None, "status": None, "error": str(e.reason)}
    except Exception as e:
        return {"alive": None, "status": None, "error": str(e)}


def wayback_snapshot(url: str) -> str | None:
    """
    Query the Wayback Machine Availability API for the closest snapshot of url.
    Returns the snapshot URL string, or None if no snapshot exists.
    Request failures raise.

    API docs: https://archive.org/help/wayback_api.php
    """
    if not url:
        return None
    params = urllib.parse.urlencode({"url": url})
    time.sleep(WAYBACK_DELAY)
    data = _get_json(f"{WAYBACK_API}?{params}")
    snapshot = data.get("archived_snapshots", {}).get("closest", {})
    if snapshot.get("available") and snapshot.get("url"):
        return snapshot["url"]
    return None


# ---------------------------------------------------------------------------
# Algolia fetching
# ---------------------------------------------------------------------------

def fetch_top_for_month(year: int, month: int, n: int = TOP_N) -> list[dict]:
    """
    Return the top-N HN stories (by points) for the given year/month.
    Makes paginated requests until we have at least n results or exhaust pages.
    """
    t_start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    t_end = int(datetime(next_year, next_month, 1, tzinfo=timezone.utc).timestamp())

    collected: list[dict] = []
    page = 0

    while len(collected) < n:
        params = urllib.parse.urlencode(
            {
                "tags": "story",
                "numericFilters": f"created_at_i>={t_start},created_at_i<{t_end}",
                "hitsPerPage": min(n - len(collected), 50),
                "page": page,
            }
        )
        url = f"{ALGOLIA_BASE}?{params}"
        data = _get_json(url)

        hits = data.get("hits", [])
        if not hits:
            break

        for hit in hits:
            collected.append(
                {
                    "rank": len(collected) + 1,
                    "objectID": hit.get("objectID"),
                    "title": hit.get("title"),
                    "url": hit.get("url"),
                    "points": hit.get("points", 0),
                    "author": hit.get("author"),
                    "created_at": hit.get("created_at"),
                    "num_comments": hit.get("num_comments", 0),
                    "hn_link": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                }
            )
            if len(collected) >= n:
                break

        nb_pages = data.get("nbPages", 1)
        if page >= nb_pages - 1:
            break
        page += 1

    # Sort by points descending and re-rank
    collected.sort(key=lambda x: x["points"], reverse=True)
    for i, item in enumerate(collected[:n], 1):
        item["rank"] = i

    return collected[:n]


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def process_year(year: int, check_urls: bool, output_dir: str) -> None:
    now = datetime.now(tz=timezone.utc)

    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{year}.json")

    # Resume: load existing data if file already exists
    if os.path.exists(output_path):
        with open(output_path) as f:
            year_data = json.load(f)
    else:
        year_data = {}

    months = range(1, 13)
    for month in months:
        # Only finished months: a stored month is skipped on re-runs, so it must be complete
        if (year, month) >= (now.year, now.month):
            break

        month_key = f"{year}-{month:02d}"
        if month_key in year_data:
            print(f"  {month_key} already fetched, skipping.")
            continue

        month_name = datetime(year, month, 1).strftime("%B")
        print(f"  Fetching {month_name} {year} ...", end=" ", flush=True)

        articles = fetch_top_for_month(year, month)
        print(f"{len(articles)} articles", flush=True)

        if check_urls:
            for i, article in enumerate(articles, 1):
                url = article.get("url")
                if url:
                    liveness = check_url(url)
                    article["liveness"] = liveness

                    if liveness["alive"]:
                        article["wayback_url"] = None
                    else:
                        article["wayback_url"] = wayback_snapshot(url)

                    status_str = (
                        f"HTTP {liveness['status']}"
                        if liveness["status"]
                        else liveness.get("error", "?")
                    )
                    alive_str = {True: "alive", False: "dead", None: "unknown"}[liveness["alive"]]
                    wayback_str = f" -> {article['wayback_url']}" if article.get("wayback_url") else ""
                    print(
                        f"    [{i:02d}/{len(articles)}] {alive_str} ({status_str}){wayback_str} {url[:60]}",
                        flush=True,
                    )
                else:
                    article["liveness"] = {"alive": None, "status": None, "error": "ask-hn/no-url"}
                    article["wayback_url"] = None

        year_data[month_key] = articles
        time.sleep(REQUEST_DELAY)

        # Persist after every month so partial progress is not lost
        with open(output_path, "w") as f:
            json.dump(year_data, f, indent=2, ensure_ascii=False)

    print(f"  Saved -> {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch top HN articles per month, year by year since 2007."
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help=f"Process a single year (default: all years {START_YEAR}-present)",
    )
    parser.add_argument(
        "--output-dir",
        default=OUTPUT_DIR,
        help=f"Directory to write JSON files (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--no-check-urls",
        action="store_true",
        help="Skip the URL liveness check (faster)",
    )
    args = parser.parse_args()

    check_urls = not args.no_check_urls
    current_year = datetime.now(tz=timezone.utc).year

    years = [args.year] if args.year else list(range(START_YEAR, current_year + 1))

    for year in years:
        print(f"\n=== {year} ===")
        process_year(year, check_urls=check_urls, output_dir=args.output_dir)

    print("\nDone.")


if __name__ == "__main__":
    main()
